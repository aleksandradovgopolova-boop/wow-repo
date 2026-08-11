#!/usr/bin/env python3
"""Проверка ArchitectureDecision (ADR) — v3.2 Architecture, Product & UI Governance.

ADR (schemas/architecture-decision.schema.json) фиксирует КОНКРЕТНОЕ структурное решение о системе
(в отличие от decisions/registry.yaml — принципы/эпизоды мышления). Валидатор держит ADR честным:

  1. schema_version=1, kind=ArchitectureDecision; id формата ADR-NNN; title/context/decision непусты;
  2. status ∈ proposed|accepted|superseded|deprecated;
  3. consequences несёт И positive, И negative (решение без негативных последствий подозрительно —
     честность симметрична: нельзя прятать издержки);
  4. status=superseded ОБЯЗАН иметь superseded_by (ADR-преемник); id/supersedes/superseded_by формата;
  5. quality_attributes: attribute + effect из допустимых enum'ов;
  6. ui_impact (если задан) ∈ none|internal|user_facing|critical (согласовано с gate_policy).

Использование:  validate_architecture_decision.py <adr.(yaml|json)> [--json]
                validate_architecture_decision.py --selftest
Возврат 0 — валиден, 1 — ошибки.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
SCHEMA = PKG / "schemas" / "architecture-decision.schema.json"

STATUS = {"proposed", "accepted", "superseded", "deprecated"}
QA_ATTR = {"performance", "security", "reliability", "maintainability", "usability",
           "accessibility", "portability", "compatibility", "scalability", "observability",
           "cost", "testability"}
QA_EFFECT = {"improves", "degrades", "tradeoff", "neutral"}
UI_IMPACT = {"none", "internal", "user_facing", "critical"}

import re
_ID = re.compile(r"^ADR-[0-9]{3,}$")


def check(data: dict):
    e = []
    if not isinstance(data, dict):
        return ["ADR не объект"]
    if data.get("schema_version") != 1:
        e.append("schema_version должен быть 1")
    if data.get("kind") != "ArchitectureDecision":
        e.append("kind должен быть 'ArchitectureDecision'")
    if not (isinstance(data.get("id"), str) and _ID.match(data["id"])):
        e.append("id должен быть формата ADR-NNN")
    for f in ("title", "context", "decision"):
        if not (isinstance(data.get(f), str) and data[f].strip()):
            e.append(f"{f} обязателен и непуст")
    if data.get("status") not in STATUS:
        e.append(f"status ∉ {sorted(STATUS)}")

    cons = data.get("consequences")
    if not isinstance(cons, dict):
        e.append("consequences обязателен (объект positive+negative)")
    else:
        for poln in ("positive", "negative"):
            v = cons.get(poln)
            if not (isinstance(v, list) and v and all(isinstance(x, str) for x in v)):
                e.append(f"consequences.{poln} — непустой список строк (издержки скрывать нельзя)")

    for i, alt in enumerate(data.get("alternatives", []) or []):
        if not isinstance(alt, dict) or not alt.get("option") or not alt.get("rejected_because"):
            e.append(f"alternatives[{i}]: нужны option + rejected_because")

    for i, qa in enumerate(data.get("quality_attributes", []) or []):
        if not isinstance(qa, dict):
            e.append(f"quality_attributes[{i}] не объект")
            continue
        if qa.get("attribute") not in QA_ATTR:
            e.append(f"quality_attributes[{i}].attribute ∉ допустимых")
        if qa.get("effect") not in QA_EFFECT:
            e.append(f"quality_attributes[{i}].effect ∉ {sorted(QA_EFFECT)}")

    ui = data.get("ui_impact")
    if ui is not None and ui not in UI_IMPACT:
        e.append(f"ui_impact ∉ {sorted(UI_IMPACT)} (или null)")

    for f in ("supersedes", "superseded_by"):
        v = data.get(f)
        if v is not None and not (isinstance(v, str) and _ID.match(v)):
            e.append(f"{f} должен быть ADR-NNN или null")
    if data.get("status") == "superseded" and not data.get("superseded_by"):
        e.append("status=superseded требует superseded_by (ADR-преемник)")
    return e


def _load(path: Path):
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        return yaml.safe_load(text)
    return json.loads(text)


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    errors = check(_load(Path(args[0])))
    if "--json" in argv:
        print(json.dumps({"errors": errors}, ensure_ascii=False, indent=2))
    elif errors:
        print("ADR: ошибки:")
        for x in errors:
            print(f"  - {x}")
    else:
        print("ADR-OK: структура валидна.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
