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
import json
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / "validation"))
sys.path.insert(0, str(PKG / "tools"))
import validate_architecture_decision as vad  # noqa: E402
import gate_policy  # noqa: E402

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


def selftest():
    import tempfile
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    # реальный реестр кита должен быть целостен
    real_errs, real_adrs = check_registry(DEFAULT_DIR)
    expect(f"реальный decisions/adr целостен ({len(real_adrs)} ADR)", real_errs == [])

    def _valid(aid, **over):
        d = {"schema_version": 1, "kind": "ArchitectureDecision", "id": aid,
             "title": "t", "status": "accepted", "context": "c", "decision": "d",
             "consequences": {"positive": ["p"], "negative": ["n"]}}
        d.update(over)
        return d

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "ADR-001.yaml").write_text(yaml.safe_dump(_valid("ADR-001")), encoding="utf-8")
        e, a = check_registry(d)
        expect("минимальный валидный реестр целостен", e == [] and set(a) == {"ADR-001"})

        # имя файла != id
        (d / "ADR-009.yaml").write_text(yaml.safe_dump(_valid("ADR-777")), encoding="utf-8")
        e, _ = check_registry(d)
        expect("имя файла != id -> ошибка", any("имя файла" in x for x in e))
        (d / "ADR-009.yaml").unlink()

        # односторонний supersede -> несогласовано
        (d / "ADR-002.yaml").write_text(yaml.safe_dump(
            _valid("ADR-002", supersedes="ADR-001")), encoding="utf-8")
        e, _ = check_registry(d)
        expect("A.supersedes=B без B.superseded_by=A -> несогласовано",
               any("несогласовано" in x for x in e))
        # двусторонний -> ок
        (d / "ADR-001.yaml").write_text(yaml.safe_dump(
            _valid("ADR-001", status="superseded", superseded_by="ADR-002")), encoding="utf-8")
        e, _ = check_registry(d)
        expect("двусторонняя supersede-цепочка -> целостна", e == [])

        # dangling related
        (d / "ADR-003.yaml").write_text(yaml.safe_dump(
            _valid("ADR-003", related=["ADR-404"])), encoding="utf-8")
        e, _ = check_registry(d)
        expect("dangling related -> ошибка", any("related" in x for x in e))
        (d / "ADR-003.yaml").unlink()

        # битый ui_impact (fitness к gate_policy)
        (d / "ADR-005.yaml").write_text(yaml.safe_dump(
            _valid("ADR-005", ui_impact="mega")), encoding="utf-8")
        e, _ = check_registry(d)
        expect("ui_impact вне gate_policy.UI_IMPACT -> ошибка (fitness)",
               any("UI_IMPACT" in x for x in e))

    print("validate_adr_registry selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
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
