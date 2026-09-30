#!/usr/bin/env python3
"""decision_candidates.py — третий источник задач-кандидатов: НЕДОБРАВШИЕ ФИЧИ как продуктовые РЕШЕНИЯ.

ПОВОД (направление product-next-as-decision-engine, T3). Кит уже нарезает кандидатов из двух мест:
непокрытые направления роадмапа (`roadmap_candidates`) и наблюдения дочек (`child_findings`). Оба —
про РАБОТУ, которую можно завести. Но самый ценный сигнал петли исхода пропадал: фича, чей замер
итога НЕ ДОТЯНУЛ (вердикт `failed` по числам, промах цели) или чья гипотеза НЕ подтвердилась, никуда
не втекала в очередь владельца как ПРОДУКТОВОЕ РЕШЕНИЕ. Кит замыкает петлю здесь: недобравший исход
поднимается не техзадачей («почини метрику»), а решением — «решить следующий шаг по фиче X, потому
что <что произошло с продуктом>». Дальше решает человек.

ЧТО ТАКОЕ «РЕШЕНИЕ НАЗРЕЛО» (иначе кандидат превратился бы в шум на каждой фиче):
  * ВЕРДИКТ `failed` НА РЕАЛЬНОМ ЗАМЕРЕ — метрика не взяла цель / просела защитная метрика
    (`is_real_measurement=True`). Это недобор, подтверждённый числами, а не мнением;
  * ГИПОТЕЗА НЕ ПОДТВЕРЖДЕНА — отчёт закрыл гипотезу как `refuted`/`inconclusive`. Даже если
    целевая метрика формально дотянула, опровергнутая гипотеза — это решение, которое нельзя не
    принять: продолжать, менять или откатывать.
`met` + цель взята + гипотеза подтверждена/не заявлена → решать нечего, кандидата НЕТ.

ЧЕСТНЫЕ ГРАНИЦЫ (те же три правила, что у outcome_insight/roadmap_candidates):
  * «ЕЩЁ НЕ ИЗМЕРЕНО» ≠ «РЕШАТЬ НЕЧЕГО». Контракт исхода есть, а отчёта (замера) нет — это НЕ
    «met» и НЕ повод для решения: кандидата нет, но фича попадает в `not_measured` с честной
    причиной. Молчаливо выдать неизмеренное за «всё хорошо» — ровно то, против чего кит.
  * WRITER ≠ JUDGE. Кандидат рождается DRAFT: `status: draft`, `active: false`,
    `requires_human_decision: true`. Модуль НИЧЕГО не пишет (ни plan.yaml, ни active-work) — он
    ПРОЕЦИРУЕТ решение. Активной работой оно станет только по приёмке владельцем (`candidates accept`).
  * ОБОСНОВАНИЕ — ИЗ СНЯТОГО ЗАМЕРА, не из головы. `rationale` несёт «потому что <сдвиг метрики
    baseline→value против цели / опровергнутая гипотеза>», выведенное `outcome_insight`. Никаких
    выдуманных чисел: то, что посчитал `evaluate_outcome`, — и ничего сверх.

ГДЕ ОН ЖИВЁТ И ПОЧЕМУ ЧИСТЫЙ. Пакет `intelligence` (слой ВЫШЕ ядра). Импортирует ТОЛЬКО
`outcome_insight` (тот же слой) — ради инсайта, «следующего действия» и обоснования. Он НЕ загружает
файлы и НЕ импортирует `validation.evaluate_outcome`: это была бы зависимость ВВЕРХ (validation —
entrypoints). Поэтому проектор ПРИНИМАЕТ уже загруженные и посчитанные входы `(feature, contract,
readout, evaluation)` — загрузку (`features/<id>/outcome-{contract,readout}.yaml`) и вердикт
(`evaluate_outcome`) делает слой CLI (`candidates_cli`), ровно как `candidate_intake` принимает уже
собранный список кандидатов. Объединение с двумя другими источниками (`gather_candidates`) — тоже CLI.
"""
from __future__ import annotations

from typing import Iterable

from ai_ops_kit.intelligence import outcome_insight

# Состояния гипотезы, при которых решение назрело: гипотеза НЕ подтверждена (в отличие от `confirmed`).
_HYPOTHESIS_UNCONFIRMED = ("refuted", "inconclusive")


def _text(v) -> str:
    return str(v or "").strip()


