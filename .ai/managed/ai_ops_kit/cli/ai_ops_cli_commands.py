#!/usr/bin/env python3
"""Слой реализации команд CLI — обработчики намерений и путь исполнения run/do.

Разрезан из `ai_ops_cli` (монолит входа): тонкий диспетч `main`, парсер argparse и декларации
`INTENTS`/`DIRECT_INTENTS` остаются в `ai_ops_cli`, а РЕАЛИЗАЦИЯ команд живёт здесь. Сюда переехали:

  * проб-несущие обработчики намерений: `backlog` / `feedback` / `status` / `next` / `review`
    / `advise` (плюс read-only помощник `_enrich_running_with_work_view`). Проб-СВОБОДНЫЕ
    обработчики живут в соседнем `ai_ops_cli_intents` (и его спутниках) — это осознанное деление;
  * путь реального запуска движка `_main_run_execute` (`run --execute` / `do`) и сессионные стражи
    перед стартом: `_session_identity` / `_announce_start` / `_session_guard_before_start`
    / `_process_gate`;
  * общие хелперы вывода и идентификации `_say` / `_audience` / `_wid_for`
    / `_intake_command_carrying_task_type` — единый путь наружу (через presenter) и сборка id работы.

Ребро импорта ОДНОСТОРОННЕЕ: этот модуль НЕ импортирует `ai_ops_cli` (ни на верхнем уровне, ни
лениво), поэтому цикла нет и порядок импорта не важен. `ai_ops_cli` импортирует отсюда имена,
ре-экспортирует их (вызывающие и тесты резолвят прежними именами `ai_ops_cli.<name>`) и
регистрирует обработчики в общий реестр интентов (декоратор `_intent` и `_INTENT_HANDLERS`
живут в `ai_ops_cli`). Обратные обращения к чисто-декларативным именам `ai_ops_cli` не нужны:
всё, что вызывают функции ниже, определено здесь же или берётся ленивым импортом из доменных
модулей `ai_ops_kit.*` / соседнего спутника `ai_ops_cli_intents`.
"""
from __future__ import annotations

import json
from pathlib import Path

def _wid_for(task, signals, feature):
    from ai_ops_kit.engine import run_plan
    return feature or run_plan.build_plan(dict(signals, task_text=task or ""),
                                          workitem_id=feature)["workitem_id"]


def _plain_signals_example(missing):
    """#864: пример ответа ОБЫЧНЫМИ СЛОВАМИ для незаявленных size/risk — вместо JSON.

    `task_type` кит спрашивать не должен (он его выводит сам — `missing_intake_signals` его и не
    просит), поэтому словами закрываются ровно те два сигнала, которые здесь вообще бывают
    незаявлены. `ai_ops_kit.shared.signal_words.parse_plain_signals` умеет прочитать эту же фразу
    обратно — строка не декоративная, она реально работает как ответ.
    -> строка вида `--signals "небольшая, неопасная"` или None (нечего показывать)."""
    keys = {m.get("signal") for m in (missing or [])}
    words = []
    if "size" in keys:
        words.append("небольшая")
    if "risk" in keys:
        words.append("неопасная")
    if not words:
        return None
    return '--signals "' + ", ".join(words) + '"'


def _intake_command_carrying_task_type(missing, task_type):
    """Готовая строка ответа на неполный intake — СЛОВАМИ вперёд, JSON вторым (#864).

    Полевой замер (cockpit, 06.09.2026): `run` без size/risk печатал подсказку
    `--signals '{"size":..,"risk":..}'` — без task_type. Оператор, следуя ей буквально, ронял
    ENGINEERING в QUICK (base_workflow QUICK -> судья code_review не запускается). Поэтому task_type,
    выведенный роутером или перенесённый со specify, по-прежнему встаёт в JSON-вариант первым
    ключом — тот, кто скопирует именно его, не потеряет уровень.

    #864 (живой zero-touch прогон): у JSON теперь есть равноправный сосед — ответ обычными
    словами (`_plain_signals_example`), который `ai_ops_cli._parse_signals_arg` умеет разобрать
    сам. JSON остаётся рабочим и внутренним (переносит task_type буквально), но не единственным —
    словам он показан ПЕРВЫМ, а не единственная строка «ответь JSON'ом»."""
    from ai_ops_kit.engine import pipeline_helpers
    base = pipeline_helpers.intake_signals_command(missing)
    if task_type and base:
        pairs = {"task_type": task_type}
        for m in missing:
            pairs[m["signal"]] = (m.get("allowed") or ["<значение>"])[0]
        inner = ", ".join(f'"{k}":"{v}"' for k, v in pairs.items())
        base = f"--signals '{{{inner}}}'"
    words = _plain_signals_example(missing)
    if words and base and words != base:
        return f"{words}  (или тем же в JSON: {base})"
    return words or base


