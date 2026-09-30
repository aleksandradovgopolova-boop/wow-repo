#!/usr/bin/env python3
"""Единый формат issue кита: заголовок `<slug>: <исход>` и четыре секции тела в фиксированном порядке.

ЗАЧЕМ. Шаблон `.github/ISSUE_TEMPLATE/task.md` объявлял формат, но issue заводили три разных пути —
ночная сверка роадмапа, сверка задач-кандидатов и агенты руками, — и каждый писал по-своему: из
последних 150 issue шаблону следовали 4. Формат, который держит только проза, расходится. Здесь он
держится кодом: все генераторы issue собирают заголовок и тело через этот модуль, а тест сверяет
секции с шаблонами `task.md` и `epic.md` (одна правда на формат, а не три).

ЧТО ЗДЕСЬ. `SECTION_HEADINGS` — заголовки секций в порядке шаблона; `render_title` и `render_body` —
сборка; `problems` — проверка готового issue (для тестов и для тех, кто заводит issue сам).
Модуль чистый: ни сети, ни файлов.
"""
from __future__ import annotations

import re

# Порядок — часть формата: читатель находит «Проверяемо» там же, где в любой другой задаче.
SECTION_HEADINGS = ("Что.", "Зачем.", "Проверяемо.", "Границы/Источник.")

TITLE_LIMIT = 110
_SECTION_RE = re.compile(r"^\*\*([^*]+?)\*\*", re.MULTILINE)


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def render_title(slug: str, outcome: str, limit: int = TITLE_LIMIT) -> str:
    """`<slug>: <исход>`; длинный исход обрезается многоточием, slug — никогда (по нему ищут)."""
    s = _one_line(slug)
    o = _one_line(outcome)
    if not s or " " in s:
        raise ValueError(f"slug issue — одно слово без пробелов, получено: {slug!r}")
    if not o:
        raise ValueError("исход в заголовке issue пуст")
    room = max(limit - len(s) - 2, 1)
    if len(o) > room:
        o = o[: max(room - 1, 1)].rstrip() + "…"
    return f"{s}: {o}"


def render_body(what: str, why: str, verifiable: str, bounds: str,
                marker: str | None = None, extra: str | None = None) -> str:
    """Тело issue: маркер (если есть) → четыре секции в порядке шаблона → `extra` (чеклисты, подзадачи).

    Пустая секция — ошибка, а не пропуск: issue без «Проверяемо» и есть тот дрейф, который модуль
    закрывает."""
    texts = (what, why, verifiable, bounds)
    for head, text in zip(SECTION_HEADINGS, texts):
        if not str(text or "").strip():
            raise ValueError(f"секция «{head}» issue пуста")
    parts = []
    if marker:
        parts.append(marker.strip())
    parts += [f"**{head}** {str(text).strip()}" for head, text in zip(SECTION_HEADINGS, texts)]
    if extra and extra.strip():
        parts.append(extra.strip())
    return "\n\n".join(parts) + "\n"


def section_headings(text: str) -> list[str]:
    """Жирные заголовки строк (`**Что.** …`) в порядке появления — те, что похожи на секции."""
    return [h.strip() for h in _SECTION_RE.findall(text or "") if h.strip().endswith(".")]


def problems(title: str, body: str) -> list[str]:
    """Чем issue расходится с форматом; пустой список — соответствует."""
    out = []
    head, sep, rest = (title or "").partition(": ")
    if not sep or not head or " " in head or not rest.strip():
        out.append(f"заголовок не в форме «<slug>: <исход>»: {title!r}")
    found = [h for h in section_headings(body) if h in SECTION_HEADINGS]
    if found != list(SECTION_HEADINGS):
        out.append(f"секции тела {found} вместо {list(SECTION_HEADINGS)} по порядку")
    return out
