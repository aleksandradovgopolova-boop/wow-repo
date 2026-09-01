#!/usr/bin/env python3
"""events.py — шина событий ядра (v3.38, trustworthy-core Wave 3).

Простой синхронный event bus: emit() → подписчики. Отказ подписчика НЕ роняет ядро
(fail-safe: ошибка ловится и записывается в журнал). Ядро только испускает события;
спутники (engops/intelligence/planning) подписываются и реагируют.

События (KernelEvent в contracts.py):
  run_completed     — прогон завершён (report, workitem_id, status)
  gate_evaluated    — гейты оценены (gate_results, workitem_id)
  delivery_completed — доставка выполнена (receipt, workitem_id)

Использование:
  from ai_ops_kit.shared.events import emit, subscribe
  subscribe("run_completed", my_handler)
  emit("run_completed", {"workitem_id": "x", "status": "done", "report": {...}})

Инвариант: ядро НЕ импортирует спутники. Спутники подписываются на события ядра.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Глобальный реестр подписчиков: event_type -> [handler, ...]
_subscribers: dict[str, list[Callable]] = {}


def subscribe(event_type: str, handler: Callable) -> None:
    """Подписать handler на события типа event_type. Handler вызывается с одним аргументом — dict."""
    _subscribers.setdefault(event_type, []).append(handler)


def unsubscribe(event_type: str, handler: Callable) -> None:
    """Отписать handler от событий типа event_type."""
    handlers = _subscribers.get(event_type, [])
    if handler in handlers:
        handlers.remove(handler)


def emit(event_type: str, data: dict[str, Any]) -> None:
    """Испустить событие. Все подписчики вызываются синхронно. Ошибка в подписчике НЕ роняет emit."""
    for handler in _subscribers.get(event_type, []):
        try:
            handler(data)
        except Exception as e:  # noqa: BLE001 — причина ЗАПИСАНА: отказ подписчика-спутника не вправе ронять ядро (fail-safe шины, см. докстринг)
            logger.warning("Event subscriber %s failed on %s: %s", handler.__name__, event_type, e)


def clear() -> None:
    """Очистить все подписчики (для тестов)."""
    _subscribers.clear()


def subscriber_count(event_type: str | None = None) -> int:
    """Количество подписчиков (для тестов/наблюдаемости)."""
    if event_type is not None:
        return len(_subscribers.get(event_type, []))
    return sum(len(v) for v in _subscribers.values())
