#!/usr/bin/env python3
"""Стадии прогона ai-ops run (пробо-СВОБОДНАЯ передняя половина lifecycle): commit-барьер,
старт lifecycle, resume-гейт, агрегат стоимости, финализация статуса работы и транзакционная
доставка за барьером.

Вынесено из `ai_ops_run_lifecycle` без изменения поведения (чистый перенос + ре-экспорт из фасада)
для снятия god-модуля с потолка module-size. Мутационные пробы сидят в ЗАДНЕЙ половине — они
остались в фасаде `ai_ops_run_lifecycle` вместе с пробируемыми функциями. Направление импортов:
фасад импортирует этот модуль на уровне модуля (для ре-экспорта), а этот модуль тянет из фасада
только ОДНО имя (`_run_start_audience`) и только ЛЕНИВО внутри `_finalize_run` — на уровне модуля
цикла нет. Хелперы из `ai_ops_run` (`_note_bookkeeping_error`, `_outbox_dir`, `_unresolved_intents`,
`_resume_context_from_handoff`) подтягиваются лениво внутри тел функций — сохраняет их
патчабельность из тестов (`ai_ops_run.<name>`).
"""
from __future__ import annotations

import contextlib
import sys

from ai_ops_kit.engine.pipeline_helpers import work_produced, delivery_pending  # noqa: E402
from ai_ops_kit.lifecycle import workitem          # noqa: E402
from ai_ops_kit.lifecycle import active_work        # noqa: E402
from ai_ops_kit.shared import lifecycle_store as _ls   # noqa: E402


def _commit_barrier(rep, child_root, features_dir, fid, lifecycle_errors):
    """Commit-barrier перед доставкой: durable RunHandoff + final report + journal-checkpoint.
    K6: вынесено из run() без изменения поведения; -> (jname, handoff_ok, report_ok, plan)."""
    _jp = features_dir / fid / "lifecycle-journal.jsonl"
    _jname = str(_jp)
    _handoff_ok = False
    from ai_ops_kit.engine import run_handoff
    try:
        wt = child_root / ".ai" / "worktrees" / fid
        handoff = run_handoff.build_handoff(rep, work_root=(wt if wt.is_dir() else child_root))
        _hw = _ls.durable_write(features_dir / fid / "run-handoff.yaml", handoff,
                                require_keys=("kind", "workitem_id"), keep_backup=True)
        if _hw.get("ok"):
            _handoff_ok = True
            rep["handoff"] = {"next_action": handoff["next_action"],
                              "resume_from_revision": handoff["resume_from_revision"],
                              "open_questions": handoff["open_questions"]}
        else:
            lifecycle_errors.append(f"run-handoff durable-write: {_hw.get('error')} "
                                    "(доставка НЕ выполняется — lifecycle не зафиксирован)")
    except Exception as _e:  # noqa: BLE001
        lifecycle_errors.append(f"run-handoff build/write: {type(_e).__name__}: {_e}")
    if lifecycle_errors:
        rep["lifecycle_errors"] = lifecycle_errors
    # durable final report (ДО доставки) — второй барьер
    _rw = _ls.durable_write_json(features_dir / fid / "run-report.json", rep, keep_backup=True)
    _report_ok = _rw.get("ok")
    if not _report_ok:
        rep.setdefault("lifecycle_errors", [])
        rep["lifecycle_errors"].append(f"run-report durable-write: {_rw.get('error')} "
                                       "(доставка НЕ выполняется)")
    # journal checkpoint: готовность к доставке + прошли ли барьеры
    _plan = rep.get("delivery_plan")
    _ls.journal_append(_jname, {"kind": "ready_for_delivery", "run_id": fid, "workitem_id": fid,
                                "ready_for_delivery": bool(_plan),
                                "handoff_durable": _handoff_ok, "report_durable": bool(_report_ok),
                                "commit": (rep.get("commit") or {}).get("sha")})
    return _jname, _handoff_ok, _report_ok, _plan


