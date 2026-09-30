"""Чистая проверка формы RunHandoff. Вынесена из `validation/validate_run_handoff.py` вниз
(слой `checks`, primitives), чтобы её звали ВНИЗ и рантайм (engine/run_handoff.build_handoff),
и CLI-обёртка-валидатор — без восходящего ребра engine -> validation (v3.38: все рёбра
«рантайм -> validation» сняты переносом логики сюда).

Инварианты (эпик Context Engineering, этап 3 — Context Lifecycle и Resume):
  1. kind=RunHandoff, есть workitem_id, next_action (следующий безопасный шаг);
  2. verification = {passed:[], failed:[]};
  3. completed/decisions/changed_files/open_questions/known_risks — списки;
  4. decisions[i] (если объект) несёт id и summary;
  5. resume_from_revision — строка (git sha) или null (прогон без коммита).
Только stdlib — никакого ввода-вывода.
"""
from __future__ import annotations

LIST_FIELDS = ("completed", "decisions", "changed_files", "open_questions", "known_risks")


def check(h):
    errors = []
    if not isinstance(h, dict) or h.get("kind") != "RunHandoff":
        errors.append("kind должен быть 'RunHandoff'")
        return errors
    if not h.get("workitem_id"):
        errors.append("нет workitem_id")
    if not h.get("next_action"):
        errors.append("нет next_action (следующий безопасный шаг обязателен)")
    ver = h.get("verification")
    if not isinstance(ver, dict) or "passed" not in ver or "failed" not in ver:
        errors.append("verification должен быть объектом с passed[] и failed[]")
    elif not isinstance(ver.get("passed"), list) or not isinstance(ver.get("failed"), list):
        errors.append("verification.passed/failed должны быть списками")
    for f in LIST_FIELDS:
        if f in h and not isinstance(h[f], list):
            errors.append(f"{f} должен быть списком")
    for i, d in enumerate(h.get("decisions", []) or []):
        if isinstance(d, dict) and (not d.get("id") or not d.get("summary")):
            errors.append(f"decisions[{i}]: нужны id и summary")
    rev = h.get("resume_from_revision")
    if rev is not None and not isinstance(rev, str):
        errors.append("resume_from_revision должен быть строкой (git sha) или null")
    return errors
