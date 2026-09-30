#!/usr/bin/env python3
"""post_release_loop.py — ЕДИНЫЙ пост-релизный путь: PRR → verify_analytics_runtime → outcome → verdict.

ПОВОД (#545, «замкнуть outcome-loop»). Все звенья пост-релизной петли уже ПОСТРОЕНЫ, но стоят
рядом и никто не соединяет их в один вызов — «built ≠ wired»:

  * `intelligence/event_arrival.verify_analytics_runtime` — producer→гейт: машинный вердикт
    `events_verified_live` из выгрузки поступления событий. Звался только из `__main__`/тестов.
  * `validation/validate_post_release_readout.check` — валидатор PRR: читает только
    ВЕРИФИЦИРОВАННУЮ доставку (`delivery_receipt.sha_verified=true`) и сверяет `readout_decision`
    с сигналами. Звался только как отдельный процесс.
  * `validation/validate_product_objects.project_outcome` / `trace_feature_rationale` — проекция
    OutcomeContract(+Readout) в фрагмент графа и трассировка «зачем существует функция». Не
    звались нигде вне своего модуля.

Этот модуль — ТОНКИЙ ОРКЕСТРАТОР-ПРОВОДКА: он НИЧЕГО не измеряет сам и не заводит нового рантайма.
Он лишь по очереди зовёт уже существующие функции и сводит их в один результат с одним вердиктом.

ГДЕ ОН ЖИВЁТ И ПОЧЕМУ. Оркестратор обязан звать И `intelligence` (event_arrival), И `validation`
(оба валидатора). По слоям (`packages/layering.yaml`) это может только слой `entrypoints`:
`intelligence` лежит НИЖЕ (зависимость вниз разрешена), `validation` — в том же слое `entrypoints`
(зависимость внутри слоя разрешена). Ни `lifecycle` (capabilities), ни `intelligence` не вправе
импортировать `validation` (это была бы зависимость ВВЕРХ). Поэтому проводка — в пакете `cli`,
который и без того уже композитит `intelligence` (см. health/replan/team в ai_ops_cli_intents).

ЧЕСТНЫЙ ДЕФОЛТ (инвариант `unavailable != zero`, как у event_arrival и учёта стоимости). Нет
выгрузки аналитики дочки — это `unknown`/`not_measured`/`watch`, НИКОГДА не «healthy»/«verified».
Отсутствие доказательства не выдаётся ни за успех, ни за провал.

ГЕЙТ ЗАКРЫВАЕТСЯ 1 ИЗ 4 — И ЭТО ПРАВИЛЬНО. Гейт `analytics_runtime_verification`
(`quality/gates.yaml`) требует ЧЕТЫРЕ доказательства:
`[events_verified_live, no_pii_in_events, cohort_identification_works, dashboard_receives_data]`.
Producer есть ТОЛЬКО у первого (`event_arrival`). Остальные три источника не имеют — и мы честно
помечаем их `not_measured`, а не выдумываем им producer'ы. Проводка сообщает «закрыто 1 из 4»
последствием, а не прячет пробел.

ВЕРДИКТ ПО ИТОГУ СЧИТАЕТСЯ ИЗ ЧИСЕЛ (#566). Когда после релиза приходит реальный замер (readout с
`measured.value`+`measured_at`), `outcome_verdict` флипается из `unknown` в `met`/`failed` —
`validate_product_objects.evaluate_outcome` сверяет baseline→measured против target и guardrails,
а НЕ берёт человеческое `target_met`. При зелёной доставке (валидный PRR) и провале итога
`product_status` явно говорит «технически done, продуктово нет».

САМ ФЛИП goal.outcome НЕ ДЕЛАЕТСЯ ЗДЕСЬ. Оркестратор ВОЗВРАЩАЕТ вердикт и выставляет
`outcome_flip_ready=True`, когда замер реален (met/failed), но `planning/plan.yaml` НЕ трогает:
запись факта в план — отдельный координационный PR (код петли ≠ правка данных). Без замера
`outcome_flip_ready` честно False.

Использование:
    post_release_loop.py [child_root] [--prr <файл>] [--contract <файл>] [--readout <файл>]
                         [--feature <id>] [--json]
"""
from __future__ import annotations

# Самодостаточный вход: положить корень пакета (маркер VERSION) в sys.path ДО пакетных импортов,
# чтобы файл можно было запустить и напрямую (как это делает ai_ops_cli).
import sys as _sys
from pathlib import Path as _P_bootstrap

