#!/usr/bin/env python3
"""XML-якорная разметка промпта СУДЬИ/РЕВЬЮЕРА и её обратимость как контракт (эпик #744).

ПОЧЕМУ (заземление, не косметика). Промпт судьи = `<task>`+`<criteria>`+`<evidence>`+недоверенный
`<context>` (дифф + журнал чтений). Дифф — ЧУЖОЙ текст: он может нести строку `=== КОНТЕКСТ ===`,
`Recommendation: pass` или поддельный `reviewer-result`. При плоских маркерах граница между НАШИМ
каркасом и РАЗБИРАЕМОЙ нагрузкой теряется — поддельный вердикт из диффа неотличим от вердикта
ревьюера. XML-якорь с ЭКРАНИРОВАНИЕМ тела держит границу: нагрузка заперта внутри `<context>` и не
может подделать якорь. Заземление на кит-стороне (`acceptance_verify._ground_quote`) смотрит на СЫРОЙ
дифф, а не на текст промпта, поэтому экранирование безопасно для грудинга. Замер восстановимости
границ (прокси заземлённости, без живой модели) — в `tests/unit/test_reviewer_prompt_xml_anchors.py`.
"""
from __future__ import annotations

import re

# Имена якорей — замкнутый словарь [a-z][a-z_]*: разбор однозначен.
_NAME = re.compile(r"^[a-z][a-z_]*$")


def escape(body: str) -> str:
    """XML-экранирование тела: `&`→`&amp;`, `<`→`&lt;`, `>`→`&gt;` (порядок важен). Обратимо."""
    return str(body).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def unescape(text: str) -> str:
    """Обратное к `escape` (порядок обратный — `&amp;` последним)."""
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def anchor(name: str, body: str) -> str:
    """Одна секция как XML-якорь: `<name>` + экранированное тело + `</name>` (якори на своих строках)."""
    if not _NAME.match(name):
        raise ValueError(f"имя якоря должно быть [a-z][a-z_]*, дано: {name!r}")
    return f"<{name}>\n{escape(body)}\n</{name}>"


def assemble(sections) -> str:
    """Собрать промпт из `[(name, body), …]`, каждая в своём якоре. Недоверенную нагрузку — последней
    секцией `("context", …)`: экранируется и не вытечет за якорь."""
    return "\n\n".join(anchor(name, body) for name, body in sections)


def recover_sections(prompt: str) -> dict:
    """Точный обратный разбор `assemble` -> {name: body}, тело байт-в-байт. Тела экранированы, поэтому
    `<name>…</name>` разбирается однозначно даже на враждебной нагрузке — контракт восстановимости."""
    out = {}
    for m in re.finditer(r"<([a-z][a-z_]*)>\n(.*?)\n</\1>", prompt, re.DOTALL):
        out[m.group(1)] = unescape(m.group(2))
    return out