def _start_lifecycle(features_dir, fid, task_text, signals, plan, engine, base, resume, execute,
                     saved_task, sandbox, baseline_diff, require_fix, author, review, open_pr,
                     write_scope, max_steps, base_binding):
    """Durable lifecycle-start: workitem.start + RunPlan/run-settings (write-barrier) + journal
    run_start + per-run снимок. K6: вынесено из run(). -> (attempt_id, error|None)."""
    workitem.start(str(features_dir), fid, task_text,
                   task_type=signals.get("task_type"), risk=signals.get("risk"))
    # v3.0.15 (finding аудита P1): RunPlan — write BARRIER. Сбой durable-записи -> прогон НЕ начат
    # (0 вызовов модели): без надёжного плана нельзя доказать routing/гейты/resume.
    _pw = _ls.durable_write(features_dir / fid / "run-plan.yaml", plan, require_keys=("workitem_id",))
    if not _pw.get("ok"):
        return None, {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": fid,
                "status": "error", "ready_for_pr": False,
                "error": f"lifecycle fail-closed: не удалось надёжно сохранить RunPlan ({_pw.get('error')}) "
                         "— прогон не начат (0 вызовов модели)"}
    # v3.0.14/v3.1 (trace v0.2): event journal — run_start. attempt_id = попытка прогона WorkItem
    # (resume/повтор -> новая попытка), детерминированно из числа снимков run-history.
    _jp = features_dir / fid / "lifecycle-journal.jsonl"
    _att = len(list((features_dir / fid / "run-history").glob("run-*.yaml"))) + 1
    _attempt_id = f"{fid}#a{_att}"
    _ls.journal_append(_jp, {"kind": "run_start", "run_id": fid, "workitem_id": fid,
                             "attempt_id": _attempt_id, "task_type": signals.get("task_type"),
                             "engine": engine, "base": base, "resume": bool(resume)})
    # v3.0-rc2 (P0.1): сохраняем ЭФФЕКТИВНУЮ политику прогона -> resume восстановит её, а не
    # переклассифицирует/деградирует до дефолтов. provider/model НЕ храним (runtime-выбор/секрет).
    if execute:
        _settings = {
            "schema_version": 1, "kind": "run-settings", "workitem_id": fid,
            # F-027: ПРОДУКТОВАЯ задача прогона хранится явно и переживает продолжение. Раньше
            # её не было нигде: signals пишутся без task_text, RunHandoff несёт только
            # next_action — и resume брал задачей служебное «что осталось». На продолжении
            # сохранённая задача не переписывается: она — идентичность работы, а не аргумент вызова.
            "task": (saved_task if (resume and saved_task) else task_text),
            "signals": {k: v for k, v in signals.items() if k != "task_text"},
            "policy": {"sandbox": sandbox, "baseline_diff": baseline_diff, "require_fix": require_fix,
                       "author": author, "review": review, "open_pr": open_pr,
                       "write_scope": write_scope, "max_steps": max_steps, "engine": engine,
                       "base": base,   # v3.0.2 (P0): резолвнутый base_ref (back-compat)
                       "base_binding": base_binding},   # v3.0.9 (P0.2): полный BaseBinding (ref+sha+mode+source)
        }
        # v3.0.12 (finding аудита блок B): run-settings — источник истины для resume, пишем DURABLE
        # (атомарно + fsync + перечитывание). Сбой записи -> FAIL-CLOSED отказ (без надёжной policy
        # resume восстановит мусор/дефолты). require_keys гарантируют, что перечитанный файл цел.
        _ws = _ls.durable_write(features_dir / fid / "run-settings.yaml", _settings,
                                require_keys=("kind", "policy", "signals"))
        if not _ws.get("ok"):
            return None, {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": fid,
                    "status": "error", "ready_for_pr": False,
                    "error": (f"lifecycle fail-closed: не удалось надёжно сохранить run-settings "
                              f"({_ws.get('error')}) — без durable policy resume небезопасен; прогон "
                              "не начат")}
        # v3.0-rc4 (P0.1): per-run СНИМОК для аудита (не только последнее состояние). Нумеруем по
        # числу уже сохранённых снимков — детерминированно, без времени (совместимо с workflow-песочницей).
        _hist = features_dir / fid / "run-history"
        _hist.mkdir(parents=True, exist_ok=True)
        _n = len(list(_hist.glob("run-*.yaml"))) + 1
        _ls.durable_write(_hist / f"run-{_n:03d}.yaml", _settings)   # v3.0.14 (#2): атомарно
    return _attempt_id, None


