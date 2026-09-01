#!/usr/bin/env python3
"""Validate plan artifact (v2.86 Product Authoring).

Гейт `plan_readiness` (ENGINEERING/PRODUCT) требует evidence: work_packages + dependencies +
write_scope. v2.86: writer пишет план-артефакт в worktree, ЭТОТ детерминированный валидатор
подтверждает его форму -> legitimate evidence (структура, не «качество плана»).
ЧЕСТНО (v2.87): проверяется только ФОРМА. write_scope — декларация плана; движок НЕ сужает по
ней запись автоматически (это не enforcement, а поле артефакта). Качество плана судит человек —
в петле --review детерминированные гейты (requirements/plan_readiness) НЕ ревьюятся.

Форма (YAML):
  schema_version: 1
  kind: plan-artifact
  workitem_id: <slug>
  work_packages:
    - id: WP1
      summary: "добавить фильтр в контроллер каталога"
      depends_on: []            # зависимости (может быть пусто, но поле обязательно)
  write_scope: ["src/catalog/"] # непустой список путей, куда план разрешает писать

check() -> список ошибок. provided_evidence() -> закрытые required_evidence-ключи гейта.

Использование:
  validate_plan_artifact.py <artifact.yaml>
  validate_plan_artifact.py --selftest
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
from ai_ops_kit.checks.plan_artifact import (   # noqa: E402,F401
    REQUIRED_EVIDENCE, check, provided_evidence)


def load(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def main(argv):
    if not argv:
        print(__doc__); return 1
    errs = check(load(argv[0]))
    if errs:
        print("PLAN-ARTIFACT: ошибки:")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PLAN-ARTIFACT-OK: форма подтверждена (work_packages + dependencies + write_scope).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
