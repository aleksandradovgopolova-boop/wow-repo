#!/usr/bin/env python3
"""Переводчики внутренних отчётов о ходе и решениях работы (вынесено из `presenter_formatters.py`).

Модуль-сосед `presenter_formatters.py`: слой человеческого языка накопил в нём две группы
переводчиков, и файл перевалил за потолок размера. Меньшая, читающая-состояние группа (осмотр
репозитория, сверка контуров, здоровье продукта) осталась рядом с переводчиками повседневных
команд; более крупная — про ход и решения работы (экономика сессии, первый час, bootstrap,
недостающие intake-сигналы, короткий путь, трата на разбор, запись обратной связи, doctor) —
выделена сюда без изменения поведения.

`message` и `_q` импортируются из `presenter.py` (их дом), а `presenter_formatters.py`
реэкспортирует эти функции обратно (`from ai_ops_kit.ui.presenter_report_formatters import …`),
так что и `presenter.from_bootstrap(...)`, и `presenter_formatters.from_doctor(...)` продолжают
работать. Цикла нет: `presenter.py` определяет `message`/`_q` в начале файла и на загрузке соседей
обратно не импортирует.
"""
from __future__ import annotations

from ai_ops_kit.ui.presenter import _q, message


def from_subsession_decision(decision: dict) -> dict:
    """SubsessionDecision -> UserMessage: может ли кит взять работу в отдельную сессию САМ.

    Читатель — владелец, а не инженер, поэтому здесь нет ни «подсессии», ни имён полей конфига в
    тексте: есть «беру сам» / «нужно твоё слово» и одно понятное действие. Внутренние имена
    (`session_economy.max_autonomous_spend_usd`, коды отказов) остаются В ДЕТАЛЯХ — по ним
    отлаживают, но наружу они не идут.

    Почему отказ не сводится к одной фразе «нельзя»: у семи отказов разное ЛЕЧЕНИЕ. «Потолок не
    объявлен» лечится одной строкой согласия, «потолок достигнут» — решением потратить ещё,
    «не могу доказать расход» — вообще не деньгами. Свести их в одно значило бы сказать человеку
    «нельзя» там, где на самом деле «скажи да».
    """
    n = (decision or {}).get("numbers") or {}
    action = (decision or {}).get("action")
    code = (decision or {}).get("refusal")
    ceiling, spent = n.get("ceiling_usd"), n.get("spent_usd")
    tech = {"решение": action, "код отказа": code or "—",
            "потолок $": ceiling if ceiling is not None else "не объявлен",
            "потрачено самостоятельно $": spent if spent is not None else "—",
            "подсессий использовано": n.get("subsessions_used", "—"),
            "состояние контекста": n.get("context_state"),
            "сессия": n.get("session_id") or "не опознана",
            "причина": (decision or {}).get("reason") or "—"}

    if action == "spawn_subsession":
        left = None if ceiling is None or spent is None else round(float(ceiling) - float(spent), 4)
        return message(
            status="ok", headline="Эту работу возьму отдельно и сам",
            summary="Начну её с чистого листа, чтобы не платить за перечитывание нашей истории."
                    + (f" В пределах разрешённого остаётся ${left}." if left is not None else ""),
            why_it_matters="Чем длиннее переписка, тем дороже каждый следующий запрос, а пользы от "
                           "старой части уже нет.",
            next_steps=["ничего не нужно — расскажу, что получилось"], technical=tech)

    if action == "continue_here":
        return message(
            status="ok", headline="Отдельная сессия пока не нужна",
            summary=(decision or {}).get("reason") or "Продолжаю здесь.",
            next_steps=["продолжаю"], technical=tech)

    # Отказы. Формулировка зависит от кода: разное лечение — разные слова.
    if code == "no_ceiling":
        # Спрашивать «сколько можно потратить» и не предлагать числа значило бы требовать решения,
        # для которого у человека нет данных: цену вызова видел только кит. Поэтому вопрос идёт
        # ВМЕСТЕ с посчитанной суммой и её основанием — владельцу остаётся согласиться.
        sug = n.get("suggested_usd")
        why = n.get("suggestion_reason")
        tech["предложено $"] = sug if sug is not None else "нет замера"
        tech["основание предложения"] = n.get("suggestion_basis") or "—"
        if sug:
            return message(
                status="blocked", headline=f"Могу дальше сам — нужно твоё «да» на ${sug}",
                summary=f"Я посчитал, сколько прошу: ${sug}. {why}",
                why_it_matters="Работать без названной границы значит тратить без границы. Пока "
                               "суммы нет, я не трачу ничего — даже когда вижу, что стоило бы.",
                decision={"question": f"разрешить мне тратить самостоятельно до ${sug}?",
                          "recommendation": f"да, ${sug} — это посчитано по реальной цене работы, "
                                            "не выбрано на глаз",
                          "on_approve": "буду брать подходящую работу отдельно и остановлюсь на "
                                        "этой сумме сам",
                          "on_reject": "останусь здесь и буду только советовать"},
                next_steps=["скажи «да» — запишу сумму в настройки проекта",
                            "или назови свою, если эта кажется большой"], technical=tech)
        return message(
            status="blocked", headline="Сам продолжить не могу — нечем обосновать сумму",
            summary="Я мог бы вести эту работу отдельно, но сумму назвать не могу: "
                    + (why or "у меня нет замеров стоимости в этом проекте")
                    + " Придумывать число я не буду.",
            why_it_matters="Названная от себя сумма выглядела бы расчётом, не будучи им. Лучше "
                           "честно попросить решение, чем подсунуть догадку.",
            decision={"question": "сколько мне можно потратить самостоятельно?",
                      "recommendation": "назначь небольшую сумму на пробу — после первых работ я "
                                        "посчитаю точнее сам",
                      "on_approve": "буду брать подходящую работу отдельно, не выходя за неё",
                      "on_reject": "останусь здесь и буду только советовать"},
            next_steps=["назови сумму — я запишу её в настройки проекта"], technical=tech)
    if code == "over_ceiling":
        return message(
            status="blocked", headline="Разрешённая сумма израсходована",
            summary=f"Самостоятельно потрачено ${spent} из ${ceiling}. Дальше — только с твоим словом.",
            why_it_matters="Это и есть та граница, о которой договаривались: дальше я не иду сам.",
            decision={"question": "продолжать самостоятельно?",
                      "recommendation": "решай по результату — что уже получено, видно",
                      "on_approve": "подними сумму, и я продолжу",
                      "on_reject": "останусь здесь"},
            next_steps=["подними разрешённую сумму или продолжим вместе"], technical=tech)
    if code == "spend_unprovable":
        return message(
            status="degraded", headline="Не могу доказать, сколько уже потратил",
            summary="Среди сделанных запросов есть такие, чья стоимость неизвестна, поэтому мой "
                    "подсчёт неполон.",
            why_it_matters="Сказать «я в пределах суммы» по неполному счёту значило бы пообещать "
                           "больше, чем я знаю. Поэтому не трачу.",
            next_steps=["продолжим здесь — я на виду"], technical=tech)
    if code == "session_unidentified":
        return message(
            status="degraded", headline="Не понимаю, к какому разговору отнести расход",
            summary="Пока я не опознаю текущий разговор, я не могу связать с ним трату.",
            why_it_matters="Иначе я потратил бы «в никуда»: проверить, остался ли я в пределах "
                           "суммы, было бы нечем.",
            next_steps=["продолжим здесь"], technical=tech)
    if code == "unsafe_boundary":
        return message(
            status="degraded", headline="Сейчас не время переключаться",
            summary="Работа в середине шага, который нельзя обрывать.",
            why_it_matters="Прерваться здесь дороже, чем дойти до безопасной точки.",
            next_steps=["дойду до безопасной точки и вернусь к этому решению"], technical=tech)
    return message(
        status="degraded", headline="Сам продолжить не могу",
        summary=(decision or {}).get("reason") or "Нет условий, чтобы взять работу отдельно.",
        next_steps=["продолжим здесь"], technical=tech)


