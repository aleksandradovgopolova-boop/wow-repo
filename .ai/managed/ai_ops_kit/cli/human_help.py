"""Человеческая «передняя дверь» CLI (#675 Human API; P1 №8/№9 — фасад владельца).

Человек-владелец видит ПЕРВЫМ экраном короткий набор ДЕЙСТВИЙ на человеческом языке (7 глаголов
фасада, `HUMAN_ACTIONS` в `ai_ops_cli`), а не argparse-стену из 34 интентов и 30 флагов. Остальные
интенты — рабочая механика кита: они полностью работают и вызываются как раньше, но показываются
только по `ai-ops help --all`. Это ВИТРИНА, а не ограничение доступа.

Единственный источник набора действий — `HUMAN_ACTIONS` рядом с `INTENTS` (не дублируется здесь):
модуль только РЕНДЕРИТ переданное. `handle()` возвращает код выхода, если вызов — запрос помощи,
иначе None (и CLI разбирает интент/фасад как обычно).
"""
from __future__ import annotations


def render(intents, actions, full=False):
    """Текст двери. `actions` — HUMAN_ACTIONS (7 действий-фасада, ВЕДУТ); `intents` — реестр INTENTS
    (имя -> (описание, handler, needs_task)) для полного списка по `help --all`."""
    lines = ["AI Ops — что можно попросить у кита.", "", "Основные действия:"]
    for verb, spec in actions.items():
        lines.append(f"  ai-ops {verb:<9} {spec['label']}")
    if not full:
        lines += ["", "Все команды и флаги — ai-ops help --all"]
    else:
        lines += ["", "Продвинутое (рабочая механика кита — работает как раньше):"]
        for name, meta in intents.items():
            lines.append(f"  ai-ops {name:<10} {meta[0]}")
    return "\n".join(lines)


def handle(argv, intents, actions):
    """Если вызов — запрос человеческой двери (пусто или `help`), напечатать её и вернуть код выхода.
    Иначе вернуть None (CLI разбирает интент/фасад как обычно)."""
    if not argv or argv[0] == "help":
        print(render(intents, actions, full=("--all" in argv)))
        return 0
    return None