def _say(child_root, translator, *args, **kwargs):
    """Внутренний отчёт -> человеческий текст. ЕДИНСТВЕННЫЙ путь наружу для команд намерений.

    Прежде каждая команда печатала своё: `ONBOARD: стек python`, `SPECIFY: создан`, `REVIEW wid:
    verdict=…`. Правило «наружу выходит смысл» держалось на памяти автора команды, и из двенадцати
    команд его соблюдали три. Здесь оно держится на том, что другого способа напечатать нет.

    Имя переводчика — строка: так `cli` не тянет `ui` на импорте (слои). Опечатка в имени падает
    громко (`AttributeError`), а не печатает пустоту, и её же ловит тест разводки.
    """
    from ai_ops_kit.ui import presenter
    fn = getattr(presenter, translator)
    print(presenter.render(fn(*args, **kwargs),
                           audience=presenter.audience_from_config(child_root)))


def _audience(child_root):
    """Уровень детализации для этого репозитория. Отдельно — там, где нужен и внутренний вывод."""
    from ai_ops_kit.ui import presenter
    return presenter.audience_from_config(child_root)


def _intent_backlog(task, child_root, signals, a):
    from ai_ops_kit.cli.ai_ops_cli_intents import _run_backlog   # backlog-домен живёт в спутнике
    js = a.json
    return _run_backlog(task, child_root, signals, js, a)


def _intent_feedback(task, child_root, signals, a):
    js = a.json
    # Наблюдение о ките — данные, а не пересказ. Без текста команда показывает судьбу уже
    # сказанного: канал в одну сторону перестают наполнять, поэтому ответ обязан быть виден.
    from ai_ops_kit.engops import kit_feedback
    # ПУТЬ РЕПОЗИТОРИЯ — НЕ НАБЛЮДЕНИЕ (проба канала на живой дочке, 18.08.2026). Обёртка
    # `./ai-ops` подставляет абсолютный путь сразу после интента, а человек по привычке от всех
    # остальных команд дописывает `.` — и второй позиционный уезжал в ТЕКСТ. `./ai-ops feedback .`
    # (ровно та команда, которую кит сам печатает как «посмотреть судьбу сказанного», плюс точка)
    # записывала наблюдение с содержанием «.», возвращала «записал» и судьбу не показывала.
    # Здесь путь читается как путь: человек просил показать судьбу, а не сообщать про каталог.
    _txt = (task or "").strip()
    if _txt and Path(_txt).is_dir():
        _txt = ""
    if not _txt:
        rep = kit_feedback.status(child_root)
        if js:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        else:
            _say(child_root, "from_kit_feedback_status", rep)
            if _audience(child_root) != "product":
                print()
                print(kit_feedback.render_status(rep))
        return 1 if rep["errors"] else 0
    ev = kit_feedback.evidence_from_args(getattr(a, "evidence_file", None),
                                         getattr(a, "evidence_command", None),
                                         getattr(a, "evidence_note", None))
    p, created, errors = kit_feedback.record(
        child_root, _txt, evidence=ev, severity=getattr(a, "severity", None),
        observation_class=getattr(a, "observation_class", None))
    if js:
        print(json.dumps({"path": str(p), "created": created, "errors": errors},
                         ensure_ascii=False, indent=2))
    else:
        try:
            shown = p.relative_to(child_root)
        except ValueError:
            shown = p
        _say(child_root, "from_kit_feedback_recorded", str(shown), created, errors,
             bool(ev), getattr(a, "observation_class", None))
    return 1 if errors else 0


