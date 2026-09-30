#!/usr/bin/env python3
"""outcome_insight.py — обратная петля Outcome → Observation → Insight → Recommendation → Next Work.

ПОВОД (#567, «дыра №6» ревью 07.09.2026). Vision обещает `Measurement → Insights → Product
improvement`, но обратной петли от Outcome в Planning не было: кит умел ПОСЧИТАТЬ вердикт по релизу
(`validation.validate_product_objects.evaluate_outcome` — met/failed/unknown ИЗ ЧИСЕЛ, #566) и
СВЕСТИ его в пост-релизный результат (`cli.post_release_loop`, #545), но дальше замер никуда не шёл.
Этот модуль замыкает петлю: из завершённого Outcome с РЕАЛЬНЫМ замером рождается Insight, из Insight —
кандидат-работа (DRAFT), а вместе с ней — рекомендация с честной эпистемикой.

ТРИ ЧЕСТНЫХ ПРАВИЛА (иначе петля превращается в генератор правдоподобных выдумок):

  1. INSIGHT ТОЛЬКО НА РЕАЛЬНОМ ВЕРДИКТЕ. Вердикт `unknown` — это «нет данных», а не слабый сигнал.
     На нём инсайт НЕ фабрикуется: `derive_insight` возвращает None, а `no_insight_reason` называет,
     почему (замера нет / нет числового baseline / замер без даты). «Не знаю» и «плохо» — разные
     состояния, и первое честнее второго.

  2. ЭПИСТЕМИКА РАЗДЕЛЕНА. Инсайт держит три РАЗНЫХ списка, не сваленных в один «вывод»:
       * `observed`  — ФАКТ, снятый замером (метрика ушла с baseline на measured; guardrail просел);
       * `inferred`  — ВЫВОД, толкование факта (гипотеза подтверждена; цель не взята → правило change);
       * `unknown`   — критически НЕизвестное (guardrail объявлен, но не отчитан; неожиданный эффект).
     `confidence` (low/medium/high) падает с каждым пробелом: полный отчёт без сюрпризов = high,
     один пробел = medium, несколько = low. Уверенность — это ЗАМЕР полноты данных, не настроение.

  3. WRITER ≠ JUDGE. Кандидат-работа рождается со `status: draft`, `active: false`,
     `requires_human_decision: true`. Модуль НИЧЕГО не пишет в дочку (ни plan.yaml, ни active-work) —
     он ПРОЕЦИРУЕТ предложение, ровно как `project_outcome` проецирует контракт в граф. Кандидат
     виден владельцу в `inbox`/`next`, но активной работой не станет без его решения. Рекомендация
     несёт evidence (сколько источников, что факт, что допущение, что критично неизвестно) и только
     ПОТОМ «поэтому предлагаю X» — рекомендация без основания читалась бы как приказ.

ГДЕ ОН ЖИВЁТ. Пакет `intelligence` (слой выше ядра, читает его события — три кольца из AGENTS.md).
Модуль ЧИСТЫЙ: он НЕ импортирует `validation` (это была бы зависимость вверх), а ПОЛУЧАЕТ уже
посчитанный `evaluation` (результат `evaluate_outcome`) параметром. Проводку «посчитать вердикт →
родить инсайт» делает слой entrypoints (`cli.post_release_loop`), который вправе звать и то и другое.

Использование (программное; собственного CLI у модуля нет — он вызывается из post_release_loop):
    from ai_ops_kit.intelligence import outcome_insight
    bundle = outcome_insight.from_outcome(contract, readout, evaluation)  # None, если замера нет
"""
from __future__ import annotations

# Вердикты реального замера, на которых петля работает (unknown сюда НЕ входит — см. правило 1).
REAL_VERDICTS = ("met", "failed")


def _text(v) -> str:
    return str(v or "").strip()


def _slug(value: str) -> str:
    """Строка -> id-узел (`^[a-z0-9][a-z0-9-]*$`). Та же нормализация, что в validate_product_objects."""
    out: list[str] = []
    for ch in _text(value).lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-") or "outcome"


