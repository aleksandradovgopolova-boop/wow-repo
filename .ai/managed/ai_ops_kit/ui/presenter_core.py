#!/usr/bin/env python3
"""Коммуникационное ядро presenter: контракт `UserMessage`, рендер и загрузка политики.

Фундамент слоя человеческого языка (см. фасад `presenter.py`): статусы/аудитории из реестра,
глоссарий продуктового языка, сборка `UserMessage` (`message`) и его рендер под аудиторию
(`render`). Проекции внутренних отчётов (`from_*`) живут в модулях-соседях `presenter_work.py`
и `presenter_graph.py`, которые импортируют отсюда `message`/`_q`. Это ядро НЕ импортирует ни
фасад, ни соседей, ни `presenter_formatters` — направление зависимостей однонаправленно.
"""
from __future__ import annotations

import re
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
# Режимы работы — ЛИНЗА поверх аудитории (источник истины — `operating_modes` в реестре). Аварийная
# копия РОВНО на случай недоступного реестра: каждый режим отображается в одну из трёх существующих
# аудиторий, новых уровней здесь нет.
_FALLBACK_MODES = {"founder": "product", "product": "product", "design": "product",
                   "delivery": "product", "engineering": "technical"}
_CONTRACT = {}          # кэш разобранного контракта: {audiences, labels, default, config_key, modes}

