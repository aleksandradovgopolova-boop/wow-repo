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

try:                                          # v3.38 (лента №5): валидатор двурежимен
    from ai_ops_kit.validation import _bootstrap   # noqa: F401 — импорт пакетом (после pip install)
except ImportError:                                # запуск скриптом: корня на пути ещё нет,
    import _bootstrap                              # noqa: F401 — и положить его может только он сам
# Проверяющая логика вынесена ВНИЗ (пакет `checks`, слой primitives): и рантайм (engine.pipeline_helpers),
# и эта CLI-обёртка импортируют её вниз — без восходящего ребра engine -> validation. check,
# provided_evidence и REQUIRED_EVIDENCE ре-экспортируются для обратной совместимости.
from ai_ops_kit.checks.requirements_artifact import (   # noqa: E402,F401
    REQUIRED_EVIDENCE, check, provided_evidence)


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
