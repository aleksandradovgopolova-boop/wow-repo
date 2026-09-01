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
HISTORY_REL = "history/plan-history.yaml"
HISTORY_KIND = "delivery-plan-history"

DECLARABLE = ("todo", "in_progress", "done", "dropped")
DERIVED = ("ready", "blocked", "waiting")
VALUE = ("high", "medium", "low")
# АКТИВНЫЙ план содержит только незакрытую работу (`plan-as-control-plane`, 2026-08-14). Закрытая
# уезжает в `history/plan-history.yaml`. Повод — замер: план кита стал одновременно планом, бэклогом,
# журналом расследований и отчётом квалификации, 20 из 25 работ были `done`, и чтобы ответить «что
# идёт сейчас», приходилось читать разбор давно закрытых дефектов. Управляющий файл, в котором
# управление занимает пятую часть, управляющим быть перестаёт.
ACTIVE_DECLARABLE = ("todo", "in_progress")
CLOSED_DECLARABLE = ("done", "dropped")
# Поля-связки: чем работа привязана к реальности. Не обязательны, но их СМЫСЛ проверяется ниже.
LINK_KEYS = ("pr", "branch", "commit", "evidence", "decision", "finding")

# Поля, называющие КОНКРЕТНОГО исполнителя. Запрещены не слова, а ПОЛЯ: «OpenAI» в заголовке
# работы — законная часть продукта, а `runtime: claude-code` в плане — привязка плана к вендору.
FORBIDDEN_ITEM_KEYS = ("runtime", "model", "provider", "executor", "assignee", "agent")

def _engine_id_ok(wid: str) -> bool:
    """Годен ли id для СОЗДАНИЯ работы движком — правило берётся у движка, а не переписывается.

    Дублировать регулярное выражение значило бы завести вторую правду об одном id: ровно из-за
    расхождения двух правил `ARCH-01` проходил валидатор плана и падал в `run` сырым ValueError.
    Если движок недоступен (валидатор запускают процессом в урезанном окружении) — проверяем
    минимум, который заведомо безопасен для пути `features/<id>/`.
    """
    try:
        from ai_ops_kit.engine.run_plan import WORKITEM_ID_RE as _RE
    except ImportError:                                # pragma: no cover — движок обязан быть рядом
        _RE = _FALLBACK_ID
    return bool(_RE.match(wid or "")) and not wid.startswith(".") and ".." not in wid


def _engine_id_pattern() -> str:
    try:
        from ai_ops_kit.engine.run_plan import WORKITEM_ID_RE as _RE
        return _RE.pattern
    except ImportError:                                # pragma: no cover
        return _FALLBACK_ID.pattern


_FALLBACK_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


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


