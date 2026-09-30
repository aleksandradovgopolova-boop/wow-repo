#!/usr/bin/env python3
"""ai-ops run — единый контроллер задачи (v2.34, Execution Engine Фаза 2, срез 1).

Собирает разрозненные шаги в ОДНУ транзакцию: классификация/маршрут → RunPlan
(base_workflow + треки + агрегированные гейты) → WorkItem → регистрация в реестре
активных работ → исполнение → компактный отчёт. Раньше это были отдельные инструменты;
теперь — один вход, как обещает продукт.

Граница исполнения (честно, без переоценки):
- **claude-code и другие рантаймы с собственным tool loop**: контроллер готовит план и
  каркас состояния (RunPlan, WorkItem, active-work, TaskState), а стадии/патчи/тесты
  исполняет сам рантайм, следуя плану. status = `planned`. Кит не притворяется, что
  исполнил за рантайм.
- **generic-orchestrator** (наш sequential-движок): контроллер реально прогоняет стадии
  и гейты (ai_ops_kit/providers/orchestrator.py) — status = done|blocked по evidence.

Аддитивно (2.x): ничего не ломает; `ai-ops run` как ОСНОВНОЙ путь и сплит на пакеты —
цель 3.0.

Использование:
  ai_ops_run.py run "<задача>" <child_root> [--signals '<json>'] [--features-dir dir]
       [--runtime claude-code|generic-orchestrator] [--provider mock] [--model ID]
       [--engine pipeline|controller] [--execute] [--open-pr] [--json]  # pipeline — по умолчанию
  ai_ops_run.py --selftest
Код возврата: 0 — успех/ready; 1 — blocked или pipeline не готов к PR; 2 — ошибка прогона.
"""
from __future__ import annotations

# v4: самодостаточный вход — файл можно запустить напрямую (без PYTHONPATH). Кладём корень пакета
# (маркер VERSION) в sys.path ДО пакетных импортов — раньше это делал плоский shim tools/ через
# _bootstrap; теперь точка входа сама себя обслуживает.
import sys as _sys
from pathlib import Path as _P_bootstrap
_root = next((_p for _p in _P_bootstrap(__file__).resolve().parents if (_p / "VERSION").is_file()), None)
if _root is not None and str(_root) not in _sys.path:
    _sys.path.insert(0, str(_root))

import json
import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402
from ai_ops_kit.engine import run_plan          # noqa: E402
from ai_ops_kit.engine.run_context import RunContext   # noqa: E402
from ai_ops_kit.engine.pipeline_helpers import work_produced, delivery_pending, _stacks_human   # noqa: E402
# Печать результата прогона вынесена в отдельный модуль (god-модуль ai_ops_run разрежается);
# ре-экспорт держит внешние вызовы (cli/ai_ops_cli, тесты) на прежних именах.
from ai_ops_kit.engine.ai_ops_run_print import _print_pipeline, _print_contour_consistency, print_human  # noqa: F401,E402
# Отчётность и жизненный цикл прогона вынесены в модули-спутники (тот же приём, что и print);
# ре-экспорт держит вызовы run() и тесты (ai_ops_run.<name>) на прежних именах без изменения поведения.
from ai_ops_kit.engine.ai_ops_run_reporting import (   # noqa: F401,E402
    _review_fix_context, _compile_context_artifacts, _add_context_reports, _enrich_run_report)
from ai_ops_kit.engine.ai_ops_run_lifecycle import (   # noqa: F401,E402
    _commit_barrier, _start_lifecycle, _resume_gate, _finalize_run_cost, _finalize_run, _deliver,
    # пробируемые delivery/resume/provider-хелперы вынесены туда же (чистый перенос + ре-экспорт):
    # run()/main() и тесты (`ai_ops_run.<name>`) продолжают резолвить их из этого модуля.
    live_provider_refusal, _reconcile_pending_delivery, _register_active_work,
    _restore_resume_policy, _resolve_models)
