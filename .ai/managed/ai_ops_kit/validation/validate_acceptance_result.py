#!/usr/bin/env python3
"""Проверка структурного результата сверки критериев приёмки (B2-14, 2026-08-14).

Ревьюер сверки возвращает не прозу «всё выполнено», а структуру: вердикт ПО КАЖДОМУ объявленному
критерию с ЦИТАТОЙ как основанием. Валидатор держит эту структуру честной — он единственное место,
где «вердикт вынесен» отличается от «вердикт выглядит вынесенным»:

  1. schema_version/kind на месте; kind == 'acceptance-result';
  2. criteria — непустой список; у каждого элемента id и status ∈ met|unmet|undetermined;
  3. вердикты покрывают РОВНО объявленные критерии: ни пропуска, ни дубля, ни выдуманного id.
     Пропущенный критерий — это НЕ «выполнен по умолчанию»: без него сверка неполна, и её нельзя
     называть сверкой (тот же инвариант, что `unavailable != 0`);
  4. `met` ОБЯЗАН иметь непустые quote и source: «выполнен» без основания неопровержим, а именно
     неопровержимое утверждение и дало ложный green B2-14;
  5. `unmet`/`undetermined` обязаны иметь reason или quote — честность симметрична: нельзя
     фабриковать ни «выполнено», ни «не выполнено» (та же симметрия, что в reviewer-result).

Почему валидатор отдельный, а не расширение `validate_reviewer_result`: там вердикт ОДИН на гейт
(status), здесь — по одному на критерий, и главное поле (`quote`) в reviewer-result отсутствует.
Смешать их значило бы ослабить оба контракта до пересечения.

Использование:  validate_acceptance_result.py <result.json> [--criteria AC-1,AC-2] [--json]
Возврат 0 — валиден, 1 — ошибки.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

try:                                          # v3.38 (лента №5): валидатор двурежимен
    from ai_ops_kit.validation import _bootstrap   # noqa: F401 — импорт пакетом (после pip install)
except ImportError:                                # запуск скриптом: корня на пути ещё нет,
    import _bootstrap                              # noqa: F401 — и положить его может только он сам
# Проверяющая логика вынесена ВНИЗ (пакет `checks`, слой primitives): и рантайм (engine.acceptance_verify),
# и эта CLI-обёртка импортируют её вниз — без восходящего ребра engine -> validation. check и
# константы ре-экспортируются для обратной совместимости.
from ai_ops_kit.checks.acceptance_result import (   # noqa: E402,F401
    CRITERION_STATUS, EVIDENCE_KINDS, check)


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    crit_ids = None
    for a in argv:
        if a.startswith("--criteria="):
            crit_ids = [x.strip() for x in a.split("=", 1)[1].split(",") if x.strip()]
    data = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    errors = check(data, crit_ids)
    if "--json" in argv:
        print(json.dumps({"errors": errors}, ensure_ascii=False, indent=2))
    elif errors:
        print("ACCEPTANCE-RESULT: ошибки:")
        for e in errors:
            print(f"  - {e}")
    else:
        print("ACCEPTANCE-RESULT-OK: структура валидна.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
