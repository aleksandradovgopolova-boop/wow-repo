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
        if not_ready:
            causes = sorted({_ADMISSION_RU.get(c, c)
                             for r in not_ready for c in (r.get("blocked_by_admission") or [])})
            why = ("Работа объявлена, но взять её сейчас нельзя: "
                   + "; ".join(causes) + ".") if causes else \
                  "Работа объявлена, но не прошла проверку готовности."
        elif blocked:
            why = f"Это не значит, что всё сделано: {len(blocked)} задач ждут снятия блокировки."
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
        why_it_matters="Потому что " + "; ".join(nb["why"]) + ".",
        next_steps=steps,
        technical={"id": nb["id"], "owner_role": nb["owner_role"], "score": nb["score"],
                   "unblocks": nb["unblocks"],
                   "parallel_with": ", ".join(p["id"] for p in par) or "—",
                   "blocked_count": len(blocked)})


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


def from_active_work(rep: dict) -> dict:
    """Реестр активных работ -> UserMessage. Ответ на «что делаем прямо сейчас».

    Прежде `status` печатал `STATUS: активной работы нет (нет .ai/runtime/active-work.yaml)` — путь к
    внутреннему файлу вместо ответа, и одинаково на всех трёх аудиториях: настройка «с кем ты
    говоришь» на эту команду не влияла вовсе. Три независимых ревью нашли это как один дефект.
    """
    active = [a for a in (rep or {}).get("active") or []
              if (a.get("status") or "") != "done"]
    if not active:
        return message(
            status="ok", headline="Сейчас ничего не идёт",
            summary="Работа не начата.",
            next_steps=["скажи, что взять, или спроси «что дальше» — предложу с обоснованием"],
            technical={"active": 0})

    n = len(active)
    what = "; ".join(
        (a.get("title") or a.get("workitem") or a.get("id") or "работа")
        for a in active[:3])
    return message(
        status="ok", headline="Работа идёт",
        summary=f"Сейчас в работе {n} {_q(n, 'задача', 'задачи', 'задач')}.",
        why_it_matters=("Пока она не закончена, работу, которая трогает те же файлы, лучше не "
                        "начинать — иначе две сессии перепишут одно место." if n else None),
        next_steps=["спроси «что дальше», если нужно чем-то заняться параллельно"],
        technical={"работ": n, "детали": what,
                   "области": ", ".join(sorted({x for a in active
                                                for x in (a.get("affected_areas")
                                                          or a.get("areas") or [])})) or "—",
                   "ветки": ", ".join(a.get("branch") or "?" for a in active),
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


def from_specification(path, created, level_name, sections, blocking_missing, next_command) -> dict:
    """Спецификация задачи -> UserMessage. Незаполненные разделы — работа человека, и она названа."""
    n_missing = len(blocking_missing or [])
    tech = {"spec": str(path), "уровень": level_name, "разделов": len(sections or []),
            "не заполнено": ", ".join(blocking_missing or []) or "—",
            "создана": bool(created)}
    if n_missing:
        return message(
            status="needs_input",
            summary=("Заготовка описания задачи " + ("создана" if created else "уже была")
                     + f"; заполнить нужно {n_missing} "
                       f"{_q(n_missing, 'раздел', 'раздела', 'разделов')}."),
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