# Глоссарий продуктового языка — аварийная копия РОВНО на случай недоступного реестра (источник
# истины — `registry/communication-policy.yaml -> product_glossary`). Держать здесь основную копию
# нельзя по той же причине, что и для статусов: реестр перестал бы быть источником истины для
# собственной политики. Заголовок работы (`title`) приходит из плана дочки и может быть написан
# внутренним языком; для аудитории `product` человеческие строки прогоняются через этот словарь.
_FALLBACK_GLOSSARY = {
    "merge queue": "очередь на слияние", "merge base": "итог слияния",
    "merge-base": "итог слияния", "pull request": "запрос на слияние",
    "auto-merge": "автослияние", "automerge": "автослияние", "write_scope": "область правок",
    "tested_revision": "проверенная версия", "GateResult": "результат проверки",
    "preflight_block": "предстартовая проверка", "ApprovalRecord": "запись согласования",
    "coverage": "покрытие тестами", "footprint": "объём поставки", "polling": "опрос состояния",
    "gate": "проверка", "SHA": "версия",  # «PR» намеренно НЕ здесь — продуктовый язык поставки.
}
_GLOSSARY = {}          # кэш: {"map": {term: plain}, "rx": compiled, "source": ...}


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
        # Режимы — линза: mode -> аудитория по умолчанию. Берём только те, что отображаются в
        # СУЩЕСТВУЮЩУЮ аудиторию; режим с чужим уровнем игнорируется (линза не вводит новый уровень).
        modes = {str(k): (v or {}).get("audience")
                 for k, v in (data.get("operating_modes") or {}).items()}
        modes = {k: a for k, a in modes.items() if a in auds}
        out = {"labels": labels, "audiences": auds, "default": default, "modes": modes,
               "config_key": data.get("config_key", "communication"), "source": "registry"}
    except PolicyMissing:
        out = {"labels": dict(_FALLBACK_STATUS_LABEL), "audiences": _FALLBACK_AUDIENCES,
               "default": "product", "modes": dict(_FALLBACK_MODES),
               "config_key": "communication", "source": "fallback"}
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
    """Аудитория из `.ai-ops.yaml -> communication.audience`, с режимом-линзой. По умолчанию — `product`.

    Default именно `product`: система по умолчанию разговаривает с владельцем продукта, а не с
    отладчиком. Обратный default — то, как внутренний язык и просачивался наружу.

    Режим работы (`communication.mode`: founder/product/design/delivery/engineering) — тонкая ЛИНЗА
    поверх этих трёх аудиторий, не новый уровень (источник истины — `operating_modes` в реестре).
    Разрешение: явно заданная `audience` ВСЕГДА побеждает; если аудитория не задана, но задан
    известный режим — берётся его аудитория по умолчанию; неизвестный/отсутствующий режим -> тот же
    `default`, что и у неизвестной аудитории. Так режим МЕНЯЕТ вывод, а не остаётся декларацией.
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
    comm = data.get(con["config_key"]) or {}
    aud = comm.get("audience")
    if aud in con["audiences"]:      # явная аудитория сильнее режима
        return aud
    if aud is None:                  # аудитория не задана — режим может выбрать уровень
        mode_aud = (con.get("modes") or {}).get(comm.get("mode"))
        if mode_aud in con["audiences"]:
            return mode_aud
    return default


def _glossary(policy=None) -> dict:
    """Глоссарий продуктового языка ИЗ РЕЕСТРА: {term: plain}, скомпилированный в один regex.

    Термины сопоставляются по границе слова, без учёта регистра; длинные фразы раньше коротких
    («merge queue» до «merge»), иначе короткий термин съедал бы часть длинного. При недоступном
    реестре работаем на аварийной копии и НЕ молчим об этом (`source`) — как и `_contract`.
    """
    if policy is None and _GLOSSARY:
        return _GLOSSARY
    src = "registry"
    try:
        data = policy if policy is not None else load_policy()
        gmap = data.get("product_glossary")
        if not isinstance(gmap, dict) or not gmap:
            raise PolicyMissing("в политике коммуникации нет product_glossary")
    except PolicyMissing:
        gmap, src = dict(_FALLBACK_GLOSSARY), "fallback"
    # Длинные ключи первыми, чтобы фраза побеждала входящее в неё слово.
    terms = sorted((str(k) for k in gmap), key=len, reverse=True)
    rx = re.compile("|".join(r"\b" + re.escape(t) + r"\b" for t in terms),
                    re.IGNORECASE) if terms else None
    lower = {str(k).lower(): str(v) for k, v in gmap.items()}
    out = {"map": lower, "rx": rx, "source": src}
    if policy is None:
        _GLOSSARY.clear()
        _GLOSSARY.update(out)
    return out


def _humanize(text: str, policy=None) -> str:
    """Заменить внутреннюю лексику плоскими эквивалентами глоссария. Перевод меняет ЯЗЫК, а не факты.

    Применяется к человеческим строкам ТОЛЬКО для аудитории `product`: заголовок работы приходит из
    плана дочки дословно, и без этого фильтра «coverage/footprint/merge queue/auto-merge/polling»
    просачивались бы в `summary`/`next`. Для `technical`/`debug` строки не трогаются — имена гейтов и
    метрик им положены по `show`-списку политики.
    """
    if not text:
        return text
    g = _glossary(policy)
    rx = g.get("rx")
    if not rx:
        return text
    gmap = g["map"]
    return rx.sub(lambda m: gmap.get(m.group(0).lower(), m.group(0)), text)


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
    # МАСКИРОВКА ЛЕКСИКИ НА ПУТИ К ЧЕЛОВЕКУ. Заголовок работы (`title`) приходит из плана дочки и
    # вставляется в `summary`/`next`/`why` дословно — если он написан внутренним языком, продакт
    # получает жаргон. `hide`-список политики объявлял категории, но НИЧТО их не ловило (аудит
    # 04.09). Фильтр применяется ТОЛЬКО для `product`: `technical`/`debug` имена гейтов и метрик
    # видят по своему `show`-списку. Технические детали (payload ниже) не трогаются — они и так
    # открыты по запросу, и там жаргон уместен.
    h = _humanize if audience == "product" else (lambda s, policy=None: s)
    L = []
    label = msg.get("headline") or con["labels"].get(msg.get("status"), msg.get("status", ""))
    L.append(h(f"{label}. {msg.get('summary', '')}".strip()))
    if msg.get("why_it_matters"):
        L.append(h(msg["why_it_matters"]))

    d = msg.get("decision")
    if d:
        L.append("")
        L.append(h(f"Нужно от тебя: {d['question']}"))
        if d.get("recommendation"):
            L.append(h(f"Рекомендую: {d['recommendation']}"))
        if d.get("on_approve"):
            L.append(h(f"Если согласен — {d['on_approve']}."))
        if d.get("on_reject"):
            L.append(h(f"Если нет — {d['on_reject']}."))

    if msg.get("next"):
        L.append("")
        L.append(h("Дальше: " + "; ".join(msg["next"]) + "."))

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
