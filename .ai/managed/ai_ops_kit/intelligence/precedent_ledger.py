#!/usr/bin/env python3
"""precedent_ledger.py — обучение как РЕЕСТР ПРЕЦЕДЕНТОВ с доказательствами, НЕ «мудрость» (#586).

ПОВОД (клин вехи 4.2, обратная связь 07.09.2026, стратегический п.5). Перенос уроков между
продуктами — НЕрешённая проблема, а не фича. Риск-вилка: либо «урок» настолько общий, что бесполезен
(«пишите тесты»), либо переобучен под частности и вреден. Причинность vs корреляция на выборке из
трёх — ловушка. Поэтому строим САМЫЙ ДЕШЁВЫЙ ЧЕСТНЫЙ вариант: кит показывает ФАКТ, В СКОЛЬКИХ СЛУЧАЯХ
он встречался и В КАКИХ КОНТЕКСТАХ — и оставляет вывод человеку.

ЧТО ТАКОЕ ПРЕЦЕДЕНТ. Запись `{pattern, evidence, case_count, contexts, source, confidence, note}`:
  * `pattern`    — короткое имя повторяющегося наблюдения (напр. класс наблюдения дочек);
  * `evidence`   — список конкретных ссылок (id + утверждение + контекст): чем подтверждён паттерн;
  * `case_count` — В СКОЛЬКИХ случаях паттерн встречался. Это ЧИСЛО, а не сила причинности;
  * `contexts`   — в каких контекстах (имена дочек/релизов) — чтобы «видел в N случаях в контекстах X»;
  * `source`     — простая метка происхождения записи (child-observations / release-outcome). При
                   слиянии сводится с меткой «детерминированное vs AI-суждение» (#588) — здесь не
                   изобретаем её глобально, кладём простое поле;
  * `confidence` — метка ЧАСТОТЫ (single-case / few-cases / recurring), НЕ сила вывода. Считается
                   ТОЛЬКО из `case_count`;
  * `note`       — явная дисклеймер-строка: это прецедент, не правило.

ТРИ ЧЕСТНЫХ ИНВАРИАНТА (иначе реестр превращается в генератор «мудрости»):

  1. НЕТ ПОЛЯ ПРИЧИННОСТИ/ПЕРЕНОСИМОСТИ. Запись НЕ несёт `causation`, `transferable`, `rule`,
     `recommendation` — ничего, что утверждало бы «отсюда следует» или «перенесётся на другой
     продукт». Переносимость — это ГИПОТЕЗА человека поверх фактов, а не поле кита.

  2. НА ВЫБОРКЕ 1 — «ПРЕЦЕДЕНТ, 1 СЛУЧАЙ», НЕ «ПРАВИЛО». `confidence` считается из `case_count`:
     1 → single-case, 2..4 → few-cases, ≥5 → recurring. Это замер ЧАСТОТЫ, а не уверенности в
     причине: пять одинаковых случаев — это пять случаев, а не доказанная закономерность.

  3. КИТ НЕ РЕШАЕТ. Реестр — ДАННЫЕ (факты + число случаев + метка частоты). Вывод и решение
     остаются за человеком; `note` говорит это прямым текстом.

ГДЕ ОН ЖИВЁТ. Пакет `intelligence` (слой выше ядра). Модуль ЧИСТЫЙ: только группирует и считает,
НИЧЕГО не пишет и не импортирует `validation`. Наблюдения дочек ему передают уже загруженными
(`child_findings`), измеренный инсайт — уже посчитанным (`post_release_loop`). Это ПРОЕКЦИЯ, а не
запись в граф — как `outcome_insight` проецирует инсайт, а не пишет его.
"""
from __future__ import annotations

# Метки частоты — из ЧИСЛА случаев, не из «уверенности». Границы намеренно грубые: точную кривую
# доверия на выборке из единиц строить нечестно (ровно ловушка «причинность на трёх»).
_SINGLE = "single-case"
_FEW = "few-cases"
_RECURRING = "recurring"

# Человекочитаемые метки частоты для владельца (product-аудитория): без внутренней лексики.
FREQUENCY_LABEL_RU = {
    _SINGLE: "прецедент, 1 случай",
    _FEW: "несколько случаев",
    _RECURRING: "повторяющийся паттерн",
}

# Дисклеймер в каждой записи — прямым текстом, чтобы «прецедент» не читался как «правило».
_NOTE = ("прецедент, не правило: кит показывает факт и число случаев, "
         "но не утверждает причинность или переносимость — вывод за тобой")


def _text(v) -> str:
    return str(v or "").strip()


def confidence_from_cases(case_count: int) -> str:
    """Метка ЧАСТОТЫ из числа случаев. 1 → single-case (не «правило»), 2..4 → few, ≥5 → recurring.

    Это ЗАМЕР ЧАСТОТЫ, а не сила причинности: инвариант 2. Ноль/отрицательное — тоже single-case
    (запись без случаев не строим, но метка не должна падать)."""
    n = int(case_count or 0)
    if n >= 5:
        return _RECURRING
    if n >= 2:
        return _FEW
    return _SINGLE


