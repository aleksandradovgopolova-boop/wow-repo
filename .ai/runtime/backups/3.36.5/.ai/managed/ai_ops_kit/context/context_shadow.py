#!/usr/bin/env python3
"""context_shadow.py (v3.6.7) — SHADOW-режим ПОЛНОЙ Context Engine v2 рядом с боевым v1.

Ревью владельца: НЕ заменять старый Context Compiler сразу. Первый режим wiring — v1 (обязательный,
им и исполняем) + v2 shadow view рядом: сохранить оба, сравнить источники, execution по-прежнему на v1.

v3.6.7: shadow теперь строит ПОЛНУЮ v2-цепочку через `context_engine.build_context` (full-text +
Repository Graph augmentation + условный semantic-lite + access-filter + rerank + budget), на
политиках CHILD-репо (AFP + DataClassificationPolicy), а не на демо-политике кита. Без точного SHA
shadow НЕ строится. Раньше shadow звал только `build_view()` (full-text) на демо-AFP без DCP.

Shadow НЕ управляет прогоном: чистая наблюдаемость перед промоушеном retrieval в runtime.

CLI:  context_shadow.py <child_root> --query "..." [--role executor] [--sha SHA] | --selftest
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402
from ai_ops_kit.context import context_engine as ce   # noqa: E402


def build_shadow(child_root, query, role="executor", sha=None, afp=None, dcp=None, budget=None,
                 v1_mandatory=None, policy_root=None, require_snapshot=True):
    """Полная v2-цепочка в shadow. sha обязателен (иначе ValueError).

    v3.6.7d: содержимое читается из `child_root` (точный execution-worktree, HEAD==sha), а ПОЛИТИКИ —
    из `policy_root` (основной checkout, где живёт .ai/policies; в worktree .ai gitignore'на). Токен-
    бюджет берётся из настоящего child BudgetContract (не жёсткие 20000). require_snapshot=True (default)
    доказывает snapshot перед чтением — иначе view невалиден."""
    policy_root = policy_root or child_root
    budget_policy = None
    if afp is None and dcp is None:
        afp, dcp, budget_policy = ce.load_child_policies(policy_root)
    budget_tokens = budget if isinstance(budget, int) else ce.budget_tokens_from(budget_policy)
    view = ce.build_context(child_root, query, role, sha=sha, afp=afp, dcp=dcp,
                            budget_tokens=budget_tokens, v1_mandatory=v1_mandatory,
                            require_snapshot=require_snapshot)
    return {"kind": "context-shadow", "mode": "shadow", "execution_uses": "context_compiler_v1",
            "role": role, "query": query, "sha": sha, "cache_key": view["cache_key"],
            "valid": view["valid"], "invalid_reasons": view["invalid_reasons"],
            "snapshot_verified": view["snapshot_verified"], "budget_tokens": view["budget_tokens"],
            "sources_used": view["sources_used"],
            "included": [i["file"] for i in view["included"]],
            "included_count": len(view["included"]), "total_tokens": view["total_tokens"],
            "excluded_access": len(view["excluded_access"]),
            "excluded_budget": len(view["excluded_budget"]),
            "mandatory_missing": view["mandatory_missing"],
            "mandatory_excluded_access": view.get("mandatory_excluded_access", [])}


def compare(shadow: dict, v1_files) -> dict:
    """Сравнение источников v1 vs ПОЛНАЯ v2-цепочка: overlap / только в v1 / только в v2."""
    v2 = set(shadow.get("included", []))
    v1 = set(v1_files or [])
    return {"overlap": sorted(v1 & v2), "v1_only": sorted(v1 - v2), "v2_only": sorted(v2 - v1),
            "v1_count": len(v1), "v2_count": len(v2)}


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1

    def _opt(n, d=None):
        return argv[argv.index(n) + 1] if n in argv else d
    sh = build_shadow(args[0], _opt("--query", ""), _opt("--role", "executor"), _opt("--sha"))
    print(json.dumps(sh, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
