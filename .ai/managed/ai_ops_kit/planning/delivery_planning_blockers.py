#!/usr/bin/env python3
"""Ранние блокеры и сигналы delivery: до срыва срока, а не постфактум (PR-15).

ГРАНИЦА С ЛЕНТОЙ 5. Здоровье delivery Green/Yellow/Red СЧИТАЕТ лента 5
(`intelligence/health_delivery.py`) — второй такой band здесь заводить нельзя (`dp-001`: дубли
уже построенного отклоняем). Этот модуль — ПРОИЗВОДИТЕЛЬ того, что лента 5 читает: он собирает из
backlog выгрузку `.ai-ops/delivery-signals.yaml` В ТОЙ ФОРМЕ, что потребляет `health_delivery`
(`milestone: {done, total}`, `overdue: {count}`), и отдельно находит РАННИЕ БЛОКЕРЫ — задачи,
которые держат больше всего ещё не сделанной работы, пока они ещё открыты. Один считает сигнал,
другой красит band; двух правд об одном числе нет.

РАННИЙ — ЗНАЧИТ ДО СРЫВА. Блокер выявляется по графу зависимостей backlog: открытая задача, от
которой (транзитивно) зависят другие открытые задачи. Чем больше держит — тем выше в списке.
Просроченность помечается, но блокер попадает в список НЕ из-за неё: смысл — увидеть узкое место,
пока срок ещё не сорван, а не отчитаться о сорванном.

ТРЕТЬЕ СОСТОЯНИЕ. Метрика без данных — `unavailable`, а не 0. Просрочку нельзя определить без даты
«сегодня» и без due у задач — тогда её НЕ выдаём числом (не пишем `overdue: 0`, будто проверили),
а честно опускаем: лента 5 прочитает отсутствие как UNKNOWN, что и есть правда. Дата «сегодня»
передаётся аргументом, а не берётся из часов (иначе тест менялся бы день ото дня).

КОНТРАКТ задачи — тот же, что у `roadmap_milestones`/`delivery_planning`, плюс необязательные:
    due       str | None   — срок задачи (YYYY-MM-DD); нет — просрочку по ней не считаем
    closed_at str | None   — дата закрытия; по ней (и только по ней) считается velocity

Использование:
  delivery_planning_blockers.py report <repo> --backlog <file> [--milestone id] [--today D] [--json]
  delivery_planning_blockers.py emit   <repo> --backlog <file> --milestone id [--today D]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path


def _open(tasks):
    return [t for t in tasks if t.get("status") != "closed"]


def _is_overdue(task, today):
    """Просрочена ли задача. -> True/False/None (None — определить нельзя, третье состояние)."""
    due = task.get("due")
    if not due or not today:
        return None
    return date.fromisoformat(due) < date.fromisoformat(today) and task.get("status") != "closed"


def _dependents(tasks):
    """Для каждой задачи — множество транзитивных зависимых (кто ждёт её). -> {id: set(ids)}."""
    ids = {t.get("id") for t in tasks}
    direct = {i: set() for i in ids}
    for t in tasks:
        for d in (t.get("dependencies") or []):
            if d in ids:                       # t зависит от d => t зависим от d
                direct[d].add(t.get("id"))

    def reach(start):
        seen, stack = set(), list(direct[start])
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(direct.get(n, ()))
        return seen

    return {i: reach(i) for i in ids}


def early_blockers(tasks, today=None):
    """Открытые задачи, держащие ещё не сделанную работу, по убыванию влияния. -> list[dict]."""
    open_ids = {t.get("id") for t in _open(tasks)}
    dependents = _dependents(tasks)
    by_id = {t.get("id"): t for t in tasks}
    out = []
    for i in open_ids:
        downstream = sorted(dependents.get(i, set()) & open_ids)
        if not downstream:
            continue
        out.append({"id": i, "blocks": downstream, "downstream": len(downstream),
                    "overdue": _is_overdue(by_id[i], today), "status": by_id[i].get("status")})
    out.sort(key=lambda b: (-b["downstream"], b["id"]))
    return out


def delivery_signals(tasks, milestone_id, today=None):
    """Выгрузка для health_delivery ленты 5 + расширенные поля. -> dict формы .ai-ops/delivery-signals.

    Пишем ТОЛЬКО то, что посчитали. Просрочку без дат не выдаём числом — опускаем ключ (лента 5
    прочитает как UNKNOWN). velocity — только если у закрытых задач есть `closed_at`.
    """
    in_ms = [t for t in tasks if t.get("milestone") == milestone_id]
    total = len(in_ms)
    done = sum(1 for t in in_ms if t.get("status") == "closed")
    sig = {"milestone": {"done": done, "total": total}}

    # Просрочка: считаем, только если хоть у одной открытой задачи есть due и передан today.
    overdue_ids = [t.get("id") for t in _open(in_ms) if _is_overdue(t, today)]
    determinable = today and any(t.get("due") for t in _open(in_ms))
    if determinable:
        sig["overdue"] = {"count": len(overdue_ids), "ids": overdue_ids}
    else:
        sig["overdue_unavailable"] = "нет даты 'сегодня' или due у задач — просрочка не определена"

    # Заблокированные внутри milestone: открытая задача с открытой зависимостью.
    open_ids = {t.get("id") for t in _open(in_ms)}
    blocked = [t.get("id") for t in _open(in_ms)
               if any(d in open_ids for d in (t.get("dependencies") or []))]
    sig["blocked"] = {"count": len(blocked), "ids": sorted(blocked)}

    # velocity: закрытия по датам. Нет ни одной closed_at — метрика недоступна, а не ноль.
    closed_dates = [t.get("closed_at") for t in in_ms
                    if t.get("status") == "closed" and t.get("closed_at")]
    if closed_dates:
        sig["velocity"] = {"closed_with_date": len(closed_dates)}
    else:
        sig["velocity_unavailable"] = "у закрытых задач нет closed_at — темп не определён"
    return sig


def report(tasks, milestone_id=None, today=None) -> dict:
    """Ранние блокеры + сигналы (если задан milestone). Чистая функция, тест на фикстуре."""
    rep = {"early_blockers": early_blockers(tasks, today)}
    if milestone_id is not None:
        rep["signals"] = delivery_signals(tasks, milestone_id, today)
    return rep


SIGNALS_REL = ".ai-ops/delivery-signals.yaml"


def write_signals(root, tasks, milestone_id, today=None) -> Path:
    """Записать `.ai-ops/delivery-signals.yaml` — вход для health_delivery ленты 5. -> путь."""
    import yaml
    out = Path(root) / SIGNALS_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(delivery_signals(tasks, milestone_id, today),
                                  allow_unicode=True, sort_keys=False), encoding="utf-8")
    return out


def _load_tasks(path):
    import yaml
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"источник backlog не найден: {p}")
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return [t for t in (doc.get("tasks") or []) if isinstance(t, dict)]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="delivery_planning_blockers.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report")
    r.add_argument("repo"); r.add_argument("--backlog", required=True)
    r.add_argument("--milestone", default=None); r.add_argument("--today", default=None)
    r.add_argument("--json", action="store_true")
    e = sub.add_parser("emit")
    e.add_argument("repo"); e.add_argument("--backlog", required=True)
    e.add_argument("--milestone", required=True); e.add_argument("--today", default=None)
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])

    try:
        tasks = _load_tasks(ns.backlog)
    except FileNotFoundError as exc:
        print(f"  ✗ {exc}"); return 2

    if ns.cmd == "emit":
        path = write_signals(ns.repo, tasks, ns.milestone, ns.today)
        print(f"выгрузка delivery: {path} (её читает health_delivery ленты 5)")
        return 0

    rep = report(tasks, ns.milestone, ns.today)
    if ns.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 1 if rep["early_blockers"] else 0
    blockers = rep["early_blockers"]
    if not blockers:
        print("РАННИХ БЛОКЕРОВ НЕТ: ни одна открытая задача не держит другую открытую")
    for b in blockers:
        od = " ПРОСРОЧЕН" if b["overdue"] else (" (просрочку не определить)" if b["overdue"] is None else "")
        print(f"  ⚠ '{b['id']}' держит {b['downstream']} задач{od}: {b['blocks'][:5]}")
    if "signals" in rep:
        s = rep["signals"]
        print(f"  сигналы milestone {ns.milestone}: "
              f"done {s['milestone']['done']}/{s['milestone']['total']}, "
              f"blocked {s['blocked']['count']}")
    print(f"BLOCKERS: ранних блокеров {len(blockers)}")
    return 1 if blockers else 0


if __name__ == "__main__":
    sys.exit(main())
