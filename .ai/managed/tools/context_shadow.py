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

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / "tools"))
import context_engine as ce   # noqa: E402


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


def selftest():
    import tempfile
    import yaml
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    def _mkrepo(root, with_policies=True):
        (root / "src").mkdir(parents=True)
        (root / "src" / "pricing.py").write_text("def apply_discount(a):\n    return a*0.9  # discount\n", encoding="utf-8")
        (root / "src" / "order.py").write_text("import pricing\n# discount order flow\n", encoding="utf-8")
        (root / ".gitignore").write_text(".ai/\n", encoding="utf-8")
        pol = root / ".ai" / "policies"
        pol.mkdir(parents=True)
        (pol / "state.py").write_text("# discount secret internal state\n", encoding="utf-8")
        if with_policies:
            (pol / "access-filter.yaml").write_text(yaml.safe_dump({
                "id": "CHILD-AFP", "kind": "AccessFilterPolicy",
                "rules": [{"role": "executor", "allowed_classes": ["public", "internal"]}]}), encoding="utf-8")
            (pol / "data-classification.yaml").write_text(yaml.safe_dump({
                "id": "CHILD-DCP", "kind": "DataClassificationPolicy", "default_class": "internal"}),
                encoding="utf-8")
            (pol / "budget.yaml").write_text(yaml.safe_dump({
                "id": "CHILD-BUD", "kind": "BudgetContract",
                "scopes": [{"scope": "run", "token_budget": 15000}]}), encoding="utf-8")
        ce._git(root, "init", "-q")
        ce._git(root, "add", "src", ".gitignore")
        ce._git(root, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", "init")
        _, head, _ = ce._git(root, "rev-parse", "HEAD")
        return head

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        head = _mkrepo(root)

        sh = build_shadow(root, "discount", role="executor", sha=head)
        expect("shadow: mode=shadow, execution_uses=v1 (не управляет прогоном)",
               sh["mode"] == "shadow" and sh["execution_uses"] == "context_compiler_v1")
        expect("shadow валиден + snapshot доказан (HEAD==sha, чистое дерево)",
               sh["valid"] is True and sh["snapshot_verified"] is True)
        expect("shadow строит ПОЛНУЮ цепочку (sources_used: fulltext + graph)",
               sh["sources_used"]["fulltext"] >= 1 and "graph_added" in sh["sources_used"])
        expect("shadow находит src/*.py по 'discount'", "src/order.py" in sh["included"])
        expect("shadow НЕ сканирует скрытые .ai/ (engine state вне контекста)",
               all(not f.startswith(".ai") for f in sh["included"]))
        expect("shadow budget взят из child BudgetContract (15000, не жёсткие 20000)",
               sh["budget_tokens"] == 15000)
        expect("shadow.cache_key привязан к sha + child-политикам (exact-revision, no demo)",
               f"sha:{head}" in sh["cache_key"] and "afp:CHILD-AFP" in sh["cache_key"])

        cmp = compare(sh, ["src/pricing.py", "docs/legacy.md"])
        expect("compare: overlap/v1_only/v2_only считаются",
               "src/pricing.py" in cmp["overlap"] and "docs/legacy.md" in cmp["v1_only"])

        # snapshot не доказан (грязное дерево) -> shadow невалиден
        (root / "src" / "pricing.py").write_text("# dirty change\n", encoding="utf-8")
        shd = build_shadow(root, "discount", sha=head)
        expect("грязное дерево -> shadow.valid=false (snapshot не доказан)", shd["valid"] is False)

        # без точного SHA shadow НЕ строится
        try:
            build_shadow(root, "discount", sha=None)
            expect("без SHA shadow НЕ строится -> ValueError", False)
        except ValueError:
            expect("без SHA shadow НЕ строится -> ValueError", True)

    # нет child-политики -> deny-by-default (никакого demo-fallback в runtime)
    with tempfile.TemporaryDirectory() as td2:
        r2 = Path(td2)
        head2 = _mkrepo(r2, with_policies=False)
        sh2 = build_shadow(r2, "discount", sha=head2)
        expect("нет child-AFP -> deny-by-default, ничего не включается (no demo policy в runtime)",
               sh2["included_count"] == 0)

    print("context_shadow selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
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
