#!/usr/bin/env python3
"""invariants.py (v1.0.0) — Formal invariant catalog for the AI Ops Kit critical path.

Machine-checkable invariant specifications for: preflight, execution pipeline, delivery,
usage honesty, and budget. Each invariant has a unique ID, description, severity, and
a check function that returns True when the invariant HOLDS.

Usage:
    from ai_ops_kit.gates.invariants import check_invariant, ALL_INVARIANTS
    assert check_invariant("INV-PREFLIGHT-001", blocked=True, reasons=["spec missing"])

Селфтесты инвариантов — в tests/unit/test_invariants_selftest.py (pytest).
"""
from __future__ import annotations

import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402
# ============================================================================
# Invariant registry
# ============================================================================

ALL_INVARIANTS: list[dict] = []


def _register(inv: dict) -> dict:
    ALL_INVARIANTS.append(inv)
    return inv


def check_invariant(invariant_id: str, **kwargs) -> bool:
    """Check a single invariant by ID. Returns True if the invariant holds."""
    for inv in ALL_INVARIANTS:
        if inv["id"] == invariant_id:
            return inv["check"](**kwargs)
    raise KeyError(f"Unknown invariant: {invariant_id}")


# ============================================================================
# Preflight Invariants
# ============================================================================

_register({
    "id": "INV-PREFLIGHT-001",
    "description": "If blocked=True, reasons must be non-empty",
    "severity": "critical",
    "check": lambda blocked, reasons, **kw: (not blocked) or (len(reasons) > 0),
})

_register({
    "id": "INV-PREFLIGHT-002",
    "description": "If ok=False, blocked must be True (no silent failures)",
    "severity": "critical",
    "check": lambda ok, blocked, **kw: ok or blocked,
})

_register({
    "id": "INV-PREFLIGHT-003",
    "description": "For ENGINEERING/PRODUCT/CRITICAL without spec artifact and not author mode, must be blocked",
    "severity": "critical",
    "check": lambda task_type, spec_artifact, author, blocked, **kw: (
        not (task_type in ("ENGINEERING", "PRODUCT", "CRITICAL") and not spec_artifact and not author)
    ) or blocked,
})

_register({
    "id": "INV-PREFLIGHT-004",
    "description": "reevaluate_only=True marks spec check with skipped_reevaluate flag",
    "severity": "warning",
    "check": lambda reevaluate_only, spec_check, **kw: (
        not reevaluate_only
    ) or (isinstance(spec_check, dict) and spec_check.get("skipped_reevaluate") is True),
})

_register({
    "id": "INV-PREFLIGHT-005",
    "description": "classification check always present in checks dict",
    "severity": "critical",
    "check": lambda checks, **kw: "classification" in checks,
})


# ============================================================================
# Execution Pipeline Invariants
# ============================================================================

_PIPELINE_REQUIRED_KEYS = {"overall_status", "ready_for_pr", "gates"}

_register({
    "id": "INV-PIPELINE-001",
    "description": "run_pipeline result always contains required keys (overall_status, ready_for_pr, gates)",
    "severity": "critical",
    "check": lambda result, **kw: isinstance(result, dict) and _PIPELINE_REQUIRED_KEYS.issubset(result.keys()),
})

_register({
    "id": "INV-PIPELINE-002",
    "description": "If ready_for_pr=True, overall_status must not be 'error'",
    "severity": "critical",
    "check": lambda ready_for_pr, overall_status, **kw: (
        not ready_for_pr
    ) or (overall_status != "error"),
})

_register({
    "id": "INV-PIPELINE-003",
    "description": "If security gate is blocking, ready_for_pr must be False",
    "severity": "critical",
    "check": lambda security_blocking, ready_for_pr, **kw: (
        not security_blocking
    ) or (not ready_for_pr),
})

_register({
    "id": "INV-PIPELINE-004",
    "description": "changed_files is always a list (never None)",
    "severity": "critical",
    "check": lambda changed_files, **kw: isinstance(changed_files, list),
})


