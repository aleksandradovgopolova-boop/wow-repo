#!/usr/bin/env python3
"""Validate MemoryGovernancePolicy (security-долг #2, OWASP ASI).

Инварианты: каждая запись памяти несёт provenance (origin + source_type); имеет expiry (ttl_days>0 /
review_date / permanent+justification); НЕ self-ingested без подтверждения человека (собственный вывод
агента не становится авторитетной памятью сам по себе); derived-запись обязана ссылаться на upstream.

  validate_memory_governance.py [examples/memory-governance-demo/MGP-001.yaml] | --selftest
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

try:                                          # v3.38: валидатор двурежимен (лента №4)
    from ai_ops_kit.validation import _bootstrap   # noqa: F401 — импорт пакетом (после pip install)
except ImportError:                                # запуск скриптом: корня на пути ещё нет,
    import _bootstrap                              # noqa: F401 — и положить его может только он сам
# Проверяющая логика вынесена ВНИЗ (пакет `checks`, слой primitives): и рантайм (security_enforcement),
# и эта CLI-обёртка импортируют её вниз — без восходящего ребра security -> validation. check и
# константы ре-экспортируются для обратной совместимости (тесты и старые вызовы
# `validate_memory_governance.check`).
from ai_ops_kit.checks.memory_governance import (   # noqa: E402
    EXPIRY_MODES, SOURCE_TYPES, check)              # noqa: F401

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
DEFAULT = PKG / "examples" / "memory-governance-demo" / "MGP-001.yaml"


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    path = Path(args[0]) if args else DEFAULT
    if not path.exists():
        print(f"нет файла: {path}"); return 1
    errs = check(yaml.safe_load(path.read_text(encoding="utf-8")))
    if errs:
        print(f"MEMORY-GOVERNANCE {path.name}: ошибки:")
        for x in errs:
            print(f"  - {x}")
        return 1
    print(f"MEMORY-GOVERNANCE-OK: {path.name} валиден.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