_root = next((_p for _p in _P_bootstrap(__file__).resolve().parents if (_p / "VERSION").is_file()), None)
if _root is not None and str(_root) not in _sys.path:
    _sys.path.insert(0, str(_root))

import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import yaml  # noqa: E402

# Гейт, который замыкает пост-релизная петля, и его четыре required_evidence (quality/gates.yaml).
GATE_ID = "analytics_runtime_verification"
# Единственное доказательство С producer'ом — его производит event_arrival.
EVIDENCE_WITH_PRODUCER = "events_verified_live"
# Три доказательства БЕЗ producer'а: честно `not_measured`, producer'ов им не выдумываем.
EVIDENCE_WITHOUT_PRODUCER = ("no_pii_in_events", "cohort_identification_works",
                             "dashboard_receives_data")

# Куда смотреть за PRR-файлом внутри дочки, если ref не указывает на конкретный файл.
_PRR_SEARCH_GLOBS = ("PRR-*.yaml", "PRR-*.yml",
                     ".ai/project/readout/PRR-*.yaml",
                     "features/*/PRR-*.yaml", "readout/PRR-*.yaml")


def _load_doc(p: Path):
    """Разобрать yaml/json-документ. -> (dict|None, error|None)."""
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        return None, f"файл не прочитан ({type(e).__name__}: {e})"
    try:
        doc = json.loads(text) if p.suffix == ".json" else yaml.safe_load(text)
    except (yaml.YAMLError, ValueError) as e:
        return None, f"документ не разобран ({type(e).__name__}: {e})"
    return (doc if isinstance(doc, dict) else None,
            None if isinstance(doc, dict) else "документ не является объектом")


def _resolve_prr(prr_ref, child_root: Path):
    """Найти PRR-файл. prr_ref — путь (абсолютный или относительный child_root) или None (искать).

    -> (path|None, error|None). Отсутствие PRR — ЗАКОННОЕ состояние (unknown), а не провал.
    """
    if prr_ref:
        p = Path(prr_ref)
        if not p.is_absolute():
            p = child_root / p
        if p.is_file():
            return p, None
        return None, f"PRR по ссылке не найден: {prr_ref}"
    for pattern in _PRR_SEARCH_GLOBS:
        found = sorted(child_root.glob(pattern))
        if found:
            return found[0], None
    return None, None  # PRR нет — это unknown, честно, без ошибки


def _events_state(met) -> str:
    """met (True|False|None) машинного events_verified_live -> человекочитаемое состояние."""
    return {True: "verified", False: "not_verified", None: "unknown"}[met]


def _assess_prr(prr_ref, child_root: Path) -> dict:
    """Звено (a): загрузить + провалидировать PRR. Композиция validate_post_release_readout.check."""
    from ai_ops_kit.validation import validate_post_release_readout as vprr
    path, ref_error = _resolve_prr(prr_ref, child_root)
    if path is None:
        return {"found": False, "path": None, "id": None, "decision": None,
                "valid": None, "errors": [], "ref_error": ref_error}
    doc, load_error = _load_doc(path)
    if doc is None:
        return {"found": True, "path": str(path), "id": None, "decision": None,
                "valid": False, "errors": [load_error], "ref_error": None}
    errors = vprr.check(doc)
    return {"found": True, "path": str(path), "id": doc.get("id"),
            "decision": doc.get("readout_decision"), "valid": not errors,
            "errors": errors, "ref_error": None}


def _assess_analytics(child_root: Path) -> dict:
    """Звено (b): verify_analytics_runtime → машинный вердикт events_verified_live + разбор гейта.

    Композиция intelligence.event_arrival: зовём и producer (events_verified_live), и проводку
    producer→гейт (verify_analytics_runtime). Три доказательства без producer'а помечаем
    not_measured — честная проводка «1 из 4», а не выдуманные источники.
    """
    from ai_ops_kit.intelligence import event_arrival
    live = event_arrival.events_verified_live(child_root)   # met: True|False|None
    gate = event_arrival.verify_analytics_runtime(child_root)
    breakdown = {EVIDENCE_WITH_PRODUCER: _events_state(live.get("met"))}
    for name in EVIDENCE_WITHOUT_PRODUCER:
        breakdown[name] = "not_measured"    # producer'а нет — не выдумываем
    return {
        "gate": GATE_ID,
        "status": gate.get("status"),
        "blocking": bool(gate.get("blocking")),
        "events_verified_live": _events_state(live.get("met")),
        "reason": live.get("reason"),
        "detail": live.get("detail"),
        "evidence_breakdown": breakdown,
        "evidence_with_producer": 1,
        "evidence_required": 1 + len(EVIDENCE_WITHOUT_PRODUCER),
        "evidence_not_measured": list(EVIDENCE_WITHOUT_PRODUCER),
        "blockers": list(gate.get("blockers") or []),
    }