from ai_ops_kit.lifecycle import workitem          # noqa: E402
from ai_ops_kit.lifecycle import active_work
from ai_ops_kit.shared import work_areas as _work_areas       # noqa: E402
from ai_ops_kit.shared import lifecycle_store as _ls   # noqa: E402 — v3.0.12: durable запись/fail-closed чтение resume-артефактов


# Проб-свободные run-хелперы вынесены в модуль-спутник ai_ops_run_exec (чистый перенос +
# ре-экспорт) — тот же приём, что print/reporting/lifecycle. Ре-экспорт держит вызовы
# `ai_ops_run.<name>` (CLI, тесты, спутники) и патчабельность (`patch("ai_ops_run._provider_trust")`)
# на прежних именах. Здесь остались только пробируемые точки (`main`, `_run_controller_path`) и
# публичный вход `run`. Пробируемые delivery/resume/provider-хелперы — в ai_ops_run_lifecycle.
from ai_ops_kit.engine.ai_ops_run_exec import (   # noqa: F401,E402
    _note_bookkeeping_error, _outbox_dir, resolve_provider_for_run, _SERVICE_TASK_MARKERS,
    is_service_text, product_task_for_resume, _profile_for_report, _unresolved_intents,
    _nonfinal_receipt_intents, _resume_context_from_handoff, _with_provider_fallback,
    _load_klp_by_env, _provider_trust, _execute_with_fix_loop, _resolve_run_base,
    _build_run_proposers, _run_preflight, exit_code, _build_run_arg_parser)


def _run_controller_path(task_text, signals, child_root, features_dir, feature,
                         runtime, provider_name, execute, session, write_scope):
    """controller/planning-путь (engine=controller): RunPlan + каркас состояния, БЕЗ внешней доставки;
    стадии исполняет рантайм/generic-orchestrator, execution+delivery-гарантии — только pipeline."""
    # 1-2. RunPlan (route + треки + гейты). feature (v2.51): привязка WorkItem к ИМЕНОВАННОЙ фиче —
    # иначе wid=wi-<hash>, и срезы истории падают на новую фичу с 1 срезом (finding обкатки 5).
    plan = run_plan.build_plan(signals, workitem_id=feature)
    fid = plan["workitem_id"]
    base_wf = plan["base_workflow"]

    # 3. WorkItem
    workitem.start(str(features_dir), fid, task_text,
                   task_type=signals.get("task_type"), risk=signals.get("risk"))

    # 4. RunPlan на диск — v3.0.16 Phase A (аудит #3): барьер, сбой durable-записи -> 0 исполнения.
    _pw2 = _ls.durable_write(features_dir / fid / "run-plan.yaml", plan)
    if not _pw2.get("ok"):
        return {"schema_version": 1, "kind": "run-report", "workitem_id": fid, "status": "error",
                "error": f"lifecycle fail-closed: не удалось надёжно сохранить RunPlan ({_pw2.get('error')})"}

    # 5. регистрация активной работы (координация параллельных сессий)
    aw_path = child_root / ".ai" / "runtime" / "active-work.yaml"
    areas = _work_areas.areas_for(signals, write_scope)   # #138: вывод, а не заглушка (см. work_areas)
    _reg_rc2 = active_work.register(aw_path, fid, f"feature/{fid}", areas, session,
                                    workitem=f"features/{fid}/workitem.yaml",
                                    child_root=child_root,
                                    published=active_work.publication_enabled(child_root))
    if _reg_rc2:
        # Тот же отказ на планирующем пути: он тоже занимает ветку и заводит артефакты работы.
        return {"schema_version": 1, "kind": "run-report", "workitem_id": fid, "status": "blocked",
                "blocked_by": "active-work",
                "error": ("работа не начата: заявку на эту работу или ветку держит другая сессия "
                          "(причина и держатель названы выше).")}

    # 6. исполнение
    status, run_state = "planned", f".ai/runtime/workitems/{fid}/TaskState.yaml"
    run_state_materialized = False   # честно: в planned run_state — обещание пути, не файл
    if execute or runtime == "generic-orchestrator":
        from ai_ops_kit.providers import orchestrator
        st, run_dir = orchestrator.run_workflow(
            base_wf, task_text, child_root,
            provider=orchestrator.make_provider(provider_name),
            provider_name=provider_name, verbose=False, workitem_id=fid,
            budget=plan.get("execution_budget"),   # v2.38: потолок вызовов из RunPlan
            gate_ids=plan.get("gates"),            # v2.54: прогон оценивает ГЕЙТЫ RUNPLAN (base+треки)
            signals=signals)                       # v2.55: условный human_approval по сигналам задачи
        status = st["status"]
        run_state = str(Path(run_dir) / "TaskState.yaml")
        run_state_materialized = True

    # 7. компактный отчёт
    report = {
        "schema_version": 1, "kind": "run-report",
        "workitem_id": fid, "base_workflow": base_wf,
        "required_tracks": [t["track"] for t in plan["required_tracks"]],
        "conditional_tracks": [t["track"] for t in plan["conditional_tracks"]],
        "skipped_tracks": [{"track": t["track"], "reason": t["reason"]} for t in plan["skipped_tracks"]],
        "gates": plan["gates"],
        "runtime": runtime, "execution": "orchestrated" if (execute or runtime == "generic-orchestrator") else "planned",
        "status": status, "run_state": run_state,
        # честно: в planned run_state — ОБЕЩАНИЕ пути; папку workitems/<id>/ создаёт рантайм при
        # реальном исполнении, не контроллер — не полагаться на её наличие (finding обкатки v2.34).
        "run_state_materialized": run_state_materialized,
        "artifacts": {"workitem": f"features/{fid}/workitem.yaml",
                      "run_plan": f"features/{fid}/run-plan.yaml"},
        # v3.0.16 Phase A (finding аудита #3): planning/orchestration-путь; ВНЕШНЯЯ ДОСТАВКА (PR) НЕ
        # выполняется здесь — execution+delivery-гарантии ТОЛЬКО в pipeline (engine=pipeline).
        "delivery": {"requested": False, "status": "not-applicable",
                     "reason": "controller/planning путь: внешняя доставка не выполняется; "
                               "execution+delivery-гарантии — только engine=pipeline"},
    }
    # report — write barrier: сбой durable-записи фиксируем в отчёте (не молча)
    _rw2 = _ls.durable_write_json(features_dir / fid / "run-report.json", report)
    if not _rw2.get("ok"):
        report["lifecycle_errors"] = [f"run-report durable-write: {_rw2.get('error')}"]
    return report