def _intent_status(task, child_root, signals, a):
    js = a.json
    from ai_ops_kit.lifecycle import active_work
    from ai_ops_kit.ui import presenter
    awp = child_root / ".ai" / "runtime" / "active-work.yaml"
    data = {"active": []}
    if awp.is_file():
        try:
            data = active_work.load(awp)
        except active_work.ActiveWorkCorrupt as e:
            # Битый реестр — не «работы нет»: координация сессий недостоверна (инвариант 3.0.12).
            print(presenter.render(presenter.message(
                status="blocked",
                summary="Не могу сказать, что идёт прямо сейчас: запись об идущих работах "
                        "повреждена.",
                why_it_matters="Пока это так, я не знаю, не перепишет ли новая работа то, что "
                               "уже правит другая сессия.",
                next_steps=["восстановить запись и повторить"],
                technical={"ошибка": str(e)}),
                audience=presenter.audience_from_config(child_root)))
            return 1
    pub = active_work.publication_enabled(child_root)
    if js:
        # Досягаемость видна и в JSON — потребитель ответа не должен угадывать её сам.
        return (active_work.list_cmd(awp, as_json=True, published=pub, child_root=child_root)
                if awp.is_file() else 0)
    aud = presenter.audience_from_config(child_root)
    # Общая карта: локальные заявки + опубликованные заявки ДРУГИХ машин (если публикация
    # включена). Так «работа идёт» становится фактом о команде, а не об одной машине.
    team = active_work.team_view(child_root, data.get("active") or [], pub)
    # #137: СВЕРКА С БАЗОЙ на чтении. Поле 17.08.2026: три записи из четырёх относились к работе,
    # давно влитой в main, а `status` отвечал «Работа идёт» и советовал не трогать те же файлы.
    team = active_work.reconcile_with_base(team, child_root)
    reconciled = active_work.persist_reconciliation(awp, team) if awp.is_file() else 0
    # ВТОРОЙ ИСТОЧНИК ПРАВДЫ СПРАШИВАЕТСЯ ЗДЕСЬ, а не заводится третьим (замер 18.08.2026):
    # реестр говорит, что исполняется на этой машине, план — что объявлено идущим. Сверка живёт в
    # `planning` осознанно: `lifecycle` не вправе его импортировать (слои), а отвечает человеку
    # entrypoint — он и складывает два ответа в один.
    from ai_ops_kit.planning import delivery_plan as _dp
    try:
        cross = _dp.crosscheck_running(child_root, team, registry_exists=awp.is_file())
    except _dp.PlanCorrupt as e:
        # Битый план — не «в плане ничего не объявлено»: это ровно тот случай, где «не знаю»
        # нельзя выдать за «нет». Ответ про реестр остаётся, а про план говорим прямо.
        print(presenter.render(presenter.message(
            status="degraded",
            summary="Про заявки на работу отвечу, а про план — нет: файл плана не разбирается.",
            why_it_matters="Пока план не читается, я не могу сказать, не объявлена ли идущей "
                           "работа, которой никто не занимается.",
            next_steps=["починить файл плана и повторить"],
            technical={"ошибка": str(e)}), audience=aud))
        cross = None
    # #565: per-work идентичность (ветка/заголовок) идущей работы берётся из ЕДИНОЙ Work-проекции —
    # того же источника, что у `work show` и `explain`. Делаем это ПОСЛЕ reconcile/persist/crosscheck
    # (они видят исходный реестр — поведение записи не меняется) и только для показа: проекция
    # прикрепляется к записи (видна в --json), ветка синхронизируется. Статус «идёт ли работа»
    # остаётся за реестром — это верный источник именно для этого вопроса.
    _enrich_running_with_work_view(child_root, team)
    print(presenter.render(presenter.from_active_work({"active": team}, published=pub,
                                                      reconciled=reconciled, crosscheck=cross),
                           audience=aud))
    return 0


def _enrich_running_with_work_view(child_root, team):
    """READ-ONLY: прикрепить единую Work-проекцию к каждой идущей записи и синхронизировать ветку
    из неё (issue #565). Ничего не пишет на диск и не меняет статус (за «идёт ли» отвечает реестр)."""
    # project_work читает по контракту и не бросает на отсутствующих/битых источниках (каждый его
    # reader глушит свой OSError/YAMLError у себя), поэтому обёртка-глушилка тут не нужна.
    from ai_ops_kit.lifecycle import work_view
    from ai_ops_kit.planning.delivery_plan import _workitem_key
    for a in team or []:
        wid = _workitem_key(a) or str(a.get("id") or "")
        if not wid:
            continue
        v = work_view.project_work(wid, child_root)
        if not v.get("sources"):
            continue
        a["work_view"] = v
        if v.get("branch"):
            a["branch"] = v["branch"]
        if v.get("title") and not a.get("title"):
            a["title"] = v["title"]


