#!/usr/bin/env python3
"""Validate requirements artifact (v2.86 Product Authoring).

Гейт `requirements` (ENGINEERING/PRODUCT) требует evidence: testable_requirements +
acceptance_scenarios. Раньше движок их не производил -> гейт честно блокировал. v2.86: writer
пишет артефакт требований в worktree, а ЭТОТ детерминированный валидатор подтверждает его ФОРМУ —
это и есть legitimate evidence (та же дисциплина, что validate_feature_blueprint: проверяем
структуру, не «качество»). ЧЕСТНО (v2.87): качество требований судит ЧЕЛОВЕК — в петле --review
детерминированные гейты (requirements/plan_readiness) НЕ ревьюятся (ревьюер закрывает только
ai-review гейты). Форма закрывает гейт, но не гарантирует осмысленность требований.

Форма (YAML):
  schema_version: 1
  kind: requirements-artifact
  workitem_id: <slug>
  requirements:
    - id: R1
      statement: "поле статуса фильтрует список заказов"   # тестируемое требование
      acceptance:                                          # >=1 сценарий приёмки
        - "when статус=paid then показаны только оплаченные"

check() -> список ошибок (пусто = валидно). provided_evidence() -> какие required_evidence-ключи
гейта закрыты (для подачи в gate_executor).

Использование:
  validate_requirements_artifact.py <artifact.yaml>
  validate_requirements_artifact.py --selftest
Возврат 0 — ок, 1 — ошибки.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

# required_evidence гейта requirements (quality/gates.yaml) — что артефакт обязан подкрепить.
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


def load(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def main(argv):
    if not argv:
        print(__doc__); return 1
    errs = check(load(argv[0]))
    if errs:
        print("REQUIREMENTS-ARTIFACT: ошибки:")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("REQUIREMENTS-ARTIFACT-OK: форма подтверждена (testable_requirements + acceptance_scenarios).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
