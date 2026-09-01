#!/usr/bin/env python3
"""session_events.py — подписчик engops на события ядра (v3.38, Wave 3).

Заменяет прямой импорт session_telemetry/guardrails из engine. Ядро испускает
событие run_completed; этот модуль подписывается и выполняет session recommendation.

ADVISE-ONLY: рекомендация по гигиене сессии не блокирует прогон/доставку.
Отсутствие рекомендации в отчёте видно по отсутствию ключа session_recommendation.

Регистрация: вызывается из engops/__init__.py при первом импорте пакета.
"""
from __future__ import annotations

from ai_ops_kit.shared.events import subscribe


def _on_run_completed(data: dict) -> None:
    """Обработчик события run_completed — session recommendation."""
    try:
        from ai_ops_kit.engops import session_telemetry as _st
        from ai_ops_kit.engops import session_guardrails as _sg
    except ImportError:
        return

    child_root = data.get("child_root", ".")
    fid = data.get("workitem_id", "")
    rep = data.get("report", {})
    if not rep or not fid:
        return

    try:
        _snap = _st.snapshot(child_root, workitem_id=fid)
        _pol = _sg.load_policy(child_root)
        _done = bool(data.get("ready_for_pr"))
        _pr = rep.get("pull_request") or (rep.get("delivery") or {}).get("pr_url")
        if _done:
            _rit = _sg.completion_ritual(_snap, _pol, workitem_id=fid, pr=_pr,
                                         next_relation="new_independent_task",
                                         at_safe_boundary=True, repo_path=str(child_root))
            rep["session_recommendation"] = _rit["session_recommendation"]
            rep["completion_ritual"] = {k: _rit[k] for k in
                                        ("completion_checklist", "complete", "next_command")}
        else:
            rep["session_recommendation"] = _sg.recommend(_snap, _pol,
                                                          next_relation="continuation", task_done=False)
    except Exception:  # noqa: BLE001,S110 — ADVISE-ONLY: ошибка не роняет ядро
        pass


# Регистрация при импорте модуля
subscribe("run_completed", _on_run_completed)
