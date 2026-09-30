#!/usr/bin/env python3
"""Билдеры отчёта и стадии конвейера для execution-pipeline.

Вынесено из execution_pipeline.py (разрез фасад + сателлит) без изменения поведения:
секции отчёта (loop/commit/containment/delivery/overall/security-проекция) и стадии прогона
(spec-drift, resolve-policy, run-gates, assess-readiness, check-invariants).
Зависимости берутся из настоящих модулей-соседей (pipeline_*, gate_executor, tool_broker),
а НЕ из execution_pipeline — иначе получился бы циклический импорт. Ни одного обратного ребра:
фасад импортирует этот модуль, этот модуль фасад не импортирует.
"""
from __future__ import annotations

from pathlib import Path

from ai_ops_kit.engine import tool_broker
from ai_ops_kit.gates import gate_executor
from ai_ops_kit.shared import gate_dimensions
from ai_ops_kit.engine.pipeline_readiness import _assess_readiness
from ai_ops_kit.engine.pipeline_failure import _diff_checks, _env_proven_ok
from ai_ops_kit.engine.pipeline_git import _committed_changed_files
from ai_ops_kit.engine.pipeline_evidence import contour_consistency_evidence
from ai_ops_kit.engine.pipeline_helpers import acceptance_blocks_ready


def _build_loop_section(loop, applied):
    """Секция loop в отчёте. v3.38 (K6): вынесено из run_pipeline."""
    return {"stopped": loop["stopped"], "steps": loop["steps"],
            "applied_writes": len(applied), "denied": len(loop["denied"]),
            "denied_reasons": [d.get("reason") for d in loop["denied"]][:10],
            "transcript": [{k: t.get(k) for k in ("step", "op", "allowed", "ok", "done", "reason")
                            if k in t} for t in (loop.get("transcript") or [])][:40]}


def _build_commit_section(work_branch, committed_sha, evidence_revision, revision_matches,
                          changed_for_verification, work_produced_by, tree_clean_before, tree_clean_after):
    """Секция commit в отчёте. v3.38 (K6): вынесено из run_pipeline."""
    return {"branch": work_branch, "sha": committed_sha,
            "evidence_revision": evidence_revision,
            "evidence_on_exact_sha": revision_matches,
            "changed_files": list(changed_for_verification or []),
            "produced_by": work_produced_by,
            "tree_clean_before_checks": tree_clean_before,
            "tree_clean_after_checks": tree_clean_after}


def _build_containment(sandbox, pol, loop):
    """Секция containment в отчёте. v3.38 (K6): вынесено из run_pipeline."""
    return {"sandbox": sandbox, "shell_mode": pol.shell_mode,
            "block_push": pol.block_push, "allow_network": pol.allow_network,
            "shell_path_guard": getattr(pol, "shell_path_guard", False),
            "shell_scope_guard": getattr(pol, "shell_scope_guard", False),
            "shell_path_violations": sum(
                len(((e.get("fs_guard") or {}).get("violations")) or [])
                for e in (loop.get("evidence") or [])),
            # R-43/#786: перечень обязан совпадать с честным списком в шапке tool_broker.py.
            "note": "enforceable-подмножество на уровне брокера: пути закрыты на обоих "
                    "каналах (write — до, shell — пост-фактум с откатом, и неудача отката "
                    "называется, а не скрывается); запись вне корня репозитория, сеть, "
                    "не-git деревья и ИГНОРИРУЕМЫЕ файлы внутри protected-путей (R-43, "
                    "открыт) — по-прежнему нет; полная FS/сеть/ресурс-изоляция — "
                    "контейнерный runtime"}


def _plan_delivery(open_pr, ready, committed_sha, work_branch, base_binding, base_ref, base_sha,
                   work_root, wid, task, delivery_pf):
    """Планирование доставки. v3.38 (K6): вынесено из run_pipeline.
    -> (delivery, delivery_plan, can_deliver)."""
    delivery_plan = None
    can_deliver = bool(open_pr and ready and committed_sha and work_branch)
    if can_deliver:
        delivery = {"requested": True, "base_binding": base_binding, "status": "planned",
                    "reason": "доставку выполняет ТОЛЬКО транзакционный контроллер после durable-фиксации "
                              "lifecycle (run_pipeline не открывает PR)"}
        delivery_plan = {"ready_for_delivery": True, "work_root": str(work_root), "work_branch": work_branch,
                         "base_ref": base_ref, "base_sha": base_sha, "committed_sha": committed_sha,
                         "wid": wid, "task": task, "base_binding": base_binding}
    else:
        delivery = {"requested": bool(open_pr), "base_binding": base_binding,
                    "preflight": delivery_pf,
                    "status": ("not-requested" if not open_pr
                               else ("not-attempted" if not ready else None))}
    return delivery, delivery_plan, can_deliver