def make_precedent(pattern: str, evidence: list[dict], contexts: list[str], source: str) -> dict:
    """Собрать одну запись-прецедент. case_count = len(evidence). Без поля причинности (инвариант 1).

    `evidence` — список {id?, statement, context?}; `contexts` — различные контексты (имена дочек/
    релизов). Запись НЕ несёт вывода/рекомендации/переносимости — только факты и их число."""
    ev = [e for e in (evidence or []) if isinstance(e, dict)]
    case_count = len(ev)
    conf = confidence_from_cases(case_count)
    return {
        "schema_version": 1,
        "kind": "Precedent",
        "pattern": _text(pattern) or "прецедент",
        "evidence": ev,
        "case_count": case_count,
        "contexts": sorted({_text(c) for c in (contexts or []) if _text(c)}),
        "source": _text(source) or "unknown",
        "confidence": conf,                 # метка ЧАСТОТЫ, не силы вывода (инвариант 2)
        "frequency_label": FREQUENCY_LABEL_RU[conf],
        "note": _NOTE,                      # инвариант 3: решает человек
    }


def _observation_context(obs: dict) -> str:
    """Контекст наблюдения — имя дочки (или путь). «в каких контекстах X» из инварианта."""
    child = obs.get("child") if isinstance(obs.get("child"), dict) else {}
    return _text(child.get("name")) or _text(child.get("path")) or "неизвестный контекст"


def _observation_evidence(obs: dict) -> dict:
    """Одно наблюдение → элемент evidence: id + утверждение + контекст. Конкретная ссылка, не вывод."""
    return {
        "id": _text(obs.get("id")) or None,
        "statement": _text(obs.get("statement")),
        "context": _observation_context(obs),
        "severity": _text(obs.get("severity")) or None,
        "state": _text(obs.get("state")) or None,
    }


def _pattern_key(obs: dict) -> str:
    """Ключ группировки прецедента: класс наблюдения (defect/friction/…). Грубо намеренно — тонкая
    кластеризация «по смыслу» потребовала бы модели и переобучилась бы (граница вехи: без нового ML)."""
    return _text(obs.get("observation_class")) or "наблюдение"


# Человекочитаемые имена паттернов для владельца (по классу наблюдения дочек).
_PATTERN_LABEL_RU = {
    "defect": "дефект в поведении кита",
    "friction": "трение в работе с китом",
    "наблюдение": "наблюдение из прогона",
}


def precedents_from_observations(observations: list[dict], *,
                                 source: str = "child-observations") -> list[dict]:
    """Наблюдения дочек → список прецедентов, сгруппированных по паттерну (классу наблюдения).

    На КАЖДЫЙ паттерн — одна запись: `case_count` = сколько наблюдений его подтверждают, `contexts`
    = различные дочки. Кит НЕ утверждает, что паттерн «доказан» — показывает, в скольких случаях и
    где видел (инварианты). Пусто/не-список → []. Порядок — по убыванию числа случаев, затем по имени
    (детерминированно), чтобы владелец видел самое частое сверху, но БЕЗ вывода «значит важнее»."""
    if not isinstance(observations, list):
        return []
    groups: dict[str, list[dict]] = {}
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        groups.setdefault(_pattern_key(obs), []).append(obs)

    out: list[dict] = []
    for key, obs_list in groups.items():
        evidence = [_observation_evidence(o) for o in obs_list]
        contexts = [_observation_context(o) for o in obs_list]
        label = _PATTERN_LABEL_RU.get(key, key)
        out.append(make_precedent(label, evidence, contexts, source))
    out.sort(key=lambda p: (-p["case_count"], p["pattern"]))
    return out


def precedent_from_insight(insight: dict | None, *, contract: dict | None = None,
                           source: str = "release-outcome") -> dict | None:
    """Измеренный инсайт (#567) → прецедент 1 случая. None, если инсайта нет (на `unknown` его и не будет).

    Один живой релиз с реальным замером = ОДИН случай. На выборке 1 запись честно несёт case_count=1
    и метку single-case («прецедент, 1 случай»), а НЕ «правило» (инвариант 2). evidence — факты из
    эпистемики инсайта (observed), контекст — метрика/исход. Причинности запись не утверждает."""
    if not isinstance(insight, dict) or not insight.get("verdict"):
        return None
    oid = _text(insight.get("outcome_id")) or "outcome"
    headline = _text(insight.get("headline")) or oid
    epi = insight.get("epistemics") if isinstance(insight.get("epistemics"), dict) else {}
    observed = [_text(x) for x in (epi.get("observed") or []) if _text(x)]
    evidence = [{
        "id": _text(insight.get("id")) or None,
        "statement": headline,
        "context": oid,
        "verdict": _text(insight.get("verdict")),
        "observed": observed,
    }]
    pattern = f"исход релиза: {headline}"
    return make_precedent(pattern, evidence, [oid], source)
