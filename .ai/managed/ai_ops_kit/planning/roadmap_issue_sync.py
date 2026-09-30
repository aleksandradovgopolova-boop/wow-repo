#!/usr/bin/env python3
"""Сверка issue-трекера с роадмапом: эпик на направление + подзадача на каждую открытую работу.

Правило владельца: каждое открытое направление роадмапа ВСЕГДА имеет issue — держит механизм, а не
память. Гибрид (решение владельца): направление под «Сейчас»/«Следующий» = эпик; незакрытая работа
(`todo`/`in_progress`) = подзадача, связанная task-list'ом.

Порты и адаптеры: `sync()` — чистая оркестрация (желаемое из `roadmap_manager.check` + работ плана,
существующее через инъектируемый `client`) → ПЛАН (создать/обновить/закрыть); мутации только при
`apply=True`. Сеть (`gh`) — в адаптере CLI, ядро тестируется на фейковом клиенте. Идемпотентно: ключ
вшит в тело `<!-- roadmap-sync: KEY -->`; issue, заведённые вручную, усыновляются по заголовку/телу.

Формат заголовка и тела — единый формат issue кита (`issue_format`: `<slug>: <исход>` + четыре
секции). Старые заголовки `[roadmap:<goal>] …` / `[<goal>] …` по-прежнему узнаются `parse_key`, а
заголовки уже заведённых issue сверка не переписывает — новый формат получают только новые issue.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from ai_ops_kit.planning import issue_format

LABEL = "roadmap-direction"
_KEY_RE = re.compile(r"<!--\s*roadmap-sync:\s*([a-z0-9:_-]+)\s*-->")
_LEGACY_EPIC_RE = re.compile(r"\[roadmap:([A-Za-z0-9_-]+)\]")
_LEGACY_WORK_TITLE_RE = re.compile(r"^\[([A-Za-z0-9_-]+)\]")
_LEGACY_WORK_RE = re.compile(r"Работа\s+`([^`]+)`")

_HZ_HUMAN = {"now": "Сейчас", "next": "Следующий результат"}
# Человекочитаемые заголовки направлений; неизвестному — сам slug.
DIRECTION_TITLES = {
    "checks-that-run": "Каждая объявленная проверка реально исполняется",
    "green-means-checked": "«Зелёное» не врёт под нагрузкой",
    "team-works-in-parallel": "Команда работает над продуктом одновременно, без конфликтов",
    "product-decision-loop": "Обоснованное продуктовое решение: baseline/target/guardrails",
    "storybook-as-visual-contract": "UI-задача проектируется и проверяется через Storybook",
    "owner-speaks-product-not-pipeline": "Владелец говорит обычным языком, кит ведёт остальное",
    "layering-ring-is-a-dag": "Кольцо capability-слоёв становится DAG (без циклов)",
    "kit-release-strategy": "Кит собирает релизы осознанно, владелец выбирает стратегию обновления",
    "nightly-product-review": "Ночной продуктовый обзор",
    "uiux-standard-as-product": "UI/UX-стандарт как продукт",
}
_OPEN_WORK = {"todo", "in_progress"}
# Работа, ждущая ЖИВОГО прогона владельца: не назначается writer'у (issue-подзадачу под неё не
# заводим), но и «работ нет» про направление с такой работой — неправда. Показываем её в теле эпика
# отдельной строкой, чтобы эпик не молчал о том, что под ним есть незакрытая работа-замер.
_WAITING_WORK = {"waiting_on_owner"}


def _one_line(text: str, limit: int = 160) -> str:
    """Свернуть многострочный `waiting_on` в одну обрезанную строку для тела issue."""
    t = " ".join(str(text or "").split())
    return (t[: limit - 1] + "…") if len(t) > limit else t


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    state: str  # "open" | "closed"


class Client(Protocol):
    """Порт к трекеру. Реальная реализация зовёт `gh`; тестовая — держит issue в памяти."""

    def list(self) -> list[Issue]: ...
    def create(self, title: str, body: str, labels: list[str]) -> int: ...
    def edit(self, number: int, body: str) -> None: ...
    def close(self, number: int) -> None: ...


@dataclass
class Action:
    kind: str            # "create" | "update" | "close"
    key: str             # dir:<goal> | work:<goal>:<workid>
    title: str
    number: int | None = None   # для update/close; для create — присвоенный после apply


@dataclass
class SyncPlan:
    actions: list[Action] = field(default_factory=list)

    @property
    def creates(self) -> list[Action]:
        return [a for a in self.actions if a.kind == "create"]

    @property
    def updates(self) -> list[Action]:
        return [a for a in self.actions if a.kind == "update"]

    @property
    def closes(self) -> list[Action]:
        return [a for a in self.actions if a.kind == "close"]

    @property
    def in_sync(self) -> bool:
        return not self.actions


def parse_key(issue: Issue) -> str | None:
    """Ключ roadmap-sync из тела: явный маркер, иначе — эвристика для issue, заведённых вручную."""
    m = _KEY_RE.search(issue.body or "")
    if m:
        return m.group(1)
    me = _LEGACY_EPIC_RE.search(issue.title or "")
    if me and "roadmap:" in (issue.title or ""):
        return f"dir:{me.group(1)}"
    mw = _LEGACY_WORK_RE.search(issue.body or "")
    if mw:
        mt = _LEGACY_WORK_TITLE_RE.search(issue.title or "")
        goal = mt.group(1) if mt else "?"
        return f"work:{goal}:{mw.group(1)}"
    return None


def _epic_key(goal: str) -> str:
    return f"dir:{goal}"


def _work_key(goal: str, work_id: str) -> str:
    return f"work:{goal}:{work_id}"


# Исход в заголовке эпика, когда у направления нет человекочитаемого названия (только slug):
# статичный, а не со счётчиком — заголовки заведённых issue сверка не переписывает.
_EPIC_FALLBACK_OUTCOME = "направление роадмапа достигнуто целиком"


def _direction_title(goal: str, title: str | None = None) -> str | None:
    """Человеческое название направления: словарь кита, иначе название цели плана (если это не slug)."""
    if goal in DIRECTION_TITLES:
        return DIRECTION_TITLES[goal]
    t = " ".join(str(title or "").split())
    return t if t and t != goal else None


def epic_title(goal: str, title: str | None = None) -> str:
    """`<goal>: <исход направления>` — единый формат issue (`issue_format`).

    Новый формат действует для НОВЫХ issue: заголовки заведённых сверка не трогает (ключ — маркер в
    теле), поэтому массового переименования живых issue нет."""
    return issue_format.render_title(goal, _direction_title(goal, title) or _EPIC_FALLBACK_OUTCOME)


def work_title(goal: str, work: dict) -> str:
    """`<id работы>: <название работы>` — slug = id работы в `planning/plan.yaml`, по нему её и ищут."""
    title = str(work.get("title") or work["id"])
    return issue_format.render_title(str(work["id"]), title)


def epic_body(goal: str, horizon: str, reached: int, total: int,
              missing: list[str], sub_numbers: list[int | None],
              waiting: list[dict] | None = None, title: str | None = None) -> str:
    waiting = waiting or []
    hz = _HZ_HUMAN.get(horizon, horizon)
    human = _direction_title(goal, title)
    what = (f"Довести направление роадмапа **`{goal}`** до всех исходов (горизонт: {hz}). "
            f"Готово {reached} из {total} исходов.")
    why = (f"Направление обещано в роадмапе на горизонте «{hz}»"
           + (f" — «{human}»" if human else "")
           + ". Пока его исходы не достигнуты, это обещание не выполнено.")
    if missing:
        verifiable = ("Все исходы направления достигнуты — тогда направление уходит из горизонтов, "
                      "и сверка закрывает эпик сама. Ещё не достигнуто:\n"
                      + "\n".join(f"- [ ] `{m}`" for m in missing))
    else:
        verifiable = ("Все исходы направления достигнуты — эпик закроется при следующей сверке.")
    bounds = ("Источник — `ROADMAP.md` и цель `" + goal + "` в `planning/plan.yaml`: там зависимости, "
              "write_scope и исходы работ. Эпик не заменяет план: его ведёт команда "
              "`roadmap sync-issues`, правки руками она перезапишет.")
    extra: list[str] = []
    if waiting:
        # Работа-замер ждёт живого прогона владельца — не writer'а. Показываем отдельно от подзадач,
        # чтобы не выдать её за назначаемую работу и не молчать о ней («работ нет» было бы неправдой).
        extra.append("### Ждёт owner-прогон")
        for w in waiting:
            reason = _one_line(w.get("waiting_on") or "")
            extra.append(f"- `{w['id']}` — ждёт прогона владельца" + (f": {reason}" if reason else ""))
        extra.append("")
    if sub_numbers:
        extra.append("### Подзадачи (работы плана)")
        for n in sub_numbers:
            extra.append(f"- [ ] #{n}" if n is not None else "- [ ] _(будет заведена)_")
        extra.append("")
    elif not waiting:
        extra.append("_Заведённых работ под направлением сейчас нет — открыт только исход выше._")
    return issue_format.render_body(what, why, verifiable, bounds,
                                    marker=f"<!-- roadmap-sync: {_epic_key(goal)} -->",
                                    extra="\n".join(extra))


def work_body(goal: str, work: dict) -> str:
    # Ссылка на эпик — по slug направления, не по номеру: номер эпика может появиться позже
    # (эпик заводится вторым), а тело работы должно быть стабильным ради идемпотентности. Связь
    # «эпик → подзадача» несёт task-list в теле эпика — именно его GitHub рисует как sub-issue.
    wid = work["id"]
    title = _one_line(work.get("title") or wid, limit=300)
    what = (f"Работа `{wid}` под направлением-эпиком `{goal}`: {title}. "
            f"Статус в плане: **{work.get('status')}**.")
    why = (_one_line(work.get("rationale") or "", limit=600)
           or f"Работа ведёт направление `{goal}` к его исходам.")
    deps = [str(d) for d in (work.get("depends_on") or [])]
    verifiable = ("Работа закрыта в `planning/plan.yaml`; issue закрывается вместе с ней при "
                  "следующей сверке." + (f" Начинается после: {', '.join(f'`{d}`' for d in deps)}."
                                         if deps else ""))
    bounds = (f"Трекер работы — `planning/plan.yaml` (id `{wid}`): там зависимости, write_scope и "
              f"исходы. Подзадача направления; поддерживается командой `roadmap sync-issues`.")
    return issue_format.render_body(what, why, verifiable, bounds,
                                    marker=f"<!-- roadmap-sync: {_work_key(goal, wid)} -->")


def _canonical(body: str) -> str:
    return (body or "").strip()


def _open_works_by_goal(plan_items: list[dict]) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = {}
    for w in plan_items:
        if w.get("status") in _OPEN_WORK and w.get("goal"):
            by.setdefault(w["goal"], []).append(w)
    return by


def _waiting_works_by_goal(plan_items: list[dict]) -> dict[str, list[dict]]:
    """Работы-замеры, ждущие живого прогона владельца (`waiting_on_owner`), по цели."""
    by: dict[str, list[dict]] = {}
    for w in plan_items:
        if w.get("status") in _WAITING_WORK and w.get("goal"):
            by.setdefault(w["goal"], []).append(w)
    return by


def sync(report: dict, plan_items: list[dict], client: Client, apply: bool = False) -> SyncPlan:
    """Свести issue-трекер с роадмапом. Возвращает план; при apply=True выполняет его.

    `report` — вывод `roadmap_manager.check` (ключи roadmap.now/next, errors, authored_present).
    `plan_items` — работы плана (`delivery_plan.items`). `client` — порт к трекеру.
    """
    roadmap = report.get("roadmap") or {}
    directions = []  # (goal, horizon, reached, total, missing)
    dir_titles: dict[str, str | None] = {}
    for hz in ("now", "next"):
        for g in roadmap.get(hz, []) or []:
            missing = [o["name"] for o in g.get("outcomes", []) if not o.get("reached")]
            directions.append((g["goal"], hz, g.get("reached", 0), g.get("total", 0), missing))
            dir_titles[g["goal"]] = g.get("title")
    works_by = _open_works_by_goal(plan_items)
    waiting_by = _waiting_works_by_goal(plan_items)

    existing = client.list()
    by_key: dict[str, Issue] = {}
    for iss in existing:
        k = parse_key(iss)
        if k is not None:
            # Открытая перекрывает закрытую при коллизии ключа: сверяем с живой.
            if k not in by_key or iss.state == "open":
                by_key[k] = iss

    plan = SyncPlan()
    desired_keys: set[str] = set()

    # 1) Подзадачи-работы: создаём/усыновляем, чтобы знать их номера для тел эпиков.
    work_number: dict[str, int | None] = {}
    for goal, _hz, _r, _t, _m in directions:
        for w in works_by.get(goal, []):
            key = _work_key(goal, w["id"])
            desired_keys.add(key)
            title = work_title(goal, w)
            cur = by_key.get(key)
            if cur is None:
                body = work_body(goal, w)
                if apply:
                    num = client.create(title, body, [LABEL])
                else:
                    num = None
                work_number[key] = num
                plan.actions.append(Action("create", key, title, num))
            else:
                work_number[key] = cur.number
                body = work_body(goal, w)
                if cur.state == "closed" or _canonical(cur.body) != _canonical(body):
                    if apply:
                        client.edit(cur.number, body)
                    plan.actions.append(Action("update", key, title, cur.number))

    # 2) Эпики направлений: тело ссылается на номера подзадач.
    for goal, hz, reached, total, missing in directions:
        key = _epic_key(goal)
        desired_keys.add(key)
        subs = [work_number.get(_work_key(goal, w["id"])) for w in works_by.get(goal, [])]
        title = epic_title(goal, dir_titles.get(goal))
        body = epic_body(goal, hz, reached, total, missing, subs, waiting_by.get(goal, []),
                         title=dir_titles.get(goal))
        cur = by_key.get(key)
        if cur is None:
            num = client.create(title, body, [LABEL]) if apply else None
            plan.actions.append(Action("create", key, title, num))
        elif cur.state == "closed" or _canonical(cur.body) != _canonical(body):
            if apply:
                client.edit(cur.number, body)
            plan.actions.append(Action("update", key, title, cur.number))

    # 3) Устаревшие: наши issue (с ключом roadmap-sync) вне желаемого — закрыть (цель/работа ушла).
    for key, iss in by_key.items():
        if iss.state == "open" and key not in desired_keys:
            if apply:
                client.close(iss.number)
            plan.actions.append(Action("close", key, iss.title, iss.number))

    return plan