def _assess_outcome(contract, readout, feature) -> dict | None:
    """Звено (d): проекция OutcomeContract(+Readout) в граф + трассировка + ВЕРДИКТ ПО ЗАМЕРУ.

    Композиция validate_product_objects.project_outcome + trace_feature_rationale + evaluate_outcome.
    Два разных вердикта живут рядом осознанно:
      * `verdict` — ПРОЕКЦИЯ поля человека `target_met` в граф (декларация, для knowledge-graph);
      * `measured_verdict` — вердикт, СЧИТАННЫЙ ИЗ ЧИСЕЛ реальным замером (met/failed/unknown). Это
        он флипается из `unknown` в `met`/`failed`, когда после релиза приходит реальный замер.
    `flip_ready` True, когда замер реален и дал met/failed — петля МОЖЕТ флипнуть goal.outcome
    (сам флип — отдельный координационный PR; здесь ничего не пишется). Возвращает None без контракта.
    """
    if not isinstance(contract, dict):
        return None
    from ai_ops_kit.validation import validate_product_objects as vpo
    graph = vpo.project_outcome(contract, readout, feature=feature)
    trace = None
    if feature:
        trace = vpo.trace_feature_rationale(graph, feature)
    node = (graph.get("nodes") or [{}])[0]
    measured = vpo.evaluate_outcome(contract, readout)   # ФЛИП: met/failed/unknown ИЗ ЧИСЕЛ
    # P0 №6: человеко-ориентированный OUTCOME-READOUT — «что изменение сделало с ПРОДУКТОМ» (сдвиг
    # целевой метрики baseline→после релиза против цели), а не «shipped»/«verified». Чистая проекция
    # уже посчитанного `measured`; нет замера -> честное «ещё не накоплен» + условие (см. модуль).
    from ai_ops_kit.intelligence import outcome_insight
    human_readout = outcome_insight.build_outcome_readout(contract, readout, measured)
    return {
        "projected": True,
        "verdict": node.get("verdict", "pending"),
        "measured_verdict": measured["verdict"],
        "measured_evaluation": measured,
        "human_readout": human_readout,
        "flip_ready": measured["verdict"] in ("met", "failed"),
        "outcome_id": node.get("id"),
        "graph": graph,
        "trace": trace,
        "gaps": (trace or {}).get("gaps", []),
    }


# ── #584: проводка кластера обучения (outcome_analytics + evolution_triggers) в пост-релизный путь ──
# built≠wired → built. Оба модуля были дормантны (0 не-тестовых импортёров); пост-релизная петля —
# единственный слой entrypoints, что вправе звать intelligence вниз, — становится их РАНТАЙМ-ПУТЁМ.
#
# РАЗВЯЗКА ДУБЛИРОВАНИЯ С #566 (issue #584). Веха 4.1 (#566/#567) построила НОВЫЙ outcome-путь
# (evaluate_outcome/outcome_insight): он отвечает на вопрос «ВЗЯЛИ ЛИ ЦЕЛЬ» (met/failed из чисел) —
# ЕДИНСТВЕННЫЙ источник истины по продуктовому ИСХОДУ. `outcome_analytics` отвечает на ДРУГОЙ вопрос:
# «СКОЛЬКО СТОИЛО и какой был эффект по прогонам» (токены, деньги, problem-rate). Это не второй
# outcome-вердикт, а сопутствующая СТОИМОСТНАЯ/ЭФФЕКТ-аналитика ПОД тем же пост-релизным путём.
# Поэтому решение — ПОДКЛЮЧИТЬ (не снять): двух параллельных outcome-вердиктов не заводим (вердикт по
# исходу остаётся за #566), а стоимость/эффект честно живут рядом как аналитика, а не как второй итог.

# Где искать в дочке отчёт product-health (product_health.compute) для evolution_triggers.
_HEALTH_REPORT_GLOBS = ("product-health-report.json", ".ai/project/product-health-report.json",
                        ".ai/project/health/product-health-*.json")