def validate_history(closed, plan=None) -> dict:
    """Контракт истории. -> {"errors": [...], "warnings": [...]}.

    ГЛАВНОЕ ПРАВИЛО: `done` — это не «PR смержен». Закрытая работа обязана назвать РЕЗУЛЬТАТ
    (`result`) и хотя бы одно место, где его можно перепроверить (`pr`/`evidence`/`finding`).
    Иначе история станет списком галочек: merged PR при незакрытом гейте — ровно тот ложный green,
    против которого стоит весь остальной контур.

    ОДНО ИСКЛЮЧЕНИЕ, И ОНО НЕ ОСЛАБЛЯЕТ ПРАВИЛО: запись с `migrated_without_result: true` — работа,
    закрытая ДО появления этого правила и перенесённая сюда миграцией кита. Требовать от неё место
    перепроверки бессмысленно: его не существует, а выдумать — значит соврать в архиве. Такие записи
    дают ПРЕДУПРЕЖДЕНИЕ, а не ошибку.
    Почему исключение не лазейка: пометку ставит только миграция, ошибка «нет result» для такой
    записи ОСТАЁТСЯ (миграция пишет туда честное «результат не записан»), а любое НОВОЕ закрытие
    проверяется как раньше — без пометки. Решение владельца 17.08.2026: переносить с честной
    пометкой, а не блокировать проект и не сочинять результаты (F-030; замер на ИИ-Среде: `next`
    отказывался отвечать и печатал 32 ошибки после обновления, которого владелец не заказывал).
    """
    errors, warns = [], []
    ids = [w.get("id") for w in closed]
    dup = sorted({i for i in ids if i and ids.count(i) > 1})
    if dup:
        errors.append(f"{HISTORY_REL}: дубли id закрытых работ: {dup}")
    plan_ids = {w.get("id") for w in items(plan or {})}
    for w in closed:
        wid = w.get("id")
        where = f"история '{wid or '<без id>'}'"
        if not wid:
            errors.append(f"{HISTORY_REL}: элемент без id"); continue
        if wid in plan_ids:
            errors.append(f"{where}: работа одновременно в активном плане и в истории — "
                          f"два состояния одной работы")
        if w.get("status") not in CLOSED_DECLARABLE:
            errors.append(f"{where}: status '{w.get('status')}' — в истории только "
                          f"{list(CLOSED_DECLARABLE)}")
        if not str(w.get("result") or "").strip():
            errors.append(f"{where}: нет result — «сделано» без названного результата не проверить "
                          f"(merged PR сам по себе результатом не является)")
        if w.get("status") == "done" and not any(w.get(k) for k in ("pr", "commit", "evidence", "finding")):
            if w.get("migrated_without_result"):
                warns.append(f"{where}: перенесено миграцией, места перепроверки нет — так и было "
                             f"на момент закрытия; для новых закрытий требование в силе")
            else:
                errors.append(f"{where}: done без pr/commit/evidence/finding — результат негде "
                              f"перепроверить")
        if not w.get("closed_at"):
            warns.append(f"{where}: нет closed_at — история без даты не читается как история")
    return {"errors": errors, "warnings": warns}


def is_template(plan) -> bool:
    """Это ещё заготовка кита, а не план продукта?

    Установщик кладёт шаблон в репозиторий, и без маркера кит уверенно советовал работу из своего
    примера («Спроектировать pipeline»), рапортовал «работа 1/5» и получал `✓` в doctor — выдавал
    догадку за факт на чужом продукте. Маркер `template: true` снимает человек, когда впишет своё.
    Заодно распознаём незаполненные id-заглушки: файл могли скопировать руками, потеряв маркер.
    """
    if not plan:
        return False
    if plan.get("template") is True:
        return True
    gids = {g.get("id") for g in (plan.get("goals") or []) if isinstance(g, dict)}
    return bool(gids) and gids <= {"goal-id-1", "goal-id-2"}


def items(plan) -> list:
    return [w for w in (plan or {}).get("work") or [] if isinstance(w, dict)]


def goals(plan) -> list:
    """Цели в объявленном порядке. Порядок — это приоритет (ranking его читает)."""
    gs = (plan or {}).get("goals") or []
    return [g for g in gs if isinstance(g, dict) and g.get("id")]


# Живость цели -> насколько её работы вправе идти первыми. Порядок в файле остаётся приоритетом,
# но ТОЛЬКО СРЕДИ ЦЕЛЕЙ ОДНОЙ ЖИВОСТИ.
#
# ЗАМЕР 19.08.2026. Первой целью плана стоит `real-project-qualification` со `status: achieved`, и
# `next` три раза подряд советовал её работу — обходя P1-находки живого прогона, заведённые в тот
# же день. Причина: приоритет считался ИНДЕКСОМ цели в файле, а `status` читался только для показа
# (`where_are_we`). Достигнутая цель — это не «самое важное направление», это направление, которое
# больше никуда не ведёт.
#
# РАБОТА ПОД ДОСТИГНУТОЙ ЦЕЛЬЮ НЕ ИСЧЕЗАЕТ, а опускается: исчезновение было бы тем же молчанием,
# которое запрещено заморозке умений («замороженное не исчезает молча»). Она остаётся кандидатом и
# получает вслух названную причину низкого приоритета.
GOAL_STATUSES = ("active", "paused", "achieved")
_GOAL_LIVENESS = {"active": 0, "paused": 1, "achieved": 2}


