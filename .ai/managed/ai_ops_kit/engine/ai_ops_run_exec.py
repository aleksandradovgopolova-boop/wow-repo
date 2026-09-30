#!/usr/bin/env python3
"""Проб-свободные run-хелперы ai-ops run (фасад): fix-loop, base-резолв, proposers, preflight, аргпарсер.

Вынесено из god-модуля `ai_ops_run` без изменения поведения (чистый перенос + ре-экспорт) —
тот же приём, что уже применён для print/reporting/lifecycle-спутников. Здесь живут функции,
у которых НЕТ мутационной пробы (`quality/mutation-probes.yaml`): пробируемые точки (`main`,
`_run_controller_path`) и публичный вход `run` остались в `ai_ops_run`. Зависимости берутся из
РЕАЛЬНЫХ домов (engine/providers/gates/lifecycle/shared), а не из `ai_ops_run` — иначе получился
бы циклический импорт. Ре-экспорт в `ai_ops_run` держит вызовы `ai_ops_run.<name>` (CLI, тесты,
спутники-модули) на прежних именах.

Самодостаточный кластер «резолв провайдера + скан незавершённых intents + resume-контекст + профиль»
вынесен в спутник `run_exec_context` (`_outbox_dir`, `resolve_provider_for_run`, `is_service_text`,
`product_task_for_resume`, `_profile_for_report`, `_unresolved_intents`, `_nonfinal_receipt_intents`,
`_resume_context_from_handoff`, `_with_provider_fallback`, `_load_klp_by_env`, `_provider_trust`) и
ре-экспортирован ниже, чтобы `ai_ops_run_exec.<name>` осталось прежним. Спутник фасад не импортирует —
обратного ребра нет, цикла нет.

ИСКЛЮЧЕНИЕ (патчабельность): `_execute_with_fix_loop` обращается к `_provider_trust` и
`_with_provider_fallback` через модуль `ai_ops_run` (`_ar.<name>`), а не по локальному имени —
тесты подменяют их как `ai_ops_run._provider_trust`, и без обращения через модуль подмена бы
не доходила до вынесенной петли. Импорт `ai_ops_run` ленивый (в теле функции) — цикла нет.
"""
from __future__ import annotations

import argparse
import contextlib
import sys

import yaml

from ai_ops_kit.engine.ai_ops_run_reporting import _review_fix_context   # noqa: E402
from ai_ops_kit.lifecycle import active_work   # noqa: E402
from ai_ops_kit.shared import lifecycle_store as _ls   # noqa: E402
# Ре-экспорт вынесенного кластера (резолв провайдера + скан intents + resume-контекст + профиль):
# фасадные функции зовут `_profile_for_report`, а `_execute_with_fix_loop` — `_provider_trust`/
# `_with_provider_fallback` через модуль `ai_ops_run`; вызовы `ai_ops_run_exec.<name>` (и через
# него `ai_ops_run.<name>`) остаются на прежних именах. Сателлит фасад НЕ импортирует — цикла нет.
from ai_ops_kit.engine.run_exec_context import (   # noqa: E402,F401
    _outbox_dir,
    resolve_provider_for_run,
    _SERVICE_TASK_MARKERS,
    is_service_text,
    product_task_for_resume,
    _profile_for_report,
    _unresolved_intents,
    _nonfinal_receipt_intents,
    _resume_context_from_handoff,
    _with_provider_fallback,
    _load_klp_by_env,
    _provider_trust,
)


def _note_bookkeeping_error(rep, what, exc):
    """Записать в отчёт УТРАТУ служебной записи, не роняя прогон. -> None (правит rep на месте).

    ЗАЧЕМ ОТДЕЛЬНАЯ ФУНКЦИЯ (ревизия 2026-08-11). Учёт usage и lifecycle-журнал писались под
    `except Exception: pass`. Решение «служебная запись не роняет прогон» — правильное и
    записанное: падать из-за журнала посреди доставки хуже, чем потерять строку журнала. Но
    вторая половина решения отсутствовала: потеря была НЕВИДИМОЙ. Для кита, чья заявленная
    ценность — Usage Truth и `unavailable != 0`, молча пропавшая запись стоимости означает
    занижённый счёт, поданный как факт. Тот же класс, что «нет расписки» вместо «не смог
    прочитать расписку».

    Образец взят в этом же файле: рядом уже есть `escalation_error` с пометкой «rc3: НЕ глотаем
    молча -> честный escalation_error». Здесь — то же для служебных записей: прогон продолжается
    (fail-open сохранён), но в отчёте появляется `bookkeeping_errors` с тем, ЧТО потеряно и почему.

    v3.36.9 (срез engine ратчета): реализация переехала в `lifecycle_store` — тот же приём, что у
    `_durable_write_yaml` в workpackage_executor. Причина: этот же ответ понадобился второму
    вызывающему (`workpackage_executor`, событие `package_end`), а два экземпляра одного решения
    расходятся. Здесь остался делегат — вызовы и тесты, ссылающиеся на него, продолжают работать.
    """
    _ls.note_bookkeeping_error(rep, what, exc)


