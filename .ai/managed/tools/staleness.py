"""Совместимость: плоское имя staleness -> ai_ops_kit.planning.staleness.

Код живёт в пакете. Здесь алиас через sys.modules — ОДИН объект модуля, не копия
(инвариант `tests/unit/test_package_surface.py`).
"""
import sys

import _bootstrap  # noqa: F401 — кладёт корень и tools/validation в sys.path

if __name__ == "__main__":
    import runpy

    runpy.run_module("ai_ops_kit.planning.staleness", run_name="__main__", alter_sys=True)
else:
    import ai_ops_kit.planning.staleness as _target

    sys.modules[__name__] = _target
