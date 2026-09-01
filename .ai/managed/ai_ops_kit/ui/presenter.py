#!/usr/bin/env python3
"""Human Communication Layer: между внутренним состоянием и человеком (v3.35.0).

Внутри кит говорит `GateResult`, `write_scope`, `tested_revision`, `preflight_block` — и обязан
продолжать: это точные имена, по которым работает код. Наружу они попадать не должны.

    внутреннее состояние -> UserMessage -> текст под выбранную аудиторию

`UserMessage` — контракт, а не совет по стилю (`registry/communication-policy.yaml`). Слой живёт в
КОДЕ, а не только в скилле, потому что соблюдение правил не может зависеть от того, вспомнила ли
конкретная модель вызвать скилл: иначе при смене runtime поведение теряется, а в половине прогонов
пользователь снова читает лог.

ЧТО СЛОЙ НЕ ДЕЛАЕТ. Не сглаживает. Простой язык — не мягкий: `degraded` остаётся `degraded` на всех
трёх уровнях, недоказанное называется недоказанным. «Готово» вместо «готово, но не проверено»
дороже любого жаргона, и presenter обязан этому мешать, а не помогать.

Использование:
  presenter.py demo [--audience product|technical|debug]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
           Path(__file__).resolve().parents[2])
POLICY = PKG / "registry" / "communication-policy.yaml"

# Аварийные значения — РОВНО на случай недоступного реестра, и в этом случае слой громко говорит,
# что читает не источник истины (см. `_contract`). Держать здесь вторую копию контракта нельзя:
# реестр перестаёт быть источником истины для собственной политики, и расхождение обнаруживается
# только глазами (тир 3 разбора перед квалификацией).
_FALLBACK_AUDIENCES = ("product", "technical", "debug")
_FALLBACK_STATUS_LABEL = {"ok": "Готово", "needs_input": "Нужно твоё решение",
                          "blocked": "Пока не могу продолжить", "done": "Готово",
                          "degraded": "Готово, но проверено не всё"}
_CONTRACT = {}          # кэш разобранного контракта: {audiences, labels, default, config_key}


def _q(n, one="вопрос", few="вопроса", many="вопросов"):
    """«1 вопрос / 4 вопроса / 6 вопросов». Русская форма — часть простого языка: сообщение,
    спотыкающееся на числительном, читается как машинный перевод, а не как речь."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


class PolicyMissing(Exception):
    """Политика коммуникации не найдена — рендерить «как-нибудь» хуже, чем сказать об этом."""


def load_policy(path=None) -> dict:
    p = Path(path or POLICY)
    if not p.is_file():
        raise PolicyMissing(f"политика коммуникации не найдена: {p}")
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise PolicyMissing(f"политика коммуникации не разбирается ({p}): {e}") from e


def _contract(policy=None) -> dict:
    """Контракт сообщений ИЗ РЕЕСТРА: аудитории, ярлыки статусов, default. -> dict.

    Реестр — источник истины, и для собственной политики коммуникации тоже. Прежде presenter держал
    копию словарей в коде: добавить статус или переименовать ярлык означало править два места, а
    расхождение обнаруживалось глазами. Кэш — по разобранному файлу; при недоступном реестре
    работаем на аварийных значениях и НЕ молчим об этом (`source`).
    """
    if policy is None and _CONTRACT:
        return _CONTRACT
    try:
        data = policy if policy is not None else load_policy()
        labels = {k: (v or {}).get("label") or _FALLBACK_STATUS_LABEL.get(k, k)
                  for k, v in (data.get("statuses") or {}).items()}
        auds = tuple((data.get("audiences") or {}).keys())
        default = data.get("default_audience") or next(
            (k for k, v in (data.get("audiences") or {}).items() if (v or {}).get("default")),
            "product")
        if not labels or not auds:
            raise PolicyMissing("в политике коммуникации нет statuses/audiences")
        out = {"labels": labels, "audiences": auds, "default": default,
               "config_key": data.get("config_key", "communication"), "source": "registry"}
    except PolicyMissing:
        out = {"labels": dict(_FALLBACK_STATUS_LABEL), "audiences": _FALLBACK_AUDIENCES,
               "default": "product", "config_key": "communication", "source": "fallback"}
    if policy is None:
        _CONTRACT.clear()
        _CONTRACT.update(out)
    return out