def from_session_economy(snapshot: dict, rec: dict) -> dict:
    """Снимок сессии + SessionRecommendation -> UserMessage. Говорится ДО траты, а не после.

    ДВА ДЕФЕКТА ОДНОГО МЕСТА (найдено полем 2026-08-13). Первый: расход назывался только в ритуале
    ЗАВЕРШЕНИЯ WorkItem — то есть решение «здесь новую сессию не начинаем» человек мог принять лишь
    после того, как уже потратил. Второй: страж перед старом печатал что-либо только при исходах
    `new_session`/`compact`, а поскольку контекст всегда был `unknown` (транскрипт не читался
    никогда), этих исходов не наступало и страж молчал всегда. Молчание читалось как «всё в порядке».

    Поэтому здесь расход называется ВСЕГДА, и «не измерено» — отдельный, видимый ответ, а не тишина.
    """
    ctx = snapshot.get("context_current")
    status = snapshot.get("context_status")
    outcome = (rec or {}).get("outcome")
    spend = (rec or {}).get("session_spend") or "н/д"
    turns = snapshot.get("turns")
    # Внутренняя причина остаётся В ДЕТАЛЯХ: в ней живут имена вроде `WorkItem`, которым наружу
    # хода нет, а отлаживать по ней надо.
    tech = {"контекст": ctx, "статус измерения": status,
            "источник": snapshot.get("context_source") or "—",
            "ходов": turns, "источник ходов": snapshot.get("turns_source") or "—",
            "расход сессии": spend, "состояние расхода": (rec or {}).get("spend_state") or "—",
            "исход": outcome, "причина": (rec or {}).get("reason") or "—",
            # Путь — В ДЕТАЛЯХ (наружу путям хода нет), но САМ ФАКТ идёт в текст ниже: уйти из
            # сессии, не записав её состояние, — это потеря труда, а не деталь реализации.
            "handoff сессии": (rec or {}).get("handoff") or "—",
            "последняя компакция": snapshot.get("last_compaction_at") or "не обнаружена"}

    if status == "unavailable":
        why = snapshot.get("session_unavailable_reason")
        tech["почему не измерено"] = why or "—"
        return message(
            status="degraded", headline="Расход этой сессии я не вижу",
            summary="Сколько сессия уже прочитала — не измерено."
                    + (f" Причина: {why}" if why else ""),
            why_it_matters="Это не «мало»: без числа я не могу вовремя сказать, что пора начинать "
                           "новую сессию, и работа будет идти дороже молча.",
            next_steps=["покажи `/context` и передай число как `--context N`"],
            technical=tech)

    human_ctx = f"{ctx / 1000:.0f}k" if ctx else "н/д"
    measured = "измерено" if status == "measured" else "оценка"
    # #708: session_spend несёт внутренний тег состояния — «… из … [over_budget]». В детали (tech) он
    # уходит целиком, а в текст человеку — без тега: [over_budget] — это жаргон состояния, а не число.
    spend_human = spend.rsplit(" [", 1)[0] if spend.endswith("]") and " [" in spend else spend
    head = f"Сессия читает {human_ctx} на каждом запросе ({measured}); прочитала всего {spend_human}"

    if outcome in ("new_session", "compact", "clear"):
        advice = {"new_session": "начать чистую сессию",
                  "compact": "сжать историю на этой безопасной границе",
                  "clear": "очистить историю — следующая работа не связана с прошлой"}[outcome]
        return message(
            status="degraded", headline="Прежде чем тратить — стоит сменить сессию",
            summary=f"{head}.",
            why_it_matters="Каждый следующий запрос заново оплачивает перечитывание этой истории. "
                           "Дальше будет только дороже, а пользы от старой переписки уже нет.",
            decision={"question": "начинать работу здесь или в чистой сессии?",
                      "recommendation": advice,
                      "on_approve": "выполни команду ниже и повтори задачу",
                      "on_reject": "продолжу здесь — решение твоё, я не блокирую"},
            # Состояние handoff — ПЕРВЫЙ шаг, а не приписка: если состояние сессии не записано,
            # уходить из неё нечем, и это важнее самой команды выхода.
            next_steps=[(rec or {}).get("handoff") or "состояние сессии не проверено",
                        (rec or {}).get("command") or "продолжаю здесь"],
            technical=tech)
    # `attention` — не «всё хорошо»: сказать «история дешёвая» при растущем счёте значило бы
    # успокаивать там, где кит как раз обязан предупредить.
    growing = "attention" in ((rec or {}).get("context_state"), (rec or {}).get("spend_state"))
    return message(
        status="ok",
        headline="Счёт растёт, но сессию менять пока рано" if growing else "Сессию менять не нужно",
        summary=f"{head} — работаю здесь.",
        why_it_matters=("Расход подходит к порогу: следующую независимую задачу лучше начать "
                        "в чистой сессии, а эту — довести до конца здесь." if growing else
                        "Пока история дешёвая, собранное знание выгоднее переиспользовать, "
                        "чем начинать с нуля."),
        technical=tech)


