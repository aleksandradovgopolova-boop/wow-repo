"""`ai-ops portfolio` — ОБЕЗЛИЧЕННЫЙ портфельный вид повторяющихся отказов дочек (T5).

Показывает, какие КЛАССЫ отказов повторяются по всему портфелю дочек, поднимая наверх ТОЛЬКО паттерны
и числа: обезличенный ключ дочки (`proj-…`), класс, счётчики. Ни путей, ни коммитов, ни имён, ни
вывода команд — за границу отвечает сторож `portfolio_patterns.check_anonymized`, и CLI прогоняет его
ПЕРЕД печатью: если что-то идентифицирующее просочилось в вид, команда не печатает вид, а честно
краснеет (лучше отказать, чем показать утечку).

Только чтение. Честные пустые состояния РАЗЛИЧНЫ и не подделываются: «дочек ещё нет» ≠ «дочки есть, но
отказов пока не наблюдалось».
"""
from __future__ import annotations

import json
from pathlib import Path


def _render(view: dict) -> str:
    L = [f"ПОРТФЕЛЬ ОТКАЗОВ (обезличенно): наблюдений {view['observation_count']} по "
         f"{view['child_count']} обезличенным дочкам (в охвате отмечено {view['registered_children']})."]
    for p in view.get("patterns") or []:
        L.append(f"  • {p['class_label']}: {p['frequency_label']} — "
                 f"{p['case_count']} наблюдений у {p['child_count']} дочек")
    kb = view.get("key_basis") or {}
    if kb:
        L.append("  основание ключей дочек: " + ", ".join(f"{k}×{v}" for k, v in kb.items()))
    L.append("  Наверх поднимаются только паттерны и числа — ни кода, ни путей, ни имён дочек.")
    return "\n".join(L)


def _render_empty(view: dict) -> str:
    if view.get("empty_state") == "no_children":
        return ("ПОРТФЕЛЬ ОТКАЗОВ: ни одна дочка ещё не отметилась в охвате — показывать нечего.\n"
                "Это «дочек пока нет», а НЕ «всё хорошо»: без дочек портфельного среза не существует.")
    return ("ПОРТФЕЛЬ ОТКАЗОВ: дочки в охвате есть, но отказов пока не наблюдалось.\n"
            "Это «наблюдений нет», а НЕ «нет дочек» — разные факты, и мы их не смешиваем.")


def run_portfolio(kit_root, js: bool = False) -> int:
    """Точка входа команды `portfolio`. Собирает обезличенный вид, прогоняет сторож границы, печатает.

    Сторож (`check_anonymized`) — ПЕРЕД печатью: утечка идентификации в вид означает провал DoD, и
    показывать такой вид нельзя. Нарушения → код 1 и список, а не молчаливый успех."""
    from ai_ops_kit.intelligence import portfolio_patterns
    view = portfolio_patterns.portfolio_patterns(Path(kit_root))
    violations = portfolio_patterns.check_anonymized(view)
    if violations:
        if js:
            print(json.dumps({"kind": "PortfolioPatterns", "anonymization_violations": violations},
                             ensure_ascii=False, indent=2))
        else:
            print("ПОРТФЕЛЬ ОТКАЗОВ: НЕ показываю — в вид просочилась идентификация дочки:")
            for v in violations:
                print(f"  ✗ {v}")
        return 1
    if js:
        print(json.dumps(view, ensure_ascii=False, indent=2))
        return 0
    print(_render(view) if view.get("empty_state") is None else _render_empty(view))
    return 0


def _intent_portfolio(task, child_root, signals, a) -> int:
    """Обработчик интента `portfolio` (регистрируется в ai_ops_cli)."""
    return run_portfolio(Path(child_root), js=bool(getattr(a, "json", False)))