def _resume_gate(child_root, fid, base, force_resume, resume):
    """Resume-preflight гейт: продолжать WorkItem поверх подтверждённой работы или честный ранний
    выход (can_resume/base-rewritten/revalidation). K6: вынесено из run(). -> (pf, resume_ctx, error|None)."""
    resume_ctx = None
    pf = None
    if resume:
        from ai_ops_kit.engine import run_handoff
        pf = run_handoff.resume_preflight(child_root, fid, base=base)
        if not pf["can_resume"]:
            return None, None, {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": fid,
                    "status": "error", "engine": "pipeline", "ready_for_pr": False,
                    "error": "resume невозможен: " + "; ".join(pf["reasons"]),
                    "resume": {"requested": True, "resumed": False, "can_resume": False,
                               "reasons": pf["reasons"]}}
        # v3.0.10 (finding аудита P0): base ПЕРЕПИСАН (force-push назад / пересоздан на несвязанном
        # SHA — сохранённый base_sha исходного прогона больше не предок текущего HEAD базы). Это НЕ
        # fast-forward: продолжать старую работу против ДРУГОЙ базы и выдать её за проверенную нельзя.
        # force_resume этот случай НЕ снимает (иначе можно тихо переобозначить базу) — только явный
        # replan (пересобрать план + переисполнить с новой базы) либо отмена.
        # v3.0.14 (finding аудита #1, вариант B): base СДВИНУЛСЯ с прошлого прогона — переписан
        # (rewrite) ИЛИ ушёл вперёд (fast-forward). В ОБОИХ случаях старая работа НЕ интегрирована с
        # новой базой: resume ПЕРЕИСПОЛЬЗУЕТ worktree, форкнутый от старой базы (не пере-форкает), а
        # baseline считался на старой — отдать PR против новой базы нельзя. Блок на resume-пути НЕ
        # снимается ни force_resume, ни replan (обе модификации resume реиспользуют устаревший worktree).
        # Recourse — СВЕЖИЙ прогон от новой базы (без --resume; --discard заменит устаревшую ветку):
        # он пере-форкает worktree от новой базы. Авто-интеграция при resume (rebase onto B + повтор
        # проверок) — запланирована на v3.1.
        if pf.get("base_rewritten") or pf.get("base_moved"):
            _kind = ("переписан (force-push/пересоздание)" if pf.get("base_rewritten")
                     else "ушёл вперёд (fast-forward)")
            return None, None, {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": fid,
                    "status": "blocked", "engine": "pipeline", "ready_for_pr": False,
                    "error": (f"resume заблокирован: base {_kind} с прошлого прогона — старую работу "
                              "нельзя выдать за проверенную против новой базы (worktree форкнут от "
                              "старой базы и не интегрирован с новой). Ни force_resume, ни replan это "
                              # B2-10: здесь назывались флаги ВНУТРЕННЕЙ точки входа (`--resume`,
                              # `--discard`), которых у `ai-ops` нет вовсе. Человек читает это
                              # сообщение, работая через `ai-ops`, и набирает несуществующее.
                              "НЕ снимают. Нужен СВЕЖИЙ прогон от новой базы: удалите ветку "
                              f"прошлого прогона (`git branch -D ai-ops/{fid}`) и запустите "
                              f"`ai-ops run . --feature {fid} --execute`. " + "; ".join(pf["reasons"])),
                    "resume": {"requested": True, "resumed": False,
                               "base_rewritten": bool(pf.get("base_rewritten")),
                               "base_moved": bool(pf.get("base_moved")),
                               "revalidation_needed": True, "reasons": pf["reasons"]}}
        # ЧЕСТНОСТЬ: база/состояние изменились -> НЕ продолжаем молча на устаревшем evidence.
        if pf["revalidation_needed"] and not force_resume:
            return None, None, {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": fid,
                    "status": "blocked", "engine": "pipeline", "ready_for_pr": False,
                    "error": "resume требует ревалидации (база/состояние изменились с прошлого "
                             "прогона) — перепроверь и запусти с force_resume=True (--force), "
                             "чтобы продолжить осознанно",
                    "resume": {"requested": True, "resumed": False, "revalidation_needed": True,
                               "reasons": pf["reasons"]}}
        # _resume_context_from_handoff остаётся в ai_ops_run (используется тестами) — ленивый импорт.
        from ai_ops_kit.engine.ai_ops_run import _resume_context_from_handoff
        resume_ctx = _resume_context_from_handoff(child_root, fid)
    return pf, resume_ctx, None