def _intent_next(task, child_root, signals, a):
    js = a.json
    # Четыре вопроса: где мы, что идёт сейчас, что блокирует, что взять следующим.
    from ai_ops_kit.planning import next_work
    from ai_ops_kit.planning import delivery_plan as _plan
    from ai_ops_kit.planning import contours as _contours
    try:
        # Личность спрашивающего — чтобы «что взять» отвечалось участнику, а не
        # репозиторию: своя работа отделяется от чужой той же меркой, что и в реестре.
        rep = next_work.compute(child_root, budget_left=getattr(a, "budget", None),
                                me=_session_identity(child_root))
    except (_plan.PlanCorrupt, _contours.ModelCorrupt) as e:
        print(f"ОШИБКА: {e}")
        return 1
    # #567: предложенный кандидат из обратной петли Outcome→Insight. Живёт на слое CLI (entrypoints
    # вправе звать intelligence вниз; next_work в `planning` тянуть вверх не может). НЕ ранжированная
    # работа — черновик, активным станет только по решению человека; показываем ОТДЕЛЬНО.
    from ai_ops_kit.cli.ai_ops_cli_intents import _inbox_findings, _inbox_outcome_candidate
    candidate = _inbox_outcome_candidate(child_root)
    # #585: обратное наследование §28 — наблюдения дочек из findings/from-children как кандидаты к
    # разбору (DRAFT). Тот же слой/принцип, что и outcome-кандидат: черновик, решает человек.
    findings = _inbox_findings(child_root)
    if js:
        rep = dict(rep, outcome_candidate=candidate, child_findings=findings)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        # v3.35 Human Communication Layer: по умолчанию говорим смыслом, а не внутренним
        # состоянием. Разбор по четырём вопросам доступен на technical/debug и по --json.
        from ai_ops_kit.ui import presenter
        aud = presenter.audience_from_config(child_root)
        print(presenter.render(presenter.from_next_work(rep), audience=aud))
        if candidate:
            # P0 №4: петля доходит до КОНКРЕТНОГО следующего действия, и оно появляется здесь, в
            # `next`, с обоснованием от произошедшего с продуктом — «потому что <что случилось>».
            # Так замыкается idea → decision → build → release → measure → learn → next decision.
            print(f"\n  Что делать дальше по итогу релиза (черновик, требует решения): "
                  f"{candidate.get('action') or candidate['what']}")
            if candidate.get("because"):
                print(f"      потому что {candidate['because']} (уверенность "
                      f"{candidate.get('confidence')}) — активной не станет без твоего решения")
            else:
                print(f"      основано на {candidate.get('sources')} набл. (уверенность "
                      f"{candidate.get('confidence')}); активной не станет без твоего решения")
        for _cand in (findings or {}).get("candidates") or []:
            print(f"\n  Наблюдение из прогона к разбору (черновик, требует решения): "
                  f"{_cand.get('title')}"
                  f"\n      контекст: {_cand.get('source_context') or '—'}; "
                  "активной не станет без твоего решения")
        # Ошибки плана и направления печатаются ВСЕГДА: «показать по запросу» относится к
        # техническим деталям исправного прогона, а не к дефекту, который блокирует ответ.
        for _e in (rep.get("plan_errors") or []):
            print(f"  ✗ план: {_e}")
        for _e in (rep.get("roadmap") or {}).get("errors") or []:
            print(f"  ✗ направление: {_e}")
        if aud != "product":
            print()
            print(next_work.render(rep))
    # Код возврата — ГОТОВНОСТЬ ОТВЕТИТЬ, а не наличие работы: без плана и с битым roadmap
    # ответ «что взять следующим» недостоверен, и молчаливый ноль это скрывал бы.
    return 0 if (rep.get("plan_present") and not rep.get("plan_errors")
                 and not rep["roadmap"]["errors"]) else 1


def _intent_review(task, child_root, signals, a):
    js = a.json
    from ai_ops_kit.engine import review_branch
    from ai_ops_kit.engine import run_plan
    wid = a.feature or _wid_for(task, signals, a.feature)
    # реальный ревьюер — отдельный провайдер (writer ≠ judge); mock не выносит вердикт (needs-reviewer)
    rev_prop = None
    prov = getattr(a, "provider", "mock") or "mock"
    if prov != "mock":
        from ai_ops_kit.providers import orchestrator
        rev_prop = orchestrator.make_provider(prov, getattr(a, "model", None))
    rep = review_branch.review(child_root, wid, reviewer_proposer=rev_prop, base=a.base)
    if js:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        _say(child_root, "from_review", rep)
        # Соответствие конституции по изменённым файлам (#847): рекомендации, не блок.
        _cf = rep.get("constitution_findings") or []
        if _cf:
            print(f"\n  Соответствие конституции (по изменённым файлам): {len(_cf)} статей "
                  "с рекомендациями — это советы, не блок на мерже:")
            for _f in _cf:
                print(f"    · {_f['article_id']} {_f['title']} ({_f['count']}) — {_f['recommendation']}")
    # v2.121 (P1.3): exit code = готовность к merge (needs-reviewer/needs-changes -> non-zero)
    return 0 if (rep.get("readiness") or {}).get("ready_for_merge") else 1


def _intent_advise(task, child_root, signals, a):
    js = a.json
    from ai_ops_kit.engops import engineering_advisor
    from ai_ops_kit.intelligence import product_advice
    result = engineering_advisor.advise(str(child_root), task_type=signals.get("task_type"))
    # #958: совет ВЕДЁТ с «что нужно продукту», а инженерная настройка кита идёт после и отдельно.
    # Продуктовые рекомендации — тонкий слой поверх существующих сигналов; инженерную часть
    # (engineering_advisor) не трогаем, лишь подаём её второй.
    result["product_advice"] = product_advice.recommend(str(child_root))
    if js:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _say(child_root, "from_advice", result)
    return 0


def _session_identity(child_root) -> str:
    """КТО держит работу — измеренная личность, а не константа.

    ЗАМЕР 18.08.2026: `session` по всему пути прогона имел значение по умолчанию `cli`, то есть ВСЕ
    параллельные сессии на машине выглядели одним держателем. Из этого следовало сразу два следствия
    заявки потребителя #150: отказ второй сессии не мог сработать в принципе (держатель «тот же»), и
    атрибуция инцидента была невозможна — в записях стояло `cli` у всех.

    Личность берётся из ТОГО ЖЕ измерения, которым кит уже считает расход сессии
    (`engops.session_telemetry`): идентификатор рантайма живёт дольше процесса, поэтому повторный
    прогон в той же сессии — тот же держатель, а не новый. Не измерилось — честный `pid:<pid>`: это
    «вот этот процесс», а не «все мы вместе»; мёртвый pid заявку не держит (`active_work`).
    """
    try:
        from ai_ops_kit.engops import session_telemetry
        sid = (session_telemetry.snapshot(str(child_root)) or {}).get("session_id")
    except Exception:      # noqa: BLE001 — телеметрия недоступна: личность НЕ теряется, см. ниже
        sid = None
    if sid:
        return f"session:{str(sid)[:8]}"
    import os as _os
    return f"pid:{_os.getpid()}"


