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
знает: `todo`, `in_progress`, `waiting_on_owner` (ждём названного шага владельца), `done`,
`dropped`. `ready`/`blocked`/`waiting` — ВЫВОД из графа
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

from ai_ops_kit.shared.gitio import git
from ai_ops_kit.planning import contours as _contours

# Кластеры плана вынесены в модули-соседи (чистый рефакторинг, поведение байт-в-байт). Имена
# ре-экспортируются, чтобы `delivery_plan.<name>` продолжал работать у прежних импортёров
# (движок, gates, cli, тесты) и у кода этого модуля ниже. Соседи `delivery_plan` не импортируют —
# цикла нет.
from ai_ops_kit.planning.plan_model import (
    PLAN_REL, KIND, HISTORY_REL, HISTORY_KIND, DECLARABLE, DERIVED, VALUE,
    OWNER_WAIT_STATUS, OWNER_WAIT_KEY, ACTIVE_DECLARABLE, CLOSED_DECLARABLE,
    LINK_KEYS, FORBIDDEN_ITEM_KEYS, GOAL_STATUSES, _GOAL_LIVENESS,
    MILESTONE_KEY, MILESTONE_STATUSES,
    FREEZE_DECISION, FREEZE_GOAL, FREEZE_OUTCOME, FREEZE_RELATIONS, FREEZE_LIFT_FIELD,
    is_template, items, goals, current_milestone, goal_priority, goal_is_live,
    freeze_state, _field_evidence_present, _freeze_lift_errors,
    goal_freeze_relation, frozen_work,
    PLACEHOLDER_GOAL_IDS, UNNAMED_GOAL_LABEL, is_placeholder_goal, goal_display_name,
)
from ai_ops_kit.planning.plan_validate import (
    validate, validate_history, _workitem_status_errors, _cycles,
    _engine_id_ok, _engine_id_pattern,
)


class PlanCorrupt(Exception):
    """План недостоверен. Пустой план означал бы «работы нет» -> `next` ответил бы «всё сделано»."""


def plan_rel(child_root) -> str:
    """Где в ЭТОМ репозитории лежит план работ. -> относительный путь.

    По умолчанию `planning/plan.yaml` в корне. Монорепозиторий, где продукт живёт в `apps/web/`,
    так себя описать не мог вовсе: `next` отвечал «плана нет» репозиторию, у которого план есть
    (тир 3 разбора перед квалификацией). Объявляется в
    `.ai-ops.yaml -> product_operating_model.paths.plan`.
    """
    try:
        return _contours.declared_path(child_root, "plan", PLAN_REL)
    except _contours.ConfigInvalid as e:
        # Недостоверное объявление пути — fail-closed: взять дефолт значило бы читать НЕ ТОТ файл и
        # уверенно отвечать по нему.
        raise PlanCorrupt(str(e)) from e


def plan_path(child_root) -> Path:
    return Path(child_root) / plan_rel(child_root)


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


def history_path(child_root) -> Path:
    """Где лежит история завершённой работы. Рядом с планом — тот же корень объявления."""
    return Path(child_root) / HISTORY_REL


def load_history(child_root):
    """Закрытая работа -> список элементов. Файла нет — пустой список (история необязательна).

    ОТКАЗ ЧТЕНИЯ НЕ МОЛЧИТ. Битую историю нельзя считать пустой: `resolve` закрывает зависимости по
    ней, и «истории не прочитали» превратилось бы в «зависимость не закрыта» — то есть в блокировку
    всей работы с невнятной причиной. Поэтому разбор падает `PlanCorrupt`, как и у самого плана.
    """
    p = history_path(child_root)
    if not p.is_file():
        return []
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise PlanCorrupt(f"{HISTORY_REL} не разобран ({type(e).__name__}) — "
                          f"история закрытой работы недостоверна") from e
    if doc.get("kind") != HISTORY_KIND:
        raise PlanCorrupt(f"{HISTORY_REL}: kind должен быть '{HISTORY_KIND}', "
                          f"получен '{doc.get('kind')}'")
    out = doc.get("work") or []
    if not isinstance(out, list):
        raise PlanCorrupt(f"{HISTORY_REL}: work должен быть списком")
    return [w for w in out if isinstance(w, dict)]


