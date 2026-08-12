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
  и гейты (tools/orchestrator.py) — status = done|blocked по evidence.

Аддитивно (2.x): ничего не ломает; `ai-ops run` как ОСНОВНОЙ путь и сплит на пакеты —
цель 3.0.

Использование:
  ai_ops_run.py run "<задача>" <child_root> [--signals '<json>'] [--features-dir dir]
       [--runtime claude-code|generic-orchestrator] [--provider mock] [--model ID]
       [--engine controller|pipeline] [--execute] [--open-pr] [--json]
  ai_ops_run.py --selftest
Код возврата: 0 — успех/ready; 1 — blocked или pipeline не готов к PR; 2 — ошибка прогона.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

import yaml

from ai_ops_kit.shared import _bootstrap  # noqa: E402
from ai_ops_kit.engine import run_plan          # noqa: E402
from ai_ops_kit.engine.pipeline_helpers import work_produced   # noqa: E402
from ai_ops_kit.lifecycle import workitem          # noqa: E402
from ai_ops_kit.lifecycle import active_work       # noqa: E402
from ai_ops_kit.lifecycle import lifecycle_store as _ls   # noqa: E402 — v3.0.12: durable запись/fail-closed чтение resume-артефактов


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
    """
    if not isinstance(rep, dict):
        return
    rep.setdefault("bookkeeping_errors", []).append(
        {"what": what, "error": f"{type(exc).__name__}: {exc}"[:200]})


def _outbox_dir(features_dir, fid):
    from pathlib import Path as _P
    return _P(features_dir) / fid / "delivery-outbox"


# --- профиль стека в отчёте (v3.28.x, review 2026-08-06, P1-3) ---
# Отчёт печатал «стек: не определён» на всех путях, где profile в отчёт не попадал
# (blocked-preflight, ошибка прогона), хотя project_detector отрабатывал верно. Плюс `', '.join(...)`
# упал бы TypeError на СЫРОМ результате detect(): stacks там — список СЛОВАРЕЙ. Обе дыры закрыты:
# профиль заполняется явно, а display несёт человекочитаемый вид «python (pip)».

def resolve_provider_for_run(explicit, child_root, execute=False, quiet=False):
    """v3.28.x (P0-1) Единая точка выбора провайдера для CLI-путей `run`.

    Автовыбор (`.ai-ops.yaml` + ключ в env -> `claude` в PATH -> mock) применяется ТОЛЬКО в
    пользовательском пути `run --execute`: без --execute модель не вызывается, и офлайн-дефолт
    mock сохраняется (CI/selftest/планирование остаются детерминированными). Решение печатается
    ДО прогона: скатились в mock — говорим прямо, а не показываем «правок 0» постфактум.
    Возвращает словарь-решение resolve_provider (имя провайдера обязан использовать вызывающий)."""
    from ai_ops_kit.providers import orchestrator_providers as _op
    if not execute:
        return {"provider": explicit or "mock", "source": "explicit" if explicit else "no-execute",
                "reason": "провайдер не вызывается (нет --execute)", "warning": None,
                "autoresolve": False, "checked": []}
    res = _op.resolve_provider(explicit=explicit, root=child_root)
    if not quiet:
        _op.print_provider_resolution(res)
    return res


def _stacks_human(profile):
    """['python (pip)', 'node (pnpm)'] из профиля любой формы: словари detect() или строки-языки."""
    out = []
    for s in (profile or {}).get("stacks") or []:
        if isinstance(s, dict):
            lang = s.get("language") or "?"
            pm = s.get("package_manager")
            out.append(f"{lang} ({pm})" if pm else str(lang))
        elif s:
            out.append(str(s))
    return out


def _profile_for_report(root, existing=None):
    """Профиль репозитория для отчёта прогона: {stacks: [язык], display: ['python (pip)'], undetermined}.
    Детекция — через публичный project_detector.detect(root); сбой детекции не роняет прогон."""
    prof = None
    try:
        from ai_ops_kit.shared import project_detector
        prof = project_detector.detect(Path(root))
    except Exception:   # noqa: BLE001 — отчёт не должен падать из-за детектора
        prof = None
    if isinstance(prof, dict):
        out = {"stacks": [s.get("language") for s in prof.get("stacks") or [] if isinstance(s, dict)],
               "display": _stacks_human(prof),
               "undetermined": list(prof.get("undetermined") or [])}
        if not out["undetermined"] and isinstance(existing, dict):
            out["undetermined"] = list(existing.get("undetermined") or [])
        return out
    if isinstance(existing, dict):
        out = dict(existing)
        out.setdefault("display", _stacks_human(existing))
        return out
    return None


def _unresolved_intents(features_dir, fid, branch=None):
    """v3.0.17 (finding аудита P0): DeliveryIntent'ы БЕЗ парного DeliveryReceipt (незавершённая доставка).
    Реконсиляция и блокировка новой доставки опираются на ФАКТ отсутствия Receipt — НЕ на поле status
    интента (иначе потеря маркера outcome_unknown при двойном сбое записи скрыла бы незавершённость)."""
    d = _outbox_dir(features_dir, fid)
    out = []
    if not d.is_dir():
        return out
    for ip in sorted(d.glob("*.intent.yaml")):
        did = ip.name[:-len(".intent.yaml")]
        g = _ls.load_guarded(ip, kind="DeliveryIntent")
        if g["state"] != "ok":
            continue
        intent = g["data"]
        if branch is not None and intent.get("branch") != branch:
            continue
        rp = d / f"{did}.receipt.yaml"
        if _ls.load_guarded(rp, kind="DeliveryReceipt")["state"] != "ok":
            out.append((did, intent))
    return out


def _reconcile_pending_delivery(features_dir, fid, child_root):
    """v3.0.16/v3.0.17 (finding аудита #2/P0): сверить с remote КАЖДУЮ незавершённую доставку (Intent без
    Receipt) и дописать DeliveryReceipt — но ТОЛЬКО при СТРОГОМ совпадении идентичности PR с Intent
    (repository + head.sha == commit_sha + base.ref). PR той же ветки, но с ДРУГИМ коммитом НЕ
    засчитывается за подтверждение старой доставки. Все записи — обязательные барьеры (реконсиляция НЕ
    рапортует успех, если Receipt фактически не сохранился). Идемпотентно, ничего не создаёт на remote.
    -> список исходов по delivery_id | None (нечего сверять)."""
    from pathlib import Path as _P
    pending = _unresolved_intents(features_dir, fid)
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
        _w = _ls.durable_write(rp, {**_base, "status": "reconciled", "remote_sha": rc.get("head_sha"),
                                    "sha_verified": True, "pr_url": rc.get("url"),
                                    "pr_number": rc.get("number"), "pr_state": rc.get("pr_state"),
                                    "merged": rc.get("merged")},
                               require_keys=("kind", "delivery_id", "status"), keep_backup=True)
        if not _w.get("ok"):
            results.append({"delivery_id": did, "status": "receipt-write-failed"})   # НЕ рапортуем успех
            continue
        _ls.journal_append(jn, {"kind": "delivery_reconciled", "run_id": fid, "workitem_id": fid,
                                "delivery_id": did, "pr_url": rc.get("url"), "remote_sha": rc.get("head_sha")})
        results.append({"delivery_id": did, "status": "reconciled", "pr_url": rc.get("url")})
    return results


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
    for name, chk in (rep.get("checks") or {}).items():
        if (chk or {}).get("status") == "fail":
            tail = ""
            for run in (chk.get("runs") or []):
                tail = (run.get("output_tail") or "")[-700:]
                if tail:
                    break
            parts.append(f"[проверка {name}] упала:\n{tail}".rstrip())
    for rv in (rep.get("reviews") or []):
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


def _resume_context_from_handoff(child_root, fid):
    """v2.109 Real Resume: собрать из RunHandoff текст-состояние для prompt tool-loop, чтобы модель
    ПРОДОЛЖИЛА, а не переделала подтверждённое. Детерминированно, из features/<fid>/run-handoff.yaml."""
    hp = Path(child_root) / "features" / fid / "run-handoff.yaml"
    if not hp.is_file():
        return None
    h = yaml.safe_load(hp.read_text(encoding="utf-8")) or {}
    lines = ["=== RESUME: ПРОДОЛЖЕНИЕ РАБОТЫ (НЕ начинай заново, НЕ переделывай уже подтверждённое) ==="]
    if h.get("completed"):
        lines.append("Уже сделано:\n" + "\n".join(f"- {c}" for c in h["completed"]))
    dec = [d for d in (h.get("decisions") or []) if isinstance(d, dict)]
    if dec:
        lines.append("Принятые решения (не пересматривай без причины):\n"
                     + "\n".join(f"- {d.get('id', '?')}: {d.get('summary', '')}" for d in dec))
    if h.get("changed_files"):
        lines.append("Уже изменены файлы: " + ", ".join(h["changed_files"]))
    if h.get("open_questions"):
        lines.append("Открытые вопросы / осталось:\n" + "\n".join(f"- {q}" for q in h["open_questions"]))
    if h.get("next_action"):
        lines.append("СЛЕДУЮЩИЙ БЕЗОПАСНЫЙ ШАГ: " + str(h["next_action"]))
    return "\n\n".join(lines)


def _with_provider_fallback(primary, secondary, on_switch=None):
    """v3.8.3-rc2 (#6) PROVIDER FALLBACK: обёртка провайдера. На RETRYABLE infra-сбой (HTTP 429 / timeout /
    provider unavailable — по _classify_failure) переключается на fallback-провайдера и остаётся на нём.
    Не-retryable исключения (плохой код/тест/секьюрити НЕ бросают из провайдера) пробрасываются как есть —
    fallback НЕ маскирует дефекты реализации. secondary=None -> возвращаем primary без обёртки."""
    if secondary is None:
        return primary
    state = {"switched": False}

    def prov(*a, **k):
        if state["switched"]:
            return secondary(*a, **k)
        try:
            return primary(*a, **k)
        except Exception as e:  # noqa: BLE001
            try:
                from ai_ops_kit.engine.workpackage_executor import _classify_failure
                _retryable = bool(_classify_failure(e).get("retryable"))
            except Exception:  # noqa: BLE001
                _retryable = False
            if not _retryable:
                raise                       # не-retryable -> НЕ fallback (fix-loop/блок разрулят)
            state["switched"] = True
            if on_switch:
                on_switch(e)
            return secondary(*a, **k)
    return prov


def _load_klp_by_env(child_root):
    """v3.8.3-rc3: KLP-записи по env_ref из child .ai/policies/key-lifecycle.yaml (TTL/ротация). {} если нет."""
    try:
        import yaml as _y
        p = child_root / ".ai" / "policies" / "key-lifecycle.yaml"
        if not p.is_file():
            return {}
        allk = _y.safe_load(p.read_text(encoding="utf-8")) or {}
        return {k.get("env_ref"): k for k in (allk.get("keys") or []) if isinstance(k, dict)}
    except Exception:  # noqa: BLE001
        return {}


def _provider_trust(provider, key_env, klp_by_env, env, now, cache):
    """v3.8.3-rc3 JIT PROVIDER TRUST: перед первым вызовом КОНКРЕТНОГО провайдера — key presence + KLP/TTL.
    Кэшируется по provider (проверяем один раз на реально вызываемую модель). -> {ready, reason, preflight}.
    primary not ready -> caller делает blocked-preflight; необязательный (fallback/escalation) not ready ->
    caller ИСКЛЮЧАЕТ кандидата + пишет причину + пробует следующего. Ранее KLP покрывал только primary+reviewer
    -> динамический fallback/escalation обходил security-инвариант (P1). Теперь покрыт каждый вызываемый."""
    if provider in cache:
        return cache[provider]
    from ai_ops_kit.security import security_enforcement as _se
    ent = klp_by_env.get(key_env) or {}
    keyspec = {"name": provider, "env_ref": key_env,
               **{k: ent[k] for k in ("ttl_days", "issued_at", "rotated_at", "next_rotation_at") if k in ent}}
    try:
        kpf = _se.key_preflight({"keys": [keyspec]}, env, critical=True, now=now)
        res = {"ready": bool(kpf.get("ready")),
               "reason": (None if kpf.get("ready") else "; ".join(kpf.get("blocks") or ["ключ отсутствует/просрочен"])),
               "preflight": kpf}
    except Exception as e:  # noqa: BLE001 — FAIL-CLOSED: ошибка проверки = не доверяем
        res = {"ready": False, "reason": f"{type(e).__name__}: {e}"[:160]}
    cache[provider] = res
    return res


def run(task_text, signals, child_root: Path, features_dir=None,
        runtime="claude-code", provider_name="mock", session="cli", execute=False,
        feature=None, engine="controller", proposer=None, open_pr=False, model=None,
        baseline_diff=False, require_fix=False, max_steps=40, discard_previous=False,
        sandbox=False, review=False, reviewer_proposer=None,
        author=False, author_proposer=None, install_deps=True,
        resume=False, force_resume=False, base=None, write_scope=None, replan=False,
        review_fix_attempts=0, calibrated_enforcement=True, ui_evidence=None,
        context_shadow=False, context_hybrid=False, reevaluate_only=False,
        progressive_escalation=False, provider_resolution=None):
    signals = dict(signals or {})
    signals.setdefault("task_text", task_text)
    child_root = Path(child_root)
    features_dir = Path(features_dir) if features_dir else child_root / "features"

    # engine=pipeline (v2.63): собранный единый движок как РЕАЛЬНЫЙ путь из контроллера
    # (adversarial-review: раньше execution_pipeline вызывался только из selftest). Делегируем
    # весь прогон в execution_pipeline.run_pipeline; proposer — из провайдера (или передан).
    if engine == "pipeline":
        from ai_ops_kit.engine import execution_pipeline
        from ai_ops_kit.engine import tool_loop
        from ai_ops_kit.providers import orchestrator
        # v3.0-rc2 (P0.1) Canonical Resume Context: при resume восстанавливаем ПОЛИТИКУ исходного прогона
        # (signals/task_type/risk + sandbox/baseline_diff/require_fix/author/review/open_pr/write_scope/
        # max_steps) из сохранённого run-settings.yaml — иначе resume молча теряет политику и
        # переклассифицирует задачу. provider/model/base приходят от вызывающего (runtime-выбор);
        # изменение базы/состояния уже требует явной ревалидации (resume_preflight).
        # v3.0-rc4 (P0.1): immutable-resume — ТОЛЬКО для пользовательского resume задачи. Внутренний
        # per-package resume executor'а (каждый пакет — своя подсистема/affected_areas, поверх общей
        # ветки) НЕ является сменой классификации: executor сам управляет policy пакета. Помечен
        # _sequence_internal -> пропускаем drift-проверку и restore run-settings.
        if resume and feature and not signals.get("_sequence_internal"):
            _sp = features_dir / feature / "run-settings.yaml"
            # v3.0.12 (finding аудита блок B): FAIL-CLOSED чтение. Прежде safe_load(...) or {} трактовал
            # битый/пустой run-settings как «отсутствует» -> resume тихо откатывался к дефолтам вызова
            # (терял классификацию/policy/BaseBinding) И перезаписывал файл дефолтами (контракт исходного
            # прогона уничтожался навсегда). Теперь: повреждён -> явный отказ (не дефолт, не перезапись).
            _g = _ls.load_guarded(_sp, required_keys=("kind", "policy"), kind="run-settings")
            if _g["state"] == "corrupt":
                return {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": feature,
                        "status": "error", "ready_for_pr": False,
                        "error": (f"run-settings повреждён ({_g['reason']}) — resume не может восстановить "
                                  "policy/классификацию исходного прогона. Нужна явная recovery (не тихий "
                                  "дефолт: иначе прогон переклассифицируется и перезапишет контракт)."),
                        "resume": {"requested": True, "resumed": False}}
            if _g["state"] == "ok":
                _saved = _g["data"]
                _ss, _pp = (_saved.get("signals") or {}), (_saved.get("policy") or {})
                # v3.0-rc4 (P0.1) IMMUTABLE resume: resume НЕ меняет классификацию/policy. Если новый
                # вызов пытается переопределить routing-сигнал (task_type/risk/size/affected_areas) или
                # write_scope значением, отличным от сохранённого — это НЕ resume, а replan: требуется
                # явный replan=True (+ ревалидация). Иначе можно было бы тихо продолжить ENGINEERING как QUICK.
                _POLICY_KEYS = ("task_type", "risk", "size", "affected_areas")
                _drift = [k for k in _POLICY_KEYS
                          if k in signals and k in _ss and signals[k] != _ss[k]]
                if write_scope is not None and _pp.get("write_scope") is not None \
                        and write_scope != _pp.get("write_scope"):
                    _drift.append("write_scope")
                if _drift and not replan:
                    return {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": feature,
                            "status": "error", "ready_for_pr": False,
                            "error": ("resume не меняет классификацию/policy исходного прогона "
                                      f"(drift: {', '.join(_drift)}). Это replan — запусти с replan=True "
                                      "(ревалидация + новый план), а не resume."),
                            "resume": {"requested": True, "resumed": False, "drift": _drift}}
                # восстанавливаем СОХРАНЁННУЮ policy как источник истины (не «or», а точное значение),
                # кроме случая replan, где новый вызов осознанно задаёт новую policy.
                if not replan:
                    signals = {**signals, **_ss}          # saved policy побеждает
                    sandbox = bool(_pp.get("sandbox", sandbox))
                    baseline_diff = bool(_pp.get("baseline_diff", baseline_diff))
                    require_fix = bool(_pp.get("require_fix", require_fix))
                    author = bool(_pp.get("author", author))
                    review = bool(_pp.get("review", review))
                    open_pr = bool(_pp.get("open_pr", open_pr))
                    write_scope = _pp.get("write_scope") if write_scope is None else write_scope
                    if max_steps == 40 and _pp.get("max_steps"):
                        max_steps = _pp["max_steps"]
                    # v3.0.2/v3.0.9 (P0): base восстанавливается из saved BaseBinding (точная база исходного
                    # запуска), с фолбэком на плоское поле base (совместимость со старыми run-settings).
                    base = ((_pp.get("base_binding") or {}).get("base_ref")) or _pp.get("base", base)
        # v3.0.8 (finding аудита P0.1): base РАЗРЕШАЕТСЯ В КОНКРЕТНУЮ ВЕТКУ ОДИН РАЗ здесь (до resume_preflight
        # и до записи run-settings). Иначе fresh auto-run сохранял base=null -> resume передавал None в
        # git rev-parse -> TypeError. На resume уже восстановлен сохранённый base (выше); для fresh —
        # auto-резолв. Явная несуществующая base -> ранний честный отказ (0 model calls).
        _brr = execution_pipeline._resolve_base(child_root, base)
        if _brr.get("mode") == "explicit" and not _brr.get("resolved"):
            return {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": feature or "?",
                    "status": "error", "ready_for_pr": False,
                    "error": (f"base-preflight: явная база '{base}' не разрешается в ветку "
                              f"({_brr.get('reason')}) — прогон не запущен (0 вызовов модели)"),
                    "base_binding": {k: _brr.get(k) for k in ("base_ref", "base_sha", "mode", "source")}}
        if _brr.get("resolved"):
            base = _brr.get("base_ref")   # конкретная ветка -> в run-settings, resume_preflight, pipeline
        # v3.0.9 (finding аудита P0.2): полный BaseBinding (ref+sha+mode+source) сохраняется/восстанавливается,
        # а не только имя ветки — чтобы resume восстанавливал ТОЧНУЮ базу исходного запуска (ловит force-push/
        # смену upstream/пересоздание ветки, не только fast-forward).
        base_binding = {"kind": "BaseBinding",
                        "base_ref": _brr.get("base_ref") or base, "base_sha": _brr.get("base_sha"),
                        "mode": _brr.get("mode"), "source": _brr.get("source")}
        # v3.7.12 Router->runtime: без явного --model резолвим модель ПО РОЛИ через model_router и физически
        # диспатчим на endpoint вендора (provider_endpoints) -> writer≠judge по МОДЕЛИ становится поведением
        # продукта, а не только резолвером. Явный --model = override (записывается). Всё под fail-safe:
        # нет резолва/ключа/endpoint -> прежнее поведение (passthrough --model) + честная запись в отчёт.
        _writer_model, _writer_prov, _rev_model, _rev_prov = model, None, model, None
        try:
            from ai_ops_kit.providers import model_router as _mr
            from ai_ops_kit.providers import provider_endpoints as _pe
            _plan = _mr.plan_run(signals=signals)   # v3.9.0-rc3: signals -> preferred_writer_tier
            _model_resolution = {"kind": "ModelResolution", "plan": _plan, "applied": False,
                                 "mode": "explicit-override" if model else "router", "notes": []}
            # v3.8.3-rc3 Dynamic Model Trust: JIT provider-preflight для КАЖДОЙ реально вызываемой модели
            # (primary/reviewer/fallback/escalation), а не только primary+reviewer. Trust-переменные видны
            # и в fix-loop (эскалация проверяет trust там).
            import os as _os
            import datetime as _dt
            _trust_cache = {}
            _klp_by_env = _load_klp_by_env(child_root)
            _trust_now = _dt.date.today().isoformat()
            _trust_env = dict(_os.environ)
            if model is None and provider_name == "openai-compatible":
                impl, rev = _plan.get("implementation") or {}, _plan.get("code_review") or {}
                if impl.get("resolved") and _pe.key_available(impl.get("provider")):
                    ep = _pe.endpoint_for(impl["provider"])
                    # JIT trust PRIMARY: не готов -> blocked-preflight (fail-closed, как раньше)
                    _pt = _provider_trust(impl["provider"], ep["key_env"], _klp_by_env, _trust_env, _trust_now, _trust_cache)
                    _model_resolution["key_preflight"] = _pt.get("preflight") or {"ready": _pt["ready"], "blocks": ([] if _pt["ready"] else [_pt.get("reason")])}
                    if not _pt["ready"]:
                        _model_resolution["preflight_blocked"] = True
                    _writer_model = impl["model_id"]
                    _writer_prov = orchestrator.make_openai_provider(impl["model_id"], ep["base_url"], ep["key_env"])
                    _model_resolution["applied"] = True
                    _model_resolution["initial_model"] = impl["model_id"]
                    _model_resolution["effective_model"] = impl["model_id"]   # обновится при эскалации/fallback
                    _model_resolution["writer"] = {"model_id": impl["model_id"], "provider": impl["provider"],
                                                   "cost_basis": impl.get("cost_basis")}
                    _model_resolution["model_attempts"] = [
                        {"attempt": 1, "model": impl["model_id"], "provider": impl["provider"],
                         "trigger": "initial", "outcome": "pending"}]
                    # v3.9.0-rc3 COMPLEXITY-AWARE ROUTING: сложный класс задачи -> сильный executor (Claude
                    # Code adapter, claude-cli) СРАЗУ, не cheap-then-fix-loop. Честный fallback: нет локального
                    # claude CLI -> остаёмся на дешёвом money-mode writer + пишем причину. Реестр/ключи не нужны
                    # (локальная сессия). Escalation-ladder чистим: некуда «эскалировать» сильного вниз на kimi/qwen.
                    _tier = _plan.get("preferred_writer_tier") or {}
                    if _tier.get("tier") == "strong-executor":
                        import shutil as _sh
                        if _sh.which("claude"):
                            _writer_model = "claude-code-local"
                            _writer_prov = orchestrator.make_claude_cli_provider()
                            _model_resolution["effective_model"] = "claude-code-local"
                            _model_resolution["writer"] = {"model_id": "claude-code-local", "provider": "claude-cli",
                                                           "tier": "strong-executor", "reason": _tier.get("reason")}
                            _model_resolution["model_attempts"][0].update(
                                model="claude-code-local", provider="claude-cli", trigger="complexity-routing")
                            if isinstance(impl, dict):
                                impl["escalation_ladder"] = []   # сильный executor — вниз не даунгрейдим
                            _model_resolution["notes"].append(
                                "complexity-aware: сложный класс -> writer=claude-cli (сильный executor) сразу")
                        else:
                            _model_resolution["strong_executor_unavailable"] = True
                            _model_resolution["notes"].append(
                                "complexity-aware: класс требует strong-executor, но локальный claude CLI "
                                "недоступен -> честный fallback на money-mode дешёвый writer")
                    # reviewer — JIT trust отдельного провайдера (writer≠judge по модели).
                    # v3.9.0-rc3: сравниваем с ЭФФЕКТИВНЫМ writer'ом (_writer_model), а не с registry-impl —
                    # иначе при complexity-override (writer=claude-cli) deepseek-ревьюер ложно считался
                    # «не независим» (deepseek==registry-impl) и откатывался в self-model -> no-verdict.
                    _rev_trusted = (rev.get("resolved") and rev.get("model_id") != _writer_model
                                    and _pe.key_available(rev.get("provider"))
                                    and _provider_trust(rev["provider"], _pe.endpoint_for(rev["provider"])["key_env"],
                                                        _klp_by_env, _trust_env, _trust_now, _trust_cache)["ready"])
                    if _rev_trusted:
                        ep2 = _pe.endpoint_for(rev["provider"])
                        _rev_model = rev["model_id"]
                        _rev_prov = orchestrator.make_openai_provider(rev["model_id"], ep2["base_url"], ep2["key_env"])
                        _model_resolution["reviewer"] = {"model_id": rev["model_id"], "provider": rev["provider"], "independent_by_model": True}
                    elif (_writer_model == "claude-code-local" and impl.get("resolved")
                          and _pe.key_available(impl.get("provider"))):
                        # v3.9.0-rc3 complexity-routing: writer=claude-cli (сильный executor) -> ревьюер =
                        # ДЕШЁВЫЙ qualified impl-судья (deepseek), независим от claude-cli по модели, даже если
                        # отдельная code_review-роль не резолвится в реестре. Это и есть owner-план review->deepseek.
                        _iep = _pe.endpoint_for(impl["provider"])
                        _rev_model = impl["model_id"]
                        _rev_prov = orchestrator.make_openai_provider(impl["model_id"], _iep["base_url"], _iep["key_env"])
                        _model_resolution["reviewer"] = {"model_id": impl["model_id"], "provider": impl["provider"],
                                                         "independent_by_model": True,
                                                         "reason": "дешёвый qualified судья vs сильный writer=claude-cli"}
                    else:
                        _rev_model, _rev_prov = _writer_model, _writer_prov
                        _model_resolution["reviewer"] = {"model_id": _writer_model, "independent_by_model": False,
                                                         "reason": "code_review не резолвится/нет ключа/trust -> self-model review (writer=judge по модели)"}
                        _model_resolution["notes"].append("reviewer=writer по модели: нет отдельной допущенной+trusted модели")
                    # v3.8.3-rc2 (#6) PROVIDER FALLBACK на RETRYABLE infra-сбое. rc3: fallback — НЕОБЯЗАТЕЛЬНЫЙ
                    # кандидат: JIT trust; НЕ готов -> ИСКЛЮЧАЕМ (не блокируем primary) + пишем причину.
                    _fb = impl.get("fallback") or {}
                    if _fb.get("model_id") and _fb.get("provider"):
                        _fpt = (_provider_trust(_fb["provider"], _pe.endpoint_for(_fb["provider"])["key_env"],
                                                _klp_by_env, _trust_env, _trust_now, _trust_cache)
                                if _pe.key_available(_fb.get("provider")) else {"ready": False, "reason": "ключ отсутствует в env"})
                        if _fpt["ready"]:
                            try:
                                _fbep = _pe.endpoint_for(_fb["provider"])
                                _fb_prov = orchestrator.make_openai_provider(_fb["model_id"], _fbep["base_url"], _fbep["key_env"])
                                _sw = {"switched_to": None}
                                _writer_prov = _with_provider_fallback(
                                    _writer_prov, _fb_prov,
                                    on_switch=lambda e, _s=_sw, _m=_fb["model_id"]: _s.update(switched_to=_m))
                                _model_resolution["writer_fallback"] = {
                                    "model_id": _fb["model_id"], "provider": _fb["provider"],
                                    "trigger": "retryable-infra-failure-only", "switch_state": _sw}
                                if not (_model_resolution.get("reviewer") or {}).get("independent_by_model"):
                                    _rev_prov = _writer_prov
                            except Exception as _fbe:  # noqa: BLE001 — сбой построения fallback не роняет прогон
                                _model_resolution["writer_fallback"] = {"error": f"{type(_fbe).__name__}: {_fbe}"[:160]}
                        else:
                            _model_resolution["writer_fallback"] = {
                                "excluded_model": _fb["model_id"], "provider": _fb.get("provider"),
                                "reason": _fpt.get("reason"),
                                "note": "необязательный fallback ИСКЛЮЧЁН по JIT-trust (не блокирует primary)"}
                else:
                    _model_resolution["notes"].append("router не применён (implementation не резолвится/нет ключа) -> passthrough --model")
        except Exception as _e:  # noqa: BLE001
            _model_resolution = {"kind": "ModelResolution", "error": str(_e)[:200], "applied": False,
                                 "mode": "explicit-override" if model else "router"}
        # v3.7.3 (#5 flip): security needs_review закрывает ТОЛЬКО КВАЛИФИЦИРОВАННЫЙ security-судья
        # (security_review.resolved в plan_run) ЛИБО человек (ApprovalRecord). Общий code reviewer — НЕТ.
        # Пока qualified security-судьи нет (до Bench v2) -> security needs_review -> pending_human до
        # человеческого ApprovalRecord (реальный human-fallback). Отдельный security_reviewer_proposer.
        _sec_qualified = bool(((_model_resolution.get("plan") or {}).get("security_review") or {}).get("resolved"))

        # v3.7.1 (#4) РЕАЛЬНЫЙ security-барьер: key preflight не пройден (ключ/ротация) -> блок ПРОГОНА
        # (не строим proposer, не зовём провайдера). Честный blocked-preflight-отчёт, ready_for_pr=false.
        if isinstance(_model_resolution, dict) and _model_resolution.get("preflight_blocked"):
            _kpf = _model_resolution.get("key_preflight", {})
            return {"schema_version": 1, "kind": "execution-pipeline", "status": "blocked-preflight",
                    "ready_for_pr": False, "provider": provider_name, "model": _writer_model,
                    "model_resolution": _model_resolution, "key_preflight": _kpf,
                    "blocked_reason": "key preflight не пройден до provider-вызова: "
                                      + "; ".join(_kpf.get("blocks", []) or ["ключ/ротация"]),
                    "not_yet": ["security key preflight: " + "; ".join(_kpf.get("blocks", []) or ["ключ отсутствует/просрочен"])]}

        # v3.10.0 Usage Truth: обёртка провайдера ставит call-context (role/trigger/provider/runtime) перед
        # вызовом -> _record_call пишет их в UsageRecord. run_id/workitem_id заполнит usage_ledger.append.
        def _uctx(_prov, _role, _trigger, _prov_name):
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
        # v2.83/v3.7.12: независимый ревьюер — ОТДЕЛЬНЫЙ провайдер (writer ≠ judge на уровне вызова);
        # при router-режиме — по возможности ДРУГАЯ модель/вендор (полная независимость судьи).
        rev_prop = reviewer_proposer
        if review and rev_prop is None and provider_name != "mock":
            rev_prop = _uctx(_rev_prov or orchestrator.make_provider(provider_name, _rev_model), "code_review", "review", _rname)
        # v2.86: author-модель для артефактов requirements/plan (отдельный вызов провайдера).
        auth_prop = author_proposer
        if author and auth_prop is None and provider_name != "mock":
            auth_prop = _uctx(_writer_prov or orchestrator.make_provider(provider_name, _writer_model), "implementation", "initial", _wname)

        # v2.94 (One Run Transaction, аудит #2): pipeline БОЛЬШЕ НЕ обходит lifecycle. Один план
        # строится здесь и передаётся в движок (не второй раз внутри); WorkItem/RunPlan/active-work/
        # concurrency-preflight/run-report — как в controller-пути. Прежде было «два мира»: движок
        # возвращал отчёт, не создавая WorkItem/active-work/run-report.
        plan = run_plan.build_plan(signals, workitem_id=feature)
        fid = plan["workitem_id"]

        # v3.0.16 Phase A (finding аудита #2): реконсиляция незавершённой доставки прошлого прогона —
        # если остался DeliveryIntent (outcome_unknown), сверяем с remote и дописываем DeliveryReceipt
        # ДО новой работы. Идемпотентно, ничего не создаёт. Best-effort (не роняет прогон).
        try:
            _rec = _reconcile_pending_delivery(features_dir, fid, child_root)
        except Exception:  # noqa: BLE001
            _rec = None

        # v2.109 Real Resume: продолжить WorkItem поверх подтверждённой работы (не начинать заново).
        # Проверяем ДО регистрации/изменения состояния, чтобы честный ранний выход ничего не оставил.
        resume_ctx = None
        if resume:
            from ai_ops_kit.engine import run_handoff
            pf = run_handoff.resume_preflight(child_root, fid, base=base)
            if not pf["can_resume"]:
                return {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": fid,
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
                return {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": fid,
                        "status": "blocked", "engine": "pipeline", "ready_for_pr": False,
                        "error": (f"resume заблокирован: base {_kind} с прошлого прогона — старую работу "
                                  "нельзя выдать за проверенную против новой базы (worktree форкнут от "
                                  "старой базы и не интегрирован с новой). Ни force_resume, ни replan это "
                                  "НЕ снимают. Нужен СВЕЖИЙ прогон от новой базы (без --resume; --discard "
                                  "для замены устаревшей ветки). " + "; ".join(pf["reasons"])),
                        "resume": {"requested": True, "resumed": False,
                                   "base_rewritten": bool(pf.get("base_rewritten")),
                                   "base_moved": bool(pf.get("base_moved")),
                                   "revalidation_needed": True, "reasons": pf["reasons"]}}
            # ЧЕСТНОСТЬ: база/состояние изменились -> НЕ продолжаем молча на устаревшем evidence.
            if pf["revalidation_needed"] and not force_resume:
                return {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": fid,
                        "status": "blocked", "engine": "pipeline", "ready_for_pr": False,
                        "error": "resume требует ревалидации (база/состояние изменились с прошлого "
                                 "прогона) — перепроверь и запусти с force_resume=True (--force), "
                                 "чтобы продолжить осознанно",
                        "resume": {"requested": True, "resumed": False, "revalidation_needed": True,
                                   "reasons": pf["reasons"]}}
            resume_ctx = _resume_context_from_handoff(child_root, fid)

        workitem.start(str(features_dir), fid, task_text,
                       task_type=signals.get("task_type"), risk=signals.get("risk"))
        # v3.0.15 (finding аудита P1): RunPlan — write BARRIER. Сбой durable-записи -> прогон НЕ начат
        # (0 вызовов модели): без надёжного плана нельзя доказать routing/гейты/resume.
        _pw = _ls.durable_write(features_dir / fid / "run-plan.yaml", plan, require_keys=("workitem_id",))
        if not _pw.get("ok"):
            return {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": fid,
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
                return {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": fid,
                        "status": "error", "ready_for_pr": False,
                        "error": (f"lifecycle fail-closed: не удалось надёжно сохранить run-settings "
                                  f"({_ws.get('error')}) — без durable policy resume небезопасен; прогон "
                                  "не начат")}
            _sdump = yaml.safe_dump(_settings, allow_unicode=True, sort_keys=False)   # снимок истории (ниже)
            # v3.0-rc4 (P0.1): per-run СНИМОК для аудита (не только последнее состояние). Нумеруем по
            # числу уже сохранённых снимков — детерминированно, без времени (совместимо с workflow-песочницей).
            _hist = features_dir / fid / "run-history"
            _hist.mkdir(parents=True, exist_ok=True)
            _n = len(list(_hist.glob("run-*.yaml"))) + 1
            _ls.durable_write(_hist / f"run-{_n:03d}.yaml", _settings)   # v3.0.14 (#2): атомарно
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
        # v2.115 Preflight Truth: проверки ДО запуска модели. Блок -> tool loop НЕ запускается,
        # правки/коммит НЕ создаются (Spec-First блокирует РЕАЛИЗАЦИЮ, а не только доставку). Единая
        # точка: spec/атомарность/overflow/approvals/lifecycle. Выполняется и для fresh, и для resume.
        from ai_ops_kit.gates import preflight as _pf
        pretruth = _pf.assess(signals, child_root, fid, plan=plan, bundle=bundle, payload=payload,
                              spec_cov=spec_cov, work_pkg=work_pkg, lifecycle_errors=lifecycle_errors,
                              author=author, reevaluate_only=reevaluate_only)
        (features_dir / fid / "preflight.yaml").write_text(
            yaml.safe_dump(pretruth, allow_unicode=True, sort_keys=False), encoding="utf-8")
        if pretruth["blocked"]:
            rep = {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": fid,
                   "status": "blocked", "engine": "pipeline", "runtime": runtime,
                   "provider": provider_name, "model": model, "ready_for_pr": False,
                   "overall_status": "blocked-preflight",
                   "error": "preflight не пройден (модель не запускалась, правок/коммита нет): "
                            + "; ".join(pretruth["reasons"]),
                   "preflight": pretruth,
                   "loop": None, "commit": {"sha": None},   # честно: ни петли, ни коммита
                   "not_yet": pretruth["reasons"],
                   # P1-3: даже заблокированный прогон честно показывает распознанный стек
                   "profile": _profile_for_report(child_root),
                   "provider_resolution": dict(provider_resolution) if provider_resolution else None,
                   "lifecycle": {"workitem": f"features/{fid}/workitem.yaml",
                                 "run_plan": f"features/{fid}/run-plan.yaml",
                                 "preflight": f"features/{fid}/preflight.yaml"}}
            if lifecycle_errors:
                rep["lifecycle_errors"] = lifecycle_errors
            _ls.durable_write_json(features_dir / fid / "run-report.json", rep)   # v3.0.14 (#2): атомарно
            return rep

        aw_path = child_root / ".ai" / "runtime" / "active-work.yaml"
        # v3.0.12 (finding аудита блок B): общий реестр координации повреждён -> FAIL-CLOSED (не стартуем
        # вслепую: пустая карта скрыла бы чужую активную работу и две сессии столкнулись бы). Проверяем
        # ДО preflight/register, чтобы register не наткнулся на corrupt-raise без обработки.
        _awg = _ls.load_guarded(aw_path, kind="active-work")
        if _awg["state"] == "corrupt":
            return {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": fid,
                    "status": "error", "ready_for_pr": False,
                    "error": (f"active-work реестр повреждён ({_awg['reason']}) — прогон не начат, чтобы не "
                              "потерять координацию параллельных сессий (пустая карта скрыла бы коллизии). "
                              "Нужна явная recovery .ai/runtime/active-work.yaml.")}
        areas = signals.get("affected_areas") or ["unspecified"]
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
        # регистрация активной работы (координация) — человекочитаемые строки в stderr, чтобы
        # stdout оставался чистым для --json.
        with contextlib.redirect_stdout(sys.stderr):
            try:
                active_work.register(aw_path, fid, f"ai-ops/{fid}", areas, session,
                                     workitem=f"features/{fid}/workitem.yaml")
            except active_work.ActiveWorkCorrupt as _e:   # v3.0.12: сбой durable-записи реестра не молчит
                lifecycle_errors.append(f"active-work register: {_e}")

        # v2.107 (finding аудита): если pipeline упадёт, active-work обязана закрыться (иначе запись
        # останется in-progress навсегда) — гарантируем через except+re-raise.
        # v3.1.8: калиброванное UI-enforcement (по умолчанию включено в контроллере). NO-OP без более
        # богатых сигналов: легаси ui_changed -> user_facing + нет evidence -> fail-closed == сегодня.
        # Ослабление возможно ТОЛЬКО при ui_impact=internal (не-safety гейты) или passing UI-evidence.
        # v3.1.9 (trust-фикс): контроллер БОЛЬШЕ НЕ собирает evidence до реализации из основного
        # checkout. Сбор перенесён В pipeline — ПОСЛЕ коммита, из рабочего worktree, на ТОЧНОМ SHA
        # (см. execution_pipeline: build_bundle(work_root) + evidence_for_gate(expected_sha=committed)).
        # ui_evidence прокидывается как есть (обычно None; bench инжектит синтетический dict напрямую).
        _calib = bool(calibrated_enforcement)

        def _pipe(_resume, _rctx):
            return execution_pipeline.run_pipeline(
                task_text, signals, child_root, prop, feature=feature, plan=plan,
                commit=execute, isolate=execute, open_pr=open_pr, baseline_diff=baseline_diff,
                require_fix=require_fix, max_steps=max_steps, discard_previous=discard_previous,
                sandbox=sandbox, review=review, reviewer_proposer=rev_prop,
                author=author, author_proposer=auth_prop, install_deps=install_deps,
                context_prelude=_hybrid_prelude,   # v3.7.16: hybrid (v1 ∪ v2-additions) реально подаётся модели
                resume=_resume, resume_context=_rctx, write_scope=write_scope,
                base=base,   # v3.0.1/v3.0.7 (P0): base сквозной; None -> auto-резолв (не хардкод main)
                defer_delivery=True,   # v3.0.15 (P0): PR открывает КОНТРОЛЛЕР после durable-фиксации lifecycle
                calibrated_enforcement=_calib, ui_evidence=ui_evidence,
                reevaluate_only=reevaluate_only,   # v3.8.3-rc: переоценка гейтов после человеко-approval БЕЗ переавторинга
                strict_judge_qualified=_sec_qualified)   # v3.7.1: нет qualified судьи -> security pending_human
        try:
            rep = _pipe(resume, resume_ctx)
            # v3.1.1 (fix-loop, находка Phase B): блокеры ревью/проверок -> писателю на ИТЕРАЦИЮ поверх
            # той же ветки (resume=True), пока не pass ЛИБО не исчерпан бюджет. fail-closed сохранён:
            # бюджет кончился и всё ещё не ready -> честный блок (ничего не форсируем в green). Не для mock.
            _fix_left = int(review_fix_attempts or 0)
            # v3.8.3 WRITER QUALITY-ESCALATION: money-mode взял дешёвого writer'а; при КАЧЕСТВЕННОМ провале
            # (impl_verification/code_review) эскалируем на СИЛЬНЕЙШУЮ допущенную модель (ладдер по success_rate),
            # а не re-prompt того же слабого. Отличается от provider_fallback (#6 — только retryable infra).
            _esc_ladder = (((_model_resolution or {}).get("plan") or {}).get("implementation") or {}).get("escalation_ladder") or []
            _esc_idx = 0
            _QUALITY_GATES = {"implementation_verification", "code_review"}
            _rev_self = not ((_model_resolution.get("reviewer") or {}).get("independent_by_model")) if isinstance(_model_resolution, dict) else True
            while (not rep.get("ready_for_pr")) and _fix_left > 0 and provider_name not in (None, "mock"):
                _fx = _review_fix_context(rep)
                if not _fx:
                    break   # блок не модель-фиксируем (human/base/lifecycle) -> не зацикливаем
                # эскалация writer'а, если провалены КАЧЕСТВЕННЫЕ гейты и ладдер не исчерпан (model=None -> router-путь)
                _unmet = set((rep.get("gates") or {}).get("unmet") or [])
                if model is None and (_unmet & _QUALITY_GATES) and _esc_idx < len(_esc_ladder):
                    if _model_resolution.get("model_attempts"):
                        _model_resolution["model_attempts"][-1]["outcome"] = "quality_failed"
                    from ai_ops_kit.providers import provider_endpoints as _pe2

                    def _cand_trusted(c):  # rc3: JIT trust кандидата эскалации (ключ + KLP/TTL)
                        if not _pe2.key_available(c.get("provider")):
                            return False, "ключ отсутствует в env"
                        _ct = _provider_trust(c["provider"], _pe2.endpoint_for(c["provider"])["key_env"],
                                              _klp_by_env, _trust_env, _trust_now, _trust_cache)
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
                        _model_resolution.setdefault("escalation_excluded", []).append(
                            {"model": _cand.get("model_id"), "provider": _cand.get("provider"), "reason": _why})
                    if _esc is not None:
                        try:
                            _eep = _pe2.endpoint_for(_esc["provider"])
                            _eprov = orchestrator.make_openai_provider(_esc["model_id"], _eep["base_url"], _eep["key_env"])
                            # #6-fallback на СЛЕДУЮЩЕГО TRUSTED кандидата (если эскалированный сам жёстко 429-ится)
                            _nxt = next((n for n in _esc_ladder[_esc_idx:] if _cand_trusted(n)[0]), None)
                            if _nxt:
                                _nep = _pe2.endpoint_for(_nxt["provider"])
                                _eprov = _with_provider_fallback(
                                    _eprov, orchestrator.make_openai_provider(_nxt["model_id"], _nep["base_url"], _nep["key_env"]))
                            _eprov_ctx = _uctx(_eprov, "implementation", "escalation", _esc.get("provider"))  # v3.10.0 Usage Truth
                            prop = tool_loop.make_model_proposer(_eprov_ctx)  # writer -> выше observed success
                            if author and author_proposer is None:
                                auth_prop = _eprov_ctx
                            if review and reviewer_proposer is None and _rev_self:
                                rev_prop = _eprov_ctx                        # self-model reviewer следует за writer'ом
                            _model_resolution["effective_model"] = _esc["model_id"]
                            _model_resolution.setdefault("model_attempts", []).append(
                                {"attempt": len(_model_resolution.get("model_attempts") or []) + 1,
                                 "model": _esc["model_id"], "provider": _esc.get("provider"),
                                 "trigger": "quality_escalation", "outcome": "pending",
                                 "observed_success_rate": _esc.get("observed_success_rate"),
                                 "corpus_version": _esc.get("corpus_version")})
                            _model_resolution.setdefault("escalations", []).append(
                                {"to": _esc["model_id"], "provider": _esc.get("provider"),
                                 "observed_success_rate": _esc.get("observed_success_rate"),
                                 "corpus_version": _esc.get("corpus_version"),
                                 "reason": "quality-failure:" + ",".join(sorted(_unmet & _QUALITY_GATES))})
                        except Exception as _ee:  # noqa: BLE001 — rc3: НЕ глотаем молча -> честный escalation_error
                            _model_resolution["escalation_error"] = f"{type(_ee).__name__}: {_ee}"[:200]
                try:
                    _ls.journal_append(features_dir / fid / "lifecycle-journal.jsonl",
                                       {"kind": "fix_attempt", "run_id": fid, "workitem_id": fid,
                                        "attempt_id": _attempt_id, "remaining": _fix_left,
                                        "unmet": (rep.get("gates") or {}).get("unmet")})
                except Exception as _je:  # noqa: BLE001 — журнал не роняет fix-loop...
                    # ...но пробел в аудит-цепочке обязан быть видимым: цепочка checksum'ов
                    # lifecycle-журнала после пропущенной записи уже не полна.
                    _note_bookkeeping_error(rep, "lifecycle_journal.fix_attempt", _je)
                rep = _pipe(True, _fx + (("\n\n" + resume_ctx) if resume_ctx else ""))
                _fix_left -= 1
        except (KeyboardInterrupt, SystemExit):
            with contextlib.redirect_stdout(sys.stderr):
                active_work.finish_cmd(aw_path, fid, status="blocked",
                                       reason="прогон прерван (Ctrl-C/exit) — работа не завершена")
            raise
        except Exception as _e:  # noqa: BLE001
            # v3.0-rc17 (finding живого прогона): исключение провайдера/инфры (напр. HTTP 429 kimi ПОСЛЕ
            # исчерпания ретраев) НЕ должно ронять CLI traceback'ом — как в sequential (rc12/rc16),
            # одиночный прогон обязан вернуть ЧЕСТНЫЙ error-отчёт (status=error, ready_for_pr=False, exit 2),
            # а не падать. Типизируем сбой (провайдер/сеть vs дефект движка).
            with contextlib.redirect_stdout(sys.stderr):
                active_work.finish_cmd(aw_path, fid, status="blocked",
                                       reason=f"прогон упал: {type(_e).__name__}")
            try:
                from ai_ops_kit.engine.workpackage_executor import _classify_failure
                _fail = _classify_failure(_e)
            except Exception:  # noqa: BLE001
                _fail = {"failure_class": "engine", "exception_type": type(_e).__name__,
                         "message": str(_e)[:400], "retryable": False}
            # v3.8.3-rc3: пометить исход текущей попытки в trace (провайдерный сбой) — видно на 429 и т.п.
            if isinstance(_model_resolution, dict) and _model_resolution.get("model_attempts"):
                _la = _model_resolution["model_attempts"][-1]
                if _la.get("outcome") == "pending":
                    _la["outcome"] = ("provider_%s" % _fail.get("failure_class")
                                      if _fail.get("retryable") else "error:" + str(_fail.get("failure_class")))
            _eff_e = _model_resolution.get("effective_model") if isinstance(_model_resolution, dict) else None
            err_rep = {"schema_version": 1, "kind": "execution-pipeline", "status": "error",
                       "workitem_id": fid, "error": f"{_fail['exception_type']}: {_fail['message']}",
                       "failure": _fail, "ready_for_pr": False, "not_yet": [],
                       "runtime": runtime, "engine": "pipeline", "provider": provider_name,
                       "model": _eff_e or model,
                       "initial_model": (_model_resolution.get("initial_model") if isinstance(_model_resolution, dict) else None),
                       "effective_model": _eff_e,
                       "model_resolution": _model_resolution if isinstance(_model_resolution, dict) else None}
            # v3.0-rc20 (finding аудита P1): DURABLE failure evidence — не только вернуть отчёт, но и
            # ЗАПИСАТЬ свежий run-report.json + failure-handoff, иначе на диске остаётся старый отчёт/
            # handoff прошлого прогона (пользователь думает, что evidence свежее). next_action — безопасный.
            try:
                _safe = ("retry прогон (сбой транзиентный: провайдер/сеть)"
                         if _fail.get("retryable") else
                         "разобрать сбой перед повтором (вероятен дефект/невалидный ввод — не транзиент)")
                _ls.durable_write_json(features_dir / fid / "run-report.json", err_rep)   # v3.0.14 (#2)
                _hf = {"schema_version": 1, "kind": "run-handoff", "workitem_id": fid,
                       "status": "error", "failure": _fail, "retryable": bool(_fail.get("retryable")),
                       "next_action": _safe}
                # v3.0.12: durable failure-handoff (атомарно) — чтобы не оставить наполовину записанный
                # или устаревший handoff прошлого прогона, который resume принял бы за свежий.
                _ls.durable_write(features_dir / fid / "run-handoff.yaml", _hf,
                                  require_keys=("kind", "workitem_id"))
                err_rep["run_report"] = f"features/{fid}/run-report.json"
                err_rep["handoff"] = {"next_action": _safe}
            except Exception:  # noqa: BLE001 — запись evidence не должна маскировать исходный сбой
                pass
            return err_rep
        rep["runtime"] = runtime
        rep["engine"] = "pipeline"
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
        if isinstance(_model_resolution, dict) and _model_resolution.get("model_attempts"):
            _last = _model_resolution["model_attempts"][-1]
            if _last.get("outcome") == "pending":
                _last["outcome"] = ("verified" if rep.get("ready_for_pr")
                                    else "not_ready:" + ",".join((rep.get("gates") or {}).get("unmet") or []))
        _eff = _model_resolution.get("effective_model") if isinstance(_model_resolution, dict) else None
        # v3.7.12/rc3: model = РЕАЛЬНО завершившая модель (effective), не только первоначальная.
        rep["model"] = _eff or (_writer_model if (isinstance(_model_resolution, dict) and _model_resolution.get("applied")) else model)
        if isinstance(_model_resolution, dict) and _model_resolution.get("applied"):
            rep["initial_model"] = _model_resolution.get("initial_model")
            rep["effective_model"] = _model_resolution.get("effective_model")
            if _model_resolution.get("escalation_error"):
                rep["escalation_error"] = _model_resolution["escalation_error"]
        rep["model_resolution"] = _model_resolution   # per-role решение роутера (видимость в каждом отчёте)
        rep["preflight"] = pretruth   # v2.115: preflight пройден (для наблюдаемости в отчёте)
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
        # v3.7.16: hybrid собран ДО прогона и РЕАЛЬНО подан модели (см. _hybrid_fed выше). Записываем что
        # именно было fed_to_model (mode/additions/references), а не пересобираем post-hoc. v1 не теряется.
        if context_hybrid and _hybrid_fed is not None:
            rep["context_hybrid"] = _hybrid_fed
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
        # v3.0.12 (finding аудита блок B): RunHandoff — состояние для resume, пишем DURABLE (атомарно +
        # fsync + перечитывание). Сбой записи БОЛЬШЕ НЕ гаснет молча (иначе на диске остаётся handoff
        # ПРОШЛОГО прогона, и resume продолжит с устаревшего состояния, думая, что оно свежее): фиксируем
        # в lifecycle_errors и в отчёт. build_handoff строится ДО записи run-report, чтобы отразить его исход.
        # v3.0.15 (finding аудита P0): ТРАНЗАКЦИОННЫЙ COMMIT BARRIER. Доставка (PR) происходит ТОЛЬКО ПОСЛЕ
        # надёжной фиксации доказательств и состояния прогона. Порядок:
        #   verification -> durable RunHandoff -> durable final report -> journal checkpoint ->
        #   delivery -> durable delivery result -> run_end.
        # Pipeline вызван с defer_delivery=True: он вернул ДОКАЗАННЫЙ результат + delivery_plan, но PR НЕ
        # открыл. Критические записи здесь — БАРЬЕРЫ: если RunHandoff или final report не зафиксированы
        # durable, доставка НЕ выполняется (fail-closed) — наружу нельзя отдавать то, что локально не зафиксировано.
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
        # DELIVERY — только за барьером: план готов И обе критические записи durable. v3.0.16 Phase A
        # (finding аудита #2): DELIVERY OUTBOX. Внешнее действие (PR) и локальная запись НЕ атомарны, поэтому:
        #   durable DeliveryIntent -> external delivery (идемпотентно) -> durable DeliveryReceipt.
        # Если после внешнего действия запись Receipt упала -> outcome_unknown + reconciliation_required
        # (не притворяемся, что доставки не было). Идемпотентность: pr_open находит существующий PR ветки
        # и не создаёт дубль; delivery_id детерминирован по (wid, branch, commit) — повтор бьёт в ту же запись.
        if _plan and _handoff_ok and _report_ok:
            import hashlib as _hl
            from ai_ops_kit.gates import concurrency_preflight as _cpp
            _branch = _plan["work_branch"]
            _csha = _plan["committed_sha"]
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
                           "base_ref": _plan["base_ref"], "base_sha": _plan["base_sha"],
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
                    _ls.journal_append(_jname, {"kind": "delivery_intent", "run_id": fid, "workitem_id": fid,
                                                "delivery_id": _did, "branch": _branch, "commit": _csha,
                                                "repository": _repo})
                    # ВНЕШНЕЕ ДЕЙСТВИЕ (идемпотентно; delivery_id вшивается в тело PR)
                    _dv = execution_pipeline._deliver_pr(
                        _plan["work_root"], _branch, _plan["base_ref"], _plan["base_sha"],
                        _plan["base_binding"], _csha, _plan["wid"], _plan["task"], delivery_id=_did)
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
                        _ls.journal_append(_jname, {"kind": "delivery_outcome_unknown", "run_id": fid,
                                                    "workitem_id": fid, "delivery_id": _did, "cause": "ambiguous-post"})
                    else:
                        _delivered = _st in ("opened", "updated")
                        _remote_sha = _pr.get("head_sha")
                        _sha_ok = (_remote_sha == _csha) if _remote_sha else None
                        _receipt = {"schema_version": 1, "kind": "DeliveryReceipt", "delivery_id": _did,
                                    "workitem_id": fid, "repository": _repo, "branch": _branch,
                                    "commit_sha": _csha, "base_ref": _plan["base_ref"], "status": _st,
                                    "remote_sha": _remote_sha, "sha_verified": _sha_ok,
                                    "pr_url": _pr.get("url"), "pr_number": _pr.get("number")}
                        _cw = _ls.durable_write(_rp, _receipt,
                                                require_keys=("kind", "delivery_id", "status"), keep_backup=True)
                        if _cw.get("ok"):
                            _ls.durable_write(_ip, {**_intent, "status": "completed"})   # receipt авторитетен
                            rep["delivery"] = {**_dv, "delivery_id": _did, "remote_sha": _remote_sha,
                                               "sha_verified": _sha_ok,
                                               "receipt": f"features/{fid}/delivery-outbox/{_did}.receipt.yaml"}
                            rep["overall_status"] = "delivered" if _delivered else "delivery-failed"
                            _ls.journal_append(_jname, {"kind": "delivery_receipt", "run_id": fid,
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
                            _ls.journal_append(_jname, {"kind": "delivery_outcome_unknown", "run_id": fid,
                                                        "workitem_id": fid, "delivery_id": _did,
                                                        "cause": "receipt-write-failed"})
        elif _plan and not (_handoff_ok and _report_ok):
            # барьер не пройден -> доставку запрещаем fail-closed (не отдаём непрозафиксированное наружу)
            rep["delivery"] = {"requested": True, "status": "blocked-lifecycle",
                               "reason": "durable RunHandoff/final report не зафиксированы — доставка "
                                         "запрещена до надёжной фиксации доказательств и состояния"}
            rep["overall_status"] = "delivery-failed"
            _ls.durable_write_json(features_dir / fid / "run-report.json", rep)
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
            _ls.journal_append(_jname, {"kind": "run_cost", "run_id": fid, "workitem_id": fid,
                                        "attempt_id": _attempt_id, **_cost_rep})
            # v3.10.0 Usage Truth: персист КАЖДОГО вызова (writer/reviewer/fix-loop/fallback/escalation)
            # в ledger задачи + продукта. Честный usage_status; неизвестное -> unavailable, не 0.
            # v3.24.0 Cost & Architecture Accuracy: extra_context штампуется на все записи —
            # task_type/workflow/risk/size/writer_tier/execution_mode/stack для economic alternatives.
            try:
                from ai_ops_kit.providers import usage_ledger as _ul
                _extra = {
                    "task_type": signals.get("task_type"),
                    "workflow": (_plan.get("base_workflow") if isinstance(_plan, dict) else None),
                    "risk": signals.get("risk"),
                    "size": signals.get("size"),
                    "writer_tier": ((_model_resolution or {}).get("writer") or {}).get("tier"),
                    "execution_mode": "sequential" if signals.get("_sequence_internal") else "single",
                    "stack": ",".join(s.get("language", "") for s in (signals.get("_stacks") or [])) or None,
                }
                _ul.append(child_root, fid, _stats, run_id=fid, extra_context={k: v for k, v in _extra.items() if v is not None})
            except Exception as _ue:  # noqa: BLE001 — учёт usage не должен ронять прогон...
                # ...но и пропасть молча не должен: занижённая стоимость, поданная как факт, —
                # это нарушение той самой Usage Truth, ради которой ledger и существует.
                _note_bookkeeping_error(rep, "usage_ledger.append", _ue)
        try:
            orchestrator.clear_call_context()
        except Exception:  # noqa: BLE001
            pass
        # v3.16.0 Development Culture Guardrails (WP5): каждый прогон завершается SessionRecommendation
        # (гигиена сессии/контекста) с точной командой. ADVISE-ONLY: НЕ блокирует прогон/доставку.
        # Контекст оценивается по ledger (estimated) — рантайм может уточнить через `ai-ops session --context`.
        try:
            from ai_ops_kit.engops import session_telemetry as _st
            from ai_ops_kit.engops import session_guardrails as _sg
            _snap = _st.snapshot(child_root, workitem_id=fid)
            _pol = _sg.load_policy(child_root)
            _done = bool(rep.get("ready_for_pr"))
            _pr = rep.get("pull_request") or (rep.get("delivery") or {}).get("pr_url")
            if _done:
                _rit = _sg.completion_ritual(_snap, _pol, workitem_id=fid, pr=_pr,
                                             next_relation="new_independent_task",
                                             at_safe_boundary=True, repo_path=str(child_root))
                rep["session_recommendation"] = _rit["session_recommendation"]
                rep["completion_ritual"] = {k: _rit[k] for k in
                                            ("completion_checklist", "complete", "next_command")}
            else:
                rep["session_recommendation"] = _sg.recommend(_snap, _pol,
                                                              next_relation="continuation", task_done=False)
        except Exception:  # noqa: BLE001 — совет по гигиене сессии не должен ронять прогон
            pass
        # run_end (исход прогона, включая итог доставки)
        _ls.journal_append(_jname, {"kind": "run_end", "run_id": fid, "workitem_id": fid,
                                    "attempt_id": _attempt_id,
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
        with contextlib.redirect_stdout(sys.stderr):
            active_work.finish_cmd(aw_path, fid, status=_st, reason=_why)
        return rep

    # 1-2. RunPlan (route + треки + агрегированные гейты).
    # feature (v2.51): привязка WorkItem к ИМЕНОВАННОЙ фиче — иначе wid=wi-<hash>, и срезы
    # истории падают на новую фичу с 1 срезом (baseline не двигается — finding обкатки 5).
    plan = run_plan.build_plan(signals, workitem_id=feature)
    fid = plan["workitem_id"]
    base_wf = plan["base_workflow"]

    # 3. WorkItem
    workitem.start(str(features_dir), fid, task_text,
                   task_type=signals.get("task_type"), risk=signals.get("risk"))

    # 4. RunPlan на диск — v3.0.16 Phase A (finding аудита #3): единые write-barriers и в этом пути.
    # RunPlan — барьер: сбой durable-записи -> прогон не начинаем (0 исполнения).
    _pw2 = _ls.durable_write(features_dir / fid / "run-plan.yaml", plan)
    if not _pw2.get("ok"):
        return {"schema_version": 1, "kind": "run-report", "workitem_id": fid, "status": "error",
                "error": f"lifecycle fail-closed: не удалось надёжно сохранить RunPlan ({_pw2.get('error')})"}

    # 5. регистрация активной работы (координация параллельных сессий)
    aw_path = child_root / ".ai" / "runtime" / "active-work.yaml"
    areas = signals.get("affected_areas") or ["unspecified"]
    active_work.register(aw_path, fid, f"feature/{fid}", areas, session,
                         workitem=f"features/{fid}/workitem.yaml")

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
        # честно: в planned run_state — ОБЕЩАНИЕ пути; папку workitems/<id>/ создаёт
        # рантайм при реальном исполнении стадий, не контроллер. Не полагаться на её
        # наличие после planned-прогона (finding обкатки v2.34).
        "run_state_materialized": run_state_materialized,
        "artifacts": {"workitem": f"features/{fid}/workitem.yaml",
                      "run_plan": f"features/{fid}/run-plan.yaml"},
        # v3.0.16 Phase A (finding аудита #3): этот путь — planning/orchestration; ВНЕШНЯЯ ДОСТАВКА (PR) НЕ
        # выполняется здесь. Транзакционные execution+delivery-гарантии (commit barrier, DeliveryIntent/
        # Receipt, reconciliation) — ТОЛЬКО в pipeline-пути (engine=pipeline). Явно, чтобы путь не
        # претендовал на те же гарантии.
        "delivery": {"requested": False, "status": "not-applicable",
                     "reason": "controller/planning путь: внешняя доставка не выполняется; "
                               "execution+delivery-гарантии — только engine=pipeline"},
    }
    # report — write barrier: сбой durable-записи фиксируем в отчёте (не молча)
    _rw2 = _ls.durable_write_json(features_dir / fid / "run-report.json", report)
    if not _rw2.get("ok"):
        report["lifecycle_errors"] = [f"run-report durable-write: {_rw2.get('error')}"]
    return report


def _print_pipeline(r):
    """Человекочитаемый вывод отчёта собранного движка (kind=execution-pipeline).

    finding аудита (P0.1): print_human безусловно читал ключи controller-отчёта
    (status/execution/required_tracks) и падал KeyError на pipeline-отчёте. Формат отчёта
    движка иной (loop/commit/checks/gates/ready_for_pr) — печатаем его явно.
    """
    if r.get("status") == "error":
        print(f"ai-ops run (pipeline) → WorkItem {r.get('workitem_id')} [ОШИБКА]")
        print(f"  {r.get('error')}")
        return
    loop = r.get("loop") or {}
    commit = r.get("commit") or {}
    gates = r.get("gates") or {}
    ready = r.get("ready_for_pr")
    print(f"ai-ops run (pipeline) → WorkItem {r.get('workitem_id')} "
          f"[{'READY_FOR_PR' if ready else 'NOT_READY'}]")
    prov = r.get("provider") or "?"
    model = f"/{r['model']}" if r.get("model") else ""
    print(f"  base_workflow: {r.get('base_workflow')} · провайдер: {prov}{model} ({r.get('runtime')})")
    _stacks = (r.get("profile") or {}).get("display") or _stacks_human(r.get("profile"))
    print(f"  стек: {', '.join(_stacks) or 'не определён'}")
    _changed = commit.get("changed_files")
    _files_note = f" · файлов в коммите {len(_changed)}" if _changed is not None and commit.get("sha") else ""
    print(f"  tool-loop: {loop.get('stopped')} · шагов {loop.get('steps')} · "
          f"правок через брокера {loop.get('applied_writes')} · "
          f"отклонено {loop.get('denied')}{_files_note}")
    # F-017 + находка ии-среды: правок через брокера 0, а файлы в коммите есть — работа сделана
    # другим каналом. Прежде строка «правок 0» стояла первой и читалась как «ничего не произошло»,
    # хотя коммит был. Теперь канал НАЗВАН, а не выведен читателем.
    _by = {"broker": "через брокера", "shell": "напрямую в дереве (writer или shell)",
           "model-commit": "модель закоммитила сама"}.get(commit.get("produced_by"))
    if _changed and not (loop.get("applied_writes") or 0):
        print(f"    работа произведена {_by or 'не через брокера'}: {', '.join(_changed[:5])}"
              + (f" и ещё {len(_changed) - 5}" if len(_changed) > 5 else ""))
    # F-012: движок никого не позвал и ничего не написал — назвать режим и что делать дальше.
    # Раньше это читалось только по косвенным признакам (созданный worktree + «not_yet: живой
    # предложитель»), и исполнитель догадывался, что код должен написать он.
    # Тот же предикат, что и у статуса работы: «движок ничего не написал» нельзя объявлять по
    # счётчику брокера, если в коммите лежат файлы.
    if (r.get("provider") == "mock") and not work_produced(r):
        _wt = (r.get("isolation") or {}).get("worktree")
        _br = (r.get("commit") or {}).get("branch") or f"ai-ops/{r.get('workitem_id')}"
        print("  исполнитель: внешний агент — движок с провайдером mock кода НЕ пишет")
        print(f"    рабочий каталог: {_wt or 'основное дерево'} · ветка: {_br}")
        print("    напиши правки там, закоммить, затем переоцени гейты: "
              f"ai-ops run \"<задача>\" . --feature {r.get('workitem_id')} --execute --reevaluate-only")
        print("    или задай живого провайдера: --provider claude-cli (нужен claude в PATH)")
    iso = (r.get("isolation") or {}).get("worktree")
    print(f"  изоляция: {iso or 'основное дерево (без worktree)'}")
    # F-014: от какой базы отрезан worktree — видно сразу, а не выясняется конфликтом при слиянии.
    _bb = r.get("base_binding") or (r.get("delivery") or {}).get("base_binding") or {}
    if _bb.get("base_ref"):
        _src = {"current-branch": "текущая ветка", "upstream": "upstream",
                "remote-default": "remote default", "explicit-local": "задана явно",
                "explicit-remote": "задана явно (origin)"}.get(_bb.get("source"), _bb.get("source"))
        print(f"  база worktree: {_bb['base_ref']} {(_bb.get('base_sha') or '')[:12]} ({_src})")
    if commit.get("sha"):
        print(f"  commit: {commit['sha'][:12]} на {commit.get('branch')} · "
              f"evidence на точном SHA: {commit.get('evidence_on_exact_sha')} · "
              f"дерево чистое: {commit.get('tree_clean_before_checks')}")
    if r.get("exemptions"):
        print(f"  освобождены (не применимо): {', '.join(r['exemptions'])}")
    if r.get("tests_warn"):
        print(f"  ⚠ {r['tests_warn']}")
    print(f"  гейты: оценено {len(gates.get('evaluated') or [])} · "
          f"не закрыто {gates.get('unmet') or []} · блокирует: {gates.get('blocked')}")
    lc = r.get("lifecycle")
    if lc:
        pf = (lc.get("concurrency_preflight") or {})
        if isinstance(pf, dict) and pf.get("error"):
            _pf_note = f"preflight НЕ ВЫПОЛНЕН ({pf['error']}) — о конфликтах ничего не известно"
        elif isinstance(pf, dict) and pf.get("conflicts") is None and "error" in pf:
            _pf_note = "preflight не выполнен"
        else:
            _pf_note = f"preflight-конфликтов: {len(pf.get('conflicts') or []) if isinstance(pf, dict) else 0}"
        print(f"  lifecycle: WorkItem+RunPlan+active-work+run-report записаны · {_pf_note}")
    cb = r.get("context_bundle")
    if cb:
        print(f"  context: ~{cb['estimated_tokens']}/{cb['context_budget']} ток."
              f"{' ⚠OVERFLOW' if cb.get('overflow') else ''} · агентов {len(cb['agents'])} · "
              f"исключено {cb['excluded_count']} источн.")
    sc = r.get("spec_coverage")
    if sc:
        esc = f" (эскалация с L{sc['escalated_from']})" if sc.get("escalated_from") is not None else ""
        print(f"  spec-level: {sc['level_name']}{esc} · не хватает разделов: "
              f"{len(sc['blocking_missing'])} · needs_human: {len(sc['needs_human'])}")
    wp = r.get("work_package")
    if wp and wp.get("should_decompose"):
        print(f"  ⚠ пакет не атомарен — рекомендуется декомпозиция ({', '.join(wp['decomposition_axes'])})")
    pr = r.get("draft_pr")
    if pr:
        print(f"  draft PR: {pr.get('status')}" + (f" — {pr.get('url')}" if pr.get('url') else ""))
    for n in r.get("not_yet") or []:
        print(f"  · not_yet: {n}")
    _print_contour_consistency(r)


def _print_contour_consistency(r):
    """Находки гейта связности контуров — человеку, в конце прогона.

    ГЕЙТ, ЧЬИ НАХОДКИ НЕ ВИДНЫ, — ЭТО ГЕЙТ, КОТОРОГО НЕТ. Гейт исполнялся, считал находки и писал
    их в evidence; вывод прогона о них молчал. Единственное место, где «описание продукта отстало от
    кода» было видно, — yaml-артефакт, который человек не открывает. Это тот же дефект, что
    «переводчик написан и не подключён», только дороже: здесь молчит главная проверка релиза 3.35.

    Печатается ПОСЛЕ вердикта прогона и отдельным блоком: находка advisory, она не отменяет
    результат, но и не должна тонуть среди строк о шагах и коммитах.
    """
    cc = r.get("contour_consistency") or {}
    rep = cc.get("report")
    if not rep:
        # Гейт не исполнялся (не коммитили) либо проверка не удалась — evidence уже сказал об этом
        # своим `warn`, и выдумывать здесь ещё одно сообщение незачем.
        return
    try:
        from ai_ops_kit.ui import presenter
        msg = presenter.from_contour_consistency(rep)
        if msg.get("status") == "ok":
            return          # согласовано — отдельного блока не нужно, вердикт прогона уже сказал всё
        print()
        print(presenter.render(msg, audience=presenter.audience_from_config(
            r.get("child_root") or ".")))
    except Exception as _e:  # noqa: BLE001 — вывод отчёта не роняет прогон...
        # ...но и молчать нельзя: молчание здесь неотличимо от «расхождений нет».
        print(f"  ⚠ находки связности контуров есть, показать не смог: {type(_e).__name__}: {_e}")


def print_human(r):
    # pipeline-отчёт имеет свою форму — не смешиваем с controller-отчётом (P0.1)
    if r.get("kind") == "execution-pipeline":
        return _print_pipeline(r)
    print(f"ai-ops run → WorkItem {r['workitem_id']} [{r['status']}]")
    print(f"  base_workflow: {r['base_workflow']} · execution: {r['execution']} ({r['runtime']})")
    if r["required_tracks"]:
        print(f"  треки (required): {', '.join(r['required_tracks'])}")
    if r["conditional_tracks"]:
        print(f"  треки (conditional): {', '.join(r['conditional_tracks'])}")
    print(f"  гейты ({len(r['gates'])}): {', '.join(r['gates'])}")
    for s in r["skipped_tracks"]:
        print(f"  · пропущен {s['track']}: {s['reason']}")
    if r["status"] == "planned":
        print("  → план и каркас готовы; стадии исполняет рантайм (claude-code) по плану.")


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


def main(argv):
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
    rp.add_argument("--engine", default="controller", choices=["controller", "pipeline"],
                    help="controller (план+каркас) или pipeline (собранный движок: detect->tool-loop->evidence->гейты->PR)")
    rp.add_argument("--model", help="ID модели для провайдера (напр. deepseek-chat); engine=pipeline")
    rp.add_argument("--open-pr", action="store_true",
                    help="открыть draft PR по результату (нужен GITHUB_TOKEN); engine=pipeline")
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
    rs.add_argument("--replan", action="store_true",
                    help="осознанно сменить классификацию/policy при продолжении (не resume, а replan "
                         "с ревалидацией) — иначе смена task_type/risk/write_scope блокируется")
    a = ap.parse_args(argv)
    if a.cmd == "resume":
        from ai_ops_kit.engine import run_handoff
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
                    print(f"  продолжить: ai-ops resume {a.child_root} {a.feature} --execute"
                          f"{' --force' if reval else ''}   (worktree/ветка переиспользуются; "
                          f"{'нужна ревалидация -> --force' if reval else 'база актуальна'})")
            return 0 if pf["can_resume"] else 1
        # РЕАЛЬНОЕ продолжение (v2.109)
        task = a.task or (pf.get("next_action") if pf.get("can_resume") else None) or "продолжить работу"
        report = run(task, json.loads(a.signals), Path(a.child_root),
                     provider_name=a.provider or "mock", model=a.model, engine="pipeline",
                     execute=True, feature=a.feature, resume=True, force_resume=a.force, base=a.base,
                     replan=a.replan)
        rinfo = report.get("resume") or {}
        if a.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"ai-ops resume {a.feature}: status={report.get('status') or report.get('overall_status')} · "
                  f"resumed={rinfo.get('resumed')} · reused_branch={rinfo.get('reused_branch')}")
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
