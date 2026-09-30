"""Чистая проверка формы ContextBundle. Вынесена из `validation/validate_context_bundle.py` вниз
(слой `checks`, primitives), чтобы её звали ВНИЗ и рантайм (context/context_compiler.compile_bundle),
и CLI-обёртка-валидатор — без восходящего ребра context -> validation (v3.38-приём; иначе взаимная
пара с validate_context_qualification -> context).

Инварианты (эпик Context Engineering, этап 1 — Context Compiler):
  1. kind=ContextBundle, есть workitem_id, included-разделы, excluded[]-с-причинами;
  2. у КАЖДОГО исключённого источника непустая причина (не «молча выкинули»);
  3. estimated_tokens/context_budget присутствуют и положительны (размер измерен ДО модели);
  4. overflow=True обязан сопровождаться open_question про бюджет (контекст не обрезан молча);
  5. включённые агенты не пересекаются с исключёнными (один источник — одно решение).
Только stdlib — никакого ввода-вывода.
"""
from __future__ import annotations

REQUIRED_INCLUDED = ("project_context", "repository_context", "specifications", "decisions",
                     "files", "rules", "skills", "agents")


def check(bundle):
    errors = []
    if not isinstance(bundle, dict) or bundle.get("kind") != "ContextBundle":
        errors.append("kind должен быть 'ContextBundle'")
        return errors
    if not bundle.get("workitem_id"):
        errors.append("нет workitem_id")
    inc = bundle.get("included")
    if not isinstance(inc, dict):
        errors.append("included должен быть объектом")
        inc = {}
    for key in REQUIRED_INCLUDED:
        if key not in inc:
            errors.append(f"included: нет раздела '{key}'")
        elif not isinstance(inc[key], list):
            errors.append(f"included.{key} должен быть списком")
    exc = bundle.get("excluded")
    if not isinstance(exc, list):
        errors.append("excluded должен быть списком")
        exc = []
    for i, e in enumerate(exc):
        if not isinstance(e, dict) or not e.get("source") or not e.get("reason"):
            errors.append(f"excluded[{i}]: нужны непустые source и reason (не выкидываем молча)")
    tok, budget = bundle.get("estimated_tokens"), bundle.get("context_budget")
    if not isinstance(tok, int) or tok < 0:
        errors.append("estimated_tokens должен быть неотрицательным целым (размер измерен ДО модели)")
    if not isinstance(budget, int) or budget < 1:
        errors.append("context_budget должен быть положительным целым")
    if bundle.get("overflow") is True:
        oq = bundle.get("open_questions") or []
        if not any("бюджет" in str(q) or "budget" in str(q).lower() or "overflow" in str(q).lower() for q in oq):
            errors.append("overflow=True без open_question про бюджет (контекст обрезан молча — запрещено)")
    inc_agents = set(inc.get("agents", []) if isinstance(inc, dict) else [])
    exc_agents = {e.get("source", "").split("agent:", 1)[-1] for e in exc if str(e.get("source", "")).startswith("agent:")}
    overlap = inc_agents & exc_agents
    if overlap:
        errors.append(f"агенты одновременно included и excluded: {sorted(overlap)}")
    return errors
