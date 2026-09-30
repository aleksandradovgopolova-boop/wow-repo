#!/usr/bin/env python3
"""model_router.py (v3.7.5) — provider-neutral runtime resolver (ADR-004).

Делает провайдер-независимость ИСПОЛНЯЕМОЙ: роль -> КОНКРЕТНАЯ самая дешёвая КВАЛИФИЦИРОВАННАЯ модель
(не class×role, не вендор). Соединяет три реестра:
  - model-roles.yaml        — требование роли (preferred/fallback CLASS) + escalation-policy;
  - model-qualification.yaml — допуск model×revision×role (status ИЗ Bench, safety-first);
  - models.yaml             — конкретные модели, классы, cost_class, revision.

resolve(role): среди допущенных для роли моделей в требуемом классе — берёт самую дешёвую. Экономика В
ДЕНЬГАХ (v3.7.10): если у ВСЕХ кандидатов есть total_cost_per_verified_change -> сортировка по деньгам
(cost_basis=money); иначе честный tokens-fallback + cost_warning (нет тарифа -> порядок может не совпасть
с деньгами). Нет допущенной модели -> resolved=false + escalation (НЕ
берём неквалифицированную ради дешевизны — safety over economy). Стоимость класса НЕ считается по
неквалифицированной модели. escalation_decision(): abstain/schema_invalid -> targeted retry -> эскалация
ТОЛЬКО review/judge-вызова (escalate_scope=review_only), не всей задачи.

Только stdlib+pyyaml. CLI: model_router.py <role> [--json] | --selftest
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
_COST_RANK = {"low": 0, "medium": 1, "high": 2, None: 1}
# ADR-004 (уточнено измеренной квалификацией 2026-07-28): роль ПИСАТЕЛЯ/эконом-ревью допускает
# conditional-модель (дешёвый пишет, гейты страхуют); строгий СУДЬЯ — только qualified (safety-first,
# судья не может быть «условным»). false_green>0 -> не допускается НИКОГДА и ни к какой роли.
WRITER_ROLES = {"implementation", "code_review"}
STRICT_JUDGE_ROLES = {"security_review", "integration_judge"}


def _eligible(q, role):
    fg = (q.get("metrics") or {}).get("false_green", 1)
    if fg is None or fg > 0:
        return False
    st = q.get("status")
    if role in STRICT_JUDGE_ROLES:
        return st == "qualified"
    return st in ("qualified", "conditional")


def _load():
    r = yaml.safe_load((PKG / "registry" / "model-roles.yaml").read_text(encoding="utf-8"))
    q = yaml.safe_load((PKG / "registry" / "model-qualification.yaml").read_text(encoding="utf-8"))
    m = yaml.safe_load((PKG / "registry" / "models.yaml").read_text(encoding="utf-8"))
    models = {x["id"]: x for x in m.get("models", []) if x.get("id")}
    return r, q.get("qualifications", []), models


def resolve(role, roles_cfg=None, quals=None, models=None, exclude_models=None):
    if roles_cfg is None:
        roles_cfg, quals, models = _load()
    req = (roles_cfg.get("roles") or {}).get(role, {})
    allowed_classes = {req.get("preferred_class"), req.get("fallback_class")} - {None}
    exclude_models = set(exclude_models or ())

    def m_classes(mid):
        return set((models.get(mid) or {}).get("classes", []) or [])

    # кандидаты: ДОПУЩЕННЫЕ для роли (writer: qualified∨conditional; судья: только qualified; fg=0 всегда)
    # И входящие в требуемый класс роли; v3.8.3: exclude_models -> writer≠judge (исключаем модель судьи)
    cands = [q for q in quals if q.get("role") == role and _eligible(q, role)
             and q.get("model_id") not in exclude_models
             and (not allowed_classes or (m_classes(q.get("model_id")) & allowed_classes))]

    # v3.7.10 экономика В ДЕНЬГАХ: если у ВСЕХ кандидатов роли есть total_cost_per_verified_change
    # (деньги), сортируем по деньгам; иначе честный tokens-fallback + warning (ранжирование может не
    # совпасть с деньгами). Токен-прокси врёт при разнице тарифов — деньги основной критерий.
    def _money(q):
        v = (q.get("economics") or {}).get("total_cost_per_verified_change")
        return v if isinstance(v, (int, float)) else None

    def _tokens(q):
        t = (q.get("economics") or {}).get("tokens_per_verified_change")
        if isinstance(t, (int, float)):
            return t
        cpc = q.get("metrics", {}).get("cost_per_change")   # legacy-поле (синтетика/старые записи)
        return cpc if isinstance(cpc, (int, float)) else None

    money_mode = bool(cands) and all(_money(q) is not None for q in cands)

    def cost_key(q):
        primary = _money(q) if money_mode else _tokens(q)
        return (primary if isinstance(primary, (int, float)) else 1e18,
                _COST_RANK.get((models.get(q.get("model_id")) or {}).get("cost_class"), 1))

    cands.sort(key=cost_key)
    if not cands:
        strict = role in STRICT_JUDGE_ROLES
        return {"kind": "ModelResolutionResult", "resolved": False, "role": role,
                "reason": ("нет QUALIFIED судьи для роли (строгая роль: conditional НЕ годится, safety over economy)"
                           if strict else
                           "нет допущенной модели для роли (qualified∨conditional при false_green=0)"),
                "required_class": sorted(allowed_classes),
                "escalation": {"needs": ("qualified судья / человек" if strict
                                         else "qualified∨conditional model в требуемом классе / человек"),
                               "escalate_scope": (roles_cfg.get("escalation_policy") or {}).get("escalate_scope")}}
    top = cands[0]
    fb = cands[1] if len(cands) > 1 else None
    cost_basis = "money" if money_mode else "tokens-fallback"
    res = {"kind": "ModelResolutionResult", "resolved": True, "role": role,
           "model_id": top["model_id"], "provider": top.get("provider"), "revision": top.get("revision"),
           "status": top.get("status"), "cost_basis": cost_basis,
           "qualification_evidence": f"{top['model_id']}@{top.get('revision')}/{role}#{top.get('corpus_version')}",
           "estimated_cost": (_money(top) if money_mode else _tokens(top)),
           "cost_currency": ((top.get("economics") or {}).get("currency") if money_mode else None),
           "reason": f"cheapest-eligible ({top.get('status')}, {cost_basis})",
           "fallback": ({"model_id": fb["model_id"], "revision": fb.get("revision"),
                         "provider": fb.get("provider"), "status": fb.get("status")} if fb else None)}
    if not money_mode:
        res["cost_warning"] = ("ранжирование в ТОКЕНАХ, не деньгах: не у всех кандидатов роли задан "
                               "total_cost_per_verified_change (нет тарифа) — порядок может не совпасть с деньгами")
    # v3.8.3 WRITER QUALITY-ESCALATION: money-mode берёт дешёвого (top), но при КАЧЕСТВЕННОМ провале
    # (impl_verification/code_review) fix-loop эскалирует writer'а на кандидата с ВЫШЕ observed success rate.
    # ЧЕСТНО (owner-review): это НАБЛЮДАЕМАЯ успешность на конкретном (малом) корпусе, НЕ универсальная «сила»
    # модели. Ладдер = кандидаты с higher_observed_success_rate, чем у top (сильнейший-по-наблюдению первым).
    def _succ(q):
        v = (q.get("metrics") or {}).get("success_rate")
        return v if isinstance(v, (int, float)) else 0.0
    _top_succ = _succ(top)
    _ladder = sorted([c for c in cands if c["model_id"] != top["model_id"] and _succ(c) > _top_succ],
                     key=_succ, reverse=True)
    res["escalation_ladder"] = [{"model_id": c["model_id"], "provider": c.get("provider"),
                                 "revision": c.get("revision"), "status": c.get("status"),
                                 "basis": "higher_observed_success_rate",
                                 "observed_success_rate": _succ(c),
                                 "corpus_version": c.get("corpus_version")} for c in _ladder]
    return res


ALL_ROLES = ("implementation", "code_review", "security_review", "integration_judge")


# v3.9.0 complexity-aware routing: классы задач, где дешёвый writer доказанно не тянет green одним проходом
# -> сразу сильный executor (Claude Code adapter), НЕ cheap-then-fix-loop.
_HEAVY_CLASSES = {"ENGINEERING", "PRODUCT", "AI_FEATURE", "CRITICAL", "RESEARCH"}


def writer_tier(signals=None):
    """v3.9.0: тир writer'а ПО КЛАССУ ЗАДАЧИ (не только по среднему success_rate модели). QUICK -> cheap-api
    (дешёвый qualified writer, money-mode). ENGINEERING/PRODUCT/AI_FEATURE/CRITICAL/RESEARCH или risk
    high/critical -> strong-executor (Claude Code adapter, provider=claude-cli) СРАЗУ: на доказанно сложном
    не тратим fix-loop на дешёвого. -> {tier, provider_hint, reason}. review->deepseek и strict-security->
    человек остаются как есть (это про WRITER-тир)."""
    s = dict(signals or {})
    tt = str(s.get("task_type") or "").upper()
    risk = str(s.get("risk") or "").lower()
    if tt in _HEAVY_CLASSES or risk in ("high", "critical"):
        return {"tier": "strong-executor", "provider_hint": "claude-cli",
                "reason": f"task_type={tt or '?'}/risk={risk or '?'} -> сильный writer сразу (без cheap-then-fix-loop)"}
    return {"tier": "cheap-api", "provider_hint": "money-mode",
            "reason": f"task_type={tt or 'QUICK'} -> дешёвый qualified writer (money-mode)"}


def plan_run(roles_cfg=None, quals=None, models=None, signals=None):
    """v3.7.12: резолв ВСЕХ рантайм-ролей одним вызовом -> bundle для RunReport.model_resolution.
    Делает решение роутера ВИДИМЫМ в отчёте прогона (writer/reviewer/security/integration независимо).
    writer≠judge по МОДЕЛИ: если code_review резолвится в модель != implementation — это разные identity.
    v3.9.0: + preferred_writer_tier (complexity-aware) — по классу задачи, если переданы signals."""
    if roles_cfg is None:
        roles_cfg, quals, models = _load()
    plan = {role: resolve(role, roles_cfg, quals, models) for role in ALL_ROLES}
    plan["preferred_writer_tier"] = writer_tier(signals)
    # v3.8.3 CONFLICT-AWARE writer≠judge: role_constraints (model-roles.yaml) вида
    # {security_review: {must_differ_from: implementation}} — если судья и writer сошлись в одной модели,
    # СУДЬЯ фиксирован (qualified), а WRITER перерезолвливается, ИСКЛЮЧАЯ модель судьи (дешёвый из оставшихся).
    # Так квалифицированный судья не судит собственное изменение, даже если он же — money-mode writer.
    _constraints = (roles_cfg.get("role_constraints") or {})
    plan["role_constraints_applied"] = []
    for _judge_role, _c in _constraints.items():
        _writer_role = (_c or {}).get("must_differ_from")
        jr, wr = plan.get(_judge_role), plan.get(_writer_role)
        if (_writer_role and jr and wr and jr.get("resolved") and wr.get("resolved")
                and jr.get("model_id") == wr.get("model_id")):
            _re = resolve(_writer_role, roles_cfg, quals, models, exclude_models={jr["model_id"]})
            plan[_writer_role] = _re
            plan["role_constraints_applied"].append(
                {"constraint": f"{_judge_role}.must_differ_from={_writer_role}",
                 "judge_fixed": jr["model_id"],
                 "writer_reresolved_to": _re.get("model_id") if _re.get("resolved") else None,
                 "writer_resolved": _re.get("resolved")})
    impl = plan["implementation"]
    rev = plan["code_review"]
    plan["writer_ne_judge_by_model"] = bool(impl.get("resolved") and rev.get("resolved")
                                            and impl.get("model_id") != rev.get("model_id"))
    # v3.8.3 обеспечивал writer≠judge ПРИ ПЛАНИРОВАНИИ. Но судья эскалируется на fallback-класс
    # во время прогона (escalation_decision), а писатель — по escalation_ladder; заново это не
    # сверялось = инвариант объявлен, но на пути эскалации не сторожится. Фиксируем исход проверки
    # независимости в плане (планировочное время), а вызывающий обязан перепроверить её после
    # КАЖДОЙ применённой эскалации через check_independence(plan, escalations=...) — fail-closed.
    plan["independence"] = check_independence(plan)
    return plan


def check_independence(plan, escalations=None):
    """Fail-closed страж инварианта writer≠judge, устойчивый к эскалации/fallback.

    plan_run переселяет писателя с модели судьи ПРИ ПЛАНИРОВАНИИ. Но во время прогона судья
    эскалируется на fallback-класс (escalation_decision, escalate_scope=review_only), а писатель —
    по escalation_ladder; ни то, ни другое повторно не сверяется, и судья может «съехать» на модель
    писателя незаметно. Эта функция берёт ФАКТИЧЕСКИ выбранные роли (с учётом переданных
    escalations — то есть по факту исполнения, а не только по плану) и проверяет, что модель писателя
    (implementation) не совпала с моделью НИ ОДНОГО судьи (все роли, кроме implementation; это и есть
    role_constraints.must_differ_from=implementation для строгих судей + эконом-судья code_review).

    escalations: {role: model_id} — на какие модели роли реально эскалировали в прогоне (или None
    при планировочной проверке). Возвращает результат; НЕ бросает — вызывающий обязан остановить
    прогон fail-closed при independent=False (ADR-004: cost_never_by_weakening_gates).
    """
    escalations = dict(escalations or {})

    def _model(role):
        return escalations.get(role) or (plan.get(role) or {}).get("model_id")

    writer = _model("implementation")
    violations = []
    for jr in (r for r in ALL_ROLES if r != "implementation"):
        jm = _model(jr)
        if writer and jm and writer == jm:
            violations.append({"judge_role": jr, "writer_role": "implementation", "model_id": jm})
    after_esc = bool(escalations)
    return {"kind": "IndependenceCheck", "independent": not violations,
            "violations": violations, "checked_after_escalation": after_esc,
            "writer_model": writer,
            "note": ("инвариант writer≠judge НАРУШЕН"
                     + (" после эскалации" if after_esc else " при планировании")
                     + ": судья и писатель на одной модели — прогон обязан остановиться fail-closed"
                     if violations else
                     "writer≠judge сохранён" + (" после эскалации" if after_esc else " при планировании"))}


def escalation_decision(role, attempt, signal, roles_cfg=None):
    """signal ∈ {ok, reviewer_abstain, schema_invalid, reviewer_uncertain}. -> действие.
    Targeted retry до max; затем эскалация ТОЛЬКО review/judge-вызова (не всей задачи)."""
    if roles_cfg is None:
        roles_cfg, _, _ = _load()
    esc = roles_cfg.get("escalation_policy") or {}
    if signal == "ok" or signal not in (esc.get("triggers") or []):
        return {"action": "proceed"}
    if attempt < int(esc.get("max_targeted_retries", 0)):
        return {"action": "retry", "attempt_next": attempt + 1, "scope": "same_model"}
    return {"action": "escalate", "scope": esc.get("escalate_scope", "review_only"),
            "note": "эскалируется только review/judge-вызов на fallback-класс, НЕ пере-прогон всей задачи"}


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    res = resolve(args[0])
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("resolved") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
