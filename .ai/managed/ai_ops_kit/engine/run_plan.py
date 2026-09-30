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
from __future__ import annotations

# v4: самодостаточный вход — файл можно запустить напрямую (без PYTHONPATH). Кладём корень пакета
# (маркер VERSION) в sys.path ДО пакетных импортов — раньше это делал плоский shim tools/ через
# _bootstrap; теперь точка входа сама себя обслуживает.
import sys as _sys
from pathlib import Path as _P_bootstrap
_root = next((_p for _p in _P_bootstrap(__file__).resolve().parents if (_p / "VERSION").is_file()), None)
if _root is not None and str(_root) not in _sys.path:
    _sys.path.insert(0, str(_root))

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
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
        from ai_ops_kit.shared import _bootstrap  # noqa: F401 — импорт ради side-effect: кладёт корень пакета в sys.path
        from ai_ops_kit.shared import ai_route
        d = ai_route.route(signals)
        return d.get("workflow"), d.get("reasons", []), d.get("classification_confidence", "normal")
    # Причина подавления ЗАПИСАНА (срез engine ратчета 2026-08-12): это ПРАВИЛЬНЫЙ образец —
    # отказ не гасится, а становится видимой строкой в `reasons`, которая уезжает в план и в отчёт
    # («fallback base_workflow=... (ai_route недоступен: ...)»). Тип не сужен намеренно: сюда
    # приходит любой сбой импорта/классификации, и любой из них обязан дать честный fallback, а не
    # уронить планирование. Тихого пути здесь нет.
    except Exception as e:  # noqa: BLE001 — отказ роутера становится ВИДИМОЙ причиной fallback, не тишиной
        tt = signals.get("task_type")
        wfs = load("registry/workflows.yaml")["workflows"]
        wf = tt if tt in wfs else "ENGINEERING"
        return wf, [f"fallback base_workflow={wf} (ai_route недоступен: {e})"], "normal"


def _escalation_disclosure(base_wf, current_gates, confidence, wfs):
    """Честное раскрытие ПРЕДВАРИТЕЛЬНОЙ классификации: что добавит прогон, если поднимется уровень.

    Находка поля (obs 64a4840a/8a891ce7): когда тяжесть задачи (size/risk) не заявлена, ai_route
    выбирает QUICK и помечает classification 'low'. Человек видит план на 3 гейта и форму L0 QUICK,
    а прогон, получив size/risk, эскалирует в ENGINEERING — применяет 11 гейтов и требует форму L1.
    План обязан НАЗВАТЬ это ДО заполнения, а не задним числом: 3 гейта — не окончательный набор.

    Это симметрично честности треков: не утверждение факта о ненаписанном коде, а условие эскалации
    («при явном сигнале тяжести …»). Эскалация детерминирована ai_route: неизвестная тяжесть -> QUICK
    (low); явный сигнал тяжести (size medium+/risk medium+) -> ENGINEERING.

    -> (provisional: bool, disclosure: dict|None).
    """
    if confidence != "low" or base_wf != "QUICK" or "ENGINEERING" not in wfs:
        return False, None
    esc_wf = "ENGINEERING"
    esc_gates = [g for g in (wfs[esc_wf].get("quality_gates") or []) if g not in current_gates]
    disclosure = {
        "reason": ("тяжесть задачи (size/risk) не заявлена в сигналах — классификация "
                   "предварительная; при явном сигнале тяжести прогон эскалирует, и набор гейтов и "
                   "глубина формы вырастут (не факт о коде: план строится до правок)"),
        "escalation_workflow": esc_wf,
        "gates_if_escalated": esc_gates,
    }
    # Форма спецификации растёт вместе с уровнем: назвать разделы, которые добавит L1, ДО заполнения
    # (obs 8a891ce7 — человек заполнил форму L0 и узнал про L1 только на run).
    try:
        from ai_ops_kit.gates import spec_levels
        cur_lvl = spec_levels.TASK_TYPE_LEVEL.get(base_wf, 0)
        esc_lvl = spec_levels.TASK_TYPE_LEVEL.get(esc_wf, 1)
        disclosure["spec_level_if_escalated"] = spec_levels.LEVEL_NAME.get(esc_lvl)
        _cur = set(spec_levels.required_sections(cur_lvl))
        disclosure["spec_sections_if_escalated"] = [
            s for s in spec_levels.required_sections(esc_lvl) if s not in _cur]
    except Exception as e:  # noqa: BLE001 — раскрытие формы не критично: гейты уже названы честно
        disclosure["spec_sections_error"] = f"глубина формы не раскрыта: {e}"
    return True, disclosure


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
    provisional, disclosure = _escalation_disclosure(base_wf, gates, classification_confidence, wfs)
    return {
        "schema_version": 1, "kind": "run-plan",
        "workitem_id": wid, "task_hash": task_hash,
        "base_workflow": base_wf,
        "required_tracks": required, "conditional_tracks": conditional, "skipped_tracks": skipped,
        "gates": gates, "route_reasons": route_reasons,
        "classification_confidence": classification_confidence,
        # Один источник истины о наборе гейтов: этот же `gates` прогон и применяет (gate_executor
        # получает gate_ids=plan["gates"]). Когда классификация предварительна, план не выдаёт 3
        # гейта за окончательные, а честно называет эскалацию (см. _escalation_disclosure).
        "classification_provisional": provisional,
        "escalation_disclosure": disclosure,
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
    # Раскрытие эскалации: провизорный план ОБЯЗАН его нести, окончательный — не должен (иначе
    # план снова обещает не то, что применит прогон). Названные к добавлению гейты обязаны резолвиться.
    disc = data.get("escalation_disclosure")
    if data.get("classification_provisional"):
        if not disc:
            errors.append("classification_provisional=true, но escalation_disclosure отсутствует")
        else:
            for g in disc.get("gates_if_escalated", []) or []:
                if g not in gate_ids:
                    errors.append(f"escalation_disclosure.gates_if_escalated: '{g}' отсутствует в quality/gates.yaml")
    elif disc:
        errors.append("escalation_disclosure присутствует при classification_provisional=false")
    return errors


def main(argv):
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
