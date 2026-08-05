#!/usr/bin/env python3
"""session_boundary.py (v3.17.0 Development Culture Guardrails, WP2) — Session Boundary Classifier:
определить отношение НОВОЙ задачи к текущей сессии/WorkItem, чтобы решить continue/compact vs clear/new.

Классы:
  same_task            — тот же WorkItem (тот же id).
  continuation         — логическое продолжение того же WorkItem (следующий шаг/пакет, тот же id или явная ссылка).
  adjacent_subtask     — соседняя подзадача: тот же репозиторий и пересекающийся scope/требования, но иной фокус.
  new_independent_task — новая независимая задача в том же репозитории (scope не пересекается).
  new_product          — другой репозиторий/продукт.

Дефолт (owner-правило): new_independent_task -> clear/new session; same_task/continuation -> continue/compact.
Детерминированно и честно: при нехватке данных не угадывает «удобно», а склоняется к БОЛЕЕ безопасному для
гигиены исходу (новая задача -> отдельная сессия). Классификатор — вход для session_guardrails.recommend.

CLI:  session_boundary.py --current WI-143 --new-task "текст" [--new-wi WI-144] [--repo-changed]
                          [--scope-overlap] [--continues] [--json]
      session_boundary.py --selftest
"""
from __future__ import annotations

import json
import sys

CLASSES = ("same_task", "continuation", "adjacent_subtask", "new_independent_task", "new_product")

# лексические маркеры «это продолжение» в тексте новой задачи
_CONT_MARKERS = ("продолж", "дожми", "доделай", "доведи", "continue", "то же", "тот же",
                 "follow-up", "followup", "next step", "следующий шаг", "закончи")


def classify(current_workitem=None, new_task="", new_workitem=None, repo_changed=False,
             scope_overlap=False, continues=None, related_requirements=False):
    """-> (class, reason). Детерминированная эвристика; при неоднозначности — безопасный для гигиены исход."""
    txt = (new_task or "").lower()

    if repo_changed:
        return "new_product", "изменился репозиторий/продукт — физически иная работа"

    # явный тот же WorkItem
    if new_workitem and current_workitem and new_workitem == current_workitem:
        return "same_task", f"тот же WorkItem {current_workitem}"

    # явное указание продолжения (флаг) или лексический маркер, без нового id
    marker = continues is True or (continues is None and any(m in txt for m in _CONT_MARKERS))
    if marker and current_workitem and not new_workitem:
        return "continuation", "логическое продолжение текущего WorkItem (явная ссылка/маркер)"

    # соседняя подзадача: тот же репо, пересекается scope/требования, но иной фокус
    if (scope_overlap or related_requirements) and current_workitem:
        return "adjacent_subtask", "тот же репозиторий, пересекающийся scope/требования, иной фокус"

    # по умолчанию — новая независимая задача (безопасно для гигиены: отдельная сессия)
    return "new_independent_task", "нет признаков продолжения/пересечения — самостоятельная задача"


def to_relation(cls):
    """Маппинг класса в next_relation для session_guardrails.recommend."""
    return cls  # словари совпадают (same_task/continuation -> continue-set; new_* -> new-set)


def check(cls):
    return [] if cls in CLASSES else [f"класс '{cls}' вне {CLASSES}"]


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    c, _ = classify(current_workitem="WI-143", new_workitem="WI-143", new_task="ещё правка")
    expect("тот же id -> same_task", c == "same_task")
    c, _ = classify(current_workitem="WI-143", new_task="дожми canary для WI-143")
    expect("маркер продолжения без нового id -> continuation", c == "continuation")
    c, _ = classify(current_workitem="WI-143", new_task="добавить environment discovery", scope_overlap=True)
    expect("пересечение scope, иной фокус -> adjacent_subtask", c == "adjacent_subtask")
    c, _ = classify(current_workitem="WI-143", new_task="добавить совершенно другую фичу")
    expect("нет признаков -> new_independent_task (безопасно для гигиены)", c == "new_independent_task")
    c, _ = classify(current_workitem="WI-143", new_task="что угодно", repo_changed=True)
    expect("сменился репозиторий -> new_product", c == "new_product")
    c, _ = classify(current_workitem=None, new_task="первая задача")
    expect("нет текущего WI -> new_independent_task", c == "new_independent_task")
    expect("continues=False подавляет лексический маркер",
           classify(current_workitem="WI-1", new_task="продолжаем", continues=False)[0] == "new_independent_task")
    expect("все классы валидны", all(check(x) == [] for x in CLASSES))
    expect("to_relation тождественно", to_relation("same_task") == "same_task")

    print("session_boundary selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    cur = new_wi = None
    new_task = ""
    repo_changed = "--repo-changed" in argv
    scope_overlap = "--scope-overlap" in argv
    continues = True if "--continues" in argv else None
    it = iter(argv)
    for a in it:
        if a == "--current":
            cur = next(it, None)
        elif a == "--new-wi":
            new_wi = next(it, None)
        elif a == "--new-task":
            new_task = next(it, None) or ""
    cls, reason = classify(cur, new_task, new_wi, repo_changed, scope_overlap, continues)
    out = {"kind": "SessionBoundary", "class": cls, "relation": to_relation(cls), "reason": reason}
    print(json.dumps(out, ensure_ascii=False, indent=2) if "--json" in argv
          else f"boundary: {cls} — {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
