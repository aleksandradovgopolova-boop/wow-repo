#!/usr/bin/env python3
"""_bootstrap.py — sys.path setup для модулей ai_ops_kit/validation/.

Тёзка `tools/_bootstrap.py` — НЕ копипаста по недосмотру, а следствие правила интерпретатора:
при запуске скрипта `sys.path[0]` — это каталог САМОГО скрипта. Поэтому `import _bootstrap` из
валидатора ищет `_bootstrap` рядом с ним, и никакой файл в `tools/` его не заменит.
Каталогов, из которых запускают скрипты, два — значит и bootstrap'ов два. Оба ставят одни и те же
пути, состояния не держат, и порядок их импорта ни на что не влияет.

v3.34: каталог переехал в `ai_ops_kit/validation/`, и путь здесь переехал вместе с ним. Корень
по-прежнему ищется маркером VERSION — глубина файла изменилась, а поиск нет, ровно ради таких
переносов он и сделан таким.

Прежде десять валидаторов делали голый `import _bootstrap`, который резолвился только там, где
`tools/` уже лежал в путях: в CI — из-за PYTHONPATH в обоих workflow, у разработчика — из-за
editable-установки. В child-репозитории и в чистом клоне те же валидаторы падали с
ModuleNotFoundError, а `ai-ops doctor` — вместе с ними.

Корень ищется по маркеру VERSION, а не по числу parents: глубина файла — не контракт (v3.30).
"""
from __future__ import annotations
import sys
from pathlib import Path

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
# корень тоже в путях: из него импортируется пакетная поверхность ai_ops_kit
for _p in (str(PKG / "tools"), str(PKG / "ai_ops_kit" / "validation"), str(PKG)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
