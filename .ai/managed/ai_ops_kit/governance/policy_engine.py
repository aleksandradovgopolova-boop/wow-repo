#!/usr/bin/env python3
"""Policy Engine (PR-19): допустимое поведение AI по действию — Suggest → Prepare → Execute →
Require approval.

ЭТО ИСПОЛНЯЕМЫЙ GATE, А НЕ ПОЖЕЛАНИЕ В ТЕКСТЕ. По типу действия политика возвращает уровень, и
вызывающий обязан ему подчиниться: `enforce()` не выполнит действие, если уровень его не
разрешает, а `require_approval` физически блокирует до явного одобрения человека. Правило без
исполнения — пожелание (граница ленты 5).

Уровни (по возрастанию автономии AI):
  suggest          — AI только предлагает; исполняет человек;
  prepare          — AI вправе подготовить (черновик/стейджинг), но не исполнить;
  execute          — AI вправе исполнить автономно;
  require_approval  — AI вправе исполнить ТОЛЬКО после явного одобрения человека.

Источник — `.ai-ops/POLICY.yaml` (артефакт Product Operating Layer). Поканальные уровни объявляет
ключ `autonomy:` (схема официального шаблона `templates/product-layer/POLICY.yaml`); историческая
форма `actions:` тоже читается (совместимость). `default:` необязателен:
  autonomy:
    update_artifacts: prepare
    create_pr:        prepare
    merge:            require_approval
    change_policy:    require_approval

FAIL-CLOSED: нет файла или действие не описано → уровень по умолчанию, а по умолчанию —
require_approval (человек в контуре). Неизвестный уровень в файле → ошибка, а не догадка.

Использование:  python3 -m ai_ops_kit.governance.policy_engine <repo_root> <action> [--approved]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

SUGGEST = "suggest"
PREPARE = "prepare"
EXECUTE = "execute"
REQUIRE_APPROVAL = "require_approval"
LEVELS = (SUGGEST, PREPARE, EXECUTE, REQUIRE_APPROVAL)

DEFAULT_LEVEL = REQUIRE_APPROVAL          # fail-closed: по умолчанию человек одобряет
POLICY_REL = ".ai-ops/POLICY.yaml"


class PolicyInvalid(Exception):
    """POLICY.yaml недостоверен — уровень не из набора. Fail-closed: не угадываем."""


class PolicyBlocked(Exception):
    """Действие не разрешено политикой (или ждёт одобрения) — enforce() его не выполнил."""


def load_policy(root: Path) -> dict:
    path = Path(root) / POLICY_REL
    if not path.is_file():
        return {"default": DEFAULT_LEVEL, "actions": {},
                "source": f"по умолчанию ({POLICY_REL} отсутствует — require_approval)"}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise PolicyInvalid(f"{POLICY_REL}: ожидался mapping, получен {type(data).__name__}")
    default = data.get("default", DEFAULT_LEVEL)
    # СВЕДЕНИЕ СХЕМ (блокер Фазы Б, enforcement.py). Официальный шаблон
    # `templates/product-layer/POLICY.yaml` объявляет поканальные уровни под ключом `autonomy:`
    # (update_artifacts/create_pr/merge/change_policy), а исторические примеры и старые дочки — под
    # `actions:`. Раньше движок читал только `actions:`/`default:`, поэтому `autonomy:`-уровни дочки
    # молча игнорировались и КАЖДОЕ действие падало в default (require_approval). Читаем ОБА ключа в
    # одну карту action->level; при совпадении имени явный `actions:` (старая форма) переопределяет.
    autonomy = data.get("autonomy") or {}
    actions_legacy = data.get("actions") or {}
    if not isinstance(autonomy, dict):
        raise PolicyInvalid(f"{POLICY_REL}: autonomy должен быть mapping")
    if not isinstance(actions_legacy, dict):
        raise PolicyInvalid(f"{POLICY_REL}: actions должен быть mapping")
    actions = {**autonomy, **actions_legacy}
    for name, level in list(actions.items()) + [("default", default)]:
        if level not in LEVELS:
            raise PolicyInvalid(
                f"{POLICY_REL}: уровень '{level}' для '{name}' не из {LEVELS}")
    return {"default": default, "actions": actions, "source": POLICY_REL}


def level_for(action: str, policy: dict) -> str:
    return (policy.get("actions") or {}).get(action, policy.get("default", DEFAULT_LEVEL))


def authorize(action: str, policy: dict, *, approved: bool = False) -> dict:
    """Решение политики по действию. -> {action, level, allowed, requires_approval, reason}."""
    level = level_for(action, policy)
    requires_approval = level == REQUIRE_APPROVAL
    if level == EXECUTE:
        allowed, reason = True, "policy: execute — исполнять автономно разрешено"
    elif level == REQUIRE_APPROVAL:
        allowed = bool(approved)
        reason = ("policy: require_approval — одобрено человеком"
                  if approved else
                  "policy: require_approval — требует одобрения человека, заблокировано")
    elif level == PREPARE:
        allowed, reason = False, "policy: prepare — можно подготовить, но не исполнять"
    else:  # SUGGEST
        allowed, reason = False, "policy: suggest — только предложить, исполняет человек"
    return {"action": action, "level": level, "allowed": allowed,
            "requires_approval": requires_approval, "reason": reason}


def may_execute(action: str, policy: dict, *, approved: bool = False) -> bool:
    return authorize(action, policy, approved=approved)["allowed"]


def enforce(action: str, policy: dict, do, *, approved: bool = False):
    """Исполнить `do()` ТОЛЬКО если политика разрешает; иначе PolicyBlocked. Это и есть
    исполнение политики: без разрешения действие не происходит."""
    decision = authorize(action, policy, approved=approved)
    if not decision["allowed"]:
        raise PolicyBlocked(decision["reason"])
    return do()


def main(argv) -> int:
    args = [a for a in argv if a != "--approved"]
    if len(args) < 2:
        print(__doc__)
        return 1
    root = Path(args[0])
    if not root.is_dir():
        print(f"не каталог: {root}", file=sys.stderr)
        return 2
    try:
        policy = load_policy(root)
    except PolicyInvalid as exc:
        print(f"политика недостоверна: {exc}", file=sys.stderr)
        return 2
    decision = authorize(args[1], policy, approved="--approved" in argv)
    decision["policy_source"] = policy.get("source")
    print(json.dumps(decision, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
