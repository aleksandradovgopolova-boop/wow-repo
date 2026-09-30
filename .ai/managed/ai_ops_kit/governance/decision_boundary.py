#!/usr/bin/env python3
"""Единый классификатор границы решений (issue #631): три оси РАЗОМ → ИМЯ класса + причина.

ЗАЧЕМ. `registry/decision-boundary.yaml` уже НАЗВАЛ модель (три оси обратимость × радиус × цена,
три класса AUTONOMOUS/COLLABORATIVE/ASSISTED, правило «худшая ось эскалирует», fail-closed), но
единый классификатор был помечен `status: planned, where: null`: оси входили в решение РАЗДЕЛЬНЫМИ
сигналами (risk, irreversible, protected-paths), а ИМЯ класса из всех трёх вместе не вычислялось.
Здесь оно вычисляется — explainable: `evaluate(action)` печатает класс, причину, требуемое
одобрение и исполнителя. Это НЕ новое исполнение автономии: класс маппится на уже исполняемые
уровни `policy_engine` (execute/require_approval/suggest), а маппинг берётся ИЗ реестра, а не
дублируется здесь — единый источник истины остаётся один.

ИНВАРИАНТЫ модели (registry/decision-boundary.yaml -> assignment_rule):
  * worst_axis_escalates — действие относится к классу по ХУДШЕЙ из трёх осей;
  * never_downgraded_silently — запрос более автономного класса, чем требуют сигналы, НЕ понижает
    строгость молча (остаётся расчётный + причина); стрОже — можно;
  * fail_closed — сомнение = более строгий класс; цену подтвердить нечем → COLLABORATIVE, не AUTONOMOUS.

Использование:  python3 -m ai_ops_kit.governance.decision_boundary classify --signals '{...}' [--json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
           Path(__file__).resolve().parents[2])
MODEL_REL = "registry/decision-boundary.yaml"

AUTONOMOUS, COLLABORATIVE, ASSISTED = "AUTONOMOUS", "COLLABORATIVE", "ASSISTED"
# Ранг автономии: выше = больше автономии кита. Нужен для правила «нельзя понизить строгость молча».
_RANK = {ASSISTED: 0, COLLABORATIVE: 1, AUTONOMOUS: 2}

# Fail-closed маппинг на уровни policy_engine, ЕСЛИ реестр недоступен. В норме берётся из реестра
# (classes[].maps_to_autonomy_level) — единый источник истины; это лишь запасной, чтобы классификатор
# не падал, а деградировал в более строгую сторону.
_FALLBACK_LEVEL = {AUTONOMOUS: "execute", COLLABORATIVE: "require_approval", ASSISTED: "suggest"}


def _axes(signals: dict) -> dict:
    """Полюс каждой из трёх осей из сигналов действия (словарь spec_levels + decision-boundary)."""
    risk = str(signals.get("risk") or "").lower()
    irreversible = bool(signals.get("irreversible")) or bool(signals.get("destructive"))
    blast = str(signals.get("blast_radius") or "").lower()
    wide = blast == "wide" or bool(signals.get("wide")) or bool(signals.get("external_consumers"))
    protected = bool(signals.get("protected_paths"))
    secret = bool(signals.get("secret_boundary"))
    if risk in ("high", "critical") or secret:
        cost = "expensive"
    elif risk == "medium":
        cost = "medium"
    elif risk == "low":
        cost = "cheap"
    else:
        cost = "unknown"
    return {
        "reversibility": "irreversible" if irreversible else "reversible",
        "blast_radius": "wide" if wide else "local",
        "error_cost": cost,
        "_protected": protected,
    }


def classify_axes(signals: dict):
    """Чистая логика отнесения: -> (class_id, [причины]). Реестр не нужен — только правило осей."""
    ax = _axes(signals)
    rev, blast, cost = ax["reversibility"], ax["blast_radius"], ax["error_cost"]
    heavy = []
    if rev == "irreversible":
        heavy.append("обратимость: необратимо (one-way door)")
    if blast == "wide":
        heavy.append("радиус: широкий (защищённые пути/внешние потребители/много контуров)")
    if cost == "expensive":
        heavy.append("цена ошибки: высокая (риск high/critical или секрет)")
    if heavy:
        return ASSISTED, ["ХОТЯ БЫ ОДНА ось в тяжёлом полюсе → решение за человеком:"] + heavy
    mixed = []
    if ax["_protected"]:
        mixed.append("затронуты защищённые пути")
    if cost == "medium":
        mixed.append("средняя цена ошибки (risk=medium)")
    if mixed:
        return COLLABORATIVE, ["смешанный профиль, ни одна ось не тяжёлая → кит готовит, человек одобряет:"] + mixed
    if rev == "reversible" and blast == "local" and cost == "cheap":
        return AUTONOMOUS, ["все три оси в мягком полюсе (reversible + local + cheap) → кит делает сам, уведомляет"]
    # fail-closed: подтвердить «дёшево» нечем (нет сигнала risk) → не AUTONOMOUS, а COLLABORATIVE
    return COLLABORATIVE, ["цену ошибки подтвердить нечем (нет сигнала risk) → fail-closed: не автономно"]


def _load_classes(model=None) -> dict:
    """classes[id] -> {maps_to_autonomy_level, owns_decision, human_role} из реестра. Пусто при порче."""
    if model is None:
        try:
            model = yaml.safe_load((PKG / MODEL_REL).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return {}
    out = {}
    for c in (model.get("classes") or []):
        if isinstance(c, dict) and c.get("id"):
            out[c["id"]] = c
    return out


def evaluate(signals: dict, *, requested_class: str = None, model=None) -> dict:
    """Классифицировать действие по трём осям разом. -> объяснимый вердикт (dict).

    signals — атрибуты действия (risk, irreversible/destructive, blast_radius/wide/protected_paths,
    secret_boundary). requested_class — желаемый класс: более автономный, чем расчётный, НЕ понижает
    строгость (остаётся расчётный + причина); более строгий — принимается.
    """
    signals = dict(signals or {})
    cls, reason = classify_axes(signals)
    escalated_from = None
    if requested_class in _RANK:
        if _RANK[requested_class] > _RANK[cls]:
            escalated_from = requested_class
            reason.append(f"запрошен более автономный {requested_class}, но сигналы требуют {cls} — "
                          f"понижение строгости отклонено (нельзя понизить молча)")
        elif _RANK[requested_class] < _RANK[cls]:
            reason.append(f"запрошен более строгий {requested_class} — принят (строже можно)")
            cls = requested_class
    classes = _load_classes(model)
    spec = classes.get(cls) or {}
    level = spec.get("maps_to_autonomy_level") or _FALLBACK_LEVEL[cls]
    owns = spec.get("owns_decision") or ("kit" if cls == AUTONOMOUS else
                                         "human" if cls == ASSISTED else "shared")
    human_role = spec.get("human_role") or ("notified" if cls == AUTONOMOUS else
                                            "decides" if cls == ASSISTED else "approves")
    requires_approval = level == "require_approval"
    executor = ("kit" if level == "execute" else
                "kit_after_human_approval" if level == "require_approval" else "human")
    ax = _axes(signals)
    return {
        "decision_class": cls,
        "reason": reason,
        "axes": {"reversibility": ax["reversibility"], "blast_radius": ax["blast_radius"],
                 "error_cost": ax["error_cost"]},
        "autonomy_level": level,
        "requires_approval": requires_approval,
        "owns_decision": owns,
        "human_role": human_role,
        "executor": executor,
        "requested_class": requested_class,
        "escalated_from": escalated_from,
    }


def describe(verdict: dict) -> list:
    """Человекочитаемые строки вердикта (переиспользуемо командой `governance` и CLI). -> [str]."""
    ax = verdict["axes"]
    lines = [f"  граница решений для этого действия: {verdict['decision_class']} "
             f"(автономия {verdict['autonomy_level']}, решает {verdict['owns_decision']})",
             f"    оси: обратимость={ax['reversibility']}, радиус={ax['blast_radius']}, "
             f"цена={ax['error_cost']}"]
    if verdict["reason"]:
        lines.append(f"    · {verdict['reason'][0]}")
    return lines


def main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="decision_boundary.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("classify", help="классифицировать действие по трём осям")
    c.add_argument("--signals", default="{}")
    c.add_argument("--requested-class", default=None)
    c.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "classify":
        v = evaluate(json.loads(a.signals), requested_class=a.requested_class)
        if a.json:
            print(json.dumps(v, ensure_ascii=False, indent=2))
        else:
            print(f"DECISION-BOUNDARY: {v['decision_class']} "
                  f"(автономия: {v['autonomy_level']}, решает: {v['owns_decision']})")
            print(f"  оси: обратимость={v['axes']['reversibility']}, "
                  f"радиус={v['axes']['blast_radius']}, цена={v['axes']['error_cost']}")
            for r in v["reason"]:
                print(f"  · {r}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
