#!/usr/bin/env python3
"""Связь roadmap ↔ milestones ↔ backlog: цепочка направление→milestone→задачи (PR-7).

Roadmap отвечает на «куда идём» направлениями и исходами; backlog (задачи, GitHub Issues) —
операционная единица работы. Между ними milestone: срез backlog под направление с датой. Этот
модуль СВЯЗЫВАЕТ три уровня и показывает, где цепочка рвётся, — не дублируя backlog в roadmap.

ГРАНИЦА С ЛЕНТОЙ 3. Backlog собирает и классифицирует лента 3 (GitHub Issues). Мы его НЕ читаем из
GitHub и НЕ лезем в её модули — работаем по КОНТРАКТУ ДАННЫХ (форма задачи ниже). Пока лента 3 не
подключила источник, связывание строится на переданных списках и тестируется на фикстуре; когда
источник появится, к `link` подставляется её вывод той же формы. Контракт — потребительский:
описан здесь как то, что нам нужно от задачи, и подлежит сверке с лентой 3.

КОНТРАКТ ЗАДАЧИ backlog (dict, лишние поля игнорируются):
    id            str            — идентификатор (номер Issue)
    title         str
    milestone     str | None     — id/имя milestone; None — не привязана (третье состояние)
    roadmap       str | None     — id направления (цели плана), которое задача продвигает; опц.
    dependencies  list[str]      — id задач, от которых зависит; опц.
    status        str            — open | in_progress | closed; опц.

КОНТРАКТ milestone (dict):
    id            str
    title         str            — опц.
    roadmap       str | None     — id направления, которому milestone служит; опц.

СВЯЗЫВАНИЕ. Направление (цель плана) ↔ milestone ↔ задачи:
  * milestone привязан к направлению, если m.roadmap == goal_id ЛИБО в нём есть задача с
    task.roadmap == goal_id;
  * задача привязана к направлению, если task.roadmap == goal_id ЛИБО её milestone привязан к нему.

ТРЕТЬЕ СОСТОЯНИЕ ВЕЗДЕ. «Backlog не подключён» (`connected=False`) ≠ «backlog пуст». «Направление
без milestone» ≠ «milestone пуст». Разрыв цепочки — ВИДИМАЯ находка отчёта, а не молчаливый ноль.

Использование:
  roadmap_milestones.py link <repo> --backlog <file.yaml|.json> [--json]
  roadmap_milestones.py link <repo>            # без источника: отчёт «backlog не подключён (лента 3)»
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ai_ops_kit.planning import delivery_plan as _plan
from ai_ops_kit.planning import roadmap_manager as _rm


@dataclass
class DirectionLink:
    goal_id: str
    horizon: str
    milestones: list = field(default_factory=list)
    task_ids: list = field(default_factory=list)
    breaks: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"goal": self.goal_id, "horizon": self.horizon,
                "milestones": list(self.milestones), "tasks": list(self.task_ids),
                "breaks": list(self.breaks)}


def _as_list(x) -> list:
    return [t for t in (x or []) if isinstance(t, dict)]


def link(roadmap: _rm.Roadmap, milestones, tasks) -> dict:
    """Связать выведенный roadmap с milestones и backlog. -> отчёт-словарь.

    `tasks is None` — источник backlog не подключён (третье состояние), а не «задач нет».
    """
    if tasks is None:
        return {"connected": False, "directions": [], "orphan_tasks": [],
                "dangling_links": [], "unlinked_now": [],
                "note": "источник backlog не подключён (лента 3) — связывать нечего"}

    tasks = _as_list(tasks)
    milestones = _as_list(milestones)
    goal_ids = {d.goal_id for d in roadmap.directions}
    ms_by_id = {m.get("id"): m for m in milestones if m.get("id")}

    # milestone -> набор направлений, которым он служит (из m.roadmap и из задач в нём).
    ms_goals: dict = {mid: set() for mid in ms_by_id}
    for mid, m in ms_by_id.items():
        if m.get("roadmap") in goal_ids:
            ms_goals[mid].add(m["roadmap"])
    for t in tasks:
        mid, g = t.get("milestone"), t.get("roadmap")
        if mid in ms_goals and g in goal_ids:
            ms_goals[mid].add(g)

    dangling, orphans = [], []
    for t in tasks:
        g = t.get("roadmap")
        if g is not None and g not in goal_ids:
            dangling.append(f"задача '{t.get('id')}': направление '{g}' не найдено в плане")
        mid = t.get("milestone")
        if mid is not None and mid not in ms_by_id:
            orphans.append(f"задача '{t.get('id')}': milestone '{mid}' не найден среди milestones")
        elif mid is not None and not ms_goals.get(mid):
            orphans.append(
                f"задача '{t.get('id')}': milestone '{mid}' не привязан ни к одному направлению")

    directions, unlinked_now = [], []
    for d in roadmap.directions:
        g = d.goal_id
        ms = sorted(mid for mid, gs in ms_goals.items() if g in gs)
        tset = [t.get("id") for t in tasks
                if t.get("roadmap") == g or (t.get("milestone") in ms)]
        breaks = []
        # milestone под направление, но без задач — разрыв в середине цепочки.
        for mid in ms:
            if not any(t.get("milestone") == mid for t in tasks):
                breaks.append(f"milestone '{mid}' привязан к направлению, но без задач")
        dl = DirectionLink(goal_id=g, horizon=d.horizon, milestones=ms,
                           task_ids=tset, breaks=breaks)
        # Now-направление обязано иметь прослеживаемую цепочку. Обрыв — на направлении.
        if d.horizon == _rm.NOW and not ms and not tset:
            dl.breaks.append("направление в Now без milestone и задач — цепочка рвётся на направлении")
            unlinked_now.append(g)
        directions.append(dl.as_dict())

    return {"connected": True, "directions": directions, "orphan_tasks": orphans,
            "dangling_links": dangling, "unlinked_now": unlinked_now}


def _load_backlog(path):
    """Прочитать источник backlog (yaml/json). -> (milestones, tasks) или (None, None) если нет пути.

    Форма файла: {"milestones": [...], "tasks": [...]}. Отсутствие файла по указанному пути —
    ОШИБКА (путь назвали, а его нет), а отсутствие самого пути — третье состояние выше.
    """
    if not path:
        return None, None
    import yaml
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"источник backlog не найден: {p}")
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return doc.get("milestones") or [], doc.get("tasks")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="roadmap_milestones.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("link")
    p.add_argument("repo")
    p.add_argument("--backlog", default=None, help="файл {milestones, tasks} (лента 3)")
    p.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])
    root = Path(ns.repo)

    try:
        plan = _plan.load(root)
    except _plan.PlanCorrupt as e:
        print(f"  ✗ {e}"); return 2
    if plan is None:
        print("  ✗ нет planning/plan.yaml — roadmap выводить не из чего"); return 2

    try:
        milestones, tasks = _load_backlog(ns.backlog)
    except FileNotFoundError as e:
        print(f"  ✗ {e}"); return 2

    roadmap = _rm.build(plan, _plan.load_history(root))
    rep = link(roadmap, milestones, tasks)

    if ns.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 1 if (rep.get("unlinked_now") or rep.get("dangling_links")) else 0

    if not rep["connected"]:
        print(f"  ⚠ {rep['note']}")
        return 0
    for d in rep["directions"]:
        if d["horizon"] not in (_rm.NOW, _rm.NEXT):
            continue
        chain = f"{len(d['milestones'])} milestone / {len(d['tasks'])} задач"
        print(f"  • {d['goal']} [{d['horizon']}]: {chain}")
        for b in d["breaks"]:
            print(f"      ⚠ {b}")
    for o in rep["orphan_tasks"]:
        print(f"  ⚠ {o}")
    for dl in rep["dangling_links"]:
        print(f"  ✗ {dl}")
    problems = len(rep["unlinked_now"]) + len(rep["dangling_links"])
    print(f"BACKLOG-LINK: разрывов в Now {len(rep['unlinked_now'])}, "
          f"висячих ссылок {len(rep['dangling_links'])}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
