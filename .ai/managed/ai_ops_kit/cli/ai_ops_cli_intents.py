"""Проб-свободные read-only intent-хендлеры представлений (work / readout / graph) CLI.

Исторически здесь жили ВСЕ проб-свободные обработчики, вынесенные из `ai_ops_cli`. Файл разросся
и был разрезан по когезивным группам на соседние модули пакета `ai_ops_kit.cli`:
`ai_ops_cli_product` (продукт/поставка/бэклог), `ai_ops_cli_lifecycle` (онбординг/governance)
и `ai_ops_cli_report` (read-only отчётность explain/inbox). Здесь остались проекции
`work` / `readout` / `graph` плюс ре-экспорт перенесённых имён: `ai_ops_cli` и тесты резолвят
обработчики и хелперы по этому модулю, поэтому имена ре-экспортируются именами `ai_ops_cli_intents`.

Регистрация обработчиков в общий реестр интентов делается в `ai_ops_cli` (декоратор `_intent`
и `_INTENT_HANDLERS` живут там). Модуль НЕ импортирует `ai_ops_cli` на верхнем уровне —
единичные обращения к его хелперам идут ленивым импортом внутри функции, поэтому цикла нет.
"""
from __future__ import annotations

import json
from pathlib import Path

# Ре-экспорт вынесенных обработчиков и хелперов: их резолвят по имени `ai_ops_cli_intents`
# как `ai_ops_cli` (диспетч/регистрация), так и тесты. Соседи не импортируют этот модуль
# на верхнем уровне, поэтому цикла нет.
from ai_ops_kit.cli.ai_ops_cli_product import (  # noqa: F401 — ре-экспорт для вызывающих/тестов
    build_preview, _print_preview, _BACKLOG_SUBS, _run_backlog, _intent_model, _product_health_report, _product_risks, _intent_contract, _intent_products, _intent_inspect, _intent_plan, _intent_session,
)
from ai_ops_kit.cli.foundation_proposal import run_intent as _intent_propose  # noqa: F401 — оркестратор `propose`
from ai_ops_kit.cli.ai_ops_cli_lifecycle import (  # noqa: F401 — ре-экспорт для вызывающих/тестов
    _intent_onboard, _intent_reach, _intent_team, _intent_replan, _intent_governance, _intent_bootstrap, _intent_health, _intent_roadmap, _intent_doctor, _copy_affects_from_plan, _intent_new, _intent_discuss,
)
from ai_ops_kit.cli.ai_ops_cli_report import (  # noqa: F401 — ре-экспорт для вызывающих/тестов
    _EXPLAIN_STATUS_LABEL, _explain_wid, _explain_reconcile, _explain_active, _explain_workitem, _explain_gates, _explain_conflicts, _explain_cost, _explain_blocker, _explain_next, _explain_next_command, _explain_living_note, _explain_cost_line, _explain_cost_tech, _explain_outcome, _explain_state, _explain_apply_outcome, _explain_message, _intent_explain, _inbox_decisions, _inbox_works, _INBOX_BRIEFS_LATEST_REL, _inbox_insight, _inbox_outcome_candidate, _inbox_findings, _inbox_release_warnings, _inbox_attention, _inbox_collect, _inbox_status, _inbox_counts, _inbox_render, _intent_inbox,
)


# ── #549: ai-ops work — единая машинная ПРОЕКЦИЯ «работы» по id ────────────────────────────────────
# Подкоманда первым словом (как backlog): `work show <id>`. Проекция read-only сводит четыре
# источника (заявка/реестр идущих работ/граф пакетов/план) в одну карточку по id. Ничего не пишет.
_WORK_SUBS = ("show",)


def _work_positionals(child_root, a):
    """Позиционные аргументы интента `work` БЕЗ каталога репозитория: подкоманда и id работы.

    Каталог `./ai-ops` подставляет то в начало, то в хвост; здесь мы отбрасываем всё, что является
    каталогом (подкоманда и id работы каталогами не бывают), и получаем [sub, id] в любом порядке
    вызова. -> (sub, work_id)."""
    def _is_dir(p):
        # Не-путь (в т.ч. слишком длинный текст) = не каталог; is_dir() кидает OSError, не False (#161).
        try:
            return Path(p).is_dir()
        except OSError:
            return False
    rest = list(getattr(a, "rest", None) or [])
    args = [x for x in rest if not _is_dir(x)]
    sub = (args[0] if args else "").strip().lower()
    work_id = args[1] if len(args) > 1 else None
    return sub, work_id


