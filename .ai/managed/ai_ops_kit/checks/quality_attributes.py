"""Чистая логика quality-attributes fitness поверх ADR-реестра. Вынесена из
`validation/validate_quality_attributes.py` вниз (лента №5), чтобы рантайм
(intelligence.evolution_triggers) звал `profile()` ВНИЗ, без восходящего ребра
intelligence -> validation.

profile() строит машиночитаемый профиль (attribute -> effect -> [adr_id]); fitness() ловит
governance-смеллы (degrades без обоснования; неуправляемое improves/degrades без tradeoff). Обе
чистые — данные на входе, никакого ввода-вывода. Только stdlib.
"""
from __future__ import annotations

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
