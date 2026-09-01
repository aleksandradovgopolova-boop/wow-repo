"""Чистая структурная проверка одного ArchitectureDecision (ADR). Вынесена из
`validation/validate_architecture_decision.py` вниз (лента №5): её зовёт `checks.adr_registry`
(проверка реестра) в СВОЁМ слое, и через реестр — рантайм (intelligence.evolution_triggers), без
восходящего ребра intelligence -> validation.

check(data) -> list[str]: id формата ADR-NNN; обязательные title/context/decision; status,
quality_attributes.effect/attribute, ui_impact из допустимых словарей; consequences.positive+negative
непусты (издержки скрывать нельзя); supersede-поля согласованы. UI_IMPACT — вокабуляр допустимых
значений ui_impact; та же величина, что gate_policy.UI_IMPACT, но здесь она в слое primitives, чтобы
проверка реестра не тянула `gates` (capabilities) вверх из checks. Только stdlib.
"""
from __future__ import annotations

import re

STATUS = {"proposed", "accepted", "superseded", "deprecated"}
QA_ATTR = {"performance", "security", "reliability", "maintainability", "usability",
           "accessibility", "portability", "compatibility", "scalability", "observability",
           "cost", "testability"}
QA_EFFECT = {"improves", "degrades", "tradeoff", "neutral"}
UI_IMPACT = {"none", "internal", "user_facing", "critical"}

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