def goal_priority(plan) -> dict:
    """Приоритет целей: живые в объявленном порядке, потом приостановленные, потом достигнутые.

    Неизвестный статус считается живым НАМЕРЕННО: молча опустить цель из-за опечатки значило бы
    переставить весь план и никому об этом не сказать. Опечатку ловит `validate` ошибкой — там она
    видна, здесь была бы невидима.
    """
    gs = list(goals(plan))
    order = sorted(range(len(gs)),
                   key=lambda i: (_GOAL_LIVENESS.get(gs[i].get("status") or "active", 0), i))
    return {gs[i]["id"]: rank for rank, i in enumerate(order)}


def goal_is_live(plan, gid) -> bool:
    """Ведёт ли цель куда-то ещё. Достигнутая и приостановленная — нет."""
    for g in goals(plan):
        if g.get("id") == gid:
            return _GOAL_LIVENESS.get(g.get("status") or "active", 0) == 0
    return True                      # цели нет в плане — это ловит validate, не молчим здесь


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
    import subprocess
    for cand in ("origin/main", "main", "origin/master", "master"):
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", cand],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return cand
    return None


def _branch_state(root, branch, trunk):
    """Что git говорит о ветке работы. -> (состояние, впереди_на).

    Состояния: `in-base` — коммиты ветки уже в стволе; `ahead` — ветка впереди; `absent` — ссылки нет
    ни локально, ни на origin (удалена после слияния либо не выкачана — РАЗЛИЧИТЬ НЕЛЬЗЯ, и мы не
    угадываем)."""
    import subprocess
    ref = None
    for cand in (branch, f"origin/{branch}"):
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", cand],
                           capture_output=True, text=True)
        if r.returncode == 0:
            ref = cand
            break
    if ref is None:
        return "absent", None
    merged = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", ref, trunk],
                            capture_output=True, text=True).returncode == 0
    if merged:
        return "in-base", 0
    r = subprocess.run(["git", "-C", str(root), "rev-list", "--count", f"{trunk}..{ref}"],
                       capture_output=True, text=True)
    try:
        ahead = int((r.stdout or "").strip())
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


