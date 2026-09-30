#!/usr/bin/env python3
"""Валидация delivery-plan: структура, семантика и контракт истории.

Вынесено из `delivery_plan.py` (чистый рефакторинг, поведение байт-в-байт). Зависит только от
`plan_model` (словарь, аксессоры, заморозка) и `contours` — `delivery_plan` отсюда НЕ импортируется,
поэтому цикла нет. `delivery_plan` ре-экспортирует `validate`/`validate_history` для прежних
импортёров (движок, gates, cli, тесты).
"""
from __future__ import annotations

import re
from pathlib import Path

from ai_ops_kit.planning import contours as _contours
from ai_ops_kit.planning.plan_model import (
    items, goals, current_milestone, freeze_state, _freeze_lift_errors, frozen_work,
    KIND, DECLARABLE, DERIVED, ACTIVE_DECLARABLE, CLOSED_DECLARABLE, VALUE,
    FORBIDDEN_ITEM_KEYS, OWNER_WAIT_STATUS, OWNER_WAIT_KEY, HISTORY_REL,
    GOAL_STATUSES, FREEZE_RELATIONS, FREEZE_DECISION,
    MILESTONE_KEY, MILESTONE_STATUSES,
)


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


def _workitem_status_errors(w, where):
    """Ошибки/предупреждения по СТАТУСУ работы: выводимый нельзя объявлять, закрытый живёт в истории,
    вне словаря активного плана, и waiting_on_owner без названного действия — фантомное состояние.
    Вынесено из validate (func-size, чистый перенос без смены поведения). -> (errors, warns)."""
    errors, warns = [], []
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
    # WAITING БЕЗ НАЗВАННОГО ДЕЙСТВИЯ — ФАНТОМНОЕ СОСТОЯНИЕ. Статус говорит «ждём владельца»,
    # но пока не сказано ЧЕГО именно ждём, план неотличим от застрявшего: ровно та ловушка,
    # ради которой статус и заведён. Поэтому `waiting_on` с непустым текстом — обязателен.
    if st == OWNER_WAIT_STATUS and not str(w.get(OWNER_WAIT_KEY) or "").strip():
        errors.append(f"{where}: статус '{OWNER_WAIT_STATUS}' без непустого '{OWNER_WAIT_KEY}' — "
                      f"waiting без названного действия владельца это фантомное состояние: план "
                      f"выглядит осмысленным, а чего он ждёт — нигде. Назовите ожидаемый шаг: "
                      f"`{OWNER_WAIT_KEY}: <какое действие владельца разблокирует работу>`")
    return errors, warns


def _milestone_errors(m, gids) -> list:
    """Минимальная проверка ИМЕНОВАННОГО текущего milestone (`current_milestone`). -> список ошибок.

    Аддитивно к остальным правилам плана. Milestone — тот же уровень объявления, что и работа,
    поэтому и правила те же по духу: `id` — slug (движок и `--feature` ждут его же), `status` — из
    объявляемого набора, `linked_goals` — существующие id целей плана, и — как у работы — НАЗВАТЬ
    ИСПОЛНИТЕЛЯ нельзя (роль/направление, не вендор: иначе смена runtime переписывала бы веху).
    Отсутствие ключа — не ошибка: milestone необязателен, паспорт тогда откатывается к прокси.
    """
    errors = []
    if not isinstance(m, dict):
        return [f"{MILESTONE_KEY}: ожидался mapping, получен {type(m).__name__}"]
    mid = m.get("id")
    if not mid or not _engine_id_ok(str(mid)):
        errors.append(f"{MILESTONE_KEY}: id '{mid}' непригоден — нужен slug нижнего регистра "
                      f"({_engine_id_pattern()})")
    if not (m.get("name") or "").strip():
        errors.append(f"{MILESTONE_KEY}: нет name — веха без имени не первоклассна")
    st = m.get("status")
    if st not in MILESTONE_STATUSES:
        errors.append(f"{MILESTONE_KEY}: status '{st}' вне объявляемого набора "
                      f"({list(MILESTONE_STATUSES)})")
    linked = m.get("linked_goals")
    if not isinstance(linked, list) or not linked:
        errors.append(f"{MILESTONE_KEY}: linked_goals должен быть непустым списком id целей — "
                      f"веха без связи с направлением не приоритизируется")
    else:
        for g in linked:
            if g not in gids:
                errors.append(f"{MILESTONE_KEY}: linked_goals '{g}' не резолвится в goals плана")
    for k in FORBIDDEN_ITEM_KEYS:
        if k in m:
            errors.append(f"{MILESTONE_KEY}: поле '{k}' запрещено — веха называет направление, "
                          f"исполнителя выбирает роутер в момент запуска")
    return errors


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


def _goal_errors(plan, gl, root) -> list:
    """Ошибки уровня ЦЕЛЕЙ: заморозка умений (freeze_relation + снятие) и статус цели.

    Вынесено из validate (func-size; чистый перенос без смены поведения).

    ЗАМОРОЗКА УМЕНИЙ ИСПОЛНЯЕТСЯ ПРОВЕРКОЙ, А НЕ ПАМЯТЬЮ (работа `capability-freeze-enforced`):
    решение владельца существовало записью с 17.08 и ничем не сверялось (18.08 кит сам предложил
    взять работу из замороженной цели). Отношение цели к заморозке — обязательное объявление:
    необъявленная цель означала бы «правило не про меня», то есть тихий обход.

    СНЯТИЕ ЗАМОРОЗКИ ОБЯЗАНО ОПИРАТЬСЯ НА ДОКАЗАТЕЛЬСТВО: решением (`freeze_lifted_by`, запись в
    реестре) или исходом (`true` + полевое доказательство). Обе половины проверяет
    `_freeze_lift_errors`. Статус цели ВЛИЯЕТ НА ПРИОРИТЕТ (`goal_priority`), поэтому опечатка в нём
    молча переставила бы весь план — ловим здесь, где она видна человеку.
    """
    errors = []
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
    errors.extend(_freeze_lift_errors(_fz, root))
    for g in gl:
        st = g.get("status")
        if st is not None and st not in GOAL_STATUSES:
            errors.append(f"цель '{g['id']}': status '{st}' вне {list(GOAL_STATUSES)} — "
                          f"от статуса зависит приоритет работ этой цели")
    return errors


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
    errors.extend(_goal_errors(plan, gl, root))

    # ИМЕНОВАННЫЙ ТЕКУЩИЙ MILESTONE (необязателен). Если объявлен — проверяем минимально: id/slug,
    # статус из набора, связь с существующими целями, запрет исполнителей.
    m = current_milestone(plan)
    if m is not None:
        errors.extend(_milestone_errors(m, gids))

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
        _se, _sw = _workitem_status_errors(w, where)
        errors.extend(_se); warns.extend(_sw)
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
