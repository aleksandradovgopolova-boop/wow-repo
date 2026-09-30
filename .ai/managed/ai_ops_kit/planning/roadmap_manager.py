#!/usr/bin/env python3
"""Автоведение roadmap: Now / Next / Later ВЫВОДЯТСЯ из плана, а не пишутся руками (PR-7).

ЧЕМ ЭТО НЕ `roadmap.py`. `roadmap.py` — валидатор АВТОРСКОГО `ROADMAP.md`: он разбирает файл,
который написал человек, и сверяет цели с планом. Он НЕ читает исходы целей и НЕ умеет сказать,
в каком горизонте направление обязано стоять по факту. Здесь — обратная сторона того же шва:
горизонт направления ВЫВОДИТСЯ из состояния плана (исходы цели + состояние её работ), поэтому
roadmap создаётся и актуализируется автоматически, а расхождение авторского файла с выведенным
состоянием становится ВИДИМЫМ (`check`). Двух правд об одном горизонте нет: авторскую сторону
по-прежнему разбирает `roadmap.parse`, мы её только сверяем.

НАПРАВЛЕНИЕ, А НЕ ЗАДАЧА. Направление roadmap — это ЦЕЛЬ плана (`goals[]`) с её OUTCOMES (словарь
`outcome`), а не работа из `work[]`. Backlog (работы, GitHub Issues) сюда не дублируется: горизонт
считается по тому, ДОСТИГНУТЫ ли исходы и ДВИЖЕТСЯ ли под них работа, а не по списку задач.

КАК ВЫВОДИТСЯ ГОРИЗОНТ (из плана, без живого состояния других лент):
    shipped — все исходы направления достигнуты (на roadmap Now/Next/Later не показывается);
    now     — под направление ИДЁТ работа (in_progress) или есть готовая к взятию (deps закрыты);
    next    — работа есть, но вся ждёт закрытия зависимостей; либо часть исходов уже достигнута,
              а под остальное активной работы нет;
    later   — направление ещё не декомпозировано в работу (исходов достигнуто ноль, работ нет).

ОТКЛОНЕНИЕ — declared vs derived. Как и везде в ките (`status_declared` ↔ выведенный,
`plan-agrees-with-git`): авторский горизонт из `ROADMAP.md` сверяется с выведенным. «Направление в
активной работе, но в roadmap стоит под Later» и «исходы достигнуты, а направление всё ещё в Now» —
тихие расхождения, которые иначе живут месяцами. Про третье состояние: нет авторского файла — это
НЕ «расхождений нет», а «сверять нечего», и так и говорится.

Использование:
  roadmap_manager.py build  <repo> [--json]   # выведенные Now/Next/Later из плана
  roadmap_manager.py render <repo>            # содержимое ROADMAP.md, собранное из плана
  roadmap_manager.py check  <repo> [--json]   # отклонение авторского ROADMAP.md от выведенного
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ai_ops_kit.planning import delivery_plan as _plan
from ai_ops_kit.planning import roadmap as _authored

# Горизонты, которые ПОКАЗЫВАЕТ roadmap. `shipped` горизонтом не является — это причина не
# показывать направление среди Now/Next/Later, но её видно в отчёте.
NOW, NEXT, LATER, SHIPPED = "now", "next", "later", "shipped"
HORIZONS = (NOW, NEXT, LATER)

# Ранг горизонта для сравнения авторского и выведенного. shipped ниже всех: завершённому направлению
# не место в будущих горизонтах.
_RANK = {SHIPPED: -1, NOW: 0, NEXT: 1, LATER: 2}

# Соответствие горизонтов `roadmap.py` (авторская сторона) нашим трём. `roadmap.py` различает
# «Дальше» и «Later»; для сверки горизонта оба — это «не сейчас», ранг 2.
_AUTHORED_TO_RANK = {"now": 0, "next_outcome": 1, "later_major": 2, "someday": 2}

# Человеческие имена горизонтов. Для не-разработчика слаги now/next/later ничего не значат, поэтому
# в тексте для человека горизонт называется словами. Слаг остаётся техническим ключом в данных
# (as_dict, --json), словами он подаётся только в печати.
HORIZON_HUMAN = {
    NOW: "в работе сейчас",
    NEXT: "запланировано следующим",
    LATER: "пока не взято в работу",
    SHIPPED: "уже достигнуто",
}


def humanize_outcomes(reached: int, total: int) -> str:
    """«исходы 2/3» человеческим языком: сколько результатов направления уже достигнуто.

    Голый счётчик «0/2» не говорит человеку, что считается. Разворачиваем в предложение, а при
    отсутствии заданных результатов честно так и пишем, а не показываем «0/0».
    """
    if not total:
        return "результаты пока не заданы"
    return f"достигнуто {reached} из {total} результатов"


@dataclass
class Outcome:
    name: str
    reached: bool


@dataclass
class Direction:
    """Направление roadmap = цель плана с её исходами. Горизонт ВЫВЕДЕН, не объявлен."""
    goal_id: str
    title: str
    horizon: str
    outcomes: list = field(default_factory=list)
    reached: int = 0
    total: int = 0
    active_work: int = 0
    blocked_work: int = 0
    work_ids: list = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "goal": self.goal_id,
            "title": self.title,
            "horizon": self.horizon,
            "outcomes": [{"name": o.name, "reached": o.reached} for o in self.outcomes],
            "reached": self.reached,
            "total": self.total,
            "active_work": self.active_work,
            "blocked_work": self.blocked_work,
            "work": list(self.work_ids),
            "note": self.note,
        }


@dataclass
class Roadmap:
    directions: list = field(default_factory=list)

    def horizon(self, h: str) -> list:
        return [d for d in self.directions if d.horizon == h]

    def as_dict(self) -> dict:
        return {h: [d.as_dict() for d in self.horizon(h)] for h in (*HORIZONS, SHIPPED)}


def closed_ids(plan, history=None) -> set:
    """id закрытых работ: история + любые закрытые в самом плане (страховка).

    Активный план держит только `todo`/`in_progress`, но полагаться на это молча нельзя: если
    закрытая работа осталась в плане, «зависимость закрыта» не должна тихо превратиться в «открыта».
    """
    out = {w.get("id") for w in (history or []) if w.get("id")}
    for w in _plan.items(plan):
        if w.get("status") in _plan.CLOSED_DECLARABLE and w.get("id"):
            out.add(w["id"])
    return out


def _work_state(work: dict, closed: set) -> str:
    """Состояние работы для расчёта горизонта: in_progress | ready | blocked | closed."""
    st = work.get("status")
    if st == "in_progress":
        return "in_progress"
    if st in _plan.CLOSED_DECLARABLE:
        return "closed"
    deps = work.get("depends_on") or []
    return "ready" if all(d in closed for d in deps) else "blocked"


def _direction(goal: dict, works: list, closed: set) -> Direction:
    outc = goal.get("outcome") or {}
    outcomes = [Outcome(name=k, reached=bool(v)) for k, v in outc.items()]
    total = len(outcomes)
    reached = sum(1 for o in outcomes if o.reached)
    states = [_work_state(w, closed) for w in works]
    active = sum(1 for s in states if s in ("in_progress", "ready"))
    blocked = sum(1 for s in states if s == "blocked")

    note = ""
    if total and reached == total:
        horizon = SHIPPED
    elif active:
        horizon = NOW
    elif blocked:
        horizon = NEXT
        note = "работа под направление есть, но вся ждёт закрытия зависимостей"
    elif works:
        # Работы есть, но все закрыты, а исходы достигнуты не все — направление между «сделано» и
        # «в работе»: результат ещё не подтверждён исходами, держим в Now, чтобы не потерять.
        horizon = NOW
        note = "работы закрыты, но не все исходы достигнуты — результат ещё не подтверждён"
    elif reached:
        horizon = NEXT
        note = "часть исходов достигнута, но под остальное нет заведённой работы"
    else:
        horizon = LATER
        note = "направление ещё не декомпозировано в работу"

    return Direction(
        goal_id=goal["id"],
        title=str(goal.get("title") or goal["id"]),
        horizon=horizon,
        outcomes=outcomes,
        reached=reached,
        total=total,
        active_work=active,
        blocked_work=blocked,
        work_ids=[w.get("id") for w in works if w.get("id")],
        note=note,
    )


def build(plan, history=None) -> Roadmap:
    """Собрать roadmap из плана. Чистая функция: тестируется на фикстуре, не на живом плане.

    ПОЧЕМУ ФИКСТУРА, А НЕ ЖИВОЙ ПЛАН (урок 20.08.2026). Проверка, зависящая от состояния живого
    плана, тихо перестаёт проверять: план меняется — тест зеленеет на другом входе. Поэтому вся
    логика ведения — здесь, на входном dict, а `build` живого плана читает CLI отдельно.
    """
    goals = _plan.goals(plan)
    single = goals[0]["id"] if len(goals) == 1 else None
    works_by_goal: dict = {g["id"]: [] for g in goals}
    for w in _plan.items(plan):
        g = w.get("goal") or single
        if g in works_by_goal:
            works_by_goal[g].append(w)

    closed = closed_ids(plan, history)
    directions = [_direction(g, works_by_goal.get(g["id"], []), closed) for g in goals]
    return Roadmap(directions=directions)


def deviations(roadmap: Roadmap, authored) -> list:
    """Отклонение авторского ROADMAP.md от выведенного состояния. -> список строк.

    `authored` — результат `roadmap.parse` (или None, если файла нет). Возвращаем ТОЛЬКО класс
    расхождений по ГОРИЗОНТУ: «в roadmap стоит не там, где по факту». Класс «цель без работы» и
    «работа без направления» уже ловит `roadmap.check` — второй правды не заводим.
    """
    if authored is None:
        return []
    # Куда авторский файл поместил каждую цель (по backtick-ссылке под заголовком горизонта).
    authored_rank: dict = {}
    for key, block in authored.items():
        rank = _AUTHORED_TO_RANK.get(key)
        if rank is None:
            continue
        for gid in block.get("goals") or []:
            # Первое (самое раннее) упоминание задаёт горизонт: цель не должна стоять в двух.
            authored_rank.setdefault(gid, rank)

    out = []
    for d in roadmap.directions:
        a_rank = authored_rank.get(d.goal_id)
        d_rank = _RANK[d.horizon]
        if d.horizon == SHIPPED:
            if a_rank is not None and a_rank >= 0:
                out.append(
                    f"«{d.goal_id}»: все результаты уже достигнуты "
                    f"({d.reached} из {d.total}), но в roadmap оно всё ещё показано "
                    f"как незавершённое"
                )
            continue
        if a_rank is None:
            # Цель не размещена в горизонтах now/next roadmap.py — уже покрыто roadmap.check как
            # «работа без направления»; здесь не дублируем.
            continue
        if d_rank < a_rank:
            out.append(
                f"«{d.goal_id}»: по плану это направление {HORIZON_HUMAN[d.horizon]} "
                f"({humanize_outcomes(d.reached, d.total)}, активных работ {d.active_work}), "
                f"а в roadmap оно показано позже — опережает своё место в плане"
            )
        elif d_rank > a_rank:
            out.append(
                f"«{d.goal_id}»: в roadmap оно обещано раньше, а по плану пока "
                f"{HORIZON_HUMAN[d.horizon]} (активных работ {d.active_work}) — "
                f"обещано раньше, чем под него движется работа"
            )
    return out


_HEAD = {
    NOW: ("## Сейчас", "какие направления главные — под ними идёт работа"),
    NEXT: ("## Следующий результат", "что должно измениться дальше — работа ждёт или не заведена"),
    LATER: ("## Дальше", "направления, ещё не взятые в работу"),
}


def render_markdown(roadmap: Roadmap) -> str:
    """Содержимое ROADMAP.md, собранное из плана. Направление = `id` цели + строка исходов.

    Формат совместим с `roadmap.parse`: заголовки горизонтов и `- \\`goal-id\\`` — те же, что читает
    авторский валидатор, поэтому сгенерированный файл он принимает без особого случая.
    """
    lines = [
        "# ROADMAP",
        "",
        "<!-- Собрано `roadmap_manager` из planning/plan.yaml. Направления и их горизонты",
        "     ВЫВОДЯТСЯ из целей и исходов плана; правьте план, а не этот файл. -->",
        "",
    ]
    for h in HORIZONS:
        head, hint = _HEAD[h]
        lines.append(head)
        lines.append(f"<!-- {hint} -->")
        block = roadmap.horizon(h)
        if not block:
            lines.append("- _(пусто)_")
        for d in block:
            lines.append(f"- `{d.goal_id}` — {d.title} · исходы {d.reached}/{d.total}")
        lines.append("")
    shipped = roadmap.horizon(SHIPPED)
    if shipped:
        lines.append("## Достигнуто")
        lines.append("<!-- все исходы направления достигнуты — на горизонтах не показывается -->")
        for d in shipped:
            lines.append(f"- `{d.goal_id}` — {d.title} · исходы {d.reached}/{d.total}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def check(child_root, plan=None):
    """Свести выведенный roadmap с авторским ROADMAP.md. -> {roadmap, deviations, authored_present}."""
    root = Path(child_root)
    if plan is None:
        plan = _plan.load(root)
    if plan is None:
        return {"errors": ["нет planning/plan.yaml — roadmap выводить не из чего"],
                "deviations": [], "roadmap": {}, "authored_present": False}
    history = _plan.load_history(root)
    rm = build(plan, history)
    authored = _authored.load(root)
    devs = deviations(rm, authored)
    return {
        "errors": [],
        "deviations": devs,
        "authored_present": authored is not None,
        "roadmap": rm.as_dict(),
    }


def _print_build(rm: Roadmap) -> None:
    labels = {NOW: "СЕЙЧАС", NEXT: "СЛЕДУЮЩИЙ", LATER: "ДАЛЬШЕ", SHIPPED: "ДОСТИГНУТО"}
    for h in (*HORIZONS, SHIPPED):
        block = rm.horizon(h)
        if not block and h == SHIPPED:
            continue
        print(f"{labels[h]}:")
        if not block:
            print("  (пусто)")
        for d in block:
            tail = f" — {d.note}" if d.note else ""
            print(f"  • {d.goal_id}: {humanize_outcomes(d.reached, d.total)}; "
                  f"работа: в деле {d.active_work}, ждёт очереди {d.blocked_work}{tail}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="roadmap_manager.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("build", "render", "check"):
        p = sub.add_parser(name)
        p.add_argument("repo")
        p.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])
    root = Path(ns.repo)

    try:
        plan = _plan.load(root)
    except _plan.PlanCorrupt as e:
        print(f"  ✗ {e}")
        return 2
    if plan is None:
        print("  ✗ нет planning/plan.yaml — roadmap выводить не из чего")
        return 2

    if ns.cmd == "render":
        print(render_markdown(build(plan, _plan.load_history(root))), end="")
        return 0

    if ns.cmd == "build":
        rm = build(plan, _plan.load_history(root))
        if ns.json:
            print(json.dumps(rm.as_dict(), ensure_ascii=False, indent=2))
        else:
            _print_build(rm)
        return 0

    # check
    rep = check(root, plan)
    if ns.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 1 if rep["deviations"] else 0
    if not rep["authored_present"]:
        print("  ⚠ авторского ROADMAP.md нет — сверять нечего; соберите его `render`")
        return 0
    for d in rep["deviations"]:
        print(f"  ✗ {d}")
    print(f"ROADMAP: отклонений {len(rep['deviations'])}")
    return 1 if rep["deviations"] else 0


if __name__ == "__main__":
    sys.exit(main())