def _intent_work(task, child_root, signals, a):
    """`ai-ops work show <id>` — read-only карточка одной работы, сведённая из четырёх источников."""
    js = a.json
    from ai_ops_kit.lifecycle import work_view
    from ai_ops_kit.ui import presenter
    root = Path(child_root)
    sub, work_id = _work_positionals(root, a)
    if sub not in _WORK_SUBS or not work_id:
        # Без подкоманды/id — назвать, что умеет, а не молча вернуть успех (тот же принцип, что backlog).
        msg = ("work: единая проекция работы по id (только чтение). Подкоманда:\n"
               "  show <id>   — карточка: стадия, ветка, кто ведёт, области, зависимости, "
               "артефакты, решения — сведено из заявки/реестра/графа/плана\n"
               "Пример: ./ai-ops work show arch-01 .")
        if js:
            print(json.dumps({"ok": False, "reason": f"нужно: work show <id> (дано: {sub or '—'})",
                              "subcommands": list(_WORK_SUBS)}, ensure_ascii=False, indent=2))
        else:
            print(msg)
        return 2

    view = work_view.project_work(work_id, root)
    if js:
        print(json.dumps(view, ensure_ascii=False, indent=2, default=str))
    else:
        print(presenter.render(presenter.from_work_view(view),
                               audience=presenter.audience_from_config(root)))
    # Код возврата — нашлась ли работа хоть в одном источнике: сведено -> 0; ни одного источника -> 2.
    return 0 if view.get("sources") else 2



# ── ai-ops readout (#545): ЕДИНЫЙ пост-релизный путь одним вызовом ─────────────────────────────────
# PRR -> verify_analytics_runtime -> outcome-проекция -> один вердикт. Обработчик проб-свободен: он
# только читает (через оркестратор post_release_loop) и печатает; ничего в дочку не пишет и никакой
# goal.outcome не флипает. Оркестратор живёт в пакете `cli` осознанно — только слой entrypoints вправе
# звать И intelligence (event_arrival), И validation (оба валидатора PRR/outcome); см. его докстринг.
def _readout_docs(a):
    """Загрузить опциональные OutcomeContract/OutcomeReadout из путей флагов. -> (contract, readout)."""
    from ai_ops_kit.cli import post_release_loop
    contract = readout = None
    cpath = getattr(a, "outcome_contract", None)
    rpath = getattr(a, "outcome_readout", None)
    if cpath:
        contract, _ = post_release_loop._load_doc(Path(cpath))
    if rpath:
        readout, _ = post_release_loop._load_doc(Path(rpath))
    return contract, readout


def _intent_readout(task, child_root, signals, a):
    js = a.json
    from ai_ops_kit.cli import post_release_loop
    from ai_ops_kit.ui import presenter
    root = Path(child_root)
    # PRR-ссылка: из --prr, иначе позиционным текстом задачи (если это путь), иначе поиск в дочке.
    prr_ref = getattr(a, "prr", None) or (task or None)
    contract, readout = _readout_docs(a)
    result = post_release_loop.run_post_release(prr_ref, root, contract=contract,
                                                readout=readout, feature=a.feature)
    if js:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        aud = presenter.audience_from_config(root)
        print(presenter.render(presenter.from_post_release_loop(result), audience=aud))
        if aud != "product":
            print()
            print(post_release_loop.render(result))
    # Код возврата — ГОТОВНОСТЬ РЕКОМЕНДОВАТЬ ВЫПУСК, а не «всё зелено»: приход событий подтверждён
    # (partially_verified) -> 0; аналитика ещё не поступила или сигнал негативный -> 1 (честно, что
    # рекомендовать выпуск нечем). Флип исхода цели ждёт реального выпуска (см. post_release_loop).
    return 0 if result.get("verdict") == "partially_verified" else 1