def _compute_overall_status(ready, can_deliver, open_pr):
    """Определить итоговый статус прогона. v3.38 (K6): вынесено из run_pipeline."""
    if not ready:
        return "error"
    if can_deliver:
        return "ready-undelivered"
    if not open_pr:
        return "delivered"
    return "delivery-failed"


def _security_pack_for_report(security_pack_result):
    """Вердикт security-пака -> в отчёт через ПРОЕКЦИЮ пака (белый список полей), а не срезом на месте.
    Срез на месте и был дефектом: четыре поля выбирались здесь, и находки терялись по дороге."""
    from ai_ops_kit.security import security_pack as _sp_report
    return _sp_report.for_report(security_pack_result)


def _pipeline_check_spec_drift(task, signals, child_root, feature, write_scope, *,
                               max_steps, commit, baseline_diff, require_fix, sandbox,
                               review, author):
    """Фаза K0-проводки: параметры прогона обязаны оставаться подмножеством объявленного
    контракта ядра (kernel/ports.ExecutionSpec). kernel/ports — контракт ТИПОВ, не шов с
    внедряемыми реализациями (Phase B DI не преследуется), поэтому это страж дрейфа КОНТРАКТА,
    а не проверка реализаций: переименование поля в ports.py или новый параметр без записи в
    контракт краснеет на каждом прогоне, в том числе в дочке."""
    from ai_ops_kit.kernel import ports as _kports
    _spec: _kports.ExecutionSpec = {
        "task": task, "signals": dict(signals or {}), "child_root": str(child_root),
        "feature": feature or "", "write_scope": list(write_scope or []),
        "max_steps": max_steps, "commit": bool(commit), "baseline_diff": bool(baseline_diff),
        "require_fix": bool(require_fix), "sandbox": bool(sandbox),
        "review": bool(review), "author": bool(author)}
    _spec_drift = set(_spec) - set(_kports.ExecutionSpec.__annotations__)
    if _spec_drift:
        raise SystemExit(f"контракт ядра разошёлся с конвейером: полей {sorted(_spec_drift)} "
                         f"нет в kernel/ports.ExecutionSpec — обновите контракт или вызов")


def _pipeline_resolve_policy(policy, sandbox, work_root, write_scope):
    """Фаза 3: политика по умолчанию execution, границы — по work_root.
    v2.81 Containment: даже базовая политика запрещает модели push-ить (block_push=True) —
    доставка (PR) идёт ТОЛЬКО через доверенный delivery-слой, не через tool-loop.
    sandbox=True дополнительно включает allowlist на shell (произвольный shell выключен)
    и denylist на сетевые бинарники — см. tool_broker.sandbox_policy()."""
    if policy is not None:
        return policy
    if sandbox:
        return tool_broker.sandbox_policy(child_root=str(work_root), write_scope=write_scope)
    return tool_broker.Policy(level="execution", child_root=str(work_root), block_push=True,
                              write_scope=write_scope)


