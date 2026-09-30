#!/usr/bin/env python3
"""Проекции жизненного цикла работы: сырой внутренний отчёт -> `UserMessage`.

Сателлит фасада `presenter.py`. Здесь переводчики про «что делать дальше», пост-релизную петлю,
реестр идущих работ и карточку одной работы. Импортирует только ядро `presenter_core` (`message`,
`_q`); ни фасад, ни `presenter_formatters` отсюда не импортируются — цикла нет.
"""
from __future__ import annotations

from ai_ops_kit.ui.presenter_core import _q, message

# ── Переводчики внутренних отчётов ────────────────────────────────────────────────────────────
# Каждая функция берёт СЫРОЙ внутренний отчёт и возвращает UserMessage. Это и есть шов: внутренние
# имена остаются внутри, наружу выходит смысл.


def from_next_work(rep: dict) -> dict:
    """`next_work.compute()` -> UserMessage. «Что делать дальше» человеческими словами."""
    if rep.get("plan_is_template"):
        return message(
            status="needs_input",
            summary="В плане работ пока лежит мой пример, а не твоя работа.",
            why_it_matters="Советовать по нему я не стану: это была бы выдумка про твой продукт, "
                           "а не факт о нём.",
            next_steps=["собери план из ответов: ./ai-ops model (ответь) → ./ai-ops bootstrap --apply",
                        "или впиши свои задачи в planning/plan.yaml и убери пометку «пример»"],
            technical={"gap": rep.get("gap")})

    if not rep.get("plan_present"):
        return message(status="blocked",
                       summary="Плана работ в проекте пока нет, поэтому предложить следующую "
                               "задачу мне нечем.",
                       why_it_matters="Без объявленных целей и работ любой мой выбор был бы "
                                      "просто первой строкой списка.",
                       next_steps=["собери план: ./ai-ops model (ответь на вопросы) → ./ai-ops bootstrap --apply"],
                       technical={"gap": rep.get("gap")})

    # ПЕРЕВОД НЕ ПРЯЧЕТ ДЕФЕКТ. Если сам план недостоверен (цикл зависимостей, поле исполнителя,
    # отсутствующее направление), ответ «что взять следующим» построен на неверных данных, и
    # сообщить об этом обязательно — иначе слой простого языка становится способом скрыть ошибку,
    # а не объяснить её. Проверка стоит ПЕРВОЙ: она сильнее любого другого исхода.
    plan_errors = list(rep.get("plan_errors") or [])
    rm_errors = list((rep.get("roadmap") or {}).get("errors") or [])
    if plan_errors or rm_errors:
        n = len(plan_errors) + len(rm_errors)
        return message(
            status="blocked",
            summary="Не могу предложить следующую работу: описание плана и направления содержит "
                    f"{n} {_q(n, 'ошибку', 'ошибки', 'ошибок')}.",
            why_it_matters="Пока они не исправлены, любой мой ответ про «что дальше» опирался бы на "
                           "неверные данные — я предпочитаю сказать это прямо.",
            next_steps=["перечислю, что именно неверно, и предложу исправления"],
            technical={f"ошибка {i + 1}": x for i, x in enumerate(plan_errors + rm_errors)})

    nb = rep.get("next_best")
    frozen = rep.get("frozen") or []
    held_others = rep.get("held_by_others") or []
    held_owner = rep.get("held") or []
    active = rep.get("in_progress") or []
    blocked = rep.get("blocked") or []
    if not nb:
        # Ведро `not_ready` — работа, ГОТОВАЯ по графу, но не прошедшая допуск (бюджет, capability,
        # конфликт записи). Прежде оно терялось, и продакту сообщался ложный факт «работа не
        # объявлена», хотя работа объявлена и всего лишь не допущена. Перевод менял не язык, а
        # факты — то, что политика запрещает прямо.
        not_ready = rep.get("not_ready") or []
        _ADMISSION_RU = {
            "within_budget": "не укладывается в остаток бюджета",
            "capabilities_ready": "требует возможностей, которых нет",
            "no_write_conflict": "трогает файлы, которые уже правит другая работа",
            "no_human_decision": "ждёт решения человека",
            "deps_done": "ждёт незакрытые зависимости",
        }
        if held_others:
            # ПРЯМОЙ ОТВЕТ ВМЕСТО ПЕРВОЙ СВОБОДНОЙ СТРОКИ (работа `next-offers-work-nobody-holds`).
            # Заявка потребителя #150: участник взял работу, которую уже держала другая сессия, и
            # половина труда ушла в закрытый пустой дубль. Кит обязан сказать «всё нужное держат
            # другие», а не выдать следующую строку списка.
            k = len(held_others)
            who = "; ".join(f"«{h.get('title') or h['id']}» — {h.get('owner_session') or 'кто-то'}"
                            for h in held_others[:3])
            return message(
                status="ok", headline="Свободной работы нет: нужное держат другие",
                summary=f"{k} {_q(k, 'работа', 'работы', 'работ')} уже взяты: {who}.",
                why_it_matters=("Брать взятое — это дубль: в поле так вышло два запроса на одну "
                                "ветку и половина труда ушла в пустой. "
                                + ((rep.get("holders_reach") or {}).get("note") or "")),
                next_steps=["подожду освобождения или возьму работу, которой ещё нет в плане",
                            "или скажи, что важнее — пересоберу порядок"],
                technical={"держат другие": ", ".join(h["id"] for h in held_others),
                           "держу я": ", ".join(h["id"] for h in (rep.get("held_by_me") or [])) or "—",
                           "досягаемость": (rep.get("holders_reach") or {})})
        elif held_owner:
            # МОЛЧАНИЕ ЗДЕСЬ БЫЛО ДЕФЕКТОМ, А НЕ ОСОБЕННОСТЬЮ. Пока `next_work.compute()` терял
            # `waiting_on_owner`-работы, эта ветка никогда не срабатывала — и когда ВСЁ оставшееся
            # было именно таким, разговор падал в generic «работа не объявлена», хотя работа
            # объявлена и известно, чего именно она ждёт. Владелец обязан услышать СВОЙ шаг, а не
            # догадку кита.
            k = len(held_owner)
            who = "; ".join(f"«{h.get('title') or h['id']}» — {h.get('waiting_on') or '?'}"
                            for h in held_owner[:3])
            return message(
                status="ok", headline="Свободной работы нет: дело за тобой",
                summary=f"{k} {_q(k, 'работа', 'работы', 'работ')} готовы механикой и ждут "
                        f"твоего шага: {who}.",
                why_it_matters="Это не «всё сделано»: код и проверка сделаны, но снять статус "
                               "может только названное владельцем действие — кит его не выведет.",
                next_steps=[f"{h['id']}: {h.get('waiting_on') or '?'}" for h in held_owner],
                technical={"ждут владельца": ", ".join(h["id"] for h in held_owner)})
        elif not_ready:
            causes = sorted({_ADMISSION_RU.get(c, c)
                             for r in not_ready for c in (r.get("blocked_by_admission") or [])})
            why = ("Работа объявлена, но взять её сейчас нельзя: "
                   + "; ".join(causes) + ".") if causes else \
                  "Работа объявлена, но не прошла проверку готовности."
        elif blocked:
            why = f"Это не значит, что всё сделано: {len(blocked)} задач ждут снятия блокировки."
        elif active:
            # РАБОТА ОБЪЯВЛЕНА И ИДЁТ — и это ФАКТ, который сообщение обязано назвать. Прежде эта
            # ветка сливалась со следующей, и `next` на самом ките печатал человеку «работа пока не
            # объявлена», тогда как `--json` рядом показывал её в `in_progress`. Перевод менял не
            # язык, а факты: отрицал объявленную работу — тот же класс, что потерянное ведро
            # `not_ready` строкой выше и `unknown`, выброшенный в `from_contour_consistency`.
            # Статус здесь `ok`, а не `blocked`: продолжение НЕ невозможно (blocked означает именно
            # это) — работа идёт, от человека ничего не нужно. Ярлык задаёт `headline`.
            n = len(active)
            titles = "; ".join(f"«{a.get('title') or a['id']}»" for a in active)
            return message(
                status="ok", headline="Работа идёт",
                summary=f"Свободной задачи сейчас нет: {n} "
                        f"{_q(n, 'работа', 'работы', 'работ')} уже в работе — {titles}.",
                # Формулировка НЕ повторяет ложное утверждение даже в опровержении: тест стережёт
                # именно строку «работа не объявлена», и цитата в отрицании обошла бы стража.
                why_it_matters="Это не «всё сделано»: начатая работа не закончена. Взять "
                               "параллельно тоже нечего — ни готовых, ни заблокированных задач в "
                               "плане не осталось.",
                next_steps=["продолжу то, что уже в работе",
                            "или покажу, чем закрывается каждая из этих работ"],
                technical={"in_progress": ", ".join(a["id"] for a in active),
                           "ready": "—", "blocked": "—", "not_admitted": "—"})
        else:
            why = "Это не значит, что всё сделано: работа пока не объявлена."
        return message(
            status="blocked",
            summary="Готовой к работе задачи сейчас нет.",
            why_it_matters=why,
            next_steps=["покажу, что именно мешает, если нужно"],
            technical={"blocked": ", ".join(b["id"] for b in blocked) or "—",
                       "in_progress": ", ".join(a["id"] for a in active) or "—",
                       "not_admitted": ", ".join(
                           f"{r.get('id')}: {', '.join(r.get('blocked_by_admission') or [])}"
                           for r in not_ready) or "—"})

    par = rep.get("parallel_with") or []
    steps = [f"возьмусь за «{nb['title']}»"]
    if par:
        steps.append("параллельно можно вести " +
                     " и ".join(f"«{p['title']}»" for p in par) +
                     " — эти работы не пересекаются по изменяемым файлам")
    return message(
        status="ok", headline="Что взять следующим",
        summary=f"Следующей имеет смысл взять «{nb['title']}».",
        why_it_matters="Потому что " + "; ".join(nb["why"]) + "."
                       # ЗАМОРОЗКА НАЗЫВАЕТСЯ, А НЕ ПРЯЧЕТСЯ. Работы, которых кит больше не
                       # предлагает, не исчезают из плана — и человек обязан знать, что они не
                       # предложены по ЕГО решению, а не потерялись. Молчание здесь читалось бы как
                       # «в плане их нет».
                       + (f" Ещё {len(frozen)} "
                          + _q(len(frozen), "работа", "работы", "работ")
                          + " не предлагаю: они помечены как расширение умений, а твоё решение "
                            "держит их до второго живого проекта."
                          if frozen else ""),
        next_steps=steps,
        technical={"id": nb["id"], "owner_role": nb["owner_role"], "score": nb["score"],
                   "unblocks": nb["unblocks"],
                   "parallel_with": ", ".join(p["id"] for p in par) or "—",
                   "blocked_count": len(blocked),
                   "заморожено": ", ".join(f["id"] for f in frozen) or "—",
                   "решение о заморозке": (rep.get("freeze") or {}).get("decision") or "—"})


