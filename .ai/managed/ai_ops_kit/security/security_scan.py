#!/usr/bin/env python3
"""Детерминированный security-scan для гейта security (v2.95, аудит 2.95 — ENGINEERING evidence).

Гейт security требует evidence [no_secrets, no_injection_surface, deps_approved]. Раньше в pipeline
НЕ было производителя этого evidence -> ENGINEERING честно, но всегда упирался в security. Этот
модуль даёт ДЕТЕРМИНИРОВАННУЮ часть:
  * no_secrets        — сканер секретов по изменённым файлам (regex известных форматов);
  * deps_approved     — аудит зависимостей: НОВЫЕ зависимости в манифестах против базы;
  * injection-surface — ФЛАГИ рискованных мест (eval/exec, shell=True, pickle, yaml.load, SQL f-string
                        и SQL через шаблонный литерал JS, new Function/vm.runIn*Context, XSS-стоки DOM,
                        child_process). Это ВХОД для судьи, не автоприёмка.

Честная граница: сканер может ДОКАЗАТЬ отсутствие известных секретов и отсутствие НОВЫХ зависимостей
(детерминированные факты) и закрыть no_secrets/deps_approved, когда чисто. no_injection_surface —
СУЖДЕНИЕ (эвристика лишь флагит места) -> его закрывает независимый security-reviewer/человек
(writer ≠ judge), сканер только поставляет флаги. Находки -> гейт остаётся блокирующим (fail-closed).

Использование:
  security_scan.py <root> [--base <sha>]   # скан изменений против базы (или всего дерева)
Возврат 0 — ок, 1 — ошибка/находки.

Проверки модуля — `pytest tests/unit/test_security_scan*.py` (AGENTS.md: selftest не живёт в
продакшн-модуле — модули ai_ops_kit/ едут в child-репозиторий). Там у КАЖДОГО правила детектора
есть образец и безобидный двойник, и правило без них краснит набор. До #1096 здесь была названа
самопроверка одной командой, которой не существовало: описание обещало запуск, которого нельзя
было сделать. Сверку «обещано ⊆ принимается argparse» держит
tests/unit/test_security_scan_promises_are_runnable.py.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# security_scan.py запускается КАК СКРИПТ (`python3 ai_ops_kit/security/security_scan.py --base …`
# в CI), поэтому НЕ импортирует пакет ai_ops_kit (иначе ModuleNotFoundError: sys.path[0] — каталог
# скрипта, не корень). Git-вызовы здесь — raw subprocess с ЯВНЫМ timeout=: инвариант «git не висит
# вечно» держится таймаутом, а не импортом gitio. Ратчет test_no_unbounded_git это допускает.

# Секреты: известные форматы + generic key-in-quotes. Плейсхолдеры (xxxx/${...}/env) отсеиваем.
#
# СПИСОК ФОРМАТОВ ЗДЕСЬ БОЛЬШЕ НЕ ОБЪЯВЛЯЕТСЯ (#1097). Он живёт в `ai_ops_kit/shared/secret_formats.py`
# и оттуда же его читают скрабы вывода (`engine.tool_broker`) и тела PR (`delivery.pr_open`). Пока
# копий было две, они разошлись: детектор знал строку подключения с паролем, JWT, npm- и LLM-ключи,
# а скраб тела PR — нет, и кит печатал наружу формат, который сам умеет опознавать.
#
# ГРУЗИМ ДВУМЯ ПУТЯМИ, И ЭТО НЕ ПЕРЕСТРАХОВКА. Этот файл работает в двух режимах:
#   * как МОДУЛЬ ПАКЕТА (`from ai_ops_kit.security import security_scan`) — обычный импорт;
#   * как СКРИПТ (`python3 ai_ops_kit/security/security_scan.py --base …` в CI) — пакета в
#     sys.path нет (sys.path[0] — каталог скрипта), обычный импорт дал бы ModuleNotFoundError.
# Во втором режиме источник истины грузится ПО ПУТИ от `__file__`. Именно поэтому в
# `shared/secret_formats.py` нет ни одного импорта из `ai_ops_kit` — иначе загрузка по пути
# развалилась бы, и сканер перестал бы запускаться в CI.
try:
    from ai_ops_kit.shared.secret_formats import SECRET_FORMATS as _SECRET_FORMATS
except ImportError:                                    # запуск КАК СКРИПТ: пакета в sys.path нет
    import importlib.util as _ilu

    _formats_path = Path(__file__).resolve().parents[1] / "shared" / "secret_formats.py"
    _spec = _ilu.spec_from_file_location("ai_ops_secret_formats", _formats_path)
    if _spec is None or _spec.loader is None:          # fail-closed: без форматов сканер не сканер
        raise RuntimeError(f"не удалось загрузить форматы секретов из {_formats_path}") from None
    _formats_mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_formats_mod)
    _SECRET_FORMATS = _formats_mod.SECRET_FORMATS

# Имя сохранено: под ним список читают `engine.tool_broker._scrub_output`, `pipeline_readiness` и
# тесты. Это ТОТ ЖЕ объект, что и `shared.secret_formats.SECRET_FORMATS`, а не его копия.
SECRET_PATTERNS = _SECRET_FORMATS


INJECTION_PATTERNS = [
    # R-40: было `\b(?:eval|exec)\s*\(` — граница слова стоит между точкой и `e`, поэтому паттерн
    # ловил `.exec(`, то есть ШТАТНЫЙ JS-API регулярок (`/re/.exec(s)`). Писался он под Python, где
    # `exec(` — встроенная функция. Цена ошибки замерена в поле (ии-среда): две находки на
    # `RegExp.exec` подняли ТРИ домена сразу (input_validation, network_ssrf, ai_prompt_injection)
    # и заблокировали security-гейт; в продукте 15 файлов используют `.exec(`.
    # Теперь: `eval(` и `exec(` ловятся как самостоятельные вызовы, `.eval(` — тоже (у него нет
    # безобидного смысла: `window.eval`/`global.eval` — тот же eval), а `.exec(` сам по себе — нет.
    # ЧЕСТНО про то, что при этом НЕ теряется: сам импорт `child_process` ловился и раньше
    # правилом `node_child_process` (строка ниже), то есть домен поднимался в любом случае. Новое
    # правило `node_child_process_exec` добавляет не факт опасности, а её АДРЕС — строку, где
    # команда реально исполняется, — и закрывает пропуск старого паттерна: префикс `node:`
    # (`require("node:child_process")`) он не матчил вовсе.
    ("eval_or_exec", re.compile(r"(?:(?<![.\w])(?:eval|exec)\s*\(|\.\s*eval\s*\()")),
    ("subprocess_shell_true", re.compile(r"(?:subprocess\.\w+|Popen)\s*\([^)]*shell\s*=\s*True")),
    ("os_system", re.compile(r"\bos\.system\s*\(")),
    ("pickle_loads", re.compile(r"\bpickle\.loads?\s*\(")),
    ("yaml_unsafe_load", re.compile(r"\byaml\.load\s*\((?![^)]*Loader)")),
    ("sql_fstring_execute", re.compile(r"(?i)\bexecute(?:many)?\s*\(\s*f['\"]")),
    ("react_dangerous_html", re.compile(r"dangerouslySetInnerHTML")),
    # R-40: добавлен префикс `node:` — современная форма импорта (`require("node:child_process")`,
    # `from "node:child_process"`) не матчилась вовсе, то есть в новом коде правило молчало.
    ("node_child_process",
     re.compile(r"require\(\s*['\"](?:node:)?child_process['\"]\s*\)|from\s+['\"](?:node:)?child_process['\"]")),
    ("dom_innerhtml_assign", re.compile(r"\.innerHTML\s*=")),
    # ── #1094: динамическое исполнение в Node/браузере мимо `eval(`. Оба места — вход для судьи,
    # а не приговор: `new Function` встречается и в шаблонизаторах. `(?!=)` тут не нужен —
    # это вызовы, а не присваивания.
    ("js_new_function", re.compile(r"\bnew\s+Function\s*\(")),
    ("node_vm_run_in_context", re.compile(r"\bvm\s*\.\s*runIn(?:New|This)Context\s*\(")),
    # ── #1094: XSS-стоки помимо `.innerHTML =` и `dangerouslySetInnerHTML`. `(?!=)` отсекает
    # СРАВНЕНИЕ (`if (el.outerHTML === s)`) — оно ничего не записывает в DOM.
    ("dom_outerhtml_assign", re.compile(r"\.outerHTML\s*=(?!=)")),
    ("dom_insert_adjacent_html", re.compile(r"\.insertAdjacentHTML\s*\(")),
    ("dom_document_write", re.compile(r"\bdocument\s*\.\s*write(?:ln)?\s*\(")),
    ("vue_v_html", re.compile(r"\bv-html\s*=")),
]


def _scan(text, patterns):
    out = []
    lines = text.splitlines()
    for lineno, line in enumerate(lines, 1):
        for pid, rx in patterns:
            # ВСЕ СОВПАДЕНИЯ НА СТРОКЕ, А НЕ ПЕРВОЕ (#1138). Прежде бралось первое, и погашенное
            # первое прятало всё остальное: `dev=…@localhost/d prod=…@db.prod.io/a` молчал целиком —
            # отсев снимал петлевой dev, а до боевого prod дело не доходило. Пока гасить было почти
            # нечем, дефект оставался недостижимым; четыре новых класса отсева сделали его
            # достижимым — нашло независимое ревью. Адрес на строку по-прежнему один на правило.
            найдено_на_строке = False
            for m in rx.finditer(line):
                # ПЛЕЙСХОЛДЕР — НЕ СЕКРЕТ, И ЭТО ВЕРНО ДЛЯ ВСЕХ ПАТТЕРНОВ, а не только для generic.
                # Прежде отсев применялся к одному правилу, и `AKIAIOSFODNN7EXAMPLE` — документированный
                # ПРИМЕР самой AWS, буквально оканчивающийся на EXAMPLE, — считался утечкой ключа в
                # четырёх местах репозитория. Сканер, который на каждом прогоне находит десять «утечек»
                # и ни одна не утечка, обучает пролистывать раздел «СЕКРЕТ» целиком.
                #
                # Отсев идёт по НАЙДЕННОМУ значению, а не по строке: комментарий «# example» рядом с
                # настоящим ключом не должен его прятать.
                value = m.group(1) if m.groups() else m.group(0)
                if _looks_like_placeholder(value):
                    continue
                if pid == "db_connection_string_password" and _is_loopback_dsn(line[m.end():]):
                    continue                       # адрес на своей машине — отзывать нечего
                if pid == "private_key_block" and not _pem_header_has_body(
                        line[m.end():], lines[lineno:lineno + _PEM_LOOKAHEAD]):
                    continue                   # заголовок без байтов ключа — упоминание формата
                if найдено_на_строке:
                    continue
                найдено_на_строке = True
                out.append({"id": pid, "line": lineno})
    return out


def scan_secrets(files):
    """files: {path: content} -> список находок секретов [{path, id, line}].

    ПРОЗА ЗДЕСЬ НЕ ИСКЛЮЧАЕТСЯ — и это отличие от скана injection (#1138). Правило разное по
    смыслу: код в документации не исполняется, а пароль в документации — всё ещё утёкший пароль.
    Проверено прямо: с исключением прозы настоящий `AKIA…` в `README.md` переставал находиться, то
    есть блокирующая проверка приобретала ПОД-срабатывание — худший вид ошибки здесь. Сторож —
    `tests/unit/test_security_scan_secret_false_blocks.py`.

    Ложные блокировки сняты в `scan_secret_noise`: там отсев говорит «это не секрет» по САМОМУ
    значению, а не по тому, где оно лежит. Цена ошибки: находка секрета возвращает ненулевой код и
    БЛОКИРУЕТ гейт — на реальном продукте это было 17 находок, 0 настоящих утечек и каждое четвёртое
    изменение (129 из 500). Стало 0.
    """
    res = []
    for path, text in files.items():
        # СОБСТВЕННЫЙ МАТЕРИАЛ ДЕТЕКТОРА ЗДЕСЬ НЕ ПРОЩАЕТСЯ — решение v3.0.4 в силе, и ревью
        # показало, чего стоила бы его отмена: по замеру этот класс не гасил НИ ОДНОЙ из 17 находок,
        # а настоящий ключ, закоммиченный в файл детектора, переставал находиться.
        for f in _scan(text, SECRET_PATTERNS):
            res.append({"path": path, **f})
    return res


# R-40: исполнение команд в Node. Отличить `/re/.exec(s)` от `child_process.exec("rm -rf /")` одной
# построчной регуляркой нельзя — обе строки выглядят как `.exec(`. Различает ПОЛУЧАТЕЛЬ вызова, а он
# объявлен в другом месте файла (import/require), поэтому правило работает на уровне файла, а не строки.
# ─── что НЕ является injection-поверхностью ───────────────────────────────────────────────────
#
# Разбор «код или проза» вынесен в сателлит `scan_prose`: после того как комментарии перестали
# считаться кодом (#1112), модуль перешагнул порог монолита в 700 строк, и ратчет размера сказал об
# этом раньше, чем это заметил бы человек. Здесь — фасад: имена те же, поведение то же.
#
# ГРУЗИТСЯ ДВУМЯ ПУТЯМИ — по той же причине и тем же способом, что и форматы секретов выше: этот
# файл запускается и как модуль пакета, и КАК СКРИПТ (`python3 ai_ops_kit/security/security_scan.py`
# в CI), где пакета в sys.path нет. Поэтому в `scan_prose.py` нет ни одного импорта из `ai_ops_kit`:
# иначе загрузка по пути развалилась бы, и сканер перестал бы запускаться.
try:
    from ai_ops_kit.security.scan_sql_template import (
        sql_template_literal_lines as _sql_template_literal_lines,
    )
    from ai_ops_kit.security.scan_secret_noise import PEM_LOOKAHEAD as _PEM_LOOKAHEAD
    from ai_ops_kit.security.scan_secret_noise import is_loopback_dsn as _is_loopback_dsn
    from ai_ops_kit.security.scan_secret_noise import pem_header_has_body as _pem_header_has_body
    from ai_ops_kit.security.scan_secret_noise import looks_like_placeholder as _looks_like_placeholder
except ImportError:                                    # запуск КАК СКРИПТ: пакета в sys.path нет
    import importlib.util as _ilu3

    _noise_path = Path(__file__).resolve().parent / "scan_secret_noise.py"
    _spec3 = _ilu3.spec_from_file_location("ai_ops_scan_secret_noise", _noise_path)
    if _spec3 is None or _spec3.loader is None:        # fail-closed: без отсева сканер блокирует зря
        raise RuntimeError(f"не удалось загрузить отсев не-секретов из {_noise_path}") from None
    _noise_mod = _ilu3.module_from_spec(_spec3)
    _spec3.loader.exec_module(_noise_mod)
    _looks_like_placeholder = _noise_mod.looks_like_placeholder
    _is_loopback_dsn = _noise_mod.is_loopback_dsn
    _pem_header_has_body = _noise_mod.pem_header_has_body
    _PEM_LOOKAHEAD = _noise_mod.PEM_LOOKAHEAD

    _sql_path = Path(__file__).resolve().parent / "scan_sql_template.py"
    _spec4 = _ilu3.spec_from_file_location("ai_ops_scan_sql_template", _sql_path)
    if _spec4 is None or _spec4.loader is None:        # fail-closed: без правила сканер слепнет
        raise RuntimeError(f"не удалось загрузить правило SQL-шаблона из {_sql_path}") from None
    _sql_mod = _ilu3.module_from_spec(_spec4)
    _spec4.loader.exec_module(_sql_mod)
    _sql_template_literal_lines = _sql_mod.sql_template_literal_lines

try:
    from ai_ops_kit.security.scan_exec_call import child_process_imported as _child_process_imported
    from ai_ops_kit.security.scan_exec_call import launch_findings as _launch_findings
    from ai_ops_kit.security.scan_inline_code import inline_code_lines as _inline_code_lines
    from ai_ops_kit.security.scan_prose import PROSE_SUFFIXES as _PROSE_SUFFIXES
    from ai_ops_kit.security.scan_prose import area_of as _area_of
    from ai_ops_kit.security.scan_prose import blank_string_contents as _blank_strings
    from ai_ops_kit.security.scan_prose import blank_comments as _blank_comments
    from ai_ops_kit.security.scan_vendor import VERSION_FILE as _VENDOR_VERSION_FILE
    from ai_ops_kit.security.scan_vendor import arrivals_note as _arrivals_note
    from ai_ops_kit.security.scan_vendor import mark_arrivals as _mark_arrivals
except ImportError:                                    # запуск КАК СКРИПТ: пакета в sys.path нет
    import importlib.util as _ilu2

    _prose_path = Path(__file__).resolve().parent / "scan_prose.py"
    _spec2 = _ilu2.spec_from_file_location("ai_ops_scan_prose", _prose_path)
    if _spec2 is None or _spec2.loader is None:        # fail-closed: без разбора прозы сканер шумит
        raise RuntimeError(f"не удалось загрузить разбор прозы из {_prose_path}") from None
    _prose_mod = _ilu2.module_from_spec(_spec2)
    _spec2.loader.exec_module(_prose_mod)
    _PROSE_SUFFIXES = _prose_mod.PROSE_SUFFIXES
    _blank_comments = _prose_mod.blank_comments
    _area_of = _prose_mod.area_of
    _blank_strings = _prose_mod.blank_string_contents

    _exec_path = Path(__file__).resolve().parent / "scan_exec_call.py"
    _spec7 = _ilu2.spec_from_file_location("ai_ops_scan_exec_call", _exec_path)
    if _spec7 is None or _spec7.loader is None:        # fail-closed: без разбора сканер слепнет
        raise RuntimeError(f"не удалось загрузить разбор запуска команд из {_exec_path}") from None
    _exec_mod = _ilu2.module_from_spec(_spec7)
    _spec7.loader.exec_module(_exec_mod)
    _child_process_imported = _exec_mod.child_process_imported
    _launch_findings = _exec_mod.launch_findings

    _inline_path = Path(__file__).resolve().parent / "scan_inline_code.py"
    _spec8 = _ilu2.spec_from_file_location("ai_ops_scan_inline_code", _inline_path)
    if _spec8 is None or _spec8.loader is None:        # fail-closed: без разбора сканер слепнет
        raise RuntimeError(f"не удалось загрузить разбор inline-кода из {_inline_path}") from None
    _inline_mod = _ilu2.module_from_spec(_spec8)
    _spec8.loader.exec_module(_inline_mod)
    _inline_code_lines = _inline_mod.inline_code_lines

    _vendor_path = Path(__file__).resolve().parent / "scan_vendor.py"
    _spec6 = _ilu2.spec_from_file_location("ai_ops_scan_vendor", _vendor_path)
    if _spec6 is None or _spec6.loader is None:        # fail-closed: без сверки версий раздел слеп
        raise RuntimeError(f"не удалось загрузить сверку поставки из {_vendor_path}") from None
    _vendor_mod = _ilu2.module_from_spec(_spec6)
    _spec6.loader.exec_module(_vendor_mod)
    _mark_arrivals = _vendor_mod.mark_arrivals
    _arrivals_note = _vendor_mod.arrivals_note
    _VENDOR_VERSION_FILE = _vendor_mod.VERSION_FILE

# СОБСТВЕННЫЙ МАТЕРИАЛ ДЕТЕКТОРА. Файл, который ОБЪЯВЛЯЕТ образцы, и тесты, которые их ПОДСОВЫВАЮТ,
# по построению содержат всё, что детектор ищет. Замер 19.08.2026: 55 флагов из 72 приходились
# ровно на них — то есть на 76% сканер читал сам себя. Список объявлен ПОИМЁННО и вправе только
# сокращаться (охрана — tests/unit/test_security_scan_tells_the_truth.py); каталогам целиком
# прощения здесь нет, иначе исключение стало бы складом.
#
# Секретов это НЕ касается: там ложные находки убраны по-настоящему — фикстуры собираются в
# рантайме из фрагментов (решение v3.0.4), а не прощаются списком.
DETECTOR_OWN_MATERIAL = {
    "ai_ops_kit/security/security_scan.py": "объявляет сами образцы injection и секретов",
    "tests/unit/test_security_scan.py": "подсовывает детектору образцы, чтобы проверить детекцию",
    "tests/unit/test_property_based.py": "property-based фикстуры того же детектора",
    # #1157: реестр доменов безопасности НАЗЫВАЕТ опасные конструкции как условия блокировки
    # («innerHTML/dangerouslySetInnerHTML без санитизации») — имя правила стоит там значением, а не
    # вызовом. Файл едет в дочку, и на свежей установке это был один из четырёх адресов раздела
    # поставки: сканер читал собственный реестр правил как код.
    "security/security-domains.yaml": "реестр правил детектора: имена опасных конструкций — "
                                      "значения условий блокировки, а не исполняемый код",
}


# ГДЕ ЛЕЖИТ ТОТ ЖЕ МАТЕРИАЛ В ДОЧКЕ. В материнском репозитории список выше совпадает с путями один
# в один, и там дефект считался закрытым. В ПОДКЛЮЧЁННОМ репозитории установленная копия кита лежит
# под `.ai/managed/`, поэтому тот же самый файл приходит как
# `.ai/managed/ai_ops_kit/security/security_scan.py` — точного совпадения нет, и детектор читал СВОИ
# образцы как код продукта. Замер 23.09.2026 на ии-среде: 20 флагов из 61 (33%) оказались про кит,
# из них 15 — его же `security_scan.py`. То есть проверка «сканер не читает сам себя» стояла не там,
# где сканер работает у пользователя.
#
# ПОЧЕМУ СНИМАЕТСЯ ПРЕФИКС, А НЕ СРАВНИВАЕТСЯ СУФФИКС. Прощать любой путь, ОКАНЧИВАЮЩИЙСЯ на имя из
# списка, — это та самая «складская» лазейка, против которой список объявлен поимённо: продуктовый
# файл с совпадающим хвостом прощался бы молча. Снимается ровно один известный префикс установки,
# дальше работает прежний поимённый список, и он по-прежнему вправе только сокращаться.
_MANAGED_INSTALL_PREFIX = ".ai/managed/"


def _is_detector_own(rel: str) -> bool:
    rel = rel.replace("\\", "/").removeprefix("./")
    rel = rel.removeprefix(_MANAGED_INSTALL_PREFIX)
    return rel in DETECTOR_OWN_MATERIAL


def scan_injection(files):
    """Флаги injection-surface (ВХОД для судьи, не автоприёмка) -> [{path, id, line}].

    Пере-срабатывание здесь безопаснее под-срабатывания — но у безопасности есть цена: список,
    где три четверти флагов приходятся на собственные образцы детектора, судья пролистывает
    целиком. Поэтому проза и собственный материал исключены ПОИМЁННО, а не «на глаз».
    """
    res = []
    for path, text in files.items():
        if path.lower().endswith(_PROSE_SUFFIXES) or _is_detector_own(path):
            continue
        найдено = _scan(text, INJECTION_PATTERNS)
        if найдено:
            # Разбор комментариев стоит дорого, поэтому включается ТОЛЬКО когда есть что проверять:
            # на файле без единого совпадения он ничего не изменит, а времени возьмёт столько же.
            # ОСТАЛЬНЫЕ ПРАВИЛА ПО-ПРЕЖНЕМУ ДОВЕРЯЮТ ГАШЕНИЮ КОММЕНТАРИЕВ (#1112): комментарий,
            # утверждающий ОБРАТНОЕ («рендер без dangerouslySetInnerHTML»), не должен становиться
            # находкой. Признак «разобрано» нужен не здесь, а там, где снимается флаг со строки
            # импорта: только это решение опирается на полноту разбора.
            без_комментариев, _ = _blank_comments(text, path)
            if без_комментариев != text:
                найдено = _scan(без_комментариев, INJECTION_PATTERNS)
        for f in найдено:
            res.append({"path": path, **f})
        # Файл тянет child_process -> его exec-вызовы разбираются как исполнение команд. ЧТО ИМЕННО
        # считается поверхностью, решает сателлит `scan_exec_call`: запуск с литеральным именем
        # бинаря, без оболочки и без встроенного кода безопасен ПО КОНСТРУКЦИИ — исполнится ровно
        # то, что написано в файле (#1161). Стойка «пере-срабатывание безопаснее под-срабатывания»
        # в силе: снимается ровно тот класс, где безопасность видна в самой строке вызова.
        # Вход читает СЫРОЙ текст, вызовы — очищенный, осознанно (#1153): импорт, съеденный
        # ошибочно угаданным комментарием, не должен выключать разбор. Границы — в `launch_findings`.
        if _child_process_imported(text):
            были_вызовы, вызовы = _launch_findings(text, path, _blank_comments, _blank_strings)
            for lineno in вызовы:
                res.append({"path": path, "id": "node_child_process_exec", "line": lineno})
            if были_вызовы:
                # Строка `import` найдена правилом `node_child_process` выше и теперь не нужна:
                # вызовы РАЗОБРАНЫ, и про каждый известно, поверхность он или нет. Если все они
                # безопасны по конструкции, файл уходит из флагов целиком — это и есть починка.
                # А вот когда вызовов не нашлось вовсе (переименование), импорт остаётся флагом:
                # там мы не разобрали ничего, и молчание означало бы «не проверено» вместо «чисто».
                res = [f for f in res
                       if not (f["path"] == path and f["id"] == "node_child_process")]
        for lineno in _sql_template_literal_lines(text):
            res.append({"path": path, "id": "sql_template_literal", "line": lineno})
        # #1161: интерпретатору в .py/.sh передан код, собранный подстановкой. Литеральный код флагом
        # не считается: исполнится ровно написанное (решение и цена — в `scan_inline_code`).
        for lineno in _inline_code_lines(text, path):
            res.append({"path": path, "id": "inline_code_interpolated", "line": lineno})
    # ОБЛАСТЬ — ЯРЛЫК, А НЕ ФИЛЬТР (#1146). Ни один флаг не исчезает: `harness` (тесты, e2e, конфиги
    # инструментов) отделён от `product`, чтобы судья не читал дюжину тестовых адресов ради одного
    # боевого. Ошибка классификации перекладывает адрес в другой раздел, но не прячет его.
    for f in res:
        f["area"] = _area_of(f["path"])
    return res


# ─── зависимости из манифестов ────────────────────────────────────────────────────────────────
#
# Разбор вынесен в сателлит `scan_deps` — по той же причине, что проза и отсев не-секретов: фасад
# подошёл к порогу монолита в 700 строк вплотную, и ратчет размера сказал об этом до слияния.
# Публичная поверхность НЕ переезжает: `new_dependencies`, `new_dependencies_detailed` и
# `DEP_MANIFESTS` остаются именами этого модуля (их читают `security_pack.run_pack` и
# `planning.architecture_invariants`).
#
# ГРУЗИТСЯ ДВУМЯ ПУТЯМИ — см. объяснение у форматов секретов выше: файл работает и как модуль
# пакета, и КАК СКРИПТ в CI дочки, где пакета в sys.path нет.
try:
    from ai_ops_kit.security.scan_deps import DEP_MANIFESTS
    from ai_ops_kit.security.scan_deps import new_dependencies, new_dependencies_detailed
except ImportError:                                    # запуск КАК СКРИПТ: пакета в sys.path нет
    import importlib.util as _ilu5

    _deps_path = Path(__file__).resolve().parent / "scan_deps.py"
    _spec5 = _ilu5.spec_from_file_location("ai_ops_scan_deps", _deps_path)
    if _spec5 is None or _spec5.loader is None:        # fail-closed: без разбора «новых нет» = незнание
        raise RuntimeError(f"не удалось загрузить разбор зависимостей из {_deps_path}") from None
    _deps_mod = _ilu5.module_from_spec(_spec5)
    _spec5.loader.exec_module(_deps_mod)
    new_dependencies = _deps_mod.new_dependencies
    new_dependencies_detailed = _deps_mod.new_dependencies_detailed
    DEP_MANIFESTS = _deps_mod.DEP_MANIFESTS


def security_evidence(secrets, injections, new_deps, deps_compared=True):
    """Собрать gate_ev-совместимый вердикт по частям security. Детерминированно закрываем ТОЛЬКО
    no_secrets и deps_approved (факты). no_injection_surface оставляем судье (даём флаги как вход).

    `deps_compared=False` — базы для сравнения не было, и «новых зависимостей нет» тогда не факт,
    а незнание. Кит различает их везде (`unknown != 0`), и здесь обязан различать тоже: иначе
    прогон без базы закрывал бы deps_approved бесплатно."""
    ev = {}
    ev["no_secrets"] = {"status": "pass" if not secrets else "fail",
                        "findings": secrets}
    if not deps_compared:
        ev["deps_approved"] = {"status": "needs_review", "new_dependencies": [],
                               "note": "база для сравнения не задана — сравнить манифесты не с чем; "
                                       "«новых зависимостей нет» здесь означало бы незнание, "
                                       "выданное за факт"}
    else:
        ev["deps_approved"] = {"status": "pass" if not new_deps else "fail",
                               "new_dependencies": new_deps}
    # no_injection_surface НЕ закрываем автоматически: эвристика лишь флагит. Судья (security-reviewer/
    # человек) выносит вердикт. Отдаём флаги + статус "needs_review" (чисто) или "fail" (есть флаги).
    ev["no_injection_surface"] = {"status": "needs_review" if not injections else "fail",
                                  "flags": injections,
                                  "note": "детерминированный сканер не закрывает injection-surface — "
                                          "нужен независимый security-reviewer (--review) или человек"}
    return ev


# v3.27.7: артефакты сборки/кэши (__pycache__/.pyc, .pytest_cache, node_modules, dist, ...) — НЕ исходники.
# security-скан не должен их читать: бинарный .pyc, прочитанный как текст (errors="ignore"), даёт мусор,
# который ложно совпадает с доменными regex'ами (напр. input_validation по байтам .pyc) -> ложный
# security-домен -> ложный блок security-гейта. Флаки по ОС/версии Python: в fix-loop selftest
# воспроизводилось на Linux/py3.12 (где __pycache__ попадал в diff коммита) и НЕ на macOS. Тот же класс
# исключений, что в execution_pipeline (cleanliness). Плюс страховка: файл с NUL-байтами не сканируем.
_ARTIFACT_RE = re.compile(
    r"(?:^|/)(?:__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.tox|\.nox|\.hypothesis|"
    r"node_modules|dist|build|out|coverage|\.next|\.nuxt|\.svelte-kit|\.turbo|target|\.venv|venv)(?:/|$)"
    r"|\.(?:pyc|pyo|class|o|so|dll|dylib)$|\.egg-info(?:/|$)")


def _is_artifact(rel: str) -> bool:
    return bool(_ARTIFACT_RE.search(rel))


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def _git_changed_files(root, base):
    # RAW с явным timeout= (скрипт-режим — без импорта пакета): зависший git не вешает security-скан.
    try:
        r = subprocess.run(["git", "-C", str(root), "diff", "--name-only", f"{base}..HEAD"],
                           capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def _read_files(root, rels):
    out = {}
    for rel in rels:
        if _is_artifact(rel):
            continue                                   # артефакт/байткод — не исходник, не сканируем
        p = Path(root) / rel
        if p.is_file():
            try:
                raw = p.read_bytes()
            except OSError:
                continue
            if _looks_binary(raw):
                continue                               # бинарь -> текстовый скан дал бы мусор/ложные матчи
            out[rel] = raw.decode("utf-8", errors="ignore")
    return out


def _git_show(root, ref, rel):
    # RAW, а не gitio.git: нужен ДОСЛОВНЫЙ снимок файла (`git show <ref>:<path>`), а gitio.git
    # стягивает stdout через .strip() и срезал бы ведущие/хвостовые пробелы содержимого. timeout=
    # обязателен явно — иначе зависший git повесил бы скан (тот же инвариант, что держит gitio).
    try:
        r = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{rel}"],
                           capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return ""
    return r.stdout if r.returncode == 0 else ""


def _vendor_split(injections):
    """Отделить поставленную копию кита от кода продукта. Ярлык уже проставлен `_area_of`."""
    vendor = [f for f in injections if f.get("area") == "vendor"]
    return [f for f in injections if f.get("area") != "vendor"], vendor


def _vendor_arrivals(root, base, vendor, files):
    """Пометить каждый адрес поставки: приехал с этим обновлением кита или был и в прежней версии.

    Прежний текст берётся ПОФАЙЛОВО и только для файлов, где флаги есть: на обновлении кита в дифф
    попадают сотни файлов, а адресов среди них единицы — читать базу целиком незачем."""
    out = []
    for path in sorted({f["path"] for f in vendor}):
        прежний = _git_show(root, base, path)
        было = scan_injection({path: прежний}) if прежний else []
        out += _mark_arrivals([f for f in vendor if f["path"] == path],
                              files.get(path, ""), было, прежний)
    return out


def delivered_surface(root, rels, base=None):
    """Флаги сканера по файлам поставленной копии кита (`.ai/managed/`) из `rels` -> [флаг].

    Для тела PR обновления кита (#1157): дочка узнаёт, какую поверхность привозит выпуск, ДО
    слияния, а не прогоном после. С `base` каждый флаг помечен `arrived` — приехал ли он с этим
    обновлением (сравнение то же, что в разделе поставки `scan_repo`)."""
    files = _read_files(root, [r for r in rels
                               if r.replace("\\", "/").startswith(_MANAGED_INSTALL_PREFIX)])
    vendor = _vendor_split(scan_injection(files))[1]
    return _vendor_arrivals(root, base, vendor, files) if (base and vendor) else vendor


def _vendor_version(root, base, files):
    """Версия установленного кита до и после. None — прочитать не удалось, и это не «не менялась»."""
    текущая = files.get(_VENDOR_VERSION_FILE)
    if текущая is None:
        try:
            текущая = (Path(root) / _VENDOR_VERSION_FILE).read_text(encoding="utf-8")
        except OSError:
            текущая = ""
    прежняя = _git_show(root, base, _VENDOR_VERSION_FILE) if base else ""
    return (прежняя.strip() or None), (текущая.strip() or None)


def scan_repo(root, base=None):
    """Скан изменений против базы (или всего дерева, если base=None/не git). -> отчёт + evidence."""
    root = Path(root)
    changed = _git_changed_files(root, base) if base else None
    if changed is None:
        # не git / нет базы: сканируем отслеживаемые текстовые файлы целиком (best-effort).
        # RAW с явным timeout= (скрипт-режим, см. _git_changed_files).
        try:
            r = subprocess.run(["git", "-C", str(root), "ls-files"],
                               capture_output=True, text=True, timeout=90)
            changed = [ln for ln in r.stdout.splitlines() if ln.strip()] if r.returncode == 0 else []
        except subprocess.TimeoutExpired:
            changed = []
    files = _read_files(root, changed)
    secrets = scan_secrets(files)
    # ПОСТАВЛЕННАЯ КОПИЯ КИТА — ОТДЕЛЬНЫЙ РАЗДЕЛ, НЕ ПРОЩЕНИЕ (#1147). `.ai/managed/` дочка не
    # писала и починить в своём PR не может: правка делается обновлением кита. Адреса оттуда видны
    # все до одного, но в вердикт гейта ПРОДУКТА не входят — иначе команда вечно отвечает за чужой
    # код. Обратная сторона названа тем же механизмом: новый `shell=True`, приехавший с
    # обновлением, помечается `arrived` и потому виден, а не тонет в общем списке.
    injections, vendor = _vendor_split(scan_injection(files))
    # СРАВНЕНИЕ БЫЛО ИЛИ НЕ БЫЛО — РАЗНЫЕ СОСТОЯНИЯ. Без базы «какие адреса новые» неизвестно, и
    # выдать это за «все были раньше» значило бы напечатать незнание фактом (тот же инвариант, что
    # у `dependencies_compared` ниже).
    vendor_compared = bool(base)
    if vendor_compared and vendor:
        vendor = _vendor_arrivals(root, base, vendor, files)
    vendor_before, vendor_after = _vendor_version(root, base, files)
    # зависимости: сравниваем манифесты после (рабочее дерево) против базы (git show base:)
    after_mani = {p: c for p, c in files.items() if Path(p).name in DEP_MANIFESTS}
    if not after_mani:  # манифесты могли не измениться — прочитаем текущие для полноты
        after_mani = _read_files(root, [m for m in DEP_MANIFESTS if (root / m).is_file()])
    # СРАВНИВАТЬ НЕ С ЧЕМ — ЭТО НЕ «ВСЁ НОВОЕ». Прежде при отсутствии базы `before` считался
    # пустым, и КАЖДАЯ зависимость репозитория объявлялась новой: на самом ките это давало 18
    # находок из 18 ложных вместе с разбором TOML. Проверка, ложная на 100% в одной из трёх своих
    # категорий, учит игнорировать себя целиком — а `security` один из восьми блокирующих гейтов.
    deps_compared = bool(base)
    before_mani = {p: (_git_show(root, base, p) if base else "") for p in after_mani}
    new_deps = new_dependencies(before_mani, after_mani) if deps_compared else []
    ev = security_evidence(secrets, injections, new_deps, deps_compared=deps_compared)
    приехало = sum(1 for f in vendor if f.get("arrived"))
    return {"schema_version": 1, "kind": "security-scan",
            "scanned_files": len(files), "secrets": secrets,
            "injection_flags": injections, "new_dependencies": new_deps,
            "dependencies_compared": deps_compared,
            "vendor_flags": vendor,
            "vendor_compared": vendor_compared,
            "vendor_kit_version": {"before": vendor_before, "after": vendor_after},
            "vendor_note": _arrivals_note(vendor_before, vendor_after, приехало, len(vendor),
                                         vendor_compared),
            "evidence": ev}


def _vendor_lines(rep):
    """Раздел поставленной зависимости — ОТДЕЛЬНЫЙ, НЕ СНОСКА (#1147).

    Поставленная копия кита — чужой код с чужим адресатом, и смешивать его с продуктовым списком
    значит спрашивать команду за то, чего она не писала. Приехавшие с обновлением адреса печатаются
    ПОИМЁННО: ради них раздел и заведён — иначе о новой поверхности, привезённой китом, дочка не
    узнаёт вовсе. Бывшие раньше свёрнуты числом: их адресат тот же, а новостью они не являются."""
    vendor = rep.get("vendor_flags") or []
    if not vendor:
        return []
    out = [f"  ── поставленная зависимость .ai/managed/ ({len(vendor)}) ──",
           f"     {rep['vendor_note']}"]
    if not rep.get("vendor_compared"):
        # Сравнения не было: назвать адреса ПОИМЁННО — единственное честное поведение. Свернуть их
        # числом «было раньше» значило бы утверждать то, чего никто не проверял.
        out += [f"     {f['id']} — {f['path']}:{f['line']}" for f in vendor]
        return out
    out += [f"     ПРИЕХАЛО С ОБНОВЛЕНИЕМ {f['id']} — {f['path']}:{f['line']}"
            for f in vendor if f.get("arrived")]
    было = sum(1 for f in vendor if not f.get("arrived"))
    if было:
        out.append(f"     было и в прежней версии кита: {было}; полный список в --json")
    return out


def main(argv):
    ap = argparse.ArgumentParser(prog="security_scan.py")
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--base", help="git-ревизия базы для diff (иначе — все отслеживаемые файлы)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rep = scan_repo(a.root, a.base)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        # ОТЧЁТ СОБИРАЕТСЯ СТРОКАМИ И ПЕЧАТАЕТСЯ ОДИН РАЗ. Раньше здесь было шесть `print` подряд, и
        # каждый новый раздел отчёта стоил ещё одного — ратчет print-discipline упирался в это
        # раньше, чем человек. Текст не изменился ни на символ: каждый прежний `print` печатал ровно
        # одну строку.
        s = [f"SECURITY-SCAN: файлов {rep['scanned_files']} · секретов {len(rep['secrets'])} · "
             f"injection-флагов {len(rep['injection_flags'])} · новых зависимостей {len(rep['new_dependencies'])}"]
        s += [f"  СЕКРЕТ {x['id']} — {x['path']}:{x['line']}" for x in rep["secrets"]]
        # Боевые адреса — поимённо, обвязка — одним числом: список, где на один боевой адрес
        # приходится дюжина тестовых, судья пролистывает целиком (#1146).
        боевые = [f for f in rep["injection_flags"] if f.get("area") != "harness"]
        обвязка = [f for f in rep["injection_flags"] if f.get("area") == "harness"]
        s += [f"  ПОДОЗРИТЕЛЬНОЕ МЕСТО {f['id']} — {f['path']}:{f['line']}" for f in боевые]
        if обвязка:
            s.append(f"  в обвязке (тесты, e2e, конфиги инструментов) ещё {len(обвязка)} — "
                     f"адресат тот же, срочность другая; полный список в --json")
        s += _vendor_lines(rep)
        if not rep["dependencies_compared"]:
            s.append("  зависимости: сравнивать не с чем — база не задана (--base <ревизия>); "
                     "это НЕ «новых нет»")
        s += [f"  НОВАЯ ЗАВИСИМОСТЬ {d} (нужно одобрение)" for d in rep["new_dependencies"]]
        print("\n".join(s))
    # ненулевой код при находках секретов/новых зависимостей (injection-флаги — не фейл сами по себе)
    return 1 if (rep["secrets"] or rep["new_dependencies"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
