#!/usr/bin/env python3
"""Валидатор Feature Blueprint (schemas/feature-blueprint.schema.json, Ф1 roadmap).

Blueprint — паспорт функции: features/<id>/blueprint.yaml со ссылками на артефакты
жизненного цикла. Ловит то, что реально ломается:
  1. невалидный YAML / не тот kind / нет обязательных полей;
  2. current_stage или ключ artifacts вне словаря стадий;
  3. стадия не позже current_stage без единого артефакта;
  4. артефакт стадии не позже current_stage: файла нет, а status не declined;
  5. status=declined без declined_reason (отказ должен быть явным и обоснованным).

Стадии (по порядку): discovery, definition, ux, architecture, delivery, analytics,
documentation, release, monitoring, retrospective.

Использование:  python3 validation/validate_feature_blueprint.py <feature-dir> [...]
                python3 validation/validate_feature_blueprint.py --selftest
Возврат 0 — чисто, 1 — есть ошибки. Требует pyyaml.
"""
from __future__ import annotations

import sys
import tempfile  # noqa: F401 — тело валидатора не зовёт, но селфтест импортирует ЧЕРЕЗ этот модуль
from pathlib import Path

import yaml  # noqa: F401 — то же: селфтест берёт yaml из пространства имён валидатора

try:                                          # v3.38 (лента №5): валидатор двурежимен
    from ai_ops_kit.validation import _bootstrap   # noqa: F401 — импорт пакетом (после pip install)
except ImportError:                                # запуск скриптом: корня на пути ещё нет,
    import _bootstrap                              # noqa: F401 — и положить его может только он сам
# Проверяющая логика (чтение каталога функции — read-only I/O) вынесена ВНИЗ в пакет `checks` (слой
# primitives): и рантайм (lifecycle.run_report), и эта CLI-обёртка импортируют её вниз — без
# восходящего ребра lifecycle -> validation. Имена ре-экспортируются для обратной совместимости
# (в т.ч. make_demo/validate_dir, которые зовут тесты селфтеста).
from ai_ops_kit.checks.feature_blueprint import (   # noqa: E402,F401
    DEBT_REL, FEATURE_STATUSES, PROFILES, STAGES, STATUSES,
    _debt_ids, make_demo, validate_dir, validate_dir_full)

def main(argv):
    if not argv:
        print("использование: validate_feature_blueprint.py <feature-dir> [...] | --selftest")
        return 1
    all_errors, all_debt = [], []
    for d in argv:
        _e, _a = validate_dir_full(Path(d).resolve())
        all_errors += _e
        all_debt += _a
    if all_debt:
        # Долг печатается ВСЕГДА и до вердикта: невидимый долг перестаёт быть долгом.
        print(f"ДОЛГ ДОКАЗАТЕЛЬСТВА ПОСТАВКИ ({len(all_debt)}) — не блокирует, но остаётся:")
        for a in all_debt:
            print(f"  · {a}")
    if all_errors:
        print(f"НАЙДЕНЫ ПРОБЛЕМЫ В FEATURE BLUEPRINT ({len(all_errors)}):")
        for e in all_errors:
            print(f"  - {e}")
        return 1
    print(f"OK: feature blueprint валиден ({len(argv)} функций проверено).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