def from_first_hour(res: dict) -> dict:
    """`first_hour.run()` -> UserMessage. Первый час ОДНИМ нарративом: понял репозиторий → что
    знаю/не знаю → (если ответы есть) направление и план → следующая работа. Склейка готовых
    функций, не новый движок; честные состояния (есть ответы / чего не хватает)."""
    stage = res.get("stage")
    cls = res.get("classification")
    n_block = len(res.get("blocking_questions") or [])
    conflicts = res.get("conflicts") or []

    if stage == "blocked_understanding":
        return message(
            status="degraded", headline="Не смог осмотреть репозиторий",
            summary="Прочитать содержимое репозитория не получилось — первый час я не начинаю.",
            why_it_matters="Любой мой вывод о проекте без этого был бы выдумкой.",
            next_steps=["проверь доступ к дереву репозитория и повтори: ./ai-ops model --flow"],
            technical={"classification": cls})

    if stage == "needs_answers":
        parts = []
        if conflicts:
            parts.append("источники истины противоречат — актуальное сам определить не могу")
        if n_block:
            parts.append(f"есть {n_block} {_q(n_block, 'блокирующий вопрос', 'блокирующих вопроса', 'блокирующих вопросов')} о продукте, которых из кода не узнать")
        return message(
            status="needs_input",
            summary="Разобрался с проектом. Чтобы собрать первое направление и план, нужны твои "
                    "ответы: " + "; ".join(parts) + ".",
            why_it_matters="Пока это открыто, направление за готовое я не выдам — недоказанное "
                           "называю недоказанным.",
            next_steps=['ответь: ./ai-ops model --answer <вопрос> "<ответ>"',
                        "потом запусти снова: ./ai-ops model --flow --apply — соберу направление, "
                        "план и предложу первую работу"],
            technical={"classification": cls, "blocking_questions": n_block,
                       "conflicts": ", ".join(c.get("category", "") for c in conflicts) or "—"})

    # ready — ответов хватает.
    boot = res.get("bootstrap") or {}
    wi = boot.get("work_items")
    n_work = len(wi) if isinstance(wi, list) else (wi or 0)
    if not res.get("bootstrap_applied"):
        will = [w for w in (boot.get("will_write") or []) if w.get("will_write")]
        return message(
            status="ok",
            summary="Понял проект и готов собрать первое направление и план из фактов репозитория.",
            why_it_matters="Ответов хватает — выдумывать ничего не нужно.",
            next_steps=["запиши направление и план: ./ai-ops model --flow --apply",
                        "после этого сразу назову, какую работу взять первой"],
            technical={"classification": cls,
                       "будет создано": ", ".join(w.get("path", "") for w in will) or "—",
                       "работ в плане": n_work})

    nxt = res.get("next") or {}
    first = nxt.get("next_best") if isinstance(nxt, dict) else None
    first_label = (first.get("title") or first.get("id")) if isinstance(first, dict) else None
    steps = []
    if first_label:
        steps.append(f"возьми первой: {first_label}")
    else:
        steps.append("спроси «что дальше» — предложу работу по собранному плану")
    steps.append("детали по любой работе — по запросу")
    return message(
        status="ok", headline="Первый час пройден",
        summary=f"Понял проект, собрал направление и план ({n_work} "
                f"{_q(n_work, 'работа', 'работы', 'работ')})"
                + (f"; следующей имеет смысл взять «{first_label}»" if first_label else "") + ".",
        why_it_matters="Дальше можно работать по плану, а не по догадкам.",
        next_steps=steps,
        technical={"classification": cls, "работ": n_work,
                   "next": (first.get("id") if isinstance(first, dict) else None) or "—"})


