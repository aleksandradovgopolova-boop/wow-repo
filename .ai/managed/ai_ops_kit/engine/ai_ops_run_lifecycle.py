#!/usr/bin/env python3
"""Жизненный цикл прогона ai-ops run: старт lifecycle, resume-гейт, commit-барьер,
доставка за барьером и финализация (стоимость + статус работы), а также вынесенные из
god-модуля пробируемые хелперы delivery/resume/provider: восстановление policy на resume
(`_restore_resume_policy`), резолв моделей по роли (`_resolve_models`), регистрация
active-work (`_register_active_work`), реконсиляция незавершённой доставки
(`_reconcile_pending_delivery`) и отказ прогона без живого провайдера (`live_provider_refusal`).

Вынесено из god-модуля `ai_ops_run` без изменения поведения (чистый перенос + ре-экспорт).
Зависимости берутся из РЕАЛЬНЫХ домов (shared/lifecycle/engine/gates/governance), а не из
ai_ops_run — иначе получился бы циклический импорт. Хелперы, оставшиеся в ai_ops_run
(`_resume_context_from_handoff`, `_note_bookkeeping_error`, `_outbox_dir`, `_unresolved_intents`,
`_nonfinal_receipt_intents`, `is_service_text`, `product_task_for_resume`, `_provider_trust`,
`_load_klp_by_env`, `_with_provider_fallback`), подтягиваются лениво внутри тела функций —
это же сохраняет их патчабельность из тестов (`ai_ops_run.<name>`).
"""
from __future__ import annotations

import contextlib
import sys

from ai_ops_kit.shared import work_areas as _work_areas       # noqa: E402
from ai_ops_kit.lifecycle import active_work        # noqa: E402
from ai_ops_kit.shared import lifecycle_store as _ls   # noqa: E402


def _run_start_audience(child_root):
    """Аудитория для служебных строк СТАРТА прогона (active-work/worktree). #708: на product их
    id-насыщенная форма — внутренний шум; берём из communication-policy дочки (default product).
    Сбой резолва не роняет прогон — падаем на technical (полная форма)."""
    try:
        from ai_ops_kit.ui import presenter
        return presenter.audience_from_config(child_root)
    except Exception:  # noqa: BLE001
        return "technical"


# Стадии прогона (пробо-свободная передняя половина) вынесены в сателлит для снятия god-модуля
# с потолка module-size. Ре-экспорт держит публичную поверхность: `ai_ops_run` и тесты
# резолвят эти имена из ЭТОГО модуля (`ai_ops_run_lifecycle.<name>`) как раньше. Это
# ЕДИНСТВЕННОЕ module-level ребро фасад->сателлит; обратно сателлит тянет только лениво.
from ai_ops_kit.engine.ai_ops_run_lifecycle_stages import (   # noqa: F401,E402
    _commit_barrier, _start_lifecycle, _resume_gate, _finalize_run_cost, _finalize_run,
    _deliver)


def live_provider_refusal(res, explicit):
    """F-026 (поле 2026-08-15, дочка ai-ops-cockpit): исполняющий прогон с заглушкой — ложный green.

    `resume --execute` уходил в `mock`: правок продукта ноль, а отчёт говорил `resumed=True`, и
    отличить это от работы можно было только в `--json` («provider»: «mock»). Печати решения мало:
    прогон, который НЕ ВЫЗЫВАЕТ модель, не должен доводиться до вердикта и коммита служебных файлов.
    Поэтому: живого нашли — идём; не нашли — ОТКАЗ с названной причиной. Офлайн остаётся доступен,
    но становится осознанным (`--provider mock`).

    Отказ только для случая `source == "fallback"` — автовыбор реально искал и не нашёл. Явный
    выбор человека и выключенный автовыбор (`AI_OPS_PROVIDER_AUTORESOLVE=0`, pytest/CI —
    офлайн-детерминизм) остаются как были. -> текст отказа или None."""
    if explicit or not isinstance(res, dict):
        return None
    if res.get("provider") != "mock" or res.get("source") != "fallback":
        return None
    checked = "; ".join(res.get("checked") or []) or "проверять было нечего"
    return ("живого провайдера не нашлось, а с заглушкой (mock) прогон не вызывает модель и правок "
            "не делает — отчёт об успехе был бы ложным. Проверено: " + checked
            + ". Дайте живого: ключ провайдера в окружении и `providers.default` в .ai-ops.yaml, "
              "либо локальный `claude` в PATH. Нужен именно офлайн — попросите его прямо: "
              "`--provider mock`.")


