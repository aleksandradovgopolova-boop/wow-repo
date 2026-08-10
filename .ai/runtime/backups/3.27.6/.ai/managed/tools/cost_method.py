#!/usr/bin/env python3
"""cost_method.py (v3.18.0 Development Culture Guardrails, WP6) — Cost-aware Work Method: единый советник
ЭКОНОМИЧНОГО способа работы, собирающий рекомендации в ФИКСИРОВАННОМ порядке приоритетов владельца:

  1. Гигиена сессии и контекста   (session_guardrails: не таскать огромную историю на каждом запросе)
  2. Делегирование разведки        (delegation_advisor: тяжёлое для контекста — в сабагента)
  3. Ограничение бесполезных итераций (стоп fix-loop, не жечь попытки без прогресса)
  4. Выбор runtime                 (model_router.writer_tier: дешёвый на простом, сильный на сложном)
  5. Effort и краткость ответа     (low на механике, выше — на сложном)

Плюс частные советы: affected-tests вместо полного прогона; не читать весь репозиторий; переиспользовать
существующий компонент. Кит СОВЕТУЕТ (не форсит). Главное правило: не экономить копейки на output, продолжая
перечитывать огромную историю — гигиена сессии важнее краткости ответа.

CLI:  cost_method.py <child_root> [--task-type T] [--risk R] [--context N] [--files N]
                     [--fix-attempts N] [--small-change] [--json]
      cost_method.py --selftest
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

FIX_LOOP_LIMIT = 2   # стартовый порог: после N безуспешных fix-итераций — стоп/эскалация, не жечь


def advise(signals, snapshot=None, child_root=None):
    """-> упорядоченный список рекомендаций {priority, category, advice}. Порядок = приоритеты владельца."""
    s = dict(signals or {})
    out = []

    def add(priority, category, advice):
        out.append({"priority": priority, "category": category, "advice": advice})

    # 1. Гигиена сессии/контекста
    try:
        import session_guardrails as _sg
        pol = _sg.load_policy(child_root or ".")
        ctx = (snapshot or {}).get("context_current")
        state = _sg.classify_context(ctx, pol)
        if state == "new_session_recommended":
            add(1, "session_hygiene", "контекст превысил порог новой сессии — не начинать новый блок здесь; "
                                      "`ai-ops session` даст точную команду (clear/new)")
        elif state == "compact_recommended":
            add(1, "session_hygiene", "контекст дорогой — на безопасной границе сделать /compact "
                                      "(см. `ai-ops session`)")
        elif state == "attention":
            add(1, "session_hygiene", "контекст растёт — следить за объёмом; при новой независимой задаче — /clear")
        elif state == "unknown":
            add(1, "session_hygiene", "объём контекста неизвестен — передайте `--context N` из /context рантайма "
                                      "для точной оценки")
    except Exception:  # noqa: BLE001
        pass

    # 2. Делегирование разведки
    try:
        import delegation_advisor as _da
        for d in _da.advise(s):
            add(2, "delegation", f"{d['trigger']}: {d['reason']} -> {d['delegate_to']}")
    except Exception:  # noqa: BLE001
        pass

    # 3. Ограничение бесполезных итераций
    attempts = int(s.get("fix_attempts") or 0)
    if attempts >= FIX_LOOP_LIMIT:
        add(3, "iteration_limit", f"уже {attempts} безуспешных fix-итераций — остановить fix-loop; "
                                  "эскалировать writer'а (сильнее) или вынести на человека, а не жечь попытки")

    # 4. Выбор runtime
    try:
        import model_router as _mr
        wt = _mr.writer_tier(s)
        if wt["tier"] == "cheap-api":
            add(4, "runtime", f"{wt['reason']} — Opus/сильный writer не нужен; дешёвый qualified writer")
        else:
            add(4, "runtime", f"{wt['reason']} — задача сложная: сильный executor оправдан")
    except Exception:  # noqa: BLE001
        pass

    # 5. Effort и краткость
    tt = str(s.get("task_type") or "").upper()
    if tt in ("QUICK", "") or s.get("mechanical"):
        add(5, "effort", "механическая/простая задача — low effort и краткий ответ достаточно")
    elif tt in ("PRODUCT", "RESEARCH", "CRITICAL"):
        add(5, "effort", f"{tt} — высокий effort оправдан (не экономить на рассуждении там, где цена ошибки высока)")
    else:
        add(5, "effort", f"{tt} — medium effort; краткость ответа без потери сути")

    # частные советы
    if s.get("small_change") or s.get("baseline_diff"):
        add(3, "tests", "малое изменение — сначала affected tests, полный прогон не обязателен сейчас")
    if s.get("needs_repo_read") and not s.get("exploration_files"):
        add(2, "exploration", "не читать весь репозиторий в основном контексте — точечная разведка/сабагент")
    if s.get("reusable_component"):
        add(4, "reuse", "возможно переиспользование существующего компонента/настройки вместо нового кода")

    out.sort(key=lambda r: r["priority"])
    return out


def check(recs):
    e = []
    for r in recs or []:
        if r.get("priority") not in (1, 2, 3, 4, 5):
            e.append(f"priority вне 1..5: {r.get('priority')}")
        if not r.get("advice"):
            e.append(f"{r.get('category')}: пустой совет")
    return e


def render(recs):
    if not recs:
        return "cost-method: явных советов нет (задача простая, сессия здоровая)."
    L = ["=== Cost-aware Work Method (порядок приоритетов) ==="]
    labels = {1: "гигиена сессии", 2: "делегирование", 3: "итерации/тесты", 4: "runtime", 5: "effort"}
    for r in recs:
        L.append(f"  [{r['priority']}·{labels.get(r['priority'], r['category'])}] {r['advice']}")
    return "\n".join(L)


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    # тяжёлый контекст -> гигиена приоритет 1 первой
    r = advise({"task_type": "ENGINEERING"}, snapshot={"context_current": 450_000})
    expect("тяжёлый контекст -> совет гигиены приоритета 1",
           r and r[0]["priority"] == 1 and r[0]["category"] == "session_hygiene")
    expect("порядок приоритетов неубывающий",
           all(r[i]["priority"] <= r[i + 1]["priority"] for i in range(len(r) - 1)))
    expect("check валиден", check(r) == [])

    # QUICK -> runtime дешёвый + low effort
    r = advise({"task_type": "QUICK"}, snapshot={"context_current": 50_000})
    cats = {x["category"]: x["advice"] for x in r}
    expect("QUICK -> дешёвый runtime", "runtime" in cats and "не нужен" in cats["runtime"])
    expect("QUICK -> low effort", "effort" in cats and "low effort" in cats["effort"])

    # много fix-итераций -> стоп fix-loop (приоритет 3)
    r = advise({"task_type": "ENGINEERING", "fix_attempts": 3}, snapshot={"context_current": 50_000})
    expect("3 fix-итерации -> совет остановить fix-loop",
           any(x["category"] == "iteration_limit" for x in r))

    # разведка 30 файлов -> делегирование (приоритет 2)
    r = advise({"task_type": "ENGINEERING", "exploration_files": 30}, snapshot={"context_current": 50_000})
    expect("30 файлов -> совет делегирования приоритета 2",
           any(x["category"] == "delegation" and x["priority"] == 2 for x in r))

    # неизвестный контекст -> совет передать /context
    r = advise({"task_type": "ENGINEERING"}, snapshot={"context_current": None})
    expect("контекст unknown -> совет передать --context",
           any("context" in x["advice"].lower() for x in r if x["category"] == "session_hygiene"))

    # малое изменение -> affected tests
    r = advise({"task_type": "ENGINEERING", "small_change": True}, snapshot={"context_current": 50_000})
    expect("малое изменение -> affected tests", any(x["category"] == "tests" for x in r))

    print("cost_method selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    sig = {"mechanical": "--mechanical" in argv, "small_change": "--small-change" in argv,
           "needs_repo_read": "--repo-read" in argv, "reusable_component": "--reuse" in argv}
    ctx = None
    args, it = [], iter(argv)
    for a in it:
        if a == "--task-type":
            sig["task_type"] = next(it, None)
        elif a == "--risk":
            sig["risk"] = next(it, None)
        elif a == "--context":
            v = next(it, None); ctx = int(v) if v and v.isdigit() else None
        elif a == "--files":
            v = next(it, None); sig["exploration_files"] = int(v) if v and v.isdigit() else 0
        elif a == "--fix-attempts":
            v = next(it, None); sig["fix_attempts"] = int(v) if v and v.isdigit() else 0
        elif not a.startswith("--"):
            args.append(a)
    root = args[0] if args else "."
    snap = None
    try:
        import session_telemetry
        snap = session_telemetry.snapshot(root, context_current=ctx)
    except Exception:  # noqa: BLE001
        snap = {"context_current": ctx}
    recs = advise(sig, snapshot=snap, child_root=root)
    print(json.dumps(recs, ensure_ascii=False, indent=2) if "--json" in argv else render(recs))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
