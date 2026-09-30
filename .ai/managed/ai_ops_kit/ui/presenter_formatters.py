#!/usr/bin/env python3
"""Переводчики повседневных команд Human Communication Layer (вынесено из `presenter.py`).

Модуль-сосед `presenter.py`: он стал god-модулем на ~1500 строк, и группа переводчиков
повседневных команд («что я собираюсь сделать», онбординг стека, каркас работы, план, описание
задачи, discovery, ревью ветки, инженерный совет) выделена сюда без изменения поведения. Контракт
`UserMessage` и рендер остаются в `presenter.py`; здесь — только форматтеры, которые собирают
`UserMessage` из сырых внутренних отчётов.

`message` и `_q` импортируются из `presenter.py` (их дом), а сам `presenter.py` реэкспортирует
эти функции обратно — так внешние вызовы `presenter.from_review(...)` продолжают работать. Цикла
нет: `presenter.py` определяет `message`/`_q` в начале файла, задолго до реэкспорта в конце.
"""
from __future__ import annotations

from ai_ops_kit.shared import gate_dimensions  # карта gate -> продуктовая фраза (общий нижний слой)
from ai_ops_kit.ui.presenter import _q, message
from ai_ops_kit.ui.presenter_ceremony import risk_ceremony_line  # связь риск->церемония (спутник)

# Вторая, более крупная группа переводчиков (ход и решения работы) вынесена в модуль-сосед
# `presenter_report_formatters.py`, чтобы этот файл держался под потолком размера. Реэкспорт
# оставляет `presenter_formatters.from_bootstrap(...)` и обращения `presenter.from_*` рабочими,
# а сам импорт — тот не-тестовый потребитель, без которого сосед был бы «построен, но не проведён».
from ai_ops_kit.shared.project_detector import lint_unguarded  # где «линтера нет» = «не нашёл»
from ai_ops_kit.ui.presenter_report_formatters import (
    code_style_unguarded,
    from_bootstrap,
    from_doctor,
    from_first_hour,
    from_intake_gap,
    from_kit_feedback_recorded,
    from_process_spend,
    from_session_economy,
    from_short_path,
    from_subsession_decision,
)


# ── Переводчики повседневных команд ───────────────────────────────────────────────────────────
# Слой коммуникации существовал для трёх команд из двенадцати. Остальные печатали внутреннее
# состояние напрямую — `ONBOARD: стек python · профиль записан …`, `SPECIFY: создан …`,
# `■ intent: run · понял: QUICK -> workflow QUICK · спецификация L0`, — и настройка «с кем ты
# говоришь» на них не влияла вовсе. Пользовательское ревью назвало это одним дефектом: чаще всего
# человек видит именно эти команды, и именно в них он читает лог вместо ответа.

def from_execution_preview(pv: dict) -> dict:
    """`build_preview()` -> UserMessage. «Что я собираюсь сделать» до запуска.

    Внутренние имена стадий и флагов остаются в технических деталях: они нужны, когда прогон пошёл
    не так, но в них нет ни одного слова о том, что произойдёт с продуктом.
    """
    u = pv.get("understood") or {}
    wd = pv.get("will_do") or {}
    du = pv.get("data_used") or {}
    approvals = list(pv.get("approvals_needed") or [])
    ctx_error = du.get("context_error")
    tech = {"intent": pv.get("intent"), "task_type": u.get("task_type"),
            "workflow": u.get("workflow"), "spec_level": u.get("spec_level"),
            "stages": len(wd.get("stages") or []), "auto_flags": wd.get("auto_flags"),
            "agents": len(du.get("agents") or []),
            "estimated_tokens": du.get("estimated_tokens"),
            "context_budget": du.get("context_budget")}
    if ctx_error:
        tech["context_error"] = ctx_error
    what = str(pv.get("expected_result") or "выполню намерение").strip()
    summary = (what[:1].upper() + what[1:]).rstrip(".") + "."

    steps = []
    if pv.get("decomposition_advised"):
        steps.append("задача больше одного шага — советую разбить её, иначе результат будет трудно "
                     "проверить")

    if ctx_error:
        # ДЕГРАДАЦИЯ ВИДНА НА ВСЕХ ТРЁХ УРОВНЯХ. Прежде сбой сборки контекста давал `агентов 0 ·
        # ~None ток.` — прогон вслепую выглядел как обычный (137 проглоченных исключений, внешнее
        # ревью). Продакту тем более нельзя показывать это как норму: он не читает числа.
        return message(
            status="degraded", headline="Могу запустить, но материалы проекта не собрались",
            summary=summary,
            why_it_matters="Прогон пойдёт без контекста продукта: я не смогу опереться ни на "
                           "правила, ни на прошлые решения, и оценку стоимости тоже не дам.",
            next_steps=steps + ["скажи, если запускать всё равно — иначе сначала разберусь, "
                                "почему контекст не собрался"],
            technical=tech)

    if approvals:
        return message(
            status="needs_input",
            summary=summary,
            why_it_matters="Задача задевает то, что я не меняю без твоего слова.",
            decision={"question": "разрешить: " + "; ".join(approvals),
                      "recommendation": "посмотреть, что именно затронуто, и подтвердить — "
                                        "без ответа я не начинаю",
                      "on_approve": "запускаю и приношу результат на проверку",
                      "on_reject": "предложу вариант, который этого не трогает"},
            next_steps=steps or None, technical=tech)

    # #708: если движок стартует сразу (do / run --execute), не зовём «запускай, когда готов» —
    # это противоречит немедленному запуску. Для превью (голый run) ожидание запуска верно.
    _default_step = ("запускаю сейчас — результат ниже" if pv.get("will_execute_now")
                     else "запускай, когда готов")
    return message(status="ok", headline="Вот что я сделаю", summary=summary,
                   next_steps=steps or [_default_step], technical=tech)


