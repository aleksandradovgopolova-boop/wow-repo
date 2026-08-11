#!/usr/bin/env python3
"""Delivery plan репозитория: `planning/plan.yaml` — что нужно сделать и в каком порядке (v3.35.0).

Уровень МЕЖДУ направлением и прогоном, которого не было. Прежде WorkItem рождался из фразы
пользователя (`_wid_for`), поэтому вопрос «что брать следующим» не имел входных данных:
`atomic_planner` разбирал задачу, которая УЖЕ выбрана человеком, а `active_work` знал только то,
что идёт сейчас. Здесь объявлена работа целиком — с зависимостями, ролями и областями записи.

ПОЧЕМУ НЕ «work-graph». В ките уже есть `work-graph.yaml` (`validate_work_graph.py`): разбор ОДНОЙ
задачи на параллельные пакеты + ParallelSafetyDecision + IntegrationPlan. Это уровень RunPlan.
Двух сущностей с одним именем в репозитории быть не должно, поэтому продуктовый уровень зовётся
delivery-plan. Элемент плана при этом — тот же **WorkItem**, что `features/<id>/workitem.yaml`:
совпадение id даёт настоящую связь уровней, а не два несвязанных списка.

СТАТУС НЕ ОБЪЯВЛЯЕТСЯ ТАМ, ГДЕ ЕГО МОЖНО ВЫВЕСТИ. Человек объявляет только факты, которых код не
знает: `todo`, `in_progress`, `done`, `dropped`. `ready`/`blocked`/`waiting` — ВЫВОД из графа
зависимостей, статуса WorkItem'а (гейты) и реестра активных работ. Приоритет источников:
факт из WorkItem/гейтов > объявленный факт человека > вывод из графа. Объявленный `ready` при
незакрытой зависимости не ломает файл, но становится ВИДИМЫМ расхождением — ровно так же, как
`status_declared` расходится с выведенным в `lifecycle/workitem.py`.

РОЛЬ, А НЕ ИСПОЛНИТЕЛЬ. План называет `owner_role`; какой runtime ей соответствует сейчас, решает
роутер в момент запуска. Поэтому поля `runtime`/`model`/`provider`/`assignee` в плане ЗАПРЕЩЕНЫ
проверкой: с ними смена Claude Code на Codex переписывала бы план продукта.

Использование:
  plan.py validate <repo> [--json]      # структура + семантика + расхождения
  plan.py resolve  <repo> [--json]      # выведенные статусы по элементам
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

from ai_ops_kit.planning import contours as _contours

PLAN_REL = "planning/plan.yaml"
KIND = "delivery-plan"

DECLARABLE = ("todo", "in_progress", "done", "dropped")
DERIVED = ("ready", "blocked", "waiting")
VALUE = ("high", "medium", "low")

# Поля, называющие КОНКРЕТНОГО исполнителя. Запрещены не слова, а ПОЛЯ: «OpenAI» в заголовке
# работы — законная часть продукта, а `runtime: claude-code` в плане — привязка плана к вендору.
FORBIDDEN_ITEM_KEYS = ("runtime", "model", "provider", "executor", "assignee", "agent")

_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


class PlanCorrupt(Exception):
    """План недостоверен. Пустой план означал бы «работы нет» -> `next` ответил бы «всё сделано»."""


def plan_path(child_root) -> Path:
    return Path(child_root) / PLAN_REL


def load(child_root, path=None):
    """План репозитория. -> dict или None, если плана нет. FAIL-CLOSED на битом файле.

    Отсутствие плана и битый план — РАЗНЫЕ ответы. Первое — «контур не заполнен» (законное
    состояние молодого репозитория, `next` скажет об этом словами). Второе — исключение: молча
    вернуть пустоту значило бы ответить «работы нет» на неразобранный файл.
    """
    p = Path(path) if path else plan_path(child_root)
    if not p.is_file():
        return None
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise PlanCorrupt(f"{p}: не разбирается ({e})") from e
    if data is None:
        raise PlanCorrupt(f"{p}: пустой файл — «работы нет» и «файл не заполнен» это разные ответы")
    if not isinstance(data, dict):
        raise PlanCorrupt(f"{p}: ожидался mapping, получен {type(data).__name__}")
    return data


def items(plan) -> list:
    return [w for w in (plan or {}).get("work") or [] if isinstance(w, dict)]


def goals(plan) -> list:
    """Цели в объявленном порядке. Порядок — это приоритет (ranking его читает)."""
    gs = (plan or {}).get("goals") or []
    return [g for g in gs if isinstance(g, dict) and g.get("id")]


def goal_priority(plan) -> dict:
    return {g["id"]: i for i, g in enumerate(goals(plan))}


def _cycles(by_id: dict) -> list:
    """Циклы в depends_on (Кан + разбор остатка). -> список id, оставшихся в цикле."""
    indeg = {k: 0 for k in by_id}
    outs = {k: [] for k in by_id}
    for wid, w in by_id.items():
        for d in w.get("depends_on") or []:
            if d in by_id:
                indeg[wid] += 1
                outs[d].append(wid)
    queue = [k for k, v in indeg.items() if v == 0]
    seen = 0
    while queue:
        n = queue.pop()
        seen += 1
        for m in outs[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    return sorted(k for k, v in indeg.items() if v > 0) if seen != len(by_id) else []


def validate(plan, model=None):
    """Структура + семантика плана. -> {"errors": [...], "warnings": [...]}.

    errors   — план недостоверен, по нему нельзя считать next work;
    warnings — план работоспособен, но говорит то, что обязан решать граф (расхождение), либо
               недоговаривает (нет `write_scope` -> параллельность недоказуема).
    """
    model = model or _contours.load_model()
    errors, warns = [], []
    if (plan or {}).get("kind") != KIND:
        errors.append(f"kind должен быть '{KIND}', получен '{(plan or {}).get('kind')}'")
    if not isinstance((plan or {}).get("schema_version"), int):
        errors.append("нет schema_version (int)")

    gl = goals(plan)
    if not gl:
        errors.append("нет ни одной цели (goals) — работа без направления не приоритизируется")
    gids = [g["id"] for g in gl]
    dup_g = sorted({g for g in gids if gids.count(g) > 1})
    if dup_g:
        errors.append(f"дубли id целей: {dup_g}")

    ws = items(plan)
    if not ws:
        warns.append("в плане нет ни одного элемента работы — направление объявлено, работа нет")
    ids = [w.get("id") for w in ws]
    dup = sorted({i for i in ids if i and ids.count(i) > 1})
    if dup:
        errors.append(f"дубли id работ: {dup}")

    roles = set((model.get("roles") or {}).keys())
    types = set((model.get("work_types") or {}).keys())
    cids = set(_contours.contour_ids(model))
    by_id = {w["id"]: w for w in ws if w.get("id")}

    for w in ws:
        wid = w.get("id")
        where = f"работа '{wid or '<без id>'}'"
        if not wid:
            errors.append("элемент работы без id"); continue
        if not _ID.match(str(wid)):
            errors.append(f"{where}: id не годится для пути features/<id>/ "
                          f"(разрешено [A-Za-z][A-Za-z0-9_-]*)")
        if not (w.get("title") or "").strip():
            errors.append(f"{where}: нет title")
        if w.get("type") not in types:
            errors.append(f"{where}: type '{w.get('type')}' вне словаря модели ({sorted(types)})")
        if w.get("owner_role") not in roles:
            errors.append(f"{where}: owner_role '{w.get('owner_role')}' вне словаря ролей модели")
        for k in FORBIDDEN_ITEM_KEYS:
            if k in w:
                errors.append(f"{where}: поле '{k}' запрещено — план называет РОЛЬ, исполнителя "
                              f"выбирает роутер в момент запуска (иначе смена runtime "
                              f"переписывает план продукта)")
        st = w.get("status")
        if st in DERIVED:
            warns.append(f"{where}: статус '{st}' ВЫВОДИМЫЙ — объявлять его нельзя, он считается "
                         f"из зависимостей/гейтов; объявляйте {list(DECLARABLE)}")
        elif st not in DECLARABLE:
            errors.append(f"{where}: status '{st}' вне словаря ({list(DECLARABLE)} объявляемые, "
                          f"{list(DERIVED)} выводимые)")
        deps = w.get("depends_on") or []
        if not isinstance(deps, list):
            errors.append(f"{where}: depends_on должен быть списком")
            deps = []
        if wid in deps:
            errors.append(f"{where}: зависит от себя")
        for d in deps:
            if d not in by_id:
                errors.append(f"{where}: depends_on '{d}' не резолвится в id работы плана")
        g = w.get("goal") or (gids[0] if len(gids) == 1 else None)
        if not g:
            errors.append(f"{where}: не указан goal, а целей в плане несколько — "
                          f"приоритет работы не определяется")
        elif g not in gids:
            errors.append(f"{where}: goal '{g}' не резолвится в goals плана")
        if w.get("value") is not None and w.get("value") not in VALUE:
            errors.append(f"{where}: value '{w.get('value')}' вне {list(VALUE)}")
        if not (w.get("write_scope") or []):
            warns.append(f"{where}: нет write_scope — параллельность с другой работой недоказуема "
                         f"(кит не сможет утверждать, что области записи не пересекаются)")
        for cid in (w.get("affects") or {}):
            if cid not in cids:
                errors.append(f"{where}: affects ссылается на контур '{cid}', которого нет в модели")

    cyc = _cycles(by_id)
    if cyc:
        errors.append(f"циклическая зависимость работ: {cyc} — это ошибка плана, не предупреждение")

    return {"errors": errors, "warnings": warns}


# ── Вывод статуса ─────────────────────────────────────────────────────────────────────────────

def _workitem_status(child_root, wid):
    """Статус WorkItem'а из `features/<id>/workitem.yaml`, если работа уже началась. -> str|None.

    Читается ЗАПИСАННЫЙ статус, а не пересчитывается: пересчёт требует гейтов и run-dir, то есть
    полного контура прогона, и в `next` это была бы вторая правда о том же. Пишет статус
    `lifecycle/workitem.py`, он же и владелец вывода.
    """
    p = Path(child_root) / "features" / str(wid) / "workitem.yaml"
    if not p.is_file():
        return None
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    return data.get("status")


def _active_map(child_root):
    """{id: запись} из реестра активных работ. Битый реестр -> исключение пробрасывается наружу:
    координация параллельных сессий на пустой карте небезопасна (инвариант 3.0.12)."""
    from ai_ops_kit.lifecycle import active_work
    p = Path(child_root) / ".ai" / "runtime" / "active-work.yaml"
    if not p.is_file():
        return {}
    data = active_work.load(p)
    out = {}
    for a in data.get("active") or []:
        key = a.get("workitem") or a.get("id")
        if key:
            out[str(key)] = a
    return out


def _scope_conflict(scope, active, exclude_id=None):
    """Пересечение области записи с активной работой. -> список id конфликтующих работ.

    Правило то же, что у ParallelSafetyDecision внутри задачи: сравнение по префиксу до первого
    `*`. Совпадение не случайно — опасность пересечения области записи не зависит от масштаба.
    """
    def _pref(g):
        return (str(g) or "").split("*")[0].rstrip("/")
    mine = [_pref(s) for s in (scope or []) if _pref(s)]
    hits = []
    for wid, a in (active or {}).items():
        if exclude_id and wid == exclude_id:
            continue
        if (a.get("status") or "") == "done":
            continue
        theirs = [_pref(x) for x in (a.get("areas") or []) if _pref(x)]
        for m in mine:
            for t in theirs:
                if m == t or m.startswith(t + "/") or t.startswith(m + "/"):
                    hits.append(wid)
                    break
            else:
                continue
            break
    return sorted(set(hits))


def resolve(plan, child_root, model=None, active=None):
    """Выведенные статусы элементов плана.

    -> {id: {"status": …, "declared": …, "source": …, "reasons": [...], "unblocks": N,
             "blocked_by": [...], "conflicts_with": [...], "drift": str|None}}

    `source` говорит, ОТКУДА статус: `workitem` (гейты — сильнейший факт), `active-work`
    (работа идёт прямо сейчас), `declared` (человек), `derived` (граф). Без этого поля вывод
    неотличим от мнения.
    """
    model = model or _contours.load_model()
    child_root = Path(child_root)
    active = _active_map(child_root) if active is None else active
    ws = items(plan)
    by_id = {w["id"]: w for w in ws if w.get("id")}

    # Транзитивные потомки: сколько работ ждёт каждую (ranking читает это как «снятие ожидания»).
    children = {k: [] for k in by_id}
    for wid, w in by_id.items():
        for d in w.get("depends_on") or []:
            if d in children:
                children[d].append(wid)

    def _downstream(root_id):
        seen, stack = set(), list(children.get(root_id) or [])
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(children.get(n) or [])
        return seen

    out = {}
    for wid in _topo_order(by_id):
        w = by_id[wid]
        declared = w.get("status")
        reasons = []
        wi = _workitem_status(child_root, wid)
        source, status = "declared", declared if declared in DECLARABLE else "todo"

        if wi:
            # Факт из гейтов сильнее объявленного: `done` в файле при провале гейта — не done.
            if wi == "done":
                status, source = "done", "workitem"
                reasons.append("WorkItem закрыт: блокирующих гейтов нет")
            elif wi in ("blocked", "needs_human_decision", "needs_more_evidence"):
                status, source = "in_progress", "workitem"
                reasons.append(f"работа начата, WorkItem в состоянии '{wi}'")
            elif wi == "draft":
                reasons.append("WorkItem создан, прогон ещё не оценивался")
        if wid in active and status not in ("done",):
            status, source = "in_progress", "active-work"
            a = active[wid]
            reasons.append(f"работа идёт сейчас: ветка {a.get('branch') or '?'}, "
                           f"сессия {a.get('session') or '?'}")

        if status in ("done", "dropped", "in_progress"):
            out[wid] = {"status": status, "declared": declared, "source": source,
                        "reasons": reasons, "unblocks": len(_downstream(wid)),
                        "blocked_by": [], "conflicts_with": [],
                        "drift": ("объявлено '%s', а по факту '%s'" % (declared, status)
                                  if declared in DECLARABLE and declared != status else None)}
            continue

        # Ниже — вывод из графа. Всё, что здесь считается, объявлять в файле нельзя.
        blocked_by, waiting_for = [], []
        for d in w.get("depends_on") or []:
            dep = out.get(d)
            if dep is None:
                # Зависимости нет в плане (это ошибка validate) либо она осталась в цикле — обе
                # ситуации блокируют: считать неизвестную зависимость закрытой нельзя.
                blocked_by.append(d)
            elif dep["status"] == "done":
                continue
            elif dep["status"] == "in_progress":
                waiting_for.append(d)
            else:
                blocked_by.append(d)

        hd = w.get("human_decision")
        conflicts = _scope_conflict(w.get("write_scope"), active, exclude_id=wid)

        if blocked_by:
            status, source = "blocked", "derived"
            reasons.append(f"зависимости не закрыты: {', '.join(blocked_by)}")
        elif hd:
            status, source = "blocked", "derived"
            reasons.append(f"нужно решение человека: {hd}")
        elif conflicts:
            status, source = "blocked", "derived"
            reasons.append(f"область записи пересекается с активной работой: {', '.join(conflicts)}")
        elif waiting_for:
            status, source = "waiting", "derived"
            reasons.append(f"ждёт идущую работу: {', '.join(waiting_for)}")
        else:
            status, source = "ready", "derived"
            reasons.append("зависимости закрыты, решений человека не ждёт, конфликтов записи нет")

        out[wid] = {"status": status, "declared": declared, "source": source, "reasons": reasons,
                    "unblocks": len(_downstream(wid)), "blocked_by": blocked_by + waiting_for,
                    "conflicts_with": conflicts,
                    "drift": (f"в файле объявлен выводимый статус '{declared}'"
                              if declared in DERIVED else None)}
    return out


def _topo_order(by_id: dict) -> list:
    """Порядок обхода, в котором зависимость посчитана РАНЬШЕ зависящего.

    Без него вывод зависел бы от порядка строк в файле: элемент, встреченный до своей зависимости,
    получал бы её статус из другого источника, и один и тот же план давал бы разные ответы после
    перестановки строк. Остаток цикла (`validate` считает это ошибкой) добавляется в конец, чтобы
    `resolve` не зависал и не молчал о таких элементах.
    """
    indeg = {k: 0 for k in by_id}
    outs = {k: [] for k in by_id}
    for wid, w in by_id.items():
        for d in w.get("depends_on") or []:
            if d in by_id:
                indeg[wid] += 1
                outs[d].append(wid)
    queue = sorted(k for k, v in indeg.items() if v == 0)
    order = []
    while queue:
        n = queue.pop(0)
        order.append(n)
        for m in sorted(outs[n]):
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    order.extend(sorted(k for k in by_id if k not in order))
    return order


def main(argv=None):
    ap = argparse.ArgumentParser(prog="plan.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("validate", "resolve"):
        s = sub.add_parser(name)
        s.add_argument("repo")
        s.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])
    root = Path(ns.repo)
    try:
        plan = load(root)
    except PlanCorrupt as e:
        print(f"ОШИБКА: {e}")
        return 1
    if plan is None:
        print(f"ПЛАНА НЕТ: ожидался {PLAN_REL} — контур Planning & Execution не заполнен")
        return 1

    if ns.cmd == "validate":
        rep = validate(plan)
        if ns.json:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        else:
            for e in rep["errors"]:
                print(f"  ✗ {e}")
            for w in rep["warnings"]:
                print(f"  ⚠ {w}")
            print(f"PLAN: ошибок {len(rep['errors'])}, предупреждений {len(rep['warnings'])}")
        return 1 if rep["errors"] else 0

    res = resolve(plan, root)
    if ns.json:
        print(json.dumps(res, ensure_ascii=False, indent=2)); return 0
    for wid, v in res.items():
        print(f"{v['status']:12} {wid} · источник {v['source']} · разблокирует {v['unblocks']}")
        for r in v["reasons"]:
            print(f"             {r}")
        if v["drift"]:
            print(f"             ⚠ расхождение: {v['drift']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