def _declared_guardrails(contract: dict) -> list[str]:
    return [_text(g.get("name")) for g in (contract.get("guardrails") or [])
            if isinstance(g, dict) and _text(g.get("name"))]


def _reported_guardrails(readout: dict) -> set[str]:
    return {_text(g.get("name")) for g in (readout.get("guardrails_observed") or [])
            if isinstance(g, dict) and _text(g.get("name"))}


def _needed_to_measure(evaluation: dict) -> str:
    """Что КОНКРЕТНО нужно, чтобы продуктовый итог стало можно измерить. Условие берём из состояния
    самого замера, а не общими словами: нет числового baseline/цели -> задать их; нет замера с датой ->
    снять его после выпуска. Так «результат ещё не накоплен» несёт названное условие, а не отписку."""
    ev = evaluation or {}
    if ev.get("baseline") is None or ev.get("target") is None:
        return "задать в контракте результата числовые baseline и цель — сравнивать замер пока не с чем"
    return "снять замер целевой метрики с датой после реального выпуска и повторить свод"


def build_outcome_readout(contract: dict | None, readout: dict | None,
                          evaluation: dict | None) -> dict | None:
    """Человеко-ориентированный свод ПРОДУКТОВОГО ИТОГА: сдвиг целевой метрики baseline→после релиза
    против цели — «что изменение сделало с продуктом», а не «shipped»/«verified» (P0 №6).

    Чистая ПРОЕКЦИЯ уже посчитанного `evaluate_outcome` (met/failed/unknown ИЗ ЧИСЕЛ) плюс контракта и
    отчёта; ничего не измеряет и не пишет. -> None без контракта (итог из воздуха не выдумываем).

    ЧЕСТНОСТЬ (инвариант «no evidence → no claim»):
      * есть реальный замер (met/failed) -> `measured=True`, несём сдвиг baseline→value против target,
        гипотезу и названные пробитые guardrail'ы;
      * замера ещё нет (unknown) -> `measured=False`, `value=None` (измеренного числа НЕ выдумываем),
        `not_accumulated_reason` + `needed_to_measure` (названное условие, что нужно, чтобы измерить).

    Значения для показа берём СЫРЫМИ из контракта/отчёта (сохраняя единицы вроде «42%»); числовой
    разбор для вердикта уже сделан в `evaluation`. `goal` — человеческая формулировка цели
    (`contract.decision`), а НЕ ключ метрики: ключ метрики — внутреннее имя, наружу не идёт.
    """
    if not isinstance(contract, dict):
        return None
    ev = evaluation or {}
    r = readout if isinstance(readout, dict) else {}
    verdict = ev.get("verdict", "unknown")
    measured = bool(ev.get("is_real_measurement")) and verdict in REAL_VERDICTS
    goal = _text(contract.get("decision"))
    metric_key = _text((contract.get("primary_metric") or {}).get("name"))
    baseline_disp = (contract.get("baseline") or {}).get("value")
    target_disp = (contract.get("target") or {}).get("value")
    value_disp = (r.get("measured") or {}).get("value") if measured else None
    hyp = _text(r.get("hypothesis")) or None
    out = {
        "kind": "OutcomeReadoutHuman",
        "goal": goal or None,
        "metric_key": metric_key or None,       # внутреннее имя — только для технических деталей
        "measured": measured,
        "verdict": verdict,
        "baseline": baseline_disp,
        "value": value_disp,
        "target": target_disp,
        "measured_at": ev.get("measured_at"),
        "direction": ev.get("direction"),
        "guardrail_breaches": list(ev.get("guardrail_breaches") or []),
        "hypothesis": hyp,
        "not_accumulated_reason": None,
        "needed_to_measure": None,
    }
    if not measured:
        out["not_accumulated_reason"] = (_text(ev.get("reason"))
                                         or "итог по релизу ещё не измерен")
        out["needed_to_measure"] = _needed_to_measure(ev)
    return out