# Где искать реестр ADR дочки (evolution_triggers сверяет обещания ADR с реальностью health).
_ADR_SEARCH_DIRS = ("decisions/adr", ".ai/project/decisions/adr")


def _assess_cost_analytics(child_root: Path) -> dict:
    """Звено (h): сводная СТОИМОСТНАЯ/ЭФФЕКТ-аналитика прогонов (outcome_analytics, #584).

    Композиция intelligence.outcome_analytics: сколько стоило (токены/деньги), problem-rate, топ задач.
    Это НЕ вердикт по исходу (тот считает #566) — сопутствующая аналитика. Честный дефолт: нет журнала
    расхода → нули, `measured` говорит правду о полноте. Сбой сбора не роняет петлю."""
    from ai_ops_kit.intelligence import outcome_analytics
    try:
        a = outcome_analytics.collect_analytics(child_root, period="all")
    except Exception as e:  # noqa: BLE001 — аналитика обогащает петлю, не является её предусловием
        return {"available": False, "reason": f"аналитика не собрана ({type(e).__name__}: {e})"}
    summary = a.get("summary") or {}
    effect = a.get("effect_metrics") or {}
    return {
        "available": True,
        "measured": bool(summary.get("total_runs")),
        "total_runs": summary.get("total_runs", 0),
        "total_tasks": summary.get("total_tasks", 0),
        "total_cost_usd": summary.get("total_cost_usd", 0),
        "avg_cost_per_task_usd": summary.get("avg_cost_per_task_usd", 0),
        "total_tokens": summary.get("total_tokens", 0),
        "problem_rate": effect.get("problem_rate"),
    }


def _find_first(child_root: Path, globs) -> Path | None:
    for pat in globs:
        found = sorted(child_root.glob(pat))
        if found:
            return found[0]
    return None


def _assess_evolution(child_root: Path) -> dict:
    """Звено (i): триггеры развития — расхождение обещаний ADR с реальным product-health (#584).

    Композиция intelligence.evolution_triggers: активные ADR обещают quality-атрибуты, product_health
    меряет реальность; триггер (advisory, НЕ gate) — где обещание не держится. ЧЕСТНЫЙ ДЕФОЛТ: нет
    отчёта health или реестра ADR → `available: false` с причиной, НЕ выдуманные триггеры. Сбой не
    роняет петлю."""
    from ai_ops_kit.checks import adr_registry
    from ai_ops_kit.intelligence import evolution_triggers
    health_path = _find_first(child_root, _HEALTH_REPORT_GLOBS)
    if health_path is None:
        return {"available": False, "reason": "отчёта product-health в дочке нет — сравнивать не с чем",
                "trigger_count": 0, "triggers": []}
    adr_dir = next((child_root / d for d in _ADR_SEARCH_DIRS if (child_root / d).is_dir()), None)
    if adr_dir is None:
        return {"available": False, "reason": "реестра ADR в дочке нет — обещаний для сверки нет",
                "trigger_count": 0, "triggers": []}
    try:
        health, _ = _load_doc(health_path)
        reg_errs, adrs = adr_registry.check_registry(adr_dir)
        if not isinstance(health, dict):
            return {"available": False, "reason": "отчёт product-health не разобран",
                    "trigger_count": 0, "triggers": []}
        if reg_errs:
            return {"available": False, "reason": "реестр ADR требует починки (см. validate_adr_registry)",
                    "trigger_count": 0, "triggers": []}
        rep = evolution_triggers.report(adrs, health)
    except Exception as e:  # noqa: BLE001 — триггеры обогащают петлю, не являются её предусловием
        return {"available": False, "reason": f"триггеры не посчитаны ({type(e).__name__}: {e})",
                "trigger_count": 0, "triggers": []}
    return {"available": True, "health_band": rep.get("health_band"),
            "trigger_count": rep.get("trigger_count", 0), "triggers": rep.get("triggers", [])}


