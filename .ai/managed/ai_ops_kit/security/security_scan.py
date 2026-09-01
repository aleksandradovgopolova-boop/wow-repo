#!/usr/bin/env python3
"""Детерминированный security-scan для гейта security (v2.95, аудит 2.95 — ENGINEERING evidence).

Гейт security требует evidence [no_secrets, no_injection_surface, deps_approved]. Раньше в pipeline
НЕ было производителя этого evidence -> ENGINEERING честно, но всегда упирался в security. Этот
модуль даёт ДЕТЕРМИНИРОВАННУЮ часть:
  * no_secrets        — сканер секретов по изменённым файлам (regex известных форматов);
  * deps_approved     — аудит зависимостей: НОВЫЕ зависимости в манифестах против базы;
  * injection-surface — ФЛАГИ рискованных мест (eval/exec, shell=True, pickle, yaml.load, SQL f-string,
                        dangerouslySetInnerHTML, child_process). Это ВХОД для судьи, не автоприёмка.

Честная граница: сканер может ДОКАЗАТЬ отсутствие известных секретов и отсутствие НОВЫХ зависимостей
(детерминированные факты) и закрыть no_secrets/deps_approved, когда чисто. no_injection_surface —
СУЖДЕНИЕ (эвристика лишь флагит места) -> его закрывает независимый security-reviewer/человек
(writer ≠ judge), сканер только поставляет флаги. Находки -> гейт остаётся блокирующим (fail-closed).

Использование:
  security_scan.py <root> [--base <sha>]   # скан изменений против базы (или всего дерева)
  security_scan.py --selftest
Возврат 0 — ок, 1 — ошибка/находки.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# Секреты: известные форматы + generic key-in-quotes. Плейсхолдеры (xxxx/${...}/env) отсеиваем.
SECRET_PATTERNS = [
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("github_pat", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("aws_secret_access_key", re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+]{40}\b")),
    ("generic_secret_assignment",
     re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|access[_-]?key)\b\s*[:=]\s*"
                r"['\"]([A-Za-z0-9/+_\-]{16,})['\"]")),
]
# Плейсхолдеры/ссылки на env — НЕ секрет (снижаем ложные срабатывания generic-паттерна).
_PLACEHOLDER = re.compile(r"(?i)(x{6,}|\$\{?[a-z_]+\}?|<[a-z_ -]+>|your[_-]?|example|changeme|placeholder|env\[)")
# Материал ключа после заголовка PEM: base64-тело. Его отсутствие означает, что назван ФОРМАТ,
# а не выдан ключ.
_PEM_BODY = re.compile(r"[A-Za-z0-9+/]{20,}")
# Сколько строк после заголовка считать телом ключа. Настоящий PEM начинает тело
# сразу; больший запас начал бы цеплять соседний текст.
_PEM_LOOKAHEAD = 2

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
]


def _scan(text, patterns):
    out = []
    lines = text.splitlines()
    for lineno, line in enumerate(lines, 1):
        for pid, rx in patterns:
            m = rx.search(line)
            if not m:
                continue
            # ПЛЕЙСХОЛДЕР — НЕ СЕКРЕТ, И ЭТО ВЕРНО ДЛЯ ВСЕХ ПАТТЕРНОВ, а не только для generic.
            # Прежде отсев применялся к одному правилу, и `AKIAIOSFODNN7EXAMPLE` — документированный
            # ПРИМЕР самой AWS, буквально оканчивающийся на EXAMPLE, — считался утечкой ключа в
            # четырёх местах репозитория. Сканер, который на каждом прогоне находит десять «утечек»
            # и ни одна не утечка, обучает пролистывать раздел «СЕКРЕТ» целиком.
            #
            # Отсев идёт по НАЙДЕННОМУ значению, а не по строке: комментарий «# example» рядом с
            # настоящим ключом не должен его прятать.
            value = m.group(1) if m.groups() else m.group(0)
            if _PLACEHOLDER.search(value):
                continue
            if pid == "private_key_block":
                # Заголовок PEM без материала ключа — упоминание ФОРМАТА, а не ключ. Так он и стоит
                # в CHANGELOG, в манифесте и в отчёте аудита: перечислением того, что ищет детектор.
                # Секрет — байты ключа, и без них флаг ничего не охраняет.
                #
                # ТЕЛО ИЩЕТСЯ И НА СЛЕДУЮЩИХ СТРОКАХ, а не только в хвосте текущей: в НАСТОЯЩЕМ
                # PEM-файле заголовок стоит отдельной строкой, и проверка только своей строки
                # пропустила бы ровно тот случай, ради которого правило существует. Поймано
                # тестом «настоящий приватный ключ всё ещё находится».
                tail = [line[m.end():]] + lines[lineno:lineno + _PEM_LOOKAHEAD]
                if not any(_PEM_BODY.search(t) for t in tail):
                    continue
            out.append({"id": pid, "line": lineno})
    return out


def scan_secrets(files):
    """files: {path: content} -> список находок секретов [{path, id, line}]."""
    res = []
    for path, text in files.items():
        for f in _scan(text, SECRET_PATTERNS):
            res.append({"path": path, **f})
    return res


# R-40: исполнение команд в Node. Отличить `/re/.exec(s)` от `child_process.exec("rm -rf /")` одной
# построчной регуляркой нельзя — обе строки выглядят как `.exec(`. Различает ПОЛУЧАТЕЛЬ вызова, а он
# объявлен в другом месте файла (import/require), поэтому правило работает на уровне файла, а не строки.
_CHILD_PROCESS_IMPORT = re.compile(
    r"""(?:require\s*\(\s*['"](?:node:)?child_process['"]|"""
    r"""from\s+['"](?:node:)?child_process['"]|"""
    r"""import\s+[^\n;]*['"](?:node:)?child_process['"])""")
_NODE_EXEC_CALL = re.compile(r"\b(?:exec|execSync|execFile|execFileSync)\s*\(")


# ─── что НЕ является injection-поверхностью ───────────────────────────────────────────────────
#
# Проза — не исполняемый код. `dangerouslySetInnerHTML`, упомянутый в CHANGELOG, ничего не
# исполняет; флаг на нём — не осторожность, а шум.
_PROSE_SUFFIXES = (".md", ".rst", ".txt")

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
}


def _is_detector_own(rel: str) -> bool:
    return rel.replace("\\", "/") in DETECTOR_OWN_MATERIAL


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
        for f in _scan(text, INJECTION_PATTERNS):
            res.append({"path": path, **f})
        # Файл тянет child_process -> любой exec-вызов в нём считаем исполнением команды.
        # Пере-срабатывание здесь безопасно (лишний needs_review), под-срабатывание — нет.
        if _CHILD_PROCESS_IMPORT.search(text):
            for lineno, line in enumerate(text.splitlines(), 1):
                if _NODE_EXEC_CALL.search(line):
                    res.append({"path": path, "id": "node_child_process_exec", "line": lineno})
    return res



# ─── зависимости из TOML ──────────────────────────────────────────────────────────────────────
#
# ПОЧЕМУ ЗДЕСЬ ОТДЕЛЬНЫЙ РАЗБОР, А НЕ РЕГУЛЯРКА ПО ВСЕМУ ФАЙЛУ. Прежде имена искались по всему
# тексту образцами `"имя" =` и `имя = "`, без оглядки на секцию. На собственном репозитории кита
# это давало 18 «новых зависимостей», и ВСЕ 18 были ключами настроек: `name`, `version`, `license`,
# `edition`, `target-version`, `addopts`, `requires-python`, `tag_format`…
#
# Цена измерена: `security` — один из восьми блокирующих гейтов MVP, и проверка, ложная на 100% в
# одной из трёх своих категорий, учит игнорировать себя ЦЕЛИКОМ. Ложная тревога дороже молчания:
# молчание не притворяется работой.
#
# Секции объявлены СПИСКОМ: «всё, что похоже на пару имя-значение» — это не про зависимости.

# pyproject.toml: где действительно живут зависимости.
_PY_ARRAY_KEYS = (("project", "dependencies"), ("build-system", "requires"))
# Секции-ТАБЛИЦЫ, где ключ и есть имя пакета. `project.optional-dependencies` сюда НЕ входит:
# там ключ — имя группы (`dev`, `test`), а зависимости лежат в массиве-значении.
_PY_TABLE_SECTIONS = ("tool.poetry.dependencies", "tool.poetry.dev-dependencies")
# Cargo.toml: секции-таблицы, где ключ — имя крейта.
_CARGO_SECTIONS = ("dependencies", "dev-dependencies", "build-dependencies")

_PEP508_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _requirement_name(spec: str) -> str:
    """`pyyaml>=6.0,<7` -> `pyyaml`; `serde = { version = "1" }` уже разобран вызывающим."""
    m = _PEP508_NAME.match(str(spec))
    return m.group(1).lower() if m else ""


def _is_dep_section(name: str, section: str) -> bool:
    """Секция таблицы, в которой КЛЮЧ — это имя зависимости."""
    if name == "Cargo.toml":
        # `[dependencies]`, `[dev-dependencies]`, `[target.'cfg(...)'.dependencies]`
        return section in _CARGO_SECTIONS or section.split(".")[-1] in _CARGO_SECTIONS
    if section in _PY_TABLE_SECTIONS:
        return True
    # `[tool.poetry.group.<имя>.dependencies]`
    return section.startswith("tool.poetry.group.") and section.endswith(".dependencies")


def _toml_dep_names(text: str, manifest_name: str = "pyproject.toml") -> set:
    """Имена зависимостей из TOML. Секции объявлены, всё прочее не считается зависимостью.

    РАЗБОР ОДИН, БЕЗ `tomllib`. Он появился в stdlib только с 3.11, а объявленный пол кита — 3.9
    (`requires-python`), и собственный `validate_python_compat` этот импорт отклоняет. Два пути
    разбора означали бы ещё и два поведения: на 3.11 один ответ, на 3.9 другой — ровно тот класс
    «у меня работает», против которого стоит охват `compatibility-matrix`.

    Первая версия этой правки имела оба пути; расхождение между ними тест поймал сразу (фолбэк
    принимал имя ГРУППЫ `dev`/`test` за пакет). Это и есть довод: сверять два разбора дешевле не
    получается, а один разбор сверять не с чем — он просто один.
    """
    return _toml_dep_names_scanned(text, manifest_name)


_SECTION = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]\s*$")
_ARRAY_KEY = re.compile(r"^\s*([A-Za-z0-9._-]+)\s*=\s*\[")
_TABLE_KEY = re.compile(r"^\s*([A-Za-z0-9._\"'-]+)\s*=")
_QUOTED = re.compile(r"[\"']([^\"']+)[\"']")


