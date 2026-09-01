"""Совместимость: плоское имя product_bootstrap -> ai_ops_kit.planning.product_bootstrap.

Код живёт в пакете. Здесь алиас через sys.modules — ОДИН объект модуля, не копия: иначе
состояние разъедется между двумя путями импорта.

Имя `product_bootstrap`, а не `bootstrap`: рядом лежит `_bootstrap.py`, который кладёт корень
репозитория в sys.path, и два «бутстрапа» в одном каталоге читались бы как одно и то же.
"""
import sys

import _bootstrap  # noqa: F401 — кладёт корень и tools в sys.path

if __name__ == "__main__":
    import runpy

    runpy.run_module("ai_ops_kit.planning.product_bootstrap", run_name="__main__", alter_sys=True)
else:
    import ai_ops_kit.planning.product_bootstrap as _target

    sys.modules[__name__] = _target