def statuses() -> dict:
    """Статусы контракта и их ярлыки. -> {status: label}."""
    return dict(_contract()["labels"])


def audiences() -> tuple:
    """Уровни детализации из реестра. -> кортеж имён."""
    return tuple(_contract()["audiences"])


def audience_from_config(child_root, policy=None) -> str:
    """Аудитория из `.ai-ops.yaml -> communication.audience`. По умолчанию — `product`.

    Default именно `product`: система по умолчанию разговаривает с владельцем продукта, а не с
    отладчиком. Обратный default — то, как внутренний язык и просачивался наружу.
    """
    con = _contract(policy)
    default = con["default"]
    cfg = Path(child_root) / ".ai-ops.yaml"
    if not cfg.is_file():
        return default
    try:
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return default
    aud = ((data.get(con["config_key"]) or {}).get("audience"))
    return aud if aud in con["audiences"] else default


def message(status, summary, why_it_matters=None, decision=None, next_steps=None,
            technical=None, headline=None) -> dict:
    """Собрать UserMessage.

    `technical` не выбрасывается, а откладывается: на уровне `product` он доступен по запросу, на
    `technical`/`debug` печатается. Выбросить его значило бы сделать кит непроверяемым.
    """
    _labels = statuses()
    if status not in _labels:
        raise ValueError(f"status '{status}' вне контракта {sorted(_labels)}")
    if not (summary or "").strip():
        raise ValueError("summary обязателен: сообщение без «что произошло» — это лог")
    msg = {"schema_version": 1, "kind": "user-message", "status": status,
           "summary": summary.strip()}
    if headline:
        # ЯРЛЫК НЕ ДОЛЖЕН ВРАТЬ. Общий ярлык статуса подходит не всякому случаю: `degraded` на
        # «нечего измерять» печатал «Готово, но проверено не всё» — а готово не было ничего.
        # Явный заголовок разрешён именно для таких мест; статус при этом не меняется, то есть
        # машиночитаемая честность сохраняется.
        msg["headline"] = headline.strip()
    if why_it_matters:
        msg["why_it_matters"] = why_it_matters.strip()
    if decision:
        # Вопрос без рекомендации — переложенная работа: правило recommend-not-enumerate.
        if not decision.get("question"):
            raise ValueError("decision без question")
        msg["decision"] = {"question": decision["question"],
                           "recommendation": decision.get("recommendation"),
                           "on_approve": decision.get("on_approve"),
                           "on_reject": decision.get("on_reject")}
    if next_steps:
        msg["next"] = list(next_steps) if isinstance(next_steps, (list, tuple)) else [next_steps]
    msg["technical_details"] = {"available": bool(technical), "payload": technical or {}}
    return msg


def render(msg: dict, audience="product", show_technical=False) -> str:
    """UserMessage -> текст. Один контракт, три языка; факты во всех трёх одни и те же."""
    con = _contract()
    if audience not in con["audiences"]:
        audience = con["default"]
    L = []
    label = msg.get("headline") or con["labels"].get(msg.get("status"), msg.get("status", ""))
    L.append(f"{label}. {msg.get('summary', '')}".strip())
    if msg.get("why_it_matters"):
        L.append(msg["why_it_matters"])

    d = msg.get("decision")
    if d:
        L.append("")
        L.append(f"Нужно от тебя: {d['question']}")
        if d.get("recommendation"):
            L.append(f"Рекомендую: {d['recommendation']}")
        if d.get("on_approve"):
            L.append(f"Если согласен — {d['on_approve']}.")
        if d.get("on_reject"):
            L.append(f"Если нет — {d['on_reject']}.")

    if msg.get("next"):
        L.append("")
        L.append("Дальше: " + "; ".join(msg["next"]) + ".")

    tech = (msg.get("technical_details") or {})
    if tech.get("available"):
        # `product` прячет детали за запрос, `technical`/`debug` показывают сразу. Явный
        # `show_technical=True` — ответ на «покажи технические детали» и работает на любом уровне.
        if audience in ("technical", "debug") or show_technical:
            L.append("")
            L.append("Технические детали:")
            for k, v in (tech.get("payload") or {}).items():
                L.append(f"  {k}: {v}")
        elif audience == "product":
            L.append("")
            L.append("Технические детали — по запросу («покажи технические детали»).")
    return "\n".join(L)


