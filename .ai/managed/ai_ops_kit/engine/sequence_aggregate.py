"""Aggregate-верификация и доставка результата последовательности WorkPackages.

Пост-обработка execute_sequence (поведение байт-в-байт, вынесено из workpackage_executor чистым
рефакторингом): baseline на точной sequence_base_sha, закрытие security/code_review на ИНТЕГРАЦИОННОМ
диффе base..final, aggregate-верификация финального SHA, агрегатный вердикт, доставка draft PR и
durable-запись sequence-report. workpackage_executor ре-экспортирует эти имена из своей шапки, поэтому
внешние импортёры (`from ai_ops_kit.engine.workpackage_executor import _aggregate_verify`) продолжают
работать без изменений.
"""
from __future__ import annotations

from pathlib import Path


def _git(root, *a):
    from ai_ops_kit.shared import gitio
    return gitio.git(root, *a)   # v3.0.13 (блок C): единый git-хелпер с таймаутом


def _collect_base_checks_at(child_root, base_sha, sandbox):
    """v3.0-rc16/rc20 (finding аудита P0): baseline-проверки СТРОГО на sequence_base_sha в отдельном
    read-only detached-worktree. rc20: PROVENANCE — результат `worktree add` и HEAD ПРОВЕРЯЮТСЯ; baseline
    считается доказанным ТОЛЬКО если worktree создан и его HEAD == base_sha. Иначе -> None (вызывающий
    НЕ деградирует на другой checkout: нет доказанного baseline -> aggregate unavailable -> нет PR).
    -> {"checks":..., "sha": base_sha, "proven": True} | None."""
    if not base_sha:
        return None
    from ai_ops_kit.shared import project_detector as _pd
    from ai_ops_kit.gates import evidence_collector as _ec
    from ai_ops_kit.engine import tool_broker as _tb
    child_root = Path(child_root)
    if _git(child_root, "rev-parse", "--is-inside-work-tree")[0] != 0:
        return None
    tmp = child_root / ".ai" / "worktrees" / f"_base-{base_sha[:12]}"
    try:
        rc_add, _, _ = _git(child_root, "worktree", "add", "--detach", "-f", str(tmp), base_sha)
        if rc_add != 0 or not tmp.is_dir():
            return None                                   # worktree add не удался -> baseline НЕ доказан
        rc_h, head, _ = _git(tmp, "rev-parse", "HEAD")
        if rc_h != 0 or (head or "").strip() != base_sha:  # HEAD обязан быть РОВНО на base_sha
            return None
        pol = (_tb.sandbox_policy(child_root=str(tmp)) if sandbox
               else _tb.Policy(level="execution", child_root=str(tmp), block_push=True))
        checks = _ec.collect(_pd.detect(tmp), tmp, pol, broker=_tb)["checks"]
        return {"checks": checks, "sha": base_sha, "proven": True}
    except Exception:  # noqa: BLE001
        return None
    finally:
        # Причина подавления ЗАПИСАНА (срез engine ратчета 2026-08-12): это УБОРКА в `finally`, и её
        # отказ не участвует ни в одном утверждении — baseline уже либо доказан, либо нет (`return`
        # выше). Ронять здесь означало бы подменить результат baseline ошибкой удаления временного
        # worktree. Цена отказа — оставленный каталог в `.ai/worktrees/`, видимый и в `git worktree
        # list`, и в дереве; это мусор, а не ложный green.
        try:
            _git(child_root, "worktree", "remove", "--force", str(tmp))
        except Exception:  # noqa: BLE001,S110 — уборка после уже вынесенного вердикта: отказ не меняет baseline
            pass


def _collect_baseline(child_root, sequence_base_sha, sandbox):
    # v3.0-rc16/rc20 (finding аудита P0): baseline СТРОГО на sequence_base_sha (detached worktree с
    # проверкой HEAD). rc20: БЕЗ fallback на child_root — если baseline не доказан на точной базе,
    # aggregate НЕДОСТУПЕН (baseline_proven=False) -> PR не открывается. Иначе sequence от develop мог
    # сравниться с baseline от main -> false green.
    _base_res = _collect_base_checks_at(child_root, sequence_base_sha, sandbox)
    base_checks = (_base_res or {}).get("checks") if _base_res else None
    baseline_proven = bool(_base_res and _base_res.get("proven"))
    return base_checks, baseline_proven