def run(task_text, signals, child_root: Path, features_dir=None,
        runtime="claude-code", provider_name="mock", session="cli", execute=False,
        feature=None, engine="pipeline", proposer=None, open_pr=False, model=None,
        baseline_diff=False, require_fix=False, max_steps=40, discard_previous=False,
        sandbox=False, review=False, reviewer_proposer=None, takeover=False, takeover_reason=None,
        author=False, author_proposer=None, install_deps=True,
        resume=False, force_resume=False, base=None, write_scope=None, replan=False,
        review_fix_attempts=0, calibrated_enforcement=True, ui_evidence=None,
        context_shadow=False, context_hybrid=False, reevaluate_only=False,
        progressive_escalation=False, provider_resolution=None):
    signals = dict(signals or {})
    signals.setdefault("task_text", task_text)
    child_root = Path(child_root)
    features_dir = Path(features_dir) if features_dir else child_root / "features"

    # engine=pipeline (v2.63): собранный единый движок как РЕАЛЬНЫЙ путь из контроллера — весь
    # прогон делегируется в execution_pipeline.run_pipeline; proposer — из провайдера (или передан).
    if engine == "pipeline":
        from ai_ops_kit.providers import orchestrator   # нужен ниже для _finalize_run_cost
        # v3.0-rc2/rc4 (P0.1) Canonical Resume Context + immutable-resume -> _restore_resume_policy
        # (K6). Блок мутирует ctx; run() синхронизирует изменённые policy-поля обратно в локалы
        # (downstream читает локалы). Поведение сохранено: restore/fail-closed/immutable-drift/F-027.
        ctx = RunContext.from_run_args(
            task_text=task_text, signals=signals, child_root=child_root, features_dir=features_dir,
            feature=feature, provider_name=provider_name, model=model, runtime=runtime,
            sandbox=sandbox, baseline_diff=baseline_diff, require_fix=require_fix, author=author,
            review=review, open_pr=open_pr, write_scope=write_scope, max_steps=max_steps,
            base=base, replan=replan)
        _rrerr = _restore_resume_policy(ctx, resume)
        if _rrerr:
            return _rrerr
        signals, task_text, _saved_task = ctx.signals, ctx.task_text, ctx.saved_task
        sandbox, baseline_diff, require_fix = ctx.sandbox, ctx.baseline_diff, ctx.require_fix
        author, review, open_pr = ctx.author, ctx.review, ctx.open_pr
        write_scope, max_steps, base = ctx.write_scope, ctx.max_steps, ctx.base
        # base -> конкретная ветка + полный BaseBinding (0 model calls на явной несуществующей базе).
        base, base_binding, _brerr = _resolve_run_base(ctx, base)
        if _brerr is not None:
            return _brerr
        # v3.7.12 Router->runtime + JIT-trust + complexity-aware + provider-fallback -> _resolve_models
        # (K6). Мутирует ctx; preflight PRIMARY не пройден -> blocked-preflight (fail-closed).
        _mrerr = _resolve_models(ctx)
        if _mrerr:
            return _mrerr
        # writer_model/model_resolution нужны отчёту/финализации; остальной routing/trust держит ctx.
        _writer_model, _model_resolution = ctx.writer_model, ctx.model_resolution

        _uctx = _build_run_proposers(ctx, proposer, reviewer_proposer, author_proposer)   # writer ≠ judge

        # v2.94 (One Run Transaction, аудит #2): pipeline НЕ обходит lifecycle — один план строится здесь
        # и передаётся в движок; WorkItem/RunPlan/active-work/run-report как в controller-пути.
        plan = run_plan.build_plan(signals, workitem_id=feature)
        fid = plan["workitem_id"]

        # v3.0.16 Phase A (аудит #2): реконсиляция незавершённой доставки прошлого прогона ДО новой
        # работы (DeliveryIntent outcome_unknown -> сверка с remote + DeliveryReceipt). Best-effort.
        try:
            _rec = _reconcile_pending_delivery(features_dir, fid, child_root)
        except Exception:  # noqa: BLE001
            _rec = None

        # v2.109 Real Resume: продолжить WorkItem поверх подтверждённой работы (не заново), ДО изменения
        # состояния — честный ранний выход ничего не оставит. resume-preflight -> _resume_gate (K6).
        pf, resume_ctx, _rerr = _resume_gate(child_root, fid, base, force_resume, resume)
        if _rerr:
            return _rerr

        # durable lifecycle-start (workitem/RunPlan/run-settings/journal) -> _start_lifecycle (K6).
        _attempt_id, _lcerr = _start_lifecycle(
            features_dir, fid, task_text, signals, plan, engine, base, resume, execute,
            _saved_task, sandbox, baseline_diff, require_fix, author, review, open_pr,
            write_scope, max_steps, base_binding)
        if _lcerr:
            return _lcerr
        (lifecycle_errors, bundle, payload, _hybrid_prelude, _hybrid_fed,   # артефакты контекста
         spec_cov, work_pkg) = _compile_context_artifacts(
            signals, child_root, features_dir, fid, plan, model,
            context_hybrid, base_binding, task_text)
        # v2.115 Preflight Truth: spec/атомарность/overflow/approvals/lifecycle ДО запуска модели
        # (Spec-First блокирует РЕАЛИЗАЦИЮ, а не только доставку) -> _run_preflight.
        pretruth, _blocked = _run_preflight(ctx, fid, plan, bundle, payload, spec_cov, work_pkg,
                                            lifecycle_errors, reevaluate_only, provider_resolution)
        if _blocked is not None:
            return _blocked

        aw_path, preflight, _awerr = _register_active_work(   # active-work + concurrency-preflight
            child_root, signals, write_scope, fid, session, lifecycle_errors,
            takeover=takeover, takeover_reason=takeover_reason)
        if _awerr:
            return _awerr

        # v2.107/v3.1.8/3.1.9: калиброванное UI-enforcement + fix-loop с quality-эскалацией writer'а ->
        # _execute_with_fix_loop (K6). (rep, terminal_error): terminal_error != None -> ранний error-отчёт.
        _calib = bool(calibrated_enforcement)
        rep, _exerr = _execute_with_fix_loop(
            ctx, _uctx, execute=execute, plan=plan, discard_previous=discard_previous,
            install_deps=install_deps, hybrid_prelude=_hybrid_prelude, calib=_calib,
            ui_evidence=ui_evidence, reevaluate_only=reevaluate_only, resume=resume,
            resume_ctx=resume_ctx, attempt_id=_attempt_id, fid=fid, aw_path=aw_path,
            review_fix_attempts=review_fix_attempts, reviewer_proposer=reviewer_proposer,
            author_proposer=author_proposer)
        if _exerr is not None:
            return _exerr
        _enrich_run_report(rep, runtime=runtime, provider_name=provider_name,
                           provider_resolution=provider_resolution, child_root=child_root,
                           base_binding=base_binding, model_resolution=_model_resolution,
                           writer_model=_writer_model, model=model, pretruth=pretruth,
                           resume=resume, pf=(pf if resume else None), force_resume=force_resume, fid=fid,
                           bundle=bundle, payload=payload, spec_cov=spec_cov,
                           work_pkg=work_pkg, preflight=preflight)
        _add_context_reports(rep, bundle=bundle, payload=payload, spec_cov=spec_cov,
                             work_pkg=work_pkg, context_shadow=context_shadow,
                             context_hybrid=context_hybrid, hybrid_fed=_hybrid_fed,
                             child_root=child_root, task_text=task_text, fid=fid)
        # v3.0.12/v3.0.15 (аудит блок B/P0): ТРАНЗАКЦИОННЫЙ COMMIT BARRIER — доставка (PR) ТОЛЬКО ПОСЛЕ
        # durable-фиксации доказательств/состояния. Порядок: verification -> durable RunHandoff ->
        # durable final report -> journal checkpoint -> delivery -> durable delivery result -> run_end.
        # Не зафиксированы durable -> доставка НЕ выполняется (fail-closed). -> _commit_barrier (K6).
        _jname, _handoff_ok, _report_ok, _plan = _commit_barrier(
            rep, child_root, features_dir, fid, lifecycle_errors)
        # DELIVERY за commit-барьером (governance-gate -> DeliveryIntent -> DeliveryReceipt,
        # outcome_unknown/fail-closed) -> _deliver (K6). Мутирует rep на месте.
        _deliver(ctx, rep, plan=_plan, handoff_ok=_handoff_ok, report_ok=_report_ok,
                 jname=_jname, fid=fid)
        # агрегат стоимости + usage-ledger + очистка call-context; затем run_completed/run_end/статус.
        _finalize_run_cost(rep, orchestrator, model, _jname, fid, _attempt_id, signals,
                           _plan, _model_resolution, child_root)
        return _finalize_run(rep, fid, child_root, _jname, _attempt_id, aw_path)

    # engine=controller: RunPlan + каркас состояния (без внешней доставки) -> _run_controller_path.
    return _run_controller_path(task_text, signals, child_root, features_dir, feature,
                                runtime, provider_name, execute, session, write_scope)


