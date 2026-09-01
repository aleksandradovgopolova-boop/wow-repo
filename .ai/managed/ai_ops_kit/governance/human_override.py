#!/usr/bin/env python3
"""Human Override (PR-20): человек всегда вправе переопределить приоритет, roadmap, статус или
рекомендацию AI — и это СИГНАЛ на будущее, а НЕ ошибка.

Override записывается в AI Decision Log (dp-001: тот же журнал, `human_overrode: true`), а не в
отдельный лог провалов. Он не роняет процесс и не помечается ошибкой: `record_override` — обычная
запись, которая возвращается нормально и остаётся читаемой. `override_signals` отдаёт прошлые
override'ы в структурированном виде, чтобы будущие решения (policy, risk, приоритизация) могли их
учесть — само обучение на override'ах это Фаза 5 (learning-from-human-overrides), здесь — канал.

Пакет governance — лист: этот модуль опирается только на decision_log (свой пакет) и stdlib,
других пакетов ai_ops_kit не импортирует.

Использование:  python3 -m ai_ops_kit.governance.human_override <repo_root> --list
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from ai_ops_kit.governance import decision_log

_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(s: str) -> str:
    return _SLUG.sub("-", s.lower()).strip("-") or "target"


def record_override(root: Path, *, target: str, ai_recommendation: str, human_decision: str,
                    reason: str, date: str, override_id=None,
                    registry_rel: str = decision_log.REGISTRY_REL) -> dict:
    """Записать человеческий override как сигнал в AI Decision Log. -> записанный эпизод.

    target — что переопределено (напр. 'priority:work-X', 'roadmap', 'status:work-Y',
    'recommendation:next'). Не бросает и не помечает ошибкой: override — законный сигнал.
    """
    episode = decision_log.build_ai_episode(
        decision_id=override_id or f"override-{date}-{_slug(target)}",
        question=f"Переопределить {target}?",
        decision=human_decision,
        reason=reason,
        date=date,
        data=f"AI рекомендовал: {ai_recommendation}",
        outcome="override человека принят как сигнал на будущее (не ошибка)",
        human_overrode=True,
        context={"override_target": target, "ai_recommendation": ai_recommendation},
    )
    decision_log.append_ai_decision(root, episode, registry_rel=registry_rel)
    return episode


def overrides(root: Path, registry_rel: str = decision_log.REGISTRY_REL) -> list:
    """Все эпизоды-override (human_overrode == true)."""
    return [e for e in decision_log.ai_decisions(root, registry_rel)
            if e.get("human_overrode") is True]


def override_signals(root: Path, registry_rel: str = decision_log.REGISTRY_REL) -> list:
    """Прошлые override'ы в форме, пригодной для учёта будущими решениями."""
    out = []
    for e in overrides(root, registry_rel):
        ctx = e.get("context") or {}
        out.append({
            "target": ctx.get("override_target"),
            "human_decision": e.get("decision"),
            "ai_recommendation": ctx.get("ai_recommendation"),
            "reason": e.get("reason"),
            "date": e.get("date"),
        })
    return out


def main(argv) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    root = Path(args[0])
    if not root.is_dir():
        print(f"не каталог: {root}", file=sys.stderr)
        return 2
    if "--list" in argv:
        print(json.dumps(override_signals(root), indent=2, ensure_ascii=False))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