# ── ai-ops graph (Knowledge Graph как ЗАПРАШИВАЕМАЯ технология) ────────────────────────────────────
# Тонкий слой ПОВЕРХ существующего: сборщик — intelligence/knowledge_graph (ниже, зависимость вниз);
# целостность — validation/validate_knowledge_graph (тот же слой entrypoints, звать вправе только
# cli). Обработчик проб-свободен: строит граф из plan.yaml+FL+blueprint и печатает продуктовым
# языком; в дочку пишет ТОЛЬКО `build --apply` (knowledge/graph.yaml).
_GRAPH_SUBS = ("build", "trace", "gaps", "questions")


def _graph_positionals(a):
    """Позиционные интента `graph` без каталога репозитория: [sub, feature] в любом порядке вызова.

    `./ai-ops` подставляет путь то в начало, то в хвост; каталогом ни подкоманда, ни id функции не
    бывают, поэтому всё, что является каталогом, отбрасывается (тот же приём, что у `work`)."""
    def _is_dir(p):
        try:
            return Path(p).is_dir()
        except OSError:
            return False
    args = [x for x in (getattr(a, "rest", None) or []) if not _is_dir(x)]
    sub = (args[0] if args else "").strip().lower()
    feature = args[1] if len(args) > 1 else getattr(a, "feature", None)
    return sub, feature


def _validate_graph_integrity(graph, child_root):
    """Прогнать собранный граф через validate_knowledge_graph. -> список ошибок целостности.

    Пишет КОПИЮ во временный каталог с blueprint-путями, приведёнными к абсолютным, чтобы проверка
    существования blueprint'а сработала без предположения о том, где ляжет graph.yaml, и без записи в
    сам репозиторий (trace/gaps — только чтение)."""
    import copy
    import tempfile
    import yaml as _yaml
    from ai_ops_kit.validation import validate_knowledge_graph as vkg
    graph_dir = Path(child_root) / "knowledge"
    g = copy.deepcopy(graph)
    for n in g.get("nodes") or []:
        bp = n.get("blueprint")
        if bp:
            n["blueprint"] = str((graph_dir / bp).resolve())
    types, rels = vkg.load_dictionary()
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "graph.yaml"
        p.write_text(_yaml.safe_dump(g, allow_unicode=True), encoding="utf-8")
        return vkg.validate_graph(p, types, rels)


def _graph_help(js):
    msg = ("graph: knowledge graph как запрашиваемая технология (поверх plan+обучение+blueprint).\n"
           "Подкоманды:\n"
           "  build             — собрать граф и показать (--apply — записать knowledge/graph.yaml)\n"
           "  trace <feature>   — зачем функция существует: цепочка цель→…→функция→исход + вердикт\n"
           "  questions <feat>  — десять вопросов жизни фичи из связанной истории (ответ или пробел)\n"
           "  gaps              — что не покрыто измеримым результатом (исходы/метрики/функции)\n"
           "Пример: ./ai-ops graph trace express-checkout .")
    if js:
        print(json.dumps({"ok": False, "reason": "нужна подкоманда graph",
                          "subcommands": list(_GRAPH_SUBS)}, ensure_ascii=False, indent=2))
    else:
        print(msg)


