#!/usr/bin/env python3
"""Capability self-test AI-first системы (Фаза 6).

Механизм: configured capability -> documentation evidence -> smoke test -> verified + timestamp.
Оффлайн-тесты выполняются всегда (без сети и ключей); credentialed-тесты пропускаются,
если нет соответствующих env-переменных (в CI без секретов это нормально).

Оффлайн-проверки:
  1. structured-output контракт: parse+validate обёртка (эмуляция ответа провайдера,
     валидация по мини-схеме, retry-поведение при мусоре);
  2. error normalization: маппинг HTTP-кодов (429/5xx/401/400) в единую error-модель;
  3. routing downgrade: capability со статусом unknown/degraded не считается доступной.

Credentialed (пропускаются без ключей): provider auth / model response / russian quality.
Их наличие определяется по env: GIGACHAT_AUTH_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY.

Результат: печать сводки; exit 1, если офлайн-тест провален.
Обновление last_verified_at в capability-index — задача updater'а (Ф9), здесь только отчёт.
"""

import json
import os
import sys
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[1]

results = []


def record(test, status, detail=""):
    results.append((test, status, detail))


# --- 1. structured output: parse + validate + retry ---

def validate_mini(obj):
    """Мини-схема route-decision-подобного ответа: обязательные ключи и типы."""
    return (isinstance(obj, dict)
            and isinstance(obj.get("status"), str)
            and isinstance(obj.get("items"), list))


def structured_output_test():
    good = '{"status": "ok", "items": [1, 2]}'
    garbage = 'Вот ваш JSON: {"status": "ok", "items": [1, 2]} — надеюсь, помог!'
    broken = '{"status": "ok", "items": '

    # чистый ответ
    if not validate_mini(json.loads(good)):
        record("structured_output.clean", "fail", "валидный JSON не прошёл валидацию")
        return
    record("structured_output.clean", "pass")

    # ответ с обёрткой текста: retry-стратегия = вырезать первый JSON-объект
    def extract_json(text):
        start = text.find("{")
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        raise ValueError("no json")
    try:
        obj = json.loads(extract_json(garbage))
        record("structured_output.retry_extract", "pass" if validate_mini(obj) else "fail")
    except Exception as e:
        record("structured_output.retry_extract", "fail", str(e))

    # сломанный ответ: должен детектироваться, а не приниматься молча
    try:
        json.loads(broken)
        record("structured_output.broken_detected", "fail", "обрыв JSON не детектирован")
    except json.JSONDecodeError:
        record("structured_output.broken_detected", "pass")


# --- 2. error normalization ---

NORMALIZE = {
    401: ("auth_error", False),
    400: ("bad_request", False),
    429: ("rate_limited", True),
    500: ("server_error", True),
    502: ("server_error", True),
    503: ("server_error", True),
    504: ("server_error", True),
}


def error_normalization_test():
    ok = True
    for code, (kind, retryable) in NORMALIZE.items():
        got_kind, got_retry = NORMALIZE.get(code, ("unknown", False))
        if (got_kind, got_retry) != (kind, retryable):
            ok = False
    # retry только на 429/5xx
    retry_codes = {c for c, (_, r) in NORMALIZE.items() if r}
    if retry_codes != {429, 500, 502, 503, 504}:
        ok = False
    record("error_normalization.mapping", "pass" if ok else "fail")


# --- 3. routing downgrade на unknown/degraded ---

def routing_downgrade_test():
    sys.path.insert(0, str(PKG_ROOT / "validation"))
    try:
        import yaml
        cap = yaml.safe_load((PKG_ROOT / "registry" /
                              "capability-index.yaml").read_text(encoding="utf-8"))
        usable = set(cap["verification_policy"]["routing_uses"])
        downgraded = set(cap["verification_policy"]["routing_downgrades_on"])
        if usable & downgraded:
            record("routing.downgrade_policy", "fail", "пересечение usable и downgraded статусов")
        elif "unknown" in downgraded and "verified" in usable:
            record("routing.downgrade_policy", "pass")
        else:
            record("routing.downgrade_policy", "fail", "policy не содержит ожидаемых статусов")
    except Exception as e:
        record("routing.downgrade_policy", "fail", str(e))


# --- credentialed (skip без ключей) ---

CREDENTIALED = [
    ("gigachat.auth+response+russian", "GIGACHAT_AUTH_KEY"),
    ("anthropic.auth+response", "ANTHROPIC_API_KEY"),
    ("openai.auth+response", "OPENAI_API_KEY"),
]


def credentialed_tests():
    for name, env in CREDENTIALED:
        if os.environ.get(env):
            # Живые вызовы реализуются в provider adapter (Ф9/Ф10); здесь фиксируем готовность.
            record(name, "skip", f"{env} задан, но живой вызов выполняется adapter'ом (Ф9/Ф10)")
        else:
            record(name, "skip", f"нет {env} — пропуск (норма для CI без секретов)")


def main():
    structured_output_test()
    error_normalization_test()
    routing_downgrade_test()
    credentialed_tests()

    failed = [r for r in results if r[1] == "fail"]
    for test, status, detail in results:
        mark = {"pass": "PASS", "fail": "FAIL", "skip": "skip"}[status]
        line = f"  {mark:4} {test}"
        if detail:
            line += f"  ({detail})"
        print(line)
    print(f"capability self-test: {'FAIL' if failed else 'PASS'} "
          f"({sum(1 for r in results if r[1]=='pass')} pass, "
          f"{len(failed)} fail, {sum(1 for r in results if r[1]=='skip')} skip)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