# ============================================================================
# Delivery Invariants
# ============================================================================

_register({
    "id": "INV-DELIVERY-001",
    "description": "DeliveryReceipt with sha_verified=True must have remote_sha",
    "severity": "critical",
    "check": lambda sha_verified, remote_sha, **kw: (
        not sha_verified
    ) or (remote_sha is not None and remote_sha != ""),
})

_register({
    "id": "INV-DELIVERY-002",
    "description": "DeliveryReceipt status 'reconciled' implies sha_verified=True",
    "severity": "critical",
    "check": lambda status, sha_verified, **kw: (
        status != "reconciled"
    ) or sha_verified,
})

_register({
    "id": "INV-DELIVERY-004",
    "description": "DeliveryReceipt with checks_verified=True must have checks_total >= 1",
    "severity": "critical",
    # R-41: вердикт «доставку проверяли» нельзя выдать при нуле прогонов. Инвариант закрывает не
    # ошибку записи, а соблазн: единственный способ получить checks_verified=True — реальные прогоны.
    "check": lambda checks_verified=None, checks_total=None, **kw: (
        not checks_verified
    ) or (isinstance(checks_total, int) and checks_total >= 1),
})

_register({
    "id": "INV-DELIVERY-003",
    "description": "DeliveryIntent always has commit_sha and branch",
    "severity": "critical",
    "check": lambda commit_sha, branch, **kw: (
        commit_sha is not None and commit_sha != ""
        and branch is not None and branch != ""
    ),
})


# ============================================================================
# Usage Honesty Invariants
# ============================================================================

_register({
    "id": "INV-USAGE-001",
    "description": "usage_status=unavailable → input_tokens=None AND output_tokens=None",
    "severity": "critical",
    "check": lambda usage_status, input_tokens, output_tokens, **kw: (
        usage_status != "unavailable"
    ) or (input_tokens is None and output_tokens is None),
})

_register({
    "id": "INV-USAGE-002",
    "description": "usage_status=measured → at least one token counter is not None",
    "severity": "critical",
    "check": lambda usage_status, input_tokens, output_tokens, **kw: (
        usage_status != "measured"
    ) or (input_tokens is not None or output_tokens is not None),
})

_register({
    "id": "INV-USAGE-003",
    "description": "cost_status=measured → cost is not None",
    "severity": "critical",
    "check": lambda cost_status, cost, **kw: (
        cost_status != "measured"
    ) or (cost is not None),
})

_register({
    "id": "INV-USAGE-004",
    "description": "cost is never negative",
    "severity": "critical",
    "check": lambda cost, **kw: cost is None or cost >= 0,
})


# ============================================================================
# Budget Invariants
# ============================================================================

_register({
    "id": "INV-BUDGET-001",
    "description": "model_calls never exceeds max_model_calls (when set)",
    "severity": "critical",
    "check": lambda model_calls, max_model_calls, **kw: (
        max_model_calls is None
    ) or (model_calls <= max_model_calls),
})

_register({
    "id": "INV-BUDGET-002",
    "description": "remaining_calls() == max_model_calls - model_calls (when max is set)",
    "severity": "critical",
    "check": lambda remaining_calls, max_model_calls, model_calls, **kw: (
        max_model_calls is None
    ) or (remaining_calls == max(0, max_model_calls - model_calls)),
})

_register({
    "id": "INV-BUDGET-003",
    "description": "BudgetExceeded raised iff model_calls >= max_model_calls (when max is set)",
    "severity": "critical",
    "check": lambda budget_exceeded_raised, model_calls, max_model_calls, **kw: (
        max_model_calls is None
    ) or (budget_exceeded_raised == (model_calls >= max_model_calls)),
})


def main(argv):
    # Ветки `--selftest` здесь нет (ревизия 2026-08-11): функция удалена в v3.30 вместе с
    # переносом селфтестов в pytest, а вызов остался и мог только упасть с `NameError`.
    # Селфтест инвариантов — `tests/unit/test_invariants_selftest.py`.
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