def _finalize_run_cost(rep, orchestrator, model, jname, fid, attempt_id, signals, plan,
                       model_resolution, child_root):
    """Агрегат run_cost (tokens/latency/cost) из вызовов модели + usage-ledger + очистка
    call-context. K6: вынесено из run(); мутирует rep, без ранних выходов."""
    # _note_bookkeeping_error остаётся в ai_ops_run (используется и forbidden-функциями/тестами) —
    # ленивый импорт, чтобы не замкнуть ai_ops_run <-> ai_ops_run_lifecycle.
    from ai_ops_kit.engine.ai_ops_run import _note_bookkeeping_error
    # v3.1 (trace v0.2): run_cost — агрегат tokens/latency/cost из вызовов модели (наблюдаемость).
    _stats_error = None
    try:
        _stats = orchestrator.drain_call_stats()
    except Exception as _se:  # noqa: BLE001 — сбор статистики не должен ронять уже сделанный прогон
        # но «не собрали» != «расхода не было»: инвариант Usage Truth требует unavailable,
        # а не тихий ноль.
        _stats, _stats_error = [], f"{type(_se).__name__}: {_se}"[:200]
        rep["run_cost"] = {"status": "unavailable", "reason": _stats_error}
    if _stats:
        _in = sum(s.get("input_tokens") or 0 for s in _stats)
        _out = sum(s.get("output_tokens") or 0 for s in _stats)
        _lat = round(sum(s.get("latency_s") or 0 for s in _stats), 3)
        _costs = [s.get("cost_usd_est") for s in _stats if s.get("cost_usd_est") is not None]
        _cost = round(sum(_costs), 6) if _costs else None
        _cost_rep = {"calls": len(_stats), "input_tokens": _in, "output_tokens": _out,
                     "latency_s": _lat, "cost_usd_est": _cost, "model": model}
        rep["cost"] = _cost_rep
        # ROID (P1, аудит 10 ролей): стоимость прогона, ПРИВЯЗАННАЯ К ИСХОДУ. cost_account считал
        # cost_per_successful_change, но модуль был дормантным (0 не-тестовых импортёров) — экономика
        # мерилась в вакууме, не против реального результата. Здесь переиспользуем ту же чистую функцию
        # с фактическими данными прогона: успешное ПРОВЕРЕННОЕ изменение = ready_for_pr (приёмка/доставка
        # пройдены к этому моменту). Не проверено -> cost_per_change=None: стоимость есть, успешных
        # изменений нет — честные потери, а не деление на ноль (честность держит сам cost_account).
        from ai_ops_kit.providers import cost_account
        _roid = cost_account.cost_per_successful_change(
            {"calls_cost": _cost, "latency_s": _lat, "delivered_verified": bool(rep.get("ready_for_pr"))})
        rep["roid"] = {"cost_per_successful_change": _roid["cost_per_change"],
                       "total_cost": _roid["total_cost"], "delivered_verified": _roid["delivered_verified"],
                       "note": _roid["note"]}
        # #637: нагруженная стоимость ИСХОДА (AI + внимание человека); сборка — в cost_account,
        # сюда только проводка в отчёт (монолит lifecycle не растёт содержательно).
        rep["roid_outcome"] = cost_account.roid_outcome_report(
            _cost, _lat, rep.get("manual_interventions"), rep.get("ready_for_pr"), child_root)
        _ls.journal_append(jname, {"kind": "run_cost", "run_id": fid, "workitem_id": fid,
                                    "attempt_id": attempt_id, **_cost_rep})
        # v3.10.0 Usage Truth: персист КАЖДОГО вызова (writer/reviewer/fix-loop/fallback/escalation)
        # в ledger задачи + продукта. Честный usage_status; неизвестное -> unavailable, не 0.
        # v3.24.0 Cost & Architecture Accuracy: extra_context штампуется на все записи —
        # task_type/workflow/risk/size/writer_tier/execution_mode/stack для economic alternatives.
        try:
            from ai_ops_kit.shared import usage_ledger as _ul
            _extra = {
                "task_type": signals.get("task_type"),
                "workflow": (plan.get("base_workflow") if isinstance(plan, dict) else None),
                "risk": signals.get("risk"),
                "size": signals.get("size"),
                "writer_tier": ((model_resolution or {}).get("writer") or {}).get("tier"),
                "execution_mode": "sequential" if signals.get("_sequence_internal") else "single",
                "stack": ",".join(s.get("language", "") for s in (signals.get("_stacks") or [])) or None,
            }
            _ul.append(child_root, fid, _stats, run_id=fid, extra_context={k: v for k, v in _extra.items() if v is not None})
        except Exception as _ue:  # noqa: BLE001 — учёт usage не должен ронять прогон...
            # ...но и пропасть молча не должен: занижённая стоимость, поданная как факт, —
            # это нарушение той самой Usage Truth, ради которой ledger и существует.
            _note_bookkeeping_error(rep, "usage_ledger.append", _ue)
    # СРЕЗ engine РАТЧЕТА 2026-08-12: здесь стоял `try/except Exception: pass` без причины.
    # Подавлять нечего: `clear_call_context` — это `dict.clear()` над модульным `_CALL_CONTEXT`
    # (`providers/orchestrator_usage.py`), он не бросает. Пустой `except` защищал от
    # несуществующего сбоя и при этом был бы единственным местом, где утрата контекста вызова
    # (Usage Truth: role/trigger/provider) прошла бы молча, если бы функция когда-нибудь стала
    # бросать. Снят, а не задокументирован: подавление без риска — это шум, а не решение.
    orchestrator.clear_call_context()


