#!/usr/bin/env python3
"""Карта gate_id -> короткая ПРОДУКТОВАЯ фраза «что проверено» + честный свод для readout.

Живёт в foundation (`shared`), а НЕ в `ui` или `gates`, потому что её читают ДВА пакета выше по
дереву импортов: `ui` (presenter_formatters.from_review — путь `review`) и `engine`
(pipeline_stages — путь ПРОГОНА). Оба импортируют её ВНИЗ (ui -> shared, engine -> shared), и ни
один инвариант слоёв не нарушен. Положить карту в `ui` означало бы восходящий импорт для engine
(запрещён: engine НЕ импортирует ui), в `gates` — восходящий для ui (ui в слое ниже gates). shared
не импортирует из кита НИЧЕГО: это чистая таблица + чистый форматтер над обычным dict.

#958 (verified_is_shown_as_trust_not_gate_list): в момент «Проверено» человек видит ОСНОВАНИЕ
доверять — какие продуктовые измерения приняты и КЕМ (детерминированная машина vs независимый
ревьюер), — а не число «гейты 3/3» и не внутренний id гейта. Ядро кита — «no false green»:
называем ТОЛЬКО реально пройденные измерения и НЕ выдаём мнение независимого ревьюера за машинную
проверку.
"""
from __future__ import annotations

# id гейта -> короткая ПРОДУКТОВАЯ фраза (винительный падеж: «...проверила/принял: X»). Ключи —
# РЕАЛЬНЫЕ гейты quality/gates.yaml. Гейта нет в карте -> смысл НЕ выдумываем: пропуск, свод падает
# на нейтральную формулировку. Две группы соответствуют двум источникам доверия:
#   машинные (validator: детерминированный CLI/чек)  и  ai-review (заключение независимого судьи).
GATE_PRODUCT_DIMENSION = {
    # ── машинные (детерминированный валидатор: тесты / сборка / схема / CI) ──
    "intake_completeness": "полноту заявки",
    "requirements": "требования",
    "specification": "спецификацию",
    "plan_readiness": "готовность плана",
    "implementation_verification": "реализацию: тесты и сборку",
    "regression_test_evidence": "регрессионные тесты",
    "concurrency_preflight": "конфликты параллельной работы",
    "contour_consistency": "согласованность продуктовой модели",
    "own_medicine": "самопроверку кита",
    "event_contract_consistency": "контракт событий",
    "surface_wiring_consistency": "проводку поверхностей",
    "feature_coverage": "охват функций",
    "deploy_readiness": "готовность к развёртыванию",
    "spec_synchronization": "синхронность спецификации",
    "archive_readiness": "готовность к архивации",
    "documentation_updated": "обновление документации",
    "feature_decision_quality": "качество решений по функциям",
    "knowledge_integrity": "целостность знаний",
    "knowledge_freshness": "свежесть знаний",
    # ── ai-review (заключение независимого ревьюера; writer ≠ judge) ──
    "code_review": "код",
    "security": "безопасность",
    "architecture_review": "архитектуру",
    "analytics_design_readiness": "контракт аналитики",
    "analytics_runtime_verification": "поступление аналитики",
    "ux_review": "пользовательский опыт",
    "accessibility_review": "доступность",
    "visual_regression": "визуальную регрессию",
    "design_system_usage": "следование дизайн-системе",
    "ai_eval": "качество AI-функции",
    "ai_red_team": "устойчивость AI-функции к атакам",
    "decision_quality": "качество продуктового решения",
    "release_safety": "безопасность выпуска",
    "observability_readiness": "наблюдаемость",
    "evidence": "доказательную базу",
    "stakeholder_readiness": "готовность для заинтересованных сторон",
    "discovery_completeness": "полноту discovery",
}


def dimensions(gate_ids: list[str] | None) -> list[str]:
    """Продуктовые фразы для набора id — пропуская незнакомые (смысл не выдумываем)."""
    return [d for gid in (gate_ids or []) for d in (GATE_PRODUCT_DIMENSION.get(gid),) if d]