def _aggregate_close_security(agg_sec, vroot, base_sha, final_sha, signals, reviewer_proposer, review,
                              security_reviewer_proposer=None, strict_judge_qualified=True,
                              wid=None, child_root=None):
    """v3.8.3-rc2 (#4/#4b — enforcement #5 на АГРЕГАТЕ): needs_review на integration-диффе закрывается ТОЛЬКО:
      (a) QUALIFIED security-судьёй — ОТДЕЛЬНЫЙ security_reviewer_proposer при strict_judge_qualified=True; ЛИБО
      (b) человеко-ApprovalRecord, привязанным к ИНТЕГРАЦИОННОМУ SHA (binds_to=final_sha).
    Общий code-reviewer (reviewer_proposer) БОЛЬШЕ НЕ закрывает aggregate security (раньше его pass -> clear —
    это и был незакрытый шов #5). blocked/error/clear не трогаем (fail-closed). Без судьи И без человеко-
    одобрения на integration-SHA needs_review остаётся needs_review (честный не-ready).
    -> (agg_sec', judge_result|None)."""
    agg_sec = dict(agg_sec or {})
    if agg_sec.get("overall") != "needs_review":
        return agg_sec, None

    # (a) QUALIFIED security-судья (writer≠judge, read-only) — только он, не общий reviewer
    if strict_judge_qualified and security_reviewer_proposer is not None:
        try:
            from ai_ops_kit.engine import execution_pipeline as _ep
            ctx = _ep._change_context_range(vroot, base_sha, final_sha)  # вся цепочка base..final
            status, res = _ep._review_security(security_reviewer_proposer, vroot, agg_sec, final_sha,
                                               {"max_model_calls": 12}, change_context=ctx)
            agg_sec["reviewer_status"] = status
            if status == "pass":
                agg_sec["overall"] = "clear"
                agg_sec["closed_by"] = "aggregate-qualified-security-judge"
            elif isinstance(res, dict) and res.get("invalid"):
                agg_sec["reviewer_invalid"] = res.get("invalid")   # false-green отклонён
            return agg_sec, res
        except Exception as e:  # noqa: BLE001 — сбой судьи на aggregate = fail-closed (не clear)
            agg_sec["overall"] = "error"
            agg_sec["review_error"] = str(e)
            return agg_sec, None

    # (b) человеко-ApprovalRecord на ИНТЕГРАЦИОННОМ SHA (#4b): КАЖДЫЙ needs_review-домен обязан иметь валидную
    # strict-запись, привязанную к integration-SHA (plan_hash=final_sha -> binds_to обязан == final_sha).
    # Проверяем напрямую по доменам aggregate (не через signals — needs_review уже перечислен).
    _root = child_root or vroot
    _nr = list(agg_sec.get("needs_review") or [])
    if wid and _root and _nr:
        try:
            from ai_ops_kit.gates import approvals as _appr
            _recs = _appr.load_approvals(_root, wid)
            _now = _appr._now_iso()
            _closed = [d for d in _nr if any(
                rc.get("approval") == d and _appr._record_valid(rc, now=_now, plan_hash=final_sha, strict=True)
                for rc in _recs)]
            _open = [d for d in _nr if d not in _closed]
            agg_sec["human_check"] = {"closed": _closed, "open": _open, "records_seen": len(_recs),
                                      "bound_to": "integration_sha"}
            if not _open:                                   # ВСЕ needs_review закрыты человеком на integration-SHA
                agg_sec["overall"] = "clear"
                agg_sec["closed_by"] = "human-approval-integration-sha"
            return agg_sec, None
        except Exception as e:  # noqa: BLE001 — сбой проверки одобрения = fail-closed
            agg_sec["overall"] = "error"
            agg_sec["review_error"] = str(e)
            return agg_sec, None

    # ни qualified-судьи, ни человека на integration-SHA -> awaiting (общий reviewer НЕ закрывает security)
    agg_sec.setdefault("closed_by", None)
    return agg_sec, None