# Внутреннее имя команды -> то, как её называет человек. Нужно потому, что пробел в профиле надо
# назвать своими словами: в поле продакт прочитал «не выведены команды ['build', 'lint',
# 'typecheck', 'test']» — repr списка Python посреди русской фразы.
_CMD_RU = {"build": "сборки", "test": "тестов", "lint": "линтера", "typecheck": "проверки типов",
           "install": "установки зависимостей", "dev": "запуска", "run": "запуска",
           "format": "форматирования", "e2e": "сквозных тестов"}


def from_onboarding_profile(prof: dict, written: str) -> dict:
    """`project_detector.detect()` -> UserMessage. «На чём написан проект и чем он проверяется».

    Отсутствие стека — не «проект пустой», а «не смог определить»: без него кит не знает, чем
    собирать и чем тестировать, и молчаливый `ok` здесь означал бы зелёный свет на пустом месте.

    Пробел называется по СТРУКТУРЕ профиля, а не пересказом готовых строк `undetermined`: те
    написаны для инженера и содержат внутренние подробности. Сами строки остаются в деталях.
    """
    stacks = list(prof.get("stacks") or [])
    langs = [str(s.get("language") or "?") for s in stacks]
    undetermined = list(prof.get("undetermined") or [])
    silent = [str(s.get("language") or "?") for s in stacks
              if not {k: v for k, v in (s.get("commands") or {}).items() if v}]
    tech = {"профиль": written, "стеки": ", ".join(langs) or "—",
            "команды": "; ".join(
                f"{s.get('language')}: " + (", ".join(f"{k}={v}" for k, v in
                                                      (s.get("commands") or {}).items() if v)
                                            or "не найдены") for s in stacks) or "—",
            "не определено": ", ".join(undetermined) or "—"}

    if not stacks:
        return message(
            status="degraded", headline="Не понял, на чём написан проект",
            summary="Стек определить не удалось.",
            why_it_matters="Это не «здесь ничего нет» — это «я не знаю»: без стека я не могу "
                           "сказать, чем проект собирается и чем проверяется.",
            next_steps=["назови язык и команды сборки и тестов — запишу и дальше буду ими "
                        "пользоваться"],
            technical=tech)

    what = ", ".join(langs)
    # format — не пробел проверки (гейт его не гоняет): без форматтера профиль не «неполон»
    missing_cmds = sorted({k for s in stacks for k, v in (s.get("commands") or {}).items()
                           if not v and k != "format"})
    # #1183: без линтера — не «не хватает команды», а последствие для кода и совет, что поставить
    bare = lint_unguarded(prof)
    style = (" " + code_style_unguarded(bare)) if bare else ""
    notes = []
    if missing_cmds:
        notes.append("команды для " + ", ".join(_CMD_RU.get(k, k) for k in missing_cmds))
    if prof.get("monorepo"):
        notes.append("покрывают ли корневые команды все пакеты — это монорепозиторий")
    if silent and not missing_cmds:
        notes.append(f"ни одной команды для {', '.join(silent)}")
    if notes:
        return message(
            status="degraded", headline="Разобрался, но не до конца",
            summary=f"Проект написан на {what}.",
            why_it_matters="Чего я не знаю: " + "; ".join(notes) + ". Пока это так, часть проверок "
                           "я провести не смогу и не буду делать вид, что провёл." + style,
            next_steps=["скажи недостающие команды — или спроси «что дальше», и я начну работу "
                        "с тем, что уже знаю"],
            technical=tech)
    if undetermined:
        # Остались непереведённые пробелы: назвать их своими словами я не умею, но и умолчать о том,
        # что профиль неполон, не имею права — «не знаю» не превращается в «в порядке».
        n = len(undetermined)
        return message(
            status="degraded", headline="Разобрался, но не до конца",
            summary=f"Проект написан на {what}.",
            why_it_matters=f"В профиле осталось {n} {_q(n, 'место', 'места', 'мест')}, где я не "
                           f"уверен; своими словами объяснить их не могу — покажу как есть.",
            next_steps=["покажу технические детали — там сказано, чего именно не хватает"],
            technical=tech)

    return message(status="ok", headline="Разобрался с проектом",
                   summary=f"Проект написан на {what}; чем его собирать и проверять — я нашёл.",
                   next_steps=["спроси «что дальше» — предложу работу с обоснованием"],
                   technical=tech)


