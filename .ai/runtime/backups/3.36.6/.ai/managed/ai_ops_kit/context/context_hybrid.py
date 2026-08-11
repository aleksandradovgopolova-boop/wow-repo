#!/usr/bin/env python3
r"""context_hybrid.py (v3.7.0) — hybrid-контекст: mandatory v1 + разрешённые v2-additions.

Промежуточная ступень promotion Context Engine (shadow -> HYBRID -> default). Hybrid НЕ заменяет v1:
execution получает ВЕСЬ обязательный контекст v1 (policy/spec/decisions) ПЛЮС дополнительные кандидаты
Context Engine v2 — и ТОЛЬКО если `context_promotion_gate` подтвердил готовность view. Иначе hybrid
отклоняется и execution честно остаётся на v1-only (fail-safe, без потери контекста).

Инварианты:
  - v1 mandatory НИКОГДА не теряется (hybrid = v1 ∪ additions, не замена);
  - v2 ТОЛЬКО добавляет (additions = included_v2 \ v1);
  - hybrid допускается ТОЛЬКО при `context_promotion_gate.ready` (5 trust-контрактов);
  - не готов -> mode=v1-only, additions=[], execution на v1 (безопасный дефолт).

Сам feeding hybrid-контекста модели — за флагом в runtime (живой шаг v3.7). Здесь — детерминированная
СБОРКА + guard (offline, проверяемо). Только stdlib.  CLI: context_hybrid.py --selftest
"""
from __future__ import annotations

import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402
from ai_ops_kit.context import context_promotion_gate as cpg   # noqa: E402

DEFAULT_MODEL_WINDOW = 120000


def build_hybrid(v1_files, v2_view, allowed_classes, model_window=DEFAULT_MODEL_WINDOW,
                 rule_refs=None, policy_refs=None):
    """Собрать hybrid-контекст ИЗ уже построенного v2-view. Pure. -> запись hybrid.

    v3.7.0-fix: mandatory = spec/decisions ФАЙЛЫ (v1_files) + applicable rules + policy references
    (rules — kit-side категории, policies — AFP/DCP id; это ССЫЛКИ, не читаемые child-файлы)."""
    v1 = list(dict.fromkeys(v1_files or []))          # обязательный v1 (файлы), уникально, порядок сохранён
    refs = [f"rule:{r}" for r in (rule_refs or [])] + [f"policy:{p}" for p in (policy_refs or [])]
    ready = cpg.check_promotion_readiness(v2_view, allowed_classes, model_window)
    if not ready["ready"]:
        return {"mode": "v1-only", "promotion_ready": False,
                "reason": "context_promotion_gate НЕ пройден -> hybrid отклонён, execution на v1 (fail-safe)",
                "violations": ready["violations"], "contracts": ready["contracts"],
                "mandatory_v1": v1, "mandatory_references": refs, "v2_additions": [], "context": v1}
    included_v2 = [i.get("file") for i in v2_view.get("included", []) if i.get("file")]
    additions = [f for f in included_v2 if f not in set(v1)]   # ТОЛЬКО добавляем
    return {"mode": "hybrid", "promotion_ready": True,
            "reason": "promotion gate ready -> mandatory (v1 файлы + rules/policy refs) + v2-additions",
            "contracts": ready["contracts"], "mandatory_v1": v1, "mandatory_references": refs,
            "v2_additions": additions, "context": v1 + additions}


def build_hybrid_from_child(child_root, query, role, *, sha, afp, dcp=None, v1_mandatory=None,
                            rule_refs=None, policy_refs=None, budget=None,
                            model_window=DEFAULT_MODEL_WINDOW, repo_id=None, require_snapshot=True):
    """Композиция: build_context(v2) -> promotion gate -> hybrid. Политики/содержимое — как в shadow.
    budget — токен-лимит из НАСТОЯЩЕГО child BudgetContract (не внутренний default)."""
    from ai_ops_kit.context import context_engine as ce
    allowed = ce.cr.role_allowed_classes(afp, role) if afp else set()
    view = ce.build_context(child_root, query, role, sha=sha, afp=afp, dcp=dcp,
                            budget_tokens=(budget or ce.DEFAULT_BUDGET_TOKENS),
                            v1_mandatory=v1_mandatory, repo_id=repo_id, require_snapshot=require_snapshot)
    h = build_hybrid(v1_mandatory or [], view, allowed, model_window,
                     rule_refs=rule_refs, policy_refs=policy_refs)
    h["snapshot_verified"] = view.get("snapshot_verified")
    h["cache_key"] = view.get("cache_key")
    h["execution_uses"] = "context_compiler_v1"   # feeding hybrid модели — за флагом (живой шаг)
    return h


def main(argv):
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