def validate(plan, model=None, closed=None, root=None):
    """Структура + семантика плана. -> {"errors": [...], "warnings": [...]}.

    errors   — план недостоверен, по нему нельзя считать next work;
    warnings — план работоспособен, но говорит то, что обязан решать граф (расхождение), либо
               недоговаривает (нет `write_scope` -> параллельность недоказуема).

    `closed` — работы из `history/plan-history.yaml`: `depends_on` резолвится и по ним, иначе разнос
    плана на активное и закрытое сделал бы каждую зависимость от завершённой работы «нерезолвимой».
    `root` — корень репозитория: нужен, чтобы проверить, что пути в `evidence`/`finding` существуют.
    """
    model = model or _contours.load_model()
    closed = list(closed or [])
    closed_ids = {w.get("id") for w in closed if w.get("id")}
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
    # ЗАМОРОЗКА УМЕНИЙ ИСПОЛНЯЕТСЯ ПРОВЕРКОЙ, А НЕ ПАМЯТЬЮ (работа `capability-freeze-enforced`).
    # Решение владельца существовало записью с 17.08 и ничем не сверялось: 18.08 кит сам предложил
    # взять работу из замороженной цели. Отношение цели к заморозке — обязательное объявление:
    # необъявленная цель означала бы «правило не про меня», то есть тихий обход.
    _fz = freeze_state(plan)
    for g in (gl if _fz.get("applies") else []):
        rel = g.get("freeze_relation")
        if rel is None:
            errors.append(f"цель '{g['id']}': не объявлено freeze_relation "
                          f"({list(FREEZE_RELATIONS)}) — заморозка умений (решение {FREEZE_DECISION}) "
                          f"проверяется по назначению цели, и необъявленное назначение делает правило "
                          f"необязательным для этой цели")
        elif rel not in FREEZE_RELATIONS:
            errors.append(f"цель '{g['id']}': freeze_relation '{rel}' вне {list(FREEZE_RELATIONS)}")
    # СНЯТИЕ ОБЯЗАНО ССЫЛАТЬСЯ НА СУЩЕСТВУЮЩЕЕ РЕШЕНИЕ. Иначе новое поле стало бы тем самым
    # «разморозить вручную», от которого механизм и защищал: строка `freeze_lifted_by: почему-то`
    # снимала бы правило без следа. Решение должно лежать в `decisions/registry.yaml` — там, где
    # его увидит человек и переживёт сессия.
    _lift = str((_fz or {}).get("lifted_by") or "").strip()
    if _lift and root is not None:
        _reg = Path(root) / "decisions" / "registry.yaml"
        _known = False
        if _reg.is_file():
            try:
                _doc = yaml.safe_load(_reg.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError) as _e:
                errors.append(f"заморозка снята решением '{_lift}', но decisions/registry.yaml "
                              f"не разобран ({type(_e).__name__}) — сослаться не на что")
                _doc = None
            if _doc is not None:
                _items = _doc.get("decisions") or _doc.get("episodes") or _doc.get("registry") or []
                if isinstance(_items, dict):
                    _known = _lift in _items
                elif isinstance(_items, list):
                    _known = any(str((d or {}).get("id")) == _lift for d in _items if isinstance(d, dict))
                if not _known:
                    errors.append(
                        f"заморозка снята решением '{_lift}', которого нет в decisions/registry.yaml — "
                        f"снятие без записанного решения это тихий обход правила, а не решение")
        else:
            errors.append(f"заморозка снята решением '{_lift}', но decisions/registry.yaml нет — "
                          f"сослаться не на что")
    # Статус цели ТЕПЕРЬ ВЛИЯЕТ НА ПРИОРИТЕТ (`goal_priority`), поэтому опечатка в нём молча
    # переставляла бы весь план. Проверяем здесь — единственное место, где она видна человеку.
    for g in gl:
        st = g.get("status")
        if st is not None and st not in GOAL_STATUSES:
            errors.append(f"цель '{g['id']}': status '{st}' вне {list(GOAL_STATUSES)} — "
                          f"от статуса зависит приоритет работ этой цели")

    ws = items(plan)
    if not ws:
        warns.append("в плане нет ни одного элемента работы — направление объявлено, работа нет")
    _frozen = frozen_work(plan)
    _frozen_ids = set(_frozen)
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
        if not _engine_id_ok(str(wid)):
            errors.append(f"{where}: id непригоден для работы — движок требует slug НИЖНЕГО "
                          f"регистра ({_engine_id_pattern()}). Прежде валидатор плана допускал "
                          f"'ARCH-01', а `ai-ops run --feature ARCH-01` падал сырым ValueError: "
                          f"два правила об одном id. Возьмите '{str(wid).lower()}'")
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
        elif st in CLOSED_DECLARABLE:
            errors.append(f"{where}: статус '{st}' — закрытая работа живёт в {HISTORY_REL}, "
                          f"а не в активном плане. Активный план отвечает на вопрос «что идёт и что "
                          f"взять следующим»; когда закрытое остаётся в нём, ответ приходится "
                          f"вычитывать из архива (замер: 20 из 25 работ были `done`)")
        elif st not in ACTIVE_DECLARABLE:
            errors.append(f"{where}: status '{st}' вне словаря активного плана "
                          f"({list(ACTIVE_DECLARABLE)}); закрытое — в {HISTORY_REL}")
        # СВЯЗЬ С РЕАЛЬНОСТЬЮ. Открытый PR и статус `todo` — противоречие: PR существует, значит
        # работа начата. Проверяется ФОРМА (в файле есть `pr`), а не состояние GitHub: объявленное
        # состояние чужой системы стареет молча, а форма — нет.
        # ЗАМОРОЗКА: замороженную работу нельзя ВЕСТИ. Объявлять её в плане можно — иначе план
        # перестал бы описывать продукт, — но взятие в работу при держащейся заморозке противоречит
        # решению владельца, и проверка называет его номером, а не пересказом.
        _fex = str(w.get("freeze_exception") or "").strip()
        if st == "in_progress" and w.get("id") in _frozen_ids:
            errors.append(f"{where}: работа взята в дело, но заморожена решением {FREEZE_DECISION} — "
                          f"{_frozen[w.get('id')]}. Либо дождитесь исхода, либо объявите исключение "
                          f"явно: `freeze_exception: <почему эта работа — условие прогона>`")
        if "freeze_exception" in w and not _fex:
            errors.append(f"{where}: freeze_exception объявлено пустым — исключение из решения "
                          f"{FREEZE_DECISION} без причины это тихий обход, а не исключение")
        if w.get("pr") and st == "todo":
            errors.append(f"{where}: указан pr, но статус 'todo' — PR существует, значит работа "
                          f"начата; поставьте 'in_progress' либо уберите ссылку на PR")
        # closed-defect-closes-its-issue: работа, закрывающая дефект, обязана довести сигнал до
        # носителя (GitHub issue). Поле issue — опциональное, но если объявлено, обязано быть int.
        # Проверка формы (есть ли поле), а не состояния GitHub: состояние чужой системы стареет.
        if "issue" in w:
            iss = w.get("issue")
            if not isinstance(iss, int) or iss <= 0:
                errors.append(f"{where}: issue должен быть положительным int (номер заявки), "
                              f"получен {iss!r}")
        if st == "in_progress" and not (w.get("pr") or w.get("branch")):
            warns.append(f"{where}: работа идёт, но не названы ни pr, ни branch — состояние работы "
                         f"негде посмотреть")
        for k in ("evidence", "finding"):
            rel = w.get(k)
            if rel and root is not None and not (Path(root) / str(rel)).exists():
                errors.append(f"{where}: {k} '{rel}' не резолвится от корня репозитория — "
                              f"ссылка на доказательство, которого нет, хуже её отсутствия")
        deps = w.get("depends_on") or []
        if not isinstance(deps, list):
            errors.append(f"{where}: depends_on должен быть списком")
            deps = []
        if wid in deps:
            errors.append(f"{where}: зависит от себя")
        for d in deps:
            if d not in by_id and d not in closed_ids:
                errors.append(f"{where}: depends_on '{d}' не резолвится ни в работу плана, "
                              f"ни в закрытую работу истории")
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

