#!/usr/bin/env python3
"""Backlog → исполнимый delivery-план: выбор под milestone, порядок, прогноз, delivery-risk (PR-10).

ЧЕМ ЭТО НЕ `delivery_plan.py`. `delivery_plan.py` — ВНУТРЕННИЙ план самого кита (что кит делает над
собой) и его инфраструктура. Здесь — продуктовый delivery-план ДОЧКИ: из её backlog (задач) под
выбранный milestone собирается исполнимая последовательность с учётом зависимостей и capacity,
считается прогноз срока и называются риски поставки. Два модуля с похожими именами — намеренно
разные уровни: имя `delivery_planning` (а не `delivery_plan`) держит границу.

ГРАНИЦА С ЛЕНТАМИ 3 И 4. Backlog даёт лента 3 по КОНТРАКТУ (та же форма задачи, что читает
`roadmap_milestones`), milestone-привязку — `roadmap_milestones`. Мы НЕ читаем GitHub. Контракт
задачи здесь расширен одним полем поверх формы `roadmap_milestones`:
    effort  int | None   — оценка трудоёмкости (абстрактные единицы); None — НЕ ЗАДАНА (третье
                           состояние), а не ноль. Без неё прогноз честно отказывается считать.

ПРОГНОЗ — ОЦЕНКА, А НЕ ФАКТ. Это главный инвариант модуля. Прогноз срока помечается `estimate` и
несёт основание (`basis`); при неизвестной capacity ИЛИ неизвестном effort хотя бы одной задачи он
возвращает `available=False` с причиной — «оценить нельзя», а НЕ выдуманную дату и не ноль. Дата
старта передаётся аргументом, а не берётся из системных часов: иначе тест зависел бы от «сегодня»
и тихо менялся день ото дня (тот же принцип, что «фикстура, не живое состояние»).

Использование:
  delivery_planning.py plan <repo> --backlog <file> --milestone <id> [--capacity N] \\
      [--start YYYY-MM-DD] [--json]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path


@dataclass
class Forecast:
    available: bool
    reason: str = ""
    effort_total: int = 0
    days: int = 0
    start: str = ""
    estimated_end: str = ""

    def as_dict(self) -> dict:
        d = {"available": self.available, "kind": "estimate", "reason": self.reason}
        if self.available:
            d.update({"effort_total": self.effort_total, "days": self.days,
                      "start": self.start, "estimated_end": self.estimated_end})
        return d


@dataclass
class DeliveryPlan:
    milestone: str
    sequence: list = field(default_factory=list)         # id задач в порядке исполнения
    excluded_closed: list = field(default_factory=list)  # уже закрытые — в план не берём
    forecast: Forecast = None
    risks: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"milestone": self.milestone, "sequence": list(self.sequence),
                "excluded_closed": list(self.excluded_closed),
                "forecast": self.forecast.as_dict() if self.forecast else None,
                "risks": list(self.risks)}


def _select(tasks, milestone_id):
    """Задачи milestone, кроме уже закрытых. -> (open_tasks, closed_ids)."""
    in_ms = [t for t in tasks if t.get("milestone") == milestone_id]
    closed = [t.get("id") for t in in_ms if t.get("status") == "closed"]
    open_tasks = [t for t in in_ms if t.get("status") != "closed"]
    return open_tasks, closed


def _sequence(open_tasks):
    """Топологический порядок по зависимостям ВНУТРИ выборки. -> (order, cycle_ids).

    Зависимости на задачи вне выборки порядок не задают (их учитывает риск ниже), но связь между
    задачами milestone обязана соблюдаться: A после того, от чего A зависит.
    """
    ids = [t.get("id") for t in open_tasks]
    idset = set(ids)
    deps = {t.get("id"): [d for d in (t.get("dependencies") or []) if d in idset]
            for t in open_tasks}
    order, visited, done = [], {}, set()
    cycle = []

    def visit(n, stack):
        if n in done:
            return
        if visited.get(n) == "open":     # уже в текущем стеке — цикл
            cycle.append(n)
            return
        visited[n] = "open"
        for d in deps.get(n, []):
            visit(d, stack + [n])
        visited[n] = "closed"
        done.add(n)
        order.append(n)

    for n in ids:                        # исходный порядок задаёт стабильность при равных
        visit(n, [])
    return order, sorted(set(cycle))


def _forecast(open_tasks, order, capacity, start):
    """Прогноз завершения. ОЦЕНКА, не факт: отказывается считать без данных, не выдумывает дату."""
    unknown = [t.get("id") for t in open_tasks if t.get("effort") is None]
    if unknown:
        return Forecast(False, reason=(
            f"оценить нельзя: у {len(unknown)} задач не задан effort ({unknown[:5]}) — "
            f"прогноз без данных был бы выдумкой, а не оценкой"))
    if not capacity or capacity <= 0:
        return Forecast(False, reason=(
            "оценить нельзя: capacity команды не задана (третье состояние, а не ноль)"))
    total = sum(int(t.get("effort") or 0) for t in open_tasks)
    days = math.ceil(total / capacity) if total else 0
    fc = Forecast(True, effort_total=total, days=days,
                  reason=f"оценка: {total} ед. трудоёмкости при capacity {capacity} ед./день")
    if start:
        d0 = date.fromisoformat(start)
        fc.start = start
        fc.estimated_end = (d0 + timedelta(days=days)).isoformat()
    return fc


def _risks(open_tasks, milestone_id, cycle, forecast, due):
    risks = []
    if cycle:
        risks.append(f"циклическая зависимость между задачами: {cycle} — порядок не определён")
    idset = {t.get("id") for t in open_tasks}
    for t in open_tasks:
        outside = [d for d in (t.get("dependencies") or []) if d not in idset]
        if outside:
            risks.append(
                f"задача '{t.get('id')}' ждёт зависимости вне milestone '{milestone_id}': {outside}")
    if not forecast.available:
        risks.append(f"прогноз недоступен — {forecast.reason}")
    elif due and forecast.estimated_end and forecast.estimated_end > due:
        risks.append(
            f"оценка завершения {forecast.estimated_end} позже дедлайна milestone {due} — "
            f"риск срыва срока (это оценка, не факт)")
    return risks


def plan(tasks, milestone_id, capacity=None, start=None, due=None) -> DeliveryPlan:
    """Собрать delivery-план под milestone. Чистая функция — тестируется на фикстуре."""
    open_tasks, closed = _select(tasks, milestone_id)
    order, cycle = _sequence(open_tasks)
    forecast = _forecast(open_tasks, order, capacity, start)
    risks = _risks(open_tasks, milestone_id, cycle, forecast, due)
    return DeliveryPlan(milestone=milestone_id, sequence=order, excluded_closed=closed,
                        forecast=forecast, risks=risks)


def _load_backlog(path):
    if not path:
        return None
    import yaml
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"источник backlog не найден: {p}")
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser(prog="delivery_planning.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("repo")
    p.add_argument("--backlog", required=True, help="файл {tasks, milestones} (лента 3)")
    p.add_argument("--milestone", required=True)
    p.add_argument("--capacity", type=float, default=None, help="ед. трудоёмкости в день")
    p.add_argument("--start", default=None, help="дата старта YYYY-MM-DD")
    p.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])

    try:
        doc = _load_backlog(ns.backlog)
    except FileNotFoundError as e:
        print(f"  ✗ {e}"); return 2
    tasks = [t for t in (doc.get("tasks") or []) if isinstance(t, dict)]
    due = None
    for m in (doc.get("milestones") or []):
        if isinstance(m, dict) and m.get("id") == ns.milestone:
            due = m.get("due")
            break

    dp = plan(tasks, ns.milestone, capacity=ns.capacity, start=ns.start, due=due)
    if ns.json:
        print(json.dumps(dp.as_dict(), ensure_ascii=False, indent=2))
        return 1 if dp.risks else 0
    print(f"MILESTONE {ns.milestone}: последовательность {dp.sequence or '(пусто)'}")
    if dp.forecast.available:
        end = f" → {dp.forecast.estimated_end}" if dp.forecast.estimated_end else ""
        print(f"  прогноз (ОЦЕНКА): {dp.forecast.days} дн.{end} — {dp.forecast.reason}")
    else:
        print(f"  прогноз: НЕДОСТУПЕН — {dp.forecast.reason}")
    for r in dp.risks:
        print(f"  ⚠ {r}")
    print(f"DELIVERY: рисков {len(dp.risks)}")
    return 1 if dp.risks else 0


if __name__ == "__main__":
    sys.exit(main())
