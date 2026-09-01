#!/usr/bin/env python3
"""Delivery Health (PR-15): здоровье delivery Green / Yellow / Red С НАЗВАННОЙ ПРИЧИНОЙ.

Отдельно от product/tech — отвечает на вопрос: движется ли работа. Два источника:

  1. Состояние работ из плана (`planning/plan.yaml`) — ПЕРЕИСПОЛЬЗУЕТ парсер плана кита
     (delivery_plan.load/items), не свой (dp-001). Считает заблокированные работы: открытая
     работа, чья зависимость всё ещё не закрыта (её id ещё в активном плане). Данные ленты 4
     (delivery-план) кит НЕ правит — читает по контракту; свой delivery_plan.py не трогает.
  2. Выгрузка `.ai-ops/delivery-signals.yaml` (milestone/velocity/cycle-time), которую кладёт
     интеграция с трекером. Формат:
         milestone: {done: 7, total: 10}
         overdue:   {count: 0}

Инвариант health_common: чего прочитать не удалось — UNKNOWN с причиной, не зелёный. Нет ни
плана, ни выгрузки — итог unknown, а не «delivery в порядке».

Использование:  python3 -m ai_ops_kit.intelligence.health_delivery <repo_root> [-o report.json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from ai_ops_kit.intelligence import health_common as hc
from ai_ops_kit.planning import delivery_plan

KIND = "delivery-health-report"
SIGNALS_REL = ".ai-ops/delivery-signals.yaml"
PLAN_REL = "planning/plan.yaml"

_OPEN = {"todo", "in_progress"}


def _blocked_signal(root: Path) -> hc.Signal:
    plan_path = root / PLAN_REL
    if not plan_path.is_file():
        return hc.Signal("blocked_work", hc.UNKNOWN,
                         f"плана работ ({PLAN_REL}) нет — заблокированность не определить")
    try:
        plan = delivery_plan.load(root, path=plan_path)
    except delivery_plan.PlanCorrupt as exc:
        return hc.Signal("blocked_work", hc.UNKNOWN,
                         f"план не разбирается ({exc}) — заблокированность не определить")
    work = delivery_plan.items(plan)
    open_ids = {w.get("id") for w in work if w.get("status") in _OPEN}
    open_work = [w for w in work if w.get("status") in _OPEN]
    if not open_work:
        return hc.Signal("blocked_work", hc.GREEN,
                         "открытых работ нет — заблокированных тоже")
    # зависимость «не закрыта» = её id всё ещё среди открытых работ активного плана
    # (закрытая работа уезжает в историю и из плана исчезает).
    blocked = [w for w in open_work
               if any(dep in open_ids for dep in (w.get("depends_on") or []))]
    if not blocked:
        return hc.Signal("blocked_work", hc.GREEN,
                         f"ни одна из {len(open_work)} открытых работ не ждёт незакрытой зависимости")
    names = ", ".join(w.get("id", "?") for w in blocked[:3])
    more = "" if len(blocked) <= 3 else f" (+{len(blocked) - 3})"
    if len(blocked) == len(open_work):
        return hc.Signal("blocked_work", hc.RED,
                         f"все {len(open_work)} открытых работ заблокированы зависимостями: {names}{more}",
                         detail={"blocked": len(blocked), "open": len(open_work)})
    return hc.Signal("blocked_work", hc.YELLOW,
                     f"{len(blocked)} из {len(open_work)} открытых работ ждут незакрытых зависимостей: {names}{more}",
                     detail={"blocked": len(blocked), "open": len(open_work)})


def _milestone_signal(root: Path) -> hc.Signal:
    path = root / SIGNALS_REL
    if not path.exists():
        return hc.Signal("milestone", hc.UNKNOWN,
                         f"выгрузка delivery ({SIGNALS_REL}) отсутствует — прогресс milestone не определить")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        ms = data.get("milestone") if isinstance(data, dict) else None
        if not isinstance(ms, dict) or "done" not in ms or "total" not in ms:
            raise ValueError("нет milestone.done/total")
    except (yaml.YAMLError, ValueError) as exc:
        return hc.Signal("milestone", hc.UNKNOWN,
                         f"выгрузка delivery не даёт прогресс milestone ({exc}) — не проверено")
    total = float(ms["total"])
    if total <= 0:
        return hc.Signal("milestone", hc.UNKNOWN,
                         "в выгрузке milestone.total = 0 — прогресс не определён")
    ratio = float(ms["done"]) / total
    overdue = 0
    ov = data.get("overdue") if isinstance(data, dict) else None
    if isinstance(ov, dict):
        overdue = int(ov.get("count", 0))
    if overdue > 0:
        return hc.Signal("milestone", hc.RED,
                         f"milestone {ms['done']}/{ms['total']}, просрочено задач: {overdue}")
    if ratio >= 0.9:
        band, why = hc.GREEN, f"milestone почти закрыт: {ms['done']}/{ms['total']}"
    elif ratio >= 0.5:
        band, why = hc.YELLOW, f"milestone в середине: {ms['done']}/{ms['total']}"
    else:
        band, why = hc.RED, f"milestone отстаёт: {ms['done']}/{ms['total']}"
    return hc.Signal("milestone", band, why, detail={"ratio": round(ratio, 4)})


def collect_signals(root: Path) -> list:
    return [_blocked_signal(root), _milestone_signal(root)]


def delivery_health_report(root: Path) -> dict:
    return hc.build_report(KIND, collect_signals(root), scope="delivery")


def main(argv) -> int:
    if not argv:
        print(__doc__)
        return 1
    root = Path(argv[0])
    if not root.is_dir():
        print(f"не каталог: {root}", file=sys.stderr)
        return 2
    report = delivery_health_report(root)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if "-o" in argv:
        out = Path(argv[argv.index("-o") + 1])
        out.write_text(text + "\n", encoding="utf-8")
        tail = "" if report["complete"] else f", не проверено: {report['unverified']}"
        print(f"отчёт: {out} (delivery health: {report['band']}{tail})")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