def no_insight_reason(evaluation: dict | None) -> str | None:
    """Почему инсайта нет. -> строка-причина, если вердикт `unknown`; None, если инсайт рождается.

    Инвариант 1: на `unknown` инсайт не фабрикуется. Причину берём из самого evaluation (там она уже
    названа: «замера нет», «замер без даты», «в контракте нет числового baseline/target») — чтобы
    владельцу говорили «данных нет и вот почему», а не молчали."""
    ev = evaluation or {}
    if ev.get("verdict") in REAL_VERDICTS:
        return None
    return _text(ev.get("reason")) or "итог по релизу ещё не измерен — инсайт строить не на чем"


def _epistemics(contract: dict, readout: dict, evaluation: dict) -> dict:
    """Три раздельных списка: observed (факт) / inferred (вывод) / unknown (критически неизвестное)."""
    metric = _text((contract.get("primary_metric") or {}).get("name")) or "основная метрика"
    baseline, target = evaluation.get("baseline"), evaluation.get("target")
    measured, measured_at = evaluation.get("measured"), evaluation.get("measured_at")
    breaches = list(evaluation.get("guardrail_breaches") or [])

    # observed — только то, что прямо снято замером.
    observed = [f"метрика «{metric}» на замере {measured} (baseline {baseline}, цель {target})"
                + (f", измерено {measured_at}" if measured_at else "")]
    reported = _reported_guardrails(readout)
    for name in sorted(reported):
        state = "просела" if name in breaches else "в пределах"
        observed.append(f"защитная метрика «{name}»: {state}")

    # inferred — толкование факта (не измерено напрямую).
    inferred = []
    if evaluation.get("verdict") == "met":
        inferred.append("основная метрика взяла цель, защитные метрики удержаны — правило решения «continue»")
    elif breaches:
        inferred.append("цель не считается достигнутой из-за просевшей защитной метрики — правило «stop/change»")
    else:
        inferred.append("основная метрика цель не взяла — правило решения «change»")
    hyp = _text(readout.get("hypothesis"))
    if hyp == "confirmed":
        inferred.append("гипотеза подтверждена")
    elif hyp == "refuted":
        inferred.append("гипотеза опровергнута")
    b2d = _text(readout.get("back_to_discovery"))
    if b2d:
        inferred.append(f"знание в discovery: {b2d}")

    # unknown — критически неизвестное: объявленное, но не отчитанное + сюрпризы + неразрешённая гипотеза.
    unknown = []
    not_reported = sorted(set(_declared_guardrails(contract)) - reported)
    for name in not_reported:
        unknown.append(f"защитная метрика «{name}» объявлена, но не отчитана — просела или нет, неизвестно")
    if hyp == "inconclusive":
        unknown.append("гипотеза не разрешилась (inconclusive) — вывод сделан по одному замеру")
    for eff in (readout.get("unexpected_effects") or []):
        unknown.append(f"неожиданный эффект: {_text(eff) if not isinstance(eff, dict) else _text(eff.get('name'))}")

    return {"observed": observed, "inferred": inferred, "unknown": unknown}


def _confidence(epistemics: dict) -> str:
    """Уверенность = замер полноты данных. Каждый критически неизвестный факт понижает её."""
    gaps = len(epistemics.get("unknown") or [])
    if gaps == 0:
        return "high"
    if gaps == 1:
        return "medium"
    return "low"


def derive_insight(contract: dict | None, readout: dict | None, evaluation: dict | None) -> dict | None:
    """Из завершённого Outcome с РЕАЛЬНЫМ замером -> Insight с честной эпистемикой. Иначе -> None.

    `evaluation` — результат `validate_product_objects.evaluate_outcome` (met/failed/unknown ИЗ
    ЧИСЕЛ). На `unknown` возвращаем None (инвариант 1: инсайт не фабрикуется без данных; причину
    называет `no_insight_reason`). На met/failed строим Insight: `verdict`, три раздела эпистемики,
    `confidence`. Узел совместим с типом `insight` из registry/entities.yaml (feeds->feature,
    derived-from->outcome), но это ПРОЕКЦИЯ, а не запись в граф."""
    ev = evaluation or {}
    if ev.get("verdict") not in REAL_VERDICTS:
        return None
    contract = contract if isinstance(contract, dict) else {}
    readout = readout if isinstance(readout, dict) else {}
    metric = _text((contract.get("primary_metric") or {}).get("name"))
    oid = _slug(metric or contract.get("decision") or "outcome")
    epi = _epistemics(contract, readout, ev)
    verdict = ev["verdict"]
    headline = ("цель по релизу достигнута" if verdict == "met"
                else "цель по релизу не достигнута")
    return {
        "schema_version": 1,
        "kind": "Insight",
        "id": f"insight-{oid}",
        "outcome_id": oid,
        "verdict": verdict,
        "headline": f"«{metric or oid}»: {headline}",
        "reason": _text(ev.get("reason")),
        "epistemics": epi,
        "confidence": _confidence(epi),
        "derived_from_outcome": oid,          # ребро derived-from -> outcome (entities.yaml)
    }


