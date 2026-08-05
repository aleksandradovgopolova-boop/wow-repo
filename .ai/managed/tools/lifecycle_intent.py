#!/usr/bin/env python3
"""lifecycle_intent.py (v3.27.0 WP1 — Process Applicability & Lifecycle Truth)

Единая стадия жизненного цикла задачи. Определяется детерминированно из состояния WorkItem
(gates, evidence, blueprint, delivery receipt). Не требует явного указания пользователем.

Lifecycle stages (упрощённый enum):
  discovery      — исследование, обсуждение, сбор требований (до спецификации)
  implementation — реализация, прогон движка, сбор evidence
  review         — готово к ревью, PR открыт, ожидает review/approval
  delivery       — готово к доставке, все гейты пройдены, ожидает merge/deploy
  completed      — доставлено и подтверждено (DeliveryReceipt получен)

Terminal states:
  cancelled      — задача отменена (с причиной)
  superseded     — задача заменена другой (с указанием какой)
  abandoned      — задача заброшена (без результата)

Детерминированный вывод:
  status=draft + нет evidence         → discovery
  status=draft + есть evidence        → implementation
  status=done + нет PR                → implementation
  status=done + PR open               → review
  status=done + PR merged             → delivery
  status=done + DeliveryReceipt       → completed
  status=blocked                      → implementation (или discovery если нет evidence)
  status=needs_human_decision         → review (ожидает approval)
  status=needs_more_evidence          → implementation (собирает evidence)

CLI: lifecycle_intent.py --status done --has-pr true --pr-merged false --has-receipt false
     lifecycle_intent.py --selftest
"""
from __future__ import annotations

import json
import sys

# Lifecycle stages
LIFECYCLE_STAGES = (
    "discovery",
    "implementation",
    "review",
    "delivery",
    "completed",
)

# Terminal states
TERMINAL_STATES = (
    "cancelled",
    "superseded",
    "abandoned",
)

ALL_STATES = LIFECYCLE_STAGES + TERMINAL_STATES


def derive(workitem_status: str, has_evidence: bool = False, has_pr: bool = False,
           pr_merged: bool = False, has_receipt: bool = False,
           terminal_state: str = None) -> str:
    """Детерминированно вывести lifecycle_intent из состояния WorkItem.

    Args:
        workitem_status: текущий status WorkItem (draft/done/blocked/needs_human_decision/needs_more_evidence)
        has_evidence: есть ли собранный evidence (implementation_verification passed)
        has_pr: открыт ли PR
        pr_merged: смержен ли PR
        has_receipt: получен ли DeliveryReceipt
        terminal_state: если задача в терминальном состоянии (cancelled/superseded/abandoned)

    Returns:
        lifecycle_intent: одна из LIFECYCLE_STAGES или TERMINAL_STATES
    """
    # Терминальные состояния имеют приоритет
    if terminal_state and terminal_state in TERMINAL_STATES:
        return terminal_state

    # completed — есть DeliveryReceipt
    if has_receipt:
        return "completed"

    # delivery — PR смержен, но нет receipt
    if pr_merged:
        return "delivery"

    # review — PR открыт, ожидает review
    if has_pr:
        return "review"

    # needs_human_decision — ожидает approval (это форма review)
    if workitem_status == "needs_human_decision":
        return "review"

    # implementation — есть evidence или статус done/needs_more_evidence
    # blocked без evidence → discovery (застрял на старте)
    # blocked с evidence → implementation (застрял в процессе)
    if has_evidence or workitem_status in ("done", "needs_more_evidence"):
        return "implementation"

    # discovery — по умолчанию для draft/blocked без evidence
    return "discovery"


def validate_transition(from_stage: str, to_stage: str) -> bool:
    """Проверить, что переход между стадиями допустим.
    Возвращает True если переход валиден, False если нет.

    Допустимые переходы:
    - discovery → implementation, cancelled, abandoned
    - implementation → review, delivery, completed, cancelled, superseded, abandoned
    - review → implementation (отклонено), delivery, completed, cancelled, superseded, abandoned
    - delivery → completed, cancelled, superseded, abandoned
    - completed → (терминальное, переходы запрещены)
    - cancelled/superseded/abandoned → (терминальные, переходы запрещены)
    """
    if from_stage not in ALL_STATES or to_stage not in ALL_STATES:
        return False

    # Из терминальных состояний переходы запрещены
    if from_stage in TERMINAL_STATES:
        return False

    # В completed можно перейти из любого нетерминального
    if to_stage == "completed":
        return from_stage in LIFECYCLE_STAGES

    # В терминальные можно перейти из любого нетерминального
    if to_stage in TERMINAL_STATES:
        return from_stage in LIFECYCLE_STAGES

    # Обычные переходы
    valid_transitions = {
        "discovery": {"implementation", "cancelled", "abandoned"},
        "implementation": {"review", "delivery", "completed", "cancelled", "superseded", "abandoned"},
        "review": {"implementation", "delivery", "completed", "cancelled", "superseded", "abandoned"},
        "delivery": {"completed", "cancelled", "superseded", "abandoned"},
    }

    return to_stage in valid_transitions.get(from_stage, set())