def run_verified_lines(evidence_verdict: dict) -> list:
    """Свод «что проверено» ПРОДУКТОВЫМИ словами для readout ПРОГОНА -> список строк (может быть пуст).

    Источник различаем ЧЕСТНО по `evidence_verdict` (веха 4.2, #588): `deterministic` — пройденные
    машинные гейты (валидатор: тест/сборка/схема), `ai_judgment` — пройденные по заключению
    НЕЗАВИСИМОГО ревьюера, `human` — одобренные человеком. Каждый список — ТОЛЬКО пройденные
    (status=pass) гейты, поэтому инвариант «no false green» держится: непройденное сюда не попадает,
    а мнение независимого ревьюера машинной проверкой не зовётся.

    Возвращает пусто, если пройденных гейтов нет: тогда вызывающий НЕ пишет «Проверено» — верифи-
    цировать нечего. Незнакомый пройденный гейт не выдумывает смысл: группа остаётся, но называется
    нейтрально («... проверки пройдены»), а не приписывает измерение, которого в карте нет.
    """
    ev = evidence_verdict or {}
    det = ev.get("deterministic") or []
    aij = ev.get("ai_judgment") or []
    hum = ev.get("human") or []
    lines = []
    if det:
        d = dimensions(det)
        lines.append("машина проверила (тесты, сборка, схемы): "
                     + (", ".join(d) if d else "детерминированные проверки пройдены"))
    if aij:
        d = dimensions(aij)
        lines.append("независимый ревьюер (не тот, кто делал работу) принял: "
                     + (", ".join(d) if d else "замечаний нет"))
    if hum:
        d = dimensions(hum)
        lines.append("человек одобрил: " + (", ".join(d) if d else "получено ручное одобрение"))
    return lines


# ── «Verified» как УРОВЕНЬ ЗНАНИЯ, а не счёт зелёных галочек (P0 №5 продуктового ревью, #982) ──
#
# «Проверено» обязано описывать, ЧТО МЫ ЗНАЕМ о работе, а не сколько гейтов зелёные. Не «8 из 8
# гейтов», а хребет НАЗВАННЫХ уровней знания, каждый с ЧЕСТНЫМ состоянием: известно / ещё
# неизвестно / измерено-и-нет. Это прямое следствие инварианта ядра «no evidence → no claim»:
# недоказанное называется недоказанным. Счётчик молчал о том, чего кит ещё не знает (доехал ли
# деплой, идут ли события, взял ли продукт цель) — а именно это человеку и важно перед выпуском.
#
# Уровень становится «известно» ТОЛЬКО когда его источник РЕАЛЬНО пройден (гейт в evidence_verdict
# как пройденный, либо измеренный outcome). Нет источника → уровень честно «ещё неизвестно»; смысл
# не выдумываем и «проверено» вместо «не проверено» не пишем.

LEVEL_KNOWN = "known"        # источник пройден — знание есть
LEVEL_UNKNOWN = "unknown"    # источника нет / не пройден — честно «ещё неизвестно»
LEVEL_NOT_MET = "not_met"    # источник ЕСТЬ и говорит «нет» (напр. измеренный outcome цель не взял)

# Хребет уровней знания. Порядок = путь работы от кода к продуктовому результату. Поля:
#   key           — стабильный идентификатор уровня (в лицо человеку не идёт);
#   known_text    — формулировка, когда уровень ДОСТИГНУТ;
#   unknown_text  — честная формулировка «ещё неизвестно» (для сводной строки про непройденное);
#   gates         — пройденные гейты-источники, закрывающие уровень (пусто -> особый источник).
# Атрибуция доверия вшита в формулировку: «независимым ревьюером» там, где источник — ai-review
# (writer ≠ judge), а не детерминированная машина. Так уровень не выдаёт мнение за машинную проверку.
KNOWLEDGE_LEVELS = (
    {"key": "implemented",
     "known_text": "Функция реализована.",
     "unknown_text": "реализация ещё не подтверждена",
     "gates": ("implementation_verification",)},
    {"key": "reviewed",
     "known_text": "Код проверен независимым ревьюером.",
     "unknown_text": "код ещё не смотрел независимый ревьюер",
     "gates": ("code_review",)},
    {"key": "critical_scenarios",
     "known_text": "Критические сценарии прошли.",
     "unknown_text": "критические сценарии ещё не проверены",
     "gates": ("regression_test_evidence",)},
    {"key": "analytics_wired",
     "known_text": "Аналитика подключена.",
     "unknown_text": "аналитика ещё не подключена",
     "gates": ("analytics_design_readiness",)},
    {"key": "deploy_checked",
     "known_text": "Развёртывание проверено.",
     "unknown_text": "развёртывание ещё не проверено",
     "gates": ("deploy_readiness",)},
    {"key": "events_flow",
     "known_text": "После релиза события в аналитику поступают.",
     "unknown_text": "поступление событий после релиза ещё не подтверждено",
     "gates": ("analytics_runtime_verification",)},
    {"key": "product_outcome",
     "known_text": "Продуктовый результат подтверждён измерением.",
     "unknown_text": "продуктовый результат ещё не накоплен",
     "not_met_text": "Продуктовый результат измерен — цель пока не взята.",
     "gates": ()},  # источник — измеренный outcome_verdict, а не гейт
)