def propose_candidate_work(insight: dict, contract: dict | None = None,
                           readout: dict | None = None) -> dict:
    """Insight -> кандидат-работа (DRAFT, НЕ автозапуск). Writer ≠ judge (инвариант 3).

    Кандидат несёт явные метки неактивности: `status: draft`, `active: false`,
    `requires_human_decision: true`. Он НЕ активная работа и станет ею только по решению человека.
    Направление берётся из readout.next_decision (настоящий next-шаг владельца), иначе — из вердикта:
    provал -> разобраться и предложить корректировку; met -> закрепить и выбрать следующую гипотезу."""
    readout = readout if isinstance(readout, dict) else {}
    verdict = insight.get("verdict")
    oid = insight.get("outcome_id") or "outcome"
    metric = _text((insight.get("headline") or "").strip("«").split("»")[0]) or oid
    if verdict == "failed":
        wtype, role = "investigation", "product-analyst"
        title = f"Разобраться, почему «{metric}» не взяла цель, и предложить корректировку"
    else:  # met
        wtype, role = "discovery", "product-manager"
        title = f"Закрепить результат по «{metric}» и выбрать следующую гипотезу"
    rationale = _text(readout.get("next_decision")) or insight.get("reason") or ""
    return {
        "schema_version": 1,
        "kind": "CandidateWork",
        "id": f"cand-{oid}",
        "title": title,
        "type": wtype,
        "owner_role": role,
        "status": "draft",             # НЕ todo/in_progress: это предложение, а не работа
        "active": False,               # не занимает область записи, не держится никем
        "requires_human_decision": True,  # не станет активной без решения человека
        "source_insight": insight.get("id"),
        "source_outcome": oid,
        "rationale": rationale,
    }


def build_recommendation(insight: dict, candidate: dict) -> dict:
    """Рекомендация с evidence: сколько источников, что факт, что допущение, что критично неизвестно —
    и ТОЛЬКО потом «поэтому предлагаю X» (инвариант 3). Рекомендация без основания читается как приказ.

    `sources` — честный счёт независимых наблюдений (замер + каждый отчитанный/неотчитанный сигнал),
    а не «уверенность из головы»."""
    epi = insight.get("epistemics") or {}
    facts = list(epi.get("observed") or [])
    assumptions = list(epi.get("inferred") or [])
    unknowns = list(epi.get("unknown") or [])
    return {
        "schema_version": 1,
        "kind": "Recommendation",
        "for_outcome": insight.get("outcome_id"),
        "confidence": insight.get("confidence"),
        "sources": len(facts) + len(unknowns),   # сколько наблюдений стоит за выводом
        "facts": facts,
        "assumptions": assumptions,
        "critical_unknowns": unknowns,
        "proposal": f"поэтому предлагаю: {candidate.get('title')}",
        "proposed_work": candidate.get("id"),
        # Явно: это предложение, а не назначение. Кит предлагает — решает человек.
        "note": "предложение; работа не станет активной без твоего решения",
    }


def _metric_name(insight: dict, contract: dict | None) -> str:
    """Имя основной метрики в ПРОДУКТОВЫХ словах. Из контракта, иначе — из заголовка инсайта."""
    if isinstance(contract, dict):
        name = _text((contract.get("primary_metric") or {}).get("name"))
        if name:
            return name
    # headline имеет вид «metric»: …; достаём metric без служебных кавычек.
    return _text((insight.get("headline") or "").strip("«").split("»")[0]) or _text(insight.get("outcome_id"))