def from_new_feature(workitem_id, title, spec_created, next_command) -> dict:
    """Создание каркаса работы -> UserMessage. Каркас — это ещё не работа, и это надо сказать."""
    return message(
        status="ok", headline="Место для работы готово",
        summary=f"Завёл работу «{title}».",
        why_it_matters="Сделано пока ничего: это только место, куда лягут описание и результат.",
        next_steps=[f"опиши, что нужно получить: {next_command}"],
        technical={"workitem_id": str(workitem_id),
                   "workitem": f"features/{workitem_id}/workitem.yaml",
                   "spec": "создана" if spec_created else "уже была"})


def from_plan_built(workitem_id, workflow, spec_level, packages, context_error=None) -> dict:
    """Построенный RunPlan -> UserMessage. Главное для человека: КОД НЕ МЕНЯЛСЯ."""
    tech = {"workitem_id": str(workitem_id), "workflow": workflow, "spec_level": spec_level,
            "work_packages": packages, "артефакты": f"features/{workitem_id}/"}
    n = int(packages or 0)
    big = (f" Задача крупная, поэтому разбита на {n} "
           f"{_q(n, 'шаг', 'шага', 'шагов')}." if n else "")
    if context_error:
        tech["context_error"] = context_error
        return message(
            status="degraded", headline="План есть, но собран не полностью",
            summary="План работы готов; код я не менял." + big,
            why_it_matters="Материалы проекта не собрались, поэтому оценка объёма — по умолчаниям, "
                           "а не по твоему продукту.",
            next_steps=["разберусь, почему контекст не собрался, — иначе оценка будет неточной"],
            technical=tech)
    return message(
        status="ok", headline="План работы готов",
        summary="Понял, что и в каком порядке делать; код я не менял." + big,
        next_steps=["скажи «запускай» — начну исполнение и принесу результат на проверку"],
        technical=tech)


