#!/usr/bin/env python3
"""Мелкие общие помощники графа знаний: нормализация id и текста.

Отдельный модуль, потому что ими пользуются ОБА файла графа — и сборка (`knowledge_graph`), и
обход (`knowledge_graph_query`). Держать их в фасаде значило бы завести цикл импорта между ним и
сателлитом; дублировать — завести две правды о том, что такое id узла.

Слой: `intelligence`. Чистые функции, диска не касаются.
"""
from __future__ import annotations


def _slug(value) -> str:
    """Строка -> id узла графа (`^[a-z0-9][a-z0-9-]*$`)."""
    out: list[str] = []
    for ch in str(value or "").lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-") or "node"


def _text(v) -> str:
    return str(v or "").strip()
