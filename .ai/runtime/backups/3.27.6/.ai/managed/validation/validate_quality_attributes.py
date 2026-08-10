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
import json
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / "validation"))
import validate_adr_registry as reg  # noqa: E402

ACTIVE = {"accepted"}


def profile(adrs: dict) -> dict:
    """Профиль: attribute -> {effect -> [adr_id...]} по всем ADR."""
    prof = {}
    for aid, d in adrs.items():
        for qa in d.get("quality_attributes", []) or []:
            attr, eff = qa.get("attribute"), qa.get("effect")
            if not attr or not eff:
                continue
            prof.setdefault(attr, {}).setdefault(eff, []).append(aid)
    return prof


def fitness(adrs: dict):
    errors = []
    # (1) degrades без обоснования
    for aid, d in adrs.items():
        for qa in d.get("quality_attributes", []) or []:
            if qa.get("effect") == "degrades" and not (qa.get("note") or "").strip():
                errors.append(f"{aid}: degrades '{qa.get('attribute')}' без note (скрытая деградация)")
    # (2) неуправляемое противоречие среди активных ADR
    active = {aid: d for aid, d in adrs.items() if d.get("status") in ACTIVE}
    prof = profile(active)
    for attr, effs in prof.items():
        if effs.get("improves") and effs.get("degrades") and not effs.get("tradeoff"):
            errors.append(
                f"неуправляемое противоречие по '{attr}': improves {effs['improves']} vs "
                f"degrades {effs['degrades']} без tradeoff-обоснования (осознайте цену или разрешите)")
    return errors


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    # реальный реестр кита проходит fitness
    real_errs, real_adrs = reg.check_registry(reg.DEFAULT_DIR)
    expect("реальный ADR-реестр целостен (предусловие)", real_errs == [])
    expect(f"реальный реестр проходит quality-attributes fitness ({len(real_adrs)} ADR)",
           fitness(real_adrs) == [])
    expect("профиль непуст и покрывает реальные атрибуты",
           bool(profile(real_adrs)) and "maintainability" in profile(real_adrs))

    def _adr(aid, qas, status="accepted"):
        return {"id": aid, "status": status, "quality_attributes": qas}

    # degrades без note -> смелл
    e = fitness({"ADR-001": _adr("ADR-001", [{"attribute": "performance", "effect": "degrades"}])})
    expect("degrades без note -> смелл", any("без note" in x for x in e))
    # degrades с note -> ок
    e = fitness({"ADR-001": _adr("ADR-001",
                 [{"attribute": "performance", "effect": "degrades", "note": "кэш прогревается"}])})
    expect("degrades с note -> ок", e == [])
    # неуправляемое противоречие improves vs degrades без tradeoff
    e = fitness({
        "ADR-001": _adr("ADR-001", [{"attribute": "security", "effect": "improves"}]),
        "ADR-002": _adr("ADR-002", [{"attribute": "security", "effect": "degrades", "note": "x"}]),
    })
    expect("improves+degrades без tradeoff -> противоречие",
           any("противоречие" in x for x in e))
    # с tradeoff -> напряжение осознано, не смелл
    e = fitness({
        "ADR-001": _adr("ADR-001", [{"attribute": "security", "effect": "improves"}]),
        "ADR-002": _adr("ADR-002", [{"attribute": "security", "effect": "degrades", "note": "x"}]),
        "ADR-003": _adr("ADR-003", [{"attribute": "security", "effect": "tradeoff"}]),
    })
    expect("improves+degrades+tradeoff -> осознанно (не смелл)",
           not any("противоречие" in x for x in e))
    # superseded ADR не участвует в противоречии активных
    e = fitness({
        "ADR-001": _adr("ADR-001", [{"attribute": "cost", "effect": "improves"}]),
        "ADR-002": _adr("ADR-002", [{"attribute": "cost", "effect": "degrades", "note": "x"}],
                        status="superseded"),
    })
    expect("superseded ADR не создаёт противоречие среди активных", e == [])

    print("validate_quality_attributes selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
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