def from_specification(path, created, level_name, sections, blocking_missing, next_command,
                       added=None, add_error=None, spec_provisional=False,
                       sections_if_escalated=None, level_if_escalated=None,
                       answer_command=None, applied=None, unmatched=None,
                       answer_error=None, task=None, level_reason=None) -> dict:
    """Спецификация задачи -> UserMessage. Незаполненные разделы — работа человека, и она названа.

    F-029: `added` — разделы, ДОПИСАННЫЕ в уже существующий файл под поднявшийся уровень. Без него
    сообщение звучало «заготовка уже была; заполнить нужно 9 разделов», а в файле лежало 6 разделов
    прошлого уровня — заполнять было нечего. `add_error` — честная причина, если дописать не вышло
    (битый spec.yaml не переписываем: описанное человеком дороже незакрытого гейта).

    Исход 3 (#768, obs 8a891ce7): `spec_provisional` — форма выдана по ПРЕДВАРИТЕЛЬНОЙ классификации
    (тяжесть size/risk не заявлена). Тогда называем ДО заполнения, до какого уровня
    (`level_if_escalated`) дорастёт форма и какие разделы (`sections_if_escalated`) добавит эскалация
    на прогоне, — чтобы человек не заполнил не ту форму и не узнал об этом задним числом.

    #863 (живой zero-touch прогон): путь описания задачи не показывает владельцу ФАЙЛ. Раньше
    единственным next_step было «заполни разделы в <path>» — правка YAML руками. Теперь
    незаполненные разделы называются ВОПРОСАМИ обычным языком (`ai_ops_kit.shared.spec_answers`),
    а `answer_command` — готовая команда ответить словами, не открывая файл (`--answers
    "слово=ответ; …"`), которую тот же модуль умеет разобрать обратно. `applied`/`unmatched`/
    `answer_error` — честный итог ЭТОГО вызова, если ответ уже пришёл вместе с ним: что
    записалось, какие слова кит не узнал, и не сломалась ли запись (битый spec.yaml не трогаем —
    тот же принцип fail-closed, что у `add_error`)."""
    from ai_ops_kit.shared import spec_answers
    n_missing = len(blocking_missing or [])
    n_added = len(added or [])
    n_applied = len(applied or [])
    tech = {"spec": str(path), "уровень": level_name, "разделов": len(sections or []),
            "не заполнено": ", ".join(blocking_missing or []) or "—",
            "создана": bool(created), "дописано": ", ".join(added or []) or "—"}
    if add_error:
        tech["дописать не удалось"] = str(add_error)
    if applied:
        tech["записано в этом ответе"] = ", ".join(sorted(applied))
    if unmatched:
        tech["не узнал слова"] = ", ".join(unmatched)
    if answer_error:
        tech["запись ответа не удалась"] = str(answer_error)
    # Исход 3 (#768) + #958 (risk_selects_the_process_not_the_human): провизорность — это внутренний
    # выбор процесса по тяжести задачи, а НЕ вопрос человеку. В лицо человеку летит простой вопрос
    # «насколько это крупно и опасно» обычными словами — БЕЗ имён уровней (L0/L1/QUICK/ENGINEERING),
    # БЕЗ числа разделов и БЕЗ терминов size/risk/эскалация. Точный уровень, разделы и то, что форма
    # предварительная, остаются в технических деталях (presenter показывает их по запросу) — кит
    # держит связь «слова тяжести -> уровень процесса» у себя.
    _disclosure = ""
    if spec_provisional:
        tech["форма предварительная"] = (
            f"тяжесть (size/risk) не заявлена; при эскалации уровень {level_if_escalated}, "
            f"добавятся разделы: {', '.join(sections_if_escalated or []) or '—'}")
        _disclosure = (
            " Скажи заодно, насколько задача крупная и рискованная — от этого зависит, "
            "насколько тщательно я её проведу. Можно просто словами: «небольшая, неопасная» "
            "или «крупная, рискованная».")
    # F-032 + #958: слова пользователя — не внутренняя кухня. Кит эхом повторяет, ЧТО он понял,
    # продуктовым языком, БЕЗ feature id и БЕЗ CLI-флагов. Так текст задачи виден человеку в самом
    # выводе (а путь репозитория и id остаются в технических деталях), и подтверждение задачи —
    # лучший UX, чем эхо CLI-команды, которое эту роль раньше нечаянно выполняло.
    # risk-based-ceremony: человек ВИДИТ, что объём процесса выбран РИСКОМ, а не полнотой, и ПОЧЕМУ.
    # Провизорность честно проговариваем прямо здесь (в _disclosure — вопрос к человеку; тут — причина).
    _ceremony = risk_ceremony_line(level_name, level_reason, provisional=spec_provisional)
    tech["процесс подобран по риску"] = _ceremony
    _echo = f"Понял: «{task.strip()}». " if (task or "").strip() else ""
    if created:
        _origin = "начата"
    elif n_added:
        # #958 (risk_selects_the_process_not_the_human): без имени уровня процесса — оно в tech.
        _origin = f"уже была — добавила {n_added} {_q(n_added, 'вопрос', 'вопроса', 'вопросов')}"
    else:
        _origin = "уже была"
    if n_missing:
        questions = spec_answers.questions_for(blocking_missing)
        shown_q = questions[:4]
        ask = "; ".join(shown_q)
        if len(questions) > 4:
            ask += f" — и ещё {len(questions) - 4} (полный список в технических деталях)"
        steps = []
        if unmatched:
            steps.append("несколько слов из ответа я не узнал: " + ", ".join(unmatched)
                        + " — назови их словами из вопросов ниже")
        steps.append("ответь словами, не открывая файл: " + ask)
        steps.append("потом скажи, что переходим к плану — дальше я работаю сам")
        # #958 (исход one_input_hides_the_pipeline): точные команды (синтаксис `--answers`,
        # следующий шаг) — НЕ в лицо человеку. В next_steps остаётся продуктовая формулировка
        # «ответь словами», а сама механика (и внутренний feature id кит держит на своей стороне)
        # уезжает в технические детали, которые presenter показывает только по запросу.
        if answer_command:
            tech["ответить командой"] = answer_command
        tech["следующий шаг"] = next_command
        return message(
            status="needs_input",
            summary=(_ceremony + " " + _echo + "Описание задачи " + _origin
                     + f"; осталось ответить на {n_missing} "
                       f"{_q(n_missing, 'вопрос', 'вопроса', 'вопросов')}."
                     + (f" Записано в этом ответе: {n_applied}." if n_applied else "")
                     + (f" Дописать разделы не удалось: {add_error}." if add_error else "")
                     + (f" Запись ответа не удалась: {answer_error}." if answer_error else "")
                     + _disclosure),
            why_it_matters="Отвечать за тебя я не буду: это как раз то, что из кода не "
                           "выводится, — зачем задача и как поймём, что получилось. Но открывать "
                           "файл не обязательно — можно просто сказать словами.",
            next_steps=steps,
            technical=tech)
    tech["следующий шаг"] = next_command
    return message(status="ok", headline="Описание задачи готово",
                   summary=_ceremony + " " + _echo + "Всё, что нужно было описать, описано." + _disclosure,
                   next_steps=["скажи, что переходим к плану — дальше я работаю сам"],
                   technical=tech)


def from_discovery_draft(path, created) -> dict:
    """Черновик discovery -> UserMessage. Пустой черновик — не результат, а приглашение."""
    return message(
        status="needs_input",
        summary=("Черновик для обсуждения идеи " + ("создан" if created else "уже был") + "."),
        why_it_matters="Он пустой намеренно: чью боль решаем и как поймём, что помогло, "
                       "я за тебя не придумаю.",
        next_steps=[f"заполни разделы в {path}",
                    "потом попроси построить описание задачи — дальше я работаю сам"],
        technical={"draft": str(path), "создан": bool(created)})