def _toml_dep_names_scanned(text: str, manifest_name: str) -> set:
    """Фолбэк для Python 3.9/3.10 и для битого TOML: тот же ответ, построчным сканером.

    Секция отслеживается, потому что именно её отсутствие и было дефектом: ключ `name` в
    `[project]` — это имя проекта, а не пакет. Две формы записи различаются, и это не мелочь:
    в `[project.optional-dependencies]` ключ — имя ГРУППЫ (`dev`, `test`), а зависимости лежат
    в массиве-значении. Считать ключ именем пакета значило бы заменить одни ложные находки
    другими.
    """
    deps, section, in_array = set(), "", False

    def take_specs(fragment):
        for q in _QUOTED.findall(fragment):
            deps.add(_requirement_name(q))

    for raw in text.splitlines():
        line = "" if raw.strip().startswith("#") else raw.split("#", 1)[0]
        m = _SECTION.match(line)
        if m:
            section, in_array = m.group(1).strip().strip("\"'"), False
            continue
        if in_array:
            take_specs(line)
            if "]" in line:
                in_array = False
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().strip("\"'")
        if value.lstrip().startswith("["):
            # Массив спецификаций — только в объявленных местах.
            if (section, key) in _PY_ARRAY_KEYS or section == "project.optional-dependencies":
                take_specs(value)
                in_array = "]" not in value
            continue
        if _is_dep_section(manifest_name, section):
            deps.add(key.lower())
    return deps - {""}