def _reconcile_pending_delivery(features_dir, fid, child_root):
    """v3.0.16/v3.0.17 (finding аудита #2/P0): сверить с remote КАЖДУЮ незавершённую доставку (Intent без
    Receipt) и дописать DeliveryReceipt — но ТОЛЬКО при СТРОГОМ совпадении идентичности PR с Intent
    (repository + head.sha == commit_sha + base.ref). PR той же ветки, но с ДРУГИМ коммитом НЕ
    засчитывается за подтверждение старой доставки. Все записи — обязательные барьеры (реконсиляция НЕ
    рапортует успех, если Receipt фактически не сохранился). Идемпотентно, ничего не создаёт на remote.
    -> список исходов по delivery_id | None (нечего сверять)."""
    from pathlib import Path as _P
    # _outbox_dir/_unresolved_intents/_nonfinal_receipt_intents остаются в ai_ops_run (первые два
    # нужны и forbidden-функции/тестам) — ленивый импорт, чтобы не замкнуть импорт-граф.
    from ai_ops_kit.engine.ai_ops_run import (
        _outbox_dir, _unresolved_intents, _nonfinal_receipt_intents)
    # незавершённые (Intent без Receipt) + #400: не-финальные receipt (sha_verified != True) —
    # ложный false из гонки P0 больше не залипает, а перепроверяется против свежего remote.
    pending = _unresolved_intents(features_dir, fid) + _nonfinal_receipt_intents(features_dir, fid)
    if not pending:
        return None
    from ai_ops_kit.delivery import pr_open
    d = _outbox_dir(features_dir, fid)
    jn = _P(features_dir) / fid / "lifecycle-journal.jsonl"
    results = []
    for did, intent in pending:
        rp = d / f"{did}.receipt.yaml"
        branch = intent.get("branch")
        try:
            rc = pr_open.reconcile_delivery(child_root, branch)
        except Exception as e:  # noqa: BLE001
            results.append({"delivery_id": did, "status": "unavailable", "reason": str(e)})
            continue
        _base = {"schema_version": 1, "kind": "DeliveryReceipt", "delivery_id": did, "workitem_id": fid,
                 "repository": intent.get("repository"), "branch": branch,
                 "commit_sha": intent.get("commit_sha"), "base_ref": intent.get("base_ref"),
                 "reconciled": True}
        if rc.get("status") == "unavailable":
            results.append({"delivery_id": did, "status": "unavailable"})   # оставляем на следующий прогон
            continue
        if rc.get("status") == "absent":
            _w = _ls.durable_write(rp, {**_base, "status": "not-delivered", "remote_sha": None},
                                   require_keys=("kind", "delivery_id", "status"))
            results.append({"delivery_id": did, "status": "reconciled-absent" if _w.get("ok")
                            else "receipt-write-failed"})
            continue
        # rc.status == found: СТРОГАЯ сверка идентичности (не доверяем имени ветки)
        _idn = (rc.get("repository") == intent.get("repository")
                and rc.get("head_sha") == intent.get("commit_sha")
                and rc.get("base_ref") == intent.get("base_ref"))
        if not _idn:
            # PR ветки есть, но это НЕ та доставка (другой SHA/base/repo) -> НЕ подтверждаем старую.
            _w = _ls.durable_write(rp, {**_base, "status": "mismatch", "remote_sha": rc.get("head_sha"),
                                        "remote_base_ref": rc.get("base_ref"),
                                        "remote_repository": rc.get("repository"), "sha_verified": False,
                                        "pr_url": rc.get("url"), "pr_number": rc.get("number")},
                                   require_keys=("kind", "delivery_id", "status"), keep_backup=True)
            results.append({"delivery_id": did, "status": "mismatch" if _w.get("ok")
                            else "receipt-write-failed", "remote_sha": rc.get("head_sha")})
            continue
        # R-41: `sha_verified` отвечает на вопрос «это наш коммит», и только на него. Отдельно
        # записываем, ПРОВЕРЯЛ ли доставку кто-нибудь: ноль прогонов больше не выглядит как зелёный.
        # Поля-факты (`checks_status`/`total`/`failed`) и поле-вердикт (`checks_verified`) пишутся из
        # одного источника — `pr_open.checks_verified()`, чтобы вердикт нельзя было проставить мимо фактов.
        _chk = rc.get("checks") or {"status": "unavailable"}
        _w = _ls.durable_write(rp, {**_base, "status": "reconciled", "remote_sha": rc.get("head_sha"),
                                    "sha_verified": True, "pr_url": rc.get("url"),
                                    "pr_number": rc.get("number"), "pr_state": rc.get("pr_state"),
                                    "merged": rc.get("merged"),
                                    "checks_status": _chk.get("status"),
                                    "checks_total": _chk.get("total"),
                                    "checks_failed": _chk.get("failed"),
                                    "checks_verified": pr_open.checks_verified(_chk)},
                               require_keys=("kind", "delivery_id", "status"), keep_backup=True)
        if not _w.get("ok"):
            results.append({"delivery_id": did, "status": "receipt-write-failed"})   # НЕ рапортуем успех
            continue
        _ls.journal_append(jn, {"kind": "delivery_reconciled", "run_id": fid, "workitem_id": fid,
                                "delivery_id": did, "pr_url": rc.get("url"), "remote_sha": rc.get("head_sha")})
        results.append({"delivery_id": did, "status": "reconciled", "pr_url": rc.get("url")})
    return results


