"""Совместимость: плоское имя work_areas -> ai_ops_kit.engine.work_areas.

Код живёт в пакете. Здесь алиас через sys.modules — ОДИН объект модуля, не копия: иначе состояние
разъедется между двумя путями импорта. Инвариант держит `tests/unit/test_package_surface.py`
(«ровно одна сторона — алиас»), и именно он поймал отсутствие этого файла в CI, когда модуль зон
появился без плоского имени.

`_bootstrap` импортируется ПЕРВЫМ и кладёт корень репозитория в sys.path — без него
`import ai_ops_kit...` падает при прямом запуске и в child-репозитории, где PYTHONPATH не задан.
У модуля зон нет точки входа CLI (это чистая функция вывода зон), поэтому runpy-ветка не нужна.
"""
import sys

import _bootstrap  # noqa: F401 — кладёт корень и tools/validation в sys.path

import ai_ops_kit.engine.work_areas as _target

sys.modules[__name__] = _target
