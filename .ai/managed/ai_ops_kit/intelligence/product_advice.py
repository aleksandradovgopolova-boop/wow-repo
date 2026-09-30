#!/usr/bin/env python3
"""Product Advice (#958, исход `kit_recommends_what_the_product_needs`) — ТОНКИЙ слой поверх уже
существующих продуктовых сигналов кита. Отвечает на вопрос «что нужно ПРОДУКТУ пользователя»
человеческим языком, продуктом вперёд, а не про инженерную настройку самого кита.

Это НЕ рекомендательный/intelligence-движок и не аналитика. Здесь НЕТ ни одного нового источника
и ни одной новой оценки: модуль читает три сигнала, которые кит уже вычисляет, и переводит их в
рекомендации «что нужно продукту». Каждая рекомендация ЗАЗЕМЛЕНА — несёт `source` (файл/сигнал),
из которого она выведена.

Три источника (переиспользуются, не изобретаются):

  1. `planning/roadmap.check` — цель под «Сейчас/Следующий результат» без единой работы в плане.
     Это ПРОДУКТОВАЯ ВОЗМОЖНОСТЬ (`opportunity`): направление объявлено, но простаивает.
  2. `intelligence/health_product.product_health_report` — метрика результата отсутствует/пуста
     или слабое место метрик. Это ПРОДУКТОВЫЙ ПРОБЕЛ (`gap`): не видно, работает ли продукт.
  3. `planning/next_work.compute` — лучшая следующая работа по ценности к цели первого приоритета.
     Это «что делать дальше по продукту» (`next`).

ГЛАВНЫЙ ИНВАРИАНТ ЧЕСТНОСТИ (как в `health_common`): нет продуктового сигнала → говорим «пока не
хватает продуктовых данных, чтобы советовать по продукту» (`enough_product_data == False`) и
НИКОГДА не выдумываем продуктовую потребность. Пустой список — законный ответ, если продуктовых
артефактов в репозитории нет; выдуманная потребность — дефект.
"""
from __future__ import annotations

import re
from pathlib import Path

OPPORTUNITY = "opportunity"
GAP = "gap"
NEXT = "next"

# Уходящая формулировка сигнала roadmap.check: «цель 'X' ... направление без работы». Читаем именно
# ЕЁ (существующий сигнал), а не пересчитываем диф roadmap↔plan заново — это было бы новой аналитикой.
_GOAL_IN_WARNING = re.compile(r"цель '([^']+)'")


def _opportunities(child_root, plan) -> list[dict]:
    """Направление без работы -> продуктовая возможность. Источник — `roadmap.check`."""
    from ai_ops_kit.planning import roadmap as _roadmap
    from ai_ops_kit.planning import delivery_plan as _plan
    try:
        rep = _roadmap.check(child_root, plan)
    except Exception:  # noqa: BLE001 — тонкий слой не обязан ронять совет из-за одного источника
        return []
    rel = _roadmap.roadmap_rel(child_root)
    recs = []
    seen = set()
    for w in rep.get("warnings", []):
        if "направление без работы" not in w:
            continue
        m = _GOAL_IN_WARNING.search(w)
        goal = m.group(1) if m else None
        # Пока у цели нет человеческого имени (id-заглушка `goal-id-N` из bootstrap-черновика),
        # не echo'им внутренний id в лицо владельца — называем безымянным направлением и
        # подсказываем, чем это чинится. Иначе — направление «<имя>».
        if goal and not _plan.is_placeholder_goal(goal):
            subject = f"направление «{goal}»"
        else:
            subject = _plan.UNNAMED_GOAL_LABEL
        # Несколько безымянных направлений схлопываются в один и тот же текст — не повторяем его
        # дважды: две одинаковые строки читателю ничего не добавляют (следствие правки: id больше
        # не различает эти направления в тексте).
        if subject in seen:
            continue
        seen.add(subject)
        recs.append({
            "kind": OPPORTUNITY,
            "need": f"начать двигать {subject} — оно объявлено как цель продукта, "
                    "но им пока никто не занят",
            "why": "объявленная цель без единого шага — это ценность продукта, которая простаивает; "
                   "начать её значит превратить намерение в результат для пользователя",
            "source": rel,
        })
    return recs


