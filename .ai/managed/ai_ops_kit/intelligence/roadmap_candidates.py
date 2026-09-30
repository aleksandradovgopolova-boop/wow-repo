#!/usr/bin/env python3
"""roadmap_candidates.py — второй источник задач-кандидатов: НЕПОКРЫТЫЕ направления роадмапа.

ПОВОД (эпик auto-slice-candidates). Направление роадмапа с исходами, но без единой заведённой работы,
доходило до плана только руками: кит выводил горизонты (`roadmap_manager.build`), видел «Дальше»/«не
взято в работу», но остановиться на этом — значит дать направлению потеряться. Этот модуль замыкает
ту же петлю, что `child_findings` для наблюдений дочек: непокрытое направление становится
кандидат-работой (DRAFT), которую кит кладёт владельцу во входящие. Решает по-прежнему человек.

ЧТО ТАКОЕ НЕПОКРЫТОЕ НАПРАВЛЕНИЕ. Цель плана (`goals[]`) с ОБЪЯВЛЕННЫМИ исходами (`outcome`), под
которую в плане нет ни одной работы (`not direction.work_ids`) и которая ещё не достигнута
(горизонт != SHIPPED). Это ровно направление, которое роадмап декларирует, а план молчит.

ДВА ВЫХОДА НЕ ЗАВОДИМ. Кандидат — та же форма, что у `child_findings._candidate_from_observation`:
DRAFT с метками неактивности (`status: draft`, `active: false`, `requires_human_decision: true`).
Активной работой без решения человека он не станет (writer ≠ judge).

ЧЕСТНЫЕ ГРАНИЦЫ:
  * Модуль ТОЛЬКО ЧИТАЕТ план и историю (`delivery_plan.load`/`load_history`) и строит роадмап
    (`roadmap_manager.build`). Ничего не пишет: ни plan.yaml, ни active-work. Кандидат — DRAFT.
  * «НЕ ЗНАЮ» ≠ «РЕЗАТЬ НЕЧЕГО». Если плана нет или он не читается — это НЕ пустой список кандидатов
    как «всё покрыто»: возвращаем `readable: false` с причиной словами. Иначе достаточно сломать
    план, чтобы кит уверенно сказал «непокрытых направлений нет».
  * Порог покрытия — САМ ФАКТ заведённой работы, а не её состояние: направление с работой (пусть
    заблокированной) уже декомпозировано, и кандидата не даёт.

ГДЕ ОН ЖИВЁТ. Пакет `intelligence` (слой ВЫШЕ ядра). Импортирует `planning` (delivery_plan,
roadmap_manager) — это зависимость ВНИЗ по слоям, разрешённая (так же делают `product_advice`,
`team_sync`). `validation` не тянет. Объединение с находками (`gather`) и запись в план живут ВЫШЕ:
слой `intelligence` в `planning` импортировать нельзя (вверх), поэтому union и приёмку держит CLI.
"""
from __future__ import annotations

from pathlib import Path


def _text(v) -> str:
    return str(v or "").strip()


def _candidate_from_direction(d, humanize) -> dict:
    """Непокрытое направление → кандидат-работа (DRAFT). Метки неактивности — те же, что у
    `child_findings`: `status: draft`, `active: false`, `requires_human_decision: true`."""
    gid = _text(d.goal_id)
    title = _text(d.title) or gid
    return {
        "schema_version": 1,
        "kind": "CandidateWork",
        "id": f"cand-dir-{gid}",
        "title": f"Декомпозировать направление: {title}",
        "type": "improvement",
        "owner_role": "product-manager",
        "status": "draft",                 # предложение, а не работа
        "active": False,                   # не занимает область записи
        "requires_human_decision": True,   # не станет активной без решения человека
        "source": "roadmap-direction",
        "source_goal": gid,
        "rationale": (f"направление «{title}» ({humanize(d.reached, d.total)}) объявлено в роадмапе, "
                      f"но под него нет ни одной заведённой работы — оно рискует потеряться"),
    }


def project_uncovered_directions(child_root) -> dict:
    """READ-ONLY проекция непокрытых направлений роадмапа в задачи-кандидаты.

    -> {"schema_version", "kind": "RoadmapDirectionCandidates", "readable", "direction_count",
        "uncovered_count", "candidates": [...], ["note"]}.

    `readable: false` — план не прочитан (нет файла или битый): честное «не знаю», а НЕ «резать
    нечего». Ничего не пишет и не бросает: обратный канал обогащает очередь владельца, а не является
    её предусловием.
    """
    from ai_ops_kit.planning import delivery_plan as _plan
    from ai_ops_kit.planning import roadmap_manager as _rm
    base = {"schema_version": 1, "kind": "RoadmapDirectionCandidates"}
    root = Path(child_root)

    try:
        plan = _plan.load(root)
    except _plan.PlanCorrupt as e:
        return {**base, "readable": False, "direction_count": 0, "uncovered_count": 0,
                "candidates": [], "note": f"план не прочитан ({e}) — состояние покрытия неизвестно, "
                                          f"это не «непокрытых направлений нет»"}
    if plan is None:
        return {**base, "readable": False, "direction_count": 0, "uncovered_count": 0,
                "candidates": [], "note": "плана нет — резать направления не из чего; это «не знаю», "
                                          "а не «всё покрыто»"}

    # История опциональна и на ОТБОР непокрытых не влияет (покрытость = сам факт work_ids, а
    # горизонт SHIPPED считается по исходам, не по истории). Битую историю не выдаём за пустую —
    # но и не роняем проекцию из-за неё: берём пустую и продолжаем отбор, который от неё не зависит.
    try:
        history = _plan.load_history(root)
    except _plan.PlanCorrupt:
        history = []

    roadmap = _rm.build(plan, history)
    directions = list(roadmap.directions)
    # Непокрытое = ИСХОД-НЕСУЩЕЕ направление (`total`) без заведённой работы и ещё не достигнутое.
    uncovered = [d for d in directions
                 if d.total and not d.work_ids and d.horizon != _rm.SHIPPED]
    candidates = [_candidate_from_direction(d, _rm.humanize_outcomes) for d in uncovered]
    return {**base, "readable": True, "direction_count": len(directions),
            "uncovered_count": len(uncovered), "candidates": candidates}