def _because(insight: dict, evaluation: dict, readout: dict | None, contract: dict | None) -> str:
    """Обоснование «потому что <что произошло с продуктом>» — ТОЛЬКО из снятого замера и разрешённой
    гипотезы. Никаких внутренних терминов в лицо человеку (не «baseline», а «старт»); числа — те же,
    что посчитал evaluate_outcome, ничего не выдумано. Гипотеза попадает в обоснование, лишь если она
    реально разрешилась (confirmed/refuted); inconclusive/отсутствует — молчим, а не досочиняем."""
    metric = _metric_name(insight, contract)
    verdict = insight.get("verdict")
    b, m, t = evaluation.get("baseline"), evaluation.get("measured"), evaluation.get("target")
    breaches = list(evaluation.get("guardrail_breaches") or [])
    parts: list[str] = []
    if verdict == "met":
        parts.append(f"метрика «{metric}» дошла до {m} при цели {t} (старт {b})")
    elif breaches:
        # цель провалена соседней метрикой — это и есть «что произошло с продуктом».
        parts.append(f"защитная метрика просела ({', '.join(breaches)}), "
                     f"а «{metric}» была на {m} при цели {t}")
    elif m is not None and m == b:
        parts.append(f"метрика «{metric}» не сдвинулась (осталась {b}) и цель {t} не взяла")
    else:
        parts.append(f"метрика «{metric}» сдвинулась с {b} до {m}, но цель {t} не взяла")
    hyp = _text((readout or {}).get("hypothesis"))
    if hyp == "confirmed":
        parts.append("гипотеза подтвердилась")
    elif hyp == "refuted":
        parts.append("гипотеза не подтвердилась")
    return "; ".join(parts)


def next_action(insight: dict | None, candidate: dict | None, evaluation: dict | None,
                readout: dict | None = None, contract: dict | None = None) -> dict | None:
    """ПОСЛЕДНИЙ шаг петли — КОНКРЕТНОЕ следующее действие с обоснованием от произошедшего с продуктом.

    Замыкает measure → learn → next decision: САМО действие берётся из кандидата (тот же DRAFT,
    writer ≠ judge — активной работой без решения человека не станет), а обоснование `because` —
    ТОЛЬКО из снятого замера и разрешённой гипотезы (`_because`), продуктовым языком.

    -> None, если инсайта/кандидата/замера нет (`unknown` — данных для «что дальше» нет; причину
    называет `no_insight_reason`). Причину не сочиняем: не разрешилась гипотеза — её в `because` нет.
    """
    if not isinstance(insight, dict) or not isinstance(candidate, dict) or not isinstance(evaluation, dict):
        return None
    return {
        "schema_version": 1,
        "kind": "NextAction",
        "verdict": insight.get("verdict"),
        "action": candidate.get("title"),            # конкретная следующая работа
        "owner_role": candidate.get("owner_role"),
        "candidate_id": candidate.get("id"),
        "because": _because(insight, evaluation, readout, contract),
        "confidence": insight.get("confidence"),
    }


def from_outcome(contract: dict | None, readout: dict | None,
                 evaluation: dict | None) -> dict | None:
    """Полная петля одним вызовом: Outcome(+замер) -> {insight, candidate_work, recommendation,
    next_action}.

    -> None, если вердикт `unknown` (нет данных — инсайт не фабрикуется; причину даёт
    `no_insight_reason`). Чистая проекция: ничего не пишет, кандидат — DRAFT (writer ≠ judge).
    `next_action` — последний шаг петли: конкретное действие + «потому что <что произошло с продуктом>»,
    ровно то, что кит показывает в `next`."""
    insight = derive_insight(contract, readout, evaluation)
    if insight is None:
        return None
    candidate = propose_candidate_work(insight, contract, readout)
    recommendation = build_recommendation(insight, candidate)
    action = next_action(insight, candidate, evaluation, readout, contract)
    return {"insight": insight, "candidate_work": candidate, "recommendation": recommendation,
            "next_action": action}