def _announce_start(child_root):
    """#708: маркер старта «— запускаю —» держим на technical/debug — там он ориентир в логе. На
    product смысл «запускаю сейчас» уже несёт превью, а дублирующая техно-ремарка только шумит."""
    if _audience(child_root) != "product":
        print("— запускаю —")


def _session_guard_before_start(child_root, task, signals, feature=None):
    """v3.22 Culture Runtime Integration: session guard ДО старта задачи.
    1. snapshot — текущее состояние сессии (контекст и расход — measured по транскрипту сессии)
    2. relation по факту — session_boundary.classify (не жёсткое значение)
    3. recommend — расход и исход НАЗЫВАЮТСЯ ВСЕГДА (advise, не block)
    4. delegation — если большая разведка, рекомендовать сабагент
    Выводит рекомендации пользователю, не блокирует прогон.

    Почему пункт 3 говорит всегда (2026-08-13): раньше он печатал что-либо только на исходах
    `new_session`/`compact`. Контекст при этом всегда был `unknown` — транскрипт сессии не читался
    ни разу, — этих исходов не наступало, и страж молчал в 100% прогонов. Молчание неотличимо от
    «всё в порядке», а решение «здесь новую сессию не начинаем» нужно ДО траты, не после.
    """
    try:
        from ai_ops_kit.engops import session_telemetry
        from ai_ops_kit.engops import session_guardrails
        from ai_ops_kit.engops import session_boundary
        from ai_ops_kit.engops import delegation_advisor
        # 1. snapshot
        snap = session_telemetry.snapshot(str(child_root), workitem_id=feature)
        # 2. relation по факту
        current_wid = snap.get("workitem_id")
        relation_cls, reason = session_boundary.classify(
            current_workitem=current_wid, new_task=task or "", new_workitem=feature)
        relation = session_boundary.to_relation(relation_cls)
        # 3. recommend — наружу через presenter, на любом исходе
        pol = session_guardrails.load_policy(child_root)
        rec = session_guardrails.recommend(snap, pol, next_relation=relation, next_task=task,
                                           task_done=False, repo_path=str(child_root))
        _say(child_root, "from_session_economy", snap, rec)
        # 3а. автономия: может ли кит взять эту работу в ОТДЕЛЬНУЮ сессию сам, и разрешено ли ему
        # тратить. Здесь только РЕШЕНИЕ — трата отсюда невозможна: исполнителя и учёт расхода
        # подключает вызывающий (см. session_launcher.spawn, шов usage_hooks). Печатаем, только когда
        # рекомендация уже говорит о смене сессии: иначе строка была бы шумом на каждом прогоне.
        if rec.get("outcome") in ("new_session", "clear"):
            # 3б. ПОДГОТОВКА ПЕРЕХОДА, а не только совет о нём. Замер 17.08.2026: совет «уйди в
            # новую сессию» существовал и был верен, но уходить было НЕ С ЧЕМ — сессионного handoff
            # кит не писал нигде, при этом текст рекомендации утверждал, что handoff сохранён.
            # Пишем ровно на тех исходах, где переход советуется: писать на каждом прогоне значило бы
            # заводить файл там, где никто никуда не уходит.
            from ai_ops_kit.engops import session_handoff
            try:
                _h = session_handoff.write(
                    child_root, session_handoff.build(child_root, snap, rec, goal=task))
                print(f"  handoff сессии записан: {_h}")
            except Exception as _he:  # noqa: BLE001 — не смогли записать: говорим, а не молчим
                print(f"⚠ handoff сессии НЕ записан: {_he}")
            from ai_ops_kit.engops import session_launcher
            dec = session_launcher.decide(str(child_root), snap, next_relation=relation,
                                          next_task=task, task_done=False)
            _say(child_root, "from_subsession_decision", dec)
        # 4. delegation
        del_signals = {"task_text": task or "", "files_count": 0}
        del_recs = delegation_advisor.advise(del_signals)
        if del_recs:
            print(f"⚠ DELEGATION: {len(del_recs)} рекомендация(ий)")
            for r in del_recs[:2]:
                print(f"  · {r.get('trigger')}: {r.get('reason', '')[:80]}")
    except Exception as e:  # noqa: BLE001
        # session guard — advise, не block; если что-то сломалось, продолжаем
        print(f"⚠ session guard: {e}")


