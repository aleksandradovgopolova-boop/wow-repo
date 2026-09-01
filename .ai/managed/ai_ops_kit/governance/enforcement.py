#!/usr/bin/env python3
"""Governance enforcement seam (Фаза 4 — проводка, работа `governance-enforce-observe`).

ЕДИНСТВЕННОЕ место, где решение политики о ДОСТАВКЕ впрыскивается в путь исполнения. Композирует
три уже построенных, но не звавшихся из рантайма куска (находка аудита 24.08.2026 «governance
декоративна»): `policy_engine` (уровень действия), `human_override` (одобрение человека —
ЕДИНСТВЕННЫЙ честный источник «approved», а не флаг и не догадка), `decision_log` (журнал: решение
без записи нельзя пересмотреть).

Два режима (`enforcement` в `.ai-ops/POLICY.yaml`, по умолчанию **observe**):
  observe — решение СЧИТАЕТСЯ и ЗАПИСЫВАЕТСЯ, но доставку НЕ останавливает. Видно, что было бы
            заблокировано, до включения принуждения — радиус поражения виден заранее.
  block   — `require_approval` без одобрения человека РЕАЛЬНО блокирует доставку.

FAIL-CLOSED по политике наследуется от `policy_engine`: нет `POLICY.yaml` → require_approval.
FAIL-OPEN по журналу сознательно: недоступный decision_log НЕ роняет доставку (решение всё равно
возвращается вызывающему) — журнал охраняет пересматриваемость, а не саму доставку.

Кит на этом шве ОТКРЫВАЕТ PR (`create_pr`), а не сливает его: слияние в main выполняет auto-merge
под branch protection, это не действие AI. Поэтому действие по умолчанию — `create_pr` (имя как в
шаблоне `templates/product-layer/POLICY.yaml`).

ИЗВЕСТНЫЙ БЛОКЕР ФАЗЫ Б (принуждение block): шаблон `POLICY.yaml` объявляет уровни под ключом
`autonomy:`, а `policy_engine.load_policy` читает `default:`/`actions:` — то есть поканальные уровни
шаблона движок сейчас НЕ читает и всё падает в `default` (require_approval). Для observe это
безопасно (fail-closed, только запись), но ДО включения block эти две схемы обязаны быть сведены,
иначе `autonomy:`-настройки дочки будут молча проигнорированы.
"""
from __future__ import annotations

import datetime
from pathlib import Path

import yaml

from ai_ops_kit.governance import decision_log, human_override, policy_engine

OBSERVE = "observe"
BLOCK = "block"
MODES = (OBSERVE, BLOCK)

DEFAULT_ACTION = "create_pr"
_APPROVE_DECISIONS = {"approve", "approved", "allow", "allowed", "yes"}


def enforcement_mode(root) -> str:
    """Режим принуждения из `.ai-ops/POLICY.yaml` (`enforcement:`). По умолчанию observe; любое
    непонятное значение трактуется как observe (безопасная сторона: не блокировать вслепую)."""
    path = Path(root) / policy_engine.POLICY_REL
    if not path.is_file():
        return OBSERVE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return OBSERVE
    mode = data.get("enforcement", OBSERVE) if isinstance(data, dict) else OBSERVE
    return mode if mode in MODES else OBSERVE


def is_approved(root, *, action: str, target: str) -> bool:
    """Одобрено ли действие человеком — по ЗАПИСАННОМУ сигналу human_override, а не по флагу.

    Совпадение по цели: сигнал с target == `{action}:{target}` или ровно `target`, и решением из
    множества одобрения. Отсутствие сигнала — «не одобрено» (fail-closed), не «не знаю = можно»."""
    want = {f"{action}:{target}", target}
    for sig in human_override.override_signals(root):
        if sig.get("target") in want and str(sig.get("human_decision", "")).lower() in _APPROVE_DECISIONS:
            return True
    return False


def gate_delivery(root, *, target: str, action: str = DEFAULT_ACTION, date: str | None = None) -> dict:
    """Решение политики о доставке + запись в журнал. Доставку НЕ выполняет — только РЕШАЕТ и
    ЗАПИСЫВАЕТ. Возвращает decision из `authorize` + поля approved/mode/outcome/blocked/logged.

    `blocked` истинно ТОЛЬКО когда режим block И действие не разрешено (в observe всегда False —
    вызывающий доставляет, но решение уже записано)."""
    root = Path(root)
    policy = policy_engine.load_policy(root)
    approved = is_approved(root, action=action, target=target)
    decision = policy_engine.authorize(action, policy, approved=approved)
    mode = enforcement_mode(root)
    blocked = bool(mode == BLOCK and not decision["allowed"])
    outcome = "allow" if decision["allowed"] else ("blocked" if blocked else "would_block")
    logged = _record(root, action=action, target=target, outcome=outcome,
                     reason=f"{decision['reason']} [режим {mode}]", date=date)
    return {**decision, "approved": approved, "mode": mode, "outcome": outcome,
            "blocked": blocked, "logged": logged}


def _record(root, *, action, target, outcome, reason, date) -> bool:
    """Записать решение в decision_log. FAIL-OPEN: журнал недоступен (нет реестра решений, дубль id)
    НЕ роняет доставку. -> удалось ли записать."""
    try:
        decision_log.log_ai_decision(
            root,
            decision_id=f"delivery-{action}-{target}",
            question=f"{action} @ {target}: разрешить доставку?",
            decision=outcome,
            reason=reason,
            date=date or datetime.date.today().isoformat(),
            data=f"governance gate: действие {action}, цель {target}")
        return True
    except (decision_log.RegistryError, OSError, yaml.YAMLError, ValueError):
        # FAIL-OPEN по журналу: нет реестра решений, дубль id, битый YAML — доставку НЕ роняем.
        return False


def main(argv) -> int:
    import json
    if len(argv) < 2:
        print("usage: enforcement.py <repo_root> <target> [--action NAME]", file=__import__("sys").stderr)
        return 1
    root, target = argv[0], argv[1]
    action = DEFAULT_ACTION
    if "--action" in argv:
        action = argv[argv.index("--action") + 1]
    print(json.dumps(gate_delivery(root, target=target, action=action), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