# ─── план против того, что говорит git ──────────────────────────────────────────────────────────
#
# ЗАЧЕМ. Цель `plan-as-control-plane` требует, чтобы план и состояние работы показывали ОДНО. До сих
# проверялась только ФОРМА записи, и это было решением: «объявленное состояние чужой системы стареет
# молча». Но git — не чужая система: он лежит рядом, отвечает мгновенно и не требует сети.
#
# ЗАМЕР 20.08.2026, до правки, на плане самого кита: 8 активных работ, ОДНО расхождение, которого не
# видела ни одна проверка — `audit-public-surface-and-guards` объявлена идущей, а её ветка целиком в
# базе (впереди 0). Работа была сделана и слита, план об этом не знал. Ровно класс аудита 19.08
# («тридцать работ объявили закрытыми, настоящими были 23»), только в другую сторону, и заметил его
# снова не механизм.
#
# ВТОРАЯ ПОЛОВИНА ЗАМЕРА: правило «открытый PR не сосуществует с `todo`» существует с 14.08 и
# СРАБОТАТЬ НЕ МОЖЕТ — оно смотрит поле `pr`, а в плане кита это поле встречается НОЛЬ раз (работы
# объявляют `branch`). Проверка без входных данных — то же «объявлено, но не исполняется».
#
# ТРЕТЬЕ СОСТОЯНИЕ НЕ СВОРАЧИВАЕТСЯ ВО ВТОРОЕ. Ошибкой называется только то, что ИЗМЕРЕНО: ссылка на
# ветку найдена и git ответил. Ветка удалена после слияния или просто не выкачана — это «не знаю», и
# оно говорится словами, а не выдаётся за согласованность.


def _trunk_ref(root):
    """Ссылка на СТВОЛ репозитория. -> str или None (не измерено).

    ПОЧЕМУ НЕ `pipeline_git._resolve_base`: он отвечает на другой вопрос — «какая база у ЭТОЙ ветки»,
    и в рабочей копии работы возвращает саму эту ветку. Замер 20.08.2026: из копии прогона сверка
    получалась `ai-ops/w` против `ai-ops/w` и объявляла любую заявку влитой. Здесь нужен ствол, и он
    берётся списком явных кандидатов — без догадок."""
    # Единый вход к git с таймаутом (см. shared/gitio): зависший git не вешает сверку с базой.
    for cand in ("origin/main", "main", "origin/master", "master"):
        if git(root, "rev-parse", "--verify", "--quiet", cand)[0] == 0:
            return cand
    return None


def _branch_state(root, branch, trunk):
    """Что git говорит о ветке работы. -> (состояние, впереди_на).

    Состояния: `in-base` — коммиты ветки уже в стволе; `ahead` — ветка впереди; `absent` — ссылки нет
    ни локально, ни на origin (удалена после слияния либо не выкачана — РАЗЛИЧИТЬ НЕЛЬЗЯ, и мы не
    угадываем)."""
    # Единый вход к git с таймаутом (см. shared/gitio).
    ref = None
    for cand in (branch, f"origin/{branch}"):
        if git(root, "rev-parse", "--verify", "--quiet", cand)[0] == 0:
            ref = cand
            break
    if ref is None:
        return "absent", None
    merged = git(root, "merge-base", "--is-ancestor", ref, trunk)[0] == 0
    if merged:
        return "in-base", 0
    _rc, out, _ = git(root, "rev-list", "--count", f"{trunk}..{ref}")
    try:
        ahead = int(out)
    except ValueError:
        ahead = None
    return "ahead", ahead


def git_disagreements(plan, root):
    """Расхождения между планом и состоянием работ в git. -> {"errors", "warnings", "measured"}.

    ОШИБКА — только измеренное противоречие:
      * ветка работы ЦЕЛИКОМ в стволе, а работа не закрыта — изменения уже в базе;
      * статус `todo`, а ветка впереди ствола — работа начата.
    ПРЕДУПРЕЖДЕНИЕ — неизвестность, названная словами: ветки не нашли, ствол не нашли.
    """
    out = {"errors": [], "warnings": [], "measured": False}
    trunk = _trunk_ref(root)
    if trunk is None:
        out["warnings"].append(
            "состояние работ в git не измерено: ствол (main/master) не найден — это «не знаю», "
            "а не «план согласован»")
        return out
    out["measured"] = True
    for w in items(plan):
        wid, st, br = w.get("id"), (w.get("status") or ""), w.get("branch")
        if not br:
            continue
        state, ahead = _branch_state(root, br, trunk)
        where = f"work[{wid}]"
        if state == "absent":
            out["warnings"].append(
                f"{where}: ветки '{br}' нет ни локально, ни на origin — удалена после слияния или "
                f"не выкачана. Состояние работы НЕ ИЗМЕРЕНО (это не «согласовано»)")
        elif state == "in-base" and st != "done":
            out["errors"].append(
                f"{where}: статус '{st}', а коммиты ветки '{br}' УЖЕ в '{trunk}' (впереди 0) — "
                f"изменения работы в базе, а план держит её открытой. Закройте работу в "
                f"{HISTORY_REL}, назвав результат, либо укажите ветку следующего среза")
        elif state == "ahead" and st == "todo":
            out["errors"].append(
                f"{where}: статус 'todo', а ветка '{br}' впереди '{trunk}' на {ahead} — работа "
                f"начата; поставьте 'in_progress'")
    return out