def from_bootstrap(rep: dict, applied=False) -> dict:
    """`bootstrap.plan()` / `bootstrap.apply()` -> UserMessage. Онбординг заканчивается РАБОТОЙ.

    Запись артефактов в чужой репозиторий владелец обязан увидеть ДО того, как она произошла, —
    поэтому сухой прогон спрашивает решение, а не сообщает о сделанном.
    """
    if rep.get("error"):
        return message(status="blocked", headline="Создавать не стал",
                       summary=str(rep["error"]),
                       why_it_matters="Перезаписать файл, который я не смог прочитать, значит "
                                      "уничтожить работу, которую в нём кто-то делал.",
                       next_steps=["починим файл и повторим"],
                       technical={"error": rep["error"]})

    if applied:
        wrote = rep.get("written") or []
        skipped = rep.get("skipped") or []
        n_work = rep.get("work_items") or 0
        n_q = rep.get("blocking_questions") or 0
        if not wrote:
            return message(
                status="ok", headline="Всё уже было на месте",
                summary="Создавать было нечего: направление и план в проекте уже есть.",
                next_steps=["спроси «что дальше» — предложу работу по существующему плану"],
                technical={"пропущено": ", ".join(s["path"] for s in skipped) or "—"})
        # СКОЛЬКО ИЗ НИХ МОЖНО НАЧАТЬ БЕЗ МЕНЯ — РАЗНЫЕ ОТВЕТЫ. Если каждая работа начинается с
        # ответа владельца, обещать «спроси что дальше — назову первую работу» нельзя: там будет
        # «ждёт решения человека», и это ровно тот разрыв обещания, из-за которого правится тир 4.
        doable = rep.get("ready_without_human")
        waiting = rep.get("awaiting_human") or 0
        tech = {"создано": ", ".join(w["path"] for w in wrote),
                "пропущено": ", ".join(s["path"] for s in skipped) or "—",
                "работ": n_work, "ждут ответа": waiting, "вопросов": n_q}
        if doable == 0 and n_work:
            return message(
                status="needs_input", headline="План есть, и он начинается с тебя",
                summary=f"Собрал направление и план: {n_work} "
                        f"{_q(n_work, 'работа', 'работы', 'работ')}; все они начинаются с твоего "
                        f"ответа.",
                why_it_matters="Это не бюрократия: без ответов я не знаю ни для кого продукт, ни "
                               "что считать результатом, — и выдумывать это я не буду.",
                next_steps=["впиши ответы в .ai/project/onboarding-answers.yaml",
                            "потом спроси «что дальше» — работа станет готовой"],
                technical=tech)
        return message(
            status="ok", headline="Готово: теперь есть с чем работать",
            summary=f"Собрал направление и план: {n_work} "
                    f"{_q(n_work, 'работа', 'работы', 'работ')} по тому, чего проекту "
                    f"не хватает.",
            why_it_matters=(f"Из них {waiting} ждут твоего ответа — из кода это не выводится, и я "
                            f"это не выдумывал; остальное могу начать сам." if waiting else
                            "Всё это выведено из твоего репозитория, а не придумано за тебя."),
            next_steps=["спроси «что дальше» — назову первую работу и обоснование",
                        "в файлах есть пометки «нужно ваше слово» — там я не стал догадываться"],
            technical=tech)

    will = rep.get("will_write") or []
    items = rep.get("work_items") or []
    if not will:
        return message(
            status="ok", headline="Создавать нечего",
            summary="Направление и план в проекте уже есть — трогать их я не буду.",
            why_it_matters="Существующий файл — факт о продукте, и он сильнее любого моего шаблона.",
            next_steps=["спроси «что дальше» — предложу работу по существующему плану"],
            technical={a["path"]: a["why"] for a in (rep.get("actions") or [])})
    n = len(items)
    return message(
        status="needs_input", headline="Могу собрать первый план",
        summary=f"Готов создать направление и план работ: {n} "
                f"{_q(n, 'работа', 'работы', 'работ')} по тому, чего проекту не хватает.",
        why_it_matters="Всё это выведено из твоего репозитория: каждая работа — область, где у "
                       "проекта нет описания. Продуктовые цели я выдумывать не буду — там, где "
                       "нужен твой ответ, останется пометка.",
        decision={"question": "создать " + " и ".join(will),
                  "recommendation": "создать — существующие файлы я не перезаписываю",
                  "on_approve": "создам и сразу скажу, какую работу брать первой",
                  "on_reject": "ничего не пишу; понимание проекта останется, плана не будет"},
        # #702-work: сухой прогон РЕКОМЕНДУЕТ создать, но команду не называл — человек не знал, как
        # сказать «согласен» (магическое слово `--apply`) и застревал. Действие идёт первым шагом.
        next_steps=(["создать план: ./ai-ops bootstrap --apply"]
                    + ([f"первой пойдёт «{items[0]['title']}»"] if items else [])),
        technical={a["path"]: a["why"] for a in (rep.get("actions") or [])})


