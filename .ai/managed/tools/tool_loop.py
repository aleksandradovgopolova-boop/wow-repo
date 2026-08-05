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

import json
import re
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
for _p in (PKG / "tools",):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import tool_broker            # noqa: E402
import budget as _budget_mod  # noqa: E402


def parse_action(text):
    """Достать JSON-предложение из ответа модели (терпимо к обрамлению текстом)."""
    if isinstance(text, dict):
        return text
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {"error": "no-json"}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"error": "bad-json"}


def make_model_proposer(provider):
    """Обернуть text-provider (orchestrator) в предложитель действий через JSON-протокол.
    Живой путь: provider = make_provider('openai-compatible', 'deepseek-chat')."""
    def propose(context):
        prompt = (
            "Ты исполнитель задачи в контролируемом рантайме. На каждом шаге верни РОВНО ОДНО "
            "действие в JSON и больше ничего:\n"
            '  {"op":"write","path":"...","content":"..."}  — создать/изменить файл\n'
            '  {"op":"shell","command":"..."}                — команда (сборка/тест/проверка)\n'
            '  {"op":"read","path":"..."}                    — прочитать файл\n'
            '  {"done":true,"summary":"..."}                 — верни ЭТО, когда задача выполнена\n'
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
               required_evidence=None, reviewed_revision=None):
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
    заключить по прочитанному — ровно то, что обязан делать компетентный судья."""
    root = Path(root)
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
            context += ("\n[ревью] ЛИМИТ ЧТЕНИЙ ИСЧЕРПАН. Больше не читай. Верни СЛЕДУЮЩИМ РОВНО один "
                        "reviewer-result по уже прочитанному (status=fail с конкретными blockers, если "
                        "чего-то не хватило для pass). НЕ выдумывай.")
        elif len(reads) == max_reads - 1:
            context += ("\n[ревью] остаётся последнее чтение до лимита — прочти только самое нужное, "
                        "затем верни reviewer-result.")
        action = reviewer(context)
        if not isinstance(action, dict) or action.get("error"):
            context += "\n[ревью] верни РОВНО один JSON: read-действие или reviewer-result."
            continue
        # терминальный вердикт: reviewer-result (по kind/status)
        if action.get("kind") == "reviewer-result" or (action.get("status") and "op" not in action):
            action.setdefault("schema_version", 1)
            action.setdefault("kind", "reviewer-result")
            action.setdefault("gate", gate_id)
            if reviewed_revision:
                action.setdefault("reviewed_revision", reviewed_revision)
            return {"result": action, "stopped": "verdict", "reads": reads, "denied": denied}
        # на форс-ходе чтения запрещены: не исполняем, повторно требуем вердикт
        if force_verdict:
            context += "\n[ревью] чтение отклонено: лимит исчерпан. Нужен reviewer-result, не read."
            continue
        # иначе — действие через брокер (read-only Policy: write/shell -> DENIED)
        ev = tool_broker.execute(action, root, policy)
        if ev["allowed"] and ev.get("ok") and ev.get("op") == "read":
            reads.append(ev.get("target"))
            context += f"\n--- {ev.get('target')} ---\n{ev.get('output_tail')}\n--- конец ---"
        elif not ev["allowed"]:
            denied.append({"op": ev.get("op"), "reason": ev["reason"]})
            context += (f"\n[ревью] действие {ev.get('op')} ОТКЛОНЕНО (ты read-only судья, не автор): "
                        f"{ev['reason']}. Верни read или reviewer-result.")
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
    bad_streak = 0
    consec_reads = 0
    for step in range(max_steps):
        try:
            bud.charge_call()                       # каждый запрос к модели — под потолком
        except _budget_mod.BudgetExceeded as e:
            stopped = f"budget: {e}"; break
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
            transcript.append({"step": step, "done": True, "summary": action.get("summary", "")})
            break
        ev = tool_broker.execute(action, root, policy)   # Policy решает + исполнение + Evidence
        evidence.append(ev)
        # счётчик подряд-чтений: read увеличивает, любое другое исполненное действие — сбрасывает
        if ev.get("op") == "read" and ev.get("ok"):
            consec_reads += 1
        else:
            consec_reads = 0
        transcript.append({"step": step, "op": ev.get("op"), "allowed": ev["allowed"],
                           "ok": ev.get("ok"), "reason": ev["reason"]})
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
            "evidence": evidence, "transcript": transcript}


def selftest():
    import tempfile
    import subprocess
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    # parse
    expect("parse: JSON в тексте", parse_action('бла {"op":"read","path":"a"} бла')["op"] == "read")
    expect("parse: битый JSON -> error", parse_action("нет json").get("error") == "no-json")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        subprocess.run(["git", "-C", td, "init", "-q"])
        subprocess.run(["git", "-C", td, "config", "user.email", "t@t"])
        subprocess.run(["git", "-C", td, "config", "user.name", "t"])
        (root / "src").mkdir()
        (root / "f").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", td, "add", "-A"]); subprocess.run(["git", "-C", td, "commit", "-q", "-m", "i"])

        # сценарный mock-предложитель: пишет в scope, пробует вне scope, shell, done
        script = [
            {"op": "write", "path": "src/a.ts", "content": "hello"},   # разрешено
            {"op": "write", "path": "config/x.yaml", "content": "y"},  # вне scope -> denied
            {"op": "shell", "command": "echo ok"},                     # execution
            {"done": True, "summary": "готово"},
        ]
        it = iter(script)
        proposer = lambda ctx: next(it)
        pol = tool_broker.Policy(level="execution", write_scope=["src/"])
        rep = run_loop(proposer, root, pol, budget={"max_model_calls": 10})

        expect("остановка по done", rep["stopped"] == "done")
        expect("write в scope исполнен", (root / "src" / "a.ts").exists()
               and any(e["op"] == "write" and e["ok"] for e in rep["executed"]))
        expect("write вне scope запрещён и НЕ создан",
               not (root / "config" / "x.yaml").exists()
               and any(e["op"] == "write" for e in rep["denied"]))
        expect("shell исполнен (evidence)", any(e["op"] == "shell" and e["ok"] for e in rep["executed"]))
        expect("model_calls посчитаны", rep["model_calls"] == 4)

        # budget обрывает петлю
        it2 = iter([{"op": "read", "path": "f"}] * 10)
        rep2 = run_loop(lambda c: next(it2), root, pol, budget={"max_model_calls": 2})
        expect("budget обрывает петлю", rep2["stopped"].startswith("budget") and rep2["model_calls"] == 2)

        # max_steps-предохранитель
        it3 = iter([{"op": "read", "path": "f"}] * 100)
        rep3 = run_loop(lambda c: next(it3), root, pol, max_steps=3)
        expect("max_steps-предохранитель", rep3["stopped"] == "max_steps" and rep3["steps"] == 3)

        # finding аудита: модель ДОЛЖНА видеть содержимое прочитанного файла в контексте
        (root / "readme.txt").write_text("SENTINEL_CONTENT_42", encoding="utf-8")
        seen = {}

        def prop_read(ctx):
            seen["ctx"] = ctx
            if not seen.get("did_read"):
                seen["did_read"] = True
                return {"op": "read", "path": "readme.txt"}
            return {"done": True, "summary": "прочитал"}

        run_loop(prop_read, root, pol, budget={"max_model_calls": 5})
        expect("модель ВИДИТ содержимое прочитанного файла в контексте",
               "SENTINEL_CONTENT_42" in seen.get("ctx", ""))

        # finding живого прогона: битый JSON НЕ убивает прогон — до N корректирующих переспросов.
        # Модель «оступается» дважды (bad-json), потом отдаёт валидный write + done.
        seq = iter([
            {"error": "bad-json"}, {"error": "bad-json"},
            {"op": "write", "path": "src/rec.ts", "content": "ok"}, {"done": True},
        ])
        rep_rec = run_loop(lambda c: next(seq), root, pol, budget={"max_model_calls": 10})
        expect("bad-json: петля восстановилась после переспросов -> done",
               rep_rec["stopped"] == "done" and (root / "src" / "rec.ts").exists())

        # корректирующая подсказка попала в контекст переспроса
        cap = {}
        seq2 = iter([{"error": "bad-json"}, {"done": True}])
        def prop_corr(ctx):
            cap["ctx"] = ctx
            return next(seq2)
        run_loop(prop_corr, root, pol, budget={"max_model_calls": 5})
        expect("bad-json: модель получает корректирующую подсказку про JSON",
               "ОШИБКА РАЗБОРА" in cap.get("ctx", ""))

        # много битых подряд -> честная остановка bad-proposal (не вечный цикл)
        rep_bad = run_loop(lambda c: {"error": "bad-json"}, root, pol,
                           budget={"max_model_calls": 10}, max_bad_proposals=3)
        expect("bad-json: N подряд -> честная остановка bad-proposal",
               rep_bad["stopped"].startswith("bad-proposal"))

        # анти-флейл (finding живого прогона): чтение по кругу -> read-cap отклоняет лишние reads
        rep_flail = run_loop(lambda c: {"op": "read", "path": "f"}, root, pol,
                             budget={"max_model_calls": 30}, max_steps=12, max_consecutive_reads=5)
        capped = [t for t in rep_flail["transcript"] if not t.get("allowed") and "read-cap" in (t.get("reason") or "")]
        expect("read-cap: чтение по кругу отклоняется после лимита", len(capped) > 0)

        # но НОРМАЛЬНЫЙ поток (2 чтения -> write -> done) не задет read-cap'ом
        norm = iter([{"op": "read", "path": "f"}, {"op": "read", "path": "f"},
                     {"op": "write", "path": "src/z.ts", "content": "z"}, {"done": True}])
        rep_norm = run_loop(lambda c: next(norm), root, pol, budget={"max_model_calls": 10},
                            max_consecutive_reads=5)
        expect("read-cap: нормальный поток (2 read -> write -> done) не задет",
               rep_norm["stopped"] == "done" and (root / "src" / "z.ts").exists())

        # v2.83: независимый ревьюер (writer ≠ judge) под READ-ONLY политикой
        ro = tool_broker.Policy(level="read-only")
        # (a) прямой вердикт pass
        rev_pass = run_review(lambda c: {"kind": "reviewer-result", "gate": "code_review",
                                         "status": "pass", "checks": [{"id": "logic", "status": "pass"}]},
                              root, ro, "code_review", budget={"max_model_calls": 5})
        expect("reviewer: терминальный вердикт pass возвращён",
               rev_pass["result"] and rev_pass["result"]["status"] == "pass"
               and rev_pass["result"]["gate"] == "code_review")
        # (b) ревьюер физически НЕ может писать (read-only): попытка write отклонена, затем вердикт fail
        rev_seq = iter([{"op": "write", "path": "src/evil.ts", "content": "x"},
                        {"kind": "reviewer-result", "gate": "code_review", "status": "fail",
                         "checks": [{"id": "logic", "status": "fail"}], "blockers": ["баг в ветке"]}])
        rev_wr = run_review(lambda c: next(rev_seq), root, ro, "code_review", budget={"max_model_calls": 5})
        expect("reviewer: write ОТКЛОНЁН (read-only судья, не автор) + файл не создан",
               rev_wr["denied"] and not (root / "src" / "evil.ts").exists())
        expect("reviewer: вердикт fail с blockers", rev_wr["result"]["status"] == "fail"
               and rev_wr["result"]["blockers"])
        # (c) ревьюер читает файл -> содержимое в контексте вердикта
        (root / "reviewme.txt").write_text("REVIEW_SENTINEL_7", encoding="utf-8")
        cap_r = {}
        rseq = iter([{"op": "read", "path": "reviewme.txt"},
                     {"kind": "reviewer-result", "gate": "code_review", "status": "pass",
                      "checks": [{"id": "x", "status": "pass"}]}])
        def rprop(ctx):
            cap_r["ctx"] = ctx
            return next(rseq_it)
        rseq_it = rseq
        run_review(rprop, root, ro, "code_review", budget={"max_model_calls": 5})
        expect("reviewer: ВИДИТ содержимое прочитанного файла в контексте",
               "REVIEW_SENTINEL_7" in cap_r.get("ctx", ""))
        # (d) не вынес вердикт за лимит -> result=None (гейт останется неподтверждённым)
        rev_none = run_review(lambda c: {"op": "read", "path": "f"}, root, ro, "code_review",
                              budget={"max_model_calls": 20}, max_reads=3)
        expect("reviewer: без вердикта за лимит -> result=None (честный не-pass)",
               rev_none["result"] is None)
        # (e) rc10: жадная reasoning-модель читает до лимита, затем на ФОРС-ХОДЕ заключает вердикт
        #     (раньше молча уходила в no-verdict). Форс-ход даёт заключить по прочитанному, НЕ фабрикуя.
        def greedy_then_verdict(c):
            if "ЛИМИТ ЧТЕНИЙ ИСЧЕРПАН" in c:
                return {"kind": "reviewer-result", "gate": "code_review", "status": "pass",
                        "checks": [{"id": "ok", "status": "pass"}]}
            return {"op": "read", "path": "f"}
        rev_fv = run_review(greedy_then_verdict, root, ro, "code_review",
                            budget={"max_model_calls": 20}, max_reads=3)
        expect("reviewer rc10: форс-ход после исчерпания чтений -> вердикт вынесен (не тихий no-verdict)",
               rev_fv["result"] is not None and rev_fv["stopped"] == "verdict"
               and len(rev_fv["reads"]) == 3)

    print("tool_loop selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