def intent_to_lifecycle(cli_intent: str) -> str | None:
    """Маппинг CLI intent на ожидаемый lifecycle_intent.
    Возвращает None если intent не связан с lifecycle transition.

    Это используется для определения, какой lifecycle_intent должен быть
    после выполнения команды.
    """
    mapping = {
        "discuss": "discovery",
        "specify": "discovery",  # спецификация — часть discovery
        "plan": "discovery",     # планирование — часть discovery
        "run": "implementation",
        "do": "implementation",
        "review": "review",
        "resume": None,  # resume не меняет lifecycle, продолжает текущий
    }
    return mapping.get(cli_intent)


def selftest():
    """Selftest: контракты и логика derive."""
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    # 1. derive: draft без evidence → discovery
    expect("derive: draft без evidence → discovery",
           derive("draft") == "discovery")

    # 2. derive: draft с evidence → implementation
    expect("derive: draft с evidence → implementation",
           derive("draft", has_evidence=True) == "implementation")

    # 3. derive: done без PR → implementation
    expect("derive: done без PR → implementation",
           derive("done") == "implementation")

    # 4. derive: done с PR → review
    expect("derive: done с PR → review",
           derive("done", has_pr=True) == "review")

    # 5. derive: done с merged PR → delivery
    expect("derive: done с merged PR → delivery",
           derive("done", has_pr=True, pr_merged=True) == "delivery")

    # 6. derive: done с receipt → completed
    expect("derive: done с receipt → completed",
           derive("done", has_receipt=True) == "completed")

    # 7. derive: needs_human_decision → review
    expect("derive: needs_human_decision → review",
           derive("needs_human_decision") == "review")

    # 8. derive: blocked без evidence → discovery
    expect("derive: blocked без evidence → discovery",
           derive("blocked") == "discovery")

    # 9. derive: blocked с evidence → implementation
    expect("derive: blocked с evidence → implementation",
           derive("blocked", has_evidence=True) == "implementation")

    # 10. derive: terminal state имеет приоритет
    expect("derive: cancelled имеет приоритет",
           derive("done", has_receipt=True, terminal_state="cancelled") == "cancelled")

    # 11. validate_transition: допустимые переходы
    expect("validate_transition: discovery → implementation допустим",
           validate_transition("discovery", "implementation") is True)
    expect("validate_transition: implementation → review допустим",
           validate_transition("implementation", "review") is True)
    expect("validate_transition: review → delivery допустим",
           validate_transition("review", "delivery") is True)
    expect("validate_transition: delivery → completed допустим",
           validate_transition("delivery", "completed") is True)

    # 12. validate_transition: недопустимые переходы
    expect("validate_transition: completed → discovery запрещён",
           validate_transition("completed", "discovery") is False)
    expect("validate_transition: cancelled → implementation запрещён",
           validate_transition("cancelled", "implementation") is False)

    # 13. intent_to_lifecycle: маппинг CLI intents
    expect("intent_to_lifecycle: discuss → discovery",
           intent_to_lifecycle("discuss") == "discovery")
    expect("intent_to_lifecycle: run → implementation",
           intent_to_lifecycle("run") == "implementation")
    expect("intent_to_lifecycle: review → review",
           intent_to_lifecycle("review") == "review")
    expect("intent_to_lifecycle: resume → None",
           intent_to_lifecycle("resume") is None)

    # 14. ALL_STATES содержит все стадии
    expect("ALL_STATES содержит discovery, implementation, review, delivery, completed",
           all(s in ALL_STATES for s in ("discovery", "implementation", "review", "delivery", "completed")))
    expect("ALL_STATES содержит терминальные cancelled, superseded, abandoned",
           all(s in ALL_STATES for s in ("cancelled", "superseded", "abandoned")))

    print("lifecycle_intent selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())

    # CLI
    status = None
    has_evidence = False
    has_pr = False
    pr_merged = False
    has_receipt = False
    terminal_state = None
    as_json = "--json" in sys.argv

    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--status" and i < len(sys.argv) - 1:
            status = sys.argv[i + 1]
        elif arg == "--has-evidence" and i < len(sys.argv) - 1:
            has_evidence = sys.argv[i + 1].lower() in ("true", "1", "yes")
        elif arg == "--has-pr" and i < len(sys.argv) - 1:
            has_pr = sys.argv[i + 1].lower() in ("true", "1", "yes")
        elif arg == "--pr-merged" and i < len(sys.argv) - 1:
            pr_merged = sys.argv[i + 1].lower() in ("true", "1", "yes")
        elif arg == "--has-receipt" and i < len(sys.argv) - 1:
            has_receipt = sys.argv[i + 1].lower() in ("true", "1", "yes")
        elif arg == "--terminal" and i < len(sys.argv) - 1:
            terminal_state = sys.argv[i + 1]

    if not status:
        print("Usage: lifecycle_intent.py --status draft|done|blocked|... [--has-evidence true] [--has-pr true] [--pr-merged true] [--has-receipt true] [--terminal cancelled|superseded|abandoned] [--json]")
        sys.exit(1)

    result = derive(status, has_evidence, has_pr, pr_merged, has_receipt, terminal_state)

    if as_json:
        print(json.dumps({"lifecycle_intent": result, "status": status,
                         "has_evidence": has_evidence, "has_pr": has_pr,
                         "pr_merged": pr_merged, "has_receipt": has_receipt,
                         "terminal_state": terminal_state}, ensure_ascii=False, indent=2))
    else:
        print(f"LIFECYCLE_INTENT: {result}")
        print(f"  (derived from status={status}, has_evidence={has_evidence}, has_pr={has_pr}, "
              f"pr_merged={pr_merged}, has_receipt={has_receipt}, terminal={terminal_state})")
