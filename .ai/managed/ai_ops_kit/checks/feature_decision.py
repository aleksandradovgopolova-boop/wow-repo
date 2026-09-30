"""Проверяющая логика feature-решений — форма feature_target и гейт каталога решений.

Вынесена из слоя `intelligence` ВНИЗ в пакет `checks` (слой primitives, #541), чтобы контур
гейтов звал её ВНИЗ: `gates.gate_executor` (ядро) не вправе импортировать слой `intelligence`
(слой выше + kernel-boundary), а `checks` зависит только от stdlib и pyyaml и не тянет ничего из
ai_ops_kit выше foundation. Тот же приём, которым в v3.38 развязали `рантайм -> validation`:
чистую/read-only проверяющую логику держим в `checks`, а вызыватели — как из слоя `intelligence`,
так и `gates.gate_executor` — импортируют её сверху вниз.

Продуктовое решение о фиче обязано нести три измеримых обязательства — baseline (где мы сейчас),
target (куда идём) и guardrails (что не должно сломаться). Форму ПРОВЕРЯЕТ механизм, а не декларация
человека: «фича с целью» без измеримого обязательства — пустая декларация.
"""
from __future__ import annotations

from pathlib import Path

import yaml

# Допустимые направления движения метрики к target.
DIRECTIONS = {"increase", "decrease", "hold"}


def check_feature_target(ft) -> list[str]:
    """Проверить ФОРМУ контракта feature_target; вернуть список ошибок.

    Пустой список = валиден. Продуктовое решение о фиче обязано нести три
    измеримых обязательства, иначе «фича с целью» — пустая декларация:

      - baseline: где мы сейчас — непустые metric (что двигаем) и value;
      - target:   куда идём — непустой value и direction ∈ {increase,decrease,hold};
      - guardrails: что не должно сломаться — хотя бы один пункт с metric и bound.

    Функция не судит о разумности чисел (это дело человека) — она отказывает
    лишь тому, что не является измеримым обязательством по форме.
    """
    if not isinstance(ft, dict):
        return ["feature_target: ожидается объект с baseline/target/guardrails"]

    errors: list[str] = []

    baseline = ft.get("baseline")
    if not isinstance(baseline, dict):
        errors.append("feature_target.baseline: отсутствует (нужны metric и value)")
    else:
        if not baseline.get("metric"):
            errors.append("feature_target.baseline.metric: пусто (назови измеряемую метрику)")
        if baseline.get("value") in (None, ""):
            errors.append("feature_target.baseline.value: пусто (где мы сейчас)")

    target = ft.get("target")
    if not isinstance(target, dict):
        errors.append("feature_target.target: отсутствует (нужны value и direction)")
    else:
        if target.get("value") in (None, ""):
            errors.append("feature_target.target.value: пусто (куда хотим прийти)")
        direction = target.get("direction")
        if direction not in DIRECTIONS:
            errors.append(
                f"feature_target.target.direction: '{direction}' не в {sorted(DIRECTIONS)}")

    guardrails = ft.get("guardrails")
    if not isinstance(guardrails, list) or not guardrails:
        errors.append("feature_target.guardrails: нужен хотя бы один пункт (metric + bound)")
    else:
        for i, g in enumerate(guardrails):
            if not isinstance(g, dict) or not g.get("metric") or g.get("bound") in (None, ""):
                errors.append(f"feature_target.guardrails[{i}]: нужны metric и bound")

    return errors


def gate_feature_decisions(decisions_dir) -> list[str]:
    """Гейт: каждое решение, объявленное фичей, несёт ВАЛИДНЫЙ feature_target.

    Обходит каталог решений (.ai/project/decisions/*.yaml — пофайловые решения из
    propose). Решение с kind: feature-decision ОБЯЗАНО нести feature_target,
    проходящий check_feature_target; иначе гейт называет, чего не хватает. Решения
    иных типов (product-decision и т.д.) не трогаются. Каталог читается только на
    чтение и НЕ создаётся — его отсутствие не ошибка (фич-решений просто нет).

    Возвращает список ошибок; пустой список = всё валидно. Механизм ПРОВЕРЯЕТ, а не
    верит на слово: фичу, объявленную целью без измеримого обязательства, он краснит.
    """
    errors: list[str] = []
    if not decisions_dir or not Path(decisions_dir).exists():
        return errors
    for f in sorted(Path(decisions_dir).glob("*.yaml")):
        try:
            d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError) as exc:
            errors.append(f"{f.name}: не читается ({exc})")
            continue
        if not isinstance(d, dict) or d.get("kind") != "feature-decision":
            continue
        ft = d.get("feature_target")
        if ft is None:
            errors.append(f"{f.name}: kind=feature-decision, но нет feature_target")
            continue
        for e in check_feature_target(ft):
            errors.append(f"{f.name}: {e}")
    return errors