# id гейта -> короткая ПРОДУКТОВАЯ фраза о том, ЧТО проверено (#958, исход
# `verified_is_shown_as_trust_not_gate_list`). Карта ВЫНЕСЕНА в общий нижний слой
# `ai_ops_kit/shared/gate_dimensions.py`, потому что тот же смысл нужен на пути ПРОГОНА (engine),
# а engine не вправе импортировать `ui` (обратный импорт запрещён). Здесь — только чтение карты ВНИЗ,
# поведение `from_review` не меняется.
_GATE_PRODUCT_DIMENSION = gate_dimensions.GATE_PRODUCT_DIMENSION


def _verified_why(passed_gates) -> tuple:
    """Свод «что проверено» продуктовыми словами для ветки `ready`. -> (clause, why).
    ИНВАРИАНТ ЧЕСТНОСТИ (ядро кита, «no false green»): называем ТОЛЬКО реально пройденные измерения и
    НЕ выдаём мнение за машинную проверку. Источник различаем без запроса в слой гейтов (`ui` не
    зависит от `gates`): в `ready` попадают ИСКЛЮЧИТЕЛЬНО ai-review гейты (`_reviewable_gates` берёт
    `classify == "ai-review"`, writer≠judge) — значит измерение здесь заключение НЕЗАВИСИМОГО РЕВЬЮЕРА,
    не детерминированного валидатора; машинной проверкой это не зовём.
    """
    dims = [d for gid in passed_gates for d in (_GATE_PRODUCT_DIMENSION.get(gid),) if d]
    if not dims:  # пройденные гейты есть, но продуктовых имён нет — измерения не выдумываем
        return ("Независимая проверка пройдена — смотрел не тот, кто делал работу.", None)
    clause = ("Независимый ревьюер (не тот, кто делал работу) посмотрел и принял: "
              + ", ".join(dims) + ".")
    # опора доверия честна: заключение ревьюера, не автоматические тесты (инвариант «no false green»)
    return clause, ("Это заключение независимого ревьюера, а не автоматических тестов, — но проверял "
                    "не тот, кто делал работу, и потому «замечаний нет» здесь имеет основание.")


def from_review(rep: dict) -> dict:
    """`review_branch.review()` -> UserMessage.

    ШЕСТЬ ВЕРДИКТОВ, И ТРИ ИЗ НИХ НЕ «ГОТОВО». `pass` — проверено. `no-ai-review-gates` — готово
    вливать, но НИЧЕГО не проверялось (ревьюируемых гейтов в плане нет). `needs-reviewer` — работа
    сделана, судить было некому: своё же изменение кит судить не вправе (writer ≠ judge).
    `no-branch` — сверять нечего. Каждый случай назван своим именем: общее «готово» на любом из них
    и есть то, из-за чего слой человеческого языка мог бы стать способом скрывать, а не объяснять.
    """
    readiness = rep.get("readiness") or {}
    ready = bool(readiness.get("ready_for_merge"))
    verdict = rep.get("verdict")
    reviews = rep.get("reviews") or []
    changed = len(rep.get("changed_files") or [])
    tech = {"verdict": verdict, "ready_for_merge": ready,
            "основание": readiness.get("reason") or "—",
            "гейтов на ревью": len(rep.get("reviewable") or []),
            "изменено файлов": changed,
            # БАЗА РЯДОМ С ЧИСЛОМ: «изменено файлов 0» без базы неотличимо от «база не выбрана»
            # (заявка #136 — там же справка обещала автоподбор, которого не было).
            "база дифа": (rep.get("base") or "не выбрана")
                         + (f" ({rep['base_source']})" if rep.get("base_source") and rep.get("base") else "")
                         + (f" — {rep['base_note']}" if rep.get("base_note") else ""),
            "по гейтам": "; ".join(f"{r.get('gate')}: {r.get('status') or 'без вердикта'}"
                                   for r in reviews) or "—",
            "evidence": rep.get("evidence_path") or "—", "note": rep.get("note") or "—"}

    if verdict == "no-branch":
        return message(
            status="degraded", headline="Проверять нечего",
            summary="Ветки с изменениями по этой работе нет.",
            why_it_matters="Это не «замечаний нет» — это «нечего смотреть».",
            next_steps=["скажи, какую работу проверять, или начни её — тогда появится что сверять"],
            technical=tech)

    if verdict == "error":
        return message(
            status="blocked", headline="Проверку провести не удалось",
            summary="Независимая проверка сломалась на полпути.",
            why_it_matters="Ни «готово», ни «не готово» я сказать не могу: проверки не было.",
            next_steps=["разберусь, почему она не запустилась"], technical=tech)

    if verdict == "no-ai-review-gates":
        return message(
            status="ok", headline="Вливать можно, но проверка не проводилась",
            summary="У этой работы нет мест, которые я обязан отдавать на независимую проверку.",
            why_it_matters="Поэтому «замечаний нет» здесь значит «их никто не искал» — "
                           "решение вливать за тобой.",
            next_steps=["можно вливать"], technical=tech)

    # «Вердикта нет» — это либо явный `needs-reviewer`, либо ни одного годного вердикта среди
    # проведённых ревью. Второй случай важнее: он выглядит как проведённая проверка.
    no_verdict = verdict == "needs-reviewer" or (
        bool(reviews) and all((r.get("status") or "") in ("", "invalid") for r in reviews))
    if no_verdict:
        return message(
            status="degraded", headline="Проверять было некому",
            summary="Работа сделана, но независимую проверку я не провёл.",
            why_it_matters="Своё же изменение я судить не имею права, а живого проверяющего "
                           "здесь не было. Это не «всё хорошо» — это «не проверено».",
            next_steps=["подключи проверяющего — тогда у вердикта появится основание"],
            technical=tech)

    if ready:
        # #958 (verified_is_shown_as_trust_not_gate_list): в «Проверено» человек видит, ЧТО ИМЕННО
        # проверено продуктовыми словами, а не число «гейты 3/3»/«замечаний нет». Основание доверять
        # важнее счётчика; имена гейтов и числа остаются в технических деталях (см. `tech`).
        passed_gates = [r.get("gate") for r in reviews
                        if r.get("status") == "pass" and r.get("valid", True) and r.get("gate")]
        clause, why = _verified_why(passed_gates)
        return message(
            status="ok", headline="Проверено",
            summary="Вот что именно проверено. " + clause,
            why_it_matters=why,
            next_steps=["можно вливать"], technical=tech)

    if verdict != "needs-changes":
        # Незнакомый вердикт — не «всё плохо» и тем более не «всё хорошо»: я его не понимаю.
        return message(
            status="degraded", headline="Не понимаю итог проверки",
            summary=f"Проверка вернула незнакомый мне итог: {verdict}.",
            why_it_matters="Пересказывать его своими словами я не буду — это была бы выдумка.",
            next_steps=["покажу отчёт проверки как есть"], technical=tech)

    return message(
        status="blocked", headline="Пока вливать нельзя",
        summary="Проверка нашла, что нужно доделать.",
        why_it_matters="Пока замечания не закрыты, изменение не готово — даже если код работает.",
        next_steps=["покажу замечания по порядку и закрою их"], technical=tech)


