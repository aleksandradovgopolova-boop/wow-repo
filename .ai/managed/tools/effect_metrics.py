"""Совместимость: плоское имя effect_metrics -> ai_ops_kit.intelligence.effect_metrics.

Код переехал в пакет. Здесь алиас через sys.modules — ОДИН объект модуля, не копия:
иначе состояние разъедется между двумя путями импорта.

_bootstrap импортируется ПЕРВЫМ и кладёт корень репозитория в sys.path — без этого
`import ai_ops_kit...` падает при запуске файла напрямую (`python3 tools/effect_metrics.py`)
и в child-репозитории, где PYTHONPATH не задан. Локально это скрывала
editable-установка.

Запуск скриптом идёт через runpy, а не через подмену sys.modules: подмена ломала
`if __name__ == "__main__"` в цели, и команда молча ничего не делала с кодом 0 (v3.31.1).
"""
import sys

import _bootstrap  # noqa: F401 — кладёт корень и tools/validation в sys.path

if __name__ == "__main__":
    # Запуск скриптом. Подменить собой __main__ здесь НЕЛЬЗЯ: тогда `if __name__ == "__main__"`
    # внутри цели не сработает никогда (у неё __name__ — имя модуля в пакете), скрипт не сделает
    # ничего и вернёт 0. Исполняем цель как __main__ — ровно то, чем был плоский файл до переезда.
    import runpy

    runpy.run_module("ai_ops_kit.intelligence.effect_metrics", run_name="__main__", alter_sys=True)
else:
    import ai_ops_kit.intelligence.effect_metrics as _target

    sys.modules[__name__] = _target
