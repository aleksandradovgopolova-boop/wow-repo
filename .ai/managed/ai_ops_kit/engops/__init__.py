"""engops — инженерная операционная модель: коммиты, ветки, окружения, сессии (11 модулей).

Пакетные имена (`ai_ops_kit.<пакет>.<модуль>`) — единственные точки входа: снятый
в 4.0 слой `tools/` больше не существует.
"""
# v3.38 (W3.4): подписка на события ядра (run_completed → session recommendation).
# Импортируется при первом обращении к пакету engops.
from ai_ops_kit.engops import session_events as _session_events  # noqa: F401