# ── Переводчики внутренних отчётов ────────────────────────────────────────────────────────────
# Каждая функция берёт СЫРОЙ внутренний отчёт и возвращает UserMessage. Это и есть шов: внутренние
# имена остаются внутри, наружу выходит смысл.

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

    return message(
        status="needs_input" if n_q else "ok",
        summary=summary, why_it_matters=why, next_steps=steps,
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
                   "questions": n_q})


def from_next_work(rep: dict) -> dict:
    """`next_work.compute()` -> UserMessage. «Что делать дальше» человеческими словами."""
    if rep.get("plan_is_template"):
        return message(
            status="needs_input",
            summary="В плане работ пока лежит мой пример, а не твоя работа.",
            why_it_matters="Советовать по нему я не стану: это была бы выдумка про твой продукт, "
                           "а не факт о нём.",
            next_steps=["впиши свои задачи в план и убери пометку «пример»",
                        "или скажи — соберу первый план из ответов на несколько вопросов"],
            technical={"gap": rep.get("gap")})

    if not rep.get("plan_present"):
        return message(status="blocked",
                       summary="Плана работ в проекте пока нет, поэтому предложить следующую "
                               "задачу мне нечем.",
                       why_it_matters="Без объявленных целей и работ любой мой выбор был бы "
                                      "просто первой строкой списка.",
                       next_steps=["создам черновик плана из шаблона, если скажешь"],
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
    # Одна фраза человеку, без слов `.ai-ops.yaml` и `team_coordination` — их место в technical.
    reach_h = ("вижу заявки всех машин команды (публикация включена)" if published
               else "вижу только ЭТУ машину — заявки других участников сюда не попадают")
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
                status="degraded", headline="План и заявки расходятся",
                summary=(f"Заявок на работу нет, но в плане {k} "
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
            # Основание ответа названо: это не «я всё осмотрел», а «заявок нет и в плане идущей
            # работы не объявлено» — два конкретных факта, которые человек может перепроверить.
            why_it_matters=("Сужу по двум вещам: заявок на работу нет и в плане идущей работы не "
                            "объявлено. " + reach_cap + "." if not published else
                            "Сужу по двум вещам: заявок на работу нет и в плане идущей работы не "
                            "объявлено."),
            next_steps=["скажи, что взять, или спроси «что дальше» — предложу с обоснованием"],
            technical={"active": 0, "объявлено идущими в плане": 0,
                       "реестр существует": (crosscheck or {}).get("registry_exists"),
                       "досягаемость": "команда" if published else "эта машина"})

    n = len(active)
    # РАБОЧИЕ КОПИИ НАЗЫВАЮТСЯ, А НЕ ТОЛЬКО МАШИНЫ. Параллельные ленты живут каждая в своём git
    # worktree/ветке; ответ, называющий лишь «эту машину», не говорит, ГДЕ идёт работа — а на одной
    # машине копий несколько. Ветка (она же лента) есть у каждой заявки, включая опубликованную
    # чужую (PUBLISHED_FIELDS её несёт); worktree-путь — только у локальных, поэтому в человеческий
    # текст идёт ветка, а путь остаётся в technical. Заявка без ветки просто не называется — сводка
    # тогда прежняя, без рассинхрона.
    copies = [a.get("branch") for a in active if a.get("branch")]
    copies_h = ("; ".join(copies[:3]) + ("…" if len(copies) > 3 else "")) if copies else ""
    what = "; ".join(
        (a.get("title") or a.get("workitem") or a.get("id") or "работа")
        for a in active[:3])
    why = f"{reach_cap}."
    if recon_note:
        why = recon_note + " " + why
    if not published:
        why += (" Пересечения по файлам ниже — про параллельные сессии здесь, не про команду; "
                "координация команды включается публикацией отдельно.")
    else:
        why += " Работу, трогающую те же файлы, лучше не начинать — иначе две сессии перепишут одно место."
    if stale:
        # Половина расхождения видна и при живой работе: заявки есть на одно, а план объявляет
        # идущим ещё что-то. Молчать об этом значит показывать половину картины.
        why += (f" В плане объявлено идущими ещё {len(stale)} "
                f"{_q(len(stale), 'работа', 'работы', 'работ')} без заявки: {stale_names} — "
                "либо брошено, либо закончено и не отмечено.")
    return message(
        status="degraded" if stale else "ok",
        headline="Работа идёт" if not stale else "Работа идёт, но план расходится с заявками",
        summary=(f"Сейчас в работе {n} {_q(n, 'задача', 'задачи', 'задач')}"
                 + (f" — в рабочих копиях {copies_h}." if copies_h else ".")),
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

    return message(status="ok", headline="Вот что я сделаю", summary=summary,
                   next_steps=steps or ["запускай, когда готов"], technical=tech)


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
    missing_cmds = sorted({k for s in stacks for k, v in (s.get("commands") or {}).items() if not v})
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
                           "я провести не смогу и не буду делать вид, что провела.",
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
                       added=None, add_error=None) -> dict:
    """Спецификация задачи -> UserMessage. Незаполненные разделы — работа человека, и она названа.

    F-029: `added` — разделы, ДОПИСАННЫЕ в уже существующий файл под поднявшийся уровень. Без него
    сообщение звучало «заготовка уже была; заполнить нужно 9 разделов», а в файле лежало 6 разделов
    прошлого уровня — заполнять было нечего. `add_error` — честная причина, если дописать не вышло
    (битый spec.yaml не переписываем: описанное человеком дороже незакрытого гейта)."""
    n_missing = len(blocking_missing or [])
    n_added = len(added or [])
    tech = {"spec": str(path), "уровень": level_name, "разделов": len(sections or []),
            "не заполнено": ", ".join(blocking_missing or []) or "—",
            "создана": bool(created), "дописано": ", ".join(added or []) or "—"}
    if add_error:
        tech["дописать не удалось"] = str(add_error)
    if created:
        _origin = "создана"
    elif n_added:
        _origin = (f"уже была, дописано {n_added} "
                   f"{_q(n_added, 'раздел', 'раздела', 'разделов')} под {level_name}")
    else:
        _origin = "уже была"
    if n_missing:
        return message(
            status="needs_input",
            summary=("Заготовка описания задачи " + _origin
                     + f"; заполнить нужно {n_missing} "
                       f"{_q(n_missing, 'раздел', 'раздела', 'разделов')}."
                     + (f" Дописать разделы не удалось: {add_error}." if add_error else "")),
            why_it_matters="Заполнять их за тебя я не буду: это как раз то, что из кода не "
                           "выводится, — зачем задача и как поймём, что получилось.",
            next_steps=[f"заполни разделы в {path}", f"потом запускай: {next_command}"],
            technical=tech)
    return message(status="ok", headline="Описание задачи готово",
                   summary="Всё, что нужно было описать, описано.",
                   next_steps=[f"запускай: {next_command}"], technical=tech)


def from_discovery_draft(path, created) -> dict:
    """Черновик discovery -> UserMessage. Пустой черновик — не результат, а приглашение."""
    return message(
        status="needs_input",
        summary=("Черновик для обсуждения идеи " + ("создан" if created else "уже был") + "."),
        why_it_matters="Он пустой намеренно: чью боль решаем и как поймём, что помогло, "
                       "я за тебя не придумаю.",
        next_steps=[f"заполни разделы в {path}",
                    "потом попроси построить описание задачи — дальше я работаю сама"],
        technical={"draft": str(path), "создан": bool(created)})


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
            summary="У этой работы нет мест, которые я обязана отдавать на независимую проверку.",
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
            summary="Работа сделана, но независимую проверку я не провела.",
            why_it_matters="Своё же изменение я судить не имею права, а живого проверяющего "
                           "здесь не было. Это не «всё хорошо» — это «не проверено».",
            next_steps=["подключи проверяющего — тогда у вердикта появится основание"],
            technical=tech)

    if ready:
        return message(
            status="ok", headline="Проверено",
            summary=f"Независимая проверка прошла: изменений в {changed} "
                    f"{_q(changed, 'файле', 'файлах', 'файлах')}, замечаний нет.",
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
    """`engineering_advisor.advise()` -> UserMessage. Совет — не исполнение, и это должно быть видно."""
    recs = list(result.get("recommendations") or [])
    urgent = [r for r in recs if int(r.get("priority") or 3) == 1]
    tech = {"repository": result.get("repository"), "task_type": result.get("task_type") or "—",
            "рекомендаций": len(recs), "сводка": result.get("summary")}
    tech.update({f"[{r.get('category')}] {i + 1}": f"{r.get('advice')} (источник: {r.get('source')})"
                 for i, r in enumerate(recs)})
    if not recs:
        return message(status="ok", headline="Замечаний по инженерной части нет",
                       summary="Смотрела окружения, поставку и процесс — советовать нечего.",
                       next_steps=["спроси «что дальше» — предложу работу"], technical=tech)
    n = len(recs)
    if urgent:
        return message(
            status="degraded", headline="Есть то, что стоит починить сначала",
            summary=f"Нашла {n} {_q(n, 'совет', 'совета', 'советов')} по инженерной части, "
                    f"из них {len(urgent)} "
                    f"{_q(len(urgent), 'срочный', 'срочных', 'срочных')}.",
            why_it_matters="Срочное здесь значит: пока это так, остальная работа будет идти "
                           "медленнее или её результат будет труднее проверить. "
                           + urgent[0].get("advice", ""),
            next_steps=["возьмусь за срочное, если скажешь", "остальное покажу списком"],
            technical=tech)
    return message(
        status="ok", headline="Совет по инженерной части",
        summary=f"Нашла {n} {_q(n, 'место', 'места', 'мест')}, где можно сделать лучше; "
                f"срочного нет.",
        why_it_matters=recs[0].get("advice"),
        next_steps=["покажу список целиком, если нужно"], technical=tech)


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
    head = f"Сессия читает {human_ctx} на каждом запросе ({measured}); прочитала всего {spend}"

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
        next_steps=[f"первой пойдёт «{items[0]['title']}»"] if items else None,
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
    steps = ["ответь одной строкой: " + hint_command] if hint_command else []
    return message(
        status="needs_input", headline="Пары слов о задаче не хватает",
        summary="Прежде чем запускать, мне нужно понять: " + ", ".join(human) + ".",
        why_it_matters="Из кода это не выводится, а без этого прогон остановится на проверке — "
                       "уже потратив время. Спрашиваю секундой, а не часом.",
        next_steps=steps or ["скажи размер и риск задачи"],
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

    def _t(n):
        return "н/д" if n is None else (f"{n / 1000:.0f} тысяч" if n >= 1000 else str(n))

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
        summary=f"На то, чтобы разобраться и описать, ушло в этой сессии {_t(spent)} токенов, а кода я "
                f"ещё не тронул. Твой потолок на это — {_t(limit)}.",
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


def from_kit_feedback_status(rep: dict) -> dict:
    """Судьба наблюдений этой дочки -> UserMessage. Ответ обязан быть виден, иначе канал умрёт."""
    total = rep.get("total") or 0
    waiting, decided = rep.get("waiting") or [], rep.get("decided") or []
    if not total:
        return message(
            status="ok", headline="Замечаний ко мне пока нет",
            summary="Ты ещё ничего мне не говорила о моей работе в этом проекте.",
            next_steps=['сказать так: ./ai-ops feedback "что было не так"'])
    if rep.get("errors"):
        # ДВЕ ПРАВКИ ПО ПРОБЕ КАНАЛА НА ЖИВОЙ ДОЧКЕ (18.08.2026), и обе про честность ответа.
        # ПЕРВАЯ — АРИФМЕТИКА: `total` считает только ЧИТАЕМЫЕ записи, поэтому «записано 1, но 1 из
        # них не разбираются» на одной хорошей и одной битой читалось как «единственная запись
        # сломана». Числа теперь названы раздельно, а сумма — сумма.
        # ВТОРАЯ — ОДНА БИТАЯ ЗАПИСЬ ГЛУШИЛА ВЕСЬ ОТВЕТ: судьба читаемых замечаний не показывалась
        # вовсе. Это ровно тот отказ, от которого канал и умирает: человек перестаёт видеть ответ и
        # перестаёт писать. Деградация остаётся деградацией — но она про непрочитанные записи, а не
        # про все.
        bad = len(rep["errors"])
        fates = [f"«{d.get('statement') or d['id']}» — {d.get('state_name') or d['state']}"
                 for d in decided[:2]]
        return message(
            status="degraded", headline="Часть замечаний я не читаю",
            summary=f"Записей {total + bad}: читаю {total}, не могу прочитать {bad}.",
            why_it_matters="Про непрочитанные я не могу обещать, что они до меня дойдут. "
                           "Остальные видны ниже — их судьба не потерялась.",
            next_steps=fates or None,
            technical={"ошибки": rep["errors"], "по состояниям": rep.get("by_state")})
    if waiting and not decided:
        return message(
            status="ok", headline="Сказанное ждёт ответа",
            summary=f"Замечаний {total}, ответа пока нет ни на одно.",
            why_it_matters="Ответ приходит, когда я разбираю их у себя: каждое станет работой или "
                           "будет отклонено с причиной. Молча они не исчезнут.",
            next_steps=[w["statement"] for w in waiting[:2]],
            technical=rep.get("by_state"))
    return message(
        status="ok", headline="Вот что стало с твоими замечаниями",
        summary=f"Замечаний {total}: с ответом {len(decided)}, ждут ответа {len(waiting)}.",
        next_steps=[f"«{d.get('statement') or d['id']}» — "
                    f"{d.get('state_name') or d['state']}"
                    + (f": {d['reason']}" if d.get("reason") else "")
                    for d in decided[:2]],
        technical=rep.get("by_state"))


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


def demo(audience="product"):
    """Один и тот же внутренний отчёт на трёх языках — то, что проверяют evals."""
    msg = message(
        status="needs_input",
        summary="Пока не начинаю разработку.",
        why_it_matters="Задача затрагивает защищённую часть проекта, поэтому мне нужно твоё "
                       "подтверждение. Остальное к работе готово.",
        decision={"question": "разрешить изменение модуля авторизации",
                  "recommendation": "разрешить только чтение агрегированных данных",
                  "on_approve": "начну реализацию и принесу результат на проверку",
                  "on_reject": "предложу вариант, который этот модуль не трогает"},
        next_steps=["после подтверждения — реализация и независимая проверка"],
        technical={"gate": "specification", "protected_paths": "auth/*",
                   "context": "128k / 150k", "approval_record": "missing"})
    return render(msg, audience=audience)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="presenter.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo")
    d.add_argument("--audience", choices=list(audiences()), default="product")
    d.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])
    if ns.cmd == "demo":
        if ns.json:
            print(json.dumps(load_policy().get("message_contract"), ensure_ascii=False, indent=2))
        else:
            print(demo(ns.audience))
    return 0


if __name__ == "__main__":
    sys.exit(main())
