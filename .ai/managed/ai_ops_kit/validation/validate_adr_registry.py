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
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
try:                                          # v3.34: валидатор двурежимен
    from ai_ops_kit.validation import _bootstrap   # noqa: F401 — импорт пакетом (после pip install)
except ImportError:                                # запуск скриптом: корня на пути ещё нет,
    import _bootstrap                              # noqa: F401 — и положить его может только он сам
from ai_ops_kit.validation import validate_architecture_decision as vad  # noqa: E402
from ai_ops_kit.gates import gate_policy  # noqa: E402

DEFAULT_DIR = PKG / "decisions" / "adr"


def check_registry(adr_dir: Path):
    errors = []
    adrs = {}
    files = sorted(Path(adr_dir).glob("ADR-*.yaml")) if Path(adr_dir).is_dir() else []
    for f in files:
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
        except yaml.YAMLError as ex:
            errors.append(f"{f.name}: не парсится YAML ({ex})")
            continue
        for e in vad.check(data):
            errors.append(f"{f.name}: {e}")
        aid = (data or {}).get("id")
        if isinstance(aid, str):
            if f.stem != aid:
                errors.append(f"{f.name}: имя файла не совпадает с id ({aid})")
            if aid in adrs:
                errors.append(f"дубликат id {aid}")
            else:
                adrs[aid] = data
    ids = set(adrs)
    for aid, d in adrs.items():
        sup, by = d.get("supersedes"), d.get("superseded_by")
        if sup == aid or by == aid:
            errors.append(f"{aid}: само-supersede запрещён")
        if sup:
            if sup not in ids:
                errors.append(f"{aid}.supersedes -> несуществующий {sup}")
            elif adrs[sup].get("superseded_by") != aid:
                errors.append(f"{aid} supersedes {sup}, но {sup}.superseded_by != {aid} (несогласовано)")
        if by:
            if by not in ids:
                errors.append(f"{aid}.superseded_by -> несуществующий {by}")
            elif adrs[by].get("supersedes") != aid:
                errors.append(f"{aid}.superseded_by={by}, но {by}.supersedes != {aid} (несогласовано)")
        for r in d.get("related", []) or []:
            if r not in ids:
                errors.append(f"{aid}.related -> несуществующий {r}")
        ui = d.get("ui_impact")
        if ui is not None and ui not in gate_policy.UI_IMPACT:
            errors.append(f"{aid}.ui_impact '{ui}' не в gate_policy.UI_IMPACT")
    return errors, adrs


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
