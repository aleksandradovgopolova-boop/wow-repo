#!/usr/bin/env python3
"""Отчётность прогона ai-ops run: артефакты контекста и обогащение run-report.

Вынесено из god-модуля `ai_ops_run` без изменения поведения (чистый перенос + ре-экспорт).
Здесь живут функции, собирающие артефакты контекстного слоя и наполняющие отчёт прогона
provenance-полями и контекст-секциями. Зависимости берутся из РЕАЛЬНЫХ домов (context/gates/
engine), а не из ai_ops_run — иначе получился бы циклический импорт. Немногочисленные хелперы,
оставшиеся в ai_ops_run (`_profile_for_report`), подтягиваются лениво внутри тела функции.
"""
from __future__ import annotations

import yaml


def _review_fix_context(rep):
    """v3.1.1 (fix-loop): собрать текст блокеров НЕ-ready прогона, которые ПИСАТЕЛЬ может устранить
    итерацией — провалившие детерминированные проверки (test/build/lint c output_tail) + незакрытые
    ai-review/security гейты. -> строка-контекст | None, если блок НЕ модель-фиксируемый (human-approval /
    base / lifecycle / preflight — их итерация писателя не закроет, зацикливать нельзя => fail-closed)."""
    if not isinstance(rep, dict) or rep.get("ready_for_pr"):
        return None
    ov, err = rep.get("overall_status"), (rep.get("error") or "").lower()
    # НЕ-фиксируемые классы: не зацикливаем
    if ov == "blocked-preflight" or any(w in err for w in
            ("human", "approval", "переписан", "fast-forward", "lifecycle", "повреждён", "replan", "base-")):
        return None
    unmet = (rep.get("gates") or {}).get("unmet") or []
    parts = []
    # ENV vs КОД (поле 02–03.09.2026): провал проверки из-за ОТСУТСТВИЯ инструмента (exit 127 /
    # `command not found` / `no module named`) писатель починить НЕ может — он не ставит тулчейн в
    # среду прогона. Прежде такой провал шёл писателю на итерацию как обычный блокер, и fix-цикл
    # гонял его бесконечно на нехватке ruff/pytest/pyyaml (зависание живого заезда). Пропускаем
    # env-обусловленные провалы: если они ОСТАЛИСЬ единственными, parts пуст -> None -> цикл честно
    # завершается NOT_READY (ready_for_pr это не трогает — env-квалификация и так в base_ok, ложного
    # green не возникает; гасится только бесполезный ретрай).
    from ai_ops_kit.engine.pipeline_failure import _check_has_env_symptom
    for name, chk in (rep.get("checks") or {}).items():
        if (chk or {}).get("status") == "fail":
            if _check_has_env_symptom(chk):
                continue   # среда, не код — не зацикливаем писателя на недоступном инструменте
            tail = ""
            for run in (chk.get("runs") or []):
                tail = (run.get("output_tail") or "")[-700:]
                if tail:
                    break
            parts.append(f"[проверка {name}] упала:\n{tail}".rstrip())
    # NO-VERDICT vs КОД (issue #577, поле 07.09.2026): тот же класс, что env-скип выше. Ревью, закрытое
    # `closed_as="refused"` (независимый ревьюер не вынес разбираемого вердикта / провайдер судьи отказал —
    # см. pipeline_evidence._run_reviews через evidence_from_no_verdict/evidence_from_judge_refusal), несёт
    # status fail/warn, но перезапуск ПИСАТЕЛЯ его починить НЕ может: refused — сигнал «ревьюер молчит/
    # недоступен», а не «код плохой». Прежде такая запись шла писателю как обычный блокер, и fix-loop
    # (ai_ops_run_exec) перезапускал пайплайн → снова _run_reviews → снова спавн `claude -p` (ревьюера):
    # (fix_attempts+1)×гейты×reads×медленный subprocess = часы, PR не открывался (живой заезд wow-repo).
    # Пропускаем refused: если он ОСТАЛСЯ единственным блокером, parts пуст → None → fix-loop делает break
    # → прогон честно завершается NOT_READY с уже названной причиной, БЕЗ повторного спавна ревьюера.
    # Содержательный fail/warn с реальными блокерами (closed_as "blocked"/"advisory") писателя ретраит как
    # раньше — фильтр по closed_as, а не по status, именно чтобы не убить полезный ретрай.
    for rv in (rep.get("reviews") or []):
        if rv.get("closed_as") == "refused":
            continue   # ревьюер не вынес вердикт — писатель это не чинит, не зацикливаем
        if rv.get("status") in ("fail", "warn"):
            bl = "; ".join(rv.get("blockers") or []) if rv.get("blockers") else "устрани замечания ревью"
            parts.append(f"[{rv.get('gate')}: {rv.get('status')}] {bl}")
    if "security" in unmet:
        ss = rep.get("security_scan") or {}
        doms = ", ".join(ss.get("needs_review") or ss.get("blocking") or []) or "security"
        parts.append(f"[security не закрыт] домены: {doms} — добавь валидацию входа/проверки по чек-листу")
    if not parts:
        return None
    return ("Прошлая попытка НЕ прошла ревью/проверки. Устрани КОНКРЕТНО эти блокеры (и только их, не "
            "ломая уже пройденное), затем заверши:\n\n" + "\n\n".join(parts))