def _register_active_work(child_root, signals, write_scope, fid, session, lifecycle_errors,
                          takeover=False, takeover_reason=None):
    """Регистрация active-work + concurrency-preflight (координация параллельных сессий).
    K6: вынесено из run() без изменения поведения. -> (aw_path, preflight, error|None)."""
    aw_path = child_root / ".ai" / "runtime" / "active-work.yaml"
    # v3.0.12 (finding аудита блок B): общий реестр координации повреждён -> FAIL-CLOSED (не стартуем
    # вслепую: пустая карта скрыла бы чужую активную работу и две сессии столкнулись бы). Проверяем
    # ДО preflight/register, чтобы register не наткнулся на corrupt-raise без обработки.
    _awg = _ls.load_guarded(aw_path, kind="active-work")
    if _awg["state"] == "corrupt":
        return None, None, {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": fid,
                "status": "error", "ready_for_pr": False,
                "error": (f"active-work реестр повреждён ({_awg['reason']}) — прогон не начат, чтобы не "
                          "потерять координацию параллельных сессий (пустая карта скрыла бы коллизии). "
                          "Нужна явная recovery .ai/runtime/active-work.yaml.")}
    # ЗАЯВКА #138: здесь стояло `or ["unspecified"]`, а `affected_areas` на одиночном пути в
    # сигналы не кладёт НИКТО — поэтому пересечение зон находилось со ВСЕМИ активными записями
    # сразу (неизвестность считалась совпадением). Зоны выводятся из `write_scope` тем же
    # правилом, что на пакетном пути (`work_areas` — одна формула на оба пути).
    areas = _work_areas.areas_for(signals, write_scope)
    # concurrency preflight ДО регистрации/изменения файлов: пересечения по областям с ДРУГОЙ
    # активной работой (тихо, через classify — без печати и без себя). Advisory в отчёт.
    try:
        _aw = active_work.load(aw_path)
        _conf = active_work.classify(
            [w for w in _aw.get("active", []) if w.get("id") != fid],
            {"id": fid, "affected_areas": list(areas), "depends_on": [], "shared_contracts": []})
        preflight = {"conflicts": _conf}
    except Exception as _pe:  # noqa: BLE001 — preflight не должен ронять прогон...
        # ...но и выглядеть пройденным не должен: при preflight=None отчёт печатал
        # «preflight-конфликтов: 0», то есть заявлял «конфликтов нет» там, где проверки
        # вообще не было. Записываем сбой явно.
        preflight = {"error": f"{type(_pe).__name__}: {_pe}"[:200], "conflicts": None}
    # #661: сигнал «писатель занят + работа независима → годится параллельный субагентный путь».
    # Момент выбран потому, что здесь у нас ОБА факта: неблокирующая проба замка писателя говорит,
    # занят ли он ПРЯМО СЕЙЧАС другим прогоном, а `preflight.conflicts` — независима ли эта работа
    # от активных сессий. Движок только СИГНАЛИТ (субагентов не спавнит) и остаётся при безопасном
    # дефолте — если координатор путь не подхватит, прогон дождётся очереди штатно. Best-effort:
    # сбой сигнала не роняет и не блокирует регистрацию (advisory), но пишется в durable-дом.
    try:
        from ai_ops_kit.providers import orchestrator_providers as _op
        from ai_ops_kit.engine import writer_contention as _wc
        _busy = _op.writer_lock_busy()
        _decision = _wc.assess(_busy, preflight.get("conflicts"))
        if _decision["parallel_viable"]:
            _wc.record(child_root, fid, _decision, areas=list(areas), work_id=fid)
    except Exception as _wce:  # noqa: BLE001 — сигнал advisory: его сбой не роняет прогон...
        # ...но и молча не исчезает: named в lifecycle_errors (как preflight-сбой выше).
        lifecycle_errors.append(f"writer-contention signal: {type(_wce).__name__}: {_wce}"[:200])
    # регистрация активной работы (координация) — человекочитаемые строки в stderr, чтобы
    # stdout оставался чистым для --json.
    # КОД ВОЗВРАТА РЕГИСТРАЦИИ ЧИТАЕТСЯ (замер 18.08.2026). Прежде он отбрасывался в обеих
    # точках вызова: `register` мог отказать (цикл зависимостей, работа в main, нет зон) — и
    # прогон всё равно продолжался. С отказом второй сессии на ту же работу/ветку цена этого
    # молчания стала прямой: заявка потребителя #150 — два PR на одну ветку и выброшенная
    # половина работы. Отказ обязан останавливать прогон ДО правок, а не после.
    _reg_rc = 1
    with contextlib.redirect_stdout(sys.stderr):
        try:
            _reg_rc = active_work.register(aw_path, fid, f"ai-ops/{fid}", areas, session,
                                           workitem=f"features/{fid}/workitem.yaml",
                                           child_root=child_root,
                                           takeover=takeover, takeover_reason=takeover_reason,
                                           published=active_work.publication_enabled(child_root),
                                           audience=_run_start_audience(child_root))
        except active_work.ActiveWorkCorrupt as _e:   # v3.0.12: сбой durable-записи реестра не молчит
            lifecycle_errors.append(f"active-work register: {_e}")
            _reg_rc = 0        # сбой записи реестра уже назван выше — не путать его с отказом
    if _reg_rc:
        return None, None, {"schema_version": 1, "kind": "run-report", "workitem_id": fid,
                "status": "blocked",
                "blocked_by": "active-work",
                "error": ("работа не начата: заявку на эту работу или ветку держит другая сессия "
                          "(причина и держатель названы выше). Перенять её можно осознанно — "
                          "`active_work.py register … --takeover --takeover-reason \"почему\"`.")}
    return aw_path, preflight, None