# ── P0 №6: продуктовый OUTCOME-READOUT (сдвиг целевой метрики) человеческими словами ──────────────
# Показываем ТРЕТЬЕ, чего не хватает поверх «доставлено» и «проверено»: что изменение сделало с
# ПРОДУКТОМ — целевая метрика была X → стала Y против цели Z, честно. Числа — из отчёта; ключ метрики
# наружу не идёт (он внутреннее имя, как gate/SHA), а держится в технических деталях.
_HYPOTHESIS_RU = {
    "confirmed": "гипотеза подтвердилась",
    "refuted": "гипотеза не подтвердилась",
    "inconclusive": "гипотеза пока не разрешилась — вывод по одному замеру",
}


def _fmt_num(v):
    """Показать число без хвоста .0, сохранив единицы-строки («42%»). None/прочее — как есть."""
    if v is None or isinstance(v, bool):
        return str(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _outcome_shift_sentence(oro: dict) -> str:
    """«Целевая метрика: было X → стало Y (цель Z)» — только числа, без внутреннего имени метрики."""
    return (f"Целевая метрика: было {_fmt_num(oro.get('baseline'))} → "
            f"стало {_fmt_num(oro.get('value'))} (цель {_fmt_num(oro.get('target'))})")


def _outcome_hypothesis_phrase(oro: dict) -> str:
    return _HYPOTHESIS_RU.get(oro.get("hypothesis") or "", "")


def _outcome_guardrail_phrase(oro: dict) -> str:
    br = oro.get("guardrail_breaches") or []
    return ("просела защитная метрика: " + ", ".join(f"«{n}»" for n in br)) if br else ""


def _outcome_tech(tech: dict, oro: dict | None) -> dict:
    """Внутреннее имя метрики — в технические детали, а не в продуктовый текст."""
    if oro and oro.get("metric_key"):
        tech = {**tech, "целевая метрика (ключ)": oro["metric_key"]}
    return tech


def from_post_release_loop(result: dict) -> dict:
    """`post_release_loop.run_post_release()` -> UserMessage. Пост-релизная петля продукту.

    Ошибку/неполноту объясняем ПОСЛЕДСТВИЕМ, не местом в коде: «выпуск рекомендовать не могу:
    аналитика дочки ещё не поступила». Внутренние имена (гейт, events_verified_live, PRR) остаются
    в технических деталях; наружу — что произошло, почему важно и что дальше.
    """
    a = result.get("analytics_runtime") or {}
    prr = result.get("prr") or {}
    verdict = result.get("verdict")
    events = a.get("events_verified_live")
    with_producer = a.get("evidence_with_producer", 1)
    required = a.get("evidence_required", 4)
    not_measured = a.get("evidence_not_measured") or []

    tech = {
        "verdict": verdict,
        "readout_decision": result.get("readout_decision"),
        "gate": a.get("gate"),
        "gate_status": a.get("status"),
        "events_verified_live": events,
        "доказательств с источником": f"{with_producer} из {required}",
        "not_measured": ", ".join(not_measured) or "—",
        "PRR": (prr.get("id") or "не найден") if prr.get("found") else "не найден",
        "outcome_flip_ready": result.get("outcome_flip_ready"),
    }
    ps = result.get("product_status") or {}
    outcome_verdict = result.get("outcome_verdict") or "unknown"
    if result.get("outcome"):
        tech["outcome_declared"] = result["outcome"].get("verdict")
        tech["outcome_measured"] = result["outcome"].get("measured_verdict")
    if ps.get("label"):
        tech["продуктовый статус"] = ps["label"]

    # ПРОДУКТОВЫЙ ИТОГ СИЛЬНЕЕ ГОТОВНОСТИ ВЫПУСКА: если итог ИЗМЕРЕН (met/failed), говорим о нём
    # первым — «технически done, продуктово нет» отличает хорошо сделанное от правильного (#566).
    # P0 №6: и показываем СДВИГ целевой метрики (было X → стало Y против цели), а не «shipped/verified».
    me = (result.get("outcome") or {}).get("measured_evaluation") or {}
    oro = result.get("outcome_readout")
    if outcome_verdict == "failed":
        done_green = ps.get("delivery_verified")
        head = ("Сделано технически, но продукт цель не взял" if done_green
                else "Продуктовый результат не достигнут")
        parts = []
        if done_green:
            parts.append("Доставка зелёная — изменение внедрено, тесты и проверки пройдены.")
        if oro:
            # Если провал из-за пробитой защитной метрики, основная могла дотянуть — тогда «цель не
            # взята» неверно; провал объяснит guardrail-фраза ниже. Иначе — цель именно не взята.
            suffix = "" if oro.get("guardrail_breaches") else " — цель не взята"
            parts.append(_outcome_shift_sentence(oro) + suffix + ".")
        else:
            parts.append("Измеренный результат по релизу цель не берёт.")
        tail = ". ".join(p for p in (_outcome_guardrail_phrase(oro) if oro else "",
                                     _outcome_hypothesis_phrase(oro) if oro else "") if p)
        summ = " ".join(parts) + ((" " + tail + ".") if tail else "")
        return message(
            status="degraded", headline=head,
            summary=summ,
            why_it_matters="Это разные вещи: «мы хорошо сделали изменение» и «мы сделали правильное "
                           "изменение». " + (me.get("reason") or ""),
            next_steps=["вернуть вывод в discovery: цель не достигнута — решать, менять подход или "
                        "откатывать по правилу решения из контракта"],
            technical=_outcome_tech(tech, oro))
    if outcome_verdict == "met":
        summ = ((_outcome_shift_sentence(oro) + " — цель взята, защитные метрики удержаны.") if oro
                else "Измеренный результат по релизу берёт цель, защитные метрики удержаны.")
        why = me.get("reason") or ("Изменение оказалось правильным по измерению, а не только "
                                   "доставленным.")
        hyp = _outcome_hypothesis_phrase(oro) if oro else ""
        if hyp:
            why = why + " " + hyp[0].upper() + hyp[1:] + "."
        return message(
            status="ok", headline="Продуктовый результат достигнут",
            summary=summ,
            why_it_matters=why,
            next_steps=["зафиксировать исход достигнутым по правилу решения из контракта"],
            technical=_outcome_tech(tech, oro))

    # Общая для всех веток оговорка: гейт закрывается одним доказательством из четырёх, потому что у
    # остальных трёх пока нет источника данных. Это НАЗЫВАЕТСЯ, а не прячется за «проверено».
    gate_note = (f"Полную проверку аналитики после выпуска пока не закрыть: из четырёх её частей "
                 f"измеримую основу имеет только одна, у остальных ({_human_evidence(not_measured)}) "
                 f"ещё нет источника данных.")

    # P0 №6: контракт результата ЕСТЬ, но продуктовый итог ещё НЕ ИЗМЕРЕН -> честно «результат ещё не
    # накоплен» + НАЗВАННОЕ условие, что нужно, чтобы измерить. «Не знаю» ≠ «плохо»: чисел не выдумываем.
    # Активный негативный сигнал аналитики (события не доезжают) сильнее — его отдаём ниже как блокер.
    if oro is not None and not oro.get("measured") and events != "not_verified":
        goal_ctx = f"По цели «{oro['goal']}»: " if oro.get("goal") else ""
        if oro.get("baseline") is not None and oro.get("target") is not None:
            shift_ctx = (f"целевая метрика была {_fmt_num(oro.get('baseline'))} "
                         f"(цель {_fmt_num(oro.get('target'))}), замера после выпуска пока нет")
        else:
            shift_ctx = "целевую метрику пока не с чем сравнить"
        return message(
            status="degraded", headline="Продуктовый результат ещё не накоплен",
            summary=f"{goal_ctx}{shift_ctx}.",
            why_it_matters="«Не знаю» — это не «плохо»: пока итог не измерен, я не выдаю его за "
                           "достигнутый и не выдумываю числа. " + gate_note,
            next_steps=[f"чтобы измерить: {oro.get('needed_to_measure')}"],
            technical=_outcome_tech(tech, oro))

    if events == "unknown":
        return message(
            status="degraded", headline="Выпуск рекомендовать пока не могу",
            summary="Продукт доставлен, но проверить, доходят ли события в аналитику, ещё нечем: "
                    "выгрузка от продукта не поступила.",
            why_it_matters="Без неё я не знаю, работает ли измерение результата — а значит не могу "
                           "честно сказать, что выпуск удался. " + gate_note,
            next_steps=["дождаться выгрузки аналитики после реального выпуска и повторить проверку"],
            technical=tech)

    if events == "not_verified":
        return message(
            status="blocked", headline="Аналитика после выпуска расходится с обещанным",
            summary="Часть событий, которые продукт обещал слать, в выгрузке не встретилась.",
            why_it_matters="Пока событие не доходит, измерять результат нечем, и продолжать как ни "
                           "в чём не бывало нельзя. " + gate_note,
            next_steps=["разобраться, почему объявленные события не доезжают, и перепроверить"],
            technical=tech)

    # events == "verified": приход подтверждён, но гейт закрыт одним доказательством из четырёх.
    return message(
        status="degraded", headline="События доходят, но проверка выпуска ещё неполная",
        summary="События в аналитику приходят — это подтверждено выгрузкой.",
        why_it_matters="Это ещё не полная проверка выпуска. " + gate_note,
        next_steps=["продолжать наблюдение; полную готовность подтвердит только реальный выпуск "
                    "с доступом к аналитике"],
        technical=tech)


def _human_evidence(names) -> str:
    """Внутренние ключи доказательств -> человеческие слова (для product-аудитории)."""
    ru = {
        "no_pii_in_events": "нет ли персональных данных в событиях",
        "cohort_identification_works": "работает ли разбиение на когорты",
        "dashboard_receives_data": "доходят ли данные до дашборда",
    }
    return "; ".join(ru.get(n, n) for n in names) or "остальные проверки"


def from_active_work(rep: dict, published: bool = False, reconciled: int = 0,
                     crosscheck: dict = None) -> dict:
    """Реестр активных работ -> UserMessage. Ответ на «что делаем прямо сейчас».

    Прежде `status` печатал `STATUS: активной работы нет (нет .ai/runtime/active-work.yaml)` — путь к
    внутреннему файлу вместо ответа, и одинаково на всех трёх аудиториях: настройка «с кем ты
    говоришь» на эту команду не влияла вовсе. Три независимых ревью нашли это как один дефект.

    `published` (18.08.2026, ep-2026-08-18-claim-medium-hybrid): реестр локален для этой машины, если
    публикация не включена. Пока она выключена, ответ обязан это СКАЗАТЬ — иначе «работа идёт»/«ничего
    не идёт» читается как факт о команде, хотя это факт об одной машине. Дефолт False — самый
    безопасный: он никогда не выдаёт локальное состояние за координацию.
    """
    # #137: снятое СВЕРКОЙ с базой — не идущая работа. Прежде фильтровался только `done`, поэтому
    # влитая работа считалась идущей и человеку советовали не трогать те же файлы.
    active = [a for a in (rep or {}).get("active") or []
              if (a.get("status") or "") not in ("done", "superseded")]
    # Снятое сверкой НАЗЫВАЕТСЯ, а не исчезает молча: человек должен видеть, почему список короче.
    recon_note = (f"Снято сверкой с базой: {reconciled} "
                  f"{_q(reconciled, 'запись', 'записи', 'записей')} — изменения уже влиты."
                  if reconciled else None)
    # Одна фраза человеку, без слов `.ai-ops.yaml`/`team_coordination`/«заявка»/«публикация» — их
    # место в technical. Наружу выходит смысл досягаемости, а не механизм.
    reach_h = ("вижу, что идёт на всех машинах команды" if published
               else "вижу только ЭТУ машину — что идёт у других участников, сюда не попадает")
    reach_cap = reach_h[0].upper() + reach_h[1:]   # для начала предложения, без рассинхрона лица
    # СВЕРКА С ПЛАНОМ (замер 18.08.2026 на самом ките). Ответ строился ТОЛЬКО по реестру рантайма, и
    # при семи работах со статусом `in_progress` в плане печатал «Сейчас ничего не идёт. Работа не
    # начата.» — утвердительно, без оговорки. Отсутствие реестра — это «не знаю, что идёт», а не
    # «ничего не идёт»; для ИСПОРЧЕННОГО реестра тот же код уже отвечает `blocked`, а для
    # отсутствующего вывод не был сделан. Расхождение теперь НАЗЫВАЕТСЯ, а не сглаживается.
    stale = (crosscheck or {}).get("only_in_plan") or []
    stale_names = "; ".join((w.get("title") or w.get("id") or "работа") for w in stale[:3])
    if not active:
        if stale:
            k = len(stale)
            return message(
                status="degraded", headline="План расходится с тем, что идёт на самом деле",
                summary=(f"Начатых работ нет, но в плане {k} "
                         + _q(k, "работа объявлена идущей", "работы объявлены идущими",
                              "работ объявлено идущими") + "."),
                why_it_matters="Значит одно из двух, и оба требуют решения: работа брошена или она "
                               "давно закончена, а в плане это не отмечено. Пока расхождение живо, "
                               "плану верить нельзя — а по нему выбирают, что делать дальше.",
                next_steps=[f"сверить и закрыть или продолжить: {stale_names}"],
                technical={"active": 0, "объявлено идущими в плане": k,
                           "id": ", ".join(str(w.get("id")) for w in stale),
                           "реестр существует": (crosscheck or {}).get("registry_exists"),
                           "досягаемость": "команда" if published else "эта машина"})
        return message(
            status="ok", headline="Сейчас ничего не идёт",
            summary="Работа не начата." if not recon_note else recon_note,
            # Основание ответа названо: это не «я всё осмотрел», а «начатых работ нет и в плане
            # идущей работы не объявлено» — два конкретных факта, которые человек может перепроверить.
            why_it_matters=("Сужу по двум вещам: начатых работ нет и в плане идущей работы не "
                            "объявлено. " + reach_cap + "." if not published else
                            "Сужу по двум вещам: начатых работ нет и в плане идущей работы не "
                            "объявлено."),
            next_steps=["скажи, что взять, или спроси «что дальше» — предложу с обоснованием"],
            technical={"active": 0, "объявлено идущими в плане": 0,
                       "реестр существует": (crosscheck or {}).get("registry_exists"),
                       "досягаемость": "команда" if published else "эта машина"})

    n = len(active)
    # ЧЕЛОВЕКУ — ЧТО ИДЁТ, А НЕ ГДЕ ЛЕЖИТ. Параллельные ленты живут каждая в своём git
    # worktree/ветке, но имя ветки и путь рабочей копии — это внутренняя кухня (как SHA/gate-id):
    # человеку они не говорят ничего, а «ai-ops/…» читается как жаргон. Наружу идут человеческие
    # заголовки задач, а ветки и worktree-пути остаются в technical (ниже). Задача без заголовка
    # просто не называется — сводка тогда по счёту, без рассинхрона.
    product_titles = "; ".join(t for a in active[:3] if (t := a.get("title")))
    what = "; ".join(
        (a.get("title") or a.get("workitem") or a.get("id") or "работа")
        for a in active[:3])
    why = f"{reach_cap}."
    if recon_note:
        why = recon_note + " " + why
    if not published:
        why += (" Совпадения по файлам ниже — про параллельную работу на этой машине, не про "
                "команду; командная координация включается отдельной настройкой.")
    else:
        why += (" Задачу, которая трогает те же файлы, лучше не вести одновременно с этими — "
                "иначе они заденут одни и те же места.")
    if stale:
        # Половина расхождения видна и при живой работе: работа идёт на одно, а план объявляет
        # идущим ещё что-то. Молчать об этом значит показывать половину картины.
        why += (f" В плане объявлено идущими ещё {len(stale)} "
                f"{_q(len(stale), 'работа', 'работы', 'работ')} без начатой работы: {stale_names} — "
                "либо брошено, либо закончено и не отмечено.")
    return message(
        status="degraded" if stale else "ok",
        headline="Работа идёт" if not stale else "Работа идёт, но план расходится с реальной работой",
        summary=(f"Сейчас в работе {n} {_q(n, 'задача', 'задачи', 'задач')}"
                 + (f": {product_titles}." if product_titles else ".")),
        why_it_matters=why,
        next_steps=["спроси «что дальше», если нужно чем-то заняться параллельно"],
        technical={"работ": n, "детали": what,
                   "досягаемость": "команда" if published else "эта машина",
                   "области": ", ".join(sorted({x for a in active
                                                for x in (a.get("affected_areas")
                                                          or a.get("areas") or [])})) or "—",
                   "ветки": ", ".join(a.get("branch") or "?" for a in active),
                   "рабочие копии": ", ".join(
                       a.get("worktree") or a.get("branch") or "?" for a in active),
                   "id": ", ".join(str(a.get("id") or "?") for a in active)})


# #549: человеческие ярлыки статуса работы (внутренние коды -> статус UserMessage-контракта). Коды
# приходят из workitem/active-work/plan, а наружу выходит один из четырёх статусов контракта.
_WORK_VIEW_STATUS = {
    "blocked": "blocked", "needs_more_evidence": "blocked",
    "needs_human_decision": "needs_input",
    "done": "ok", "in-progress": "ok", "in_progress": "ok", "review": "ok",
    "draft": "ok", "todo": "ok",
}
# Человеческие имена четырёх источников проекции — для честной фразы «сведено из…», без внутренних
# слов workitem/active-work/work-graph/plan.
_WORK_VIEW_SOURCE_RU = {
    "workitem": "заявка на работу", "active_work": "реестр идущих работ",
    "work_graph": "граф пакетов работы", "plan": "план продукта",
    # #565: приёмники хребта — попадают в «сведено из…», когда реально прочитаны.
    "runs": "прогоны", "delivery": "доставка", "outcome": "исход",
}
# «Обязательные» источники идентичности работы — их отсутствие называется как пробел («не нашёл…»).
# runs/delivery/outcome сюда НЕ входят намеренно: у ранней работы их ещё нет, и это не пробел, а
# стадия жизни. Их присутствие видно из самих полей, а провенанс — из `sources`.
_WORK_VIEW_ALL_SOURCES = ("workitem", "active_work", "work_graph", "plan")


def from_work_view(view: dict) -> dict:
    """Проекция «работы» (lifecycle.work_view.project_work) -> UserMessage. Ответ на «что известно
    про эту работу», сведённое из четырёх источников в ОДНУ карточку.

    Продуктовый язык: идентификатор ведущей сессии, области записи и пути-глобы — это жаргон (как
    SHA/gate-id в explain), поэтому в человеческие строки они не идут сырьём — их место в технических
    деталях. Наружу выходит смысл: чем занята работа, что про неё сведено и чего пока нет.
    """
    view = view or {}
    title = view.get("title") or view.get("id") or "работа"
    sources = view.get("sources") or []
    if not sources:
        # Работа не нашлась ни в одном источнике — честное «не знаю такой», а не пустая карточка,
        # выданная за факт.
        return message(
            status="degraded", headline="Такой работы я не вижу",
            summary=f"Ни в одном источнике нет работы с идентификатором «{view.get('id')}».",
            why_it_matters="Собрать по ней нечего: заявки, записи об идущей работе, графа пакетов и "
                           "строки в плане с таким идентификатором нет.",
            next_steps=["проверь идентификатор работы или спроси «что дальше»"],
            technical={"id": view.get("id"), "источники": "—"})

    st = view.get("status")
    status = _WORK_VIEW_STATUS.get(st, "ok")
    # Чего НЕ хватает — называется, а не замалчивается: проекция честна о том, что сведено, а что нет.
    missing = [s for s in _WORK_VIEW_ALL_SOURCES if s not in sources]
    have_ru = ", ".join(_WORK_VIEW_SOURCE_RU[s] for s in sources)
    miss_ru = ", ".join(_WORK_VIEW_SOURCE_RU[s] for s in missing)

    # Кто ведёт — фактом, но без сырого идентификатора сессии: он в технических деталях.
    agent_line = ("Сейчас работу кто-то ведёт." if view.get("current_agent")
                  else "Активной сессии за работой сейчас нет.")
    dcount = len(view.get("decisions") or [])
    acount = len(view.get("artifacts") or [])
    # #565: хребет — прогоны, доставка (PR), исход. В продуктовую строку идёт СМЫСЛ (доставлено ли,
    # каков исход), сырьё (url PR, band здоровья) остаётся в технических деталях.
    runs = view.get("runs") or []
    delivery = view.get("delivery") or {}
    prs = delivery.get("prs") or []
    outcome = view.get("outcome") or {}
    pr_nums = ", ".join(("#" + str(p.get("number")) if p.get("number") else (p.get("url") or "?"))
                        for p in prs)
    delivered_line = (f" Доставлена в PR {pr_nums}." if prs else "")
    if outcome.get("goal_outcome_reached") is True:
        outcome_line = " Исход: цель достигнута."
    elif outcome.get("goal_outcome") is not None:
        outcome_line = " Исход: цель ещё не достигнута."
    elif outcome:
        outcome_line = " Исход пока измеряется."
    else:
        outcome_line = ""
    why = (f"Свёл всё, что известно, из {len(sources)} "
           + _q(len(sources), "источника", "источников", "источников") + f": {have_ru}.")
    if missing:
        why += (f" Не нашёл: {miss_ru} — по этим сторонам работы данных пока нет.")

    return message(
        status=status,
        headline=f"«{title}»",
        summary=(agent_line
                 + (f" Прогонов: {len(runs)}." if runs else "")
                 + delivered_line + outcome_line
                 + (f" Артефактов собрано: {acount}." if acount else "")
                 + (f" Связанных решений: {dcount}." if dcount else "")),
        why_it_matters=why,
        next_steps=["подробности по любой стороне — по запросу"],
        technical={"id": view.get("id"), "статус": st or "—",
                   "workflow": view.get("workflow") or "—",
                   "стадия": view.get("lifecycle_intent") or "—",
                   "ветка": view.get("branch") or "—",
                   "прогонов": len(runs) if runs else "—",
                   "PR": pr_nums or "—",
                   "исход": (outcome.get("readout_decision")
                             or (outcome.get("prr") or {}).get("readout_decision")
                             or ("достигнут" if outcome.get("goal_outcome_reached")
                                 else ("не достигнут" if outcome.get("goal_outcome") is not None
                                       else "—"))),
                   "ведущая сессия": view.get("current_agent") or "—",
                   "участники": ", ".join(view.get("participants") or []) or "—",
                   "области записи": ", ".join(view.get("write_scope") or []) or "—",
                   "зависит от": ", ".join(view.get("depends_on") or []) or "—",
                   "общие контракты": ", ".join(view.get("shared_contracts") or []) or "—",
                   "артефакты": ", ".join(
                       (a.get("path") or a.get("ref") or "?") for a in view.get("artifacts") or [])
                       or "—",
                   "доказательства": ", ".join(
                       e.get("path") or "?" for e in view.get("evidence") or []) or "—",
                   "решения": ", ".join(
                       str(d.get("id") or "?") for d in view.get("decisions") or []) or "—",
                   "источники": ", ".join(sources)})