def classify_product_status(delivery_verified: bool, outcome_verdict: str) -> dict:
    """Свести состояние ДОСТАВКИ и вердикт по ПРОДУКТОВОМУ ИТОГУ в один явный статус (#566).

    Главный различитель: доставка зелёная (merged/tests/gates/delivery ✓), а итог провален — это
    «технически done, продуктово нет». Так «мы хорошо сделали изменение» отделяется от «мы сделали
    ПРАВИЛЬНОЕ изменение». `delivery_verified` — доставка подтверждена (у петли это валидный PRR по
    верифицированной доставке); `outcome_verdict` — met/failed/unknown из реального замера.

    -> {"status", "label", "technically_done_not_product": bool, "delivery_verified", "outcome_verdict"}.
    """
    tech_done_not_product = bool(delivery_verified and outcome_verdict == "failed")
    if tech_done_not_product:
        key, label = "delivered_not_met", "технически done, продуктово нет"
    elif outcome_verdict == "failed":
        key, label = "outcome_failed", "продуктовый результат не достигнут"
    elif outcome_verdict == "met" and delivery_verified:
        key, label = "delivered_and_met", "доставлено, продуктовый результат достигнут"
    elif outcome_verdict == "met":
        key, label = "outcome_met", "продуктовый результат достигнут (доставка не подтверждена)"
    else:  # unknown
        key = "delivered_outcome_unmeasured" if delivery_verified else "outcome_unmeasured"
        label = "итог по релизу ещё не измерен"
    return {"status": key, "label": label,
            "technically_done_not_product": tech_done_not_product,
            "delivery_verified": bool(delivery_verified), "outcome_verdict": outcome_verdict}


def _synthesize_verdict(prr: dict, analytics: dict, outcome: dict | None) -> dict:
    """Звено (e): свести ЕДИНЫЙ verdict + readout_decision. Честный дефолт — watch/unknown.

    Правила (в порядке силы):
      * аналитика не поступила (events unknown) -> verdict=unknown: рекомендовать выпуск нечем;
      * аналитика нашла недоехавшее (events not_verified) -> verdict=attention: негативный сигнал;
      * аналитика подтвердила приход, но гейт неполон (3 из 4 без producer'а) ->
        verdict=partially_verified: доказано ОДНО из четырёх, «verified» сказать нельзя.
    readout_decision берём из валидного PRR (его согласованность с сигналами уже проверена
    валидатором); без валидного PRR — консервативный watch/investigate по сигналу аналитики.
    НИКОГДА не «healthy» без полного доказательства.
    """
    events = analytics.get("events_verified_live")
    if events == "unknown":
        verdict = "unknown"
    elif events == "not_verified":
        verdict = "attention"
    else:  # verified, но гейт закрыт 1 из 4
        verdict = "partially_verified"

    # readout_decision: доверяем валидному PRR (его решение сверено с сигналами валидатором).
    if prr.get("valid") and prr.get("decision"):
        readout_decision = prr["decision"]
    elif events == "not_verified":
        readout_decision = "investigate"
    else:
        readout_decision = "watch"

    return {"verdict": verdict, "readout_decision": readout_decision}