def _dep_names(path, text):
    """Множество имён зависимостей из манифеста (по типу файла). Best-effort, детерминированно."""
    name = Path(path).name
    deps = set()
    if name == "package.json":
        try:
            data = json.loads(text)
            for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                deps |= set((data.get(key) or {}).keys())
        except json.JSONDecodeError:
            pass
    elif name == "requirements.txt":
        for ln in text.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                # maxsplit=1 по имени: позиционная передача объявлена устаревшей в Python 3.13
                # и подлежит удалению. Пол объявлен (3.9), потолка у requires-python нет —
                # значит кит однажды поедет на интерпретаторе, где это TypeError.
                deps.add(re.split(r"[<>=!~\[ ]", ln, maxsplit=1)[0].strip().lower())
    elif name == "go.mod":
        # обе формы: однострочная `require github.com/x/y v1.2.3` и блок `require ( ... )`
        for m in re.finditer(r"^\s*(?:require\s+)?([\w][\w./\-]+)\s+v\d", text, re.M):
            if m.group(1) != "require":
                deps.add(m.group(1))
    elif name in ("pyproject.toml", "Cargo.toml"):
        deps |= _toml_dep_names(text, name)
    return deps


def new_dependencies(before, after):
    """before/after: {manifest_path: content}. -> отсортированный список НОВЫХ имён зависимостей."""
    added = set()
    for path, after_text in after.items():
        before_names = _dep_names(path, before.get(path, ""))
        added |= (_dep_names(path, after_text) - before_names)
    return sorted(added)


