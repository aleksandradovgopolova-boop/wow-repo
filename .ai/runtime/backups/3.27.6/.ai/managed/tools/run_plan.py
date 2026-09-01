#!/usr/bin/env python3
"""RunPlan — план исполнения задачи (v2.32, Execution Engine Фаза 1).

Модель «один workflow» заменяется на base_workflow + tracks. base_workflow (из ai_route)
задаёт основной характер задачи; tracks (registry/tracks.yaml) — обязательные области
качества, ВЫВЕДЕННЫЕ из затронутых зон. Трек добавляет свои гейты к гейтам base_workflow.
Так «Design/Analytics/Docs by Default» становится механикой: пользователь не обязан
помнить про состояния экранов, события, документацию, rollback — система выводит их из
сигналов и явно объясняет пропуски (explainable skips).

Использование:
  run_plan.py plan --signals '<json>' [--workitem-id id] [--json]
  run_plan.py validate [run-plan.yaml]      # + всегда проверяет целостность tracks.yaml
  run_plan.py --selftest
Возврат 0 — ок, 1 — ошибка.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]

# finding аудита (P1.1): workitem_id доходит до путей (worktree add строит root/<wt>/<wid>).
# Пускаем только безопасный slug — иначе `../`, абсолютные пути и разделители дают traversal.
WORKITEM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def validate_workitem_id(wid):
    """Возвращает wid если он безопасный slug, иначе бросает ValueError.

    Отвергаем всё, что может выйти за пределы каталога worktree: разделители пути,
    `..`, абсолютные пути, пустое/слишком длинное. Точку-в-начале и '..' режем явно.
    """
    if not isinstance(wid, str) or not WORKITEM_ID_RE.match(wid):
        raise ValueError(
            f"недопустимый workitem_id {wid!r}: разрешён slug {WORKITEM_ID_RE.pattern} "
            "(нижний регистр, [a-z0-9._-], без '/', '\\', '..', 1..64 символа)")
    if wid.startswith(".") or ".." in wid:
        raise ValueError(f"недопустимый workitem_id {wid!r}: '.' в начале или '..' запрещены")
    return wid


def load(rel):
    return yaml.safe_load((PKG / rel).read_text(encoding="utf-8"))


def _base_workflow(signals):
    """base_workflow из ai_route; при сбое — честный fallback. -> (workflow, reasons, confidence)."""
    try:
        sys.path.insert(0, str(PKG / "validation"))
        import ai_route
        d = ai_route.route(signals)
        return d.get("workflow"), d.get("reasons", []), d.get("classification_confidence", "normal")
    except Exception as e:  # маршрутизатор не должен ронять планирование
        tt = signals.get("task_type")
        wfs = load("registry/workflows.yaml")["workflows"]
        wf = tt if tt in wfs else "ENGINEERING"
        return wf, [f"fallback base_workflow={wf} (ai_route недоступен: {e})"], "normal"


def build_plan(signals, workitem_id=None):
    tracks = load("registry/tracks.yaml")["tracks"]
    wfs = load("registry/workflows.yaml")["workflows"]
    base_wf, route_reasons, classification_confidence = _base_workflow(signals)
    base_gates = list(wfs.get(base_wf, {}).get("quality_gates", []))

    required, conditional, skipped = [], [], []
    gates = list(base_gates)
    for name, t in tracks.items():
        active = bool(signals.get(t.get("signal")))
        entry = {"track": name, "reason": t.get("reason") if active else t.get("skip_reason"),
                 "gates": list(t.get("gates", []))}
        if not active:
            skipped.append({"track": name, "reason": t.get("skip_reason")})
            continue
        (required if t.get("kind") == "required" else conditional).append(entry)
        for g in t.get("gates", []):
            if g not in gates:
                gates.append(g)

    task_text = signals.get("task_text", "")
    task_hash = hashlib.sha256(task_text.encode("utf-8")).hexdigest()[:12] if task_text else None
    # Явный workitem_id валидируем (может дойти до путей); авто-сгенерированный безопасен by construction.
    wid = validate_workitem_id(workitem_id) if workitem_id else (f"wi-{task_hash}" if task_hash else "wi-unknown")
    return {
        "schema_version": 1, "kind": "run-plan",
        "workitem_id": wid, "task_hash": task_hash,
        "base_workflow": base_wf,
        "required_tracks": required, "conditional_tracks": conditional, "skipped_tracks": skipped,
        "gates": gates, "route_reasons": route_reasons,
        "classification_confidence": classification_confidence,
        "execution_budget": {"max_cost": None, "max_duration": None, "max_model_calls": None},
    }


def validate_tracks():
    """Целостность registry/tracks.yaml: гейты треков резолвятся, поля на месте."""
    errors = []
    tracks = load("registry/tracks.yaml").get("tracks", {})
    gate_ids = set(load("quality/gates.yaml")["gates"])
    for name, t in tracks.items():
        if not t.get("signal"):
            errors.append(f"трек {name}: нет signal")
        if t.get("kind") not in ("required", "conditional"):
            errors.append(f"трек {name}: kind '{t.get('kind')}' не в required|conditional")
        if not t.get("skip_reason"):
            errors.append(f"трек {name}: нет skip_reason (explainable skip обязателен)")
        for g in t.get("gates", []) or []:
            if g not in gate_ids:
                errors.append(f"трек {name}: гейт '{g}' отсутствует в quality/gates.yaml")
    return errors


def validate_plan(data):
    errors = []
    if data.get("kind") != "run-plan":
        errors.append("kind должен быть 'run-plan'")
    if not data.get("base_workflow"):
        errors.append("нет base_workflow")
    gate_ids = set(load("quality/gates.yaml")["gates"])
    for g in data.get("gates", []) or []:
        if g not in gate_ids:
            errors.append(f"gates: '{g}' отсутствует в quality/gates.yaml")
    for key in ("required_tracks", "conditional_tracks", "skipped_tracks"):
        for e in data.get(key, []) or []:
            if not e.get("reason"):
                errors.append(f"{key}: у трека '{e.get('track')}' нет reason")
    return errors


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    expect("tracks.yaml целостен (гейты резолвятся)", validate_tracks() == [])

    # продуктовая фича: UI + измеримое поведение + граница безопасности
    sig = {"task_type": "PRODUCT", "risk": "medium",
           "available_providers": ["anthropic"], "available_runtimes": ["claude-code"],
           "ui_changed": True, "measurable_behavior": True, "security_surface_changed": True,
           "user_facing_change": True, "task_text": "фильтр по статусу в каталоге заказов"}
    p = build_plan(sig)
    req = {t["track"] for t in p["required_tracks"]}
    expect("PRODUCT + UI/analytics/security -> треки VISUAL/ANALYTICS/SECURITY/DOCUMENTATION",
           {"VISUAL", "ANALYTICS", "SECURITY", "DOCUMENTATION"} <= req)
    # аудит: PRODUCT сам по себе не имел ux/analytics гейтов — трек их добавил
    expect("гейты треков добавлены к base (ux_review/analytics_design_readiness/security)",
           {"ux_review", "analytics_design_readiness", "security"} <= set(p["gates"]))
    expect("base_workflow = PRODUCT", p["base_workflow"] == "PRODUCT")
    expect("plan валиден", validate_plan(p) == [])

    # пропуски объяснены: без UI VISUAL уходит в skipped с причиной
    sig2 = dict(sig); sig2["ui_changed"] = False
    p2 = build_plan(sig2)
    vis_skip = next((t for t in p2["skipped_tracks"] if t["track"] == "VISUAL"), None)
    expect("нет UI -> VISUAL в skipped с причиной", vis_skip and "UI" in vis_skip["reason"])
    expect("skipped VISUAL -> его гейтов нет в наборе", "ux_review" not in p2["gates"])

    # conditional: AI-компонент -> AI в conditional_tracks
    sig3 = dict(sig); sig3["ai_component"] = True
    p3 = build_plan(sig3)
    expect("ai_component -> AI в conditional_tracks",
           any(t["track"] == "AI" for t in p3["conditional_tracks"]))
    expect("AI-трек добавил ai_red_team", "ai_red_team" in p3["gates"])

    # P1.1: валидация workitem_id (доходит до путей)
    expect("валидный wid принят", validate_workitem_id("wi-abc_123.v2") == "wi-abc_123.v2")
    expect("wid принят в build_plan", build_plan(sig, "feat-42")["workitem_id"] == "feat-42")
    for bad in ["../evil", "a/b", "/abs", "..", ".hidden", "UPPER", "x" * 65, "", "a b", "a\\b"]:
        try:
            validate_workitem_id(bad)
            expect(f"невалидный wid отвергнут: {bad!r}", False)
        except ValueError:
            expect(f"невалидный wid отвергнут: {bad!r}", True)

    print("run_plan selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    ap = argparse.ArgumentParser(prog="run_plan.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("plan")
    pp.add_argument("--signals", required=True, help="JSON сигналов задачи")
    pp.add_argument("--workitem-id"); pp.add_argument("--json", action="store_true")
    vp = sub.add_parser("validate")
    vp.add_argument("file", nargs="?")
    a = ap.parse_args(argv)

    if a.cmd == "plan":
        signals = json.loads(a.signals)
        plan = build_plan(signals, a.workitem_id)
        if a.json:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
        else:
            print(yaml.safe_dump(plan, allow_unicode=True, sort_keys=False))
        return 0
    if a.cmd == "validate":
        errs = validate_tracks()
        if a.file:
            data = yaml.safe_load(Path(a.file).read_text(encoding="utf-8")) or {}
            errs += validate_plan(data)
        if errs:
            print("RUN-PLAN: ошибки:")
            for e in errs:
                print(f"  - {e}")
            return 1
        print("RUN-PLAN-OK: tracks.yaml целостен" + (", run-plan валиден" if a.file else "") + ".")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
