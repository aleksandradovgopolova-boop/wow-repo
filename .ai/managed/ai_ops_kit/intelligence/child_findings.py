#!/usr/bin/env python3
"""child_findings.py — обратное наследование дочка→кит: findings/from-children → планирование (#585, §28).

ПОВОД (клин вехи 4.2, обратная связь 07.09.2026, repo-reality §28). ФАКТ (сверено на main): канал
`findings/from-children` НЕ пуст — там реальные наблюдения от живых прогонов дочек. Не хватает не
канала, а ОБРАТНОЙ ПРОВОДКИ: эти наблюдения никуда не втекают. Двусторонняя модель §28 работала только
в одну сторону (кит → дочка), обратно — нет. Этот модуль замыкает петлю: наблюдения дочек становятся
кандидат-работами и уроками (прецедентами), которые кит показывает владельцу.

ДВА ВЫХОДА, ПО СОСТОЯНИЮ НАБЛЮДЕНИЯ (states из engops.kit_feedback):
  * ОТКРЫТОЕ наблюдение (`delivered`/`accepted` — доехало в кит, но ещё не решено) → КАНДИДАТ-РАБОТА
    (DRAFT): предложение владельцу, активной работой без его решения не станет (writer ≠ judge).
  * ЗАКРЫТОЕ наблюдение (`became_work`/`rejected`) → входит в УРОК: `precedent_ledger` группирует
    наблюдения в прецеденты (факт + число случаев + контексты), БЕЗ утверждения причинности (#586).
Так «замечание, которое ещё надо разобрать» и «опыт, который уже накоплен» — разные вещи, и первое
не выдаётся за второе.

ЧЕСТНЫЕ ГРАНИЦЫ:
  * Модуль ТОЛЬКО ЧИТАЕТ (`findings/from-children/*.yaml` через kit_feedback.load_kit_findings) и
    ПРОЕЦИРУЕТ. Ничего не пишет: ни plan.yaml, ни active-work. Кандидат — DRAFT.
  * Кандидат не станет активной работой без решения человека (`requires_human_decision: true`).
  * Живой прогон, дописывающий СВЕЖУЮ строку в findings/from-children, проходит ровно этот же путь —
    новая строка тем же кодом станет видимым кандидатом (доказывается тестом; сам живой прогон — #587).

ГДЕ ОН ЖИВЁТ. Пакет `intelligence` (слой выше ядра). Импортирует `engops.kit_feedback` (capabilities,
слой ниже — зависимость ВНИЗ разрешена) ради единственного источника пути/состояний канала и
`precedent_ledger` (тот же слой). `validation` не тянет.
"""
from __future__ import annotations

from pathlib import Path

# Состояния наблюдения, при которых оно ещё ЖДЁТ разбора (доехало в кит, но не решено). `new` —
# ещё в дочке (в кит не доставлено), сюда не попадает; `became_work`/`rejected` — уже закрыто.
_OPEN_STATES = ("delivered", "accepted")


def _text(v) -> str:
    return str(v or "").strip()


def load_findings(kit_root) -> list[dict]:
    """Наблюдения из `findings/from-children/*.yaml` (кит-сторона канала §28). Read-only, не бросает.

    Путь и разбор берём у единственного владельца канала — engops.kit_feedback, чтобы не завести
    второй источник истины о раскладке. Битые файлы пропускаются молча: это read-only проекция для
    планирования, а не валидатор канала (у канала свой)."""
    from ai_ops_kit.engops import kit_feedback
    root = Path(kit_root)
    d = root / kit_feedback.KIT_DIR
    out: list[dict] = []
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.yaml")):
        doc, err = kit_feedback._load(f)
        if err or not isinstance(doc, dict):
            continue
        out.append(doc)
    return out


def _candidate_from_observation(obs: dict) -> dict:
    """Открытое наблюдение → кандидат-работа (DRAFT). Метки неактивности — как у outcome_insight (#567):
    `status: draft`, `active: false`, `requires_human_decision: true`. Кит предлагает — решает человек."""
    oid = _text(obs.get("id")) or "obs"
    statement = _text(obs.get("statement")) or oid
    cls = _text(obs.get("observation_class")) or "наблюдение"
    child = obs.get("child") if isinstance(obs.get("child"), dict) else {}
    context = _text(child.get("name")) or _text(child.get("path")) or "прогон дочки"
    title = statement if len(statement) <= 100 else statement[:97] + "..."
    return {
        "schema_version": 1,
        "kind": "CandidateWork",
        "id": f"cand-{oid}",
        "title": f"Разобрать наблюдение из прогона: {title}",
        "type": "investigation" if cls == "defect" else "improvement",
        "owner_role": "product-manager",
        "status": "draft",                 # предложение, а не работа
        "active": False,                   # не занимает область записи
        "requires_human_decision": True,   # не станет активной без решения человека
        "source": "child-finding",         # источник кандидата (для ярлыка во входящих/приёмке)
        "source_observation": oid,
        "source_context": context,
        "severity": _text(obs.get("severity")) or None,
        "rationale": statement,
    }


def project_findings(kit_root) -> dict:
    """READ-ONLY проекция канала §28 в планирование: кандидаты (из открытых) + уроки-прецеденты (из всех).

    -> {"observation_count", "open_count", "candidates": [...], "precedents": [...]}.
    Кандидаты — только из ОТКРЫТЫХ наблюдений (ждут разбора). Прецеденты — из ВСЕХ наблюдений
    (накопленный опыт: факт + число случаев + контексты, без причинности — #586). Ничего не пишет."""
    from ai_ops_kit.intelligence import precedent_ledger
    observations = load_findings(kit_root)
    open_obs = [o for o in observations if _text(o.get("state")) in _OPEN_STATES]
    candidates = [_candidate_from_observation(o) for o in open_obs]
    precedents = precedent_ledger.precedents_from_observations(observations)
    return {
        "schema_version": 1,
        "kind": "ChildFindingsProjection",
        "observation_count": len(observations),
        "open_count": len(open_obs),
        "candidates": candidates,
        "precedents": precedents,
    }
