#!/usr/bin/env python3
"""Validate + render spec-change artifact (v2.89 Product Authoring: specification).

Гейт `specification` (ENGINEERING/PRODUCT) требует evidence openspec_valid + requirements_covered.
Раньше движок его не производил -> честно блокировал. v2.89: author-модель отдаёт СТРУКТУРНОЕ
описание изменения (capability + требования + сценарии), а движок РЕНДЕРИТ его в точный OpenSpec-
markdown и прогоняет НАСТОЯЩИМ `openspec validate --strict`. Формат markdown контролирует движок
(не модель) — поэтому валидная структура надёжно проходит strict-валидацию.

check() проверяет ФОРМУ структурного описания. render() пишет OpenSpec-change (proposal/tasks/
specs) в <openspec_root>/changes/<id>/. openspec_valid даёт ТОЛЬКО реальный CLI (в движке);
requirements_covered — структурно (есть требования).

Форма (YAML) от автора:
  schema_version: 1
  kind: spec-change
  capability: pricing            # slug [a-z0-9][a-z0-9-]*
  why: "зачем"
  what_changes: ["что меняется"]
  impact: "на что влияет"        # опционально
  tasks: ["шаг 1", "шаг 2"]
  requirements:
    - name: "Price formatting"
      text: "The system SHALL ..."         # нормативное требование
      scenarios:
        - {name: "Thousands", when: "...", then: "..."}

Использование:
  validate_spec_artifact.py <artifact.yaml>
  validate_spec_artifact.py --selftest
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
# Проверка ФОРМЫ и рендер СОДЕРЖИМОГО вынесены ВНИЗ (пакет `checks`, слой primitives): и рантайм
# (engine.pipeline_evidence), и эта CLI-обёртка зовут их вниз — без восходящего ребра engine ->
# validation. Запись файлов (I/O) остаётся здесь и в движке — ниже ядра ей не место.
from ai_ops_kit.checks.spec_artifact import (   # noqa: E402,F401
    REQUIRED_EVIDENCE, _CAP_RE, _slug_tasks, check, provided_evidence, render_content)


def render(data, openspec_root, change_id):
    """Записать spec-change в OpenSpec-структуру под <openspec_root>/changes/<change_id>/.
    Возвращает список записанных файлов. Предполагает, что data уже прошла check().

    Содержимое строит чистая `checks.spec_artifact.render_content`; здесь — только запись (I/O).
    Ту же чистую функцию зовёт движок (engine.pipeline_evidence) и пишет файлы сам, поэтому она
    ниже ядра, а запись — нет."""
    root = Path(openspec_root)
    written = []
    for rel, content in render_content(data, change_id):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(str(target))
    return written


def load(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def main(argv):
    if not argv:
        print(__doc__); return 1
    errs = check(load(argv[0]))
    if errs:
        print("SPEC-ARTIFACT: ошибки:")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("SPEC-ARTIFACT-OK: структура spec-change валидна (openspec_valid подтверждает CLI отдельно).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
