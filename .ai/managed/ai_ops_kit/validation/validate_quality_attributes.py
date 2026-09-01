#!/usr/bin/env python3
"""Quality-attributes fitness поверх ADR-реестра — v3.2 Architecture Governance.

Каждый ADR декларирует влияние на quality attributes (improves/degrades/tradeoff/neutral).
Разрозненно это just-metadata; на уровне системы нужен fitness: агрегировать профиль и ловить
governance-смеллы, пока решения не расползлись в скрытые противоречия:

  1. degrades ОБЯЗАН нести note (обоснование): «стало хуже» без причины — скрытая деградация;
  2. неуправляемое противоречие: среди АКТИВНЫХ (status=accepted) ADR один атрибут одновременно
     improves и degrades, и НИ один не помечает его tradeoff -> напряжение не осознано (нужно либо
     tradeoff-обоснование, либо разрешение). tradeoff явно признаёт цену -> это НЕ смелл.

Профиль (machine-readable) полезен для evolution-triggers (v3.2.x): видно, какие атрибуты система
осознанно улучшает, а какие приносит в жертву.

Использование:  validate_quality_attributes.py [decisions/adr] [--json]
                validate_quality_attributes.py --selftest
Возврат 0 — fitness пройден, 1 — есть смелл.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

try:                                          # v3.34: валидатор двурежимен
    from ai_ops_kit.validation import _bootstrap   # noqa: F401 — импорт пакетом (после pip install)
except ImportError:                                # запуск скриптом: корня на пути ещё нет,
    import _bootstrap                              # noqa: F401 — и положить его может только он сам
from ai_ops_kit.validation import validate_adr_registry as reg  # noqa: E402
# Чистая логика fitness вынесена ВНИЗ (пакет `checks`, слой primitives): рантайм
# (intelligence.evolution_triggers) зовёт profile() отсюда вниз, без ребра intelligence -> validation.
# profile/fitness/ACTIVE ре-экспортируются для обратной совместимости (лента №5).
from ai_ops_kit.checks.quality_attributes import ACTIVE, fitness, profile  # noqa: E402,F401


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    adr_dir = Path(args[0]) if args else reg.DEFAULT_DIR
    reg_errs, adrs = reg.check_registry(adr_dir)
    if reg_errs:
        print("QUALITY-ATTRIBUTES: сначала почините реестр ADR:")
        for x in reg_errs:
            print(f"  - {x}")
        return 1
    errs = fitness(adrs)
    if "--json" in argv:
        print(json.dumps({"profile": profile(adrs), "fitness_errors": errs},
                         ensure_ascii=False, indent=2))
    elif errs:
        print(f"QUALITY-ATTRIBUTES: {len(errs)} смеллов:")
        for x in errs:
            print(f"  - {x}")
    else:
        print(f"QUALITY-ATTRIBUTES-OK: профиль по {len(profile(adrs))} атрибутам, противоречий нет.")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
