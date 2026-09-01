#!/usr/bin/env python3
"""Единый execution-pipeline (v2.58, P0-эпик) — СБОРКА исполнения в один движок.

Аудит: компоненты есть, но не собраны; generic-путь гонял doc-оркестратор, а не tool-loop.
Этот модуль соединяет уже построенные части в ОДНУ цепочку:

  detect (RepositoryProfile) -> tool-loop (модель предлагает, Policy решает, Broker исполняет,
  результат в контекст) -> evidence collector (реальный прогон build/lint/typecheck/test через
  Broker) -> RunPlan-гейты (base_workflow + треки) -> единый отчёт.

Честная граница (НЕ имитируется): commit + reverify на точном SHA и открытие draft PR — ещё НЕ
здесь (нужен git-commit шаг и живой прогон); pipeline доводит до «изменения применены + evidence
собран + гейты оценены». Механика детерминирована и тестируется offline mock-предложителем;
живой предложитель — swap провайдера (как tool_loop.make_model_proposer).

Использование (программно):
  run_pipeline(task, signals, child_root, proposer, policy, budget, max_steps) -> отчёт.
  execution_pipeline.py --selftest
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402
from ai_ops_kit.shared import project_detector      # noqa: E402
from ai_ops_kit.engine import tool_loop             # noqa: E402
from ai_ops_kit.engine import tool_broker           # noqa: E402
from ai_ops_kit.gates import evidence_collector    # noqa: E402
from ai_ops_kit.engine import run_plan              # noqa: E402
from ai_ops_kit.gates import gate_executor         # noqa: E402
from ai_ops_kit.gates import gate_policy           # noqa: E402  (v3.1.8 калиброванное UI-enforcement)
from ai_ops_kit.ui import storybook_adapter     # noqa: E402  (v3.1.9 exact-SHA UI evidence)


# ---------------------------------------------------------------------------
# Submodule imports — functions extracted into focused modules for maintainability.
# All names are re-exported so that `execution_pipeline.XXX` continues to work.
# ---------------------------------------------------------------------------
from ai_ops_kit.engine.pipeline_helpers import (  # noqa: E402,F401
    _profile_summary, _intake_evidence, NO_SELF_REVIEW, _reviewable_gates,
    _gate_checklist, _parse_yaml_block, _openspec_validate, _authoring_specs,
    acceptance_blocks_ready,
)
from ai_ops_kit.engine.pipeline_git import (  # noqa: E402,F401
    _git, _has_changes, _head_advanced, _tree_clean, _TOOL_CACHE_RE, _tree_clean_after_checks,
    _untracked, _committed_changed_files, _commit_on_branch, _resolve_base,
    _verify_remote_base, _change_context, _change_context_range,
    delivery_preflight as _delivery_preflight,
    managed_drift_preflight as _managed_drift_preflight,
)
from ai_ops_kit.engine.pipeline_failure import (  # noqa: E402,F401
    _ENV_SYMPTOMS, _check_has_env_symptom, _env_proven_ok, _env_unqualified,
    _baseline_failure_summary, _failure_signal, _FAILURE_ID_PATTERNS,
    _VOLATILE_RE, _normalize_failure_id, _failure_ids, _diff_checks,
    _evidence_ref_errors, _security_verdict_errors,
)
from ai_ops_kit.engine.pipeline_evidence import (  # noqa: E402,F401
    _install_dependencies, _author_with_retry, _run_spec_authoring,
    _run_authoring, _authored_context, _reevaluate_artifact_evidence,
    _run_reviews, _review_security, _human_approval_domains_uncovered,
    contour_consistency_evidence,          # v3.35: исполнение гейта connectivity контуров
)


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
            "note": "enforceable-подмножество на уровне брокера: пути закрыты на обоих "
                    "каналах (write — до, shell — пост-фактум с откатом); запись вне "
                    "корня репозитория, сеть и не-git деревья — по-прежнему нет; полная "
                    "FS/сеть/ресурс-изоляция — контейнерный runtime"}


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


def _build_not_yet_list(commit, env_qualified, open_pr, spec_prestage_bad, spec_depth_missing,
                        spec_incomplete, spec_bad_status, context_overflow, approvals_cover_ok,
                        approval_recheck, acceptance_block_reason=None):
    """Список «что ещё не сделано» — информирование вызывающего. v3.38 (K6): вынесено из run_pipeline."""
    # Импорт локальный: при выносе из run_pipeline (K6) ссылка _sl уехала от своего импорта —
    # NameError всплывал на живом пути spec-first (CI lint, F821), а не при импорте модуля.
    from ai_ops_kit.gates import spec_levels as _sl
    not_yet = ["живой предложитель (swap провайдера)"]
    if acceptance_block_reason:
        # Причина и способ закрыть — первыми, а не только внутри блока acceptance_criteria.
        not_yet.insert(0, f"приёмка: {acceptance_block_reason}")
    if spec_prestage_bad:
        not_yet.insert(0, "spec-first (P0.1): author вернул невалидную спецификацию ["
                       + ", ".join(str(e.get("gate")) for e in spec_prestage_bad)
                       + "] — реализация НЕ запускалась (0 tool-loop вызовов); почини author/спеку")
    if not commit:
        not_yet.insert(0, "commit+reverify (запусти с commit=True) — без коммита ready_for_pr всегда False")
    if not env_qualified:
        not_yet.insert(0, "окружение не квалифицировано: install упал И проверки не смогли отработать "
                          "(нет тулчейна/зависимостей) — почини установку стека")
    if not open_pr:
        not_yet.append("draft PR (запусти с open_pr=True + GITHUB_TOKEN)")
    if spec_depth_missing:
        not_yet.append("spec-depth: не закрыты разделы уровня " + ", ".join(spec_depth_missing))
    if spec_incomplete:
        _bad = {b["id"]: b for b in spec_bad_status}
        _empty = [s for s in spec_incomplete if s not in _bad]
        if _empty:
            not_yet.append("spec-first: features/<wid>/spec.yaml неполон — заполни разделы: "
                           + ", ".join(_empty))
        for sid, b in _bad.items():
            not_yet.append(
                f"spec-first: раздел {sid} {'заполнен, но' if b.get('has_content') else 'имеет'} "
                f"нераспознанный статус '{b.get('given')}' — допустимо: "
                + "/".join(sorted(_sl.SECTION_STATUSES - {"missing"})))
    if context_overflow:
        not_yet.append("context budget превышен — задачу нужно декомпозировать (см. work_package)")
    if not approvals_cover_ok:
        not_yet.insert(0, "human-approval: scope одобрения не покрывает изменённые пути ("
                       + ", ".join(u["domain"] for u in approval_recheck.get("uncovered") or [])
                       + ") — переодобри под фактический дифф")
    return not_yet


def _deliver_pr(work_root, work_branch, base_ref, base_sha, base_binding, committed_sha, wid, task,
                delivery_id=None):
    """v3.0.15/v3.0.16 (finding аудита P0/#1): доверенная доставка draft PR — единственная точка открытия
    PR. Fail-closed по remote base (verified-equal -> PR; unverifiable/moved -> НЕ открываем). Вызывается
    ИСКЛЮЧИТЕЛЬНО транзакционным контроллером (ai_ops_run) ПОСЛЕ durable-фиксации RunHandoff+final report+
    journal+DeliveryIntent. run_pipeline НИКОГДА не вызывает эту функцию (только возвращает DeliveryPlan) —
    так прямой вызов pipeline не может обойти lifecycle-барьер. Идемпотентно (pr_open находит существующий
    PR ветки и возвращает 'updated', не создавая дубль). -> delivery dict."""
    delivery = {"requested": True, "base_binding": base_binding}
    if not base_binding.get("resolved") or not base_sha:
        delivery.update(status="unavailable",
                        reason=f"base '{base_ref}' не разрешилась в ветку: {base_binding.get('reason')} "
                               "— PR к произвольному HEAD не открываем")
        return delivery
    _rv = _verify_remote_base(work_root, base_ref, base_sha)
    if _rv.get("verdict") == "unverifiable":
        delivery.update(status="unavailable",
                        reason=f"remote-base-unverified: {_rv.get('reason')} — доставка невозможна fail-closed")
        return delivery
    if _rv.get("verdict") == "verified-moved":
        delivery.update(status="not-attempted",
                        reason=f"remote base сдвинулась (validated {base_sha[:12]} != remote "
                               f"{(_rv.get('remote_sha') or '?')[:12]}) — нужна ревалидация; PR не открыт")
        return delivery
    from ai_ops_kit.delivery import pr_open
    pr = pr_open.open_draft_pr(work_root, work_branch, title=f"ai-ops: {task[:60]}", base=base_ref,
                               body=f"Автопрогон AI Ops. WorkItem: {wid}. База {base_ref} "
                                    f"({base_sha[:12]}) → evidence на {committed_sha}.",
                               delivery_id=delivery_id)
    delivery.update(status=(pr or {}).get("status"), pr=pr)
    return delivery


def _security_pack_for_report(security_pack_result):
    """Вердикт security-пака -> в отчёт через ПРОЕКЦИЮ пака (белый список полей), а не срезом на месте.
    Срез на месте и был дефектом: четыре поля выбирались здесь, и находки терялись по дороге."""
    from ai_ops_kit.security import security_pack as _sp_report
    return _sp_report.for_report(security_pack_result)


def _evaluate_security(work_root, child_root, wid, committed_sha, is_git, gate_ev, signals,
                       *, review, strict_judge_qualified, security_reviewer_proposer,
                       reviewer_proposer, budget):
    """Доменный security-вердикт (security/security-domains.yaml) -> gate_ev['security'].
    v3.38 (K6-глубина): вынесено из run_pipeline без изменения поведения.

    Проверяются только ПРИМЕНИМЫЕ к изменению домены; детерминированные (secrets/deps/
    injection) блокируют по severity; домены с security_reviewer/human -> needs_review
    (судья/человек). security проходит ТОЛЬКО если pack 'clear'. Возвращает обновлённый
    gate_ev, результат пака и effective_approval_signals (намерение + findings-derived).
    -> (gate_ev, security_pack_result, effective_approval_signals)."""
    security_pack_result = None
    _security_scan_error = None
    # v2.125 (finding живого прогона): security pack запускается на ЛЮБОМ коммите (не только когда
    # "security" в плане workflow). Security-релевантная находка в диффе (новая зависимость/секрет)
    # обязана быть замечена и в QUICK — иначе новая зависимость в QUICK-задаче проскакивала без
    # ApprovalRecord. Если находка -> gate_ev.security=fail -> ниже security форсируется в оценку гейтов.
    if committed_sha and is_git and "security" not in gate_ev:
        from ai_ops_kit.security import security_pack
        try:
            security_pack_result = security_pack.run_pack(work_root, base=f"{committed_sha}~1", signals=signals)
        except Exception as _e:  # noqa: BLE001
            _security_scan_error = str(_e)
            security_pack_result = None
    # v3.0-rc2 (P0.6): universal security scan — техническая ОШИБКА скана = FAIL-CLOSED, а не тихий обход.
    # Раньше exception -> result=None -> security-гейт не добавлялся -> QUICK оставался зелёным.
    effective_approval_signals = dict(signals)   # v3.0-rc2 (P0.5): signals намерения + findings-derived
    if _security_scan_error:
        gate_ev = dict(gate_ev)
        gate_ev["security"] = {"status": "fail",
                               "blockers": [f"security scan упал (fail-closed): {_security_scan_error}"]}
    elif security_pack_result:
        overall = security_pack_result["overall"]
        gate_ev = dict(gate_ev)
        # v2.123 (P0.2): ЕДИНЫЙ ApprovalDecision. Требования человеко-одобрения выводим из ВХОДНЫХ signals
        # И из РЕАЛЬНЫХ находок security pack (новая зависимость/секрет, внесённые самой правкой), даже
        # если сигнала заранее не было. boolean signals.human_approved БОЛЬШЕ НЕ используется — засчитывается
        # ТОЛЬКО валидный ApprovalRecord (человек). Reviewer (writer≠judge) НЕ заменяет человеко-одобрение.
        from ai_ops_kit.gates import approvals as _appr
        _merged_sig = {**signals, **_appr.signals_from_findings(security_pack_result)}
        effective_approval_signals = _merged_sig   # v3.0-rc2 (P0.5): используется и в recheck ниже
        _appr_missing = list(_appr.check(_merged_sig, child_root, wid).get("missing") or [])
        if _merged_sig.get("destructive"):
            _recs = _appr.load_approvals(child_root, wid)
            # v3.0.11 (finding аудита P1): destructive — high-risk, поэтому STRICT-валидация (expiry +
            # plan-binding + trusted source), как для остальных high-risk доменов. Прежде вызывался
            # _record_valid(r) с дефолтами -> просроченное/привязанное к другому плану/недоверенное
            # одобрение проходило (слабее, чем approvals.check() для high-risk).
            _dnow = _appr._now_iso()
            _dph = _appr.plan_binding_hash(child_root, wid)
            if not any(r.get("approval") == "destructive"
                       and _appr._record_valid(r, now=_dnow, plan_hash=_dph, strict=True) for r in _recs):
                _appr_missing.append({"domain": "destructive",
                                      "reason": "нет строго-валидного ApprovalRecord для деструктивного "
                                                "действия (expiry/plan-binding/trusted source)"})
        human_ok = not _appr_missing
        if not human_ok:
            # человеко-одобрение требуется (по сигналам ИЛИ по находкам диффа) и его нет -> fail, независимо
            # от чистого scan / pass ревьюера.
            gate_ev["security"] = {"status": "fail",
                                   "blockers": [f"{m['domain']}: {m.get('reason', 'нужно человеко-одобрение (ApprovalRecord)')}"
                                                for m in _appr_missing],
                                   "approvals_missing": _appr_missing,
                                   "pack": {"applicable": security_pack_result["applicable_domains"]}}
        elif overall in ("clear", "advisory"):
            # `advisory` — домены, поднятые ТОЛЬКО совпадением по содержимому и БЕЗ находок
            # (`security_pack._content_only`). Гейт их не держит, но и не молчит: список уезжает в
            # evidence и в run-report, иначе «проверено чисто» и «проверять было нечего» слились бы.
            _adv = security_pack_result.get("advisory") or []
            gate_ev["security"] = {"status": "pass",
                                   "provided": ["no_secrets", "no_injection_surface", "deps_approved"],
                                   "advisory": _adv,
                                   "pack": {"applicable": security_pack_result["applicable_domains"],
                                            "advisory": _adv,
                                            "note": ("все применимые security-домены закрыты детерминированным evidence"
                                                     if not _adv else
                                                     "детерминированные проверки чисты; домены "
                                                     + ", ".join(_adv) + " подняты только совпадением по "
                                                     "содержимому и без находок — предупреждение, не ворота")}}
        elif (overall == "needs_review" and not security_pack_result["blocking"]
              and committed_sha and not (strict_judge_qualified and review)
              and not (signals or {}).get("_sequence_internal")):
            # v3.7.3 (#5) STRICT SECURITY JUDGE: security needs_review закрывает ТОЛЬКО КВАЛИФИЦИРОВАННЫЙ
            # security-судья (strict_judge_qualified) ЛИБО ЧЕЛОВЕК (ApprovalRecord). Общий code reviewer НЕ
            # закрывает security. Нет qualified судьи -> pending_human ДО валидного человеческого одобрения.
            # ПОД-ПАКЕТ executor'а (_sequence_internal) НЕ хардстопим здесь: security судится на АГРЕГАТЕ
            # (integration-SHA, _aggregate_close_security). Enforcement #5 на агрегате executor'а — следующий шаг.
            from ai_ops_kit.gates import approvals as _appr_sec
            _sec_recs = _appr_sec.load_approvals(child_root, wid)
            _sec_now, _sec_ph = _appr_sec._now_iso(), _appr_sec.plan_binding_hash(child_root, wid)
            _sec_domains = {"security", "security_review", *security_pack_result["needs_review"]}
            # v3.8.3: одобрение валидно, если привязано к ревизии плана (_sec_ph) ЛИБО к ТОЧНОМУ committed_sha.
            # SHA-binding стабилен при reevaluate (SHA не меняется, даже если run() перезаписал run-plan.yaml)
            # и семантически сильнее: человек одобряет КОНКРЕТНЫЙ код, а не ревизию плана (как aggregate #4b).
            def _appr_valid_here(r):
                return (_appr_sec._record_valid(r, now=_sec_now, plan_hash=_sec_ph, strict=True)
                        or (committed_sha and _appr_sec._record_valid(r, now=_sec_now, plan_hash=committed_sha, strict=True)))
            _human_closed = any(r.get("approval") in _sec_domains and _appr_valid_here(r) for r in _sec_recs)
            if _human_closed:
                gate_ev["security"] = {"status": "pass",
                                       "provided": ["no_secrets", "no_injection_surface", "deps_approved"],
                                       "human_approved": True,
                                       "pack": {"applicable": security_pack_result["applicable_domains"],
                                                "note": "нет qualified security-судьи -> needs_review закрыт "
                                                        "человеком (валидный ApprovalRecord)"}}
            else:
                _why_no_judge = ("судья на этом уровне задачи выключен автоподбором"
                                 if not review else "нет QUALIFIED security-судьи")
                gate_ev["security"] = {"status": "fail", "human_handoff": True, "pending_human": True,
                                       "blockers": [_why_no_judge + ": needs_review домены "
                                                    "закрывает ТОЛЬКО квалифицированный судья или человек "
                                                    "(валидный ApprovalRecord); общий code reviewer НЕ "
                                                    "закрывает security. Домены: "
                                                    + ", ".join(security_pack_result["needs_review"])
                                                    + ". Человеку закрыть так: python3 "
                                                    ".ai/managed/ai_ops_kit/gates/approvals.py record . "
                                                    + str(wid) + " --approval <домен> --by <кто> "
                                                    "--scope <что> --reason <почему>"],
                                       "pack": {"applicable": security_pack_result["applicable_domains"],
                                                "needs_review": security_pack_result["needs_review"]}}
        elif (overall == "needs_review" and not security_pack_result["blocking"]
              and review and committed_sha and (security_reviewer_proposer or reviewer_proposer) is not None):
            # v2.106/v3.7.3: КВАЛИФИЦИРОВАННЫЙ security-судья (strict_judge_qualified) закрывает needs_review.
            # Судья — ОТДЕЛЬНЫЙ security_reviewer_proposer (не общий code reviewer); fallback только если он
            # не передан (совместимость). Блокирующие детерминированные находки судья НЕ переопределяет.
            sec_status, sec_res = _review_security(security_reviewer_proposer or reviewer_proposer, work_root,
                                                   security_pack_result, committed_sha, budget)
            if sec_status == "pass":
                gate_ev["security"] = {"status": "pass",
                                       "provided": ["no_secrets", "no_injection_surface", "deps_approved"],
                                       "reviewer": {"status": sec_status},
                                       "pack": {"applicable": security_pack_result["applicable_domains"],
                                                "note": "детерминированные домены чисты + независимый "
                                                        "security-reviewer вынес pass по needs_review"}}
            else:
                # v3.6.8 (finding живой квалификации): раньше причина отказа вердикта была НЕМА
                # («не вынес pass»). Теперь фиксируем ТОЧНЫЕ ошибки вердикта (_review_security кладёт их
                # в res["invalid"]) + структурную диагностику — чтобы видеть, промпт/формат это или модель.
                _inv = sec_res.get("invalid") if isinstance(sec_res, dict) else None
                _raw = (sec_res.get("raw") if isinstance(sec_res, dict) and "raw" in sec_res else sec_res)
                _diag = {}
                if isinstance(_raw, dict):
                    _dr = _raw.get("domain_results")
                    _diag = {"raw_keys": sorted(_raw.keys()),
                             "has_domain_results": isinstance(_dr, list) and bool(_dr),
                             "domain_results_count": len(_dr) if isinstance(_dr, list) else 0}
                gate_ev["security"] = {"status": "fail", "blockers": ["security-reviewer не вынес pass"],
                                       "reviewer": {"status": sec_status},
                                       "verdict_errors": _inv, "verdict_diag": _diag,
                                       "pack": {"applicable": security_pack_result["applicable_domains"],
                                                "needs_review": security_pack_result["needs_review"]}}
        else:
            blockers = []
            if security_pack_result["blocking"]:
                blockers.append("блокирующие домены (critical/high находки): " + ", ".join(security_pack_result["blocking"]))
            if security_pack_result["needs_review"]:
                blockers.append("нужен независимый security-reviewer/человек по доменам: "
                                + ", ".join(security_pack_result["needs_review"]))
            gate_ev["security"] = {"status": "fail", "blockers": blockers,
                                   "pack": {"applicable": security_pack_result["applicable_domains"],
                                            "blocking": security_pack_result["blocking"],
                                            "needs_review": security_pack_result["needs_review"]}}

        # v3.0-rc20 (finding аудита P0): БРАНЧ-НЕЗАВИСИМАЯ проверка — high-risk домены, применимые ПО
        # РЕАЛЬНО ИЗМЕНЁННЫМ ПУТЯМ (Dockerfile/CI/auth), требуют человеческого ApprovalRecord, даже если
        # security-reviewer/детерминированные проверки дали pass. Неожиданное изменение прод-конфига без
        # одобрения -> security=fail. Форсируется поверх любой ветки выше.
        # v3.0.2 (finding аудита P1): изменённые файлы берём из EXECUTION-root (worktree), а
        # ApprovalRecord'ы/plan-binding — из LIFECYCLE-root (child_root/features), где их создаёт человек
        # после preflight-блока. Раньше и то и другое читалось из work_root -> человеческое одобрение в
        # lifecycle-каталоге отсутствовало в worktree -> ложный uncovered.
        try:
            from ai_ops_kit.security import security_scan as _ss
            _sec_changed = _ss._git_changed_files(work_root, committed_sha + "^") if committed_sha else []
        except Exception:  # noqa: BLE001
            _sec_changed = []
        _hu = _human_approval_domains_uncovered(child_root, wid, _sec_changed, diff_root=work_root)
        if _hu and (gate_ev.get("security") or {}).get("status") != "fail":
            gate_ev["security"] = {"status": "fail",
                                   "blockers": ["high-risk изменение по путям без человеческого ApprovalRecord "
                                                "(reviewer не закрывает): " + ", ".join(_hu)],
                                   "human_approval_uncovered": _hu,
                                   "pack": {"applicable": security_pack_result["applicable_domains"]}}
    return gate_ev, security_pack_result, effective_approval_signals


def _setup_isolation(child_root, wid, base, *, isolate, resume, reevaluate_only,
                     discard_previous, open_pr):
    """BASE BINDING + worktree-изоляция/resume: рабочая ветка ai-ops/<wid> форкается от РАЗРЕШЁННОГО
    base (не от HEAD), весь прогон идёт в отдельном worktree, основное дерево не трогается.

    K6: вынесено из run_pipeline без изменения поведения. -> dict: при отказе {"error": <report>}
    (ранний честный выход ДО модели/worktree), иначе {"work_root", "worktree_rel", "resume_info",
    "base_binding", "base_ref", "base_sha"}.
    """
    work_root, worktree_rel = child_root, None
    resume_info = ({"requested": bool(resume), "resumed": False,
                    "reused_worktree": False, "reused_branch": False}
                   if (resume or reevaluate_only) else None)
    # v3.0.1/v3.0.7 (P0): рабочая ветка форкается от РАЗРЕШЁННОГО base (--base), а НЕ от HEAD.
    # base=None -> AUTO-резолв (upstream/remote-default/текущая ветка), не хардкод 'main'.
    _br = _resolve_base(child_root, base)   # base может быть None (auto) или явной веткой
    base_sha = _br.get("base_sha")
    base_ref = _br.get("base_ref") or base or "HEAD"
    base_binding = {"base_ref": base_ref, "base_sha": base_sha, "mode": _br.get("mode"),
                    "resolved": bool(_br.get("resolved")), "source": _br.get("source"),
                    "reason": _br.get("reason")}
    # B2-23: доставка проверяла remote-базу ПОСЛЕ работы (13.5 мин живой модели, только потом «база
    # сдвинулась»). База известна ЗДЕСЬ, до первого вызова модели — предупреждаем заранее (бесплатно),
    # прогон не останавливаем, но в тех же словах, что скажет доставка.
    delivery_pf = _delivery_preflight(child_root, base_ref, base_sha, open_pr)
    if delivery_pf:
        print(f"  ⚠ {delivery_pf['warning']}")
    # B2-27: update --in-place оставляет managed-файлы в рабочем дереве, а worktree создаётся от HEAD
    # -> прогон на старом ките. Предупреждаем ДО изоляции.
    managed_pf = _managed_drift_preflight(child_root)
    if managed_pf:
        print(f"  ⚠ {managed_pf['warning']}")
    # P0.2: ЯВНО переданная, но неразрешённая base -> preflight-блок ДО модели/worktree (не выполнять
    # от HEAD). auto всегда разрешается, поэтому блокирует только явную несуществующую ветку.
    if isolate and _br.get("mode") == "explicit" and not _br.get("resolved"):
        return {"error": {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": wid,
                "status": "error", "ready_for_pr": False, "base_binding": base_binding,
                "error": (f"base-preflight: явная база '{base}' не разрешается в ветку "
                          f"({_br.get('reason')}) — прогон остановлен ДО вызова модели (не выполняем "
                          f"от произвольного HEAD)"),
                "loop": None, "isolation": {"worktree": None}, "gates": None, "overall_status": "error"}}
    if isolate:
        from ai_ops_kit.engine import worktree as _wt
        branch = f"ai-ops/{wid}"
        wp = child_root / ".ai" / "worktrees" / wid
        branch_exists = _wt._branch_exists(child_root, branch)
        # v2.109 Real Resume: продолжаем ПОВЕРХ подтверждённой работы — ветку/коммиты НЕ удаляем.
        reused = False
        if (resume or reevaluate_only) and (branch_exists or wp.is_dir()):
            if not wp.is_dir() and branch_exists:
                # worktree утерян, но ветка (коммиты) на месте -> пере-подключаем worktree к ветке
                rc = _wt.add(child_root, wid, branch)
                if rc != 0:
                    return {"error": {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": wid,
                            "status": "error",
                            "error": f"resume: не удалось пере-подключить worktree к ветке {branch} "
                                     f"(занята? не в .gitignore?) — прогон остановлен, работа не тронута",
                            "loop": None, "isolation": {"worktree": None}, "gates": None,
                            "ready_for_pr": False, "resume": {**resume_info, "resumed": False}}}
                resume_info["reused_branch"] = True
            else:
                resume_info["reused_worktree"] = True
                resume_info["reused_branch"] = branch_exists
            work_root = wp
            worktree_rel = wp.relative_to(child_root).as_posix()
            resume_info["resumed"] = True
            reused = True
        if not reused:
            if resume:
                # resume запрошен, но продолжать нечего (ни ветки, ни worktree) — честный свежий старт
                resume_info["reason"] = (f"ни ветки {branch}, ни worktree нет — продолжать нечего; "
                                         f"выполняется свежий старт")
            # finding живого прогона: worktree от ПРЕДЫДУЩЕГО прогона того же wid молча
            # переиспользовался -> прогон шёл поверх грязного состояния (нечистый baseline).
            # P0.3 (аудит v2.79): но слепо удалять прошлую ветку ОПАСНО — там могут быть НЕсохранённые
            # коммиты (PR не открылся и т.п.). Удаляем только если на ветке нет работы ЛИБО явный discard.
            if wp.is_dir() or branch_exists:
                ahead = 0
                if branch_exists:
                    # коммиты на ветке ai-ops/<wid>, которых нет в текущем HEAD -> несохранённая работа
                    rc_a, out_a, _ = _git(child_root, "rev-list", "--count", branch, "^HEAD")
                    ahead = int(out_a) if rc_a == 0 and out_a.isdigit() else 0
                if ahead > 0 and not discard_previous:
                    return {"error": {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": wid,
                            "status": "error",
                            # obs 2dbfc337 (поле 20.08.2026) + B2-10: здесь назывались ВНУТРЕННИЕ
                            # параметры движка — `resume=True (--resume)` и `discard_previous=True
                            # (--discard)`. Человек читает это через `ai-ops`, где `--resume` нет
                            # вовсе (argparse принимает его за сокращение `--resume-from` и падает),
                            # а продолжение — это ИНТЕНТ `resume`. Печатаем РЕАЛЬНЫЕ команды.
                            "error": f"предыдущий прогон feature='{wid}' имеет {ahead} несохранённых "
                                     f"коммит(ов) на ветке {branch}. Чтобы не потерять работу, прогон "
                                     f"остановлен. Продолжить поверх них: "
                                     f"`ai-ops resume . --feature {wid} --execute`. Перезаписать: "
                                     f"`git branch -D {branch}` и "
                                     f"`ai-ops run . --feature {wid} --execute`. Или возьми другой "
                                     f"--feature.",
                            "loop": None, "isolation": {"worktree": None}, "gates": None,
                            "ready_for_pr": False, "overall_status": "error"}}
                _wt.remove(child_root, wid, force=True)
                _git(child_root, "worktree", "prune")
                _git(child_root, "branch", "-D", branch)
            rc = _wt.add(child_root, wid, branch, base=(base_sha or "HEAD"))   # v3.0.1: форк от base_sha
            if rc == 0:
                work_root = wp
                worktree_rel = wp.relative_to(child_root).as_posix()
                # v3.0.1 (P0): свежая ветка обязана форкнуться РОВНО от base_sha (иначе `--base` — фикция)
                if base_sha:
                    _rc_h, _wh, _ = _git(wp, "rev-parse", "HEAD")
                    if _rc_h != 0 or (_wh or "").strip() != base_sha:
                        return {"error": {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": wid,
                                "status": "error", "base_binding": base_binding,
                                "error": (f"base binding нарушен: ветка {branch} форкнулась от "
                                          f"{(_wh or '?').strip()[:12]}, а заявлен base={base_ref}"
                                          f" ({base_sha[:12]}) — прогон остановлен"),
                                "loop": None, "isolation": {"worktree": None}, "gates": None,
                                "ready_for_pr": False, "overall_status": "error"}}
        if work_root is child_root:
            # finding adversarial-review: НЕ деградируем молча в основное дерево — это исполнило бы
            # правки и коммит в main вопреки isolate=True. Останавливаемся честной ошибкой.
            return {"error": {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": wid,
                    "status": "error",
                    "error": f"isolate=True, но worktree .ai/worktrees/{wid} не создан "
                             f"(ветка занята? не в .gitignore?) — прогон остановлен, основное дерево не тронуто",
                    "loop": None, "isolation": {"worktree": None}, "gates": None,
                    "ready_for_pr": False}}
    return {"work_root": work_root, "worktree_rel": worktree_rel, "resume_info": resume_info,
            "base_binding": base_binding, "base_ref": base_ref, "base_sha": base_sha,
            "delivery_pf": delivery_pf}


def _assemble_evidence(profile, work_root, pol, child_root, wid, plan, signals, loop, *,
                       commit, is_git, committed_sha, base_sha, authored_ev, allow_missing_tests,
                       calibrated_enforcement, ui_evidence, review, reviewer_proposer, budget,
                       strict_judge_qualified, security_reviewer_proposer, reevaluate_only):
    """Сбор evidence на зафиксированном SHA и наполнение gate_ev: реальный прогон проверок профиля
    через Broker, intake/regression/authored/reevaluate-seed, освобождения по неприменимым проверкам,
    UI-evidence на точном SHA, seam-scan advisory, независимые ревью и доменный security-вердикт.

    K6: вынесено из run_pipeline без изменения поведения. -> dict со всем, что нужно дальше для гейтов
    и отчёта (changed_for_verification/coll/gate_ev/tree_clean_after_checks/regression_proof/exempt/
    not_applicable/exempt_reason/tests_warn/ui_evidence_bundle/seam_advisory/reviews/security_pack_result/
    effective_approval_signals).
    """
    # v3.26.1 Progressive Verification: передаём changed_files для targeted test execution
    _changed_for_verification = _committed_changed_files(work_root, committed_sha) if (commit and is_git and committed_sha) else None
    coll = evidence_collector.collect(profile, work_root, pol, changed_files=_changed_for_verification, broker=tool_broker)

    # 6a. finding аудита (P0.5): проверки могли намутить дерево (build-артефакты, lock-файлы) —
    #     тогда собранный evidence уже не отражает закоммиченный SHA. Фиксируем факт, не скрываем.
    # v2.119: чистота ПОСЛЕ проверок терпима к тул-кэшам (pytest/npm/... создают их рутинно);
    # tracked-правки от проверок по-прежнему делают дерево грязным (evidence-целостность сохранена).
    tree_clean_after_checks = _tree_clean_after_checks(work_root) if (commit and is_git) else None

    # 6b. intake-evidence из сигналов: классификация УЖЕ произошла (task_type/size/risk в signals) —
    #     это реальный evidence для intake_completeness, а не фабрикация (finding живого прогона).
    gate_ev = dict(coll["gate_evidence"])
    intake = _intake_evidence(signals)
    if intake:
        gate_ev.setdefault("intake_completeness", intake)
    # v3.30 (раунд C, T1/T2/T4): доказательство того, что правка ЧИНИТ. Тест из коммита прогоняется
    # на БАЗОВОЙ ревизии и обязан там упасть — иначе он не покрывает исправление. Считаем только
    # когда есть что сравнивать (коммит на git-дереве); сбой самой проверки не роняет прогон, а
    # честно уходит в unverifiable.
    regression_proof = None
    if commit and is_git and committed_sha:
        try:
            from ai_ops_kit.gates import regression_evidence
            regression_proof = regression_evidence.prove(
                work_root, base_sha, committed_sha, profile,
                changed_files=_changed_for_verification)
            gate_ev.setdefault("regression_test_evidence", regression_evidence.gate_evidence(
                regression_proof, behavior_unchanged=(loop or {}).get("behavior_unchanged")))
        except Exception as _e:  # noqa: BLE001 — доказательство не должно ронять уже сделанную работу
            regression_proof = {"kind": "RegressionEvidence", "status": "unverifiable",
                                "reason": f"проверка не отработала: {type(_e).__name__}: {_e}"[:200]}
    # v2.86: evidence артефакт-гейтов (requirements/plan_readiness) из author-стадии — форма
    # подтверждена детерминированно; НЕ перетираем уже имеющееся evidence (setdefault).
    for _gid, _ev in (authored_ev or {}).items():
        gate_ev.setdefault(_gid, _ev)

    # v3.8.3 reevaluate-only: SHA НЕ менялся -> артефакт-гейты (requirements/specification/plan_readiness)
    # пере-выводим ДЕТЕРМИНИРОВАННО из существующих на диске артефактов (без модели, без чтения
    # клоббер-подверженного run-report). code_review переподтверждается ревью на том же SHA (--review).
    # security НЕ сеем — переоценим ниже с человеко-approval. setdefault -> не перетираем свежий
    # impl_verification из evidence_collector.
    if reevaluate_only:
        # (1) primary: персистированное build-evidence по committed_sha (включая model-вердикт code_review) —
        # НЕ ре-ревьюим (недетерминизм) и не зависим от клоббер-подверженного run-report;
        # (2) fallback: детерминированный re-derive артефакт-гейтов из существующих на диске артефактов.
        try:
            import json as _json
            _rep = Path(child_root) / ".ai" / f"reevaluate-evidence-{wid}.json"
            if _rep.is_file():
                _rj = _json.loads(_rep.read_text(encoding="utf-8"))
                if _rj.get("sha") == committed_sha:
                    for _gid, _ev in (_rj.get("gate_ev") or {}).items():
                        if _gid != "security":
                            gate_ev.setdefault(_gid, _ev)
        # Решение о подавлении ЗАПИСАНО (ревизия 2026-08-11): это ЧТЕНИЕ кеша переоценки, чистая
        # оптимизация. Его утрата безвредна по построению — гейты просто пересчитаются заново, и
        # ни одно утверждение о них не станет менее доказанным. Поэтому здесь `pass` уместен, в
        # отличие от учёта usage и lifecycle-журнала, где терялась АУДИТ-запись.
        except Exception:  # noqa: BLE001,S110 — потеря кеша не меняет вердикт, пересчитаем
            pass
        for _gid, _ev in _reevaluate_artifact_evidence(work_root, wid, plan["gates"]).items():
            gate_ev.setdefault(_gid, _ev)

    # 6c. «умное ослабление» (v2.61): инструмента нет в подтверждённом стеке -> флаг освобождается
    #     (build/lint/typecheck). tests — особый случай: по умолчанию тоже освобождаем + громкий
    #     warn; policy allow_missing_tests=False эскалирует до блока (untested -> not ready).
    exempt = set(coll.get("not_applicable") or [])
    tests_warn = None
    if coll.get("tests_absent"):
        if allow_missing_tests:
            exempt.add("tests_passed")
            tests_warn = "нет тестов в стеке — implementation_verification освобождён по tests (allow_missing_tests=True); это осознанное послабление"
        else:
            exempt.discard("tests_passed")   # тесты обязательны -> гейт заблокирует
            tests_warn = "нет тестов, а require_tests -> implementation_verification блокирует"
    not_applicable = {"implementation_verification": exempt}
    # Причина освобождения едет ВМЕСТЕ с ним (B2-08): без неё отчёт называет «нет инструмента в
    # стеке» даже там, где инструмент есть и просто не нужен — изменение только документации.
    exempt_reason = {"implementation_verification": coll.get("not_applicable_reason")}

    # 6d. v2.83 Full RunPlan: постадийный НЕЗАВИСИМЫЙ ревью для ai-review гейтов плана
    #     (code_review, ux_review, security-non-human, ...). writer ≠ judge: ревьюер — отдельный
    #     вызов под READ-ONLY политикой (писать/шеллить не может), выносит СТРУКТУРНЫЙ вердикт.
    #     Честно: детерминированные артефакт-гейты (requirements/specification/plan_readiness) и
    #     human-approval (security при privileged/destructive) ревьюер НЕ закрывает — остаются
    #     блокирующими. review только на зафиксированной ревизии (иначе судить нечего).
    # v3.1.9 EXACT-SHA UI EVIDENCE (trust-фикс): собираем UI-evidence ПОСЛЕ реализации, из РАБОЧЕГО
    # worktree, на ТОЧНОМ committed_sha, по файлам, изменённым этим коммитом; связываем с committed_sha.
    # Устаревшее/непривязанное/чужое evidence (meta.commit_sha != committed_sha) -> not_run (fail-closed),
    # НЕ освобождает гейт. Инжектированный ui_evidence (bench/синтетика) используется как есть (не строим).
    ui_evidence_bundle = None
    if calibrated_enforcement and ui_evidence is None and committed_sha:
        try:
            _changed = _committed_changed_files(work_root, committed_sha)
            # v3.11.0 UI Evidence Readiness: UI-CI ТОЛЬКО при изменении UI-файлов ИЛИ VISUAL-задаче.
            # Иначе — skip (не применимо; НЕ маскируем — просто не гоняем UI-CI зря на не-UI изменении).
            from ai_ops_kit.ui import ui_readiness as _uir
            _ui_run, _ui_reason = _uir.should_run_ui_evidence(_changed, signals)
            if not _ui_run:
                ui_evidence, ui_evidence_bundle = None, None
            else:
                # v3.7 UI-CI: собрать РЕАЛЬНЫЙ UI-evidence на committed_sha (vitest interaction + axe a11y +
                # storybook visual). Не-UI child / нет артефактов -> build_bundle честно вернёт not_run/absent.
                try:
                    from ai_ops_kit.ui import ui_evidence_collect
                    ui_evidence_collect.collect(work_root, committed_sha)
                # Причина подавления ЗАПИСАНА (срез engine ратчета 2026-08-12): сбор UI-evidence не
                # выдаёт вердикт — вердикт выдаёт `evidence_for_gate` ниже, и он fail-closed:
                # не собрали -> `ui_evidence=None` -> гейт НЕ освобождён. Пропущенный сбор не может
                # превратиться в зелёное, он превращается в незакрытый гейт.
                except Exception:   # noqa: BLE001,S110 — не собрали -> гейт не освобождён (fail-closed ниже)
                    pass
                ui_evidence_bundle = storybook_adapter.build_bundle(work_root, changed_files=_changed)
                ui_evidence = storybook_adapter.evidence_for_gate(ui_evidence_bundle,
                                                                  expected_sha=committed_sha)
        except Exception:   # noqa: BLE001 — сбой сбора evidence не освобождает гейт (ui_evidence=None)
            ui_evidence, ui_evidence_bundle = None, None

    # v3.7.4 SEAM-SCAN (ADVISORY, non-blocking до обкатки): детектор «дефекта шва» по дифу base..committed.
    # Surfaces тихие швы (запись без round-trip / catch без happy-path / stub без real-run / optional-поле
    # в контракте / смена предусловия без аудита вызывающих). НЕ блокирует (advisory); станет gate после
    # обкатки на child. Экономия/скорость НЕ ослабляют проверку (ADR-004).
    seam_advisory = _seam_scan_advisory(work_root, base_sha, committed_sha)

    reviews = None
    if review and reviewer_proposer is not None and committed_sha:
        gate_ev, reviews = _run_reviews(reviewer_proposer, work_root, plan["gates"], gate_ev,
                                        signals, committed_sha, budget,
                                        calibrated_enforcement=calibrated_enforcement,
                                        ui_evidence=ui_evidence)

    # 6e. v2.95 -> v2.101 Security Pack: доменный security-вердикт -> gate_ev['security'].
    #     v3.38 (K6): тело вынесено в _evaluate_security (см. функцию выше).
    gate_ev, security_pack_result, effective_approval_signals = _evaluate_security(
        work_root, child_root, wid, committed_sha, is_git, gate_ev, signals,
        review=review, strict_judge_qualified=strict_judge_qualified,
        security_reviewer_proposer=security_reviewer_proposer,
        reviewer_proposer=reviewer_proposer, budget=budget)
    return {"changed_for_verification": _changed_for_verification, "coll": coll, "gate_ev": gate_ev,
            "tree_clean_after_checks": tree_clean_after_checks, "regression_proof": regression_proof,
            "exempt": exempt, "not_applicable": not_applicable, "exempt_reason": exempt_reason,
            "tests_warn": tests_warn, "ui_evidence_bundle": ui_evidence_bundle,
            "seam_advisory": seam_advisory, "reviews": reviews,
            "security_pack_result": security_pack_result,
            "effective_approval_signals": effective_approval_signals}


def _assess_readiness(gates, coll, signals, plan, child_root, wid, work_root, *,
                      baseline_diff, baseline_checks, committed_sha, base_sha,
                      reviewer_proposer, budget):
    """Ready-критерии уровня спеки: spec-depth enforcement (незакрытые разделы уровня, мапящиеся на
    unmet-гейты), Real Spec-First (реальный spec.yaml неполон -> блок), сверка критериев приёмки
    независимым судьёй (B2-14, не блокирует, но unmet при verified блокирует у вызывающего) и
    context-budget overflow.

    K6: вынесено из run_pipeline без изменения поведения. -> dict (spec_depth_missing/spec_depth_ok/
    spec_incomplete/spec_bad_status/spec_complete_ok/level/acceptance_criteria/context_overflow).
    """
    # v2.106 #2 Spec-depth enforcement: разделы спецификации уровня задачи, ЗАКРЫВАЕМЫЕ evidence
    # гейтов, но незакрытые -> блокируют ready. Маппим только доказуемые разделы (недоказуемые не
    # над-блокируем). Это подмножество unmet-гейтов -> не блокирует сверх гейтов, но делает
    # spec-depth явным ready-критерием ("реализация не начинается без блокирующих разделов").
    from ai_ops_kit.gates import spec_levels as _sl
    _SECTION_GATE = {
        "goal": "intake_completeness", "scope": "intake_completeness",
        "acceptance_criteria": "intake_completeness",
        "requirements": "requirements", "acceptance_scenarios": "requirements",
        "implementation_plan": "plan_readiness", "verification_strategy": "implementation_verification",
        "problem": "discovery_completeness", "users_jtbd": "discovery_completeness",
        "value": "discovery_completeness", "success_metrics": "analytics_readiness",
    }
    _unmet = set(gates["unmet_gates"])
    # v3.8.4 (finding живой full-stack квалификации): spec_depth ДОЛЖЕН быть baseline-осведомлён.
    # verification_strategy маппится на implementation_verification; в baseline-diff режиме этот гейт
    # baseline-освобождён (красная база не блокирует — см. other_blocking_unmet ниже). Раньше spec_depth
    # брал СЫРОЙ _unmet -> предсуществующий провал базы (напр. flaky date-тест) блокировал ready через
    # verification_strategy, ОБХОДЯ baseline-diff. Теперь: если правка НЕ внесла новых регрессий, гейт
    # implementation_verification не считается незакрытым и для spec_depth (реальная регрессия ПРАВКИ —
    # по-прежнему блокирует, т.к. тогда _diff_checks вернёт непустые regressions).
    _iv_baseline_exempt = bool(baseline_diff) and not _diff_checks(baseline_checks, coll["checks"])[0]
    _unmet_for_spec = (_unmet - {"implementation_verification"}) if _iv_baseline_exempt else _unmet
    _level = _sl.classify(signals)["level"]
    _req_sections = set(_sl.required_sections(_level))
    spec_depth_missing = sorted({s for s, g in _SECTION_GATE.items()
                                 if s in _req_sections and g in plan["gates"] and g in _unmet_for_spec})
    spec_depth_ok = not spec_depth_missing

    # v2.110 Real Spec-First enforcement: если для этого WorkItem СУЩЕСТВУЕТ явный spec-артефакт
    # (features/<wid>/spec.yaml), но он НЕ полон (есть blocking_missing) -> «неполная спека не
    # пускает в implementation» (аудит). Спеки нет -> поведение прежнее (spec-first опционален для
    # мелких задач, spec_depth через гейты). Читаем реальный артефакт, а не сигналы.
    spec_incomplete, spec_bad_status = [], []
    try:
        _cov = _sl.assess_from_artifacts(signals, child_root, wid, work_root=work_root)
        if _cov.get("spec_artifact") and _cov.get("blocking_missing"):
            spec_incomplete = list(_cov["blocking_missing"])
        # F-013: разделы с содержимым, но нераспознанным статусом — это НЕ «не заполнено».
        # Прежний вывод отправлял заполнять уже заполненное; настоящая правка — одно слово.
        spec_bad_status = list(_cov.get("invalid_status") or [])
    except Exception as _e:  # noqa: BLE001 — v3.0.11 (finding аудита P2): FAIL-CLOSED. Прежде исключение
        # -> spec_incomplete=[] -> spec_complete_ok=True: реальный, но неоцениваемый spec.yaml проходил в
        # реализацию. Теперь ошибка оценки спеки = блокирующий незакрытый пункт (не тихий пропуск).
        spec_incomplete = [f"<spec-assess-failed: {type(_e).__name__}>"]
    spec_complete_ok = not spec_incomplete

    # КРИТЕРИИ ПРИЁМКИ НЕ СВЕРЯЮТСЯ С РЕЗУЛЬТАТОМ (B2-14, живой прогон 14.08.2026).
    #
    # ЗАМЕР, а не опасение. Прогон на реальном продукте отдал владельцу draft PR со
    # `sha_verified: True` и `overall_status: delivered`, тогда как критерий приёмки требовал
    # дословно «в README нет строк с `public/media`» — а в доставленном тексте эта строка осталась,
    # только описание стало расплывчатым. Ложное утверждение о проекте (каталога не существует) не
    # ушло, а замаскировалось. `spec-coverage` при этом сообщал `acceptance_criteria: complete`.
    #
    # `complete` В SPEC-COVERAGE ОЗНАЧАЕТ «РАЗДЕЛ ЗАПОЛНЕН», А НЕ «КРИТЕРИЙ ВЫПОЛНЕН». Разница в
    # одном слове, а цена — ложный green на последнем шаге: владелец получает работу, помеченную
    # проверенной, и приёмка перекладывается на него без предупреждения.
    #
    # ПЕРВАЯ ПОЛОВИНА (14.08, #111): непроверенное перестало выглядеть проверенным.
    # ВТОРАЯ ПОЛОВИНА (здесь): появилась САМА СВЕРКА. Независимый read-only судья (writer ≠ judge)
    # читает дифф против КАЖДОГО критерия и выносит вердикт с цитатой; цитата проверяется кодом в
    # диффе и в названном файле, иначе вердикт не принимается — `ai_ops_kit/engine/acceptance_verify.py`.
    # СВЕРКА НЕ БЛОКИРУЕТ ready. Порядок из плана обязателен: advisory + полевые доказательства
    # качества вердиктов, и только потом блокировка. Гейт, включённый до замера, останавливал бы все
    # прогоны на непроверенном вердикте — и его научились бы обходить.
    from ai_ops_kit.engine import acceptance_verify as _av
    _ac_text, _ac_items, _ac_problem = _av.criteria_from_spec(child_root, wid)
    # СВЕРКА НЕ ЗАВИСИТ ОТ ФЛАГА `review` (полевой замер 14.08.2026, пере-прогон BNBM). Судья
    # включается автоподбором по классу задачи: для QUICK `review=False`. Правка документа — это
    # QUICK, и именно на правке документа родился B2-14. То есть механизм против ложного green не
    # работал ровно на том классе, где ложный green и случился: за весь живой прогон сверка не
    # запустилась НИ РАЗУ. Критерии, если они объявлены, сверяются всегда, когда есть кому судить.
    if _ac_items and reviewer_proposer is not None and committed_sha:
        try:
            # Контекст судьи — ВЕСЬ диапазон base..head, а не последний коммит (ревью PR #118).
            # Критерии приёмки описывают изменение целиком; на resume и reevaluate_only ветка
            # несёт несколько коммитов, и критерий, выполненный в предыдущем, не попал бы в дифф —
            # судья честно ответил бы `unmet`/`undetermined` о работе, которая сделана. Тот же
            # довод, по которому диапазон берёт seam_scan выше.
            acceptance_criteria = _av.verify(
                work_root, _ac_items, reviewer_proposer, revision=committed_sha,
                change_context=_change_context_range(work_root, base_sha, committed_sha),
                budget=budget)
        except Exception as _e:  # noqa: BLE001 — FAIL-CLOSED: сбой сверки = «не сверено» с названной
            # причиной, а не отсутствие блока. attempted=True: сюда попадаем ТОЛЬКО с поднятым судьёй,
            # и крах сверки не должен давать READY_FOR_PR в обход рубер-штамп-блока (green-means-checked).
            acceptance_criteria = _av._unverified(
                _ac_items, f"сверка не выполнена ({type(_e).__name__}: {_e})"[:300], attempted=True)
    elif _ac_problem:
        # Спека ЕСТЬ, но не разобрана: это «не знаю», а не «критериев нет». Молчание здесь было бы
        # тем же ложным green — `spec-coverage` для того же файла говорит `complete`.
        acceptance_criteria = _av._unverified(
            [], f"критерии приёмки НЕ прочитаны: {_ac_problem} — сверка невозможна, проверь вручную",
            declared=True)
    elif _ac_text and not _ac_items:
        # Раздел заполнен, но проверяемых пунктов из него не извлеклось (одни заголовки/разделители).
        acceptance_criteria = _av._unverified(
            [], "раздел критериев заполнен, но ни одного проверяемого пункта в нём не найдено — "
                "сверять нечего по существу (проверь формат: пункты списка или строки)",
            declared=True)
    else:
        acceptance_criteria = _av._unverified(
            _ac_items,
            ("критерии объявлены, но с результатом НЕ сверялись: независимый ревьюер не запускался "
             "(нужны --execute с коммитом и провайдер судьи); `spec-coverage: complete` означает "
             "«раздел заполнен», а не «критерий выполнен»"
             if _ac_items else "критерии приёмки не объявлены — сверять нечего"),
            declared=bool(_ac_items))

    # v2.106 #3 Context-budget enforcement: если контекст задачи превышает бюджет (ContextBundle
    # overflow) -> пакет не атомарен, доставлять как один нельзя -> блок ready (аудит: "при
    # превышении context budget выполнение блокируется или задача дробится"). Мягкие оси
    # (подсистемы/размер) остаются advisory (в report['work_package']), блокирует только жёсткий лимит.
    context_overflow = _context_budget_overflow(signals, work_root, plan)
    return {"spec_depth_missing": spec_depth_missing, "spec_depth_ok": spec_depth_ok,
            "spec_incomplete": spec_incomplete, "spec_bad_status": spec_bad_status,
            "spec_complete_ok": spec_complete_ok, "level": _level,
            "acceptance_criteria": acceptance_criteria, "context_overflow": context_overflow}


def run_pipeline(task, signals, child_root, proposer, policy=None, budget=None,
                 max_steps=40, feature=None, commit=False, allow_missing_tests=True,
                 isolate=False, open_pr=False, install_deps=True, baseline_diff=False,
                 require_fix=False, discard_previous=False, sandbox=False,
                 review=False, reviewer_proposer=None,
                 author=False, author_proposer=None, plan=None, context_prelude=None,
                 resume=False, resume_context=None, write_scope=None, base=None, defer_delivery=False,
                 calibrated_enforcement=False, ui_evidence=None,   # v3.1.8 калиброванное UI-enforcement
                 strict_judge_qualified=True,   # v3.7.1: есть ли QUALIFIED security/integration судья
                 security_reviewer_proposer=None,   # v3.7.3 (#5): ОТДЕЛЬНЫЙ security-судья (не общий reviewer)
                 reevaluate_only=False):   # v3.8.4: переоценить гейты существующего HEAD БЕЗ переавторинга
    """Один прогон движка: [worktree-изоляция] -> детект -> правки через tool-loop ->
    [commit на ветке] -> evidence (на зафиксированном SHA) -> гейты RunPlan.

    v2.108 (Operational Context): context_prelude — compiled payload из ContextBundle (реальное
    содержимое релевантных правил/решений/спек), который РЕАЛЬНО попадает в prompt модели (prepend к
    base_context tool loop) — не только статистика в отчёте.

    v2.109 (Real Resume): resume=True — ПРОДОЛЖИТЬ WorkItem поверх уже подтверждённой работы, а не
    начинать заново. Ветка ai-ops/<wid> и её коммиты НЕ удаляются (иначе потеряли бы результат);
    worktree переиспользуется (или пере-подключается к сохранившейся ветке). resume_context —
    состояние из RunHandoff (что сделано/решения/следующий шаг), реально подаётся модели в начало
    prompt, чтобы она продолжила, а не переделала подтверждённое.

    v2.94 (One Run Transaction): если plan передан контроллером — используем ЕГО (не строим второй),
    чтобы pipeline и lifecycle жили в одной транзакции с общим WorkItem/RunPlan."""
    # v3.38 (K0-проводка): параметры прогона обязаны оставаться подмножеством объявленного
    # контракта ядра (kernel/ports.ExecutionSpec). Это НЕ проверка реализации портов (реализации
    # им ещё не соответствуют — долг Phase B, записан в installer.UNWIRED_MODULES), а страж
    # дрейфа КОНТРАКТА: переименование поля в ports.py или новый параметр без записи в контракт
    # краснеет на каждом прогоне, в том числе в дочке.
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
    child_root = Path(child_root)
    signals = dict(signals or {})
    signals.setdefault("task_text", task)

    # 2. план (нужен workitem_id для имени ветки/worktree). v2.94: принимаем готовый план от
    #    контроллера; иначе строим сами (обратная совместимость: прямой вызов run_pipeline).
    if plan is None:
        plan = run_plan.build_plan(signals, workitem_id=feature)
    wid = plan["workitem_id"]

    # 1b. изоляция + base-binding + resume -> _setup_isolation (K6). Прогон в отдельном worktree на
    #     ветке ai-ops/<id>, основное дерево child не трогается; при отказе — ранний честный выход.
    _iso = _setup_isolation(child_root, wid, base, isolate=isolate, resume=resume,
                            reevaluate_only=reevaluate_only, discard_previous=discard_previous,
                            open_pr=open_pr)
    if _iso.get("error"):
        return _iso["error"]
    work_root, worktree_rel = _iso["work_root"], _iso["worktree_rel"]
    resume_info, base_binding = _iso["resume_info"], _iso["base_binding"]
    base_ref, base_sha, delivery_pf = _iso["base_ref"], _iso["base_sha"], _iso["delivery_pf"]

    # 1. детект стека (в рабочем дереве)
    profile = project_detector.detect(work_root)

    # 3. политика по умолчанию: execution, границы — по work_root.
    #    v2.81 Containment: даже базовая политика запрещает модели push-ить (block_push=True) —
    #    доставка (PR) идёт ТОЛЬКО через доверенный delivery-слой, не через tool-loop.
    #    sandbox=True дополнительно включает allowlist на shell (произвольный shell выключен)
    #    и denylist на сетевые бинарники — см. tool_broker.sandbox_policy().
    if policy is not None:
        pol = policy
    elif sandbox:
        pol = tool_broker.sandbox_policy(child_root=str(work_root), write_scope=write_scope)
    else:
        pol = tool_broker.Policy(level="execution", child_root=str(work_root), block_push=True,
                                 write_scope=write_scope)
    is_git = _git(work_root, "rev-parse", "--is-inside-work-tree")[0] == 0

    # 3b/3c. фаза install-deps (K6: _prepare_environment).
    prepare, prepare_ok, baseline_checks, prepare_mutated_tree = _prepare_environment(
        profile, work_root, pol, is_git, install_deps=install_deps, isolate=isolate,
        baseline_diff=baseline_diff)

    # 4/4a. фаза spec-gate: prompt-контекст (task+профиль+prelude/resume+провалы базы) + pre-authoring
    #       Spec-First (K6: _assemble_context_and_author).
    ctx, authored, authored_ev, spec_prestage_bad = _assemble_context_and_author(
        task, profile, plan, wid, work_root, budget,
        context_prelude=context_prelude, resume_context=resume_context,
        baseline_diff=baseline_diff, baseline_checks=baseline_checks,
        author=author, author_proposer=author_proposer, reevaluate_only=reevaluate_only)

    # 4b. фаза execute (tool-loop): реализация + распознавание факта правок (K6: _run_tool_loop).
    loop, applied, shell_changed, self_committed, head_sha = _run_tool_loop(
        proposer, work_root, pol, ctx, is_git, budget=budget, max_steps=max_steps,
        reevaluate_only=reevaluate_only, spec_prestage_bad=spec_prestage_bad)

    # 5. фаза commit: фиксация на ветке ai-ops/<wid> ДО evidence — evidence бьётся о ТОЧНЫЙ SHA
    #    (K6: _commit_work).
    committed_sha, work_branch, work_produced_by, tree_clean_before_checks = _commit_work(
        work_root, wid, task, is_git, applied, authored, shell_changed, self_committed, head_sha,
        commit=commit, reevaluate_only=reevaluate_only)

    # 6. evidence на зафиксированном SHA + наполнение gate_ev (collect/intake/regression/authored/
    #    reevaluate-seed/освобождения/UI-evidence/seam-scan/reviews/security) -> _assemble_evidence (K6).
    _ev = _assemble_evidence(
        profile, work_root, pol, child_root, wid, plan, signals, loop,
        commit=commit, is_git=is_git, committed_sha=committed_sha, base_sha=base_sha,
        authored_ev=authored_ev, allow_missing_tests=allow_missing_tests,
        calibrated_enforcement=calibrated_enforcement, ui_evidence=ui_evidence,
        review=review, reviewer_proposer=reviewer_proposer, budget=budget,
        strict_judge_qualified=strict_judge_qualified,
        security_reviewer_proposer=security_reviewer_proposer, reevaluate_only=reevaluate_only)
    _changed_for_verification = _ev["changed_for_verification"]
    coll = _ev["coll"]
    gate_ev = _ev["gate_ev"]
    tree_clean_after_checks = _ev["tree_clean_after_checks"]
    regression_proof = _ev["regression_proof"]
    exempt = _ev["exempt"]
    not_applicable = _ev["not_applicable"]
    exempt_reason = _ev["exempt_reason"]
    tests_warn = _ev["tests_warn"]
    ui_evidence_bundle = _ev["ui_evidence_bundle"]
    seam_advisory = _ev["seam_advisory"]
    reviews = _ev["reviews"]
    security_pack_result = _ev["security_pack_result"]
    effective_approval_signals = _ev["effective_approval_signals"]

    # 7. гейты RunPlan (base + треки), c evidence из коллектора + сигналы (условный approval) +
    #    освобождения по неприменимым проверкам. tested_revision -> в evidence/аудит гейтов.
    # v2.125 (finding живого прогона): security-релевантная НАХОДКА в диффе (новая зависимость/секрет →
    # gate_ev.security=fail) обязана блокировать НЕЗАВИСИМО от workflow. QUICK не содержит security-гейта,
    # поэтому новая зависимость в QUICK-задаче проскакивала. Форсируем security в оценку, если он упал.
    _gate_ids = list(plan["gates"])
    if (gate_ev.get("security") or {}).get("status") == "fail" and "security" not in _gate_ids:
        _gate_ids.append("security")
    gates = gate_executor.evaluate(plan["base_workflow"], gate_ev,
                                   gate_ids=_gate_ids, tested_revision=committed_sha,
                                   signals=signals, not_applicable=not_applicable,
                                   exempt_reason=exempt_reason)
    # КТО ЗАКРЫЛ — ЧЕЛОВЕКУ, А НЕ ТОЛЬКО В JSON. Замер 19.08.2026: 19 гейтов из 35 не имеют
    # исполняемого валидатора, и в выводе прогона это ничем не отличалось от проверенного машиной.
    # Строка печатается всегда: молчать о ней там, где мнения нет, значило бы приучать к тому, что
    # её отсутствие ничего не значит.
    _cl = gates.get("closure") or {}
    _cnt = _cl.get("counts") or {}
    _opinion = _cl.get("judged_or_human") or []
    print(f"  гейты: проверено машиной {_cnt.get('validator', 0)} из {len(_gate_ids)}"
          + (f"; остальное — мнение: {', '.join(_opinion)}" if _opinion else "; мнением не закрыт ни один"))

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
        # ЗАПИСЬ того же кеша — симметрично чтению выше: не записали, значит следующий прогон
        # пересчитает. Вердикт не зависит от наличия файла (ревизия 2026-08-11).
        except Exception:  # noqa: BLE001,S110 — потеря кеша не меняет вердикт, пересчитаем
            pass

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
    from ai_ops_kit.gates import spec_levels as _sl   # для report.spec_first (_spec_path) ниже

    # baseline-diff (finding живого прогона): что правка сломала/починила против базы
    regressions, fixed = _diff_checks(baseline_checks, coll["checks"]) if baseline_diff else ([], [])
    no_regressions = (len(regressions) == 0) if baseline_diff else None
    # P0.1 (аудит v2.79): baseline-режим делает baseline-осведомлённым ТОЛЬКО
    # implementation_verification (красная база не должна блокировать). ВСЕ ОСТАЛЬНЫЕ блокирующие
    # гейты (requirements/specification/plan/code_review/security/треки) остаются обязательными —
    # иначе baseline-diff обходит их и выдаёт ложный ready. unmet_gates уже только блокирующие.
    other_blocking_unmet = [g for g in gates["unmet_gates"] if g != "implementation_verification"]

    # 8. финал: draft PR (только если готово к PR и явно запрошено). Механизм честен offline:
    #    нет токена/remote -> unavailable, PR не имитируется.
    # finding аудита (P0.5): ready_for_pr ТРЕБУЕТ реального коммита (committed_sha),
    # evidence на точном SHA и чистого дерева до/после проверок. dry-run (commit=False) НИКОГДА
    # не бывает ready — нет ревизии, к которой привязать draft PR.
    tree_ok = bool(tree_clean_before_checks) and (tree_clean_after_checks is not False)
    # P0.6 + v2.118: окружение квалифицировано, если install прошёл ЛИБО проверки реально отработали
    # (нет симптомов неподготовленного окружения). Провал install при прошедших проверках больше не
    # даёт false-negative (finding живого прогона: `pip install -e .` падает на не-пакете, а pytest
    # проходит) — при этом сломанное окружение (exit 127 / нет тулчейна) по-прежнему блокирует.
    # v2.121 (P1.4): провал install игнорируется ТОЛЬКО если окружение ДОКАЗАННО рабочее — хотя бы
    # одна проверка реально отработала (pass или честный fail без env-симптома). Нет проверок вовсе
    # ИЛИ все падения — env-симптомы -> НЕ квалифицировано (дыра v2.118 закрыта).
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
            # же diff коммита, что и recheck одобрений. Прежде гейт был объявлен в реестре, но его
            # никто не вызывал: связность контуров существовала как библиотека и как обещание в
            # CHANGELOG (найдено независимым ревью 3.35). Advisory: несогласованность даёт warn.
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
    # переоценка существующего committed HEAD после человеко-одобрения). Раньше base_ok требовал
    # stopped=="done" -> reevaluate НИКОГДА не мог достичь ready_for_pr (delivery-after-approval путь был
    # недостижим). Остальные условия base_ok (committed_sha/revision/tree/env/approvals) по-прежнему строги.
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

    # 8. доставка (P0.4 аудит v2.79): draft PR отделён от ready_for_pr. Если --open-pr запрошен,
    #    УСПЕХ прогона требует реально открытого PR; провал доставки не маскируется зелёным.
    # v3.0.16 Phase A (finding аудита #1): run_pipeline НИКОГДА не выполняет внешнюю доставку — только
    # возвращает DeliveryPlan. Единственный разрешённый вызывающий _deliver_pr — транзакционный контроллер
    # (ai_ops_run), который доставляет ТОЛЬКО после durable-фиксации RunHandoff+report+journal +
    # DeliveryIntent. Так прямой вызов run_pipeline(..., open_pr=True) больше НЕ может обойти lifecycle-
    # барьер (прежде defer_delivery=False давал inline-доставку). Параметр defer_delivery устарел и
    # игнорируется (внешнее действие из pipeline запрещено архитектурно).
    delivery, delivery_plan, can_deliver = _plan_delivery(
        open_pr, ready, committed_sha, work_branch, base_binding, base_ref, base_sha,
        work_root, wid, task, delivery_pf)
    # ready есть, доставка НЕ выполнена в pipeline: overall — «готово к доставке» (контроллер финализирует).
    overall_status = _compute_overall_status(ready, can_deliver, open_pr)

    not_yet = _build_not_yet_list(commit, env_qualified, open_pr, spec_prestage_bad,
                                  spec_depth_missing, spec_incomplete, spec_bad_status,
                                  context_overflow, approvals_cover_ok, approval_recheck,
                                  acceptance_block_reason=(acceptance_block_reason if acceptance_block else None))

    report = {
        "schema_version": 1, "kind": "execution-pipeline",
        "workitem_id": plan["workitem_id"],
        "child_root": str(child_root),          # нужен вывода: уровень детализации берётся из репо
        "base_workflow": plan["base_workflow"],
        "profile": {"stacks": [s.get("language") for s in profile.get("stacks", [])],
                    "undetermined": profile.get("undetermined", [])},
        "containment": _build_containment(sandbox, pol, loop),
        "loop": _build_loop_section(loop, applied),
        "isolation": {"worktree": worktree_rel},   # каталог изоляции (None -> прогон в основном дереве)
        "base_binding": base_binding,              # v3.0.1 (P0): base_ref + base_sha, от которого форкнута ветка
        "resume": resume_info,                     # v2.109: продолжение поверх подтверждённой работы (None если resume не запрошен)
        "prepare": prepare,                        # установка зависимостей стека (npm ci/... ) в worktree; None вне изоляции
        "prepare_ok": prepare_ok,                  # install-команды стека прошли (для наблюдаемости)
        "env_qualified": env_qualified,            # v2.118: install прошёл ЛИБО проверки реально отработали
        "prepare_mutated_tree": prepare_mutated_tree,  # P0.6: подготовка меняла tracked -> откачено до модели
        "commit": _build_commit_section(work_branch, committed_sha, evidence_revision,
                                        revision_matches, _changed_for_verification,
                                        work_produced_by, tree_clean_before_checks,
                                        tree_clean_after_checks),
        # v3.30: доказательство исправления — в отчёте, а не только в вердикте гейта: постфактум
        # видно, ЧЕМ подтверждена правка (или почему не подтверждена).
        "regression": regression_proof,
        "checks": coll["checks"],
        "exemptions": sorted(exempt),          # флаги, освобождённые как неприменимые (видно, не тихо)
        "tests_warn": tests_warn,              # громкий сигнал об отсутствии тестов (если есть)
        "gates": {"evaluated": gates["evaluated_gates"], "unmet": gates["unmet_gates"],
                  "blocked": gates["blocked"],
                  "other_blocking_unmet": other_blocking_unmet,   # P0.1: блокирующие ≠ impl_verification
                  # КТО ЗАКРЫЛ: разбивка validator/judge/writer/human. Без неё отчёт утверждал
                  # «гейты пройдены» одинаково и там, где считала машина, и там, где высказался
                  # судья. 19 гейтов из 35 не имеют валидатора вовсе.
                  "closure": gates.get("closure"),
                  # evidence/аудит (аудит v2.79): полные per-gate результаты, не только сводка
                  "gate_results": gates.get("gate_results"),
                  "tested_revision": committed_sha},
        # v2.121 (P1.2 п.4): покрыло ли человеко-одобрение фактически изменённые пути (после диффа)
        "approval_recheck": approval_recheck,
        # v3.35.2: НАХОДКИ ГЕЙТА СВЯЗНОСТИ ДОХОДЯТ ДО ЧЕЛОВЕКА. Гейт исполнялся и писал evidence, но
        # вывод прогона о нём молчал: «описание продукта отстало от кода» существовало только внутри
        # yaml-артефакта. Гейт, чьи находки не видны, — это гейт, которого нет.
        "contour_consistency": contour_consistency,
        # v2.83 Full RunPlan: трейс независимых ревью (какие ai-review гейты судились, вердикт,
        # что читал судья, что отклонено). None -> ревью не запускалось (нет --review/reviewer).
        "reviews": reviews,
        # v3.1.9: UIEvidenceBundle, собранный на ТОЧНОМ committed_sha (qualification evidence). None,
        # если калибровка выкл / evidence инжектировано / нет коммита. commit_sha в bundle привязан.
        "ui_evidence": ui_evidence_bundle,
        # v3.7.4: seam-scan advisory (дефект шва по дифу base..committed). would_block=true -> шов без
        # доказанного перехода; пока advisory (не влияет на overall), станет gate после обкатки.
        "seam_scan": seam_advisory,
        # v2.95: детерминированный security-скан (секреты/новые зависимости/injection-флаги). None,
        # если гейта security нет в плане или не коммитили. Закрывает no_secrets/deps_approved (факты);
        # no_injection_surface — судье. Находка -> security блокирует.
        # ЗАЯВКА #139 (вторая половина): здесь стояли ровно четыре поля, и `domain_results` — где
        # лежат САМИ находки (path/line/класс) и `applies_because` — в отчёт не попадали вовсе.
        # Гейт отправляет человека в этот артефакт со словами «блокирующие домены (critical/high
        # находки)», поэтому отчёт без находок делает утверждение гейта непроверяемым. Проекция
        # `security_pack.for_report` — белый список полей: значения секретов и содержимое файлов в
        # отчёт не уезжают (он лежит в репозитории и попадает в PR).
        "security_scan": _security_pack_for_report(security_pack_result),
        # v2.86 Product Authoring: трейс произведённых артефактов (requirements/plan) — что
        # авторизовано, валидна ли форма, какие required_evidence закрыты. None -> без --author.
        "authored": authored,
        # baseline-diff: None вне режима; иначе — статусы проверок на базе + регрессии/починки
        "baseline": ({"checks": {k: (v or {}).get("status") for k, v in (baseline_checks or {}).items()},
                      "regressions": regressions, "fixed": fixed, "no_regressions": no_regressions}
                     if baseline_diff else None),
        "ready_criterion": ready_criterion,    # all-green | no-regressions
        # v2.106 enforcement: spec-depth (незакрытые разделы уровня, мапящиеся на unmet-гейты) и
        # context-budget overflow — блокируют ready наравне с гейтами.
        "spec_depth": {"level": _level, "missing": spec_depth_missing, "ok": spec_depth_ok},
        # v2.110 Real Spec-First: реальный spec.yaml существует, но неполон -> блокирует implementation
        "spec_first": {"artifact_present": bool(spec_incomplete) or _sl._spec_path(child_root, wid).is_file(),
                       "incomplete_sections": spec_incomplete, "ok": spec_complete_ok,
                       # v2.123 (P0.1): pre-authoring запущен ДО реализации; невалидная спека -> 0 кода
                       "prestage": {"ran": bool(author and author_proposer is not None),
                                    "invalid": [e.get("gate") for e in spec_prestage_bad],
                                    "implementation_skipped": bool(spec_prestage_bad)}},
        "context_overflow": context_overflow,
        "acceptance_criteria": acceptance_criteria,   # B2-14: сверялись ли критерии с результатом
        # honest: «готово к PR» = петля done + коммит + evidence на SHA + prepare_ok + spec-depth +
        # не-overflow + (all-green: гейты не блокируют | no-regressions: нет новых провалов И blocking-гейты пройдены)
        "ready_for_pr": ready,
        "delivery": delivery,                  # P0.4: статус доставки draft PR отдельно от ready
        "delivery_plan": delivery_plan,        # v3.0.15 (P0): план для контроллера при defer_delivery
        "overall_status": overall_status,      # error | delivery-failed | delivered | ready-undelivered
        "draft_pr": delivery.get("pr"),        # результат открытия PR (None если deferred/не открыт)
        "not_yet": not_yet,
    }
    # v3.38 (K7): инварианты pipeline — fail-closed, нарушение записывается в отчёт.
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
    return report


def _prepare_environment(profile, work_root, pol, is_git, *, install_deps, isolate, baseline_diff):
    """Фаза install-deps: зависимости стека + baseline-evidence + откат мутаций подготовки до правок
    модели. v3.38 (K6): вынесено из run_pipeline. -> (prepare, prepare_ok, baseline_checks, mutated)."""
    # P0.6/v2.93: снимок untracked ДО install/baseline — удалить только НОВЫЕ (package-lock и т.п.),
    # не тронув untracked пользователя. Игнорируемые (node_modules) сюда не попадают.
    untracked_before_prep = _untracked(work_root) if is_git else set()
    # 3b. зависимости стека В ИЗОЛИРОВАННОМ worktree (иначе build/lint/test = exit 127); в основном
    #     дереве НЕ ставим. node_modules обычно в .gitignore -> дерево чистое.
    prepare = None
    if install_deps and isolate:
        prepare = _install_dependencies(profile, work_root, pol)
    # P0.6: install обязан ПРОЙТИ — иначе baseline/проверки недостоверны, прогон не может быть ready.
    prepare_ok = (prepare is None) or all(p.get("ok") for p in prepare)
    # 3c. baseline-evidence: прогон проверок на БАЗЕ до правок — отличить пред-существующие провалы
    #     репо от РЕГРЕССИЙ этой правки (finding живого прогона: ii-sreda был красным сам по себе).
    baseline_checks = None
    if baseline_diff:
        baseline_checks = evidence_collector.collect(profile, work_root, pol, broker=tool_broker)["checks"]
    # P0.6+v2.93: install/baseline могли намутить tracked (lock/снапшоты) И создать новые untracked.
    # Откатываем ОБА вида ДО модели, иначе `git add -A` втянул бы файлы подготовки в AI-коммит:
    # tracked — `checkout -- .`; новые untracked (delta к снимку) — адресно (untracked юзера не трогаем).
    prepare_mutated_tree = False
    if is_git and not _tree_clean(work_root):
        prepare_mutated_tree = True
        _git(work_root, "checkout", "--", ".")
        new_untracked = _untracked(work_root) - untracked_before_prep
        for rel in new_untracked:
            try:
                fp = (work_root / rel)
                if fp.is_file() or fp.is_symlink():
                    fp.unlink()
            except OSError:
                pass
    return prepare, prepare_ok, baseline_checks, prepare_mutated_tree


def _assemble_context_and_author(task, profile, plan, wid, work_root, budget, *,
                                 context_prelude, resume_context, baseline_diff, baseline_checks,
                                 author, author_proposer, reevaluate_only):
    """Фаза spec-gate: собрать base_context tool-loop (task+профиль+prelude/resume+провалы базы) и
    pre-authoring по Spec-First (автор -> валидация формы -> реализация только при валидной спеке).
    v3.38 (K6): вынесено из run_pipeline. -> (ctx, authored, authored_ev, spec_prestage_bad)."""
    ctx = f"{task}\n\n{_profile_summary(profile)}"
    # v2.108: compiled payload из ContextBundle РЕАЛЬНО в prompt (не только отчёт).
    if context_prelude:
        ctx = context_prelude + "\n\n" + ctx
    # v2.109 Real Resume: состояние из RunHandoff в начало prompt — модель ПРОДОЛЖАЕТ, а не переделывает.
    if resume_context:
        ctx = resume_context + "\n\n" + ctx
    if baseline_diff:
        fails = _baseline_failure_summary(baseline_checks)
        if fails:
            ctx += ("\n\n=== ТЕКУЩИЕ ПРОВАЛЫ ПРОВЕРОК НА БАЗЕ (почини относящиеся к задаче; "
                    "не ломай остальное) ===\n" + fails)
    # 4a. v2.123 (P0.1) Spec-First: СНАЧАЛА автор (requirements/plan/spec), движок валидирует ФОРМУ.
    #     Невалидный артефакт -> tool loop НЕ запускается (0 impl-вызовов). Валидные -> в prompt.
    #     Качество судит независимый ревьюер (--review)/человек, не эта проверка формы.
    authored, authored_ev = None, {}
    spec_prestage_bad = []
    if author and author_proposer is not None and not reevaluate_only:
        authored_ev, authored, _wrote_art = _run_authoring(
            author_proposer, work_root, plan["gates"], {}, wid, task, budget)
        spec_prestage_bad = [e for e in (authored or []) if e.get("valid") is False]
        if not spec_prestage_bad:
            _spec_ctx = _authored_context(authored, work_root, wid)
            if _spec_ctx:
                ctx = _spec_ctx + "\n\n" + ctx
    return ctx, authored, authored_ev, spec_prestage_bad


def _run_tool_loop(proposer, work_root, pol, ctx, is_git, *, budget, max_steps,
                   reevaluate_only, spec_prestage_bad):
    """Фаза execute: снять HEAD до правок, прогнать реализацию через модель (или пропустить при
    reevaluate/невалидной pre-spec), распознать факт правок из git (broker/shell/model-commit).
    v3.38 (K6): вынесено из run_pipeline. -> (loop, applied, shell_changed, self_committed, head_sha)."""
    # HEAD НА СТАРТЕ — точка отсчёта «что произвёл ИМЕННО ЭТОТ прогон». С base_sha сравнивать нельзя:
    # при resume/reevaluate и на ушедшей вперёд ветке HEAD != база ДО работы -> кит увидел бы работу,
    # которой не делали.
    _rc_hb, _out_hb, _ = _git(work_root, "rev-parse", "HEAD") if is_git else (1, "", "")
    head_before = _out_hb.strip() if _rc_hb == 0 else None
    # 4b. tool-loop: реализация. Пропускается при невалидной pre-spec (Spec-First: нет спеки -> нет кода).
    if reevaluate_only:
        # v3.8.4: НЕ авторим и НЕ гоняем loop — переоцениваем существующий HEAD как есть (0 вызовов).
        loop = {"schema_version": 1, "kind": "tool-loop-report", "stopped": "reevaluate-only",
                "steps": 0, "model_calls": 0, "executed": [], "denied": [], "evidence": [], "transcript": []}
    elif spec_prestage_bad:
        loop = {"schema_version": 1, "kind": "tool-loop-report", "stopped": "spec-prestage-failed",
                "steps": 0, "model_calls": 0, "executed": [], "denied": [], "evidence": [], "transcript": []}
    else:
        loop = tool_loop.run_loop(proposer, work_root, pol, budget=budget,
                                  max_steps=max_steps, base_context=ctx)
    applied = [e for e in loop["executed"] if e.get("op") == "write" and e.get("ok")]
    # v2.93: факт правок из git (tracked-diff ИЛИ новые untracked), не только из счётчика write —
    # иначе правки через shell (sed/форматтер) не считались «применено» и коммит терялся.
    shell_changed = bool(applied) or (is_git and _has_changes(work_root))
    # НАХОДКА ИИ-СРЕДЫ: модель может закоммитить САМА — дерево чистое, applied пусто, _has_changes False,
    # хотя коммит уже на ветке. Третий факт: HEAD сдвинулся ЗА ЭТОТ прогон.
    self_committed, head_sha = (_head_advanced(work_root, head_before)
                                if is_git else (False, None))
    return loop, applied, shell_changed, self_committed, head_sha


def _commit_work(work_root, wid, task, is_git, applied, authored, shell_changed, self_committed,
                 head_sha, *, commit, reevaluate_only):
    """Фаза commit: зафиксировать правки на ветке ai-ops/<wid> ДО evidence (evidence бьётся о ТОЧНЫЙ
    SHA); reevaluate переиспользует HEAD. work_produced_by (broker/shell/model-commit) — факт для
    человека. v3.38 (K6): вынесено из run_pipeline. -> (committed_sha, work_branch, produced_by, clean)."""
    committed_sha, work_branch = None, None
    # ЧЕМ произведена работа: «правок 0» при живом коммите читается как «кит не работает».
    work_produced_by = ("broker" if applied else ("shell" if shell_changed else None))
    tree_clean_before_checks = None
    # v2.93: коммитим при правках В ДЕРЕВЕ (git-diff/untracked, вкл. shell и артефакты), не только
    # при applied. Для не-git репо fallback на applied.
    have_work = ((is_git and _has_changes(work_root)) or bool(applied) or bool(authored)
                 or self_committed)
    if reevaluate_only:
        # v3.8.4: существующий HEAD — уже зафиксированная работа; НЕ создаём коммит, план/SHA не меняются.
        work_branch = f"ai-ops/{wid}"
        _rc_h, _out_h, _ = _git(work_root, "rev-parse", "HEAD")
        committed_sha = _out_h.strip() if _rc_h == 0 else None
        tree_clean_before_checks = _tree_clean(work_root)
    elif commit and have_work:
        work_branch = f"ai-ops/{wid}"
        committed_sha = _commit_on_branch(work_root, work_branch,
                                          f"ai-ops: {task[:60]}")
        # Коммитить нечего, но HEAD ушёл от базы -> модель зафиксировала сама. Берём ЕЁ коммит: ground truth — git.
        if committed_sha is None and self_committed:
            _rc_b, _out_b, _ = _git(work_root, "rev-parse", "--abbrev-ref", "HEAD")
            work_branch = _out_b.strip() if _rc_b == 0 and _out_b.strip() != "HEAD" else work_branch
            committed_sha = head_sha
            work_produced_by = "model-commit"
        # P0.5: после коммита дерево обязано быть чистым — иначе часть правок не в SHA.
        tree_clean_before_checks = _tree_clean(work_root)
    return committed_sha, work_branch, work_produced_by, tree_clean_before_checks


def _seam_scan_advisory(work_root, base_sha, committed_sha):
    """v3.7.4 SEAM-SCAN (ADVISORY, non-blocking): детектор «дефекта шва» по дифу base..committed
    (запись без round-trip / catch без happy-path / stub без real-run / optional-поле / смена
    предусловия). НЕ блокирует; станет gate после обкатки. v3.38 (K6): вынесено. -> seam_advisory."""
    seam_advisory = None
    if committed_sha:
        try:
            from ai_ops_kit.security import seam_scan
            _diff = _change_context_range(work_root, base_sha, committed_sha, max_chars=20000)
            _sc = seam_scan.scan_diff(_diff or "")
            _dec = seam_scan.gate_decision(_sc)
            seam_advisory = {"mode": "advisory", "would_block": _dec["block"],
                             "blockers": _dec["blockers"], "advisories": _dec["advisories"],
                             "findings": _sc["findings"]}
        except Exception as _e:  # noqa: BLE001 — advisory-детектор не должен ронять прогон
            seam_advisory = {"error": f"seam_scan failed: {type(_e).__name__}: {_e}"[:200]}
    return seam_advisory


def _context_budget_overflow(signals, work_root, plan):
    """v2.106 #3 Context-budget: контекст задачи превышает бюджет (ContextBundle overflow) -> пакет
    не атомарен -> блок ready. FAIL-CLOSED: ошибка сборки bundle = overflow. v3.38 (K6): вынесено.
    -> context_overflow (bool)."""
    context_overflow = False
    try:
        from ai_ops_kit.context import context_compiler as _cc
        _bundle = _cc.compile_bundle(signals, work_root, plan=plan)
        context_overflow = bool(_bundle.get("overflow"))
    except Exception:  # noqa: BLE001 — v3.0.11 (P2): FAIL-CLOSED. Прежде исключение -> overflow=False ->
        # блокер «превышает context budget» тихо исчезал. Теперь ошибка = overflow (блокируем, не молчим).
        context_overflow = True
    return context_overflow


def main(argv):
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
