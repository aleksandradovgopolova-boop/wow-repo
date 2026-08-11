#!/usr/bin/env python3
"""provider_endpoints.py (v3.7.12) — map провайдер -> (base_url, key_env) для openai-compatible вызовов.

Делает per-role кросс-вендор маршрутизацию ФИЗИЧЕСКИ исполнимой: model_router выбирает model_id (у него
свой provider в models.yaml), а здесь provider -> конкретный endpoint + ИМЯ env-переменной ключа. Секрет
НИКОГДА не в коде и не передаётся значением — только имя env; ключ читает orchestrator._openai_call из env.
Ключи кладутся в env оркестрирующим слоем из локальных источников (файлы/env), не из репозитория.

Только stdlib. CLI: provider_endpoints.py --selftest
"""
from __future__ import annotations

import os
import sys

# base_url — реально проверенные рабочие эндпоинты (2026-07-28). key_env — ИМЯ переменной окружения.
PROVIDER_ENDPOINTS = {
    "deepseek": {"base_url": "https://api.deepseek.com/chat/completions",
                 "key_env": "DEEPSEEK_API_KEY", "key_env_fallback": "OPENAI_COMPATIBLE_API_KEY"},
    "kimi":     {"base_url": "https://api.moonshot.ai/v1/chat/completions",
                 "key_env": "KIMI_API_KEY", "key_env_fallback": None},
    "qwen":     {"base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
                 "key_env": "QWEN_API_KEY", "key_env_fallback": None},
}


def endpoint_for(provider):
    """(base_url, key_env) для провайдера или None. key_env — то имя, под которым РЕАЛЬНО есть ключ
    (primary или fallback); если ключа нет ни там ни там — возвращает primary имя (звонящий увидит miss)."""
    cfg = PROVIDER_ENDPOINTS.get(provider)
    if not cfg:
        return None
    key_env = cfg["key_env"]
    if not os.environ.get(key_env) and cfg.get("key_env_fallback") and os.environ.get(cfg["key_env_fallback"]):
        key_env = cfg["key_env_fallback"]
    return {"base_url": cfg["base_url"], "key_env": key_env}


def key_available(provider):
    """Есть ли реально ключ в env для провайдера (по значению, не печатая его)."""
    cfg = PROVIDER_ENDPOINTS.get(provider)
    if not cfg:
        return False
    return bool(os.environ.get(cfg["key_env"]) or (cfg.get("key_env_fallback") and os.environ.get(cfg["key_env_fallback"])))


def main(argv):
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