def _gaps(child_root) -> tuple[list[dict], bool]:
    """Пробел метрики результата -> продуктовый пробел. Источник — `health_product`.

    Второй возврат — был ли прочитан хотя бы один ПОЛОЖИТЕЛЬНЫЙ продуктовый сигнал (артефакт
    продукта реально есть). Он нужен инварианту честности: «определить метрику результата» —
    осмысленный совет, только когда продукт УЖЕ существует (есть направление/паспорт). На пустом
    репозитории продукта нет, и советовать мерить его результат было бы выдумкой потребности.
    """
    from ai_ops_kit.intelligence import health_product as _hp
    try:
        report = _hp.product_health_report(Path(child_root))
    except Exception:  # noqa: BLE001
        return [], False
    signals = {s.get("name"): s for s in report.get("signals", [])}
    product_present = any(s.get("band") != "unknown" for s in report.get("signals", []))
    metrics = signals.get("product_metrics") or {}
    band = metrics.get("band")
    recs = []
    if not product_present:
        # Ни одного продуктового артефакта — потребность выдумывать нельзя, молчим по пробелам.
        return [], False
    if band == "unknown":
        recs.append({
            "kind": GAP,
            "need": "определить и начать измерять метрику результата продукта — пока не видно, "
                    "работает ли он для пользователя",
            "why": "без метрики результата любой вывод «продукт в порядке» — предположение, "
                   "а не факт; это самый дешёвый способ увидеть, приносит ли продукт пользу",
            "source": _hp.METRICS_REL,
        })
    elif band in ("yellow", "red"):
        reason = (metrics.get("reason") or "").strip()
        recs.append({
            "kind": GAP,
            "need": "заняться слабым местом продуктовых метрик — результат просел",
            "why": reason or "метрики результата ниже порога — это прямой сигнал о продукте",
            "source": _hp.METRICS_REL,
        })
    return recs, product_present


def _next_step(child_root) -> tuple[list[dict], bool]:
    """Лучшая следующая работа по ценности -> «что дальше по продукту». Источник — `next_work`.

    Второй возврат — виден ли план вообще (чтобы `enough_product_data` не сказал «данных нет»
    там, где план есть, но срочной следующей работы нет)."""
    from ai_ops_kit.planning import next_work as _nw
    from ai_ops_kit.planning import delivery_plan as _plan
    try:
        rep = _nw.compute(child_root)
    except Exception:  # noqa: BLE001 — битый план назовёт свой валидатор; совет не роняем
        return [], False
    plan_present = bool(rep.get("plan_present")) and not rep.get("plan_is_template")
    nb = rep.get("next_best")
    if not nb:
        return [], plan_present
    why = "; ".join(nb.get("why") or []) or "готова по зависимостям и двигает продукт вперёд"
    return [{
        "kind": NEXT,
        "need": f"взять следующей работу «{nb.get('title') or nb.get('id')}» — "
                "это ближайший шаг к цели продукта",
        "why": why,
        "source": _plan.plan_rel(child_root),
    }], plan_present


def recommend(child_root) -> dict:
    """Собрать 1–3 продуктовые рекомендации из трёх существующих сигналов.

    -> {kind, recommendations: [{kind, need, why, source}], enough_product_data: bool, note}.

    Порядок: возможности (простаивающее направление) → пробелы (метрика результата) → следующий
    шаг. Нет ни одного продуктового сигнала → пустой список и `enough_product_data == False` с
    честной нотой; продуктовая потребность НИКОГДА не выдумывается.
    """
    from ai_ops_kit.planning import delivery_plan as _plan
    try:
        plan = _plan.load(child_root)
    except Exception:  # noqa: BLE001 — читаем сигналы как есть; битость назовёт валидатор плана
        plan = None

    opportunities = _opportunities(child_root, plan)
    gaps, product_present = _gaps(child_root)
    nxt, plan_present = _next_step(child_root)

    recs = (opportunities + gaps + nxt)[:3]

    # Хватает ли данных, чтобы вообще советовать по продукту: либо мы что-то нашли, либо в
    # репозитории есть продуктовый артефакт/план — иначе честно «данных не хватает».
    enough = bool(recs) or product_present or plan_present
    note = None
    if not enough:
        note = ("пока не хватает продуктовых данных, чтобы советовать по продукту — "
                "продуктовую потребность выдумывать не буду")
    elif not recs:
        note = "продуктовые данные есть, но срочного по продукту сейчас нет"

    return {
        "kind": "ProductAdvice",
        "schema_version": 1,
        "recommendations": recs,
        "enough_product_data": enough,
        "note": note,
    }