def from_intake_gap(missing, hint_command=None) -> dict:
    """Незаданные intake-сигналы -> UserMessage. Спрашиваем ДО прогона, а не после.

    В живой квалификации так сгорело 6 прогонов из 6, самый долгий — 36 минут: `size` требует
    блокирующий гейт, вывести его из репозитория нечем, и человек узнавал об этом из вердикта.
    Команду с готовым ответом печатаем в `next` — на уровне `product` он тоже виден, иначе
    сообщение сообщало бы о препятствии и не давало его убрать.
    """
    miss = list(missing or [])
    names = {"size": "насколько большая задача", "risk": "насколько рискованная",
             "task_type": "какого рода работа"}
    human = [names.get(m.get("signal"), m.get("signal")) for m in miss]
    # Человеку — не «ответь этим JSON», а «скопируй строку, а если задача крупнее — поменяй два
    # слова». Механизм тот же (--signals), но подан так, чтобы не знать его формат было не нужно.
    if hint_command:
        steps = ["если задача небольшая и обычная — просто скопируй эту строку в команду:  "
                 + hint_command,
                 "если она крупнее или рискованнее — поменяй в строке size (small/medium/large) "
                 "и risk (low/medium/high)"]
    else:
        steps = ["скажи размер и риск задачи — например «небольшая, не рискованная»"]
    return message(
        status="needs_input", headline="Пары слов о задаче не хватает",
        summary="Прежде чем запускать, мне нужно понять: " + ", ".join(human) + ".",
        why_it_matters="Из кода это не выводится, а без этого прогон остановится на проверке — "
                       "уже потратив время. Спрашиваю секундой, а не часом.",
        next_steps=steps,
        technical={m.get("signal"): " | ".join(m.get("allowed") or []) or "значение"
                   for m in miss})


