#!/usr/bin/env python3
"""Preflight Truth — проверки ДО запуска модели (v2.115).

Аудит: Spec-First блокировал ДОСТАВКУ, а не РЕАЛИЗАЦИЮ — pipeline сначала гонял tool loop, писал код и
коммит, и лишь ПОТОМ проверял полноту спеки. Это delivery-gate, не Spec-First. Здесь — единый preflight,
который выполняется ДО tool loop; при провале модель НЕ запускается, правки/коммит НЕ создаются.

Порядок (fail-closed):
  classification -> ContextPayload собран -> spec достаточна -> задача атомарна ИЛИ декомпозиция
  подтверждена -> context budget не превышен -> необходимые human approvals присутствуют -> только
  потом tool loop.

Инварианты честности:
  * неполная (существующая) спека -> блок ДО реализации (ноль вызовов tool loop, ноль коммитов);
  * context overflow -> блок ДО исполнения;
  * неатомарная задача -> блок, пока человек не подтвердит декомпозицию ИЛИ не выберет один пакет;
  * ошибки Context Compiler/Spec/Planner -> fail-closed для ENGINEERING/PRODUCT/CRITICAL;
  * доменные human_approval_conditions исполняются через ApprovalRecord (не boolean).

Использование:
  preflight.py --selftest
"""
from __future__ import annotations

import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402
from ai_ops_kit.gates import spec_levels   # noqa: E402
from ai_ops_kit.gates import approvals     # noqa: E402
from ai_ops_kit.shared.contracts import PreflightResult  # noqa: E402

# Уровни, для которых слой контекста обязан быть здоров (ошибки -> fail-closed, не «продолжаем молча»).
_HEAVY = {"ENGINEERING", "PRODUCT", "CRITICAL", "AI_FEATURE", "RESEARCH"}