def _execute_with_fix_loop(ctx, uctx, *, execute, plan, discard_previous, install_deps,
                           hybrid_prelude, calib, ui_evidence, reevaluate_only, resume,
                           resume_ctx, attempt_id, fid, aw_path, review_fix_attempts,
                           reviewer_proposer, author_proposer):
    """Исполнение прогона движком + fix-loop с quality-эскалацией writer'а.

    K6: вынесено из run() без изменения поведения. Читает/мутирует `ctx` (prop/rev_prop/auth_prop,
    model_resolution, trust-группа). -> (rep, terminal_error): при обычном исходе terminal_error=None
    и rep — доказанный результат; при сбое провайдера/инфры terminal_error — честный error-отчёт
    (durable-записанный), а rep=None; KeyboardInterrupt/SystemExit ПРОБРАСЫВАЕТСЯ (active-work закрыт).

    v3.1.1 fix-loop: блокеры ревью/проверок -> писателю на ИТЕРАЦИЮ поверх той же ветки (resume=True),
    пока не pass ЛИБО не исчерпан бюджет. fail-closed: бюджет кончился и всё ещё не ready -> честный
    блок (ничего не форсируем в green). Не для mock. v3.8.3 WRITER QUALITY-ESCALATION: money-mode взял
    дешёвого writer'а; при КАЧЕСТВЕННОМ провале эскалируем на СИЛЬНЕЙШУЮ допущенную модель по ladder.
    """
    from ai_ops_kit.engine import execution_pipeline
    from ai_ops_kit.engine import tool_loop
    from ai_ops_kit.providers import orchestrator
    # _provider_trust/_with_provider_fallback берём через модуль ai_ops_run (ленивый импорт): тесты
    # подменяют их как `ai_ops_run.<name>`, и без обращения через модуль подмена бы сюда не дошла.
    from ai_ops_kit.engine import ai_ops_run as _ar

    def _pipe(_resume, _rctx):
        return execution_pipeline.run_pipeline(
            ctx.task_text, ctx.signals, ctx.child_root, ctx.prop, feature=ctx.feature, plan=plan,
            commit=execute, isolate=execute, open_pr=ctx.open_pr, baseline_diff=ctx.baseline_diff,
            require_fix=ctx.require_fix, max_steps=ctx.max_steps, discard_previous=discard_previous,
            sandbox=ctx.sandbox, review=ctx.review, reviewer_proposer=ctx.rev_prop,
            author=ctx.author, author_proposer=ctx.auth_prop, install_deps=install_deps,
            context_prelude=hybrid_prelude,   # v3.7.16: hybrid (v1 ∪ v2-additions) реально подаётся модели
            resume=_resume, resume_context=_rctx, write_scope=ctx.write_scope,
            base=ctx.base,   # v3.0.1/v3.0.7 (P0): base сквозной; None -> auto-резолв (не хардкод main)
            defer_delivery=True,   # v3.0.15 (P0): PR открывает КОНТРОЛЛЕР после durable-фиксации lifecycle
            calibrated_enforcement=calib, ui_evidence=ui_evidence,
            reevaluate_only=reevaluate_only,   # v3.8.3-rc: переоценка гейтов после человеко-approval БЕЗ переавторинга
            strict_judge_qualified=ctx.sec_qualified)   # v3.7.1: нет qualified судьи -> security pending_human
    try:
        rep = _pipe(resume, resume_ctx)
        _fix_left = int(review_fix_attempts or 0)
        # v3.8.3 WRITER QUALITY-ESCALATION: ладдер по success_rate (impl из model_resolution).
        _esc_ladder = (((ctx.model_resolution or {}).get("plan") or {}).get("implementation") or {}).get("escalation_ladder") or []
        _esc_idx = 0
        _QUALITY_GATES = {"implementation_verification", "code_review"}
        _rev_self = not ((ctx.model_resolution.get("reviewer") or {}).get("independent_by_model")) if isinstance(ctx.model_resolution, dict) else True
        while (not rep.get("ready_for_pr")) and _fix_left > 0 and ctx.provider_name not in (None, "mock"):
            _fx = _review_fix_context(rep)
            if not _fx:
                break   # блок не модель-фиксируем (human/base/lifecycle) -> не зацикливаем
            # эскалация writer'а, если провалены КАЧЕСТВЕННЫЕ гейты и ладдер не исчерпан (model=None -> router-путь)
            _unmet = set((rep.get("gates") or {}).get("unmet") or [])
            if ctx.model is None and (_unmet & _QUALITY_GATES) and _esc_idx < len(_esc_ladder):
                if ctx.model_resolution.get("model_attempts"):
                    ctx.model_resolution["model_attempts"][-1]["outcome"] = "quality_failed"
                from ai_ops_kit.providers import provider_endpoints as _pe2

                def _cand_trusted(c):  # rc3: JIT trust кандидата эскалации (ключ + KLP/TTL)
                    if not _pe2.key_available(c.get("provider")):
                        return False, "ключ отсутствует в env"
                    _ct = _ar._provider_trust(c["provider"], _pe2.endpoint_for(c["provider"])["key_env"],
                                              ctx.klp_by_env, ctx.trust_env, ctx.trust_now, ctx.trust_cache)
                    return _ct["ready"], _ct.get("reason")
                # найти СЛЕДУЮЩЕГО кандидата ладдера, прошедшего JIT-trust; не готов -> исключить+записать
                _esc = None
                while _esc_idx < len(_esc_ladder):
                    _cand = _esc_ladder[_esc_idx]; _esc_idx += 1
                    try:
                        _ok, _why = _cand_trusted(_cand)
                    except Exception as _ce:  # noqa: BLE001 — сбой trust-проверки -> исключаем честно
                        _ok, _why = False, f"trust-check упал: {type(_ce).__name__}"
                    if _ok:
                        _esc = _cand; break
                    ctx.model_resolution.setdefault("escalation_excluded", []).append(
                        {"model": _cand.get("model_id"), "provider": _cand.get("provider"), "reason": _why})
                if _esc is not None:
                    try:
                        _eep = _pe2.endpoint_for(_esc["provider"])
                        _eprov = orchestrator.make_openai_provider(_esc["model_id"], _eep["base_url"], _eep["key_env"])
                        # #6-fallback на СЛЕДУЮЩЕГО TRUSTED кандидата (если эскалированный сам жёстко 429-ится)
                        _nxt = next((n for n in _esc_ladder[_esc_idx:] if _cand_trusted(n)[0]), None)
                        if _nxt:
                            _nep = _pe2.endpoint_for(_nxt["provider"])
                            _eprov = _ar._with_provider_fallback(
                                _eprov, orchestrator.make_openai_provider(_nxt["model_id"], _nep["base_url"], _nep["key_env"]))
                        _eprov_ctx = uctx(_eprov, "implementation", "escalation", _esc.get("provider"))  # v3.10.0 Usage Truth
                        ctx.prop = tool_loop.make_model_proposer(_eprov_ctx)  # writer -> выше observed success
                        if ctx.author and author_proposer is None:
                            ctx.auth_prop = _eprov_ctx
                        if ctx.review and reviewer_proposer is None and _rev_self:
                            ctx.rev_prop = _eprov_ctx                        # self-model reviewer следует за writer'ом
                        ctx.model_resolution["effective_model"] = _esc["model_id"]
                        ctx.model_resolution.setdefault("model_attempts", []).append(
                            {"attempt": len(ctx.model_resolution.get("model_attempts") or []) + 1,
                             "model": _esc["model_id"], "provider": _esc.get("provider"),
                             "trigger": "quality_escalation", "outcome": "pending",
                             "observed_success_rate": _esc.get("observed_success_rate"),
                             "corpus_version": _esc.get("corpus_version")})
                        ctx.model_resolution.setdefault("escalations", []).append(
                            {"to": _esc["model_id"], "provider": _esc.get("provider"),
                             "observed_success_rate": _esc.get("observed_success_rate"),
                             "corpus_version": _esc.get("corpus_version"),
                             "reason": "quality-failure:" + ",".join(sorted(_unmet & _QUALITY_GATES))})
                    except Exception as _ee:  # noqa: BLE001 — rc3: НЕ глотаем молча -> честный escalation_error
                        ctx.model_resolution["escalation_error"] = f"{type(_ee).__name__}: {_ee}"[:200]
            try:
                _ls.journal_append(ctx.features_dir / fid / "lifecycle-journal.jsonl",
                                   {"kind": "fix_attempt", "run_id": fid, "workitem_id": fid,
                                    "attempt_id": attempt_id, "remaining": _fix_left,
                                    "unmet": (rep.get("gates") or {}).get("unmet")})
            except Exception as _je:  # noqa: BLE001 — журнал не роняет fix-loop...
                # ...но пробел в аудит-цепочке обязан быть видимым: цепочка checksum'ов
                # lifecycle-журнала после пропущенной записи уже не полна.
                _note_bookkeeping_error(rep, "lifecycle_journal.fix_attempt", _je)
            rep = _pipe(True, _fx + (("\n\n" + resume_ctx) if resume_ctx else ""))
            _fix_left -= 1
    except (KeyboardInterrupt, SystemExit):
        from ai_ops_kit.engine.ai_ops_run_lifecycle import _run_start_audience
        with contextlib.redirect_stdout(sys.stderr):
            active_work.finish_cmd(aw_path, fid, status="blocked", child_root=ctx.child_root,
                                   reason="прогон прерван (Ctrl-C/exit) — работа не завершена",  # #695
                                   audience=_run_start_audience(ctx.child_root))
        raise
    except Exception as _e:  # noqa: BLE001
        # v3.0-rc17 (finding живого прогона): исключение провайдера/инфры (напр. HTTP 429 kimi ПОСЛЕ
        # исчерпания ретраев) НЕ должно ронять CLI traceback'ом — как в sequential (rc12/rc16), одиночный
        # прогон обязан вернуть ЧЕСТНЫЙ error-отчёт (exit 2), а не падать. Типизируем сбой ниже.
        from ai_ops_kit.engine.ai_ops_run_lifecycle import _run_start_audience
        with contextlib.redirect_stdout(sys.stderr):
            active_work.finish_cmd(aw_path, fid, status="blocked", child_root=ctx.child_root,  # #695
                                   reason=f"прогон упал: {type(_e).__name__}",
                                   audience=_run_start_audience(ctx.child_root))
        try:
            from ai_ops_kit.engine.workpackage_executor import _classify_failure
            _fail = _classify_failure(_e)
        except Exception:  # noqa: BLE001
            _fail = {"failure_class": "engine", "exception_type": type(_e).__name__,
                     "message": str(_e)[:400], "retryable": False}
        # v3.8.3-rc3: пометить исход текущей попытки в trace (провайдерный сбой) — видно на 429 и т.п.
        if isinstance(ctx.model_resolution, dict) and ctx.model_resolution.get("model_attempts"):
            _la = ctx.model_resolution["model_attempts"][-1]
            if _la.get("outcome") == "pending":
                _la["outcome"] = ("provider_%s" % _fail.get("failure_class")
                                  if _fail.get("retryable") else "error:" + str(_fail.get("failure_class")))
        _eff_e = ctx.model_resolution.get("effective_model") if isinstance(ctx.model_resolution, dict) else None
        err_rep = {"schema_version": 1, "kind": "execution-pipeline", "status": "error",
                   "workitem_id": fid, "error": f"{_fail['exception_type']}: {_fail['message']}",
                   "failure": _fail, "ready_for_pr": False, "not_yet": [],
                   "runtime": ctx.runtime, "engine": "pipeline", "provider": ctx.provider_name,
                   "model": _eff_e or ctx.model,
                   "initial_model": (ctx.model_resolution.get("initial_model") if isinstance(ctx.model_resolution, dict) else None),
                   "effective_model": _eff_e,
                   "model_resolution": ctx.model_resolution if isinstance(ctx.model_resolution, dict) else None}
        # v3.0-rc20 (finding аудита P1): DURABLE failure evidence — не только вернуть отчёт, но и
        # ЗАПИСАТЬ свежий run-report.json + failure-handoff, иначе на диске остаётся старый отчёт/
        # handoff прошлого прогона (пользователь думает, что evidence свежее). next_action — безопасный.
        try:
            _safe = ("retry прогон (сбой транзиентный: провайдер/сеть)"
                     if _fail.get("retryable") else
                     "разобрать сбой перед повтором (вероятен дефект/невалидный ввод — не транзиент)")
            _ls.durable_write_json(ctx.features_dir / fid / "run-report.json", err_rep)   # v3.0.14 (#2)
            _hf = {"schema_version": 1, "kind": "run-handoff", "workitem_id": fid,
                   "status": "error", "failure": _fail, "retryable": bool(_fail.get("retryable")),
                   "next_action": _safe}
            # v3.0.12: durable failure-handoff (атомарно) — чтобы не оставить наполовину записанный
            # или устаревший handoff прошлого прогона, который resume принял бы за свежий.
            _ls.durable_write(ctx.features_dir / fid / "run-handoff.yaml", _hf,
                              require_keys=("kind", "workitem_id"))
            err_rep["run_report"] = f"features/{fid}/run-report.json"
            err_rep["handoff"] = {"next_action": _safe}
        # СРЕЗ engine РАТЧЕТА 2026-08-12. Решение «запись evidence не маскирует исходный сбой»
        # остаётся верным: подменять причину падения ошибкой записи нельзя. Но у него не было
        # второй половины. Комментарий ВЫШЕ сам называет цену: не записали свежий отчёт/handoff
        # — «на диске остаётся старый отчёт прошлого прогона, пользователь думает, что evidence
        # свежее». При `pass` происходило ровно это, и МОЛЧА: `err_rep` даже не упоминал, что
        # обещанные им `run_report`/`handoff` на диск не легли. Теперь упоминает.
        except Exception as _we:  # noqa: BLE001 — исходный сбой важнее сбоя записи, но утрата видна
            _note_bookkeeping_error(err_rep, "failure_evidence.write", _we)
        _ls.merge_bookkeeping_losses(err_rep)
        return None, err_rep
    return rep, None