def _aggregate_code_review(vroot, base_sha, final_sha, signals, reviewer_proposer, review):
    """v3.0-rc13 (finding аудита P0): независимый code_review ИНТЕГРИРОВАННОГО диффа. rc16 (P0): ревьюер
    получает контекст ВСЕЙ цепочки base..final (`_change_context_range`), а не только последний коммит —
    иначе риск взаимодействия пакет1↔пакет3 не виден. -> (ok, reviews|None). ok=False, если ревьюер
    ЗАБЛОКИРОВАЛ (fail / warn-на-блокирующем). Без ревью — ok=True (per-package код-ревью уже прошло)."""
    if not (review and reviewer_proposer):
        return True, None
    try:
        from ai_ops_kit.engine import execution_pipeline as _ep
        ctx = _ep._change_context_range(vroot, base_sha, final_sha)
        gev, revs = _ep._run_reviews(reviewer_proposer, vroot, ["code_review"], {},
                                     dict(signals or {}), final_sha, {"max_model_calls": 16},
                                     change_context=ctx)
        # v3.0-rc20 (finding аудита P0): ТОЛЬКО явный валидный pass закрывает aggregate code_review.
        # Раньше `return (not blocked)` был fail-OPEN: no-verdict/невалидная структура/timeout/budget
        # оставляли blocked=False -> ok=True -> aggregate_ready БЕЗ подтверждённого вердикта. Теперь
        # source of truth — gate_ev['code_review'].status=='pass' (ставится _run_reviews только при
        # валидном НЕ-fail вердикте); всё остальное -> ok=False (как per-package).
        ok = (gev.get("code_review") or {}).get("status") == "pass"
        return ok, revs
    except Exception:  # noqa: BLE001 — сбой aggregate-ревью = fail-closed
        return False, None


def _aggregate_verify(child_root, wid, sandbox, final_sha, base_checks, sequence_base_sha,
                      signals, reviewer_proposer, review, baseline_proven,
                      security_reviewer_proposer=None, strict_judge_qualified=True):
    """v3.0.13 (блок C): AGGREGATE-верификация финального интегрированного SHA — вынесена из
    execute_sequence БЕЗ изменения поведения (чистый вход->aggregate-dict). Перепроверяет результат
    ЦЕЛИКОМ на final_sha против sequence_base_sha: регрессии проверок, дерево чистое, HEAD==final_sha,
    evidence на final_sha, агрегатный security (полный дифф base..final) и aggregate code_review."""
    from ai_ops_kit.shared import project_detector as _pd2
    from ai_ops_kit.gates import evidence_collector as _ec2
    from ai_ops_kit.engine import tool_broker as _tb2
    from ai_ops_kit.engine import execution_pipeline as _ep
    from ai_ops_kit.security import security_pack as _sp2
    try:
        wt = Path(child_root) / ".ai" / "worktrees" / wid
        vroot = wt if wt.is_dir() else Path(child_root)
        # v3.0-rc2 (P0.4): проверяем ИМЕННО финальный SHA — HEAD worktree обязан == final_sha.
        head_sha = _git(vroot, "rev-parse", "HEAD")[1]
        revision_ok = (head_sha == final_sha)
        _vpol = (_tb2.sandbox_policy(child_root=str(vroot)) if sandbox
                 else _tb2.Policy(level="execution", child_root=str(vroot), block_push=True))
        coll = _ec2.collect(_pd2.detect(vroot), vroot, _vpol, broker=_tb2)
        final_checks = coll["checks"]
        _is_git = _git(vroot, "rev-parse", "--is-inside-work-tree")[0] == 0
        tree_clean = _ep._tree_clean_after_checks(vroot) if _is_git else True
        agg_reg, _agg_fix = _ep._diff_checks(base_checks, final_checks) if base_checks else ([], [])
        # v3.0-rc13 (P0): база security = sequence_base_sha (HEAD ДО пакета 1); деградация -> первый родитель
        _base_sha = sequence_base_sha
        if not _base_sha and final_sha:
            _fp = _git(vroot, "rev-list", "--max-parents=0", final_sha)[1].split("\n")[0]
            _base_sha = _fp or None
        agg_sec = None
        try:
            agg_sec = _sp2.run_pack(vroot, base=(_base_sha or None), signals=(signals or {}))
        except Exception:  # noqa: BLE001
            agg_sec = {"overall": "error"}
        # needs_review != провал — awaiting: закрываем независимым security-reviewer на aggregate-диффе
        agg_sec, agg_sec_review = _aggregate_close_security(
            agg_sec, vroot, _base_sha, final_sha, signals, reviewer_proposer, review,
            security_reviewer_proposer=security_reviewer_proposer,
            strict_judge_qualified=strict_judge_qualified, wid=wid, child_root=child_root)
        # `advisory` наравне с `clear`: домены, поднятые только совпадением по содержимому и без
        # находок, не держат агрегат — тот же выбор, что и на гейте (`security_pack._content_only`).
        agg_sec_ok = (agg_sec or {}).get("overall") in ("clear", "advisory")
        agg_code_ok, agg_code_reviews = _aggregate_code_review(
            vroot, _base_sha, final_sha, signals, reviewer_proposer, review)
        return {"verified": True, "regressions": agg_reg, "no_regressions": not agg_reg,
                "final_sha": final_sha, "sequence_base_sha": _base_sha,
                "baseline_proven": baseline_proven,   # rc20 (P0): baseline доказан на точной базе
                "revision_ok": revision_ok, "tree_clean": tree_clean,
                "evidence_revision": coll.get("revision"),
                "evidence_revision_ok": (coll.get("revision") == final_sha),
                "security_overall": (agg_sec or {}).get("overall"), "security_ok": agg_sec_ok,
                "security_reviewer_status": (agg_sec or {}).get("reviewer_status"),
                # ТА ЖЕ БОЛЕЗНЬ НА ПОСЛЕДОВАТЕЛЬНОМ ПУТИ (заявка #139): наружу уходил только
                # `overall`, и человек, которому гейт назвал блокирующие домены, не мог увидеть ни
                # одной находки. Проекция та же, что у одиночного прогона — одна правда об охвате и
                # находках, а не два разных ответа на один вопрос.
                "security_scan": _sp2.for_report(agg_sec),
                "code_review_ok": agg_code_ok, "code_reviews": agg_code_reviews,
                "checks": {k: (v or {}).get("status") for k, v in (final_checks or {}).items()}}
    except Exception as e:  # noqa: BLE001
        return {"verified": False, "error": str(e)}


