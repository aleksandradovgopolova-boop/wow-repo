#!/usr/bin/env python3
"""Provider error types and their recognisers — фундамент для orchestrator_providers.

Вынесено из orchestrator_providers.py (чистый разрез монолита): типы отказов провайдера и
распознаватели их сигнатур. Ни от каких других провайдер-модулей НЕ зависит — на него опираются
`provider_calls` (граница `claude -p`) и фасад. Поведение байт-в-байт прежнее.
"""
from __future__ import annotations

import re as _re


class ProviderLimitError(RuntimeError):
    """Лимит модели/сессии исчерпан — это НЕ транзиентный сбой и НЕ дефект продукта.

    ПОВОД, поле 20.08.2026 (obs 99aa67ef, прогон ⌘K в ai-ops-cockpit). При исчерпании лимита сессии
    claude-cli («You've hit your session limit», HTTP 429) кит делал пять повторов с backoff и затем
    ронял `RuntimeError` полным трейсбеком. Человек видел стену питона вместо того, что делать: лимит
    сам собой за 30 секунд backoff не вернётся — ждать надо до сброса или сменить провайдера.

    Отдельный тип, чтобы граница CLI показала это ФРАЗОЙ и кодом возврата, а не трейсбеком, и чтобы
    не спутать с транзиентным 529 (F-011): тот повторять НУЖНО, этот — бессмысленно.
    """

    def __init__(self, provider, detail, reset_hint=None):
        self.provider = provider
        self.detail = detail
        self.reset_hint = reset_hint
        super().__init__(self.human_message())

    def human_message(self):
        when = f" вернусь после {self.reset_hint}," if self.reset_hint else ""
        return (f"Лимит модели ({self.provider}) исчерпан:{when} "
                f"либо укажи другого провайдера: --provider anthropic|openai|qwen. "
                f"(подробно: {self.detail})")


class ProviderEnvUnavailableError(RuntimeError):
    """Исполнитель структурно не запускается в ЭТОЙ среде — это не сетевой сбой и не дефект кита.

    ПОВОД, заявка #160 (поле 18.08.2026, ИИ-Среда). Когда `run --execute` запущен ИЗНУТРИ уже
    открытой сессии Claude Code, вложенный `claude -p` возвращается мгновенно синтетическим
    конвертом-ошибкой, НЕ дойдя до модели: `duration_api_ms:0`, ноль токенов, `terminal_reason:
    api_error`. Причина — сама среда (сессия внутри сессии), поэтому повтор её не лечит: прежде кит
    делал пять бессмысленных попыток и ронял трейсбек «claude -p не удался после 5 попыток», а человек
    не видел ни причины, ни выхода.

    Отдельный тип, чтобы граница CLI назвала последствие и ОБА выхода фразой, а не трейсбеком, и чтобы
    не спутать ни с транзиентным 529 (тот повторять НУЖНО, F-011), ни с исчерпанием лимита
    (ProviderLimitError — там ждать сброса или менять провайдера, здесь — сменить среду запуска).
    """

    def __init__(self, provider, detail=""):
        self.provider = provider
        self.detail = detail
        super().__init__(self.human_message())

    def human_message(self):
        base = (f"Исполнитель `{self.provider}` в этой среде запуститься не смог, поэтому работу "
                "выполнить не удалось. Так бывает, когда запуск идёт изнутри уже открытой сессии "
                "Claude — вложенный вызов обрывается сразу, не дойдя до модели, и повторять его "
                "бесполезно.\n"
                "  что сделать (одно из двух): запусти `run --execute` из обычного терминала, вне "
                "сессии Claude; либо укажи другого исполнителя с ключом — "
                "`--provider anthropic|openai|qwen`.")
        return base + (f"\n  (подробно: {self.detail})" if self.detail else "")


def _session_limit(text):
    """Отличить исчерпание ЛИМИТА СЕССИИ/КВОТЫ от транзиентного 5xx/529.

    Лимит сессии часто несёт в тексте и «429», поэтому проверяется ПЕРВЫМ — иначе он попал бы в
    `_transient` и был бы бессмысленно повторён пять раз (окно backoff ≤30с, а сброс лимита —
    минуты/часы).
    """
    t = (text or "").lower()
    return any(s in t for s in ("session limit", "hit your", "usage limit", "quota",
                                "daily limit", "monthly limit", "resets at", "try again at",
                                "limit reached", "usage_limit"))


def _reset_hint(text):
    """Время сброса из текста, если названо (для человека). -> str|None."""
    m = _re.search(r"(?:resets? at|try again at|after)\s+([0-9:apm\s\.]{3,20})", text or "",
                   _re.IGNORECASE)
    return m.group(1).strip().rstrip(".") if m else None


def _env_unavailable_envelope(d):
    """Распознать СТРУКТУРНЫЙ отказ среды по конверту claude-cli (заявка #160).

    Сигнатура из поля: `is_error:true` + `terminal_reason:"api_error"` + `duration_api_ms:0` +
    нулевые input/output-токены. Вложенный `claude -p` внутри активной сессии Claude Code
    обрывается ДО обращения к модели — ни времени в API, ни токенов, — и это детерминированно:
    повтор его не лечит, потому что причина в самой среде, а не в транзиентном сбое сети.

    Проверяется отдельно от `_transient`/`_session_limit`: тот отказ повторять нужно, лимит — ждать
    сброса, а этот — сменить среду запуска. Требуем ВСЕ признаки, чтобы не спутать со случайным
    `is_error` (напр. 529 Overloaded несёт ненулевой duration_api_ms и попадает в транзиентную ветку).
    """
    if not isinstance(d, dict) or not d.get("is_error"):
        return False
    if str(d.get("terminal_reason") or "").strip().lower() != "api_error":
        return False

    def _is_zero(value):
        try:
            return int(value) == 0
        except (TypeError, ValueError):
            return False

    return (_is_zero(d.get("duration_api_ms"))
            and _is_zero(d.get("input_tokens"))
            and _is_zero(d.get("output_tokens")))