def from_short_path(decision: dict, trace: dict = None, next_command: str = None) -> dict:
    """Решение о коротком пути -> UserMessage. Три случая, и они РАЗНЫЕ для человека.

    Короткий путь взят — говорим, что описание не переписываем и что от него осталось в следе.
    Заявлено, но минимума нет — называем ровно то, чего не хватает: это единственное, что человеку
    нужно сделать, чтобы получить короткий путь. Не заявлено — сообщения нет вовсе: кит не
    предлагает владельцу выключить собственные проверки.
    """
    names = decision.get("human_names") or {}
    keys = list(names)
    if decision.get("short_path"):
        tr = trace or {}
        declined = len(tr.get("declined") or [])
        return message(
            status="ok", headline="Работа уже описана — иду сразу делать",
            summary="Описание у тебя есть: понятно, чего добиваемся, как поймём, что готово, и где "
                    "править. Заново расспрашивать и планировать не буду.",
            why_it_matters="Я останусь в этой работе как след: что решено, по каким признакам это "
                           "видно и что я пропустил — записано, и это можно проверить позже."
                           + (f" Разделов, которые я не требую, {declined} — у каждого написано, "
                              f"почему." if declined else ""),
            next_steps=[f"делаю: {next_command}"] if next_command else ["беру работу в исполнение"],
            technical={"признаки": {names[k]: decision["minimum"][k]["detail"] for k in keys},
                       "заявлено": decision.get("declared_by"),
                       "пропущено": ", ".join(decision.get("skipped_steps") or []) or "—",
                       "решение": decision.get("decision_ref"),
                       "след": str((trace or {}).get("record") or "—")})
    if decision.get("unknown"):
        return message(
            status="degraded", headline="Похоже, описание есть, но я его не читаю",
            summary="Ты сказала, что работа описана, но проверить это я не могу: "
                    + "; ".join(decision["minimum"][k]["detail"] for k in decision["unknown"]),
            why_it_matters="Пойти коротким путём на непрочитанном описании — то же самое, что "
                           "поверить на слово. Поэтому иду обычным путём, а не притворяюсь, что "
                           "проверил.",
            next_steps=["поправить описание, чтобы оно читалось, — и короткий путь включится сам"],
            technical={"не прочитано": decision["unknown"]})
    return message(
        status="needs_input", headline="Чтобы идти сразу делать, не хватает малого",
        summary="Ты сказала, что работа описана. Чего я в описании не нашёл: "
                + ", ".join(decision.get("missing_names") or []) + ".",
        why_it_matters="Это тот самый минимум, по которому потом можно сказать «готово» и не "
                       "обмануться. Без него я не пропускаю разбор — иначе проверять результат "
                       "будет нечем.",
        next_steps=["дописать это в описание — дальше пойду коротким путём без вопросов"],
        technical={names.get(k, k): decision["minimum"][k]["detail"]
                   for k in (decision.get("missing") or [])})