def run_post_release(prr_ref, child_root, *, contract=None, readout=None,
                     feature=None) -> dict:
    """ЕДИНЫЙ пост-релизный путь одним вызовом: PRR → verify → outcome → verdict.

    Чистая функция-композиция (read-only, без сети): зовёт по очереди уже существующие звенья и
    сводит их в один результат. НИЧЕГО не измеряет сама и НЕ пишет в дочку — в частности, никакой
    `goal.outcome` не флипается (см. `outcome_flip_ready` ниже и TODO(#545)).

    prr_ref   — путь к PRR-файлу (абс. или относит. child_root) или None (искать в дочке).
    contract  — OutcomeContract dict (опционально) для outcome-проекции.
    readout   — OutcomeReadout dict (опционально); без него verdict outcome честно `pending`.
    feature   — id функции для трассировки «зачем существует».

    -> PostReleaseLoopResult (dict).
    """
    child_root = Path(child_root)

    # (a) PRR: загрузить и провалидировать (только по ВЕРИФИЦИРОВАННОЙ доставке).
    prr = _assess_prr(prr_ref, child_root)
    # (b)+(c) verify_analytics_runtime: машинный events_verified_live + разбор гейта 1-из-4.
    analytics = _assess_analytics(child_root)
    # (d) outcome-проекция + трассировка (если дан контракт).
    outcome = _assess_outcome(contract, readout, feature)
    # (e) единый вердикт (аналитика/готовность выпуска) с честным дефолтом.
    synth = _synthesize_verdict(prr, analytics, outcome)

    # (f) ВЕРДИКТ ПО ПРОДУКТОВОМУ ИТОГУ, посчитанный из реального замера, и явный продуктовый статус.
    #     Доставка подтверждена = валидный PRR (валидатор PRR читает только верифицированную доставку,
    #     delivery_receipt.sha_verified=true), поэтому «PRR валиден» — честный прокси «доставка зелёная».
    delivery_verified = bool(prr.get("found") and prr.get("valid"))
    outcome_verdict = (outcome or {}).get("measured_verdict", "unknown")
    product_status = classify_product_status(delivery_verified, outcome_verdict)
    # ФЛИП ГОТОВ, когда реальный замер дал met/failed. САМ ФЛИП goal.outcome — отдельный
    # координационный PR (код петли ≠ правка данных плана); здесь по-прежнему ничего не пишется.
    outcome_flip_ready = bool(outcome and outcome.get("flip_ready"))

    # (g) ОБРАТНАЯ ПЕТЛЯ Outcome → Insight → кандидат-работа (#567). Замыкается ЗДЕСЬ по той же
    #     причине, что и вердикт из чисел: только слой entrypoints вправе звать И intelligence, И
    #     validation. Инсайт строится на РЕАЛЬНОМ замере (measured_evaluation): на `unknown` петля
    #     возвращает None (нет данных — инсайт не фабрикуется). Кандидат — DRAFT (writer ≠ judge),
    #     в дочку ничего не пишется. `no_insight_reason` честно называет, почему инсайта нет.
    from ai_ops_kit.intelligence import outcome_insight
    measured_eval = (outcome or {}).get("measured_evaluation")
    loop = outcome_insight.from_outcome(contract, readout, measured_eval) if measured_eval else None
    insight = (loop or {}).get("insight")
    candidate_work = (loop or {}).get("candidate_work")
    recommendation = (loop or {}).get("recommendation")
    # #987 (P0 №4): ПОСЛЕДНИЙ шаг петли — конкретное следующее действие с обоснованием от произошедшего
    #     с продуктом. Именно оно доходит до `next` (measure→learn→next decision замкнуты).
    next_action = (loop or {}).get("next_action")
    insight_gap = None if insight else outcome_insight.no_insight_reason(measured_eval)

    # (h)+(i) #584: провести кластер обучения в контур — стоимостная аналитика прогонов и триггеры
    #     развития (ADR↔health). Оба были дормантны; здесь у них появляется рантайм-импортёр.
    cost_analytics = _assess_cost_analytics(child_root)
    evolution = _assess_evolution(child_root)
    attention = _assess_attention(child_root)

    # (j) #586: из измеренного инсайта рождается ПРЕЦЕДЕНТ (факт + число случаев + контекст), без
    #     утверждения причинности. Один живой релиз = один случай ("прецедент, 1 случай", не "правило").
    #     Проекция, не запись; None, если инсайта нет (unknown — данных для прецедента ещё нет).
    from ai_ops_kit.intelligence import precedent_ledger
    precedent = precedent_ledger.precedent_from_insight(insight, contract=contract) if insight else None

    notes: list[str] = []
    if not prr["found"]:
        notes.append("PRR не найден: пост-релизного отчёта о доставке ещё нет")
    if analytics["events_verified_live"] == "unknown":
        notes.append("выгрузка аналитики дочки не поступила — приход событий не проверить")
    notes.append(f"гейт {GATE_ID}: доказательство с источником только "
                 f"{analytics['evidence_with_producer']} из {analytics['evidence_required']} "
                 f"({', '.join(EVIDENCE_WITHOUT_PRODUCER)} — not_measured, producer'а нет)")
    if product_status["technically_done_not_product"]:
        notes.append("технически done, продуктово нет: доставка зелёная, а измеренный итог провален "
                     "(" + (outcome.get("measured_evaluation") or {}).get("reason", "") + ")")
    elif outcome_verdict != "unknown":
        notes.append(f"итог по релизу измерен: {outcome_verdict} "
                     "(" + (outcome.get("measured_evaluation") or {}).get("reason", "") + ")")
    if next_action:
        notes.append(f"что делать дальше: {next_action.get('action')} — "
                     f"потому что {next_action.get('because')} "
                     f"(черновик, активной не станет без твоего решения)")
    elif candidate_work:
        notes.append(f"обратная петля: из измеренного итога родился инсайт (уверенность "
                     f"{insight.get('confidence')}) и кандидат-работа «{candidate_work.get('title')}» "
                     f"— черновик, активной не станет без твоего решения")
    elif insight_gap:
        notes.append(f"инсайт не строю: {insight_gap} (нет данных — не выдумываю)")
    if evolution.get("available") and evolution.get("trigger_count"):
        notes.append(f"триггеры развития: {evolution['trigger_count']} — обещание ADR разошлось с "
                     "реальным здоровьем продукта, стоит пересмотреть решение (advisory)")
    if precedent:
        notes.append(f"прецедент записан: {precedent.get('frequency_label')} — факт из исхода, "
                     "решение и перенос на другие продукты остаются за тобой (не правило)")

    return {
        "schema_version": 1,
        "kind": "PostReleaseLoopResult",
        "child_root": str(child_root),
        "prr": prr,
        "analytics_runtime": analytics,
        "outcome": outcome,
        # `verdict` — вердикт ГОТОВНОСТИ ВЫПУСКА (приход аналитики), как и был.
        "verdict": synth["verdict"],
        "readout_decision": synth["readout_decision"],
        # `outcome_verdict` — вердикт по ПРОДУКТОВОМУ ИТОГУ из реального замера: unknown -> met/failed.
        "outcome_verdict": outcome_verdict,
        # `product_status` — явный статус, в т.ч. «технически done, продуктово нет».
        "product_status": product_status,
        # P0 №6: OUTCOME-READOUT — сдвиг целевой метрики baseline→после релиза против цели человеческим
        # языком; None без контракта, `measured=False`+условие без замера (итог из воздуха не выдумываем).
        "outcome_readout": (outcome or {}).get("human_readout"),
        # ФЛИП ГОТОВ, когда пришёл реальный замер (met/failed). Без замера — False (честный дефолт).
        # Сам флип goal.outcome не делается здесь: это отдельный координационный PR (см. #566).
        "outcome_flip_ready": outcome_flip_ready,
        # ОБРАТНАЯ ПЕТЛЯ (#567): инсайт из измеренного итога, кандидат-работа (DRAFT) и рекомендация
        # с evidence. None, если итог не измерен (`insight_gap` называет почему). Ничего не пишется:
        # кандидат виден в inbox/next, активной работой станет только по решению человека.
        "insight": insight,
        "candidate_work": candidate_work,
        "recommendation": recommendation,
        # #987 (P0 №4): последний шаг петли — конкретное действие + «потому что <что произошло с
        # продуктом>». None, если итог не измерен (тогда `next` НИЧЕГО из петли не добавляет).
        "next_action": next_action,
        "insight_gap": insight_gap,
        # #584: кластер обучения проведён в контур — стоимостная аналитика и триггеры развития.
        "cost_analytics": cost_analytics,
        "evolution_triggers": evolution,
        # #676: человеческое внимание на путь к исходу — из durable-шины внимания (нижняя граница).
        "attention": attention,
        # #586: прецедент из измеренного исхода (факт + число случаев, без причинности). None без замера.
        "precedent": precedent,
        "notes": notes,
    }