def _finalize_run(rep, fid, child_root, jname, attempt_id, aw_path):
    """Финализация прогона: событие run_completed + журнал run_end + вывод статуса работы
    (done/blocked) + снятие active-work с учёта. K6: вынесено из run(); -> rep."""
    # _run_start_audience остаётся в фасаде ai_ops_run_lifecycle (его зовёт и _register_active_work)
    # — ленивый импорт ВНУТРИ функции, чтобы module-level импорт-граф satellite->facade был 
    # односторонним (фасад импортирует стадии, стадии фасад — только лениво, цикла нет).
    from ai_ops_kit.engine.ai_ops_run_lifecycle import _run_start_audience
    # v3.16.0 Development Culture Guardrails (WP5): каждый прогон завершается SessionRecommendation
    # (гигиена сессии/контекста) с точной командой. ADVISE-ONLY: НЕ блокирует прогон/доставку.
    # v3.38 (W3.2): ядро НЕ импортирует спутники — session recommendation через событие run_completed.
    # Подписчик (engops/session_guardrails) реагирует синхронно и пишет в rep in-place.
    from ai_ops_kit.shared.events import emit as _emit
    try:
        _emit("run_completed", {
            "event_type": "run_completed",
            "workitem_id": fid,
            "child_root": str(child_root),
            "status": rep.get("overall_status") or ("ready" if rep.get("ready_for_pr") else "not-ready"),
            "ready_for_pr": bool(rep.get("ready_for_pr")),
            "report": rep,
        })
    except Exception:  # noqa: BLE001,S110 — событие ADVISE-ONLY: его утрата не меняет вердикт
        pass
    # run_end (исход прогона, включая итог доставки)
    _ls.journal_append(jname, {"kind": "run_end", "run_id": fid, "workitem_id": fid,
                                "attempt_id": attempt_id,
                                "status": rep.get("overall_status") or ("ready" if rep.get("ready_for_pr")
                                                                        else "not-ready"),
                                "ready_for_pr": bool(rep.get("ready_for_pr")),
                                "commit": (rep.get("commit") or {}).get("sha")})
    # F-012: `done` только когда работа действительно доведена. NOT_READY -> blocked, иначе
    # `ai-ops status` показывает пустоту при незакрытых гейтах и ненаписанном коде.
    _ready = bool(rep.get("ready_for_pr"))
    _unmet = (rep.get("gates") or {}).get("unmet") or []
    # НАХОДКА ИИ-СРЕДЫ (ежедневная): факт работы брался из счётчика write-операций брокера, а
    # писать могут иначе — writer уровня `claude -p` своими инструментами, `sed -i` в shell,
    # и модель может закоммитить сама. Тогда `applied_writes == 0` при живом коммите, и статус
    # работы становился «blocked: код не написан — правок 0». По отчёту выглядело, будто кит не
    # работает, хотя он работал. Ground truth — git: коммит и его файлы.
    _wrote = work_produced(rep)
    if _ready:
        _st, _why = "done", None
    elif _wrote:
        _st, _why = "blocked", f"гейты не закрыты: {', '.join(_unmet) or 'см. отчёт'}"
    else:
        _st, _why = "blocked", "код не написан — правок 0 (нужен живой провайдер или внешний исполнитель)"
    # B2-20 (повтор B2-12, живой прогон 14.08.2026): `resume` завершённой-но-НЕДОСТАВЛЕННОЙ работы
    # заново звал писателя, получал ноль правок (делать уже нечего) и хоронил готовый READY_FOR_PR в
    # `blocked: код не написан`. Продолжение поверх ветки без новых правок — это НЕ «код не написан».
    if _st == "blocked" and delivery_pending(rep):
        # #695: называем ОДНУ автономную команду доставки готового (printed-commands-are-runnable).
        print("  работа прошлого прогона на ветке, дописывать нечего — нужна ДОСТАВКА, а не "
              "повторный прогон. Одной командой доведёт до открытого PR (нужен GITHUB_TOKEN):\n"
              f"    ./ai-ops resume {child_root} {fid} --deliver-only --open-pr --execute")
        with contextlib.redirect_stdout(sys.stderr):
            active_work.finish_cmd(aw_path, fid, status="blocked", child_root=child_root,  # #695
                                   reason="ждёт доставки: работа готова на ветке, новых правок нет",
                                   audience=_run_start_audience(child_root))
        _ls.merge_bookkeeping_losses(rep)
        return rep
    with contextlib.redirect_stdout(sys.stderr):
        active_work.finish_cmd(aw_path, fid, status=_st, reason=_why, child_root=child_root,  # #695
                               audience=_run_start_audience(child_root))
    _ls.merge_bookkeeping_losses(rep)   # утраченные записи журнала называются в отчёте, а не пропадают
    return rep


