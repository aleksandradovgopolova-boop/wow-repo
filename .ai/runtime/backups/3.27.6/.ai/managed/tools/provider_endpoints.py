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


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    expect("kimi endpoint -> moonshot.ai + KIMI_API_KEY",
           endpoint_for("kimi")["base_url"].startswith("https://api.moonshot.ai") and endpoint_for("kimi")["key_env"] == "KIMI_API_KEY")
    expect("qwen endpoint -> dashscope-intl", "dashscope-intl" in endpoint_for("qwen")["base_url"])
    expect("deepseek endpoint -> api.deepseek.com", "api.deepseek.com" in endpoint_for("deepseek")["base_url"])
    expect("неизвестный провайдер -> None", endpoint_for("ghost") is None)

    # deepseek fallback на OPENAI_COMPATIBLE_API_KEY, если DEEPSEEK_API_KEY нет
    _saved_d = os.environ.pop("DEEPSEEK_API_KEY", None)
    _saved_o = os.environ.get("OPENAI_COMPATIBLE_API_KEY")
    os.environ["OPENAI_COMPATIBLE_API_KEY"] = "x"
    expect("deepseek без DEEPSEEK_API_KEY -> fallback OPENAI_COMPATIBLE_API_KEY",
           endpoint_for("deepseek")["key_env"] == "OPENAI_COMPATIBLE_API_KEY" and key_available("deepseek") is True)
    os.environ["DEEPSEEK_API_KEY"] = "y"
    expect("deepseek с DEEPSEEK_API_KEY -> primary", endpoint_for("deepseek")["key_env"] == "DEEPSEEK_API_KEY")
    os.environ.pop("DEEPSEEK_API_KEY", None)
    if _saved_d is not None:
        os.environ["DEEPSEEK_API_KEY"] = _saved_d
    if _saved_o is None:
        os.environ.pop("OPENAI_COMPATIBLE_API_KEY", None)
    else:
        os.environ["OPENAI_COMPATIBLE_API_KEY"] = _saved_o

    _saved_k = os.environ.pop("KIMI_API_KEY", None)
    expect("kimi без ключа -> key_available False", key_available("kimi") is False)
    if _saved_k is not None:
        os.environ["KIMI_API_KEY"] = _saved_k

    print("provider_endpoints selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
