"""Чистая проверка формы requirements-artifact. Вынесена из
`validation/validate_requirements_artifact.py` вниз (лента №5), чтобы движок (pipeline_helpers)
звал её ВНИЗ, без восходящего ребра engine -> validation.

Форма: requirements — непустой список объектов {id (уникальный), statement (тестируемое требование),
acceptance: [непустые сценарии приёмки]}. Только stdlib.
"""
from __future__ import annotations

REQUIRED_EVIDENCE = ["testable_requirements", "acceptance_scenarios"]


def check(data):
    errors = []
    if not isinstance(data, dict) or data.get("kind") != "requirements-artifact":
        errors.append("kind должен быть 'requirements-artifact'")
        data = data if isinstance(data, dict) else {}
    if data.get("schema_version") is None:
        errors.append("нет schema_version")
    reqs = data.get("requirements")
    if not isinstance(reqs, list) or not reqs:
        errors.append("requirements должен быть непустым списком")
        reqs = []
    seen = set()
    for i, r in enumerate(reqs):
        if not isinstance(r, dict):
            errors.append(f"requirement[{i}] должен быть объектом"); continue
        rid = r.get("id", f"#{i}")
        if not r.get("id"):
            errors.append(f"requirement[{i}]: нет id")
        elif r["id"] in seen:
            errors.append(f"дублирующийся id требования: {r['id']}")
        seen.add(r.get("id"))
        st = r.get("statement")
        if not (isinstance(st, str) and st.strip()):
            errors.append(f"{rid}: пустой/отсутствующий statement (требование должно быть сформулировано)")
        acc = r.get("acceptance")
        if not (isinstance(acc, list) and acc and all(isinstance(a, str) and a.strip() for a in acc)):
            errors.append(f"{rid}: acceptance должен быть непустым списком непустых сценариев приёмки")
    return errors


def provided_evidence(data):
    """Ключи required_evidence гейта requirements, подтверждённые валидным артефактом.
    Пусто, если артефакт невалиден (нельзя подтверждать по битой форме)."""
    return list(REQUIRED_EVIDENCE) if not check(data) else []