# ── Вывод статуса ─────────────────────────────────────────────────────────────────────────────

GLOBAL_SCOPE = "*"          # маркер «пишет всюду»: конфликтует с любой областью


def declared_running(plan) -> list:
    """Работы, ОБЪЯВЛЕННЫЕ идущими (факт человека, не вывод из графа). -> список работ плана."""
    return [w for w in items(plan) if (w.get("status") or "") == "in_progress"]


def crosscheck_running(child_root, registry_active, *, registry_exists, plan=None) -> dict:
    """Сверить «что идёт» по ДВУМ источникам: реестр рантайма и план.

    ЗАМЕР 18.08.2026 НА САМОМ КИТЕ. `ai-ops status` печатал «Сейчас ничего не идёт. Работа не
    начата.» при СЕМИ работах в статусе `in_progress` в плане. Проба: одной работе поставили
    `in_progress` с веткой — ответ не изменился ни одним словом. Причина — два источника правды об
    одном вопросе, которые не встречались: `status` читал только реестр рантайма, а `in_progress` в
    плане не видел ни `status`, ни `next`.
    Цена уже заплачена: семь закрытых работ простояли `in_progress` четыре дня (14–18.08), и сказать
    об этом было некому.

    ОТСУТСТВИЕ РЕЕСТРА — НЕ «РАБОТЫ НЕТ». На соседней ветке того же кода кит рассуждает правильно:
    для ИСПОРЧЕННОГО реестра ответ становится `blocked` («битый реестр — не „работы нет“»). Для
    ОТСУТСТВУЮЩЕГО тот же вывод не был сделан, и «не знаю» выдавалось за «нет» — форма ложного
    green в самом частом вопросе управления.

    Третьего места, где живёт «что идёт», НЕ ЗАВОДИМ: здесь только сверка двух существующих.
    Расхождение НАЗЫВАЕТСЯ, а не сглаживается: список работ, объявленных идущими и не подтверждённых
    ни одной заявкой, — это либо брошенная работа, либо закрытая и не закрытая в плане.
    """
    plan = plan if plan is not None else load(child_root)
    declared = declared_running(plan)
    ids_in_registry = {str(a.get("workitem") or a.get("id") or "") for a in (registry_active or [])}
    only_in_plan = [w for w in declared if str(w.get("id")) not in ids_in_registry]
    return {
        "plan_exists": plan is not None,
        "registry_exists": bool(registry_exists),
        "declared": [{"id": w.get("id"), "title": w.get("title")} for w in declared],
        "only_in_plan": [{"id": w.get("id"), "title": w.get("title")} for w in only_in_plan],
        "registry_count": len(registry_active or []),
    }


def scope_prefix(glob) -> str:
    """Область записи -> нормализованный префикс. Глобальная область -> GLOBAL_SCOPE.

    Нормализация обязательна: сравнение было строковым, и `./src/`, `src/`, `src\\` считались
    РАЗНЫМИ каталогами — три сессии уходили писать один. Отдельно глобальный случай: `**`, `.`, `/`
    и пустой префикс означают «пишет всюду», а прежде отфильтровывались как пустая строка и давали
    «пересечений нет». Этот же fail-closed уже был потерян и починен в
    `engine/parallel_planner.py` (баг v3.6.5) — здесь он повторился в новом коде.
    """
    s = str(glob or "").replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    head = s.split("*")[0].strip("/")
    if not head:
        return GLOBAL_SCOPE
    return head


def scopes_overlap(a: str, b: str) -> bool:
    """Пересекаются ли две нормализованные области записи. Глобальная пересекается со всем."""
    if not a or not b:
        return False
    if a == GLOBAL_SCOPE or b == GLOBAL_SCOPE:
        return True
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _workitem_key(entry: dict) -> str:
    """id работы из записи active-work. Движок пишет `workitem` ПУТЁМ `features/<id>/workitem.yaml`.

    Прежде здесь брали значение как есть, поэтому ключ никогда не совпадал с id элемента плана.
    """
    wi = str(entry.get("workitem") or "").replace("\\", "/").strip()
    if wi:
        parts = [x for x in wi.split("/") if x]
        if len(parts) >= 2 and parts[0] == "features":
            return parts[1]
        if not wi.endswith(".yaml"):
            return wi                              # уже id, а не путь
    return str(entry.get("id") or "")


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
        # Мёртвый процесс работу не держит — иначе `next` прятал бы её от всех, а `status` уже
        # научился такую заявку отпускать (18.08.2026). Две правды об одном тут недопустимы.
        if active_work.holder_is_gone(a):
            continue
        # `workitem` движок пишет ПУТЁМ (`features/<id>/workitem.yaml`), а не id: см.
        # `engine/ai_ops_run.py` -> active_work.register(..., workitem=f"features/{fid}/…").
        # Прежде здесь ждали id, поэтому карта активных работ индексировалась путями и НИКОГДА не
        # совпадала с id элемента плана: вопрос «что делаем прямо сейчас» был всегда пуст, а
        # проверка конфликта записи не срабатывала ни разу. Тесты этого не ловили, потому что
        # фикстуры писали форму, которой движок не производит.
        out[_workitem_key(a)] = a
    out.pop("", None)
    return out