def _pipeline_run_gates(plan, gate_ev, committed_sha, signals, not_applicable, exempt_reason, *,
                        reevaluate_only, worktree_rel, child_root, wid):
    """Фаза 7: гейты RunPlan (base + треки) + печать закрытия человеку + персист пройденного
    gate-evidence билда.

    v2.125 (finding живого прогона): security-релевантная НАХОДКА в диффе (новая зависимость/секрет →
    gate_ev.security=fail) обязана блокировать НЕЗАВИСИМО от workflow. QUICK не содержит security-гейта,
    поэтому новая зависимость в QUICK-задаче проскакивала. Форсируем security в оценку, если он упал."""
    _gate_ids = list(plan["gates"])
    if (gate_ev.get("security") or {}).get("status") == "fail" and "security" not in _gate_ids:
        _gate_ids.append("security")
    gates = gate_executor.evaluate(plan["base_workflow"], gate_ev,
                                   gate_ids=_gate_ids, tested_revision=committed_sha,
                                   signals=signals, not_applicable=not_applicable,
                                   exempt_reason=exempt_reason)
    # «ПРОВЕРЕНО» — ЭТО УРОВЕНЬ ЗНАНИЯ, А НЕ СЧЁТ ГЕЙТОВ. #958 + P0 №5 (#982): в лицо человеку идёт
    # хребет НАЗВАННЫХ уровней знания, каждый с честным состоянием (известно / ещё неизвестно), а не
    # счёт «N из M» и не id гейтов. Так «Проверено» не выглядит увереннее заслуженного: непройденное
    # (деплой, события, продуктовый результат) НАЗЫВАЕТСЯ, а не замалчивается — прямое следствие
    # инварианта «no evidence → no claim». Счёт «N из M» и id гейтов остаются в technical-readout
    # (_print_pipeline) и в JSON-отчёте (closure), но не здесь. Атрибуция источника (детерминированная
    # машина vs независимый ревьюер) вшита в формулировки — мнение за автоматический тест не выдаётся.
    _ev = gates.get("evidence_verdict") or {}
    _readout = gate_dimensions.knowledge_readout(_ev)
    if gate_dimensions.knowledge_has_known(_ev):
        print("  Проверено — вот что уже известно о работе:")
    else:
        print("  Проверено — подтверждать пока нечего; вот граница знания:")
    for _line in _readout:
        print(f"    · {_line}")
    # веха 4.2 (#588): вердикт честности evidence — «зелёное» без детерминированной опоры advisory.
    _ev = gates.get("evidence_verdict") or {}
    if _ev and not _ev.get("verified") and _ev.get("advisory"):
        print(f"  evidence: {_ev.get('reason')}")

    # v3.8.3: персистим ПРОЙДЕННОЕ gate-evidence билда (кроме security) по committed_sha в worktree/.ai —
    # чтобы последующий reevaluate (после человеко-approval) переиспользовал model-вердикт code_review и
    # артефакт-гейты БЕЗ ре-ревью (недетерминизм) и без зависимости от клоббер-подверженного run-report.
    # Только non-reevaluate билд с коммитом (reevaluate не перетирает источник).
    if committed_sha and not reevaluate_only and worktree_rel is not None:
        try:
            import json as _json
            _passed = {gid: ev for gid, ev in gate_ev.items()
                       if gid != "security" and isinstance(ev, dict) and ev.get("status") == "pass"}
            # пишем в child_root/.ai (репо-корень), ВНЕ worktree-дерева -> не грязним committed_sha
            (Path(child_root) / ".ai").mkdir(parents=True, exist_ok=True)
            (Path(child_root) / ".ai" / f"reevaluate-evidence-{wid}.json").write_text(
                _json.dumps({"sha": committed_sha, "gate_ev": _passed}, ensure_ascii=False), encoding="utf-8")
        # ЗАПИСЬ того же кеша — симметрично чтению: не записали, значит следующий прогон
        # пересчитает. Вердикт не зависит от наличия файла (ревизия 2026-08-11).
        except Exception:  # noqa: BLE001,S110 — потеря кеша не меняет вердикт, пересчитаем
            pass
    return gates