def _restore_resume_policy(ctx, resume):
    """v3.0-rc2 (P0.1) Canonical Resume Context: при resume восстановить ПОЛИТИКУ исходного прогона.

    K6: вынесено из run() без изменения поведения. Мутирует `ctx` (signals/task_type/risk +
    sandbox/baseline_diff/require_fix/author/review/open_pr/write_scope/max_steps/base/task_text/
    saved_task; sandbox здесь — policy enforcement, не security isolation: флаг политики прогона)
    из сохранённого run-settings.yaml — иначе resume молча теряет политику и
    переклассифицирует задачу. provider/model/base приходят от вызывающего (runtime-выбор);
    изменение базы/состояния уже требует явной ревалидации (resume_preflight). -> error-dict | None.

    v3.0-rc4 (P0.1): immutable-resume — ТОЛЬКО для пользовательского resume задачи. Внутренний
    per-package resume executor'а (каждый пакет — своя подсистема/affected_areas, поверх общей
    ветки) НЕ является сменой классификации: executor сам управляет policy пакета. Помечен
    _sequence_internal -> пропускаем drift-проверку и restore run-settings.
    """
    # is_service_text/product_task_for_resume остаются в ai_ops_run (используются тестами и CLI) —
    # ленивый импорт, чтобы не замкнуть импорт-граф и сохранить патчабельность.
    from ai_ops_kit.engine.ai_ops_run import is_service_text, product_task_for_resume
    ctx.saved_task = None    # F-027: продуктовая задача исходного прогона (переживает продолжение)
    if resume and ctx.feature and not ctx.signals.get("_sequence_internal"):
        _sp = ctx.features_dir / ctx.feature / "run-settings.yaml"
        # v3.0.12 (finding аудита блок B): FAIL-CLOSED чтение. Прежде safe_load(...) or {} трактовал
        # битый/пустой run-settings как «отсутствует» -> resume тихо откатывался к дефолтам вызова
        # (терял классификацию/policy/BaseBinding) И перезаписывал файл дефолтами (контракт исходного
        # прогона уничтожался навсегда). Теперь: повреждён -> явный отказ (не дефолт, не перезапись).
        _g = _ls.load_guarded(_sp, required_keys=("kind", "policy"), kind="run-settings")
        if _g["state"] == "corrupt":
            return {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": ctx.feature,
                    "status": "error", "ready_for_pr": False,
                    "error": (f"run-settings повреждён ({_g['reason']}) — resume не может восстановить "
                              "policy/классификацию исходного прогона. Нужна явная recovery (не тихий "
                              "дефолт: иначе прогон переклассифицируется и перезапишет контракт)."),
                    "resume": {"requested": True, "resumed": False}}
        if _g["state"] == "ok":
            _saved = _g["data"]
            _ss, _pp = (_saved.get("signals") or {}), (_saved.get("policy") or {})
            if isinstance(_saved.get("task"), str) and _saved["task"].strip():
                ctx.saved_task = _saved["task"]
            # v3.0-rc4 (P0.1) IMMUTABLE resume: resume НЕ меняет классификацию/policy. Если новый
            # вызов пытается переопределить routing-сигнал (task_type/risk/size/affected_areas) или
            # write_scope значением, отличным от сохранённого — это НЕ resume, а replan: требуется
            # явный replan=True (+ ревалидация). Иначе можно было бы тихо продолжить ENGINEERING как QUICK.
            _POLICY_KEYS = ("task_type", "risk", "size", "affected_areas")
            _drift = [k for k in _POLICY_KEYS
                      if k in ctx.signals and k in _ss and ctx.signals[k] != _ss[k]]
            if ctx.write_scope is not None and _pp.get("write_scope") is not None \
                    and ctx.write_scope != _pp.get("write_scope"):
                _drift.append("write_scope")
            if _drift and not ctx.replan:
                return {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": ctx.feature,
                        "status": "error", "ready_for_pr": False,
                        "error": ("resume не меняет классификацию/policy исходного прогона "
                                  f"(drift: {', '.join(_drift)}). Это replan — запусти с replan=True "
                                  "(ревалидация + новый план), а не resume."),
                        "resume": {"requested": True, "resumed": False, "drift": _drift}}
            # восстанавливаем СОХРАНЁННУЮ policy как источник истины (не «or», а точное значение),
            # кроме случая replan, где новый вызов осознанно задаёт новую policy.
            if not ctx.replan:
                ctx.signals = {**ctx.signals, **_ss}          # saved policy побеждает
                ctx.sandbox = bool(_pp.get("sandbox", ctx.sandbox))
                ctx.baseline_diff = bool(_pp.get("baseline_diff", ctx.baseline_diff))
                ctx.require_fix = bool(_pp.get("require_fix", ctx.require_fix))
                ctx.author = bool(_pp.get("author", ctx.author))
                ctx.review = bool(_pp.get("review", ctx.review))
                ctx.open_pr = bool(_pp.get("open_pr", ctx.open_pr))
                ctx.write_scope = _pp.get("write_scope") if ctx.write_scope is None else ctx.write_scope
                if ctx.max_steps == 40 and _pp.get("max_steps"):
                    ctx.max_steps = _pp["max_steps"]
                # v3.0.2/v3.0.9 (P0): base восстанавливается из saved BaseBinding (точная база исходного
                # запуска), с фолбэком на плоское поле base (совместимость со старыми run-settings).
                ctx.base = ((_pp.get("base_binding") or {}).get("base_ref")) or _pp.get("base", ctx.base)
        # F-027: задача исполнителя на продолжении обязана остаться ПРОДУКТОВОЙ. Служебный
        # next_action кита («закрыть незакрытые гейты: …») сюда доезжал как task_text — и автор
        # честно писал требования про гейты кита, заводил под них openspec-изменение и
        # validate_gates.py. Продуктовая спека при этом цела, потому и выглядело осмысленно.
        # Проверка стоит в движке, а не только в CLI: путь resume есть и у прямых вызывающих.
        if is_service_text(ctx.task_text):
            _pt = product_task_for_resume(ctx.child_root, ctx.feature, ctx.features_dir)
            if not _pt["task"]:
                return {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": ctx.feature,
                        "status": "error", "ready_for_pr": False,
                        "error": ("продолжение получило служебный текст кита вместо продуктовой "
                                  f"задачи («{(ctx.task_text or '')[:60]}…»), а восстановить исходную "
                                  "не из чего (нет ни task в run-settings, ни задачи в "
                                  "workitem.yaml, ни раздела goal в спеке). Назовите задачу явно: "
                                  "--task \"<что делаем для продукта>\". Служебное «что осталось» "
                                  "задачей исполнителя не бывает."),
                        "resume": {"requested": True, "resumed": False}}
            ctx.task_text = _pt["task"]
            ctx.signals["task_text"] = ctx.task_text
    return None