def _decision_reason(readout: dict, evaluation: dict) -> str | None:
    """Назрело ли решение по этой фиче. -> код повода | None (решать нечего).

    `underperformed` — вердикт `failed` на РЕАЛЬНОМ замере (недобор подтверждён числами).
    `hypothesis_unconfirmed` — отчёт закрыл гипотезу как refuted/inconclusive (решение назрело даже
    при формально взятой цели). Оба сигнала независимы: провал по числам ИЛИ неподтверждённая гипотеза.
    """
    ev = evaluation if isinstance(evaluation, dict) else {}
    if ev.get("verdict") == "failed" and bool(ev.get("is_real_measurement")):
        return "underperformed"
    hyp = _text((readout or {}).get("hypothesis")) if isinstance(readout, dict) else ""
    if hyp in _HYPOTHESIS_UNCONFIRMED:
        return "hypothesis_unconfirmed"
    return None


def _hypothesis_action_because(readout: dict, evaluation: dict) -> tuple[str, str]:
    """Действие+обоснование для повода `hypothesis_unconfirmed` — ПРО ГИПОТЕЗУ, а не поздравление.

    Когда цель ЧИСЛЕННО взята (verdict met), успешная ветка insight дала бы «Закрепить результат…» и
    «метрика дошла до цели» — это поздравление. Но решение назрело именно потому, что гипотеза НЕ
    подтвердилась, и заголовок обязан быть решением про неё, а не про успех. Обоснование честно несёт
    оба факта: гипотеза опровергнута/не разрешилась И была ли цель формально взята. Числа не выдумываем.
    -> (action, because)."""
    hyp = _text((readout or {}).get("hypothesis"))
    ev = evaluation if isinstance(evaluation, dict) else {}
    met_formally = ev.get("verdict") == "met" and bool(ev.get("is_real_measurement"))
    if hyp == "refuted":
        action = "разобрать опровергнутую гипотезу и решить следующий шаг по фиче"
        state = "гипотеза продукта опровергнута отчётом"
    else:  # inconclusive
        action = "разобрать неразрешённую гипотезу и решить следующий шаг по фиче"
        state = "гипотеза продукта не разрешилась по отчёту (inconclusive)"
    tail = ("хотя целевая метрика формально взята" if met_formally
            else "а целевой замер ещё не сравнить с целью")
    return action, f"{state}, {tail}"


def _feature_name(contract: dict, feature_id: str) -> str:
    """Человеческое имя фичи для заголовка. Сам id фичи — он и есть «фича X»."""
    return _text(feature_id) or "фича"


def _decision_candidate(feature_id: str, contract: dict, readout: dict,
                        evaluation: dict, reason: str) -> dict:
    """Недобравший исход → кандидат-РЕШЕНИЕ (DRAFT). Заголовок оформлен как РЕШЕНИЕ, а не техзадача;
    `rationale` несёт «потому что <что произошло с продуктом>» из `outcome_insight`."""
    feature = _text(feature_id) or "feature"
    name = _feature_name(contract, feature)

    if reason == "hypothesis_unconfirmed":
        # Повод — НЕПОДТВЕРЖДЁННАЯ ГИПОТЕЗА (цель могла быть формально взята). Действие/обоснование —
        # ПРО ГИПОТЕЗУ, НЕЗАВИСИМО от того, что insight успешной ветки не None: успешный next_action
        # («Закрепить результат…», «метрика дошла до цели») здесь читался бы поздравлением, а не решением.
        action, because = _hypothesis_action_because(readout, evaluation)
        confidence = None
    else:
        # `underperformed`: реальный `failed` → insight-путь верен (обоснование про недобор из петли
        # исхода, «потому что метрика … цель не взяла»). derive_insight на failed инсайт даёт всегда.
        insight = outcome_insight.derive_insight(contract, readout, evaluation)
        if insight is not None:
            base = outcome_insight.propose_candidate_work(insight, contract, readout)
            na = outcome_insight.next_action(insight, base, evaluation, readout, contract) or {}
            action = _text(na.get("action")) or "решить следующий шаг по фиче"
            because = _text(na.get("because"))
            confidence = insight.get("confidence")
        else:
            action = "разобрать недобравший итог и решить следующий шаг по фиче"
            because = _text((evaluation or {}).get("reason"))
            confidence = None

    rationale = (f"решение по фиче назрело, потому что {because}" if because else
                 "решение по фиче назрело: отчёт снят, но целевой замер ещё не сравнить с целью")
    cand = {
        "schema_version": 1,
        "kind": "CandidateWork",
        "id": f"cand-decision-{feature}",
        # Оформлено РЕШЕНИЕМ, а не техзадачей: «решить следующий шаг», а конкретное действие — внутри.
        "title": f"Решить следующий шаг по фиче «{name}»: {action}",
        "type": "decision",               # DRAFT-метка; приёмка переводит её в словарь плана
        "owner_role": "product-manager",
        "status": "draft",                # предложение, а не работа
        "active": False,                  # не занимает область записи
        "requires_human_decision": True,  # не станет активной без решения человека
        "source": "outcome-decision",     # источник кандидата (ярлык во входящих/приёмке)
        "source_feature": feature,
        "decision_reason": reason,        # underperformed | hypothesis_unconfirmed (для показа)
        "rationale": rationale,
    }
    goal = _text(contract.get("goal")) or None
    if goal:
        cand["source_goal"] = goal
    if confidence:
        cand["confidence"] = confidence
    return cand