def _process_gate(intent, task, child_root, signals, a, preview_mode):
    """Два механизма ПЕРЕД процессным шагом. -> код возврата (шаг делать не надо) или None.

    Решение владельца 2026-08-17 по работе `kit-as-first-step-or-as-trace`. Порядок здесь —
    содержательный, не случайный:

    1. КОРОТКИЙ ПУТЬ (`planning/short_path`). Если работа уже описана, описывать её заново незачем,
       и потолок траты на описание к ней применять тоже незачем — повода тратить просто нет.
    2. ПОТОЛОК ПРОЦЕССНОЙ ФАЗЫ (`engops/process_spend`). Ловит противоположный случай: описания нет,
       разбор идёт, кода всё ещё нет. Замер поля — две сессии по 200+ тысяч токенов.

    Превью не задето: оно ничего не делает и ничего не тратит, а короткий путь — это ДЕЙСТВИЕ.
    """
    from ai_ops_kit.engops import process_spend
    if preview_mode or intent not in process_spend.PROCESS_INTENTS:
        return None
    from ai_ops_kit.planning import short_path
    child_root = Path(child_root)
    wid = a.feature or _wid_for(task, signals, a.feature)

    if not getattr(a, "full_process", False):
        d = short_path.assess(task, signals, child_root, wid)
        if d["short_path"]:
            tr = short_path.trace(child_root, wid, signals, d)
            run_cmd = f'./ai-ops run "{task or "<задача>"}" --feature {wid} --execute'
            if a.json:
                print(json.dumps({"kind": "short-path", "decision": d,
                                  "spec": str(tr.get("spec")), "record": str(tr.get("record")),
                                  "sections_filled": tr.get("filled"),
                                  "sections_declined": tr.get("declined"),
                                  "trace_error": tr.get("error"),
                                  "next_command": run_cmd}, ensure_ascii=False, indent=2))
            else:
                _say(child_root, "from_short_path", d, tr, run_cmd)
                if _audience(child_root) != "product":
                    print()
                    print(short_path.render(d))
            # След не записался — это не короткий путь, а пропуск проверок без записи о нём.
            # Отдаём тот же код, что у незакрытого гейта: молча продолжать нельзя.
            return 1 if tr.get("error") else 0
        if d["declared"] and not a.json:
            # Заявлено, но не подтверждено: называем, чего не хватает, и идём ОБЫЧНЫМ путём.
            _say(child_root, "from_short_path", d, None, None)

    # ПОВТОРНЫЙ `specify` ПОД ПОДНЯВШИЙСЯ УРОВЕНЬ — НЕ РАЗБОР (B2-26, поле 19.08.2026).
    # Потолок ловит «описание уточняется, кода нет, деньги текут». Дописывание недостающих разделов
    # ни того, ни другого не делает: шаг детерминированный, конечный и модель не зовёт вовсе. Отказ
    # экономической причиной здесь не просто лишний — он ВРЁТ: человек читает про деньги, а дело в
    # разделах, и уровень в файле так и остаётся прежним.
    top_up = {"missing": []}
    if intent == "specify":
        from ai_ops_kit.gates import spec_levels
        top_up = spec_levels.pending_sections(child_root, wid, signals)

    check = process_spend.assess(child_root, wid, intent)
    if check["blocks"] and top_up["missing"]:
        _lvl = f"{top_up.get('level_in_file')} -> {top_up['level_now']} ({top_up['level_name']})"
        if a.json:
            print(json.dumps({"kind": "spec-top-up-exempt", "check": check,
                              "level": _lvl, "sections_to_add": top_up["missing"]},
                             ensure_ascii=False, indent=2))
        else:
            # печатается ОБЕИМ аудиториям намеренно: владелец видел на этом месте отказ про деньги,
            # и заменить его молчанием значило бы починить только техническую половину
            print(f"· уровень описания поднялся ({_lvl}): дописываю разделы "
                  f"{', '.join(top_up['missing'])}. Это конечный шаг без обращения к модели — "
                  f"потолок траты на разбор к нему не применяю.")
        return None

    if check["blocks"] and not getattr(a, "spend_ok", False):
        run_cmd = f'./ai-ops run "{task or "<задача>"}" --feature {wid} --execute'
        cont_cmd = f'./ai-ops {intent} "{task or "<задача>"}" --feature {wid} --spend-ok'
        if a.json:
            print(json.dumps({"kind": "process-spend-ceiling", "exit": 2, "check": check,
                              "run_command": run_cmd, "continue_command": cont_cmd},
                             ensure_ascii=False, indent=2))
        else:
            _say(child_root, "from_process_spend", check, cont_cmd, run_cmd)
        return 2
    if check["state"] == "unknown" and not a.json and _audience(child_root) != "product":
        # «Не знаю» не выдаём за норму — но и не тревожим владельца тем, что мерить нечем.
        _say(child_root, "from_process_spend", check, None, None)
    return None


