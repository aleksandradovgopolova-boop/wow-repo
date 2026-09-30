#!/usr/bin/env python3
"""Контекст прогона ai-ops run: резолв провайдера, скан незавершённых intents, resume-контекст, профиль.

Вынесено из `ai_ops_run_exec` без изменения поведения (чистый перенос + ре-экспорт) — тот же приём,
что уже применён для pipeline/tool-broker-спутников. Здесь живёт самодостаточный кластер: выбор
провайдера (`resolve_provider_for_run`, `_with_provider_fallback`, `_provider_trust`, `_load_klp_by_env`),
сканирование outbox на незавершённую доставку (`_outbox_dir`, `_unresolved_intents`,
`_nonfinal_receipt_intents`), восстановление продуктовой задачи и состояния для продолжения
(`product_task_for_resume`, `is_service_text`, `_SERVICE_TASK_MARKERS`, `_resume_context_from_handoff`)
и профиль стека для отчёта (`_profile_for_report`). Ни одна из этих функций НЕ зовёт fix-loop/preflight/
аргпарсер фасада — обратного ребра нет, цикла импорта нет. Ре-экспорт в `ai_ops_run_exec` держит вызовы
`ai_ops_run_exec.<name>` (и через него `ai_ops_run.<name>`) на прежних именах.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from ai_ops_kit.engine.pipeline_helpers import _stacks_human   # noqa: E402
from ai_ops_kit.shared import lifecycle_store as _ls   # noqa: E402


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


# --- продуктовая задача при продолжении (F-027) -------------------------------------------------
# Тексты, которые кит генерирует САМ как «следующий шаг» (build_handoff). На продолжении они
# оказывались ЗАДАЧЕЙ исполнителя: автор честно писал требования про гейты кита вместо продукта,
# а продуктовая спека оставалась цела — потому и выглядело осмысленно.
_SERVICE_TASK_MARKERS = (
    "закрыть незакрытые гейты",
    "открыть/обновить draft PR",
    "продолжить реализацию (петля остановилась",
    "проверить отчёт и решить следующий шаг",
    "продолжить работу",
)


def is_service_text(text):
    """Похоже ли на служебный next_action кита (а не на продуктовую задачу)."""
    t = (text or "").strip().lower()
    return bool(t) and any(t.startswith(m.lower()) for m in _SERVICE_TASK_MARKERS)


def product_task_for_resume(child_root, wid, features_dir=None):
    """F-027: восстановить ПРОДУКТОВУЮ задачу для продолжения. -> {"task": str|None, "source": str}.

    Порядок источников: run-settings исходного прогона (contract прогона) -> workitem.yaml ->
    раздел `goal` спеки. Служебные тексты кита отбрасываются на каждом источнике: workitem.yaml
    прошлого resume мог быть уже испорчен ими (так и было в поле). Ничего не нашли — говорим
    прямо, а не подставляем «что осталось»: задача исполнителя обязана оставаться продуктовой."""
    import yaml
    root = Path(child_root)
    fdir = Path(features_dir) if features_dir else root / "features"
    candidates = []
    try:
        _s = yaml.safe_load((fdir / str(wid) / "run-settings.yaml").read_text(encoding="utf-8")) or {}
        candidates.append(("run-settings", (_s.get("task") if isinstance(_s, dict) else None)))
    except (OSError, yaml.YAMLError):
        pass
    try:
        _w = yaml.safe_load((fdir / str(wid) / "workitem.yaml").read_text(encoding="utf-8")) or {}
        candidates.append(("workitem", (_w.get("task") if isinstance(_w, dict) else None)))
    except (OSError, yaml.YAMLError):
        pass
    try:
        _sp = yaml.safe_load((fdir / str(wid) / "spec.yaml").read_text(encoding="utf-8")) or {}
        _goal = ((_sp.get("sections") or {}).get("goal") or {}) if isinstance(_sp, dict) else {}
        candidates.append(("spec:goal", _goal.get("content") if isinstance(_goal, dict) else None))
    except (OSError, yaml.YAMLError):
        pass
    for source, text in candidates:
        if isinstance(text, str) and text.strip() and not is_service_text(text):
            return {"task": " ".join(text.split()), "source": source}
    return {"task": None, "source": "не найдено"}


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


def _nonfinal_receipt_intents(features_dir, fid, branch=None):
    """#400 (обратная связь ИИ-Среды): DeliveryIntent'ы, у которых Receipt ЕСТЬ, но он НЕ финально
    подтверждён (`sha_verified` != True — mismatch / not-delivered / ложный false из гонки чтения
    head_sha, P0/#399). Такие надо перепроверить против СВЕЖЕГО remote: `_unresolved_intents` смотрит
    лишь НАЛИЧИЕ файла receipt, поэтому однажды записанный ложный false залипал навсегда и remote,
    уже совпавший с коммитом, больше не сверялся (приходилось руками удалять файл леджера).
    Финально-подтверждённый (`sha_verified` is True) не трогаем — он окончателен."""
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
        rg = _ls.load_guarded(rp, kind="DeliveryReceipt")
        if rg["state"] != "ok":
            continue  # receipt отсутствует/битый — это область _unresolved_intents, не наша
        if (rg["data"] or {}).get("sha_verified") is True:
            continue  # финально подтверждён — окончателен, не перепроверяем
        out.append((did, intent))
    return out


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
