"""engops — инженерная операционная модель: коммиты, ветки, окружения, сессии (11 модулей).

Реальный код — здесь; плоское имя (`tools/<module>.py`) осталось алиасом через
`sys.modules` ради существующих импортов и входных точек doctor.
"""
# v3.38 (W3.4): подписка на события ядра (run_completed → session recommendation).
# Импортируется при первом обращении к пакету engops.
from ai_ops_kit.engops import session_events as _session_events  # noqa: F401