def _resolve_run_base(ctx, base):
    """v3.0.8/3.0.9 (P0.1/P0.2): base -> КОНКРЕТНАЯ ВЕТКА один раз + полный BaseBinding; явная
    несуществующая base -> ранний отказ (0 model calls). Возвращает (base, base_binding, err)."""
    from ai_ops_kit.engine import execution_pipeline
    child_root, feature = ctx.child_root, ctx.feature
    _brr = execution_pipeline._resolve_base(child_root, base)
    if _brr.get("mode") == "explicit" and not _brr.get("resolved"):
        return base, None, {"schema_version": 1, "kind": "execution-pipeline",
                "workitem_id": feature or "?",
                "status": "error", "ready_for_pr": False,
                "error": (f"base-preflight: явная база '{base}' не разрешается в ветку "
                          f"({_brr.get('reason')}) — прогон не запущен (0 вызовов модели)"),
                "base_binding": {k: _brr.get(k) for k in ("base_ref", "base_sha", "mode", "source")}}
    if _brr.get("resolved"):
        base = _brr.get("base_ref")   # конкретная ветка -> в run-settings, resume_preflight, pipeline
    base_binding = {"kind": "BaseBinding",
                    "base_ref": _brr.get("base_ref") or base, "base_sha": _brr.get("base_sha"),
                    "mode": _brr.get("mode"), "source": _brr.get("source")}
    return base, base_binding, None