GLOBAL_SCOPE = "*"          # маркер «пишет всюду»: конфликтует с любой областью


FREEZE_DECISION = "ep-2026-08-17-capability-freeze-until-second-brownfield"
FREEZE_GOAL = "second-real-brownfield"
FREEZE_OUTCOME = "owner_reaches_verified_pr_without_patching_the_kit"
FREEZE_RELATIONS = ("run_condition", "extension")
# СНЯТИЕ РЕШЕНИЕМ — ОТДЕЛЬНОЕ ПОЛЕ, А НЕ ИСХОД (19.08.2026, разбор плана после аудита).
#
# Заморозка снималась единственным способом — исходом, ставшим верным; так и задумано, чтобы
# «разморозить вручную технически нечем». 19.08 владелец решил снять её ДО достижения условия
# (`ep-2026-08-19-freeze-lifted`), и снятие записали единственным доступным способом: поставили
# `owner_reaches_verified_pr_without_patching_the_kit: true`. Комментарий рядом честно говорил,
# что условие не достигнуто, — но ЧИТАЕТ-ТО КОД ЗНАЧЕНИЕ, а не комментарий.
#
# ЦЕНА БЫЛА НЕ В ЗАМОРОЗКЕ. Этот же исход стережёт канал `stable`: пока он `true`, и `next`, и
# любая проверка считают второй brownfield пройденным, а `verified PR = 0` и `field_evidence`
# пуст. То есть решение о процессе молча переписало ФАКТ о продукте.
#
# Развязка: снятие объявляется своим полем на цели и обязано ссылаться на существующее решение
# (проверяет `validate`), а исход остаётся тем, что он есть. Оба намерения сохранены и видны
# порознь: заморозка снята явно, гейт stable честен.
FREEZE_LIFT_FIELD = "freeze_lifted_by"