def _dep_specs(path, text):
    """{name: version|None} из манифеста (версия best-effort: requirements '==', package.json значение)."""
    name = Path(path).name
    specs = {}
    if name == "package.json":
        try:
            data = json.loads(text)
            for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                for k, v in (data.get(key) or {}).items():
                    specs[k] = str(v)
        except json.JSONDecodeError:
            pass
    elif name == "requirements.txt":
        for ln in text.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                nm = re.split(r"[<>=!~\[ ]", ln, maxsplit=1)[0].strip().lower()  # см. выше про 3.13
                mv = re.search(r"==\s*([0-9][\w.\-]*)", ln)
                specs[nm] = mv.group(1) if mv else None
    else:
        for nm in _dep_names(path, text):
            specs[nm] = None
    return specs


def new_dependencies_detailed(before, after):
    """v3.0-rc5 (P1.2): НОВЫЕ зависимости с деталями для fingerprint approval.
    -> [{name, version, manifest, operation:'add'}] (отсортировано по manifest, name)."""
    out = []
    for path in sorted(after):
        b, a = _dep_specs(path, before.get(path, "")), _dep_specs(path, after[path])
        for nm in sorted(set(a) - set(b)):
            out.append({"name": nm, "version": a.get(nm), "manifest": Path(path).name, "operation": "add"})
    return out


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
    r = subprocess.run(["git", "-C", str(root), "diff", "--name-only", f"{base}..HEAD"],
                       capture_output=True, text=True)
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
    r = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{rel}"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


DEP_MANIFESTS = ("package.json", "requirements.txt", "go.mod", "pyproject.toml", "Cargo.toml")


def scan_repo(root, base=None):
    """Скан изменений против базы (или всего дерева, если base=None/не git). -> отчёт + evidence."""
    root = Path(root)
    changed = _git_changed_files(root, base) if base else None
    if changed is None:
        # не git / нет базы: сканируем отслеживаемые текстовые файлы целиком (best-effort)
        r = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True)
        changed = [ln for ln in r.stdout.splitlines() if ln.strip()] if r.returncode == 0 else []
    files = _read_files(root, changed)
    secrets = scan_secrets(files)
    injections = scan_injection(files)
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
    return {"schema_version": 1, "kind": "security-scan",
            "scanned_files": len(files), "secrets": secrets,
            "injection_flags": injections, "new_dependencies": new_deps,
            "dependencies_compared": deps_compared,
            "evidence": ev}


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
        print(f"SECURITY-SCAN: файлов {rep['scanned_files']} · секретов {len(rep['secrets'])} · "
              f"injection-флагов {len(rep['injection_flags'])} · новых зависимостей {len(rep['new_dependencies'])}")
        for s in rep["secrets"]:
            print(f"  СЕКРЕТ {s['id']} — {s['path']}:{s['line']}")
        if not rep["dependencies_compared"]:
            print("  зависимости: сравнивать не с чем — база не задана (--base <ревизия>); "
                  "это НЕ «новых нет»")
        for d in rep["new_dependencies"]:
            print(f"  НОВАЯ ЗАВИСИМОСТЬ {d} (нужно одобрение)")
    # ненулевой код при находках секретов/новых зависимостей (injection-флаги — не фейл сами по себе)
    return 1 if (rep["secrets"] or rep["new_dependencies"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