def from_process_spend(check: dict, continue_command: str = None,
                       run_command: str = None) -> dict:
    """Потолок траты на описание до первой правки кода -> UserMessage (решение владельца 2026-08-17).

    Это ВОПРОС, а не отказ: владелец решила предупреждать и спрашивать, а не останавливать молча.
    Поэтому у сообщения есть и рекомендация, и то, что будет при обоих ответах.
    """
    spent, limit = check.get("spent_on_process"), check.get("ceiling")

    if check.get("state") == "unknown":
        return message(
            status="degraded", headline="Сколько уходит на разбор — не вижу",
            summary="Потолок траты на описание я применить не могу: расход этой сессии не измеряется.",
            why_it_matters="Называть это нормой было бы неправдой: я не знаю числа, а не знаю, что "
                           "оно маленькое.",
            technical={"причина": check.get("reason")})
    step = check.get("intent") or "разбор"
    return message(
        status="needs_input", headline="Разбор уже дороже, чем ты разрешила",
        summary="Разбор и описание этой задачи пошли по кругу: обсуждаем и уточняем заметно дольше, "
                "чем закладывалось, а к правке кода я ещё не притронулся.",
        why_it_matters="Ровно так уже сгорали сессии: описание уточнялось по кругу, а работа не "
                       "начиналась. Но пропускать объявленный шаг я не советую — путь "
                       "specify→plan→run затем и объявлен, чтобы результат было чем проверить.",
        decision={"question": f"довести шаг «{step}» до конца или ты считаешь описание готовым?",
                  "recommendation": f"довести {step} и идти дальше по объявленному пути; если разбор "
                                    "пошёл по кругу — назвать, чего конкретно не хватает, а не "
                                    "углубляться дальше. Шаг не пропускать.",
                  "on_approve": f"продолжаю {step}: {continue_command}" if continue_command
                                else f"продолжаю {step}",
                  "on_reject": f"описание готово — беру в исполнение: {run_command}" if run_command
                               else "описание готово — беру работу в исполнение"},
        next_steps=[c for c in (continue_command, run_command) if c],
        technical={"потрачено на описание": spent, "потолок": limit,
                   "шаги описания": ", ".join(check.get("process_steps") or []) or "—",
                   "расход сессии всего": check.get("session_total_tokens"),
                   "решение": check.get("decision_ref")})


def from_kit_feedback_recorded(path, created, errors, has_evidence, declared_class) -> dict:
    """Наблюдение о ките записано (или не записано) -> UserMessage.

    Отказ здесь — не бюрократия: «дефект» без улики попал бы в кит утверждением, за которое некому
    отвечать. Поэтому сообщение НЕ ругает человека, а называет, что именно приложить.
    """
    if errors:
        return message(
            status="needs_input", headline="Записать не могу — не на что опереться",
            summary="Ты говоришь, что я сделал что-то не так, и я хочу это запомнить. Но как "
                    "дефект это уйдёт ко мне утверждением без доказательства, а такие я сам же и "
                    "учусь не производить.",
            why_it_matters="Достаточно одной опоры: файл и строка из него — или команда и то, что "
                           "она напечатала. Если опоры нет, скажи это как трение или вопрос — их я "
                           "принимаю без доказательств.",
            next_steps=["добавить файл со строкой или команду с выводом",
                        "либо записать как трение: то же самое со словом «мешает», без улик"],
            technical={"почему не записано": "; ".join(errors)})
    if not created:
        return message(
            status="ok", headline="Это я уже записал",
            summary="Такое наблюдение у меня уже есть — второй раз не завожу, чтобы не считать одно "
                    "и то же дважды.",
            next_steps=["посмотреть судьбу сказанного: ./ai-ops feedback"],
            technical={"файл": path})
    return message(
        status="ok", headline="Записал — и это дойдёт до меня самого",
        summary="Твоё замечание сохранено в проекте вместе с тем, чем оно подтверждено."
                + ("" if has_evidence else " Улик нет, поэтому дефектом я это не называю."),
        why_it_matters="Раньше такое доезжало до меня только пересказом — то есть если человек "
                       "вспомнит. Теперь это данные: их видно, у них будет ответ.",
        next_steps=["посмотреть судьбу сказанного: ./ai-ops feedback"],
        technical={"файл": path, "класс": declared_class or "выведен из улик",
                   "улики": "есть" if has_evidence else "нет"})


# Состояние строки doctor -> насколько это плохо. Порядок важен: вердикт следует за ХУДШЕЙ строкой.
_DOCTOR_RANK = {"ok": 0, "info": 0, "unknown": 1, "gap": 1, "warn": 1, "fail": 2, "blocked": 2}