def _scope_conflict(scope, active, exclude_id=None):
    """Пересечение области записи с активной работой. -> список id конфликтующих работ.

    Правило то же, что у ParallelSafetyDecision внутри задачи: сравнение по префиксу до первого
    `*`. Совпадение не случайно — опасность пересечения области записи не зависит от масштаба.
    """
    mine = [scope_prefix(s) for s in (scope or [])]
    if not mine:
        return []
    hits = []
    for wid, a in (active or {}).items():
        if exclude_id and wid == exclude_id:
            continue
        if (a.get("status") or "") == "done":
            continue
        # `affected_areas` — РЕАЛЬНОЕ имя поля (`lifecycle/active_work.py`); `areas` оставлен для
        # записей, сделанных вручную и в старых версиях.
        theirs = [scope_prefix(x) for x in (a.get("affected_areas") or a.get("areas") or [])]
        if any(scopes_overlap(m, o) for m in mine for o in theirs):
            hits.append(wid)
    return sorted(set(hits))


def resolve(plan, child_root, model=None, active=None, closed=None):
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

    # ЗАКРЫТАЯ РАБОТА ПОДАЁТСЯ В ВЫВОД ПЕРВОЙ. Ниже зависимость, которой нет в `out`, считается
    # блокирующей — и это верно («неизвестную зависимость закрытой считать нельзя»). Но после
    # разноса плана на активное и закрытое каждая зависимость от завершённой работы стала бы
    # неизвестной, и весь план оказался бы заблокирован с невнятной причиной. История — это факт
    # закрытия, и она обязана доехать до вывода, иначе разнос ломает `next`.
    if closed is None:
        try:
            closed = load_history(child_root)
        except PlanCorrupt:
            # Битая история НЕ превращается в «зависимостей нет»: пусть блокирует честно, а причину
            # назовёт валидатор истории. Молча пустой список здесь был бы ложным green.
            closed = []
    out = {}
    for w in closed or []:
        cid = w.get("id")
        if cid and cid not in by_id:
            out[cid] = {"status": w.get("status") or "done", "declared": w.get("status"),
                        "source": "history", "reasons": [f"закрыта в {HISTORY_REL}"],
                        "unblocks": 0, "blocked_by": [], "conflicts_with": [], "drift": None}
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

        # WAITING-ON-OWNER — ОБЪЯВЛЕННОЕ, НЕ ВЫВОДИМОЕ. Ждём названного шага владельца, а не
        # снятия графовой блокировки, поэтому статус не пересчитывается из зависимостей: иначе
        # `waiting_on_owner` затёрся бы на `ready`/`waiting`/`blocked` и снова читался бы как
        # «работа идёт/встала». Причину показываем ту, что назвал человек.
        if status == OWNER_WAIT_STATUS and source == "declared":
            reasons.append(f"механизм готов, ждёт названного действия владельца: "
                           f"{str(w.get(OWNER_WAIT_KEY) or '').strip() or '?'}")

        if status in ("done", "dropped", "in_progress", OWNER_WAIT_STATUS):
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
        print(f"ПЛАНА НЕТ: ожидался {plan_rel(root)} — контур Planning & Execution не заполнен")
        return 1

    if ns.cmd == "validate":
        # История проверяется ВМЕСТЕ с планом: они одно состояние работы, разнесённое по двум
        # файлам. Отдельная команда означала бы, что одну половину можно не проверить.
        try:
            closed = load_history(root)
            hrep = validate_history(closed, plan)
        except PlanCorrupt as e:
            closed, hrep = [], {"errors": [str(e)], "warnings": []}
        rep = validate(plan, closed=closed, root=root)
        # ГИТ — ЧАСТЬ ТОГО ЖЕ ОТВЕТА, а не отдельная команда: план и состояние работы это одно
        # состояние, разнесённое по файлу и по ветке. Отдельная команда означала бы, что одну
        # половину можно не проверить, — тем же соображением история проверяется здесь же.
        grep_ = git_disagreements(plan, root)
        rep = {"errors": rep["errors"] + hrep["errors"] + grep_["errors"],
               "warnings": rep["warnings"] + hrep["warnings"] + grep_["warnings"]}
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
