"""Совместимость: плоское имя session_launcher -> ai_ops_kit.engops.session_launcher.

Модуль НОВЫЙ и сразу живёт в пакете — здесь ничего не «переезжало». Плоское имя появилось по двум
причинам репозитория: инвариант `test_package_surface` требует, чтобы у модуля пакета была ровно
одна сторона-алиас (иначе две копии кода расходятся), а точки входа (`ai-ops`, doctor) до сих
пор импортируют плоские имена. Алиас через sys.modules — ОДИН объект модуля, не копия.

_bootstrap импортируется ПЕРВЫМ и кладёт корень репозитория в sys.path — без этого
`import ai_ops_kit...` падает при запуске файла напрямую (`python3 tools/session_launcher.py`)
и в child-репозитории, где PYTHONPATH не задан. Локально это скрывала editable-установка.

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

    runpy.run_module("ai_ops_kit.engops.session_launcher", run_name="__main__", alter_sys=True)
else:
    import ai_ops_kit.engops.session_launcher as _target

    sys.modules[__name__] = _target