def from_advice(result: dict) -> dict:
    """`advise` -> UserMessage. ВЕДЁТ с «что нужно ПРОДУКТУ» (#958), инженерная настройка кита —
    после и явно отделена. Совет — не исполнение, и это должно быть видно.

    Продуктовая часть говорит о продукте пользователя простым языком: возможности, пробелы,
    следующий шаг. Инженерная часть (окружения/поставка/процесс — про настройку самого кита)
    уходит в технические детали и в отдельную строку «Дальше», чтобы её не спутать с продуктом.
    Инвариант честности: нет продуктовых данных → так и говорим, потребность не выдумываем.
    """
    pa = result.get("product_advice") or {}
    precs = list(pa.get("recommendations") or [])
    enough = pa.get("enough_product_data")

    eng_recs = list(result.get("recommendations") or [])

    # Технические детали: сперва продуктовые рекомендации с источниками, затем — ЯВНО отделённая
    # инженерная часть про настройку кита (её лексика — .ai-ops.yaml, CI и т.п. — живёт только тут).
    tech = {"продуктовых рекомендаций": len(precs)}
    tech.update({f"продукт · {p.get('kind')} {i + 1}":
                 f"{p.get('need')} — {p.get('why')} (источник: {p.get('source')})"
                 for i, p in enumerate(precs)})
    if pa.get("note"):
        tech["продукт · примечание"] = pa["note"]
    tech["— ниже про НАСТРОЙКУ КИТА, не про продукт —"] = (
        f"{len(eng_recs)} инженерных заметок")
    tech["repository"] = result.get("repository")
    tech["task_type"] = result.get("task_type") or "—"
    tech.update({f"настройка кита · [{r.get('category')}] {i + 1}":
                 f"{r.get('advice')} (источник: {r.get('source')})"
                 for i, r in enumerate(eng_recs)})

    # Инженерная часть подаётся ПОСЛЕ и отдельными словами: «это про настройку кита, не про продукт».
    eng_line = (f"отдельно есть заметки про настройку самого кита — это не про продукт, "
                f"покажу по запросу") if eng_recs else None

    if precs:
        lead = precs[0]
        needs = "; ".join(p.get("need", "") for p in precs)
        next_steps = ["возьмусь за первое, если скажешь"]
        if len(precs) > 1:
            next_steps.append("остальное по продукту покажу списком")
        if eng_line:
            next_steps.append(eng_line)
        return message(
            status="ok", headline="Что нужно продукту",
            summary=f"Вот что, по-моему, сейчас важно для твоего продукта: {needs}.",
            why_it_matters=lead.get("why"),
            next_steps=next_steps, technical=tech)

    # Продуктовых рекомендаций нет. Честно различаем «данных не хватает» и «данные есть, срочного нет».
    if not enough:
        next_steps = ["заполним продуктовую картину — тогда смогу советовать по продукту"]
        if eng_line:
            next_steps.append(eng_line)
        return message(
            status="degraded", headline="Пока не могу советовать по продукту",
            summary="Пока не хватает продуктовых данных, чтобы советовать по продукту.",
            why_it_matters="Придумывать продуктовую потребность я не буду — это была бы выдумка, "
                           "а не совет.",
            next_steps=next_steps, technical=tech)

    next_steps = ["спроси «что дальше» — предложу работу"]
    if eng_line:
        next_steps.append(eng_line)
    return message(
        status="ok", headline="По продукту сейчас советовать нечего",
        summary="Продуктовые данные есть, но срочного по продукту сейчас нет.",
        next_steps=next_steps, technical=tech)