def from_doctor(lines) -> dict:
    """Строки проверки установки -> UserMessage. Вердикт следует за ХУДШЕЙ строкой.

    Прежде итог `doctor: OK` не зависел от строк с `✗` в том же выводе: человек либо перестаёт
    читать строки, либо перестаёт верить вердикту. Оба исхода делают проверку бесполезной.
    """
    rows = list(lines or [])
    worst = max((_DOCTOR_RANK.get(r.get("state"), 1) for r in rows), default=0)
    gaps = [r for r in rows if _DOCTOR_RANK.get(r.get("state"), 1) >= 1]
    blocking = [r for r in rows if _DOCTOR_RANK.get(r.get("state"), 1) >= 2]

    if worst == 0:
        return message(status="ok", headline="Всё в порядке",
                       summary="Кит на месте и работает как ожидается.",
                       next_steps=["можно ставить задачу"],
                       technical={"проверок": len(rows)})
    n = len(gaps)
    if worst >= 2:
        # БЛОКИРУЮЩЕЕ СЧИТАЕМ ОТДЕЛЬНО ОТ ЗАМЕЧАНИЙ. Общий счётчик называл «проблемами, из-за
        # которых работать нельзя» и обычные предупреждения — число врало в сторону паники, а это
        # такая же неправда, как зелёный вердикт на красном выводе.
        nb = len(blocking)
        rest = n - nb
        return message(
            status="blocked",
            summary=f"Кит проверил себя и нашёл {nb} {_q(nb, 'причину', 'причины', 'причин')}, "
                    f"из-за которых работать нельзя: "
                    + "; ".join(r.get("text", "") for r in blocking[:2])
                    + ("…" if nb > 2 else "."),
            why_it_matters="Пока это не исправлено, всё остальное, что я скажу, ничего не доказывает."
                           + (f" Кроме этого есть {rest} "
                              f"{_q(rest, 'замечание', 'замечания', 'замечаний')}." if rest else ""),
            next_steps=[r.get("text", "") for r in blocking][:2],
            technical={r.get("id", f"строка{i}"): r.get("text") for i, r in enumerate(rows)})
    return message(
        status="degraded", headline="Работать можно, но есть замечания",
        summary=f"Кит на месте; замечаний {n}.",
        why_it_matters="Работать можно; замечания стоит закрыть, чтобы проверки говорили полную правду.",
        next_steps=[r.get("text", "") for r in gaps][:2],
        technical={r.get("id", f"строка{i}"): r.get("text") for i, r in enumerate(rows)})


# ── Стиль кода, который никто не держит (#1183) ────────────────────────────────────────────────
# Прежде владелец читал «освобождено (нет инструмента в стеке): lint_passed» — внутреннее имя флага
# и причину без последствия. Последствие и есть то, ради чего сообщают: без линтера каждый агент
# пишет по-своему, и стиль расползается от правки к правке. Одна рекомендация на язык, не список:
# политика общения требует совета, а не меню.
_STYLE_LINTER = {"python": "Ruff", "node": "ESLint", "go": "golangci-lint"}
_STYLE_LANG = {"python": "Python", "node": "JavaScript/TypeScript", "go": "Go"}
CODE_STYLE_POLICIES = ("advisory", "required")


def code_style_unguarded(languages, policy="advisory", declared=None) -> str:
    """Находка «линтера нет» — одна фраза для отчёта гейта, сводки прогона и онбординга.

    `policy` — значение `.ai-ops.yaml -> standard.lint`: `advisory` (по умолчанию) сообщает,
    `required` отказывает в приёмке, незнакомое значение отказывает тоже (fail-closed) и называет,
    что именно не понято. «Не нашёл» — не «нет»: судим по репозиторию, чужую машину не видим.
    """
    langs = [str(x) for x in (languages or [])]
    offer = ", ".join(f"для {_STYLE_LANG.get(x, x)} — {_STYLE_LINTER[x]}"
                      for x in langs if x in _STYLE_LINTER)
    offer = (f"Предлагаю поставить набор правил под язык проекта: {offer}." if offer
             else "Предлагаю поставить набор правил под язык проекта.")
    found = "в репозитории я не нашёл ни линтера, ни его настроек"
    if policy == "advisory":
        return (f"Стиль кода в проекте никто не проверяет: {found}, поэтому каждый агент пишет "
                f"по-своему — имена, оформление и границы модулей расходятся от правки к правке. {offer}")
    if policy == "required":
        return (f"Работу принять не могу: в .ai-ops.yaml проверка стиля объявлена обязательной "
                f"(standard.lint: required), а {found} — проверить стиль нечем. {offer}")
    return (f"Не понимаю настройку standard.lint = «{declared}» в .ai-ops.yaml: допустимо "
            f"{' или '.join(CODE_STYLE_POLICIES)}. Пока она не исправлена, считаю проверку стиля "
            f"обязательной, а {found}, — поэтому работу не принимаю. Исправьте значение.")