def _pipeline_assess_readiness(gates, coll, signals, plan, child_root, wid, work_root, *,
                               baseline_diff, baseline_checks, committed_sha, base_sha,
                               reviewer_proposer, budget, loop, require_fix,
                               tree_clean_before_checks, tree_clean_after_checks, prepare_ok,
                               commit, effective_approval_signals, security_pack_result, gate_ev):
    """Фаза 8-вердикт: собирает готовность прогона к PR — evidence-ревизия, spec-depth, baseline-diff,
    квалификация окружения, перепроверка одобрений после диффа, связность контуров и итоговый ready.
    Возвращает dict полей, которые далее проецируются в отчёт и в список not_yet."""
    # честность evidence: ревизия сбора совпадает с зафиксированным SHA (если коммитили)
    evidence_revision = coll.get("revision")
    revision_matches = (committed_sha is not None and evidence_revision == committed_sha)

    # v2.106 ready-критерии уровня спеки: spec-depth enforcement + Real Spec-First + сверка критериев
    #    приёмки (B2-14) + context-budget overflow -> _assess_readiness (K6).
    _rd = _assess_readiness(gates, coll, signals, plan, child_root, wid, work_root,
                            baseline_diff=baseline_diff, baseline_checks=baseline_checks,
                            committed_sha=committed_sha, base_sha=base_sha,
                            reviewer_proposer=reviewer_proposer, budget=budget)
    spec_depth_missing = _rd["spec_depth_missing"]
    spec_depth_ok = _rd["spec_depth_ok"]
    spec_incomplete = _rd["spec_incomplete"]
    spec_bad_status = _rd["spec_bad_status"]
    spec_complete_ok = _rd["spec_complete_ok"]
    _level = _rd["level"]
    acceptance_criteria = _rd["acceptance_criteria"]
    context_overflow = _rd["context_overflow"]

    # baseline-diff (finding живого прогона): что правка сломала/починила против базы
    regressions, fixed = _diff_checks(baseline_checks, coll["checks"]) if baseline_diff else ([], [])
    no_regressions = (len(regressions) == 0) if baseline_diff else None
    # P0.1 (аудит v2.79): baseline-режим делает baseline-осведомлённым ТОЛЬКО
    # implementation_verification (красная база не должна блокировать). ВСЕ ОСТАЛЬНЫЕ блокирующие
    # гейты (requirements/specification/plan/code_review/security/треки) остаются обязательными —
    # иначе baseline-diff обходит их и выдаёт ложный ready. unmet_gates уже только блокирующие.
    other_blocking_unmet = [g for g in gates["unmet_gates"] if g != "implementation_verification"]

    # finding аудита (P0.5): ready_for_pr ТРЕБУЕТ реального коммита (committed_sha),
    # evidence на точном SHA и чистого дерева до/после проверок. dry-run (commit=False) НИКОГДА
    # не бывает ready — нет ревизии, к которой привязать draft PR.
    tree_ok = bool(tree_clean_before_checks) and (tree_clean_after_checks is not False)
    # P0.6 + v2.118/v2.121 (P1.4): окружение квалифицировано, если install прошёл ЛИБО хотя бы одна
    # проверка реально отработала (нет симптомов неподготовленного окружения). Нет проверок вовсе
    # ИЛИ все падения — env-симптомы -> НЕ квалифицировано.
    env_qualified = prepare_ok or _env_proven_ok(coll["checks"])

    # v2.121 (P1.2, п.4): ПОСЛЕ диффа перепроверяем, что человеко-одобрение покрывает РЕАЛЬНО
    # изменённые пути. Preflight проверил наличие одобрения ДО правок; здесь — что scope одобрения
    # накрыл то, что модель реально тронула. scope не покрывает изменения -> одобрено не то -> НЕ ready.
    approval_recheck = {"ok": True, "uncovered": []}
    contour_consistency = None
    if commit and committed_sha:
        try:
            from ai_ops_kit.gates import approvals as _appr
            _changed = _committed_changed_files(work_root, committed_sha)
            # v3.35 Product Operating Model: гейт `contour_consistency` ИСПОЛНЯЕТСЯ здесь — на том
            # же diff коммита, что и recheck одобрений. Advisory: несогласованность даёт warn.
            contour_consistency = contour_consistency_evidence(child_root, wid, _changed)
            gate_ev["contour_consistency"] = {
                "status": contour_consistency["status"],
                "provided": contour_consistency["provided"],
                "evidence": contour_consistency["evidence"]}
            # v3.0-rc2 (P0.5): recheck по ЭФФЕКТИВНЫМ сигналам (намерение + findings-derived), а не только
            # входным — иначе scope одобрения для НАЙДЕННОЙ зависимости/секрета не перепроверяется на дифф.
            approval_recheck = _appr.recheck_after_diff(child_root, wid, _changed, signals=effective_approval_signals)
            # v3.0-rc5 (P1.2): SEMANTIC dependency approval — каждая НОВАЯ зависимость из диффа должна
            # покрываться ApprovalRecord с covers_packages для ИМЕННО этого пакета (не только путём файла).
            _dep_findings = [f for r in ((security_pack_result or {}).get("results") or [])
                             for f in (r.get("findings") or []) if f.get("type") == "new_dependency"]
            if _dep_findings:
                _dep_rc = _appr.recheck_dependencies(child_root, wid, _dep_findings)
                if not _dep_rc.get("ok"):
                    approval_recheck = {"ok": False,
                                        "uncovered": (approval_recheck.get("uncovered") or []) + _dep_rc["uncovered"],
                                        "dependency_uncovered": _dep_rc["uncovered"]}
        except Exception as _e:  # noqa: BLE001 — v2.123 (P0.2b): approval FAIL-CLOSED. Сбой recheck НЕ
            # трактуется как «покрыто»: для одобрения безопаснее заблокировать, чем пропустить непроверенное.
            approval_recheck = {"ok": False, "uncovered": [{"domain": "*", "reason": f"recheck упал: {_e}"}],
                                "error": str(_e)}
    approvals_cover_ok = bool(approval_recheck.get("ok"))

    # v3.8.4 (finding живой квалификации): reevaluate-only — легитимно завершённый прогон (0 шагов,
    # переоценка существующего committed HEAD после человеко-одобрения). Остальные условия base_ok
    # (committed_sha/revision/tree/env/approvals) по-прежнему строги.
    base_ok = (loop["stopped"] in ("done", "reevaluate-only")) and (committed_sha is not None) \
        and revision_matches and tree_ok and env_qualified and approvals_cover_ok
    # ПРИЁМКА КАК УСЛОВИЕ READY: B2-30 (сверка состоялась, критерий не выполнен) И
    # green-means-checked (судья поднят и отработал, но сверка не установлена — 0 reads / рубер-штамп,
    # прежде давало READY_FOR_PR на QUICK). Разбор и граница #176 — в предикате.
    acceptance_block, acceptance_block_reason = acceptance_blocks_ready(acceptance_criteria)
    if baseline_diff:
        # критерий «no-regressions»: implementation_verification baseline-осведомлён (красная база
        # не блокирует), НО все ОСТАЛЬНЫЕ блокирующие гейты обязательны (P0.1). require_fix (для
        # fix-задач): дополнительно требуем, чтобы правка РЕАЛЬНО починила падавшую проверку.
        ready = base_ok and not acceptance_block and no_regressions and (not other_blocking_unmet) \
            and (not require_fix or len(fixed) > 0) and spec_depth_ok and (not context_overflow) \
            and spec_complete_ok
        ready_criterion = "no-regressions+require-fix" if require_fix else "no-regressions"
    else:
        ready = base_ok and not acceptance_block and (not gates["blocked"]) and spec_depth_ok \
            and (not context_overflow) and spec_complete_ok
        ready_criterion = "all-green"
    return {
        "evidence_revision": evidence_revision, "revision_matches": revision_matches,
        "spec_depth_missing": spec_depth_missing, "spec_depth_ok": spec_depth_ok,
        "spec_incomplete": spec_incomplete, "spec_bad_status": spec_bad_status,
        "spec_complete_ok": spec_complete_ok, "level": _level,
        "acceptance_criteria": acceptance_criteria, "context_overflow": context_overflow,
        "regressions": regressions, "fixed": fixed, "no_regressions": no_regressions,
        "other_blocking_unmet": other_blocking_unmet, "env_qualified": env_qualified,
        "approval_recheck": approval_recheck, "approvals_cover_ok": approvals_cover_ok,
        "contour_consistency": contour_consistency, "ready": ready, "ready_criterion": ready_criterion,
        "acceptance_block": acceptance_block, "acceptance_block_reason": acceptance_block_reason,
        "baseline_diff": baseline_diff, "baseline_checks": baseline_checks,
        "unstable_checks": _rd.get("unstable_checks") or []}  # #405: pass<->fail флип, шум ВХОДА


def _pipeline_check_invariants(report):
    """v3.38 (K7): инварианты pipeline — fail-closed, нарушение записывается в отчёт."""
    from ai_ops_kit.gates.invariants import check_invariant as _ci
    _pipe_breaches = []
    for _inv_id, _kw in [
        ("INV-PIPELINE-001", {"result": report}),
        ("INV-PIPELINE-002", {"ready_for_pr": report.get("ready_for_pr"),
                               "overall_status": report.get("overall_status")}),
        ("INV-PIPELINE-004", {"changed_files": report.get("commit", {}).get("changed_files", [])}),
    ]:
        try:
            if not _ci(_inv_id, **_kw):
                _pipe_breaches.append(_inv_id)
        except (KeyError, TypeError):
            pass
    if _pipe_breaches:
        report["invariant_breaches"] = _pipe_breaches