def _main_run_execute(intent, task, child_root, signals, a, pv):
    """Единственный путь, реально запускающий движок: `run --execute` и `do` (v3.22: `do` — alias
    с авторазрешением блокировщиков review_fix_attempts/author/open_pr). Возвращает код возврата."""
    if (intent == "run" and a.execute) or intent == "do":
        from ai_ops_kit.engine import ai_ops_run
        from ai_ops_kit.engine import pipeline_helpers
        # v3.38 (W3): регистрация подписчиков спутников — ЗДЕСЬ, на входе, а не в ядре (kernel-boundary:
        # ядро испускает события и НЕ импортирует спутники). Явный импорт виден и test_capability_reachability.
        from ai_ops_kit.engops import session_events as _session_events  # noqa: F401
        # v3.28.x (F-015): intake-сигналы проверяем ДО старта — `size` требует блокирующий гейт
        # intake_completeness, иначе пользователь узнавал о пропаже только из вердикта ПОСЛЕ прогона
        # (раунд C: 6 из 6 прогонов сгорели). Fail-closed (exit 2), но платится секундами, а не часом.
        _missing = pipeline_helpers.missing_intake_signals(signals)
        if _missing:
            # Подсказка сохраняет уже известный task_type (роутер/перенос со specify): следовать ей
            # буквально не должно ронять уровень в QUICK (полевой замер cockpit 06.09.2026).
            _cmd = _intake_command_carrying_task_type(_missing, signals.get("task_type"))
            _hint = pipeline_helpers.intake_signals_hint(_missing, task)
            if _hint and signals.get("task_type"):
                _hint = _hint[:-1] + [f"  добавь: {_cmd}"]
            if a.json:
                print(json.dumps({"kind": "intake-incomplete", "exit": 2,
                                  "missing": _missing, "hint": _hint}, ensure_ascii=False, indent=2))
            else:
                # Готовая команда с ответом обязана дойти до человека на любом уровне детализации.
                _say(Path(child_root), "from_intake_gap", _missing, _cmd)
            return 2
        # #564: Decision Loop проведён в маршрут. Для триггерного профиля (заявлено фича-решение)
        # отсутствие Decision-контракта закрывает продвижение fail-closed — ДО выбора провайдера и
        # любой траты. Для остальных работ сигнал не взведён и гейт возвращает None (не мешает).
        from ai_ops_kit.intelligence import decision_loop
        _dc = decision_loop.decision_contract_gate(signals, child_root, task, a.feature)
        if _dc is not None:
            if a.json:
                print(json.dumps(_dc, ensure_ascii=False, indent=2))
            else:
                print(f"ОТКАЗ: {_dc['message']}")
                print(f"  завести контракт: {_dc['propose_command']}")
            return _dc["exit"]
        flags = pv["will_do"]["auto_flags"]
        # v3.28.x (P0-1): провайдер выбирается ОДИН раз здесь и идёт под своим именем во все ветки
        # (sequential/обычная) — иначе автовыбор терялся бы по дороге (v2.120/v3.0-rc2).
        _pres = ai_ops_run.resolve_provider_for_run(a.provider, Path(child_root), execute=True,
                                                    quiet=a.json)
        # F-026: прогон без вызова модели не доводится до вердикта — отказ (офлайн только `--provider mock`).
        _refusal = ai_ops_run.live_provider_refusal(_pres, a.provider)
        if _refusal:
            if a.json:
                print(json.dumps({"kind": "run", "status": "error", "exit": 2,
                                  "error": _refusal, "provider_resolution": _pres},
                                 ensure_ascii=False, indent=2))
            else:
                print(f"ОТКАЗ: {_refusal}")
            return 2
        provider = _pres["provider"]
        # v3.22: `do` форсирует флаги автономного прогона
        if intent == "do":
            flags["author"] = True
            flags["review"] = True
            a.open_pr = True
        # #542: --parallel — ЯВНЫЙ opt-in настоящей конкурентности (disposable-клон на пакет, governed
        # fan-in). Дефолт НЕ трогаем: ветка входит только при явном флаге. Атомарная задача -> None ->
        # проваливаемся в обычный прогон ниже. Основной checkout не трогается (работа на клонах).
        if getattr(a, "parallel", False):
            from ai_ops_kit.engine import parallel_live_dispatch
            _pwid = a.feature or _wid_for(task, signals, a.feature)
            _prec = parallel_live_dispatch.run_parallel_live(
                task, signals, Path(child_root), feature=_pwid, provider=provider, model=a.model,
                base=a.base, open_pr=a.open_pr, max_steps=a.max_steps, repo_slug=getattr(a, "repo", None))
            if _prec is not None:
                parallel_live_dispatch.print_parallel(_prec)
                return parallel_live_dispatch.exit_code(_prec)
            print("— задача атомарна: конкурентный мультипакетный прогон не требуется, обычный прогон —")
        # v3.1/v2.120: --sequential — неатомарную задачу исполнить по WorkPackages (пакет за пакетом);
        # sequential НАСЛЕДУЕТ провайдера/модель/sandbox/install/baseline/open-pr/budget (аудит P0.2).
        if a.sequential:
            from ai_ops_kit.engine import atomic_planner
            from ai_ops_kit.engine import workpackage_executor
            from ai_ops_kit.engine import tool_loop
            from ai_ops_kit.providers import orchestrator
            wid = a.feature or _wid_for(task, signals, a.feature)
            wp = atomic_planner.decompose(signals, wid=wid, child_root=Path(child_root))
            # v3.0-rc13 (P1): доверенный retry — архив попытки + reset на checkpoint предшественника,
            # затем продолжаем как resume_from (без ручного git reset у пользователя).
            resume_from = a.resume_from
            if a.retry_package:
                rt = workpackage_executor.retry_package(Path(child_root), wid, a.retry_package)
                if not rt.get("ok"):
                    print(f"RETRY ОТКАЗ: {rt.get('error')}")
                    return 2
                print(f"RETRY {a.retry_package}: ветка восстановлена на checkpoint "
                      f"{(rt.get('checkpoint') or '')[:12]} (предшественник {rt.get('predecessor') or 'база'}); "
                      f"попытка заархивирована -> {rt.get('archived_attempt') or '—'}")
                resume_from = a.retry_package
            if wp["should_decompose"] and wp["work_packages"]:
                base_prop = tool_loop.make_model_proposer(orchestrator.make_provider(provider, a.model))
                auth = orchestrator.make_provider(provider, a.model) if flags["author"] and provider != "mock" else None
                rev = orchestrator.make_provider(provider, a.model) if flags["review"] and provider != "mock" else None
                print(f"— исполняю по WorkPackages: {len(wp['work_packages'])} пакет(ов) —")
                seq = workpackage_executor.execute_sequence(
                    task, signals, Path(child_root), wp["work_packages"], lambda pkg: base_prop,
                    feature=wid, base=a.base, provider_name=provider, model=a.model,
                    author=flags["author"], author_proposer=auth,
                    review=flags["review"], reviewer_proposer=rev, baseline_diff=flags["baseline_diff"],
                    sandbox=flags["sandbox"], install_deps=True, open_pr=a.open_pr, max_steps=a.max_steps,
                    # v2.123 (P0.3): package write_scope РЕАЛЬНО протянут — брокер ограничит пакет его каталогом
                    write_scope_for=lambda pkg: pkg.get("write_scope"),
                    resume_from=resume_from)   # v2.124: resume; v3.0-rc13: retry -> resume_from=retry-package
                _dlv = seq.get("delivery") or {}
                print(f"SEQUENCE {wid}: executed_all={seq['executed_all']} · ready_all={seq['ready_all']} · "
                      f"пакетов {seq['total']} · остановлен_на={seq['stopped_at'] or '—'}"
                      + (f" · доставка={_dlv.get('status')}" if _dlv.get('requested') else ""))
                for p in seq["packages"]:
                    print(f"  [{p['id']}] {p['status']} · sha={(p.get('sha') or '')[:12] or '—'} · ready={p.get('ready')}")
                # v2.120/2.124 exit-код: 0 — ready_all И (если запрошен PR) он реально открыт;
                # 1 — исполнено, но не готово / доставка не удалась; 2 — цепочка блокирована/ошибка.
                if seq["ready_all"]:
                    if _dlv.get("requested") and _dlv.get("status") not in ("opened", "updated"):
                        return 1   # готово, но draft PR не открыт -> не полный успех
                    return 0
                return 1 if seq["executed_all"] else 2
            print("— задача атомарна: последовательное исполнение не требуется, обычный прогон —")
        _announce_start(Path(child_root))
        # v3.22: session guard ДО старта — snapshot + relation по факту + delegation
        _session_guard_before_start(Path(child_root), task, signals, a.feature)
        # v2.120: канонический вход ПРОВОДИТ провайдера/модель/base/open-pr/max-steps/require-fix в движок
        # (аудит P0.1: раньше уходило в mock). v3.22: `do` добавляет review_fix_attempts=2.
        review_fix = 2 if intent == "do" else getattr(a, "review_fix_attempts", 0)
        rep = ai_ops_run.run(task, signals, Path(child_root), engine=flags["engine"],
                             session=_session_identity(child_root),
                             feature=a.feature, execute=True, sandbox=flags["sandbox"],
                             baseline_diff=flags["baseline_diff"], review=flags["review"],
                             author=flags["author"], provider_name=provider, model=a.model,
                             base=a.base, open_pr=a.open_pr, max_steps=a.max_steps,
                             takeover=getattr(a, "takeover", False),
                             takeover_reason=getattr(a, "takeover_reason", None),
                             require_fix=flags.get("require_fix", False),
                             review_fix_attempts=review_fix,
                             # #570-follow-up: `--reevaluate-only` теперь принимается и CLI-обёрткой
                             # (`./ai-ops run ... --reevaluate-only`), а не только движком напрямую —
                             # иначе штатная переоценка после записи вердикта-ревью падала бы
                             # «unrecognized arguments». Уровень (task_type) берётся из сохранённых на
                             # specify сигналов (`_carry_stored_signals`), поэтому ENGINEERING не
                             # превращается молча в QUICK на переоценке без --signals.
                             reevaluate_only=getattr(a, "reevaluate_only", False),
                             provider_resolution={k: _pres.get(k) for k in
                                                  ("provider", "source", "reason", "warning")})
        ai_ops_run.print_human(rep)
        return ai_ops_run.exit_code(rep)
    return 0