def _resolve_models(ctx):
    """v3.7.12 Router->runtime: без явного --model резолвим модель ПО РОЛИ через model_router и
    физически диспатчим на endpoint вендора (provider_endpoints) -> writer≠judge по МОДЕЛИ.

    K6: вынесено из run() без изменения поведения. Мутирует `ctx` (writer/reviewer model+prov,
    model_resolution, sec_qualified, klp_by_env/trust_cache/trust_now/trust_env). Явный --model =
    override (записывается). Всё под fail-safe: нет резолва/ключа/endpoint -> прежнее поведение
    (passthrough --model) + честная запись в отчёт. JIT provider-preflight PRIMARY не пройден ->
    возвращает blocked-preflight-отчёт (fail-closed, provider не строится). -> error-dict | None.
    """
    from ai_ops_kit.providers import orchestrator
    # _load_klp_by_env/_provider_trust/_with_provider_fallback остаются в ai_ops_run (patched тестами,
    # нужны и _execute_with_fix_loop) — ленивый импорт сохраняет патчабельность (`ai_ops_run.<name>`).
    from ai_ops_kit.engine.ai_ops_run import (
        _load_klp_by_env, _provider_trust, _with_provider_fallback)
    ctx.writer_model, ctx.writer_prov, ctx.rev_model, ctx.rev_prov = ctx.model, None, ctx.model, None
    try:
        from ai_ops_kit.providers import model_router as _mr
        from ai_ops_kit.providers import provider_endpoints as _pe
        _plan = _mr.plan_run(signals=ctx.signals)   # v3.9.0-rc3: signals -> preferred_writer_tier
        ctx.model_resolution = {"kind": "ModelResolution", "plan": _plan, "applied": False,
                                "mode": "explicit-override" if ctx.model else "router", "notes": []}
        # v3.8.3-rc3 Dynamic Model Trust: JIT provider-preflight для КАЖДОЙ реально вызываемой модели
        # (primary/reviewer/fallback/escalation), а не только primary+reviewer. Trust-переменные видны
        # и в fix-loop (эскалация проверяет trust там).
        import os as _os
        import datetime as _dt
        ctx.trust_cache = {}
        ctx.klp_by_env = _load_klp_by_env(ctx.child_root)
        ctx.trust_now = _dt.date.today().isoformat()
        ctx.trust_env = dict(_os.environ)
        if ctx.model is None and ctx.provider_name == "openai-compatible":
            impl, rev = _plan.get("implementation") or {}, _plan.get("code_review") or {}
            if impl.get("resolved") and _pe.key_available(impl.get("provider")):
                ep = _pe.endpoint_for(impl["provider"])
                # JIT trust PRIMARY: не готов -> blocked-preflight (fail-closed, как раньше)
                _pt = _provider_trust(impl["provider"], ep["key_env"], ctx.klp_by_env, ctx.trust_env, ctx.trust_now, ctx.trust_cache)
                ctx.model_resolution["key_preflight"] = _pt.get("preflight") or {"ready": _pt["ready"], "blocks": ([] if _pt["ready"] else [_pt.get("reason")])}
                if not _pt["ready"]:
                    ctx.model_resolution["preflight_blocked"] = True
                ctx.writer_model = impl["model_id"]
                ctx.writer_prov = orchestrator.make_openai_provider(impl["model_id"], ep["base_url"], ep["key_env"])
                ctx.model_resolution["applied"] = True
                ctx.model_resolution["initial_model"] = impl["model_id"]
                ctx.model_resolution["effective_model"] = impl["model_id"]   # обновится при эскалации/fallback
                ctx.model_resolution["writer"] = {"model_id": impl["model_id"], "provider": impl["provider"],
                                                  "cost_basis": impl.get("cost_basis")}
                ctx.model_resolution["model_attempts"] = [
                    {"attempt": 1, "model": impl["model_id"], "provider": impl["provider"],
                     "trigger": "initial", "outcome": "pending"}]
                # v3.9.0-rc3 COMPLEXITY-AWARE ROUTING: сложный класс задачи -> сильный executor (Claude
                # Code adapter, claude-cli) СРАЗУ, не cheap-then-fix-loop. Честный fallback: нет локального
                # claude CLI -> остаёмся на дешёвом money-mode writer + пишем причину. Реестр/ключи не нужны
                # (локальная сессия). Escalation-ladder чистим: некуда «эскалировать» сильного вниз на kimi/qwen.
                _tier = _plan.get("preferred_writer_tier") or {}
                if _tier.get("tier") == "strong-executor":
                    # СПРАШИВАЕМ ТЕМ ЖЕ, ЧЕМ ЗАПУСТИМ (замер 18.08.2026). Здесь стоял голый
                    # `shutil.which("claude")`, а `make_claude_cli_provider()` запускает то, что
                    # найдёт `claude_lookup` — то есть путь, названный владельцем в
                    # AI_OPS_CLAUDE_BIN, сильнее PATH. Расхождение давало ровно тот класс, из-за
                    # которого функция и заводилась: рабочий исполнитель назван, но не в PATH ->
                    # «strong executor недоступен» и тихий откат на дешёвого writer'а; битый
                    # названный путь при claude в PATH -> writer выбран, а первый же вызов модели
                    # отказывается работать посреди начатого прогона.
                    if orchestrator.claude_binary():
                        ctx.writer_model = "claude-code-local"
                        ctx.writer_prov = orchestrator.make_claude_cli_provider()
                        ctx.model_resolution["effective_model"] = "claude-code-local"
                        ctx.model_resolution["writer"] = {"model_id": "claude-code-local", "provider": "claude-cli",
                                                          "tier": "strong-executor", "reason": _tier.get("reason")}
                        ctx.model_resolution["model_attempts"][0].update(
                            model="claude-code-local", provider="claude-cli", trigger="complexity-routing")
                        if isinstance(impl, dict):
                            impl["escalation_ladder"] = []   # сильный executor — вниз не даунгрейдим
                        ctx.model_resolution["notes"].append(
                            "complexity-aware: сложный класс -> writer=claude-cli (сильный executor) сразу")
                    else:
                        ctx.model_resolution["strong_executor_unavailable"] = True
                        _look = orchestrator.claude_lookup()
                        ctx.model_resolution["notes"].append(
                            "complexity-aware: класс требует strong-executor, но локальный claude CLI "
                            "недоступен ("
                            + ("назван путь AI_OPS_CLAUDE_BIN, файла нет или он не исполняемый"
                               if _look["where"] == "named" else "в PATH процесса кита не найден")
                            + ") -> честный fallback на money-mode дешёвый writer")
                # reviewer — JIT trust отдельного провайдера (writer≠judge по модели).
                # v3.9.0-rc3: сравниваем с ЭФФЕКТИВНЫМ writer'ом (ctx.writer_model), а не с registry-impl —
                # иначе при complexity-override (writer=claude-cli) deepseek-ревьюер ложно считался
                # «не независим» (deepseek==registry-impl) и откатывался в self-model -> no-verdict.
                _rev_trusted = (rev.get("resolved") and rev.get("model_id") != ctx.writer_model
                                and _pe.key_available(rev.get("provider"))
                                and _provider_trust(rev["provider"], _pe.endpoint_for(rev["provider"])["key_env"],
                                                    ctx.klp_by_env, ctx.trust_env, ctx.trust_now, ctx.trust_cache)["ready"])
                if _rev_trusted:
                    ep2 = _pe.endpoint_for(rev["provider"])
                    ctx.rev_model = rev["model_id"]
                    ctx.rev_prov = orchestrator.make_openai_provider(rev["model_id"], ep2["base_url"], ep2["key_env"])
                    ctx.model_resolution["reviewer"] = {"model_id": rev["model_id"], "provider": rev["provider"], "independent_by_model": True}
                elif (ctx.writer_model == "claude-code-local" and impl.get("resolved")
                      and _pe.key_available(impl.get("provider"))):
                    # v3.9.0-rc3 complexity-routing: writer=claude-cli (сильный executor) -> ревьюер =
                    # ДЕШЁВЫЙ qualified impl-судья (deepseek), независим от claude-cli по модели, даже если
                    # отдельная code_review-роль не резолвится в реестре. Это и есть owner-план review->deepseek.
                    _iep = _pe.endpoint_for(impl["provider"])
                    ctx.rev_model = impl["model_id"]
                    ctx.rev_prov = orchestrator.make_openai_provider(impl["model_id"], _iep["base_url"], _iep["key_env"])
                    ctx.model_resolution["reviewer"] = {"model_id": impl["model_id"], "provider": impl["provider"],
                                                        "independent_by_model": True,
                                                        "reason": "дешёвый qualified судья vs сильный writer=claude-cli"}
                else:
                    ctx.rev_model, ctx.rev_prov = ctx.writer_model, ctx.writer_prov
                    ctx.model_resolution["reviewer"] = {"model_id": ctx.writer_model, "independent_by_model": False,
                                                        "reason": "code_review не резолвится/нет ключа/trust -> self-model review (writer=judge по модели)"}
                    ctx.model_resolution["notes"].append("reviewer=writer по модели: нет отдельной допущенной+trusted модели")
                # v3.8.3-rc2 (#6) PROVIDER FALLBACK на RETRYABLE infra-сбое. rc3: fallback — НЕОБЯЗАТЕЛЬНЫЙ
                # кандидат: JIT trust; НЕ готов -> ИСКЛЮЧАЕМ (не блокируем primary) + пишем причину.
                _fb = impl.get("fallback") or {}
                if _fb.get("model_id") and _fb.get("provider"):
                    _fpt = (_provider_trust(_fb["provider"], _pe.endpoint_for(_fb["provider"])["key_env"],
                                            ctx.klp_by_env, ctx.trust_env, ctx.trust_now, ctx.trust_cache)
                            if _pe.key_available(_fb.get("provider")) else {"ready": False, "reason": "ключ отсутствует в env"})
                    if _fpt["ready"]:
                        try:
                            _fbep = _pe.endpoint_for(_fb["provider"])
                            _fb_prov = orchestrator.make_openai_provider(_fb["model_id"], _fbep["base_url"], _fbep["key_env"])
                            _sw = {"switched_to": None}
                            ctx.writer_prov = _with_provider_fallback(
                                ctx.writer_prov, _fb_prov,
                                on_switch=lambda e, _s=_sw, _m=_fb["model_id"]: _s.update(switched_to=_m))
                            ctx.model_resolution["writer_fallback"] = {
                                "model_id": _fb["model_id"], "provider": _fb["provider"],
                                "trigger": "retryable-infra-failure-only", "switch_state": _sw}
                            if not (ctx.model_resolution.get("reviewer") or {}).get("independent_by_model"):
                                ctx.rev_prov = ctx.writer_prov
                        except Exception as _fbe:  # noqa: BLE001 — сбой построения fallback не роняет прогон
                            ctx.model_resolution["writer_fallback"] = {"error": f"{type(_fbe).__name__}: {_fbe}"[:160]}
                    else:
                        ctx.model_resolution["writer_fallback"] = {
                            "excluded_model": _fb["model_id"], "provider": _fb.get("provider"),
                            "reason": _fpt.get("reason"),
                            "note": "необязательный fallback ИСКЛЮЧЁН по JIT-trust (не блокирует primary)"}
            else:
                ctx.model_resolution["notes"].append("router не применён (implementation не резолвится/нет ключа) -> passthrough --model")
    except Exception as _e:  # noqa: BLE001
        ctx.model_resolution = {"kind": "ModelResolution", "error": str(_e)[:200], "applied": False,
                                "mode": "explicit-override" if ctx.model else "router"}
    # v3.7.3 (#5 flip): security needs_review закрывает ТОЛЬКО КВАЛИФИЦИРОВАННЫЙ security-судья
    # (security_review.resolved в plan_run) ЛИБО человек (ApprovalRecord). Общий code reviewer — НЕТ.
    # Пока qualified security-судьи нет (до Bench v2) -> security needs_review -> pending_human до
    # человеческого ApprovalRecord (реальный human-fallback). Отдельный security_reviewer_proposer.
    ctx.sec_qualified = bool(((ctx.model_resolution.get("plan") or {}).get("security_review") or {}).get("resolved"))
    # v3.7.1 (#4) РЕАЛЬНЫЙ security-барьер: key preflight не пройден (ключ/ротация) -> блок ПРОГОНА
    # (не строим proposer, не зовём провайдера). Честный blocked-preflight-отчёт, ready_for_pr=false.
    if isinstance(ctx.model_resolution, dict) and ctx.model_resolution.get("preflight_blocked"):
        _kpf = ctx.model_resolution.get("key_preflight", {})
        # #633: provider/key preflight не пройден до вызова модели — вызов человека, живший только в
        # run-report. Кладём в шину внимания, чтобы `inbox` его показал (fail-safe, прогон не роняем).
        with contextlib.suppress(Exception):
            from ai_ops_kit.lifecycle import attention_bus as _ab
            _ab.record(ctx.child_root, key=f"provider-preflight:{ctx.feature}",
                       source="прогон: доступ к модели",
                       reason="работа остановлена до вызова модели: "
                              + "; ".join(_kpf.get("blocks", []) or ["ключ/ротация провайдера"]),
                       kind=_ab.BLOCKED, work_id=ctx.feature)
        return {"schema_version": 1, "kind": "execution-pipeline", "status": "blocked-preflight",
                "ready_for_pr": False, "provider": ctx.provider_name, "model": ctx.writer_model,
                "model_resolution": ctx.model_resolution, "key_preflight": _kpf,
                "blocked_reason": "key preflight не пройден до provider-вызова: "
                                  + "; ".join(_kpf.get("blocks", []) or ["ключ/ротация"]),
                "not_yet": ["security key preflight: " + "; ".join(_kpf.get("blocks", []) or ["ключ отсутствует/просрочен"])]}
    return None