def _feature_measured_outcome(child_root, feature):
    """РЕАЛЬНЫЙ outcome фичи из её контракта+отчёта: сдвиг метрики после релиза (met/failed ИЗ ЧИСЕЛ)
    + следующий шаг по уроку. -> (measured_outcome_human|None, next_action|None).

    Композиция живёт на слое CLI СОЗНАТЕЛЬНО: `evaluate_outcome` — из `validation`, а граф знаний
    (`intelligence`) её не импортирует (это была бы зависимость вверх — тот же инвариант, что у
    `outcome_insight`). Поэтому «посчитать вердикт из чисел» делает entrypoint, а не сам граф.

    Источник — `features/<id>/outcome-contract.yaml` (+ `outcome-readout.yaml`, если снят замер).
    Нет контракта -> (None, None): исход из воздуха не выдумываем (no evidence → no claim)."""
    import yaml as _yaml
    root = Path(child_root)
    base = root / "features" / str(feature)
    cpath = base / "outcome-contract.yaml"
    if not cpath.is_file():
        return None, None

    def _load(p):
        if not p.is_file():
            return None
        try:
            doc = _yaml.safe_load(p.read_text(encoding="utf-8"))
        except (OSError, _yaml.YAMLError):
            return None
        return doc if isinstance(doc, dict) else None

    contract = _load(cpath)
    if contract is None:
        return None, None
    readout = _load(base / "outcome-readout.yaml")
    from ai_ops_kit.validation import validate_product_objects as vpo
    from ai_ops_kit.intelligence import outcome_insight
    evaluation = vpo.evaluate_outcome(contract, readout)
    human = outcome_insight.build_outcome_readout(contract, readout, evaluation)
    loop = outcome_insight.from_outcome(contract, readout, evaluation)
    next_action = (loop or {}).get("next_action")
    return human, next_action


def _intent_graph(task, child_root, signals, a):
    """`ai-ops graph build|trace <feature>|gaps` — один граф из трёх источников, вопрос за проход."""
    js = a.json
    from ai_ops_kit.intelligence import knowledge_graph as kg
    from ai_ops_kit.ui import presenter
    root = Path(child_root)
    sub, feature = _graph_positionals(a)
    if sub not in _GRAPH_SUBS:
        _graph_help(js)
        return 2

    graph = kg.build_graph(root)
    integrity = _validate_graph_integrity(graph, root)
    if integrity:
        # Целостность важнее ответа: строить вывод на битом графе — врать. Называем ПОСЛЕДСТВИЕ.
        # НЕ подавляем отказ: сырые ошибки валидатора остаются на месте, exit 1 остаётся — мы лишь
        # ДОБАВЛЯЕМ диагноз. Если среди ошибок есть рассинхрон СЛОВАРЯ ТИПОВ (узел с неизвестным
        # типом / запрещённая связь), самая вероятная причина — установленный реестр типов отстал
        # от сборщика. Для одних лишь висящих ссылок (порча данных проекта) подсказку «обнови кит»
        # НЕ даём: это был бы ложный диагноз.
        from ai_ops_kit.validation import validate_knowledge_graph as vkg
        skew = vkg.classify_errors(integrity).get("has_type_vocab_error", False)
        remediation = (
            "Похоже, установленный реестр типов (registry/entities.yaml) устарел относительно "
            "сборщика графа — типы/связи, которые кит порождает, реестр ещё не знает. Обнови кит: "
            "`ai-ops update`. Если граф правил вручную — приведи типы в соответствие с реестром."
        ) if skew else None
        if js:
            payload = {"ok": False, "reason": "граф не прошёл проверку целостности",
                       "errors": integrity}
            if remediation:
                payload["remediation"] = remediation
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(presenter.render(presenter.message(
                status="blocked",
                summary="Собрал граф из плана, обучения и паспортов функций, но он не сошёлся сам с "
                        "собой — отвечать по нему не буду.",
                why_it_matters="Ответ на битом графе хуже отсутствия ответа: он выглядит как факт.",
                next_steps=[remediation] if remediation else None,
                technical={"errors": integrity}), audience=presenter.audience_from_config(root)))
        return 1

    aud = presenter.audience_from_config(root)
    if sub == "build":
        wrote = None
        if getattr(a, "apply", False):
            import yaml as _yaml
            out = root / "knowledge" / "graph.yaml"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(_yaml.safe_dump(graph, allow_unicode=True, sort_keys=False),
                           encoding="utf-8")
            wrote = out
        if js:
            print(json.dumps({"graph": graph, "written_to": str(wrote) if wrote else None},
                             ensure_ascii=False, indent=2))
        else:
            print(presenter.render(presenter.from_graph_build(graph, wrote), audience=aud))
        return 0

    if sub == "trace":
        if not feature:
            _graph_help(js)
            return 2
        result = kg.trace(graph, feature)
        # РЕАЛЬНЫЙ outcome (сдвиг метрики после релиза) + следующий шаг по уроку — из контракта фичи.
        # Граф чист от validation; вердикт из чисел считает этот слой (см. _feature_measured_outcome).
        measured_outcome, next_action = _feature_measured_outcome(root, feature)
        if measured_outcome is not None:
            result["measured_outcome"] = measured_outcome
            if next_action:
                result["next_action"] = next_action
            if measured_outcome.get("measured"):
                # Замер снят: «нет outcome / нечем измерить» больше не пробел — исход есть и измерен.
                result["gaps"] = [g for g in (result.get("gaps") or [])
                                  if "targets" not in g and "measured-by" not in g]
        if js:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            print(presenter.render(presenter.from_graph_trace(result), audience=aud))
        # Код возврата — есть ли ПРОБЕЛЫ: полная цепочка с измеренным исходом -> 0, иначе 1 (честно,
        # что ценность функции ещё не подтверждена измеримым результатом). Узла нет в графе -> 2.
        if result.get("verdict") == "unknown":
            return 2
        return 0 if not result.get("gaps") else 1

    if sub == "questions":
        if not feature:
            _graph_help(js)
            return 2
        # Тот же связанный граф; поля из связанных первоисточников (аудитория/гипотеза/…) читает
        # фасад (у него диск), обход графа остаётся без диска. Реальный исход и следующий шаг — из
        # контракта фичи (числа считает этот слой), чтобы вопросы 8 и 10 отвечались измеренным.
        source = kg.feature_life_source(graph, feature, root)
        measured_outcome, next_action = _feature_measured_outcome(root, feature)
        result = kg.answer_ten_questions(graph, feature, source=source,
                                         measured_outcome=measured_outcome,
                                         next_action=next_action)
        if js:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            print(presenter.render(presenter.from_graph_questions(result), audience=aud))
        # Код возврата: нет узла -> 2; все десять отвечены -> 0; иначе 1 (история собрана не целиком).
        if result.get("verdict") == "unknown":
            return 2
        return 0 if result.get("complete") else 1

    # gaps
    result = kg.gaps(graph)
    if js:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(presenter.render(presenter.from_graph_gaps(result), audience=aud))
    total = sum(len(v) for v in result.values())
    return 0 if total == 0 else 1


