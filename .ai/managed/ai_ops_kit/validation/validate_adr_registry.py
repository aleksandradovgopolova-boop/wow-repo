#!/usr/bin/env python3
"""Проверка реестра ArchitectureDecision (decisions/adr/*.yaml) — v3.2 fitness.

ADR — это governed-набор, а не разрозненные файлы. Поверх поштучной структурной проверки
(validate_architecture_decision.check) реестр держит КРОСС-целостность и fitness:

  1. каждый ADR структурно валиден; имя файла == id (ADR-NNN.yaml);
  2. id уникальны; related-ссылки резолвятся в существующие ADR;
  3. supersede-цепочка ДВУНАПРАВЛЕННО согласована: A.supersedes=B => B.superseded_by=A (и наоборот);
     нет само-supersede; status=superseded требует superseded_by (уже в поштучной проверке);
  4. fitness: ui_impact ∈ gate_policy.UI_IMPACT (архитектурные UI-решения наследуют тир политики).

Использование:  validate_adr_registry.py [decisions/adr] [--json]
                validate_adr_registry.py --selftest
Возврат 0 — реестр целостен, 1 — есть нарушение.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path  # noqa: F401 — тело не зовёт, но селфтест импортирует ЧЕРЕЗ этот модуль

import yaml  # noqa: F401 — то же: селфтест берёт yaml из пространства имён валидатора

try:                                          # v3.38 (лента №5): валидатор двурежимен
    from ai_ops_kit.validation import _bootstrap   # noqa: F401 — импорт пакетом (после pip install)
except ImportError:                                # запуск скриптом: корня на пути ещё нет,
    import _bootstrap                              # noqa: F401 — и положить его может только он сам
# Проверка реестра ADR (read-only чтение каталога) вынесена ВНИЗ в пакет `checks`: рантайм
# (intelligence.evolution_triggers) зовёт check_registry()/DEFAULT_DIR отсюда вниз, без ребра
# intelligence -> validation. Раньше проверка ui_impact бралась из gate_policy.UI_IMPACT (gates,
# capabilities) — из primitives его тянуть нельзя, вокабуляр теперь из architecture_decision.
# Имена ре-экспортируются для обратной совместимости.
from ai_ops_kit.checks.adr_registry import DEFAULT_DIR, check_registry  # noqa: E402,F401


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    adr_dir = Path(args[0]) if args else DEFAULT_DIR
    errors, adrs = check_registry(adr_dir)
    if "--json" in argv:
        print(json.dumps({"count": len(adrs), "errors": errors}, ensure_ascii=False, indent=2))
    elif errors:
        print(f"ADR-REGISTRY: {len(errors)} нарушений:")
        for x in errors:
            print(f"  - {x}")
    else:
        print(f"ADR-REGISTRY-OK: {len(adrs)} ADR, реестр целостен.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