# Гейты, уже названные ХРЕБТОМ. Пройденные гейты вне этого множества (безопасность, архитектура,
# требования и т.д.) не теряются: их доносит дополнительная строка с честной атрибуцией источника.
_SPINE_GATES = frozenset(g for lv in KNOWLEDGE_LEVELS for g in lv["gates"])


def knowledge_levels(evidence_verdict: dict, *, outcome_verdict: str | None = None) -> list:
    """Хребет уровней знания с ЧЕСТНЫМ состоянием каждого -> список dict {key, state, ...}.

    `evidence_verdict` (веха 4.2, #588) уже делит пройденные гейты по источнику доверия;
    здесь важен сам факт «гейт пройден», поэтому берём объединение всех трёх списков. Уровень
    `product_outcome` резолвится не из гейта, а из ИЗМЕРЕННОГО `outcome_verdict` (`met`/`failed`);
    без измерения — честно «ещё неизвестно», а не «достигнут». Инвариант «no false green»: ни один
    уровень не становится «известно» без реально пройденного источника.
    """
    ev = evidence_verdict or {}
    passed = (set(ev.get("deterministic") or [])
              | set(ev.get("ai_judgment") or [])
              | set(ev.get("human") or []))
    out = []
    for lv in KNOWLEDGE_LEVELS:
        if lv["key"] == "product_outcome":
            if outcome_verdict == "met":
                state = LEVEL_KNOWN
            elif outcome_verdict == "failed":
                state = LEVEL_NOT_MET
            else:
                state = LEVEL_UNKNOWN
        else:
            state = LEVEL_KNOWN if any(g in passed for g in lv["gates"]) else LEVEL_UNKNOWN
        out.append({"key": lv["key"], "state": state,
                    "known_text": lv["known_text"], "unknown_text": lv["unknown_text"],
                    "not_met_text": lv.get("not_met_text")})
    return out


def knowledge_readout(evidence_verdict: dict, *, outcome_verdict: str | None = None) -> list:
    """«Проверено» уровнями знания -> список человеко-строк (продуктовый язык, без жаргона).

    Известные уровни идут утверждениями; измеренный-и-непройденный — своей строкой; всё ещё
    неизвестное СОБИРАЕТСЯ в одну честную строку «Что пока неизвестно: …», чтобы «Проверено» не
    выглядело более уверенным, чем заслужено. Пройденные измерения вне хребта (безопасность,
    архитектура и пр.) не теряются: их доносит дополнительная строка с честной атрибуцией источника
    (машина / независимый ревьюер / человек), а мнение машинной проверкой по-прежнему не зовётся.
    """
    ev = evidence_verdict or {}
    levels = knowledge_levels(ev, outcome_verdict=outcome_verdict)
    lines = []
    for lv in levels:
        if lv["state"] == LEVEL_KNOWN:
            lines.append(lv["known_text"])
        elif lv["state"] == LEVEL_NOT_MET and lv["not_met_text"]:
            lines.append(lv["not_met_text"])
    unknown = [lv["unknown_text"] for lv in levels if lv["state"] == LEVEL_UNKNOWN]
    if unknown:
        lines.append("Что пока неизвестно: " + "; ".join(unknown) + ".")

    # Пройденное вне хребта — не теряем, но и не выдумываем смысл незнакомым гейтам (dimensions
    # пропускает те, которых нет в карте). Источник называем честно: мнение ≠ машинная проверка.
    det = dimensions([g for g in (ev.get("deterministic") or []) if g not in _SPINE_GATES])
    aij = dimensions([g for g in (ev.get("ai_judgment") or []) if g not in _SPINE_GATES])
    hum = dimensions([g for g in (ev.get("human") or []) if g not in _SPINE_GATES])
    if det:
        lines.append("Машина также проверила: " + ", ".join(det) + ".")
    if aij:
        lines.append("Независимый ревьюер также принял: " + ", ".join(aij) + ".")
    if hum:
        lines.append("Человек также одобрил: " + ", ".join(hum) + ".")
    return lines


def knowledge_has_known(evidence_verdict: dict, *, outcome_verdict: str | None = None) -> bool:
    """Есть ли хоть один ДОСТИГНУТЫЙ уровень знания — чтобы вызывающий не писал «известно» впустую."""
    return any(lv["state"] == LEVEL_KNOWN
               for lv in knowledge_levels(evidence_verdict, outcome_verdict=outcome_verdict))