# Где искать OutcomeContract/OutcomeReadout в дочке (те же места, что читают explain/inbox/next).
_OUTCOME_CONTRACT_GLOBS = ("outcome-contract.yaml", ".ai/project/readout/outcome-contract.yaml",
                           "features/*/outcome-contract.yaml")
_OUTCOME_READOUT_GLOBS = ("outcome-readout.yaml", ".ai/project/readout/outcome-readout.yaml",
                          "features/*/outcome-readout.yaml")


def _assess_attention(child_root: Path) -> dict:
    """#676: замер человеческого внимания на путь к исходу — из durable-шины внимания (attention_bus).

    «Внимание на verified-результат» из внешнего ревью: оптимизируем не токены, а сколько раз кит
    вынужден был позвать человека. Read-only; всегда доступен (пустая шина = 0 обращений). Нижняя
    граница — ловит только проведённое через attention_bus.record (preflight/эскалация/governance)."""
    from ai_ops_kit.lifecycle import attention_bus
    return {"available": True, **attention_bus.attention_summary(child_root)}


def discover_and_run(child_root) -> dict | None:
    """Найти OutcomeContract(+Readout) в стандартных местах и прогнать петлю. Read-only.

    Единый путь автообнаружения для inbox/next (#567) и explain (#566) — один механизм, не второй.
    -> результат `run_post_release` либо None (контракта нет — тогда и петли нет)."""
    root = Path(child_root)

    def _first(globs):
        for pat in globs:
            found = sorted(root.glob(pat))
            if found:
                return found[0]
        return None

    cpath = _first(_OUTCOME_CONTRACT_GLOBS)
    if cpath is None:
        return None
    contract, _ = _load_doc(cpath)
    if not isinstance(contract, dict) or contract.get("kind") != "OutcomeContract":
        return None
    rpath = _first(_OUTCOME_READOUT_GLOBS)
    readout = _load_doc(rpath)[0] if rpath is not None else None
    return run_post_release(None, root, contract=contract, readout=readout)