def _deliver(ctx, rep, *, plan, handoff_ok, report_ok, jname, fid):
    """Транзакционная доставка за commit-барьером: governance-gate -> DeliveryIntent -> внешнее
    действие (PR, идемпотентно) -> DeliveryReceipt. K6: вынесено из run() без изменения поведения.

    DELIVERY — только за барьером: план готов И обе критические записи durable. v3.0.16 Phase A:
    DELIVERY OUTBOX — внешнее действие (PR) и локальная запись НЕ атомарны, поэтому durable
    DeliveryIntent -> external delivery (идемпотентно) -> durable DeliveryReceipt; если запись
    Receipt упала -> outcome_unknown + reconciliation_required. GOVERNANCE (Фаза 4): решение политики
    впрыскивается здесь (decision_log ВСЕГДА; блокирует только в enforcement=block, по умолчанию
    observe). Мутирует `rep` на месте (delivery/overall_status/lifecycle_errors); -> None.
    """
    from ai_ops_kit.engine import execution_pipeline
    # _outbox_dir/_unresolved_intents остаются в ai_ops_run (последний нужен и forbidden-функции
    # _reconcile_pending_delivery) — ленивый импорт, чтобы не замкнуть импорт-граф.
    from ai_ops_kit.engine.ai_ops_run import _outbox_dir, _unresolved_intents
    child_root, features_dir = ctx.child_root, ctx.features_dir
    _gate = None
    if plan and handoff_ok and report_ok:
        from ai_ops_kit.governance import enforcement as _enf
        _gate = _enf.gate_delivery(child_root, target=fid)
        rep.setdefault("governance", {})["delivery"] = _gate
    if _gate and _gate.get("blocked"):
        rep["delivery"] = {"requested": True, "status": "blocked-by-policy",
                           "reason": f"policy: {_gate.get('reason')} — доставка остановлена "
                                     "governance (режим block)"}
        rep["overall_status"] = "delivery-blocked"
        _ls.durable_write_json(features_dir / fid / "run-report.json", rep)
    elif plan and handoff_ok and report_ok:
        import hashlib as _hl
        from ai_ops_kit.gates import concurrency_preflight as _cpp
        _branch = plan["work_branch"]
        _csha = plan["committed_sha"]
        # ДРЕЙФ-БЕЗОПАСНАЯ РЕ-ВЕРИФИКАЦИЯ (перед DeliveryIntent): доказательство прогона привязано к
        # base_sha — итогу слияния на момент проверок. Сдвинулась цель (remote base) с тех пор ->
        # evidence относится к СТАРОМУ итогу; выдать его за проверенное против фактического merge
        # нельзя. Fail-closed: помечаем stale-needs-reverify, Intent НЕ создаём и PR НЕ открываем
        # (доставки устаревшего не будет) — нужен ре-прогон против новой цели. Цель не двигалась ->
        # путь ниже без изменений (счастливый путь сохранён).
        _reverify = execution_pipeline._reverify_against_current_target(
            plan["work_root"], plan["base_ref"], plan["base_sha"], _csha)
        if _reverify.get("stale"):
            rep["delivery"] = {"requested": True, "status": "stale-needs-reverify",
                               "evidence_base": _reverify.get("evidence_base"),
                               "current_target": _reverify.get("current_target"),
                               "merge_preview": _reverify.get("merge_preview"),
                               "reason": _reverify.get("reason")}
            rep["overall_status"] = "delivery-stale-needs-reverify"
            _ls.durable_write_json(features_dir / fid / "run-report.json", rep)
            _ls.journal_append(jname, {"kind": "delivery_stale", "run_id": fid, "workitem_id": fid,
                                       "current_target": _reverify.get("current_target")})
            return
        # repository identity (owner/name из origin) — часть СТРОГОЙ идентичности доставки
        _ru = execution_pipeline._git(child_root, "remote", "get-url", "origin")
        _orn = _cpp._parse_owner_repo(_ru[1]) if _ru[0] == 0 else None
        _repo = f"{_orn[0]}/{_orn[1]}" if _orn else None
        # delivery_id детерминирован по (repository, wid, branch, commit) — идемпотентный ключ
        _did = _hl.sha256(f"{_repo}:{fid}:{_branch}:{_csha}".encode("utf-8")).hexdigest()[:16]
        _obx = _outbox_dir(features_dir, fid)
        _ip = _obx / f"{_did}.intent.yaml"
        _rp = _obx / f"{_did}.receipt.yaml"
        # v3.0.17 (P0): НЕразрешённая доставка (Intent без Receipt) на ЭТОЙ ветке (иной delivery_id)
        # БЛОКИРУЕТ новую внешнюю доставку до reconciliation — не затираем неизвестный исход.
        _blocking = [d for (d, _i) in _unresolved_intents(features_dir, fid, branch=_branch) if d != _did]
        if _blocking:
            rep["delivery"] = {"requested": True, "status": "blocked-unresolved-delivery",
                               "reason": f"есть неразрешённая доставка {_blocking[0]} на ветке {_branch} "
                                         "(нет DeliveryReceipt) — новая доставка запрещена до reconciliation"}
            rep["overall_status"] = "delivery-failed"
            _ls.durable_write_json(features_dir / fid / "run-report.json", rep)
        else:
            # DeliveryIntent (BARRIER) со СТРОГОЙ идентичностью
            _intent = {"schema_version": 1, "kind": "DeliveryIntent", "delivery_id": _did,
                       "workitem_id": fid, "repository": _repo, "branch": _branch,
                       "base_ref": plan["base_ref"], "base_sha": plan["base_sha"],
                       "commit_sha": _csha, "status": "intended"}
            _iw = _ls.durable_write(_ip, _intent,
                                    require_keys=("kind", "delivery_id", "commit_sha", "repository"),
                                    keep_backup=True)
            if not _iw.get("ok"):
                rep["delivery"] = {"requested": True, "status": "blocked-lifecycle",
                                   "reason": f"DeliveryIntent не зафиксирован durable ({_iw.get('error')}) "
                                             "— внешнее действие не выполняется"}
                rep["overall_status"] = "delivery-failed"
                _ls.durable_write_json(features_dir / fid / "run-report.json", rep)
            else:
                _ls.journal_append(jname, {"kind": "delivery_intent", "run_id": fid, "workitem_id": fid,
                                           "delivery_id": _did, "branch": _branch, "commit": _csha,
                                           "repository": _repo})
                # ВНЕШНЕЕ ДЕЙСТВИЕ (идемпотентно; delivery_id вшивается в тело PR)
                _dv = execution_pipeline._deliver_pr(
                    plan["work_root"], _branch, plan["base_ref"], plan["base_sha"],
                    plan["base_binding"], _csha, plan["wid"], plan["task"], delivery_id=_did)
                _st = _dv.get("status")
                _pr = _dv.get("pr") or {}
                if _st == "outcome_unknown":
                    # неоднозначный POST -> НЕ пишем confirmed Receipt; помечаем Intent (BARRIER).
                    _uw = _ls.durable_write(_ip, {**_intent, "status": "outcome_unknown",
                                                  "reconciliation_required": True},
                                            require_keys=("kind", "delivery_id", "status"))
                    rep["delivery"] = {**_dv, "delivery_id": _did, "reconciliation_required": True,
                                       "intent_marker_durable": bool(_uw.get("ok"))}
                    rep["overall_status"] = "delivery-outcome-unknown"
                    _ls.durable_write_json(features_dir / fid / "run-report.json", rep)
                    _ls.journal_append(jname, {"kind": "delivery_outcome_unknown", "run_id": fid,
                                               "workitem_id": fid, "delivery_id": _did, "cause": "ambiguous-post"})
                else:
                    _delivered = _st in ("opened", "updated")
                    _remote_sha = _pr.get("head_sha")
                    _sha_ok = (_remote_sha == _csha) if _remote_sha else None
                    _receipt = {"schema_version": 1, "kind": "DeliveryReceipt", "delivery_id": _did,
                                "workitem_id": fid, "repository": _repo, "branch": _branch,
                                "commit_sha": _csha, "base_ref": plan["base_ref"], "status": _st,
                                "remote_sha": _remote_sha, "sha_verified": _sha_ok,
                                "pr_url": _pr.get("url"), "pr_number": _pr.get("number")}
                    # v3.38 (K7): инварианты DeliveryReceipt — fail-closed.
                    from ai_ops_kit.gates.invariants import check_invariant as _ci
                    _del_breaches = []
                    for _inv_id, _kw in [
                        ("INV-DELIVERY-001", {"sha_verified": _sha_ok, "remote_sha": _remote_sha}),
                        ("INV-DELIVERY-002", {"status": _st, "sha_verified": _sha_ok}),
                        ("INV-DELIVERY-003", {"commit_sha": _csha, "branch": _branch}),
                    ]:
                        try:
                            if not _ci(_inv_id, **_kw):
                                _del_breaches.append(_inv_id)
                        except (KeyError, TypeError):
                            pass
                    if _del_breaches:
                        _receipt["invariant_breaches"] = _del_breaches
                    _cw = _ls.durable_write(_rp, _receipt,
                                            require_keys=("kind", "delivery_id", "status"), keep_backup=True)
                    if _cw.get("ok"):
                        _ls.durable_write(_ip, {**_intent, "status": "completed"})   # receipt авторитетен
                        rep["delivery"] = {**_dv, "delivery_id": _did, "remote_sha": _remote_sha,
                                           "sha_verified": _sha_ok,
                                           "receipt": f"features/{fid}/delivery-outbox/{_did}.receipt.yaml"}
                        rep["overall_status"] = "delivered" if _delivered else "delivery-failed"
                        _ls.journal_append(jname, {"kind": "delivery_receipt", "run_id": fid,
                                                   "workitem_id": fid, "delivery_id": _did, "status": _st,
                                                   "delivered": _delivered, "remote_sha": _remote_sha,
                                                   "pr_url": _pr.get("url")})
                        _dw = _ls.durable_write_json(features_dir / fid / "run-report.json", rep,
                                                     keep_backup=True)
                        if not _dw.get("ok"):
                            rep.setdefault("lifecycle_errors", [])
                            rep["lifecycle_errors"].append(f"delivery-report durable-write: {_dw.get('error')}")
                    else:
                        # ВНЕШНЕЕ ДЕЙСТВИЕ ВЫПОЛНЕНО, Receipt НЕ сохранён -> outcome_unknown (Intent BARRIER).
                        # Даже если и эта запись упадёт: reconciliation ловит Intent-БЕЗ-Receipt по факту.
                        _uw = _ls.durable_write(_ip, {**_intent, "status": "outcome_unknown",
                                                      "reconciliation_required": True,
                                                      "observed": {"status": _st, "pr_url": _pr.get("url")}},
                                                require_keys=("kind", "delivery_id", "status"))
                        rep["delivery"] = {**_dv, "delivery_id": _did, "status": "outcome_unknown",
                                           "reconciliation_required": True,
                                           "intent_marker_durable": bool(_uw.get("ok")),
                                           "reason": f"внешнее действие выполнено, но DeliveryReceipt не "
                                                     f"зафиксирован durable ({_cw.get('error')}) — исход "
                                                     "сверится с remote при следующем прогоне (идемпотентно)"}
                        rep["overall_status"] = "delivery-outcome-unknown"
                        _ls.durable_write_json(features_dir / fid / "run-report.json", rep)
                        _ls.journal_append(jname, {"kind": "delivery_outcome_unknown", "run_id": fid,
                                                   "workitem_id": fid, "delivery_id": _did,
                                                   "cause": "receipt-write-failed"})
    elif plan and not (handoff_ok and report_ok):
        # барьер не пройден -> доставку запрещаем fail-closed (не отдаём непрозафиксированное наружу)
        rep["delivery"] = {"requested": True, "status": "blocked-lifecycle",
                           "reason": "durable RunHandoff/final report не зафиксированы — доставка "
                                     "запрещена до надёжной фиксации доказательств и состояния"}
        rep["overall_status"] = "delivery-failed"
        _ls.durable_write_json(features_dir / fid / "run-report.json", rep)