def _compute_aggregate_verdict(completed, ordered, stopped_at, results, final_sha, chain_ok,
                               base_drift, child_root, wid, sandbox, base_checks, sequence_base_sha,
                               signals, reviewer_proposer, review, baseline_proven,
                               security_reviewer_proposer, strict_judge_qualified):
    executed_all = len(completed) == len(ordered) and stopped_at is None
    ready_all = executed_all and all(r.get("ready") for r in results)

    # v2.124: AGGREGATE verification на ФИНАЛЬНОМ интегрированном SHA — перепроверяем результат ЦЕЛИКОМ
    # (не только конъюнкцию per-package вердиктов), чтобы поймать межпакетные взаимодействия (каждый
    # пакет зелен по отдельности, но интеграция сломана). Сравниваем финальные проверки с БАЗОЙ (до п.1).
    # v3.0.13 (блок C): тело aggregate-верификации вынесено в _aggregate_verify (чистый вход->dict) —
    # execute_sequence перестал быть god-функцией на этом участке, поведение идентично.
    aggregate = {"verified": False}
    if executed_all and final_sha:
        aggregate = _aggregate_verify(child_root, wid, sandbox, final_sha, base_checks,
                                      sequence_base_sha, signals, reviewer_proposer, review, baseline_proven,
                                      security_reviewer_proposer=security_reviewer_proposer,
                                      strict_judge_qualified=strict_judge_qualified)
    # v3.0-rc2/rc4 (P0.4/P1.1): FAIL-CLOSED. aggregate_ready ТОЛЬКО если верификация РЕАЛЬНО выполнена
    # И чиста: verified, нет регрессий, HEAD==final_sha, evidence на final_sha, дерево чистое, агрегатный
    # security clear на полном диффе. Сбой/недоступность -> НЕ ready.
    # v3.0-rc20 (finding аудита P0): + baseline ДОКАЗАН на точной sequence_base_sha (нет fallback-базы),
    # + НЕТ base_drift (base-ветка не сдвинулась с начала цепочки) — иначе evidence против не той базы.
    agg_ok = bool(aggregate.get("verified") and aggregate.get("no_regressions")
                  and aggregate.get("baseline_proven")
                  and aggregate.get("revision_ok") and aggregate.get("tree_clean")
                  and aggregate.get("evidence_revision_ok") and aggregate.get("security_ok")
                  and aggregate.get("code_review_ok", True))
    aggregate_ready = ready_all and chain_ok and agg_ok and (base_drift is None)
    return executed_all, ready_all, aggregate, aggregate_ready