# ── Человекочитаемый разбор (для --json=off из CLI используется presenter; здесь — технический) ──
def render(result: dict) -> str:
    L = [f"Пост-релизная петля ({result['verdict']}): решение — {result['readout_decision']}"]
    a = result["analytics_runtime"]
    L.append(f"  аналитика: events_verified_live = {a['events_verified_live']} "
             f"(гейт {a['status']}, доказательств с источником {a['evidence_with_producer']}"
             f"/{a['evidence_required']})")
    for name in a["evidence_not_measured"]:
        L.append(f"    · {name}: not_measured (producer'а нет)")
    prr = result["prr"]
    if prr["found"]:
        L.append(f"  PRR {prr.get('id') or '—'}: {'валиден' if prr['valid'] else 'нарушения'}"
                 + (f" ({'; '.join(prr['errors'])})" if prr.get("errors") else ""))
    else:
        L.append("  PRR: не найден")
    if result.get("outcome"):
        o = result["outcome"]
        L.append(f"  outcome {o.get('outcome_id') or '—'}: declared={o['verdict']}, "
                 f"measured={o.get('measured_verdict')} (flip_ready={o.get('flip_ready')})")
        me = o.get("measured_evaluation") or {}
        if me.get("reason"):
            L.append(f"    · замер: {me['reason']}")
        for g in o.get("gaps") or []:
            L.append(f"    · пробел: {g}")
    ps = result.get("product_status") or {}
    if ps.get("label"):
        L.append(f"  продуктовый статус: {ps['label']} "
                 f"(доставка {'подтверждена' if ps.get('delivery_verified') else 'не подтверждена'}, "
                 f"итог {ps.get('outcome_verdict')})")
    att = result.get("attention") or {}
    if att.get("total"):
        L.append(f"  человеческое внимание: {att['total']} обращений кита к человеку "
                 f"({att['decision']} за решением, {att['blocked']} остановок; не снято {att['pending']}) "
                 "— нижняя граница по durable-шине внимания")
    ins, cand, rec = result.get("insight"), result.get("candidate_work"), result.get("recommendation")
    if ins:
        L.append(f"  инсайт {ins.get('id')}: {ins.get('headline')} (уверенность {ins.get('confidence')})")
        epi = ins.get("epistemics") or {}
        for k, ru in (("observed", "наблюдал"), ("inferred", "вывел"), ("unknown", "не знаю")):
            for item in epi.get(k) or []:
                L.append(f"    · {ru}: {item}")
        if rec:
            L.append(f"    рекомендация ({rec.get('sources')} набл., факты {len(rec.get('facts') or [])}, "
                     f"допущения {len(rec.get('assumptions') or [])}, "
                     f"критично неизвестно {len(rec.get('critical_unknowns') or [])}): {rec.get('proposal')}")
        if cand:
            L.append(f"    кандидат-работа {cand.get('id')} [{cand.get('status')}, требует решения "
                     f"человека]: {cand.get('title')} (роль {cand.get('owner_role')})")
        na = result.get("next_action")
        if na:
            L.append(f"    что делать дальше: {na.get('action')} — потому что {na.get('because')}")
    elif result.get("insight_gap"):
        L.append(f"  инсайт не строю: {result['insight_gap']}")
    for n in result.get("notes") or []:
        L.append(f"  — {n}")
    return "\n".join(L)


def main(argv):
    args = [a for a in argv if not a.startswith("-")]
    child_root = args[0] if args else "."

    def _opt(flag):
        if flag in argv:
            i = argv.index(flag)
            return argv[i + 1] if i + 1 < len(argv) else None
        return None

    prr_ref = _opt("--prr")
    feature = _opt("--feature")
    contract, readout = None, None
    cpath, rpath = _opt("--contract"), _opt("--readout")
    if cpath:
        contract, _ = _load_doc(Path(cpath))
    if rpath:
        readout, _ = _load_doc(Path(rpath))

    result = run_post_release(prr_ref, child_root, contract=contract, readout=readout,
                              feature=feature)
    if "--json" in argv:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result))
    # Код возврата — ГОТОВНОСТЬ ОТВЕТИТЬ, а не «всё зелено»: аналитика unknown -> честный 1
    # (рекомендовать выпуск нечем), негативный сигнал -> 1, частичное подтверждение -> 0.
    return 0 if result["verdict"] == "partially_verified" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