# ── Переводчики внутренних отчётов: чтение состояния проекта ───────────────────────────────────
# Вторая группа, вынесенная из `presenter.py`: переводчики сырых внутренних отчётов. Здесь осталась
# её читающая-состояние часть — осмотр репозитория, сверка контуров, здоровье продукта; более
# крупная часть про ход и решения работы (экономика сессии, первый час, bootstrap, doctor и др.)
# уехала в модуль-сосед `presenter_report_formatters.py` (реэкспорт в шапке).
# Поведение не меняется — это тот же шов «внутренние имена внутри, наружу выходит смысл».

def from_repository_understanding(rep: dict) -> dict:
    """`repo_audit.run()` -> UserMessage.

    Плохо: «Artifact coverage 8/15. architecture=inferred, data_model=partial, delivery=verified».
    Хорошо: «Осмотрел проект. Техническую картину восстановил сам; не хватает того, что из кода
    честно не узнать». Числа остаются в технических деталях — они не врут, они просто не ответ.
    """
    cls = rep["classification"]["class"]
    aud = rep["audit"]
    ask = rep["ask"]
    n_q = len(ask["questions"])
    known = ", ".join(k.replace("_", " ") for k, v in rep["reconstructed"].items()
                      if v["status"] in ("verified", "inferred") and v.get("value"))
    human_needed = [c["title"] for c in aud["contours"] if c["needs_human"]]
    # Противоречие источников истины — не «известно» и не «неизвестно»: кит ВИДИТ факт, но источники
    # спорят. Это отдельная строка доверия, и её нельзя проглотить в числах контуров.
    conflicts = rep.get("conflicts") or []

    if cls == "NEW_PRODUCT":
        summary = ("Похоже, это новый продукт: работающей системы и истории разработки я не нашёл. "
                   "Сначала соберём минимальную модель продукта, потом смогу предложить "
                   "архитектуру и план работ.")
        why = None
    elif cls == "UNKNOWN":
        summary = "Не смог осмотреть репозиторий — прочитать его содержимое не получилось."
        why = "Без этого любой мой вывод о проекте был бы выдумкой, поэтому я не начинаю."
    else:
        summary = ("Я разобрался с проектом. Это " + ("уже работающий продукт"
                   if cls == "EXISTING_PRODUCT" else "ранняя стадия продукта") + ".")
        why = ("Техническую картину я восстановил сам" + (f": {known}" if known else "") +
               ". А то, что из кода честно узнать нельзя, спрошу у тебя — "
               "выдумывать это я не буду.") if known else None

    steps = []
    if n_q:
        steps.append(f"задам {n_q} "
                     + ("короткий " if n_q % 10 == 1 and n_q % 100 != 11 else "коротких ")
                     + _q(n_q))
    # ОНБОРДИНГ ЗАКАНЧИВАЕТСЯ РАБОТОЙ. Прежде здесь стояло «соберу недостающие материалы и покажу
    # их тебе на проверку» — обещание, которого кит не выполнял ничем: BOOTSTRAP существовал строкой
    # в реестре. Теперь названа команда, которая это делает, и она рядом.
    steps.append("соберу первое направление и план из фактов репозитория: ./ai-ops bootstrap")
    if not n_q:
        steps.append("после этого покажу, какую работу имеет смысл взять первой")

    # Противоречие источников поднимается ДО обычного «нужны ответы»: оно требует решения владельца
    # (какой источник актуален), а не сбора недостающего. Молчаливого выбора кит не делает.
    conflict_line = None
    if conflicts:
        conflict_line = ("Источники истины противоречат друг другу — сам выбрать актуальный не "
                         "могу: " + "; ".join(c["summary"] for c in conflicts))

    return message(
        status="needs_input" if (n_q or conflicts) else "ok",
        summary=summary, why_it_matters=(conflict_line or why), next_steps=steps,
        decision=({"question": f"ответить на {n_q} {_q(n_q)} о продукте, направлении и границах",
                   "recommendation": "ответить сразу — дальше я работаю без остановок",
                   "on_approve": "соберу базовую модель продукта и предложу первые задачи",
                   "on_reject": "оставлю эти области помеченными как «не подтверждено» и не буду "
                                "их выдумывать"} if n_q else None),
        technical={"classification": cls,
                   "confidence": rep["classification"]["confidence"],
                   "contours_verified": len(aud["ready"]),
                   "contours_total": len(aud["contours"]),
                   "ai_can_build": ", ".join(aud["ai_can_build"]) or "—",
                   "needs_human": ", ".join(human_needed) or "—",
                   "blocking_gaps": ", ".join(aud["blocking_gaps"]) or "—",
                   "conflicting_sources": ", ".join(c["category"] for c in conflicts) or "—",
                   "questions": n_q})