def main(argv):
    ap = _build_run_arg_parser()
    a = ap.parse_args(argv)
    if a.cmd == "resume":
        from ai_ops_kit.engine import run_handoff
        # #406: флаги, которые ПРИМЕНЯЮТСЯ только при реальном продолжении (--execute), не должны
        # молча проглатываться на preflight. Раньше `resume --open-pr` без --execute печатал прогноз
        # готовности и выходил, НИ РАЗУ не взглянув на флаг — человек уходил в уверенности, что PR
        # откроется/заявка перенимается, а не происходило ничего. Тихо проигнорированный флаг = ложь
        # об исходе; здесь он становится явным отказом с понятной причиной.
        _exec_only = []
        if getattr(a, "open_pr", False):
            _exec_only.append("--open-pr")
        if getattr(a, "takeover", False):
            _exec_only.append("--takeover")
        if _exec_only and not a.execute:
            _msg = ("флаги " + ", ".join(_exec_only) + " действуют только при реальном продолжении "
                    "работы — добавьте --execute. Без него resume лишь проверяет, можно ли "
                    "продолжить, и ни открыть PR, ни перенять заявку не может (флаг был бы "
                    "проигнорирован молча).")
            if a.json:
                print(json.dumps({"kind": "resume", "status": "error", "error": _msg,
                                  "resume": {"requested": True, "resumed": False}},
                                 ensure_ascii=False, indent=2))
            else:
                print(f"ai-ops resume {a.feature}: ОТКАЗ — {_msg}")
            return 2
        pf = run_handoff.resume_preflight(a.child_root, a.feature, base=a.base)
        if not a.execute:
            if a.json:
                print(json.dumps(pf, ensure_ascii=False, indent=2))
            else:
                print(f"ai-ops resume {a.feature}: can_resume={pf['can_resume']} · "
                      f"revalidation_needed={pf.get('revalidation_needed')}")
                for r_ in pf["reasons"]:
                    print(f"  · {r_}")
                if pf.get("next_action"):
                    print(f"  следующий шаг: {pf['next_action']}")
                if pf["can_resume"]:
                    reval = pf.get("revalidation_needed")
                    # Подсказка обязана быть исполнимой ТЕМ ЖЕ `./ai-ops`, которым человек сюда попал
                    # (живой прогон на child, 2026-08-14): форма `resume <root> <feature>` разбиралась
                    # intent-CLI как task="." -> workitem_id "." -> ValueError со стеком в лицо.
                    print(f"  продолжить: ai-ops resume {a.child_root} --feature {a.feature} --execute"
                          f"{' --force' if reval else ''}   (worktree/ветка переиспользуются; "
                          f"{'нужна ревалидация -> --force' if reval else 'база актуальна'})")
            return 0 if pf["can_resume"] else 1
        # РЕАЛЬНОЕ продолжение (v2.109)
        # F-027: задачей продолжения берём ПРОДУКТОВУЮ задачу исходного прогона, а не next_action
        # кита. next_action остаётся контекстом («что осталось») и печатается человеку — но задачей
        # исполнителя он не становится ни на одном заходе.
        _pt = product_task_for_resume(a.child_root, a.feature)
        task = a.task or _pt["task"]
        if not task:
            _err = ("нечего продолжать как продуктовую задачу: исходная задача не найдена "
                    "(ни task в run-settings, ни задача в workitem.yaml, ни раздел goal в спеке). "
                    "Назовите её явно: --task \"<что делаем для продукта>\".")
            if a.json:
                print(json.dumps({"kind": "resume", "status": "error", "error": _err,
                                  "resume": {"requested": True, "resumed": False}},
                                 ensure_ascii=False, indent=2))
            else:
                print(f"ai-ops resume {a.feature}: ОТКАЗ — {_err}")
            return 2
        # F-026: провайдер выбирается ТЕМ ЖЕ автовыбором, что у `run --execute`, и решение печатается
        # ДО прогона. Раньше resume молча уходил в mock: модель не вызывалась, правок ноль, а отчёт
        # говорил resumed=True — увидеть подмену можно было только в --json.
        _pres = resolve_provider_for_run(a.provider, Path(a.child_root), execute=True, quiet=a.json)
        _refusal = live_provider_refusal(_pres, a.provider)
        if _refusal:
            if a.json:
                print(json.dumps({"kind": "resume", "status": "error",
                                  "error": f"resume --execute: {_refusal}",
                                  "provider_resolution": _pres,
                                  "resume": {"requested": True, "resumed": False}},
                                 ensure_ascii=False, indent=2))
            else:
                print(f"ai-ops resume {a.feature}: ОТКАЗ — {_refusal}")
            return 2
        report = run(task, json.loads(a.signals), Path(a.child_root),
                     provider_name=_pres["provider"], model=a.model, engine="pipeline",
                     # #695: та же измеренная личность, что и у run (intent-CLI подаёт `--session`) —
                     # иначе resume регистрируется под "cli" и не узнаёт свою же работу.
                     session=getattr(a, "session", "cli"),
                     execute=True, feature=a.feature, resume=True, force_resume=a.force, base=a.base,
                     replan=a.replan, open_pr=getattr(a, "open_pr", False),
                     # #403/deliver-only: без этого проброса флаг, объявленный подкомандой resume и
                     # прокинутый intent-CLI из `--deliver-only`, доходил до argparse, но НЕ до движка —
                     # resume всё равно переавторил и плодил коммит вместо доставки готового READY.
                     reevaluate_only=getattr(a, "reevaluate_only", False),
                     takeover=getattr(a, "takeover", False),
                     takeover_reason=getattr(a, "takeover_reason", None),
                     provider_resolution={k: _pres.get(k) for k in
                                          ("provider", "source", "reason", "warning")})
        rinfo = report.get("resume") or {}
        if a.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"ai-ops resume {a.feature}: status={report.get('status') or report.get('overall_status')} · "
                  f"resumed={rinfo.get('resumed')} · reused_branch={rinfo.get('reused_branch')} · "
                  f"провайдер={_pres['provider']}")
            # F-026/F-027: чем продолжали и откуда взята задача — видно БЕЗ --json.
            print(f"  задача: {task}  (источник: {'--task' if a.task else _pt['source']})")
            if pf.get("next_action"):
                print(f"  что осталось по мнению кита: {pf['next_action']} — это контекст, не задача")
            if report.get("error"):
                print(f"  · {report['error']}")
            if report.get("ready_for_pr") is not None:
                print(f"  ready_for_pr={report.get('ready_for_pr')}")
        if report.get("status") in ("error", "blocked"):
            return 2 if report.get("status") == "error" else 1
        return 0 if report.get("ready_for_pr") else 1
    if a.cmd == "run":
        # P0-1: провайдер резолвится ОДИН раз здесь и уходит в движок под своим именем (в отчёте
        # он же). Автовыбор — только в пользовательском пути --execute; без --execute (планирование)
        # провайдер не вызывается вовсе, поэтому остаётся офлайн-дефолт mock.
        prov = resolve_provider_for_run(a.provider, Path(a.child_root), execute=a.execute,
                                        quiet=a.json)
        # F-026 (то же правило, что у resume): исполняющий прогон без живого провайдера — фикция.
        # Здесь решение хотя бы печаталось, но вердикт всё равно выносился по прогону, в котором
        # модель не вызывалась ни разу. Офлайн остаётся, но как явный выбор человека.
        _refusal = live_provider_refusal(prov, a.provider) if a.execute else None
        if _refusal:
            if a.json:
                print(json.dumps({"kind": "run", "status": "error",
                                  "error": f"run --execute: {_refusal}",
                                  "provider_resolution": prov}, ensure_ascii=False, indent=2))
            else:
                print(f"ОТКАЗ: {_refusal}")
            return 2
        report = run(a.task, json.loads(a.signals), Path(a.child_root), a.features_dir,
                     a.runtime, prov["provider"], a.session, a.execute, feature=a.feature,
                     engine=a.engine, open_pr=a.open_pr, model=a.model,
                     baseline_diff=a.baseline_diff, require_fix=a.require_fix, max_steps=a.max_steps,
                     discard_previous=a.discard, sandbox=a.sandbox, review=a.review, author=a.author,
                     review_fix_attempts=a.fix_attempts, context_shadow=a.context_shadow,
                     context_hybrid=a.context_hybrid, reevaluate_only=a.reevaluate_only,
                     provider_resolution={k: prov.get(k) for k in
                                          ("provider", "source", "reason", "warning")})
        if a.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print_human(report)
        # finding аудита (P0.1): CLI отдаёт ненулевой код при ошибке/не-готовности —
        # чтобы CI/скрипты видели провал, а не считали любой прогон успешным.
        return exit_code(report)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