def assess(signals, child_root, wid, plan=None, bundle=None, payload=None,
           spec_cov=None, work_pkg=None, lifecycle_errors=None, domains=None, author=False,
           reevaluate_only=False) -> PreflightResult:
    """-> PreflightResult {kind, ok, blocked, checks{...}, reasons[]}. Детерминированно, без модели и без правок.
    v3.8.3: reevaluate_only=True — переоценка гейтов УЖЕ построенной фичи (после человеко-approval),
    БЕЗ переавторинга. Build-preconditions (spec-first/atomic/context-budget) НЕ применяются (реализация
    уже была); classification/approvals/lifecycle проверяются (approval — именно то, ради чего переоценка)."""
    signals = dict(signals or {})
    child_root = Path(child_root)
    tt = (signals.get("task_type") or (plan or {}).get("base_workflow") or "QUICK").upper()
    heavy = tt in _HEAVY
    lifecycle_errors = list(lifecycle_errors or [])
    checks, reasons = {}, []

    def block(reason):
        reasons.append(reason)

    # 1. classification
    ok_class = bool(tt)
    checks["classification"] = {"ok": ok_class, "task_type": tt}
    if not ok_class:
        block("classification: не удалось определить тип задачи")

    # 2. ContextPayload собран (для heavy — обязателен; ошибка сборки -> fail-closed)
    payload_ok = payload is not None and bool((payload or {}).get("text"))
    checks["context_payload"] = {"ok": payload_ok or (not heavy), "built": payload is not None}
    if heavy and not payload_ok:
        block("context_payload: compiled payload не собран для heavy-задачи (fail-closed)")

    # 3. spec достаточна. Два условия (v2.121 P1.1 — обязательность спеки ДО tool loop для heavy):
    #    (a) существующая, но неполная спека НЕ пускает в реализацию (фикс #1, все workflow);
    #    (b) для heavy (ENGINEERING/PRODUCT/CRITICAL) спека ОБЯЗАТЕЛЬНА до реализации: её отсутствие
    #        блокирует, ЕСЛИ прогон не идёт с --author (тогда движок авторизует спеку пре-стадией, а
    #        артефакт-гейты specification/requirements всё равно проверят её на готовность). QUICK — light.
    spec_artifact = bool((spec_cov or {}).get("spec_artifact"))
    spec_missing = list((spec_cov or {}).get("blocking_missing") or [])
    spec_incomplete = spec_artifact and bool(spec_missing)
    spec_absent_heavy = heavy and (not spec_artifact) and (not author)
    spec_ok = not (spec_incomplete or spec_absent_heavy)
    checks["spec"] = {"ok": spec_ok, "artifact_present": spec_artifact, "missing": spec_missing,
                      "required_for_heavy": heavy, "author_stage": bool(author)}
    if reevaluate_only:
        checks["spec"]["skipped_reevaluate"] = True   # build-precondition неприменим к переоценке
    elif spec_incomplete:
        block(f"spec-first: спека features/{wid}/spec.yaml существует, но неполна "
              f"(не заполнено: {', '.join(spec_missing)}) — реализация не начинается")
    elif spec_absent_heavy:
        block(f"spec-first: {tt} требует спеку ДО реализации — features/{wid}/spec.yaml отсутствует; "
              f"создай спеку (ai-ops specify/new) ИЛИ запусти с --author (движок авторизует её пре-стадией)")

    # 4. атомарность. v2.120 (P0.4/P0.6): boolean-подтверждения НЕДОСТАТОЧНО — неатомарная задача
    #    идёт либо через sequential executor, либо как КОНКРЕТНЫЙ существующий WorkPackage
    #    (work_package_id, который РЕАЛЬНО есть в плане). Вымышленный id и голый decomposition_confirmed
    #    больше не пускают блоб одним tool loop.
    should_decompose = bool((work_pkg or {}).get("should_decompose"))
    selected = signals.get("work_package_id")
    # авторитетный источник id пакетов — из sequence-исполнителя (он строит план), плюс work_pkg прогона
    plan_ids = set(signals.get("_sequence_plan_ids") or [])
    plan_ids |= {p.get("id") for p in ((work_pkg or {}).get("work_packages") or [])}
    selected_valid = bool(selected) and selected in plan_ids
    atomic_ok = (not should_decompose) or selected_valid or reevaluate_only
    checks["atomic"] = {"ok": atomic_ok, "should_decompose": should_decompose,
                        "selected_package": selected, "selected_valid": selected_valid}
    if not atomic_ok:
        n = len((work_pkg or {}).get("work_packages") or [])
        if selected and not selected_valid:
            block(f"atomic-planning: work_package_id='{selected}' отсутствует в плане ({n} пакетов) — "
                  f"нельзя исполнить по вымышленному ID")
        else:
            block(f"atomic-planning: задача не атомарна ({n} пакетов) — исполняй через sequential "
                  f"executor (ai-ops run … --sequential) ИЛИ выбери СУЩЕСТВУЮЩИЙ work_package_id из плана")

    # 5. context budget не превышен -> блок ДО исполнения
    overflow = bool((bundle or {}).get("overflow"))
    checks["context_budget"] = {"ok": (not overflow) or reevaluate_only, "overflow": overflow}
    if overflow and not reevaluate_only:
        block("context-budget: контекст задачи превышает бюджет — декомпозируй до исполнения")

    # 6. human approvals: доменные условия через ApprovalRecord (+ destructive)
    appr = approvals.check(signals, child_root, wid, domains=domains)
    missing = list(appr["missing"])
    # destructive не является security-доменом -> требуем отдельный ApprovalRecord "destructive"
    if signals.get("destructive"):
        recs = approvals.load_approvals(child_root, wid)
        has_destructive = any(r.get("approval") == "destructive" and approvals._record_valid(r) for r in recs)
        if not has_destructive:
            missing = missing + [{"domain": "destructive", "condition": "деструктивное действие",
                                  "trigger": "destructive", "reason": "нет валидного ApprovalRecord"}]
    approvals_ok = not missing
    checks["approvals"] = {"ok": approvals_ok, "required": appr["required"], "missing": missing}
    if not approvals_ok:
        block("human-approval: не хватает одобрений (ApprovalRecord): "
              + ", ".join(m["domain"] for m in missing))

    # 7. ошибки слоя контекста -> fail-closed для heavy
    lifecycle_ok = (not lifecycle_errors) or (not heavy)
    checks["lifecycle"] = {"ok": lifecycle_ok, "errors": lifecycle_errors}
    if heavy and lifecycle_errors:
        block("lifecycle: сбой слоя контекста (Compiler/Spec/Planner) для heavy-задачи -> "
              "fail-closed: " + "; ".join(lifecycle_errors))

    # 7. v3.21.0 EngOps срез 3: ЭКОНОМИЧЕСКАЯ граница ДО tool loop. Прежде деньги узнавались ПОСЛЕ
    #    траты: контекстный бюджет (шаг 5) денег не касается, Budget.charge_call рвётся на N-м вызове
    #    (когда N-1 уже оплачены), cost_account сверяет расход после прогона. Здесь — оценка по истории
    #    ЭТОГО репозитория против лимитов RunPlan.execution_budget ДО первого вызова.
    #    Блокирует только ПЕССИМИСТИЧНАЯ оценка против жёсткого лимита (единственный знаемый случай);
    #    нет истории -> НЕ блок (иначе первый прогон в репозитории невозможен), а честный proceed_unknown.
    #    reevaluate_only — переоценка уже построенной фичи, новой существенной траты нет: не применяем.
    if not reevaluate_only:
        try:
            from ai_ops_kit.gates import economic_preflight as _ep
            _est = _ep.estimate(child_root)
            _ev = _ep.check_economics(_est, (plan or {}).get("execution_budget") or {},
                                      _ep.policy_from_config(child_root))
        except Exception as e:  # noqa: BLE001 — недоступность оценщика не выдаём за «бесплатно»
            checks["economic_budget"] = {"ok": True, "verdict": "proceed_unknown",
                                         "estimate_status": "unavailable",
                                         "error": f"{type(e).__name__}: {e}"}
        else:
            checks["economic_budget"] = {
                "ok": _ev["allowed"], "verdict": _ev["verdict"],
                "estimate_status": _est["status"],
                "cost_median": _est["cost_median"], "cost_max": _est["cost_max"],
                "sample_tasks": _est["sample_tasks"],
                "advisories": [a["rule"] for a in _ev["advisories"]],
            }
            if not _ev["allowed"]:
                block("economic-preflight: " + "; ".join(x["detail"] for x in _ev["violations"])
                      or "economic-preflight: прогон не разрешён политикой экономики")

    ok = not reasons
    return {"schema_version": 1, "kind": "PreflightTruth", "ok": ok, "blocked": not ok,
            "task_type": tt, "checks": checks, "reasons": reasons}


def main(argv):
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
