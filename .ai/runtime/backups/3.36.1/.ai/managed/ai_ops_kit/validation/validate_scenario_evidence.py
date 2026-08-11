#!/usr/bin/env python3
"""Validate scenario-evidence (Вывод 1 «дефектов одной сессии»: СЦЕНАРИЙ, А НЕ СЛОЙ).

Находка: гейт implementation_verification = «сборка/линт/тесты на ревизии». Пять дефектов одной сессии
ВСЕ его проходят — тесты смотрели на функцию/слой, а не на пользовательский сценарий тем же путём, что
продукт. Правило 2 из SEAM_DEFECTS работало только когда его применяли ДО кода (как чек-лист), и не
работало как воспоминание после. Чтобы работать, оно должно быть ПОЛЕМ ГЕЙТА.

Здесь — ADVISORY-проверка (до обкатки не блокирует): для ENGINEERING/PRODUCT/CRITICAL evidence гейта
обязан нести `named_user_scenario` (назван сценарий, который правка обязана сохранить) и `scenario_test`
(ссылка на тест, проходящий сценарий, а не юнит одного слоя). Отсутствие -> advisory-находка.
ЧЕСТНАЯ ОГОВОРКА: статический evidence НЕ доказывает «тем же набором вызовов, что продукт» — это
graduation-бар (перенос из advisory в required_evidence + сквозной тест).

  validate_scenario_evidence.py --selftest
"""
from __future__ import annotations

import sys

APPLICABLE = {"ENGINEERING", "PRODUCT", "CRITICAL"}


def _looks_like_test_ref(s):
    s = s.lower()
    return ("test" in s) or ("::" in s) or ("#" in s) or ("spec" in s)


def check(ev, task_type):
    """ev — evidence-словарь гейта implementation_verification; task_type — из signals.
    -> список ADVISORY-находок (пусто = ок / неприменимо). НИКОГДА не блокирует сам по себе."""
    findings = []
    if task_type not in APPLICABLE:
        return findings  # для QUICK/VISUAL/ANALYTICS/AI_FEATURE сценарный evidence не требуется
    if not isinstance(ev, dict):
        return ["evidence отсутствует — нельзя предъявить названный сценарий и покрывающий тест"]
    scen = ev.get("named_user_scenario")
    test = ev.get("scenario_test")
    if not (isinstance(scen, str) and scen.strip()):
        findings.append("нет named_user_scenario: какой ПОЛЬЗОВАТЕЛЬСКИЙ сценарий правка обязана "
                        "сохранить? (Вывод 1: сценарий, а не слой)")
    if not (isinstance(test, str) and test.strip()):
        findings.append("нет scenario_test: тест, проходящий сценарий ТЕМ ЖЕ путём, что продукт "
                        "(не юнит одного слоя)")
    elif not _looks_like_test_ref(test):
        findings.append(f"scenario_test='{test[:60]}' не похож на ссылку на тест (path::name / file#test)")
    return findings


def main(argv):
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