def _deliver_pr(open_pr, aggregate_ready, final_sha, child_root, wid, base, sequence_base_sha,
                task, ordered):
    from ai_ops_kit.engine import execution_pipeline as _ep
    # v2.124 (P0.4): доставка draft PR — ОТДЕЛЬНЫЙ шаг ПОСЛЕ агрегатного вердикта, на финальном
    # интегрированном SHA. PR открывается ТОЛЬКО при aggregate_ready — не по готовности отдельного пакета.
    pr, delivery = None, {"requested": bool(open_pr), "status": "not-requested" if not open_pr else None}
    if open_pr:
        if aggregate_ready and final_sha:
            wt = child_root / ".ai" / "worktrees" / wid
            _drt = wt if wt.is_dir() else child_root
            # v3.0-rc20 (finding аудита P0): DELIVERY BASE BINDING — evidence собрано против
            # sequence_base_sha; перед PR сверяем АКТУАЛЬНУЮ remote base с этой базой. Разошлась
            # (remote main сдвинулся после старта цепочки) -> НЕ открываем PR (проверенное состояние
            # != потенциальному merge-состоянию); нужна ревалидация. Иначе — «verified» PR был бы ложью.
            # v3.0.9 (finding аудита P0): ЕДИНЫЙ fail-closed RemoteBaseVerifier (как single-run). Раньше
            # sequential был fail-OPEN: remote_base=None (нет origin/сети/ветки) -> else -> открывал PR.
            # Теперь: verified-equal -> PR; verified-moved -> revalidation; unverifiable -> unavailable.
            _rv = _ep._verify_remote_base(_drt, base, sequence_base_sha)
            _vd = _rv.get("verdict")
            if _vd == "verified-equal":
                try:
                    from ai_ops_kit.delivery import pr_open
                    pr = pr_open.open_draft_pr(_drt, f"ai-ops/{wid}", base=base,
                                               title=f"ai-ops: {task[:60]}",
                                               body=(f"Sequential WorkPackages: {len(ordered)} пакет(ов). "
                                                     f"База {base} ({(sequence_base_sha or '?')[:12]}) → финал {final_sha}. "
                                                     f"Агрегатный вердикт: aggregate_ready."))
                    delivery["status"] = (pr or {}).get("status") or "failed"
                    delivery["validated_base"] = sequence_base_sha
                    delivery["base_ref"] = base
                except Exception as e:  # noqa: BLE001
                    delivery["status"] = "failed"
                    delivery["error"] = str(e)
            elif _vd == "verified-moved":
                delivery["status"] = "not-attempted"
                delivery["base_moved"] = {"base_ref": base, "validated_base": sequence_base_sha,
                                          "remote_base": _rv.get("remote_sha")}
                delivery["reason"] = ("remote base сдвинулась с момента сбора evidence — нужна ревалидация; "
                                      "PR не открыт (иначе непроверенное merge-состояние)")
            else:   # unverifiable -> доставка НЕДОСТУПНА (fail-closed), НЕ открываем PR
                delivery["status"] = "unavailable"
                delivery["reason"] = f"remote-base-unverified: {_rv.get('reason')} — доставка невозможна fail-closed"
        else:
            delivery["status"] = "not-attempted"   # последовательность не готова -> PR НЕ открываем
    return pr, delivery


def _finalize_sequence_report(wid, results, completed, stopped_at, executed_all, ready_all,
                              aggregate_ready, final_sha, chain_ok, ordered, sequence_base_sha,
                              base_drift, aggregate, delivery, pr, resume_from, features_dir):
    seq = {"schema_version": 1, "kind": "WorkPackageSequence", "workitem_id": wid,
           "packages": results, "completed": sorted(completed), "stopped_at": stopped_at,
           "executed_all": executed_all, "ready_all": ready_all, "aggregate_ready": aggregate_ready,
           "final_sha": final_sha, "sequential_chain": chain_ok, "total": len(ordered),
           "sequence_base_sha": sequence_base_sha, "base_drift": base_drift,   # rc16 (P0/P1)
           "aggregate": aggregate, "delivery": delivery, "draft_pr": pr,
           "resumed_from": resume_from}
    # v3.0.14 (finding аудита #2): sequence-report — durable (атомарно); сбой фиксируем в отчёте, не молчим
    from ai_ops_kit.shared import lifecycle_store as _ls2
    # Срез engine ратчета 2026-08-12: пропущенные записи журнала называются в ОТЧЁТЕ, а не только в
    # возврате, который никто не читал. Слив ДО durable_write — иначе на диске лёг бы отчёт, из
    # которого утрата исчезла. Слив обнуляет накопитель: та же утрата не приедет во второй отчёт.
    _ls2.merge_bookkeeping_losses(seq)
    _sr = _ls2.durable_write(features_dir / wid / "sequence-report.yaml", seq)
    if not _sr.get("ok"):
        seq["report_persist_error"] = _sr.get("error")
    return seq