def _compile_context_artifacts(signals, child_root, features_dir, fid, plan, model,
                               context_hybrid, base_binding, task_text):
    """Артефакты контекста (bundle/payload/hybrid/spec/work-package); ошибки не гаснут —
    копятся в lifecycle_errors. K6: вынесено из run() без изменения поведения."""
    # v2.107 (finding аудита): ошибки слоя контекста больше НЕ гаснут молча — фиксируем в
    # lifecycle_errors и в отчёт (критический слой не должен исчезать без следа).
    lifecycle_errors = []
    # v2.97 Context Compiler: минимальный релевантный ContextBundle для WorkItem (детерминированно).
    from ai_ops_kit.context import context_compiler
    try:
        bundle = context_compiler.compile_bundle(signals, child_root, plan=plan)
        (features_dir / fid / "context-bundle.yaml").write_text(
            yaml.safe_dump(bundle, allow_unicode=True, sort_keys=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001 — не роняем прогон, но и не молчим
        bundle = None; lifecycle_errors.append(f"context_compiler: {type(e).__name__}: {e}")
    # v2.108 Operational Context: compiled payload -> реально в prompt модели (context_prelude).
    payload = None
    try:
        payload = context_compiler.build_payload(signals, child_root, plan=plan, bundle=bundle, model=model)
        (features_dir / fid / "context-payload.yaml").write_text(
            yaml.safe_dump({k: v for k, v in payload.items() if k != "text"},
                           allow_unicode=True, sort_keys=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        payload = None; lifecycle_errors.append(f"context_payload: {type(e).__name__}: {e}")
    # v3.7.16 Live Context Hybrid FED_TO_MODEL: при --context-hybrid собираем hybrid (mandatory v1 +
    # разрешённые v2-additions через promotion gate) ДО прогона и РЕАЛЬНО подаём модели (читаем контент
    # additions из base-состояния child и дописываем к payload). v1 НИКОГДА не теряется; gate не готов
    # -> v1-only (fail-safe, additions=[]). Раньше hybrid только фиксировался в отчёте post-hoc.
    _hybrid_prelude = (payload or {}).get("text")
    _hybrid_fed = None
    if context_hybrid and payload:
        try:
            from ai_ops_kit.context import context_hybrid as _chyb
            from ai_ops_kit.context import context_engine as _ce
            _mand = None
            if bundle:
                _inc = bundle.get("included", {})
                _mand = list(_inc.get("specifications", [])) + list(_inc.get("decisions", []))
            _afp, _dcp, _bud = _ce.load_child_policies(child_root)
            _rule_refs = list((bundle.get("included", {}) or {}).get("rules", [])) if bundle else []
            _pol_refs = [p.get("id") for p in (_afp, _dcp) if isinstance(p, dict) and p.get("id")]
            _budget = _ce.budget_tokens_from(_bud)
            _base_sha = base_binding.get("base_sha")
            # v3.7.1 (#3) EXACT-SNAPSHOT: require_snapshot=True -> content читается ТОЛЬКО если child
            # РОВНО на base_sha и дерево чисто; иначе view invalid -> hybrid v1-only (не подаём
            # возможно-несоответствующий base_sha контент). Ровно exact-SHA дисциплина.
            _hyb = _chyb.build_hybrid_from_child(
                child_root, task_text, "executor", sha=_base_sha, afp=_afp, dcp=_dcp,
                v1_mandatory=_mand, rule_refs=_rule_refs, policy_refs=_pol_refs,
                budget=_budget, require_snapshot=True)
            _adds = _hyb.get("v2_additions") or []
            # не кормим модель служебными артефактами кита (features/lifecycle, .ai/) — только реальный код/доки
            _adds = [f for f in _adds if not (f.startswith("features/") or f.startswith(".ai/"))]
            _fed, _dropped = [], []
            if _hyb.get("mode") == "hybrid" and _adds:
                # v3.7.1 (#3) ПОЛНЫЙ token budget: считаем весь prompt (v1 payload + additions) против
                # hard-window; additions, не влезающие в бюджет, ДРОПАЕМ (не раздуваем hard-window).
                _base_txt = (payload or {}).get("text") or ""
                _used = len(_base_txt) // 4
                _hard = _budget if isinstance(_budget, int) and _budget > 0 else 20000
                _extra = []
                for _f in _adds:
                    _p = child_root / _f
                    if not _p.is_file():
                        continue
                    _c = _p.read_text(encoding="utf-8", errors="replace")[:8000]
                    _t = len(_c) // 4
                    if _used + _t > _hard:
                        _dropped.append(_f); continue
                    _used += _t; _fed.append(_f); _extra.append(f"### {_f}\n{_c}")
                if _extra:
                    _hybrid_prelude = _base_txt + "\n\n## Hybrid v2-additions (fed_to_model)\n" + "\n\n".join(_extra)
            _hybrid_fed = {"kind": "ContextHybrid", "mode": _hyb.get("mode"),
                           "v2_additions": _adds, "fed_additions": _fed, "dropped_over_budget": _dropped,
                           "fed_to_model": bool(_hyb.get("mode") == "hybrid" and _fed),
                           "prompt_tokens_est": (len(_hybrid_prelude or "") // 4), "hard_window": (_budget or 20000),
                           "exact_snapshot": True,
                           "mandatory_references": _hyb.get("mandatory_references"),
                           "promotion_ready": _hyb.get("promotion_ready"), "base_sha": _base_sha}
        except Exception as _e:  # noqa: BLE001 — hybrid feed не должен ронять прогон
            _hybrid_fed = {"kind": "ContextHybrid", "error": f"hybrid feed failed: {type(_e).__name__}: {_e}"[:300],
                           "fed_to_model": False}
    # v2.98 Adaptive Spec-First: уровень спецификации (L0..L3) по сигналам + эскалация по риску.
    from ai_ops_kit.gates import spec_levels
    try:
        # v2.110 Real Spec-First: coverage из РЕАЛЬНЫХ артефактов (features/<fid>/spec.yaml +
        # засчёт requirements/plan/openspec), а не из сигналов с пустым provided.
        _wt_pre = child_root / ".ai" / "worktrees" / fid
        spec_cov = spec_levels.assess_from_artifacts(
            signals, child_root, fid, work_root=(_wt_pre if _wt_pre.is_dir() else None))
        (features_dir / fid / "spec-coverage.yaml").write_text(
            yaml.safe_dump(spec_cov, allow_unicode=True, sort_keys=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        spec_cov = None; lifecycle_errors.append(f"spec_levels: {type(e).__name__}: {e}")
    # v2.100 Atomic Planning: оценка размера пакета + нужна ли декомпозиция по контекстному бюджету.
    from ai_ops_kit.engine import atomic_planner
    try:
        # v2.111: decompose — при необходимости строит КОНКРЕТНЫЕ WorkPackages (не только оси).
        work_pkg = atomic_planner.decompose(signals, wid=fid, child_root=child_root, bundle=bundle)
        (features_dir / fid / "work-package.yaml").write_text(
            yaml.safe_dump(work_pkg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        work_pkg = None; lifecycle_errors.append(f"atomic_planner: {type(e).__name__}: {e}")
    return (lifecycle_errors, bundle, payload, _hybrid_prelude, _hybrid_fed,
            spec_cov, work_pkg)


def _add_context_reports(rep, *, bundle, payload, spec_cov, work_pkg, context_shadow,
                         context_hybrid, hybrid_fed, child_root, task_text, fid):
    """Контекст-отчёты в rep (bundle/shadow/hybrid/payload/spec/work-package); всё guarded.
    K6: вынесено из run() без изменения поведения; мутирует rep на месте."""
    if bundle:
        rep["context_bundle"] = {"estimated_tokens": bundle["estimated_tokens"],
                                 "context_budget": bundle["context_budget"],
                                 "overflow": bundle["overflow"],
                                 "agents": bundle["included"]["agents"],
                                 "rules": bundle["included"]["rules"],
                                 "excluded_count": len(bundle["excluded"])}
    # v3.6.4 SHADOW-wiring (по умолчанию OFF): Context Engine v2 shadow-view РЯДОМ с боевым v1.
    # Execution по-прежнему на v1 (context_compiler); shadow — чистая наблюдаемость перед
    # промоушеном retrieval в runtime. Полностью guarded: сбой shadow не влияет на прогон.
    if context_shadow:
        try:
            from ai_ops_kit.context import context_shadow as _cshadow
            # v3.6.7d: содержимое читаем из ТОЧНОГО execution-worktree (HEAD==committed_sha,
            # require_snapshot доказывает это), политики — из основного checkout (.ai/policies).
            # Обязательный контекст v1 (spec/decisions) берём из реального ContextBundle и передаём
            # в orchestrator — иначе инвариант «mandatory не потерян» не проверяется. Демо-политик
            # в runtime нет (afp=None -> child-политики / deny-by-default). Execution по-прежнему v1.
            _wt = child_root / ".ai" / "worktrees" / fid
            _content_root = _wt if _wt.is_dir() else child_root
            _mandatory = None
            if bundle:
                _inc = bundle.get("included", {})
                _mandatory = list(_inc.get("specifications", [])) + list(_inc.get("decisions", []))
            _csha = (rep.get("commit") or {}).get("sha")   # v3.6.7d-fix: SHA в rep["commit"]["sha"]
            rep["context_shadow"] = _cshadow.build_shadow(
                _content_root, task_text, role="executor", sha=_csha,
                policy_root=child_root, v1_mandatory=_mandatory, require_snapshot=True)
        except Exception as _e:  # noqa: BLE001 — shadow не должен ронять прогон
            # честно фиксируем реальную причину (не влияет на execution=v1) — иначе баг wiring немой
            rep["context_shadow"] = {"error": f"shadow build failed: {type(_e).__name__}: {_e}"[:300]}
    # v3.7.16: hybrid собран ДО прогона и РЕАЛЬНО подан модели (см. hybrid_fed выше). Записываем что
    # именно было fed_to_model (mode/additions/references), а не пересобираем post-hoc. v1 не теряется.
    if context_hybrid and hybrid_fed is not None:
        rep["context_hybrid"] = hybrid_fed
    if payload:
        rep["context_payload"] = {"payload_tokens": payload["payload_tokens"],
                                  "payload_budget": payload["payload_budget"],
                                  "context_budget": payload["context_budget"],
                                  "included_items": len(payload["included_items"]),
                                  "excluded_for_budget": len(payload["excluded_for_budget"]),
                                  "fed_to_model": bool(payload.get("text"))}
    if spec_cov:
        rep["spec_coverage"] = {"level": spec_cov["level"], "level_name": spec_cov["level_name"],
                                "escalated_from": spec_cov["escalated_from"],
                                "blocking_missing": spec_cov["blocking_missing"],
                                "needs_human": spec_cov["needs_human"],
                                # v2.110: реальность — есть ли явный spec.yaml и что засчитано из артефактов
                                "spec_artifact": spec_cov.get("spec_artifact", False),
                                "covered_sections": spec_cov.get("covered_sections", []),
                                "provided_sources": spec_cov.get("provided_sources", {})}
    if work_pkg:
        rep["work_package"] = {"atomic": work_pkg["atomic"],
                               "should_decompose": work_pkg["should_decompose"],
                               "decomposition_axes": work_pkg["decomposition_axes"],
                               "decomposition_reasons": work_pkg["decomposition_reasons"],
                               # v2.111: конкретные пакеты (id/scope/deps) + основная ось
                               "primary_axis": work_pkg.get("primary_axis"),
                               "work_packages": work_pkg.get("work_packages", [])}


def _enrich_run_report(rep, *, runtime, provider_name, provider_resolution, child_root,
                       base_binding, model_resolution, writer_model, model, pretruth,
                       resume, pf, force_resume, fid, bundle, payload, spec_cov, work_pkg,
                       preflight):
    """Provenance-поля отчёта (runtime/provider/model/base/preflight/resume/lifecycle-dict).
    K6: вынесено из run() без изменения поведения; мутирует rep на месте."""
    # _profile_for_report остаётся в ai_ops_run (используется и forbidden-функциями/тестами) —
    # тянем лениво, чтобы не замкнуть импорт ai_ops_run <-> ai_ops_run_reporting.
    from ai_ops_kit.engine.ai_ops_run import _profile_for_report
    rep["runtime"] = runtime
    rep["engine"] = "pipeline"
    # #702-work: отчёт несёт свой child_root, чтобы печать для человека выбрала аудиторию по
    # ЕГО communication-policy, а не по CWD процесса. Без этого поля печать остаётся на technical
    # (синтетические отчёты тестов и минимальные отчёты честно видят полную стену).
    rep["child_root"] = str(child_root)
    rep["provider"] = provider_name
    # P0-1 side-effect proof: КАК выбран провайдер — в отчёте (и в run-report.json на диске),
    # а не только в stdout: иначе решение резолва невозможно проверить постфактум.
    if provider_resolution:
        rep["provider_resolution"] = dict(provider_resolution)
    # P1-3: обогащаем профиль движка (там stacks — только языки) человекочитаемым display
    rep["profile"] = _profile_for_report(child_root, rep.get("profile"))
    # F-014: в отчёт кладём базу, выбранную резолвером ПРОГОНА. Движок резолвит повторно, но
    # получает уже конкретную ветку и потому всегда рапортует source=explicit-* — по такому
    # отчёту не отличить «человек задал --base» от «кит выбрал сам».
    if isinstance(base_binding, dict) and base_binding.get("base_ref"):
        rep["base_binding"] = {k: v for k, v in base_binding.items() if k != "kind"}
    # v3.8.3-rc3: финализировать model_attempts (исход последней попытки) + честные initial/effective_model.
    if isinstance(model_resolution, dict) and model_resolution.get("model_attempts"):
        _last = model_resolution["model_attempts"][-1]
        if _last.get("outcome") == "pending":
            _last["outcome"] = ("verified" if rep.get("ready_for_pr")
                                else "not_ready:" + ",".join((rep.get("gates") or {}).get("unmet") or []))
    _eff = model_resolution.get("effective_model") if isinstance(model_resolution, dict) else None
    # v3.7.12/rc3: model = РЕАЛЬНО завершившая модель (effective), не только первоначальная.
    rep["model"] = _eff or (writer_model if (isinstance(model_resolution, dict) and model_resolution.get("applied")) else model)
    if isinstance(model_resolution, dict) and model_resolution.get("applied"):
        rep["initial_model"] = model_resolution.get("initial_model")
        rep["effective_model"] = model_resolution.get("effective_model")
        if model_resolution.get("escalation_error"):
            rep["escalation_error"] = model_resolution["escalation_error"]
    rep["model_resolution"] = model_resolution   # per-role решение роутера (видимость в каждом отчёте)
    rep["preflight"] = pretruth   # v2.115: preflight пройден (для наблюдаемости в отчёте)
    # #637: пост-фактум «работа стоила $X вместо ожидаемых $Y». Оценка до прогона живёт в preflight
    # (checks["economic_budget"]), факт — в rep["cost"]; сводим их с дельтой. Честно: нет оценки или
    # факта -> unavailable, не 0. Дельта не считается за факт поверх неполных данных.
    _eb = ((pretruth or {}).get("checks") or {}).get("economic_budget") if isinstance(pretruth, dict) else None
    if _eb is not None:
        from ai_ops_kit.providers import cost_account as _ca
        rep["cost_delta"] = _ca.estimate_vs_actual(_eb, rep.get("cost"))
    # v2.119: заметка «живой предложитель (swap провайдера)» уместна только для mock-прогона —
    # на живом провайдере она вводит в заблуждение (предложитель УЖЕ живой). Честный отчёт.
    if provider_name and provider_name != "mock" and isinstance(rep.get("not_yet"), list):
        rep["not_yet"] = [n for n in rep["not_yet"] if "живой предложитель" not in n]
    # v2.109 Real Resume: если продолжали — честно фиксируем в отчёте preflight-контекст (в т.ч.
    # что ревалидация требовалась и была осознанно переопределена --force), не только факт reuse.
    if resume and isinstance(rep.get("resume"), dict):
        rep["resume"]["preflight_reasons"] = pf["reasons"]
        rep["resume"]["revalidation_needed"] = pf["revalidation_needed"]
        rep["resume"]["revalidation_overridden"] = bool(pf["revalidation_needed"] and force_resume)
    # v2.94: единая транзакция — фиксируем lifecycle-артефакты в отчёте и на диске
    rep["lifecycle"] = {
        "workitem": f"features/{fid}/workitem.yaml",
        "run_plan": f"features/{fid}/run-plan.yaml",
        "context_bundle": (f"features/{fid}/context-bundle.yaml" if bundle else None),
        "context_payload": (f"features/{fid}/context-payload.yaml" if payload else None),
        "spec_coverage": (f"features/{fid}/spec-coverage.yaml" if spec_cov else None),
        "work_package": (f"features/{fid}/work-package.yaml" if work_pkg else None),
        "active_work": ".ai/runtime/active-work.yaml",
        "run_report": f"features/{fid}/run-report.json",
        "run_handoff": f"features/{fid}/run-handoff.yaml",
        "concurrency_preflight": preflight,
    }