def _kit_claim_results(child_root):
    """Результаты проверки реестра честности (`knowledge/claims.yaml`) для доли покрытия evidence.

    Композиция ЖИВЁТ на слое CLI СОЗНАТЕЛЬНО: `validate_claims` — из `validation` (entrypoints), а
    `product_scorecard` (`intelligence`) её не импортирует (это была бы зависимость вверх, тот же
    инвариант, что у графа знаний). Поэтому «прочитать реестр» делает entrypoint, а долю из него
    считает карта. Нет файла/сбой чтения -> None (метрика 2 честно «не измерено», не выдумка)."""
    from ai_ops_kit.validation import validate_claims
    claims_file = Path(child_root) / "knowledge" / "claims.yaml"
    if not claims_file.is_file():
        return None
    try:
        return validate_claims.build(claims_file)
    except Exception:  # noqa: BLE001 — один недоступный источник не роняет всю карту
        return None


def _intent_scorecard(task, child_root, signals, a):
    """`ai-ops scorecard` — единая карта продукта кита из 5 метрик (мерит СЕБЯ как продукт).

    Метрики 1/3 считаются из графа знаний, 2 — из реестра честности (читает этот слой, долю считает
    карта), 4/5 стоят честным каркасом «не измерено». Только чтение, ничего не пишет."""
    from ai_ops_kit.intelligence import product_scorecard as ps
    from ai_ops_kit.ui import presenter
    root = Path(child_root)
    scorecard = ps.build_scorecard(root, claim_results=_kit_claim_results(root))
    if a.json:
        print(json.dumps(scorecard, ensure_ascii=False, indent=2, default=str))
    else:
        print(presenter.render(presenter.from_scorecard(scorecard),
                               audience=presenter.audience_from_config(root)))
    # Код возврата: вся карта измерена -> 0, иначе 1 (честно: часть карты ещё «не измерено»).
    return 0 if not scorecard.get("unmeasured_count") else 1