def _build_run_proposers(ctx, proposer, reviewer_proposer, author_proposer):
    """v3.7.12/v2.83/v2.86: собрать writer/reviewer/author-предложителей (writer ≠ judge); судья
    готовится всегда при живом провайдере. Кладёт prop/rev_prop/auth_prop в ctx; возвращает `_uctx`
    (обёртка call-context, её читает вынесенный fix-loop: role/trigger/provider/runtime)."""
    from ai_ops_kit.engine import tool_loop
    from ai_ops_kit.providers import orchestrator
    runtime, provider_name = ctx.runtime, ctx.provider_name
    _writer_model, _writer_prov = ctx.writer_model, ctx.writer_prov
    _rev_model, _rev_prov = ctx.rev_model, ctx.rev_prov
    _model_resolution = ctx.model_resolution

    def _uctx(_prov, _role, _trigger, _prov_name):   # ставит call-context -> _record_call в UsageRecord
        if _prov is None:
            return None
        def _w(_prompt):
            orchestrator.set_call_context(role=_role, trigger=_trigger, provider=_prov_name, runtime=runtime)
            return _prov(_prompt)
        return _w
    _wname = ((_model_resolution or {}).get("writer") or {}).get("provider") or provider_name
    _rname = ((_model_resolution or {}).get("reviewer") or {}).get("provider") or provider_name
    prop = proposer or tool_loop.make_model_proposer(
        _uctx(_writer_prov or orchestrator.make_provider(provider_name, _writer_model), "implementation", "initial", _wname))
    # v2.83/v3.7.12: независимый ревьюер — ОТДЕЛЬНЫЙ провайдер (writer ≠ judge на уровне вызова).
    rev_prop = reviewer_proposer
    if rev_prop is None and provider_name != "mock":
        if ctx.review:
            # путь ревью гейтов: недоступный провайдер судьи — ошибка прогона, как и было
            rev_prop = _uctx(_rev_prov or orchestrator.make_provider(provider_name, _rev_model),
                             "code_review", "review", _rname)
        else:
            # путь сверки критериев: отсутствие судьи не роняет прогон (сверка скажет «недоступен»)
            try:
                rev_prop = _uctx(_rev_prov or orchestrator.make_provider(provider_name, _rev_model),
                                 "code_review", "review", _rname)
            except (SystemExit, Exception):   # noqa: BLE001 — «судьи нет» называется в отчёте сверки
                rev_prop = None
    # v2.86: author-модель для артефактов requirements/plan (отдельный вызов провайдера).
    auth_prop = author_proposer
    if ctx.author and auth_prop is None and provider_name != "mock":
        auth_prop = _uctx(_writer_prov or orchestrator.make_provider(provider_name, _writer_model), "implementation", "initial", _wname)
    ctx.prop, ctx.rev_prop, ctx.auth_prop = prop, rev_prop, auth_prop   # fix-loop читает/перевязывает через ctx
    return _uctx


