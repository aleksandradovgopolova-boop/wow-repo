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

try:                                          # v3.38 (лента №5): валидатор двурежимен
    from ai_ops_kit.validation import _bootstrap   # noqa: F401 — импорт пакетом (после pip install)
except ImportError:                                # запуск скриптом: корня на пути ещё нет,
    import _bootstrap                              # noqa: F401 — и положить его может только он сам
# Структурная проверка одного ADR вынесена ВНИЗ (пакет `checks`, слой primitives): её зовёт
# `checks.adr_registry` в своём слое, и через реестр — рантайм, без ребра intelligence -> validation.
# check и вокабуляры ре-экспортируются для обратной совместимости.
from ai_ops_kit.checks.architecture_decision import (   # noqa: E402,F401
    QA_ATTR, QA_EFFECT, STATUS, UI_IMPACT, check)

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
SCHEMA = PKG / "schemas" / "architecture-decision.schema.json"


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
