#!/usr/bin/env python3
"""Общий язык health-отчётов (PR-13/14/15): band + НАЗВАННАЯ причина.

Три измерения здоровья — product / tech / delivery — отвечают на разные вопросы, но говорят
одним языком: Green / Yellow / Red, и НИКОГДА без причины (цвет без причины бесполезен —
граница ленты 5). Здесь общий словарь этого языка, чтобы health_product / health_tech /
health_delivery не копировали агрегацию цвета (dp-001: дубли уже построенного отклоняем).

ГЛАВНЫЙ ИНВАРИАНТ (правило кита «третье состояние ≠ второе»): «не проверено» (UNKNOWN) —
это НЕ «в порядке» (GREEN). Сигнал, который не удалось прочитать, попадает в UNKNOWN с
причиной, а не молча в зелёный. Итоговый band отражает то, что мы ЗНАЕМ; непроверенное
выносится отдельным списком `unverified`, и отчёт честно ставит `complete: false`.

Правило свёртки: худший ИЗВЕСТНЫЙ цвет побеждает (red > yellow > green). UNKNOWN не красит и
не зеленит. Если известных сигналов нет вовсе — итог UNKNOWN (пустой сигнал даёт unknown, а
не green).
"""
from __future__ import annotations

from dataclasses import dataclass, field

GREEN = "green"
YELLOW = "yellow"
RED = "red"
UNKNOWN = "unknown"

# severity для свёртки: чем больше, тем «хуже». UNKNOWN=0 — он не участвует в выборе худшего.
_SEVERITY = {RED: 3, YELLOW: 2, GREEN: 1, UNKNOWN: 0}
_VALID = set(_SEVERITY)


@dataclass
class Signal:
    """Один прочитанный сигнал здоровья: имя, цвет и ПРИЧИНА цвета (обязательна)."""

    name: str
    band: str
    reason: str
    detail: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.band not in _VALID:
            raise ValueError(
                f"неизвестный band {self.band!r}; допустимые: {sorted(_VALID)}"
            )
        if not self.reason or not self.reason.strip():
            raise ValueError(
                f"сигнал {self.name!r} без причины — цвет без причины бесполезен"
            )

    def as_dict(self) -> dict:
        out = {"name": self.name, "band": self.band, "reason": self.reason}
        if self.detail:
            out["detail"] = self.detail
        return out


def rollup(signals) -> str:
    """Свести список сигналов в итоговый band. Худший известный цвет побеждает; при отсутствии
    известных сигналов — UNKNOWN."""
    known = [s for s in signals if s.band != UNKNOWN]
    if not known:
        return UNKNOWN
    return max((s.band for s in known), key=lambda b: _SEVERITY[b])


def build_report(kind: str, signals, *, scope: str = "product") -> dict:
    """Собрать машиночитаемый health-отчёт из сигналов.

    `reasons` — причины именно тех сигналов, что задали итоговый band (drivers). Если итог
    UNKNOWN — причины непрочитанных сигналов, чтобы человек видел, ПОЧЕМУ здоровье не определено.
    """
    signals = list(signals)
    band = rollup(signals)
    unverified = [s.name for s in signals if s.band == UNKNOWN]
    if band == UNKNOWN:
        drivers = [s for s in signals if s.band == UNKNOWN]
    else:
        drivers = [s for s in signals if s.band == band]
    return {
        "schema_version": 1,
        "kind": kind,
        "scope": scope,
        "band": band,
        "reasons": [s.reason for s in drivers],
        "complete": len(unverified) == 0,
        "unverified": unverified,
        "signals": [s.as_dict() for s in signals],
    }
