#!/usr/bin/env python3
"""delegation_advisor.py (v3.17.0 Development Culture Guardrails, WP4) — Delegation Culture: советует
вынести «тяжёлые для контекста» работы в ОТДЕЛЬНЫЙ краткоживущий контекст (сабагент), а в основную сессию
вернуть только СВОДКУ, чтобы разведка/логи/сравнения не оседали в контексте до конца сессии.

По умолчанию делегируются:
  repository_wide_exploration · large_log_analysis · many_file_comparison ·
  external_research · independent_review · mass_mechanical_inspection

В основной контекст возвращаются: краткий вывод, релевантные пути, evidence, открытые вопросы, следующий шаг.
НЕ возвращаются: полный transcript разведки, все прочитанные файлы, длинные необработанные логи,
промежуточные рассуждения.

Детекция — по сигналам задачи (числа/флаги). Пороги стартовые, калибруются. Кит СОВЕТУЕТ (не форсит).

CLI:  delegation_advisor.py [--files N] [--compare N] [--log-lines N] [--research] [--review]
                           [--mechanical] [--json]
      delegation_advisor.py --selftest
"""
from __future__ import annotations

import json
import sys

RETURN_TO_MAIN = ["краткий вывод", "релевантные пути", "evidence", "открытые вопросы", "следующий шаг"]
DO_NOT_RETURN = ["полный transcript разведки", "все прочитанные файлы", "длинные необработанные логи",
                 "промежуточные рассуждения"]

DEFAULT_THRESHOLDS = {"exploration_files": 8, "compare_files": 5, "log_lines": 500}


def advise(signals, thresholds=None):
    """-> список рекомендаций делегирования. Каждая: {trigger, reason, delegate_to, return_to_main, do_not_return}."""
    s = dict(signals or {})
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    recs = []

    def add(trigger, reason, delegate_to="Explore-сабагент"):
        recs.append({"trigger": trigger, "reason": reason, "delegate_to": delegate_to,
                     "return_to_main": list(RETURN_TO_MAIN), "do_not_return": list(DO_NOT_RETURN)})

    files = int(s.get("exploration_files") or 0)
    if s.get("repository_wide_exploration") or files >= t["exploration_files"]:
        n = files or "много"
        add("repository_wide_exploration",
            f"разведка затронет ~{n} файлов — последовательное чтение в основной сессии осядет в контексте "
            "до её конца; вернуть карту реализации и релевантные файлы, не сырьё")

    cmp_n = int(s.get("compare_files") or 0)
    if cmp_n >= t["compare_files"]:
        add("many_file_comparison",
            f"сравнение {cmp_n} файлов — держать все в основном контексте дорого; вернуть таблицу различий/вывод")

    log_n = int(s.get("log_lines") or 0)
    if s.get("large_log_analysis") or log_n >= t["log_lines"]:
        add("large_log_analysis",
            f"анализ большого лога (~{log_n or 'много'} строк) — не тащить сырые логи в основной контекст; "
            "вернуть найденные причины/строки-улики")

    if s.get("external_research"):
        add("external_research", "внешнее исследование — вернуть выводы и источники, не сырые страницы",
            delegate_to="research-сабагент")

    if s.get("independent_review"):
        add("independent_review", "независимое ревью (writer≠judge) — отдельный контекст судьи; "
            "вернуть вердикт и находки, не полный разбор", delegate_to="reviewer-сабагент")

    if s.get("mass_mechanical_inspection"):
        add("mass_mechanical_inspection",
            "массовая механическая проверка — вынести в дешёвый краткоживущий контекст; вернуть сводку")

    return recs


def check(recs):
    """Валидация: каждая рекомендация делегирует и возвращает СВОДКУ, а не сырьё."""
    e = []
    for r in recs or []:
        if not r.get("return_to_main"):
            e.append(f"{r.get('trigger')}: нет return_to_main (сводка обязательна)")
        if any(x in (r.get("return_to_main") or []) for x in DO_NOT_RETURN):
            e.append(f"{r.get('trigger')}: в основной контекст возвращается сырьё")
    return e


def render(recs):
    if not recs:
        return "делегирование не требуется: задача не тяжёлая для контекста."
    L = []
    for r in recs:
        L.append(f"▸ {r['trigger']}: {r['reason']}")
        L.append(f"    делегировать: {r['delegate_to']}")
        L.append(f"    вернуть в основной контекст: {', '.join(r['return_to_main'])}")
        L.append(f"    НЕ возвращать: {', '.join(r['do_not_return'])}")
    return "\n".join(L)


def main(argv):
    sig = {"external_research": "--research" in argv, "independent_review": "--review" in argv,
           "mass_mechanical_inspection": "--mechanical" in argv}
    it = iter(argv)
    for a in it:
        if a == "--files":
            v = next(it, None); sig["exploration_files"] = int(v) if v and v.isdigit() else 0
        elif a == "--compare":
            v = next(it, None); sig["compare_files"] = int(v) if v and v.isdigit() else 0
        elif a == "--log-lines":
            v = next(it, None); sig["log_lines"] = int(v) if v and v.isdigit() else 0
    recs = advise(sig)
    print(json.dumps(recs, ensure_ascii=False, indent=2) if "--json" in argv else render(recs))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
