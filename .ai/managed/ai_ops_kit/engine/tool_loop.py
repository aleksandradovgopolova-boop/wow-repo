#!/usr/bin/env python3
"""Tool-calling loop (v2.42, Execution Engine Фаза 2, срез 3 — механика петли).

Замыкает «task → controlled execution»: модель ПРЕДЛАГАЕТ действие (JSON), Policy решает
(tool_broker), Broker исполняет и собирает Evidence, результат идёт обратно в контекст —
и так до «done» / потолка budget / max_steps. Модель не решает, что ей можно; запрещённое
не исполняется, а возвращается модели как DENIED (чтобы скорректировалась).

Механика детерминирована и тестируется offline mock-предложителем. Живой предложитель —
это provider из orchestrator (anthropic/openai/openai-compatible) в JSON-режиме; его
качество проверяется живым прогоном (как Шаг A для текста), но ЛОГИКА петли — здесь и
проверяема без ключа.

Формат предложения модели (одно на шаг):
  {"op":"read|write|shell|git", "path":..., "content":..., "command":...}
  {"done": true, "summary": "..."}

Использование (программно): run_loop(proposer, root, policy, budget) -> отчёт петли.
  tool_loop.py --selftest
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402
from ai_ops_kit.engine import tool_broker            # noqa: E402
from ai_ops_kit.shared import budget as _budget_mod  # noqa: E402


def parse_action(text):
    """Достать JSON-предложение из ответа модели (терпимо к обрамлению текстом).

    v3.30: разбор проверяет ФОРМУ, а не только синтаксис. Прежде `{}` и `{"чужая":"схема"}`
    возвращались как есть — «полудействие» без op/done проходило разбор, брокер его отклонял,
    но счётчик max_bad_proposals не срабатывал: модель, зациклившаяся на пустом объекте, жгла
    все шаги и бюджет вместо раннего останова. Обрезанный JSON при этом назывался «no-json»,
    и подсказка, уходящая обратно в модель, уводила её не туда."""
    if isinstance(text, dict):
        return text
    raw = text or ""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"error": "bad-json" if "{" in raw else "no-json"}
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"error": "bad-json"}
    if not isinstance(obj, dict):
        return {"error": "no-action"}
    # действие цикла (op/done) ИЛИ вердикт ревьюера (kind/status) — иначе это не предложение
    if not (obj.get("op") or obj.get("done") or obj.get("kind") or obj.get("status")):
        return {"error": "no-action"}
    return obj


def make_model_proposer(provider):
    """Обернуть text-provider (orchestrator) в предложитель действий через JSON-протокол.
    Живой путь: provider = make_provider('openai-compatible', 'deepseek-chat')."""
    def propose(context):
        prompt = (
            # ТЫ И ЕСТЬ ДВИЖОК (полевой замер 15.08.2026, `ai-product-quest`). Кит кладёт в дочку
            # правило CLAUDE.md: «когда просят изменение кода — НЕ правь файлы напрямую, запусти
            # ./ai-run». Писатель кита — это Claude Code, он читает CLAUDE.md рабочего дерева,
            # добросовестно слушается и не пишет НИЧЕГО: два прогона подряд дали 10 и 7 шагов при
            # нуле правок, а кит объяснил это как «нужен живой провайдер». В соседнем репозитории
            # без такого правила тот же писатель отработал с первой попытки.
            "Ты — ИСПОЛНИТЕЛЬ ВНУТРИ движка AI Ops: этот прогон и есть тот самый `ai-run`, на "
            "который ссылаются правила репозитория. Указания вида «не правь файлы напрямую, "
            "запусти кит» относятся к работе ПОМИМО движка и к тебе НЕ применяются — правки "
            "делаешь ты, а гейты и ревью стоят после тебя.\n"
            "Работа идёт в контролируемом рантайме. На каждом шаге верни РОВНО ОДНО "
            "действие в JSON и больше ничего:\n"
            '  {"op":"write","path":"...","content":"..."}  — создать/изменить файл\n'
            '  {"op":"shell","command":"..."}                — команда (сборка/тест/проверка)\n'
            '  {"op":"read","path":"..."}                    — прочитать файл\n'
            '  {"done":true,"summary":"..."}                 — верни ЭТО, когда задача выполнена\n'
            '  {"done":true,"summary":"...","behavior_unchanged":"<причина>"} — ТОЛЬКО если правка\n'
            '      не меняет поведение (рефакторинг, переименование, форматирование). Обычная\n'
            '      правка ОБЯЗАНА сопровождаться тестом, который падает на коде ДО неё:\n'
            '      без него исправление ничем не подтверждено и гейт его не пропустит.\n'
            "Правила: НЕ повторяй уже успешно выполненный шаг (см. журнал ниже — там строки "
            "'-> OK'). Как только нужные файлы записаны и проверка (shell) прошла — сразу верни "
            "done, не пиши файл повторно. ЧИТАЙ (read) МИНИМУМ — 1-2 файла, чтобы понять, затем "
            "СРАЗУ вноси правку через write. НЕ читай по кругу: если файл уже прочитан (есть в "
            "журнале), не читай его снова — пиши фикс. Только JSON, без пояснений.\n\n"
            "=== ЗАДАЧА И ЖУРНАЛ УЖЕ ВЫПОЛНЕННЫХ ШАГОВ ===\n" + context)
        return parse_action(provider(prompt))
    return propose


def make_reviewer_proposer(provider, gate_id, checklist="", required_evidence=None,
                           reviewed_revision=None):
    """v2.83 Full RunPlan: НЕЗАВИСИМЫЙ ревьюер (writer ≠ judge). Оборачивает text-provider в
    read-only судью: он читает изменение и выносит СТРУКТУРНЫЙ вердикт reviewer-result, а НЕ
    правит код. Independence обеспечивается на уровне роли (отдельный вызов, отдельный промпт)
    и на уровне capability (петля гоняет его под read-only Policy — write/shell брокер отклонит).
    ЧЕСТНО: та же базовая модель — более слабая независимость, чем другой судья/человек; но
    писатель НЕ может закрыть свой же гейт словом, и физически не может писать в роли ревьюера."""
    req = list(required_evidence or [])
    def propose(context):
        prompt = (
            f"Ты НЕЗАВИСИМЫЙ ревьюер гейта '{gate_id}' (не автор изменения). Только чтение.\n"
            f"Проверяемая ревизия: {reviewed_revision or 'HEAD'}.\n"
            + (f"Чек-лист:\n{checklist}\n" if checklist else "")
            + (f"Требуемые доказательства (required_evidence): {', '.join(req)}. Ставь pass ТОЛЬКО "
               f"если реально подтвердил их чтением; иначе fail/warn с конкретикой.\n" if req else "")
            + "На каждом шаге верни РОВНО ОДИН JSON:\n"
            '  {"op":"read","path":"..."}  — прочитать файл, чтобы удостовериться\n'
            '  {"kind":"reviewer-result","gate":"' + gate_id + '","status":"pass|warn|fail",'
            '"checks":[{"id":"...","status":"pass|warn|fail"}],"blockers":["..."]}  — ИТОГ\n'
            "Правила: читай минимально; выноси вердикт по фактам из прочитанного. status=fail И "
            "status=warn требуют непустой blockers с КОНКРЕТНЫМИ проблемами (warn на блокирующем "
            "гейте тоже блокирует). Честность симметрична: НЕ выдумывай pass (чего не подтвердил "
            "чтением — не pass), но и НЕ выдумывай сомнения ради подстраховки — если прочитал "
            "изменение и КОНКРЕТНОЙ проблемы нет, это pass, а не warn «на всякий случай». Только JSON.\n\n"
            "=== КОНТЕКСТ (изменение и журнал чтений) ===\n" + context)
        return parse_action(provider(prompt))
    return propose


def run_review(reviewer, root, policy, gate_id, budget=None, max_reads=6, base_context="",
               required_evidence=None, reviewed_revision=None,
               terminal_kind="reviewer-result", terminal_field=None):
    """Один независимый ревью-проход под READ-ONLY политикой -> reviewer-result (dict) + трейс.

    Ревьюер может читать файлы (write/shell брокер отклонит — capability-независимость от писателя),
    затем обязан вернуть терминальный reviewer-result. Возвращает {"result": <reviewer-result|None>,
    "stopped": ..., "reads": [...], "denied": [...]}. Если ревьюер не вынес вердикт — result=None
    (гейт останется неподтверждённым, честный fail на уровне gate_executor).

    v3.0-rc10 (finding живого прогона kimi): reasoning-модель на многофайловом диффе тратила ВЕСЬ
    read-бюджет на чтение и не успевала вынести вердикт -> тихий no-verdict. Теперь: (1) нудж, когда
    осталось последнее чтение; (2) выделенный ФОРС-ХОД вердикта после исчерпания чтений — читать
    больше нельзя, принимается только reviewer-result. Вердикт НЕ фабрикуется: если ревьюер и на
    форс-ходе не заключает — честный no-verdict (fail). Мы лишь ограничиваем фазу чтения и требуем
    заключить по прочитанному — ровно то, что обязан делать компетентный судья.

    B2-14 (2026-08-14): вид терминального вердикта стал ПАРАМЕТРОМ. Петля read-only судьи нужна не
    только гейтам: сверка критериев приёмки — тот же шов (независимый судья, те же нуджи, тот же
    форс-ход, тот же брокер), но её вердикт — `acceptance-result` с вердиктом по каждому критерию,
    а не один `status` на гейт. Значения по умолчанию оставлены прежними, поэтому путь
    `reviewer-result` не меняется ни на байт; `terminal_field` называет поле, по которому вердикт
    узнаётся, когда модель не проставила `kind`."""
    root = Path(root)
    # Локальный импорт (как ai_ops_run зовёт providers): response_contract — лист, engine не тянет,
    # цикла нет; локально — чтобы не расширять статический граф engine ради одного класса-исключения.
    from ai_ops_kit.providers.response_contract import ProviderRefusal as _ProviderRefusal
    bud = budget if isinstance(budget, _budget_mod.Budget) else _budget_mod.Budget.from_dict(budget)
    context = base_context
    reads, denied = [], []
    stopped = "no-verdict"
    for _ in range(max_reads + 2):                  # +1 нудж-запас, +1 форс-ход вердикта
        try:
            bud.charge_call()
        except _budget_mod.BudgetExceeded as e:
            stopped = f"budget: {e}"; break
        force_verdict = len(reads) >= max_reads     # бюджет чтений исчерпан -> только вердикт
        if force_verdict:
            context += (f"\n[ревью] ЛИМИТ ЧТЕНИЙ ИСЧЕРПАН. Больше не читай. Верни СЛЕДУЮЩИМ РОВНО один "
                        f"{terminal_kind} по уже прочитанному (чего не подтвердил чтением — то и "
                        f"скажи конкретно). НЕ выдумывай.")
        elif len(reads) == max_reads - 1:
            context += (f"\n[ревью] остаётся последнее чтение до лимита — прочти только самое нужное, "
                        f"затем верни {terminal_kind}.")
        try:
            action = reviewer(context)
        except _ProviderRefusal as refusal:
            # Провайдер судьи НАЗВАЛ отказ (пусто/обрезано/отказ модели). Прежде это был
            # неперехваченный подъём (после того как провайдеры научились называть пустой ответ);
            # теперь — честный no-verdict с названной причиной, которую поднимет gate_executor.
            return {"result": None, "stopped": f"refusal: {refusal.reason}",
                    "refusal": refusal.as_dict(), "reads": reads, "denied": denied}
        if not isinstance(action, dict) or action.get("error"):
            context += f"\n[ревью] верни РОВНО один JSON: read-действие или {terminal_kind}."
            continue
        # терминальный вердикт: по kind, по названному полю вердикта либо (для reviewer-result) по status
        _terminal = (action.get("kind") == terminal_kind
                     or (terminal_field is not None and action.get(terminal_field) is not None
                         and "op" not in action)
                     or (terminal_field is None and action.get("status") and "op" not in action))
        if _terminal:
            action.setdefault("schema_version", 1)
            action.setdefault("kind", terminal_kind)
            if gate_id is not None:
                action.setdefault("gate", gate_id)
            if reviewed_revision:
                action.setdefault("reviewed_revision", reviewed_revision)
            return {"result": action, "stopped": "verdict", "reads": reads, "denied": denied}
        # на форс-ходе чтения запрещены: не исполняем, повторно требуем вердикт
        if force_verdict:
            context += f"\n[ревью] чтение отклонено: лимит исчерпан. Нужен {terminal_kind}, не read."
            continue
        # иначе — действие через брокер (read-only Policy: write/shell -> DENIED)
        ev = tool_broker.execute(action, root, policy)
        if ev["allowed"] and ev.get("ok") and ev.get("op") == "read":
            reads.append(ev.get("target"))
            context += f"\n--- {ev.get('target')} ---\n{ev.get('output_tail')}\n--- конец ---"
        elif not ev["allowed"]:
            denied.append({"op": ev.get("op"), "reason": ev["reason"]})
            # Вид вердикта здесь тоже параметр (ревью PR #118): судья сверки приёмки, получив
            # отказ брокера, читал «верни reviewer-result» — то есть подсказку вернуть вердикт
            # ЧУЖОЙ формы, который не пройдёт терминальную проверку. Это путь, на который он
            # попадает при попытке записи, — ровно там подсказка должна быть верной.
            context += (f"\n[ревью] действие {ev.get('op')} ОТКЛОНЕНО (ты read-only судья, не автор): "
                        f"{ev['reason']}. Верни read или {terminal_kind}.")
        else:
            context += f"\n[ревью] {ev.get('op')} -> {ev.get('reason')}"
    return {"result": None, "stopped": stopped, "reads": reads, "denied": denied}


def run_loop(proposer, root, policy, budget=None, max_steps=20, base_context="",
             max_bad_proposals=3, max_consecutive_reads=5):
    """Гонять петлю до done / budget / max_steps. proposer(context)->action|{'done':true}.

    finding живого прогона: живая модель (DeepSeek) недетерминирована и иногда возвращает
    невалидный JSON. Раньше ОДНА кривая реплика обрывала весь прогон. Теперь — до
    max_bad_proposals ПОДРЯД корректирующих переспросов; счётчик сбрасывается на валидном действии.

    finding живого прогона (fix-задача): слабая модель может «читать по кругу» — 20 read подряд,
    0 записей, упор в max_steps. Анти-флейл: после max_consecutive_reads чтений подряд без
    записи следующее read ОТКЛОНЯЕТСЯ на уровне петли с требованием вернуть write/done.
    """
    root = Path(root)
    bud = budget if isinstance(budget, _budget_mod.Budget) else _budget_mod.Budget.from_dict(budget)
    evidence, transcript = [], []
    context = base_context
    stopped = "max_steps"
    behavior_unchanged = None      # v3.30: объявление writer'а при завершении, см. обработку done
    bad_streak = 0
    consec_reads = 0
    for step in range(max_steps):
        try:
            bud.charge_call()                       # каждый запрос к модели — под потолком
        except _budget_mod.BudgetExceeded as e:
            stopped = f"budget: {e}"; break
        # B2-22 (пере-прогон 14.08.2026): прогон молчал. Один и тот же пустой экран означал и
        # «работает», и «повисло»: живой вызов модели занимал от секунд до трёх минут, и владелец
        # либо ждал вслепую, либо прерывал работающий прогон. Отличить я смог только через `ps` —
        # владелец так делать не станет. Данные для честной строки у нас уже есть: номер шага,
        # потолок и что мы сейчас ждём. Пишем в stderr, чтобы не портить машиночитаемый stdout.
        print(f"  · шаг {step + 1}/{max_steps}: жду ответа модели…", file=sys.stderr, flush=True)
        action = proposer(context)
        if not isinstance(action, dict) or action.get("error"):
            bad_streak += 1
            err = action.get("error") if isinstance(action, dict) else action
            if bad_streak >= max_bad_proposals:
                stopped = f"bad-proposal: {err}"; break
            # корректирующий переспрос: показать модели её ошибку и потребовать чистый JSON
            context += (f"\n[шаг {step}] ОШИБКА РАЗБОРА ({err}): твой ответ не распарсился как "
                        f"JSON. Верни РОВНО ОДИН JSON-объект действия и НИЧЕГО больше — без "
                        f"markdown-обрамления, без пояснений, без текста до/после.")
            continue
        bad_streak = 0
        # анти-флейл: слишком много чтений подряд без записи -> не исполняем ещё одно чтение
        if action.get("op") == "read" and consec_reads >= max_consecutive_reads:
            transcript.append({"step": step, "op": "read", "allowed": False,
                               "reason": f"read-cap: {consec_reads} чтений подряд без записи"})
            context += (f"\n[шаг {step}] СТОП-ЧТЕНИЕ: уже {consec_reads} чтений подряд без единой "
                        f"записи. Больше НЕ читай. Верни СЛЕДУЮЩИМ РОВНО одно "
                        f"{{\"op\":\"write\",\"path\":...,\"content\":...}} с фактическим фиксом "
                        f"или {{\"done\":true}}.")
            continue
        if action.get("done"):
            stopped = "done"
            # v3.30: объявление «поведение не менялось» — единственный законный способ закрыть
            # regression_test_evidence без теста. Это НЕ тихий обход: причина едет в отчёт и в
            # warnings гейта. Пустую строку за объявление не считаем.
            _bu = action.get("behavior_unchanged")
            behavior_unchanged = str(_bu).strip() if isinstance(_bu, str) and _bu.strip() else None
            transcript.append({"step": step, "done": True, "summary": action.get("summary", ""),
                               "behavior_unchanged": behavior_unchanged})
            break
        ev = tool_broker.execute(action, root, policy)   # Policy решает + исполнение + Evidence
        evidence.append(ev)
        # счётчик подряд-чтений: read увеличивает, любое другое исполненное действие — сбрасывает
        if ev.get("op") == "read" and ev.get("ok"):
            consec_reads += 1
        else:
            consec_reads = 0
        # ЧТО ИМЕННО исполнялось — в транскрипт (полевой замер 15.08.2026). Три прогона подряд дали
        # «шагов 10 · правок 0», и разобрать было НЕЧЕМ: транскрипт хранил вид операции и вердикт
        # брокера, но не команду и не файл. Владелец в такой ситуации видит «кит не справился» без
        # единой зацепки, а сам кит объясняет неудачу неверной причиной («нужен живой провайдер»
        # при работающем провайдере). Команда усечена: транскрипт — след, а не журнал.
        _what = action.get("path") or action.get("command") or ""
        transcript.append({"step": step, "op": ev.get("op"), "allowed": ev["allowed"],
                           "ok": ev.get("ok"), "reason": ev["reason"],
                           "what": str(_what)[:200] or None})
        # результат обратно в контекст (в т.ч. DENIED — чтобы модель скорректировалась).
        # ВАЖНО (finding аудита): модель должна ВИДЕТЬ содержимое/вывод, иначе read/shell слепы —
        # это не агентная петля. Передаём output_tail для read (содержимое) и shell (stdout/stderr).
        if not ev["allowed"]:
            verdict = "DENIED: " + ev["reason"]
        elif ev.get("ok"):
            verdict = "OK"
            tail = ev.get("output_tail")
            if ev.get("op") == "read":
                verdict += f"\n--- содержимое {ev.get('target')} ---\n{tail}\n--- конец ---"
            elif ev.get("op") in ("shell", "git") and tail:
                verdict += f" (exit={ev.get('exit_code')})\n--- вывод ---\n{tail}\n--- конец ---"
        else:
            verdict = f"FAILED (exit={ev.get('exit_code')}): {ev.get('output_tail') or ev.get('error', '')}"
        context += f"\n[шаг {step}] {ev.get('op')} {ev.get('target')} -> {verdict}"
    return {"schema_version": 1, "kind": "tool-loop-report",
            "stopped": stopped, "steps": len(transcript),
            "model_calls": bud.model_calls,
            "executed": [e for e in evidence if e["allowed"]],
            "denied": [e for e in evidence if not e["allowed"]],
            "evidence": evidence, "transcript": transcript,
            # v3.30: объявление «поведение не менялось» — единственный законный способ закрыть
            # regression_test_evidence без теста; его читает execution_pipeline.
            "behavior_unchanged": behavior_unchanged}


def main(argv):
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