def from_contour_consistency(rep: dict) -> dict:
    """`contours.reconcile()` -> UserMessage. Ровно тот случай, ради которого модель нужна.

    ГЛАВНЫЙ ИНВАРИАНТ ОБЯЗАН ДОЖИВАТЬ ДО ЧЕЛОВЕКА. Прежде при отсутствии major-находок перевод
    печатал «Изменение согласовано с описанием продукта», выбрасывая все `unknown_contour`: кит
    проверил один контур из восьми и сообщил владельцу, что всё согласовано. `unknown` был защищён
    в `contours.py` пятью тестами и не защищён здесь ни одним — мутационное ревью это и поймало.
    Непроверенное называется непроверенным на всех трёх уровнях детализации.
    """
    findings = rep.get("findings") or []
    major = [f for f in findings if f.get("severity") == "major"]
    unknown = [f for f in findings if f.get("id") == "unknown_contour"]

    if not rep.get("comparable"):
        # «Сверять нечего» — это не прогресс. Прежде здесь стоял `ok`, и ярлык печатал
        # «Работа продвинулась» на месте непроведённой проверки.
        return message(
            status="degraded", headline="Сверять нечего",
            summary="Изменений не предъявлено.",
            why_it_matters="Это не «всё согласовано» — это «проверка не проводилась».",
            next_steps=["сверю, когда появится изменение"],
            technical={"comparable": False, "findings": len(findings)})

    if major:
        behind = [f["contour"] for f in major if f.get("id") == "source_of_truth_behind"]
        parts = []
        if behind:
            parts.append("изменилось то, что описано в проекте, а само описание не обновлено")
        other = [f for f in major if f.get("id") != "source_of_truth_behind"]
        if other:
            parts.append("есть расхождения между заявленным и сделанным")
        msg = message(
            status="degraded",
            summary="Изменение готово, но описание продукта за ним не поспело: "
                    + "; ".join(parts) + ".",
            why_it_matters="Следующая сессия — и человек, и агент — прочитает устаревшее описание "
                           "как правду. Именно так расходятся код и представление о нём."
                           + (f" Ещё {len(unknown)} "
                              f"{_q(len(unknown), 'область', 'области', 'областей')} проверить "
                              f"нечем." if unknown else ""),
            next_steps=["обновлю затронутые описания и покажу изменения",
                        "либо скажи, что менять их не нужно, и я запишу это как решение"],
            technical={f["contour"]: f["detail"] for f in major})
        return msg

    if unknown:
        n = len(unknown)
        return message(
            status="degraded", headline="Проверил не всё",
            summary=f"Расхождений не нашёл, но {n} "
                    f"{_q(n, 'область', 'области', 'областей')} продукта мне здесь не видно.",
            why_it_matters="Про них я не говорю «в порядке» — я говорю «не знаю»: подменять "
                           "признание утверждением значит зеленить непроверенное.",
            next_steps=[f"назови, где в проекте живут эти области "
                        f"({', '.join(f['contour'] for f in unknown[:3])}…), и я начну их видеть"],
            technical={f["contour"]: f["detail"] for f in unknown})

    return message(status="ok", headline="Согласовано",
                   summary="Изменение согласовано с описанием продукта — проверены все области.",
                   next_steps=["продолжаю"],
                   technical={"findings": len(findings)})


def from_product_health(rep) -> dict:
    """Product Health -> UserMessage. Отсутствие данных — НЕ «всё хорошо».

    Прежняя формулировка была честной по сути («без данных score не считается») и негодной по форме:
    путь к файлу и слово `score` продакту не нужны, а что делать дальше — не сказано.
    """
    if not rep:
        return message(
            status="degraded", headline="Пока не могу измерить",
            summary="Данных о состоянии продукта я не получил.",
            why_it_matters="Это не «всё хорошо» — это «не знаю»: считать по пустому месту я не буду.",
            next_steps=["подключи метрики продукта, и я начну показывать динамику"],
            technical={"input": "product/product-health.yaml", "status": "unavailable"})
    hs = (rep.get("health_score") or {})
    band = hs.get("band")
    value = hs.get("value")
    good = band in ("good", "excellent", "healthy")
    return message(
        status="ok" if good else "degraded",
        summary=(f"Состояние продукта: {band}." if band else "Состояние продукта измерено."),
        why_it_matters=None if good else "Стоит посмотреть, что тянет вниз, до следующей работы.",
        next_steps=["покажу разбор по метрикам, если нужно"],
        technical={"health_score": value, "band": band})
