#!/usr/bin/env python3
"""Кросс-артефактная консистентность (v2.3; идея — Spec Kit `analyze`).

Первый и главный класс расхождений (подтверждён боевым прогоном ii-sreda):
события, на которые ссылается dashboard-spec (Source events, Funnels),
обязаны быть объявлены в tracking plan той же фичи. Каждый артефакт по
отдельности валиден — вместе противоречивы; это ловим механически.

Правила:
  1. dashboard-spec есть, tracking plan отсутствует -> PROBLEM;
  2. событие из dashboard-spec не объявлено в tracking plan -> PROBLEM;
  3. артефакты отсутствуют или таблица событий не парсится -> мягкая деградация:
     SKIP/WARN, не ложный fail (гипотеза №2 прогона ii-sreda);
  4. событие объявлено, но нигде не используется -> WARN (информационно).

События извлекаются из markdown-таблиц: tracking plan — первая колонка таблицы
раздела Events; dashboard-spec — snake_case-токены в колонке Source events и в
разделе Funnels. Таксономия object_action (snake_case) — из шаблонов кита.

Использование:  validate_cross_artifacts.py <feature-dir> [...] | --selftest
Возврат 0 — чисто/skip, 1 — есть PROBLEM. Требует pyyaml (для селфтеста — нет).
"""
from __future__ import annotations

import sys
import tempfile  # noqa: F401 — тело валидатора не зовёт, но селфтест импортирует ЧЕРЕЗ этот модуль
from pathlib import Path

try:                                          # v3.38 (лента №5): валидатор двурежимен
    from ai_ops_kit.validation import _bootstrap   # noqa: F401 — импорт пакетом (после pip install)
except ImportError:                                # запуск скриптом: корня на пути ещё нет,
    import _bootstrap                              # noqa: F401 — и положить его может только он сам
# Проверяющая логика (чтение tracking-plan/dashboard-spec — read-only I/O) вынесена ВНИЗ в пакет
# `checks` (слой primitives): и рантайм (lifecycle.run_report), и эта CLI-обёртка импортируют её
# вниз — без восходящего ребра lifecycle -> validation. Имена ре-экспортируются для совместимости.
from ai_ops_kit.checks.cross_artifacts import (   # noqa: E402,F401
    DASHBOARD, EVENT_RE, TRACKING, check_feature, declared_events, md_section, used_events)

TP_OK = """# Tracking Plan
## Events
| Event name | Trigger | Properties | Required | Owner |
|---|---|---|---|---|
| checkout_started | клик | cart_value | yes | pa |
| checkout_completed | заказ | order_id | yes | pa |
"""
DS_OK = """# Dashboard Specification
## Blocks
| Block | Metric(s) | Visualisation | Source events | Segment / filter |
|---|---|---|---|---|
| Conversion | CR | line | checkout_started, checkout_completed | all |
## Funnels
checkout_started -> checkout_completed
"""
DS_BAD = DS_OK.replace("checkout_completed", "checkout_finished")


def main(argv):
    if not argv:
        print("использование: validate_cross_artifacts.py <feature-dir> [...] | --selftest")
        return 1
    all_p = []
    for d in argv:
        p, w, s = check_feature(Path(d).resolve())
        for x in p:
            print(f"  [PROBLEM] {x}")
        for x in w:
            print(f"  [WARN] {x}")
        if s:
            print(f"  [SKIP] {s}")
        all_p += p
    if all_p:
        print(f"НАЙДЕНЫ КРОСС-АРТЕФАКТНЫЕ РАСХОЖДЕНИЯ ({len(all_p)}).")
        return 1
    print(f"OK: кросс-артефактная консистентность чиста ({len(argv)} функций).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
