#!/usr/bin/env python3
"""Файл ответов владельца на вопросы онбординга: чтение, разбор комментариев, перезапись.

Проб-свободный спутник `repo_audit.py` (module-size, P2): чистый перенос группы помощников,
работающих с `.ai/project/onboarding-answers.yaml`. Поведение не меняется — `repo_audit`
ре-экспортирует эти имена, внешние импортёры (`repo_audit.write_question_file`,
`from ai_ops_kit.planning.repo_audit import read_answers, answers_path, AnswersCorrupt`) работают
по-прежнему.

Единственный источник истины про формат этого файла: сюда стянуто всё, что его разбирает и
пересобирает, чтобы правило «ответ владельца не затирается, комментарий-основание не теряется»
жило в одном месте. Мутационно-охраняемых строк здесь нет (в `repo_audit` их тоже не было).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml


ANSWERS_REL = ".ai/project/onboarding-answers.yaml"


def answers_path(child_root) -> Path:
    return Path(child_root) / ANSWERS_REL


class AnswersCorrupt(Exception):
    """Файл ответов онбординга существует, но не разбирается.

    Отдельный тип, а не `return {}`: «ответов нет» и «ответы есть, но прочитать нечем» — разные
    ответы, и второй НЕ даёт права переспрашивать и перезаписывать. Ровно так был потерян
    заполненный файл в живом прогоне niti (F-023).
    """


def read_answers(child_root) -> dict:
    """Ответы человека из файла онбординга. Пустое значение — это «ещё не ответил», а не ответ."""
    p = answers_path(child_root)
    if not p.is_file():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        # НЕРАЗОБРАННЫЙ ФАЙЛ — ЭТО НЕ «ОТВЕТОВ НЕТ» (F-023, живой прогон niti 2026-08-12).
        # Прежде здесь стоял `return {}`, и это было началом потери данных: кит получал «ответов
        # нет», переспрашивал всё заново и ПЕРЕЗАПИСЫВАЛ файл пустым. Ответы владельца исчезали
        # молча — вместе с тем, что он вписал руками. Тот же инвариант, что «не смог прочитать» !=
        # «нет»: см. DeliveryReceipt и реестр гейтов.
        raise AnswersCorrupt(f"{p}: файл ответов не разбирается ({e}) — починить, а не отвечать "
                             f"заново; иначе ответы будут перезаписаны пустыми") from e
    ans = (data.get("answers") or {}) if isinstance(data, dict) else {}
    return {k: v for k, v in ans.items()
            if v not in (None, "", [], {}) and str(v).strip() not in ("", "?")}


_ANSWER_KEY_RE = re.compile(r"^ {2}([A-Za-z_][A-Za-z0-9_.-]*):(?:\s|$)")


def _inline_comment(rest: str) -> str:
    """Комментарий в хвосте строки значения. -> `# …` или пустая строка.

    Резать по первому `#` нельзя: значение пишется как JSON-строка и `#` внутри кавычек — часть
    ОТВЕТА, а не комментарий (`goal: "рост #1 по выручке"`). Поэтому кавычки считаются, экранирование
    учитывается, и решётка признаётся началом комментария только вне кавычек и после пробела —
    ровно правило YAML.
    """
    in_quotes, escaped = False, False
    for i, ch in enumerate(rest):
        if escaped:
            escaped = False
            continue
        if in_quotes and ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            continue
        if ch == "#" and not in_quotes and (i == 0 or rest[i - 1] in " \t"):
            return rest[i:].rstrip()
    return ""


def _owner_comments(text: str, kit_lines: set) -> dict:
    """Что в существующем файле написал ЧЕЛОВЕК, а не кит. -> {header, before, inline, tail}.

    ЗАЧЕМ (F-020, живой прогон niti 2026-08-12). `write_question_file` пересобирает файл целиком:
    значения переносились через `read_answers`, а комментарии — нет. Владелец вписал к каждому
    ответу источник (`file:line`), и после следующего `ai-ops model` их осталось НОЛЬ строк. Для
    файла, чья роль — «подтверждённые факты», это потеря ОСНОВАНИЯ: утверждение остаётся, а отличить
    подтверждённое от переписанного больше нечем. Обход в том прогоне — прятать ссылки внутрь
    значений — был обходом, а не решением.

    ЧЕЙ КОММЕНТАРИЙ — РЕШАЕТСЯ СРАВНЕНИЕМ С ТЕМ, ЧТО КИТ ПИШЕТ САМ (`kit_lines`), а не догадкой по
    форме. Строка, совпавшая с собственной строкой кита, принадлежит киту и будет сгенерирована
    заново; всё остальное — владельца и переносится дословно.

    ГРАНИЦА НАЗВАНА, А НЕ СПРЯТАНА: если формулировка вопроса ИЗМЕНИЛАСЬ между версиями кита, старая
    строка перестаёт совпадать с новой и будет сохранена как авторская — один раз, при первом
    прогоне после обновления. Выбор осознанный: лишняя строка видна и удаляется рукой, а потерянное
    основание не восстанавливается ничем. Обратный порядок предпочтений сделал бы ровно тот дефект,
    который здесь чинится.
    """
    header, before, inline, tail = [], {}, {}, []
    pending, in_answers = [], False
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not in_answers:
            if stripped == "answers:":
                in_answers = True
            elif stripped.startswith("#") and line not in kit_lines:
                header.append(stripped)
            continue
        m = _ANSWER_KEY_RE.match(line)
        if m:
            qid = m.group(1)
            if pending:
                before[qid] = list(pending)
                pending = []
            c = _inline_comment(line[m.end(1) + 1:])
            if c:
                inline[qid] = c
            continue
        if stripped.startswith("#") and line not in kit_lines:
            pending.append(stripped)
    tail = pending                      # комментарии после последнего ключа — тоже человеческие
    return {"header": header, "before": before, "inline": inline, "tail": tail}


def record_answer(child_root, qid, value, ask: dict, why=None):
    """Записать ОДИН ответ владельца в форму онбординга БЕЗ ручной правки YAML (issue #612).

    -> (ok: bool, message: str). Валидирует qid против пакета открытых вопросов и уже отвеченного
    (переспросить/переответить законно); why становится inline-комментарием-основанием (F-020),
    комментарии владельца при этом сохраняются существующей перезаписью. Прямая цель ROADMAP:
    «владелец больше не обязан править YAML руками».
    """
    valid = {q.get("id") for q in (ask.get("questions") or []) if q.get("id")}
    valid |= set(read_answers(child_root))
    if qid not in valid:
        opts = ", ".join(sorted(x for x in valid if x)) or "(открытых вопросов сейчас нет)"
        return False, f"Вопроса онбординга '{qid}' нет. Доступны: {opts}"
    path = write_question_file(child_root, ask, overrides={qid: value},
                              notes=({qid: why} if why else None))
    answered = read_answers(child_root)
    remaining = [q.get("id") for q in (ask.get("questions") or [])
                 if q.get("id") and q.get("id") not in answered]
    try:
        shown = path.relative_to(Path(child_root))
    except ValueError:
        shown = path
    tail = (f" Осталось вопросов: {len(remaining)}." if remaining
            else " Все онбординговые вопросы отвечены.")
    return True, (f"Записал ответ: {qid} = {value} — это подтверждённый факт, переспрашиваться "
                  f"не будет ({shown}).{tail}")


def write_question_file(child_root, ask: dict, overrides=None, notes=None):
    """Создать/дополнить файл, в который человек ВПИШЕТ ответы. -> путь.

    `overrides` (#612): {qid: value} — значения, записанные командой `model --answer`, а не рукой;
    сливаются поверх уже прочитанных ответов. `notes`: {qid: text} — основание ответа, ложится
    inline-комментарием (тот же механизм, что комментарии-основания владельца, F-020).

    Прежде `ai-ops model` печатал вопросы и ЗАВЕРШАЛСЯ: куда писать ответы, не сказано, интерактива
    нет — человек прочитал двенадцать вопросов и не может ответить. Тупик на главном шаге первого
    сценария, при том что онбординг обещал «соберу материалы и покажу на проверку».

    Существующие ответы НЕ затираются никогда: файл дополняется новыми вопросами, ответы остаются.
    ВМЕСТЕ С НИМИ ОСТАЮТСЯ КОММЕНТАРИИ ВЛАДЕЛЬЦА (F-020): и те, что стоят над ответом, и хвостовые
    в той же строке. Ответ без основания — это утверждение, которое нечем проверить, а основание
    владелец пишет именно комментарием: `main_goal_now: "…"  # docs/product.md:14`.

    ЛИШНЕЙ ЗАПИСИ НЕ ДЕЛАЕМ. Если содержимое не изменилось, файл не перезаписывается: `ai-ops model`
    зовут и просто «посмотреть состояние», и трогать mtime (а в чужом репозитории — показывать файл
    изменённым в `git status`) ради того же текста команда не вправе. Это единственный файл, который
    она создаёт, и создаёт ровно потому, что вопросам нужно место для ответа.
    """
    p = answers_path(child_root)
    # Если файл есть, но не читается — НЕ перезаписываем: это уничтожило бы ответы владельца.
    # Пусть ошибка дойдёт до человека словами «починить, а не отвечать заново» (F-023).
    existing = read_answers(child_root)
    # #612: `model --answer` подаёт значение сюда, а не человек в редакторе. Сливаем поверх уже
    # прочитанного; None (значение не задано) не затирает существующий ответ.
    if overrides:
        existing = {**existing, **{k: v for k, v in overrides.items() if v is not None}}
    header = [
        "# Ответы владельца на вопросы онбординга AI Ops.",
        "#",
        "# Здесь только то, что из кода честно не выводится: цели продукта, его пользователи,",
        "# границы, которые нельзя нарушать. Кит эти вещи не выдумывает — поэтому спрашивает.",
        "#",
        "# Как отвечать: впишите значение после двоеточия. Пустая строка означает «ещё не ответил»,",
        "# и вопрос останется. Ответ сильнее любого вывода кита: он становится подтверждённым фактом",
        "# (`user_confirmed`) и больше не переспрашивается.",
        "#",
        "# После заполнения запустите снова:  ./ai-ops model",
    ]
    # Строки, которые кит пишет САМ, — эталон для разбора «чей комментарий» (F-020). Собираются до
    # генерации, потому что разбор существующего файла обязан знать их заранее.
    kit_comments, kit_lines = {}, set(header)
    for q in ask.get("questions") or []:
        qid = q.get("id")
        if not qid or qid in kit_comments:
            continue
        block = [f"  # {q.get('ask', '')}"]
        if q.get("proposal") and q["proposal"].get("value"):
            block.append(f"  #   по коду предполагаю: {q['proposal']['value']} — подтвердите или "
                         f"замените")
        kit_comments[qid] = block
        kit_lines.update(block)

    own = {"header": [], "before": {}, "inline": {}, "tail": []}
    if p.is_file():
        try:
            own = _owner_comments(p.read_text(encoding="utf-8"), kit_lines)
        except OSError:
            pass                               # прочитать не смогли — комментариев не будет, но
            # ответы уже прочитаны выше через `read_answers`, и потерять их этот путь не может

    def _value_line(qid, val):
        """Строка значения вместе с хвостовым комментарием владельца/основанием (#612), если был."""
        dumped = json.dumps(val, ensure_ascii=False) if val is not None else '""'
        note = (notes or {}).get(qid)
        if note:
            c = note if note.lstrip().startswith("#") else f"# {note}"
        else:
            c = own["inline"].get(qid)
        return f"  {qid}: {dumped}" + (f"  {c}" if c else "")

    lines = header + own["header"] + ["answers:"]
    seen = set()
    for q in ask.get("questions") or []:
        qid = q.get("id")
        if not qid or qid in seen:
            continue
        seen.add(qid)
        lines.extend(kit_comments[qid])
        lines.extend(f"  {c}" for c in own["before"].get(qid, []))
        val = existing.get(qid)
        # ЗНАЧЕНИЕ ПИШЕТСЯ ОДНОЙ СТРОКОЙ, БЕЗ МАРКЕРОВ ДОКУМЕНТА (F-023, живой прогон niti).
        #
        # Прежде стоял `yaml.safe_dump(val, default_flow_style=True).strip()`, и он ломал файл
        # ДВАЖДЫ. Первое: для голого скаляра PyYAML дописывает маркер конца документа —
        # `safe_dump("текст")` даёт `'текст\n...\n'`, а `.strip()` убирает только пробелы, поэтому
        # в файл попадала строка `...`, начинавшая НОВЫЙ YAML-документ. Второе: дефолт `width=80`
        # переносил длинное значение, а префикс `  {qid}: ` добавлялся лишь к первой строке —
        # продолжение оказывалось на отступе соседних ключей.
        #
        # Итог был потерей данных: кит записывал файл, который сам не мог прочитать, `read_answers`
        # отдавал «ответов нет», кит переспрашивал всё заново и перезаписывал файл пустым. Ломалось
        # ровно на содержательных ответах — короткие выживали.
        #
        # `json.dumps` даёт валидный YAML (YAML — надмножество JSON): двойные кавычки, одна строка,
        # никаких маркеров документа.
        lines.append(_value_line(qid, val))
        lines.append("")
    # Ответы на вопросы, которых больше не задают, сохраняем: человек их дал, они факт.
    for qid, val in existing.items():
        if qid not in seen:
            # ТОТ ЖЕ json.dumps, ЧТО ВЫШЕ (F-021, вторая половина). Первая правка закрыла только
            # ветку «вопрос ещё задаётся», а ОТВЕЧЕННЫЕ идут ИМЕННО СЮДА — их уже не спрашивают.
            # То есть дефект остался ровно там, где живут данные: на niti ответы гибли и после
            # «исправления», пока это место не поправили. Поймано повторным прогоном на живом
            # продукте, а не чтением кода.
            lines.extend(f"  {c}" for c in own["before"].get(qid, []))
            lines.append(_value_line(qid, val))
    lines.extend(own["tail"])
    body = "\n".join(lines).rstrip() + "\n"
    if p.is_file():
        try:
            if p.read_text(encoding="utf-8") == body:
                return p                       # тот же текст — писать нечего
        except OSError:
            pass                               # прочитать не смогли — перезапишем, это не потеря
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p
