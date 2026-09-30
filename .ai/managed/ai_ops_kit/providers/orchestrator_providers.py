#!/usr/bin/env python3
"""Provider implementations for orchestrator — mock, anthropic, openai, claude-cli.

Extracted from orchestrator.py. Imports HTTP client from orchestrator_http and
usage recording from orchestrator_usage.

СТРУКТУРА (разрез монолита, сателлиты): чтобы модуль не рос выше потолка module-size, реализация
разложена на три соседних файла, а этот модуль — ФАСАД: он держит у себя кластер РЕЗОЛВА провайдера
(где искать `claude`, что выбрать для прогона и как громко об этом сказать) и ре-экспортирует
остальное. Публичная поверхность НЕ меняется — весь код и тесты по-прежнему обращаются к именам как
`orchestrator_providers.X`.

  · provider_errors      — типы отказов провайдера и распознаватели их сигнатур (фундамент);
  · provider_writer_lock — машинный замок локального писателя `claude -p` (фундамент);
  · provider_calls       — как СОБРАТЬ и ВЫЗВАТЬ провайдера (mock/anthropic/openai/claude-cli).

Направление зависимостей однонаправленное: фундамент ← provider_calls ← фасад. `provider_calls`
на уровне модуля фасад НЕ импортирует; `claude_binary`/`claude_missing_message` он берёт из фасада
ленивым импортом внутри `_claude_cli_call` — цикла импортов нет.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
from ai_ops_kit.shared import _bootstrap  # noqa: E402,F401

# _CALL_STATS читаем ЖИВЫМ (drain пересоздаёт список); модуль ре-экспортируется — часть кода
# импортирует `orchestrator_usage` именно отсюда.
from ai_ops_kit.providers import orchestrator_usage  # noqa: E402,F401

# --- Ре-экспорт публичной поверхности из сателлитов ------------------------------------------
# Явный перечень (не `import *`): фасад обязан отдавать КАЖДОЕ перенесённое имя как свой атрибут —
# внешние импортёры используют их как `orchestrator_providers.X`, в т.ч. приватные с `_`, которые
# импортируются извне (`_anthropic_call`, `_openai_call`, `_claude_cli_call`, `_MAX_TOKENS`, …).
from ai_ops_kit.providers.provider_errors import (  # noqa: E402,F401
    ProviderEnvUnavailableError,
    ProviderLimitError,
    _env_unavailable_envelope,
    _reset_hint,
    _session_limit,
)
from ai_ops_kit.providers.provider_writer_lock import (  # noqa: E402,F401
    _writer_lock_poll_seconds,
    _writer_serialization_lock,
    writer_lock_busy,
    writer_lock_path,
)
from ai_ops_kit.providers.provider_calls import (  # noqa: E402,F401
    DECLARED_NOT_IMPLEMENTED,
    DEFAULT_MODELS,
    OPENAI_COMPATIBLE_VENDORS,
    _MAX_TOKENS,
    _anthropic_call,
    _backoff,
    _claude_cli_call,
    _http_post_json,
    _human_error,
    _openai_call,
    _record_call,
    _response_format_for,
    _tagged,
    _transient,
    for_contract,
    make_claude_cli_provider,
    make_openai_provider,
    make_provider,
    mock_provider,
)


# ---------------- резолв провайдера (v3.28.x, review 2026-08-06, P0-1) ----------------
#
# Проблема: `--provider` имел хардкод-дефолт `mock`, поэтому в чистом репозитории `run --execute`
# давал «провайдер: mock · правок 0» даже когда `claude` есть в PATH, а `.ai-ops.yaml` объявляет
# providers.default: anthropic с ключом в env. Резолв ниже выбирает провайдера ЯВНО и ГРОМКО.
#
# Приоритет (первый сработавший побеждает):
#   1. явный --provider X (в т.ч. явный `mock`) — решение человека всегда сильнее автовыбора;
#   2. .ai-ops.yaml -> providers.default, ЕСЛИ ключ этого провайдера РЕАЛЬНО есть в env
#      (credentials_ref: "env:ANTHROPIC_API_KEY" -> проверяем os.environ, значение не читаем в лог);
#   3. `claude` в PATH -> claude-cli (локальная сессия, ключ не нужен);
#   4. иначе mock + ГРОМКОЕ предупреждение ДО прогона (а не молчаливый ноль правок постфактум).
#
# Инвариант офлайн-детерминизма (failure mode №1 Change Brief): автовыбор применяется ТОЛЬКО в
# пользовательском пути `run --execute`. Всё остальное (selftest, pytest, CI) получает mock —
# см. autoresolve_enabled(): под pytest/в CI автовыбор выключен по умолчанию, а
# AI_OPS_PROVIDER_AUTORESOLVE=0 выключает его где угодно явно (selftest-пути ставят именно его).

PROVIDER_AUTORESOLVE_ENV = "AI_OPS_PROVIDER_AUTORESOLVE"
_FALSY = {"0", "false", "no", "off", "none", ""}
_TRUTHY = {"1", "true", "yes", "on"}

# Имя env-переменной с ключом по провайдеру — ИЗ registry/providers.yaml (auth.env[0]).
# Используется только когда child-конфиг не задал credentials_ref явно.
PROVIDER_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "gigachat": "GIGACHAT_AUTH_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "qwen": "QWEN_API_KEY",
    "kimi": "KIMI_API_KEY",
    "local": "LOCAL_LLM_BASE_URL",
    "openai-compatible": "OPENAI_COMPATIBLE_API_KEY",
    "custom": "CUSTOM_PROVIDER_TOKEN",
}

NO_LIVE_PROVIDER_WARNING = ("живой провайдер не настроен → правок не будет; "
                            "задайте ANTHROPIC_API_KEY или установите claude CLI")

CLAUDE_BIN_ENV = "AI_OPS_CLAUDE_BIN"


def claude_lookup(env=None, which=None):
    """Где искали `claude` и что нашли -> {"path": str|None, "where": "named"|"path"}.

    ЗАЧЕМ ОТДЕЛЬНАЯ ФУНКЦИЯ, а не `shutil.which` по месту: проверка присутствия и запуск обязаны
    смотреть в ОДНО И ТО ЖЕ. Прежде выбор провайдера звал `which("claude")`, а запуск подставлял
    короткое имя `claude` — то есть два разных решения, и между ними успевало помещаться расхождение
    (поле 13.08 и 15.08.2026, дочка ИИ-Среда: `FileNotFoundError: 'claude'` при живом claude в
    терминале). Здесь путь вычисляется один раз и им же исполняется.

    ЗАЧЕМ ЕЩЁ И `where` (замер 18.08.2026): одного пути мало — решение о провайдере обязано
    называть, ОТКУДА взялся исполнитель. Автовыбор печатал «claude CLI найден в PATH» и в том
    случае, когда путь пришёл словом владельца, то есть говорил про PATH неправду; а на живом
    прогоне с битым `AI_OPS_CLAUDE_BIN` при claude в PATH — печатал ту же строку, выбирал
    claude-cli без предупреждения и умирал на первом вызове модели, уже зарегистрировав работу
    и подготовив рабочее дерево.

    `which` по умолчанию берёт PATH ИЗ ПЕРЕДАННОГО env, а не из окружения процесса: иначе замер с
    подменённым PATH молча смотрел бы в настоящий PATH и показывал ложную картину.
    """
    env = os.environ if env is None else env
    if which is None:
        def which(name, _path=env.get("PATH")):
            return shutil.which(name, path=_path)
    named = str(env.get(CLAUDE_BIN_ENV) or "").strip()
    if named:
        ok = os.path.isfile(named) and os.access(named, os.X_OK)
        return {"path": named if ok else None, "where": "named"}
    return {"path": which("claude"), "where": "path"}


def claude_binary(env=None, which=None):
    """Абсолютный путь к `claude` или None. Явное слово владельца (AI_OPS_CLAUDE_BIN) сильнее PATH."""
    return claude_lookup(env=env, which=which)["path"]


def claude_found_reason(lookup):
    """Человеческая причина «чем пойдём» — по ФАКТУ находки, а не по догадке о её источнике."""
    if lookup["where"] == "named":
        return (f"claude CLI взят из {CLAUDE_BIN_ENV}={lookup['path']} "
                "(путь назван явно; PATH не спрашивался)")
    return "claude CLI найден в PATH (локальная сессия, API-ключ не нужен)"


def claude_missing_message(env=None, extra=""):
    """Человеческая причина вместо `FileNotFoundError: 'claude'` — с ЗАМЕРОМ, а не с догадкой.

    Поле 13.08 и 15.08.2026 (ИИ-Среда): прогон падал этой строкой, и по ней нельзя было отличить
    «бинаря нет» от «бинарь есть, но не в PATH этого процесса» — а различие определяет, что делать.
    Поэтому сообщение называет: что искали, ГДЕ искали (реальный PATH процесса кита) и куда смотреть.
    """
    env = os.environ if env is None else env
    named = str(env.get(CLAUDE_BIN_ENV) or "").strip()
    entries = [p for p in (env.get("PATH") or "").split(os.pathsep) if p]
    where = (f"{CLAUDE_BIN_ENV}={named} — файла нет или он не исполняемый"
             if named else
             f"PATH процесса кита, записей {len(entries)}: {os.pathsep.join(entries)}")
    return ("не найден исполняемый файл `claude`, поэтому исполняющий прогон не начат"
            + (f" ({extra})" if extra else "") + ".\n"
            f"  где искали: {where}\n"
            "  почему это бывает при живом claude в терминале: PATH интерактивной оболочки "
            "(.zshrc/.zprofile) в процесс кита не попадает — например при запуске из другого "
            "окружения, из venv или из планировщика.\n"
            f"  что сделать (одно из трёх): назвать путь явно — {CLAUDE_BIN_ENV}=/путь/к/claude; "
            "добавить каталог claude в PATH того окружения, из которого запускаете кит; "
            "или попросить офлайн прямо — `--provider mock` (модель не вызывается, правок не будет).")


def autoresolve_enabled(env=None) -> bool:
    """Разрешён ли автовыбор провайдера. Явный AI_OPS_PROVIDER_AUTORESOLVE побеждает всегда;
    без него автовыбор выключен под pytest и в CI (офлайн-детерминизм, деньги не тратятся)."""
    env = os.environ if env is None else env
    raw = env.get(PROVIDER_AUTORESOLVE_ENV)
    if raw is not None:
        return str(raw).strip().lower() not in _FALSY
    if env.get("PYTEST_CURRENT_TEST"):
        return False
    if str(env.get("CI", "")).strip().lower() in _TRUTHY:
        return False
    return True


def _child_providers(root):
    """providers-секция child-конфига `.ai-ops.yaml`: {default, key_env: {id: ENV_NAME}}.
    Из credentials_ref берём ТОЛЬКО имя env-переменной (env:NAME); secret:-ссылку проверить
    из движка нельзя -> такой провайдер автовыбором не берём (fail-closed, не «вроде бы есть»)."""
    out = {"default": None, "key_env": {}, "unverifiable": {}}
    if not root:
        return out
    try:
        import yaml as _yaml
        p = Path(root) / ".ai-ops.yaml"
        if not p.is_file():
            return out
        data = _yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:   # noqa: BLE001 — битый/нечитаемый конфиг не должен ронять прогон
        return out
    prov = (data or {}).get("providers") if isinstance(data, dict) else None
    if not isinstance(prov, dict):
        return out
    d = prov.get("default")
    out["default"] = d if isinstance(d, str) and d else None
    for item in prov.get("configured") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        ref = str(item.get("credentials_ref") or "")
        if ref.startswith("env:") and ref[4:]:
            out["key_env"][item["id"]] = ref[4:]
        elif ref:
            out["unverifiable"][item["id"]] = ref.split(":", 1)[0]
    return out


def resolve_provider(explicit=None, root=None, env=None, which=None):
    """Выбрать провайдера для пользовательского прогона. Возвращает словарь-решение:
    {provider, source, reason, warning, autoresolve, checked} — имя провайдера НЕ теряется по
    дороге: вызывающий обязан передать его в make_provider и записать в отчёт прогона.

    explicit — значение --provider (None = пользователь не задавал; 'mock' = задал явно).
    root     — корень child-репозитория (там ищем .ai-ops.yaml).
    env/which — инъекция окружения и shutil.which (тестируемость без сети и без CLI)."""
    env = os.environ if env is None else env
    which = shutil.which if which is None else which
    checked = []

    if explicit:
        # ЯВНЫЙ ВЫБОР ЧЕЛОВЕКА НЕ ОСПАРИВАЕТСЯ, НО И НЕ ОСТАЁТСЯ НЕПРОВЕРЕННЫМ (поле 13.08 и
        # 15.08.2026, ИИ-Среда). Прежде здесь не проверялось ничего: `--provider claude-cli` без
        # бинаря доводил прогон до вызова модели и ронял его сырым `FileNotFoundError`, уже после
        # разбора, плана и подготовки дерева. Провайдер остаётся тем, что назвал человек — меняется
        # только то, что об отсутствии сказано ДО прогона, а не трейсбеком посреди него.
        warning = None
        if explicit == "claude-cli" and not claude_binary(env=env, which=which):
            warning = ("выбран claude-cli, но исполняемый файл `claude` не найден — прогон дойдёт "
                       "до вызова модели и остановится. " + claude_missing_message(env=env))
            checked.append("claude CLI не найден (провайдер задан явно)")
        return {"provider": explicit, "source": "explicit", "autoresolve": False,
                "reason": f"задан явно: --provider {explicit}", "warning": warning,
                "checked": checked}

    if not autoresolve_enabled(env):
        return {"provider": "mock", "source": "autoresolve-disabled", "autoresolve": False,
                "reason": (f"автовыбор выключен ({PROVIDER_AUTORESOLVE_ENV}=0 / pytest / CI) — "
                           "офлайн-детерминизм: mock"),
                "warning": None, "checked": checked}

    cfg = _child_providers(root)
    default = cfg.get("default")
    if default and default != "mock":
        key_env = cfg["key_env"].get(default) or PROVIDER_KEY_ENV.get(default)
        if default in cfg["unverifiable"] and default not in cfg["key_env"]:
            checked.append(f".ai-ops.yaml providers.default={default}: credentials_ref — "
                           f"{cfg['unverifiable'][default]}:-ссылка, из движка не проверяется")
        elif not key_env:
            checked.append(f".ai-ops.yaml providers.default={default}: неизвестно, какой env-ключ "
                           "проверять (нет credentials_ref и записи в registry)")
        elif env.get(key_env):
            return {"provider": default, "source": "child-config", "autoresolve": True,
                    "reason": f".ai-ops.yaml providers.default={default}, ключ {key_env} есть в env",
                    "warning": None, "checked": checked, "key_env": key_env}
        else:
            checked.append(f".ai-ops.yaml providers.default={default}: {key_env} отсутствует в env")

    # АВТОВЫБОР СПРАШИВАЕТ ТО ЖЕ, ЧЕМ БУДЕТ ЗАПУСКАТЬ (замер 18.08.2026). Здесь стоял голый
    # `which("claude")` — тот самый второй взгляд, который `claude_lookup` заводился устранить, и
    # расхождение осталось живым ровно на пути автовыбора (явный `--provider claude-cli` проверялся
    # с 17.08, PR #141). Замерено два направления, и оба врали человеку:
    #   · назван рабочий путь, claude вне PATH (запуск из venv/планировщика) -> `which` пусто ->
    #     «живого провайдера не нашлось» и mock, то есть «правок не будет» при живом исполнителе;
    #   · claude в PATH, назван битый путь -> `which` есть -> claude-cli без предупреждения, работа
    #     зарегистрирована, дерево подготовлено, и прогон умирает на первом вызове модели.
    _look = claude_lookup(env=env, which=which)
    if _look["path"]:
        return {"provider": "claude-cli",
                "source": "claude-cli-named" if _look["where"] == "named" else "claude-cli-in-path",
                "reason": claude_found_reason(_look),
                "autoresolve": True, "warning": None, "checked": checked}
    checked.append(f"{CLAUDE_BIN_ENV}={env.get(CLAUDE_BIN_ENV)} — файла нет или он не исполняемый"
                   if _look["where"] == "named" else "claude CLI не найден в PATH")

    # СОВЕТ ПО ПРИЧИНЕ, А НЕ ОДИН НА ВСЕ СЛУЧАИ: «установите claude CLI» человеку, у которого CLI
    # стоит и назван, а сломан путь, отправляет чинить не то. Названный путь сильнее PATH осознанно
    # (иначе кит молча пошёл бы другим исполнителем, чем ему сказали), поэтому здесь именно отказ с
    # причиной — до прогона, а не посреди него.
    warning = (f"назван {CLAUDE_BIN_ENV}={env.get(CLAUDE_BIN_ENV)}, но файла нет или он не "
               "исполняемый → правок не будет; поправьте путь или уберите переменную, чтобы "
               "искать claude в PATH") if _look["where"] == "named" else NO_LIVE_PROVIDER_WARNING
    return {"provider": "mock", "source": "fallback", "autoresolve": True,
            "reason": "живого провайдера не нашлось", "warning": warning,
            "checked": checked}


def print_provider_resolution(res, printer=print):
    """Громкая печать решения ДО прогона (честность деклараций: скатились в mock — говорим сразу)."""
    if not isinstance(res, dict):
        return
    if res.get("warning"):
        # ИМЯ ПРОВАЙДЕРА БЕРЁТСЯ ИЗ РЕШЕНИЯ, а не вписано «mock» намертво: предупреждение бывает и
        # у живого выбора (явный claude-cli без бинаря), и тогда жёсткая строка сообщала владельцу
        # неправду о том, чем пойдёт прогон. Для прежнего случая (fallback) провайдер и есть mock,
        # поэтому вывод там не изменился.
        printer(f"⚠ провайдер: {res.get('provider')} — {res['warning']}")
        for c in res.get("checked") or []:
            printer(f"  · {c}")
    elif res.get("source") not in (None, "explicit"):
        printer(f"провайдер: {res.get('provider')} — {res.get('reason')}")


def main(argv):
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