def _run_preflight(ctx, fid, plan, bundle, payload, spec_cov, work_pkg,
                   lifecycle_errors, reevaluate_only, provider_resolution):
    """v2.115 Preflight Truth: проверки ДО запуска модели (fresh и resume). Возвращает
    (pretruth, blocked_report): report != None -> ранний blocked-preflight (durable, 0 вызовов)."""
    from ai_ops_kit.gates import preflight as _pf
    signals, child_root, features_dir = ctx.signals, ctx.child_root, ctx.features_dir
    runtime, provider_name, model = ctx.runtime, ctx.provider_name, ctx.model
    pretruth = _pf.assess(signals, child_root, fid, plan=plan, bundle=bundle, payload=payload,
                          spec_cov=spec_cov, work_pkg=work_pkg, lifecycle_errors=lifecycle_errors,
                          author=ctx.author, reevaluate_only=reevaluate_only)
    (features_dir / fid / "preflight.yaml").write_text(
        yaml.safe_dump(pretruth, allow_unicode=True, sort_keys=False), encoding="utf-8")
    # #633: blocked-preflight — вызов человека, у которого раньше не было durable-дома (жил только в
    # run-report). Кладём его в шину внимания, чтобы `inbox` его показал; при прохождении — снимаем.
    from ai_ops_kit.lifecycle import attention_bus as _ab
    _att_key = f"preflight:{fid}"
    if not pretruth["blocked"]:
        with contextlib.suppress(Exception):
            _ab.resolve(child_root, _att_key)
        return pretruth, None
    with contextlib.suppress(Exception):
        _ab.record(child_root, key=_att_key, source="прогон: preflight",
                   reason="работа остановлена до запуска модели: " + "; ".join(pretruth["reasons"]),
                   kind=_ab.BLOCKED, work_id=fid)
    rep = {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": fid,
           "status": "blocked", "engine": "pipeline", "runtime": runtime,
           "provider": provider_name, "model": model, "ready_for_pr": False,
           "overall_status": "blocked-preflight",
           "error": "preflight не пройден (модель не запускалась, правок/коммита нет): "
                    + "; ".join(pretruth["reasons"]),
           "preflight": pretruth,
           "loop": None, "commit": {"sha": None},   # честно: ни петли, ни коммита
           "not_yet": pretruth["reasons"],
           "profile": _profile_for_report(child_root),   # P1-3: даже блок честно показывает стек
           "provider_resolution": dict(provider_resolution) if provider_resolution else None,
           "lifecycle": {"workitem": f"features/{fid}/workitem.yaml",
                         "run_plan": f"features/{fid}/run-plan.yaml",
                         "preflight": f"features/{fid}/preflight.yaml"}}
    if lifecycle_errors:
        rep["lifecycle_errors"] = lifecycle_errors
    _ls.merge_bookkeeping_losses(rep)   # утраты записей журнала — ДО записи отчёта на диск
    _ls.durable_write_json(features_dir / fid / "run-report.json", rep)   # v3.0.14 (#2): атомарно
    return pretruth, rep


