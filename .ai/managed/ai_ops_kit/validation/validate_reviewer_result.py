#!/usr/bin/env python3
"""Проверка структурного результата ревьюера (v2.33, Execution Engine Фаза 1).

Reviewer возвращает не свободный markdown, а структуру (schemas/reviewer-result.schema.json):
status/checks/blockers. Она — источник истины для гейта (gate_executor.collect_evidence
читает stage-<id>.reviewer.json первым); человеческий текст генерится поверх. Валидатор
держит структуру честной:

  1. schema_version/kind/gate/status/checks на месте; status ∈ pass|warn|fail;
  2. gate резолвится в quality/gates.yaml;
  3. каждый check: id + status ∈ pass|warn|fail;
  4. status ∈ {fail, warn} ОБЯЗАН иметь blockers (иначе «заблокировано без причины»): warn на
     блокирующем гейте блокирует так же, как fail (writer≠judge, v2.85) — значит и обязан назвать
     конкретные сомнения. Честность симметрична: нельзя ни фабриковать pass, ни фабриковать блок;
  5. согласованность: если есть check со status=fail, общий status не может быть pass.

Использование:  validate_reviewer_result.py <result.json> [--json]
                validate_reviewer_result.py --selftest
Возврат 0 — валиден, 1 — ошибки.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

try:                                          # v3.38 (лента №5): валидатор двурежимен
    from ai_ops_kit.validation import _bootstrap   # noqa: F401 — импорт пакетом (после pip install)
except ImportError:                                # запуск скриптом: корня на пути ещё нет,
    import _bootstrap                              # noqa: F401 — и положить его может только он сам
# Проверяющая логика вынесена ВНИЗ (пакет `checks`, слой primitives): и рантайм (engine.pipeline_evidence,
# providers.orchestrator), и эта CLI-обёртка импортируют её вниз — без восходящих рёбер к validation.
# check и ST ре-экспортируются для обратной совместимости (тесты, старые вызовы .check/.ST).
from ai_ops_kit.checks.reviewer_result import ST, check   # noqa: E402,F401

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])


def _gate_ids():
    """Множество id гейтов из реестра, либо None — «реестр не прочитан». (2026-08-12)

    Прежде на любом отказе возвращалось ПУСТОЕ множество, и это давало верный вердикт с НЕВЕРНОЙ
    причиной: `gid not in set()` истинно для любого гейта, поэтому нечитаемый `quality/gates.yaml`
    печатал «gate 'code_review' отсутствует в quality/gates.yaml» — про каждый гейт по очереди.
    Читающий шёл искать гейт в реестре, где тот на месте, а сломан был сам реестр.

    `None` отличает «неизвестно» от «пусто». Fail-closed при этом СОХРАНЁН и не ослаблен: main
    добавляет отдельную ошибку о непрочитанном реестре, поэтому валидатор по-прежнему возвращает
    ненулевой код — меняется только то, что он о себе сообщает.
    """
    try:
        return set(yaml.safe_load((PKG / "quality" / "gates.yaml").read_text(encoding="utf-8"))["gates"])
    except (OSError, yaml.YAMLError, KeyError, TypeError):
        return None


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__); return 1
    data = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    gate_ids = _gate_ids()
    errors = []
    if gate_ids is None:
        # Fail-closed сохранён: ошибка есть, но называет ПРИЧИНУ — сломан реестр, а не гейт.
        errors.append("quality/gates.yaml не прочитан — принадлежность гейта реестру НЕ проверена; "
                      "это не «гейт отсутствует»")
    errors += check(data, gate_ids)
    if "--json" in argv:
        print(json.dumps({"errors": errors}, ensure_ascii=False, indent=2))
    elif errors:
        print("REVIEWER-RESULT: ошибки:")
        for e in errors:
            print(f"  - {e}")
    else:
        print("REVIEWER-RESULT-OK: структура валидна.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
