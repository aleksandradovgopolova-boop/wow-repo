#!/usr/bin/env python3
"""session_guardrails.py (v3.16.0 Development Culture Guardrails, WP3+WP5) — политика экономии сессии
(пороги контекста) + рекомендация по границе сессии + Task Completion Ritual.

WP3 SessionEconomyPolicy: пороги гигиены контекста (стартовые, калибруются на реальных данных).
WP5 Task Completion Ritual: после завершения WorkItem — обязательная SessionRecommendation с ТОЧНОЙ
командой (один из 4 исходов: continue / compact / clear / new_session), плюс defer на НЕбезопасной границе.

ГРАНИЦА ЧЕСТНОСТИ: кит НЕ управляет рантаймом Claude Code и НЕ форсит /clear|/compact — он даёт СИЛЬНЫЙ
совет с точной командой (решение владельца — enforcement=advise, не block). Кит не прерывает посреди
миграции/коммита: на небезопасной границе рекомендация — дождаться безопасной точки.

CLI:  session_guardrails.py <child_root> [--workitem WID] [--context N] [--next-relation R]
                            [--next "текст"] [--pr URL] [--checks "183/183"] [--unsafe] [--json]
      session_guardrails.py --selftest
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402
from ai_ops_kit.engops import session_telemetry  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

SESSION_ECONOMY_DEFAULTS = {
    "target_context_tokens": 150000,
    "compact_recommended_at": 250000,
    "new_session_recommended_at": 400000,
    "one_task_per_session": True,
    "allow_cross_task_session": False,
}

CONTEXT_STATES = ("normal", "attention", "compact_recommended", "new_session_recommended", "unknown")
# отношение следующей задачи к текущей (словарь WP2; здесь принимается как вход)
CONTINUE_RELATIONS = ("same_task", "continuation")
NEW_RELATIONS = ("new_independent_task", "new_product")


def load_policy(child_root):
    """SessionEconomyPolicy из .ai-ops.yaml (session_economy), поверх дефолтов."""
    pol = dict(SESSION_ECONOMY_DEFAULTS)
    cfg_p = Path(child_root) / ".ai-ops.yaml"
    if yaml and cfg_p.is_file():
        try:
            se = (yaml.safe_load(cfg_p.read_text(encoding="utf-8")) or {}).get("session_economy") or {}
            for k in SESSION_ECONOMY_DEFAULTS:
                if k in se:
                    pol[k] = se[k]
        except (yaml.YAMLError, OSError):
            pass
    return pol


def classify_context(tokens, policy=None):
    """Порог гигиены контекста. None -> unknown (честно: без числа не классифицируем)."""
    p = policy or SESSION_ECONOMY_DEFAULTS
    if tokens is None:
        return "unknown"
    if tokens >= p["new_session_recommended_at"]:
        return "new_session_recommended"
    if tokens >= p["compact_recommended_at"]:
        return "compact_recommended"
    if tokens >= p["target_context_tokens"]:
        return "attention"
    return "normal"


def _compact_cmd(wid):
    return (f"/compact Сохрани цель {wid or 'текущего WorkItem'}, принятые решения, изменённые файлы, "
            "результаты проверок, открытые блокеры и следующий безопасный шаг. Удали подробные логи, "
            "повторяющиеся исследования и промежуточные попытки.")


def _clear_cmds(wid, next_task):
    nxt = f'\nai-ops do "{next_task}"' if next_task else '\nai-ops do "<следующая задача>"'
    return f"/clear {wid or 'task'}-completed{nxt}"


def _new_session_cmds(repo_path, next_task):
    nxt = next_task or "<следующая задача>"
    return (f"/exit\n# в терминале:\ncd {repo_path or '<путь/к/репозиторию>'}\nclaude\n"
            f'# затем:\nai-ops do "{nxt}"')


def recommend(snapshot, policy=None, next_relation="new_independent_task",
              next_task=None, at_safe_boundary=True, task_done=True, repo_path=None):
    """4 исхода + defer. Приоритет: гигиена сессии/контекста > продолжение/новизна задачи.
    strong-advice: outcome + причина + ТОЧНАЯ команда; НЕ блокирует прогон движка."""
    p = policy or SESSION_ECONOMY_DEFAULTS
    ctx = snapshot.get("context_current")
    state = classify_context(ctx, p)
    wid = snapshot.get("workitem_id")
    ctx_txt = "н/д" if ctx is None else f"{ctx/1000:.0f}k [{snapshot.get('context_status')}]"

    if not at_safe_boundary:
        return {"outcome": "defer", "context_state": state,
                "reason": "небезопасная граница (миграция/незавершённый commit) — не прерываем; "
                          "дождаться безопасной точки, затем перезапросить рекомендацию.",
                "command": None, "context": ctx_txt}

    same = next_relation in CONTINUE_RELATIONS

    # слишком тяжёлая сессия -> новая сессия ВНЕ зависимости от новизны (гигиена важнее)
    if state == "new_session_recommended":
        if same and not task_done:
            return {"outcome": "compact", "context_state": state,
                    "reason": f"контекст {ctx_txt} превысил порог новой сессии, но WorkItem не завершён "
                              "и его контекст нужен — сначала compact на безопасной границе.",
                    "command": _compact_cmd(wid), "context": ctx_txt}
        return {"outcome": "new_session", "context_state": state,
                "reason": f"контекст {ctx_txt} — слишком тяжёлая сессия; не начинать новый блок работы "
                          "здесь. Handoff/решения сохранены в репозитории.",
                "command": _new_session_cmds(repo_path, next_task), "context": ctx_txt}

    if same:
        # продолжение того же WorkItem
        if not task_done and state in ("compact_recommended",):
            return {"outcome": "compact", "context_state": state,
                    "reason": f"тот же WorkItem не завершён, контекст {ctx_txt} уже дорогой, логическая "
                              "часть закрыта — compact и продолжить.",
                    "command": _compact_cmd(wid), "context": ctx_txt}
        return {"outcome": "continue", "context_state": state,
                "reason": f"следующий шаг — тот же WorkItem {wid or ''}, контекст {ctx_txt}; "
                          "собранные знания переиспользуются, повторное исследование не нужно.",
                "command": None, "context": ctx_txt}

    # новая независимая задача / новый продукт
    if not task_done:
        return {"outcome": "continue", "context_state": state,
                "reason": "текущий WorkItem ещё не завершён — сначала закрыть его, потом переключаться.",
                "command": None, "context": ctx_txt}
    if p.get("one_task_per_session", True):
        return {"outcome": "clear", "context_state": state,
                "reason": f"новая независимая задача (one-task-per-session); продолжение здесь заставит "
                          f"перечитывать нерелевантную историю ({ctx_txt}). Задача закрыта, handoff сохранён.",
                "command": _clear_cmds(wid, next_task), "context": ctx_txt}
    return {"outcome": "continue", "context_state": state,
            "reason": "cross-task-сессии разрешены политикой (allow_cross_task_session=true).",
            "command": None, "context": ctx_txt}


def completion_ritual(snapshot, policy=None, *, workitem_id=None, pr=None, checks=None,
                      next_relation="new_independent_task", next_task=None,
                      at_safe_boundary=True, handoff_saved=True, decisions_recorded=True,
                      committed=True, repo_path=None):
    """WP5: обязательный ритуал завершения WorkItem -> отчёт + usage + SessionRecommendation + NextCommand."""
    p = policy or SESSION_ECONOMY_DEFAULTS
    rec = recommend(snapshot, p, next_relation=next_relation, next_task=next_task,
                    at_safe_boundary=at_safe_boundary, task_done=True, repo_path=repo_path)
    checklist = {
        "result_achieved": True, "checks_passed": bool(checks),
        "state_saved": True, "handoff_created": handoff_saved,
        "decisions_recorded": decisions_recorded, "commit_or_pr": bool(pr) or committed,
        "usage_counted": snapshot.get("turns", 0) > 0,
    }
    return {
        "kind": "CompletionRitual", "schema_version": 1,
        "workitem_id": workitem_id or snapshot.get("workitem_id"),
        "completion_report": {"pr": pr, "checks": checks},
        "usage_summary": {
            "input_tokens": snapshot.get("input_tokens"), "output_tokens": snapshot.get("output_tokens"),
            "estimated_cost": snapshot.get("estimated_cost"), "cost_complete": snapshot.get("cost_complete"),
            "context_current": snapshot.get("context_current"), "context_status": snapshot.get("context_status"),
            "turns": snapshot.get("turns"),
        },
        "session_recommendation": rec,
        "next_command": rec.get("command"),
        "completion_checklist": checklist,
        "complete": all(checklist.values()),
    }


def check(r):
    """Валидация ритуала: 4 допустимых исхода(+defer), рекомендация присутствует, команда есть кроме continue."""
    e = []
    if not isinstance(r, dict) or r.get("kind") != "CompletionRitual":
        return ["kind должен быть CompletionRitual"]
    rec = r.get("session_recommendation") or {}
    if rec.get("outcome") not in ("continue", "compact", "clear", "new_session", "defer"):
        e.append(f"недопустимый outcome: {rec.get('outcome')}")
    if rec.get("outcome") in ("compact", "clear", "new_session") and not rec.get("command"):
        e.append(f"исход {rec.get('outcome')} обязан нести точную команду (NextCommand)")
    return e


def render_block(ritual):
    """Обязательный пользовательский блок (WP5)."""
    us, rec = ritual["usage_summary"], ritual["session_recommendation"]
    cost = us["estimated_cost"]
    cost_txt = ("н/д" if cost is None else
                f"${cost:.4f}" + ("" if us["cost_complete"] else " (НЕПОЛНАЯ)"))
    ctx = us["context_current"]
    ctx_txt = "н/д" if ctx is None else f"{ctx/1000:.0f}k [{us['context_status']}]"
    saved = [k for k, v in ritual["completion_checklist"].items() if v]
    L = [f"Задача завершена: {ritual['workitem_id'] or '—'}.", "",
         f"PR: {ritual['completion_report']['pr'] or '—'}",
         f"Проверки: {ritual['completion_report']['checks'] or '—'}",
         f"Стоимость: {cost_txt}",
         f"Контекст сессии: {ctx_txt}, ходов: {us['turns']}",
         f"Что сохранено: {', '.join(saved) or '—'}",
         f"Рекомендация: {rec['outcome'].upper()} — {rec['reason']}"]
    if rec.get("command"):
        L += ["", "Точная команда:", rec["command"]]
    if not ritual["complete"]:
        missing = [k for k, v in ritual["completion_checklist"].items() if not v]
        L += ["", f"⚠ ритуал НЕ завершён — не закрыто: {', '.join(missing)}"]
    return "\n".join(L)


def main(argv):
    wid = ctx = nrel = nxt = pr = checks = None
    args, it = [], iter(argv)
    unsafe = "--unsafe" in argv
    for a in it:
        if a == "--workitem":
            wid = next(it, None)
        elif a == "--context":
            v = next(it, None); ctx = int(v) if v and v.isdigit() else None
        elif a == "--next-relation":
            nrel = next(it, None)
        elif a == "--next":
            nxt = next(it, None)
        elif a == "--pr":
            pr = next(it, None)
        elif a == "--checks":
            checks = next(it, None)
        elif not a.startswith("--"):
            args.append(a)
    root = args[0] if args else "."
    pol = load_policy(root)
    snap = session_telemetry.snapshot(root, workitem_id=wid, context_current=ctx)
    # v3.17.0 WP2: если отношение не задано явно, но есть текст следующей задачи — классифицируем.
    if not nrel and nxt:
        try:
            from ai_ops_kit.engops import session_boundary
            nrel, _ = session_boundary.classify(current_workitem=wid, new_task=nxt)
        except Exception:  # noqa: BLE001
            nrel = None
    rit = completion_ritual(snap, pol, workitem_id=wid, pr=pr, checks=checks,
                            next_relation=nrel or "new_independent_task", next_task=nxt,
                            at_safe_boundary=not unsafe, repo_path=str(Path(root).resolve()))
    print(json.dumps(rit, ensure_ascii=False, indent=2) if "--json" in argv else render_block(rit))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
