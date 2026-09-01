"""Совместимость: плоское имя acceptance_verify -> ai_ops_kit.engine.acceptance_verify.

Код живёт в пакете. Здесь алиас через sys.modules — ОДИН объект модуля, не копия: иначе состояние
разъедется между двумя путями импорта (инвариант `tests/unit/test_package_surface.py`).

_bootstrap импортируется ПЕРВЫМ и кладёт корень репозитория в sys.path — без этого
`import ai_ops_kit...` падает при запуске файла напрямую и в child-репозитории, где PYTHONPATH
не задан.
"""
import sys

import _bootstrap  # noqa: F401 — кладёт корень и tools/validation в sys.path

if __name__ == "__main__":
    # Запуск скриптом идёт через runpy: подмена sys.modules ломала бы `if __name__ == "__main__"`
    # в цели, и команда молча ничего не делала бы с кодом 0 (v3.31.1).
    import runpy

    runpy.run_module("ai_ops_kit.engine.acceptance_verify", run_name="__main__", alter_sys=True)
else:
    import ai_ops_kit.engine.acceptance_verify as _target

    sys.modules[__name__] = _target