def _unpack(entry) -> tuple:
    """(feature, contract, readout, evaluation) из элемента входа. Кривой элемент → (None,)*4
    (не бросаем: обратный канал обогащает очередь, а не является её предусловием)."""
    if isinstance(entry, (list, tuple)) and len(entry) == 4:
        return entry[0], entry[1], entry[2], entry[3]
    return None, None, None, None


def project_decision_candidates(items: Iterable) -> dict:
    """READ-ONLY проекция недобравших исходов в задачи-кандидаты ПРОДУКТОВЫХ РЕШЕНИЙ.

    `items` — уже загруженные и посчитанные входы `(feature, contract, readout, evaluation)`
    (загрузку файлов и `evaluate_outcome` делает слой CLI — см. докстринг модуля). Проектор ЧИСТ:
    ничего не читает с диска, ничего не пишет и не бросает.

    -> {"schema_version": 1, "kind": "OutcomeDecisionCandidates", "considered": N,
        "candidates": [...], "not_measured": [{"feature", "reason"}]}.

    `candidates` — DRAFT-решения по фичам, где решение НАЗРЕЛО (недобор по числам ИЛИ неподтверждённая
    гипотеза). `not_measured` — фичи с контрактом, но БЕЗ отчёта: честное «ещё не измерено», а НЕ
    «решать нечего» (иначе неизмеренное молча читалось бы как «всё хорошо»).
    """
    candidates: list = []
    not_measured: list = []
    considered = 0
    for entry in (items or []):
        feature_id, contract, readout, evaluation = _unpack(entry)
        if not isinstance(contract, dict):
            continue                       # без контракта исхода нет — считать нечего
        considered += 1
        if not isinstance(readout, dict) or not readout:
            # Контракт есть, замер (outcome-readout) не снят — решать пока не по чему. Это «ещё не
            # измерено», а не «met» и не «решать нечего»: называем фичу и причину, кандидата НЕ даём.
            not_measured.append({
                "feature": _text(feature_id) or "feature",
                "reason": "контракт результата есть, а замер (outcome-readout) ещё не снят — "
                          "решать по этой фиче пока не по чему; это «ещё не измерено», а не «всё хорошо»",
            })
            continue
        reason = _decision_reason(readout, evaluation)
        if reason is None:
            # Решать нечего по гипотезе. Но «нечего» бывает двух РАЗНЫХ видов, и их нельзя смешивать:
            #   * замер РЕАЛЬНЫЙ и цель взята (verdict met) → продукт действительно ок — молчим;
            #   * замер снят, но НЕ сравним с целью (verdict unknown: нечисловой контракт или нет
            #     measured.value/measured_at) → это НЕ «ок», а «ещё не измерено». Молча пропустить
            #     значило бы выдать неизмеренное за «всё хорошо» — ровно то, против чего T3.
            ev = evaluation if isinstance(evaluation, dict) else {}
            if not bool(ev.get("is_real_measurement")):
                not_measured.append({
                    "feature": _text(feature_id) or "feature",
                    "reason": "замер снят, но не сравним с целью — нечисловой контракт или нет "
                              "measured.value/measured_at; это «ещё не измерено», а не «всё хорошо»",
                })
            continue
        candidates.append(_decision_candidate(feature_id, contract, readout, evaluation, reason))
    return {"schema_version": 1, "kind": "OutcomeDecisionCandidates",
            "considered": considered, "candidates": candidates, "not_measured": not_measured}
