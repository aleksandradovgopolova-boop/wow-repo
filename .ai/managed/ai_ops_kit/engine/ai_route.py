#!/usr/bin/env python3
"""Совместимость: маршрутизатор переехал в ai_ops_kit/shared/ai_route.py (K5, 2026-09-10).

Этот модуль — ТОНКИЙ ре-экспорт по старому адресу. Он существует не для рантайма кита (тот
импортирует `ai_ops_kit.shared.ai_route` напрямую — workitem, run_plan, валидаторы), а для
кода, который зовёт маршрутизатор по прежнему пути `ai_ops_kit.engine.ai_route` через границу
версий: валидаторы прежних тегов при кросс-версионных операциях (например, апдейт дочки, где
smoke-валидатор прошлого тега импортирует `from ai_ops_kit.engine import ai_route` и обращается к
`ai_route.route`, `ai_route.SCENARIOS`, `ai_route.REQUIRED_KEYS`).

ЭТО НЕ ВОССТАНАВЛИВАЕТ ВЗАИМНУЮ ПАРУ engine<->lifecycle: ре-экспорт импортирует shared ВНИЗ по
слоям (foundation), а lifecycle его не трогает — mutual_pairs остаётся 0. Когда все живые дочки
пройдут через тег с новым адресом, шим можно снять отдельным срезом.
"""
from __future__ import annotations

from ai_ops_kit.shared.ai_route import *  # noqa: F403 — ре-экспорт публичной поверхности по старому пути