def freeze_state(plan) -> dict:
    """Держится ли заморозка новых умений. -> {"frozen": bool, "reason": str, ...}.

    Решение владельца `ep-2026-08-17-capability-freeze-until-second-brownfield`: новое умение не
    принимается, пока исход `owner_reaches_verified_pr_without_patching_the_kit` не стал верным.

    СНЯТИЕ — НЕ ДАТА И НЕ ФЛАГ, а тот самый исход: он читается из цели, поэтому «разморозить
    вручную» технически нечем. Если цели или исхода в плане нет — это НЕ «заморозки нет»: считаем
    заморозку держащейся и говорим, что состояние не прочиталось (fail-closed: иначе достаточно
    удалить строку, чтобы правило исчезло).
    """
    g = next((x for x in goals(plan) if x.get("id") == FREEZE_GOAL), None)
    # ПРАВИЛО ОТНОСИТСЯ К РЕПОЗИТОРИЮ КИТА, А НЕ К ПРОДУКТАМ ДОЧЕК. Заморозка — внутреннее решение о
    # развитии кита; требовать её классификацию от плана чужого продукта значило бы экспортировать
    # свою политику в чужой CI. Поймано первым же полным прогоном: обязательный признак уронил 12
    # проверок, включая планы, которые кит СОЗДАЁТ дочке (`bootstrap`) и шаблон плана.
    # Признак применимости — наличие САМОЙ ЦЕЛИ заморозки в плане: она есть только там, где решение
    # принято.
    if not g:
        return {"frozen": False, "applies": False, "readable": True, "decision": FREEZE_DECISION,
                "reason": f"в этом плане нет цели '{FREEZE_GOAL}' — заморозка умений относится к "
                          f"репозиторию кита, а не к плану продукта"}
    lifted = str(g.get(FREEZE_LIFT_FIELD) or "").strip()
    if lifted:
        # Снято решением человека, а не достижением исхода. Исход при этом НЕ трогаем и
        # возвращаем как есть: «правило больше не держит» и «условие выполнено» — разные факты,
        # и путать их значит потерять второй.
        reached = bool((g.get("outcome") or {}).get(FREEZE_OUTCOME))
        return {"frozen": False, "applies": True, "readable": True, "decision": FREEZE_DECISION,
                "lifted_by": lifted, "outcome_reached": reached,
                "reason": (f"заморозка снята решением {lifted}"
                           + ("" if reached else
                              f"; исход {FREEZE_OUTCOME} при этом ещё НЕ достигнут — "
                              f"снято решением, а не результатом"))}
    if not isinstance(g.get("outcome"), dict) or FREEZE_OUTCOME not in g["outcome"]:
        # ЦЕЛЬ ЕСТЬ, А ИСХОДА НЕТ — это НЕ «заморозки нет»: иначе правило снималось бы удалением
        # одной строки в том же файле, который оно охраняет.
        return {"frozen": True, "applies": True, "readable": False, "decision": FREEZE_DECISION,
                "reason": f"исход {FREEZE_GOAL}.{FREEZE_OUTCOME} не найден в плане — "
                          f"состояние заморозки не прочитано, поэтому считается держащейся"}
    reached = bool(g["outcome"][FREEZE_OUTCOME])
    return {"frozen": not reached, "applies": True, "readable": True, "decision": FREEZE_DECISION,
            "lifted_by": None, "outcome_reached": reached,
            "reason": (f"исход {FREEZE_OUTCOME} верен — заморозка снята" if reached else
                       f"исход {FREEZE_OUTCOME} ещё не верен — новые умения не принимаются")}


def goal_freeze_relation(plan) -> dict:
    """{id цели: run_condition|extension|None}. Отношение объявлено НА ЦЕЛИ намеренно: оговорка
    решения сказана про назначение работы, а проверять её по формулировке заявки — открыть лазейку
    через слова (это названо в самом решении)."""
    return {g["id"]: g.get("freeze_relation") for g in goals(plan)}


def frozen_work(plan, single_goal=None) -> dict:
    """{id работы: причина} для работ, которые заморозка не пускает в дело.

    Заморожена работа цели `extension`, пока держится заморозка, — КРОМЕ работы с явным
    `freeze_exception: <причина>`: исключение существует, но только словами и с причиной, потому что
    молчаливый обход правила и есть то, от чего правило не работает.
    """
    st = freeze_state(plan)
    if not st["frozen"]:
        return {}
    rel = goal_freeze_relation(plan)
    out = {}
    for w in items(plan):
        goal = w.get("goal") or single_goal
        if rel.get(goal) != "extension":
            continue
        if str(w.get("freeze_exception") or "").strip():
            continue
        out[w.get("id")] = (f"цель '{goal}' помечена как расширение умений, а заморозка держится "
                            f"({st['reason']}; решение {st['decision']})")
    return out


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