def exit_code(r):
    """Код возврата CLI по отчёту (finding аудита P0.1: раньше всегда 0).

    pipeline: 2 при status=error, 1 если не ready_for_pr (гейты/петля/коммит не сошлись), 0 если ready.
    controller: 1 при status=blocked, 0 иначе (planned/done — успешная транзакция).
    """
    if r.get("kind") == "execution-pipeline":
        if r.get("status") == "error":
            return 2
        if r.get("status") == "blocked":   # v2.115: preflight не пройден — не ready, но не ошибка исполнения
            return 1
        # v3.0.11 (finding аудита P1): завершённый прогон несёт overall_status (delivered|delivery-failed|
        # error), НЕ top-level status. Прежде exit_code читал только status -> None -> падал на
        # ready_for_pr=True -> код 0 даже при delivery-failed (--open-pr не доставил PR, а CI видел успех).
        _ov = r.get("overall_status")
        if _ov == "error":
            return 2
        if _ov == "delivery-failed":   # ready, но PR НЕ доставлен (нет origin/unverifiable/ошибка pr_open)
            return 1
        return 0 if r.get("ready_for_pr") else 1
    return 1 if r.get("status") == "blocked" else 0


def _build_run_arg_parser():
    """Собрать argparse ai_ops_run (подкоманды run/resume) — тело main() без разбора аргументов."""
    ap = argparse.ArgumentParser(prog="ai_ops_run.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("run")
    rp.add_argument("task"); rp.add_argument("child_root")
    rp.add_argument("--signals", default="{}")
    rp.add_argument("--features-dir")
    rp.add_argument("--runtime", default="claude-code")
    # v3.28.x (P0-1): дефолта `mock` больше НЕТ — без явного флага провайдера выбирает резолв
    # (orchestrator_providers.resolve_provider) и печатает решение до прогона. Явный --provider
    # (в т.ч. `mock`) всегда побеждает; автовыбор работает только при --execute.
    rp.add_argument("--provider", default=None,
                    help="провайдер (mock|anthropic|openai|openai-compatible|claude-cli|qwen|"
                         "deepseek|kimi). Без флага при --execute — авторезолв: .ai-ops.yaml + ключ "
                         "в env -> claude в PATH -> mock (с предупреждением). "
                         "AI_OPS_PROVIDER_AUTORESOLVE=0 выключает авторезолв")
    rp.add_argument("--session", default="cli")
    rp.add_argument("--execute", action="store_true")
    rp.add_argument("--feature", help="имя существующей фичи — привязать WorkItem к ней "
                                      "(иначе wi-<hash>; срезы истории не накопятся на одну фичу)")
    rp.add_argument("--engine", default="pipeline", choices=["pipeline", "controller"],
                    help="pipeline (КАНОНИЧЕСКИЙ путь доставки по умолчанию: detect->tool-loop->evidence->гейты->PR) "
                         "или controller (план+каркас/оркестрация именованных агентов — явная альтернатива)")
    rp.add_argument("--model", help="ID модели для провайдера (напр. deepseek-chat); engine=pipeline")
    rp.add_argument("--open-pr", action="store_true",
                    help="открыть draft PR по результату (нужен GITHUB_TOKEN); engine=pipeline")
    rp.add_argument("--takeover", action="store_true",
                    help="перенять брошенную/устаревшую заявку на работу или ветку (run/resume)")
    rp.add_argument("--takeover-reason", default=None, help="причина переятия (для атрибуции)")
    rp.add_argument("--context-shadow", action="store_true",
                    help="построить Context Engine v2 shadow-view рядом с боевым v1 (наблюдаемость "
                         "перед промоушеном; execution по-прежнему на v1); engine=pipeline")
    rp.add_argument("--context-hybrid", action="store_true",
                    help="собрать hybrid-контекст (mandatory v1 + разрешённые v2-additions) через "
                         "context_promotion_gate; не готов -> v1-only; запись в отчёт; engine=pipeline")
    rp.add_argument("--baseline-diff", action="store_true",
                    help="судить по 'нет новых провалов против базы' (пред-существующие красные "
                         "проверки репо не блокируют); engine=pipeline")
    rp.add_argument("--require-fix", action="store_true",
                    help="для fix-задач: ready требует, чтобы правка РЕАЛЬНО починила падавшую "
                         "проверку (fixed непустой), а не только 'не сломала'; engine=pipeline+baseline-diff")
    rp.add_argument("--max-steps", type=int, default=40,
                    help="потолок шагов tool-loop (по умолчанию 40; reasoning-моделям нужен "
                         "запас на цикл понять->починить->проверить->done); engine=pipeline")
    rp.add_argument("--discard", action="store_true",
                    help="перезаписать worktree/ветку прошлого прогона того же --feature, даже "
                         "если там есть несохранённые коммиты (по умолчанию — остановка, чтобы "
                         "не потерять работу); engine=pipeline+isolate")
    rp.add_argument("--sandbox", action="store_true",
                    help="containment (v2.81): shell модели — только по allowlist dev-инструментов "
                         "(произвольный shell выключен), сетевые бинарники и git push из петли "
                         "запрещены; доставка PR — только движком. Полная FS/сеть/ресурс-изоляция — "
                         "контейнерный runtime; engine=pipeline")
    rp.add_argument("--review", action="store_true",
                    help="full RunPlan (v2.83): постадийный НЕЗАВИСИМЫЙ ревью ai-review гейтов "
                         "(code_review/ux_review/...) — отдельный вызов модели под read-only "
                         "политикой выносит структурный вердикт (writer ≠ judge). Артефакт-гейты "
                         "(requirements/spec/plan) и human-approval ревьюер НЕ закрывает; "
                         "engine=pipeline, нужна живая модель (не mock)")
    rp.add_argument("--author", action="store_true",
                    help="product authoring (v2.86): движок производит артефакты requirements/plan "
                         "(отдельный вызов модели) и подтверждает их ФОРМУ детерминированно -> "
                         "закрывает артефакт-гейты requirements/plan_readiness. Качество судит "
                         "ревьюер (--review)/человек. specification (OpenSpec) не входит; нужна "
                         "живая модель (не mock)")
    rp.add_argument("--fix-attempts", type=int, default=1,
                    help="v3.1.1 fix-loop: сколько раз вернуть блокеры ревью/провалившихся проверок "
                         "писателю на итерацию поверх той же ветки, пока не pass (0 = однопроходно, "
                         "как раньше). fail-closed: бюджет исчерпан и не ready -> честный блок. Не для mock.")
    rp.add_argument("--reevaluate-only", action="store_true", dest="reevaluate_only",
                    help="v3.8.3: ПЕРЕОЦЕНИТЬ гейты существующей фичи БЕЗ переавторинга (0 model-вызовов, "
                         "план/SHA стабильны) — для случая «человек добавил ApprovalRecord»: security "
                         "закрывается человеком -> ready -> доставка. Нужен --execute + --feature. engine=pipeline")
    rp.add_argument("--json", action="store_true")
    # v2.99: resume — продолжить WorkItem по последнему RunHandoff (не начинать заново)
    # v2.109 Real Resume: с --execute РЕАЛЬНО продолжает tool-loop поверх ветки/worktree прошлого
    # прогона (не рестарт); без --execute — только preflight (что продолжим, нужна ли ревалидация).
    rs = sub.add_parser("resume")
    rs.add_argument("child_root"); rs.add_argument("feature")
    rs.add_argument("--base", default=None); rs.add_argument("--json", action="store_true")
    rs.add_argument("--session", default="cli")  # #695: intent-CLI подаёт измеренную личность, как run
    rs.add_argument("--task", help="задача-продолжение (по умолчанию — next_action из RunHandoff)")
    rs.add_argument("--signals", default="{}")
    rs.add_argument("--execute", action="store_true",
                    help="РЕАЛЬНО продолжить прогон (tool-loop поверх ветки прошлого прогона); "
                         "без флага — только preflight")
    rs.add_argument("--force", action="store_true",
                    help="продолжить, даже если нужна ревалидация (база/состояние изменились) — "
                         "осознанное решение человека")
    # resume НЕ автовыбирает провайдера (продолжение прогона не должно менять исполнителя молча):
    # без флага — прежний офлайн-дефолт mock.
    rs.add_argument("--provider", default=None)
    rs.add_argument("--model", help="ID модели для провайдера (напр. deepseek-chat)")
    # #695: resume доводит готовую-на-ветке работу до ОТКРЫТОГО PR и снимает брошенную заявку.
    rs.add_argument("--open-pr", action="store_true",
                    help="открыть/обновить draft PR по результату (нужен GITHUB_TOKEN)")
    rs.add_argument("--takeover", action="store_true",
                    help="перенять брошенную/устаревшую заявку на работу или ветку")
    rs.add_argument("--takeover-reason", default=None, help="причина переятия (для атрибуции)")
    rs.add_argument("--replan", action="store_true",
                    help="осознанно сменить классификацию/policy при продолжении (не resume, а replan "
                         "с ревалидацией) — иначе смена task_type/risk/write_scope блокируется")
    # #403/deliver-only: intent-CLI прокидывает `--reevaluate-only` в resume, когда владелец зовёт
    # `ai-ops resume --deliver-only` (доставить готовый READY-коммит без переавторинга). Подкоманда
    # resume этот флаг НЕ объявляла — argparse падал «unrecognized arguments: --reevaluate-only», и
    # весь deliver-only-путь resume был мёртв (объявлен CLI, не исполнялся движком). Объявляем здесь.
    rs.add_argument("--reevaluate-only", action="store_true", dest="reevaluate_only",
                    help="доставить готовый READY-коммит фичи БЕЗ переавторинга (0 model-вызовов, "
                         "HEAD как committed_sha): переоценить гейты и открыть/обновить PR. "
                         "Прокидывается из `ai-ops resume --deliver-only`. Нужен --execute + --feature")
    return ap
