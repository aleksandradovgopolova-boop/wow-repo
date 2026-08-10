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

PKG = Path(__file__).resolve().parents[1]
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
    return plan


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


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    roles_cfg, quals, models = _load()

    # измеренный реестр (N6, 2026-07-28): три conditional вендора, ВСЕ priced -> MONEY-MODE. По деньгам/
    # изменение: deepseek-v4-flash $0.0115 < qwen $0.072 < kimi $0.467 -> preferred deepseek, fallback qwen.
    r_impl = resolve("implementation", roles_cfg, quals, models)
    expect("implementation -> resolved (writer допускает conditional)",
           r_impl["resolved"] and r_impl.get("model_id") and r_impl["reason"].startswith("cheapest-eligible"))
    expect("implementation cost_basis=money (все кандидаты с ценой) + без warning",
           r_impl["cost_basis"] == "money" and "cost_warning" not in r_impl)
    expect("implementation cheapest по ДЕНЬГАМ -> deepseek-v4-flash ($0.0115)",
           r_impl["model_id"] == "deepseek-v4-flash" and r_impl["provider"] == "deepseek")
    expect("implementation fallback -> qwen3-coder-plus (2-й по деньгам, $0.072)",
           (r_impl.get("fallback") or {}).get("model_id") == "qwen3-coder-plus")
    # v3.8.3 quality-escalation ladder: сильнее top по success_rate, сильнейший первым. top=deepseek
    # (success 0.667). Ладдер: kimi (1.0) > qwen (0.833). Оба сильнее -> оба в ладдере, kimi первым.
    _lad = r_impl.get("escalation_ladder") or []
    expect("escalation_ladder: выше observed success rate первым (kimi)",
           bool(_lad) and _lad[0]["model_id"] == "kimi-k2.7-code-highspeed"
           and _lad[0]["basis"] == "higher_observed_success_rate")
    expect("escalation_ladder: отсортирован по observed_success_rate DESC + несёт corpus_version",
           [x["observed_success_rate"] for x in _lad] == sorted([x["observed_success_rate"] for x in _lad], reverse=True)
           and all("corpus_version" in x for x in _lad))
    expect("escalation_ladder: только выше top (deepseek 0.667 не в ладдере)",
           all(x["model_id"] != "deepseek-v4-flash" for x in _lad)
           and all(x["observed_success_rate"] > 0.667 for x in _lad))

    # money-mode: у ВСЕХ кандидатов есть деньги -> сортировка по ДЕНЬГАМ, не токенам (доказ. тезиса)
    ms2 = {"a": {"classes": ["balanced"], "cost_class": "low"}, "b": {"classes": ["balanced"], "cost_class": "low"}}
    q_money = [
        {"role": "implementation", "status": "conditional", "model_id": "a", "provider": "pa", "revision": "a",
         "corpus_version": "t", "metrics": {"false_green": 0},
         "economics": {"tokens_per_verified_change": 50000, "total_cost_per_verified_change": 0.90}},   # мало токенов, ДОРОГО
        {"role": "implementation", "status": "conditional", "model_id": "b", "provider": "pb", "revision": "b",
         "corpus_version": "t", "metrics": {"false_green": 0},
         "economics": {"tokens_per_verified_change": 150000, "total_cost_per_verified_change": 0.07}}]  # много токенов, ДЁШЕВО
    rm = resolve("implementation", {"roles": {"implementation": {"preferred_class": "balanced"}}}, q_money, ms2)
    expect("money-mode: выбран дешёвый по ДЕНЬГАМ (b $0.07), а НЕ по токенам (a 50k)",
           rm["cost_basis"] == "money" and rm["model_id"] == "b" and "cost_warning" not in rm)

    r_sec = resolve("security_review", roles_cfg, quals, models)
    expect("security_review -> НЕ resolved (строгий судья требует qualified; conditional/пусто не годится)",
           r_sec["resolved"] is False and "escalation" in r_sec)

    # синтетика: строгий судья vs писатель при одном и том же conditional-кандидате в нужном классе
    ms = {"m-cond": {"classes": ["high-reasoning"], "cost_class": "low"}}
    q_cond = lambda role: [{"role": role, "status": "conditional", "model_id": "m-cond", "provider": "x",
                            "revision": "r", "corpus_version": "t", "metrics": {"false_green": 0, "cost_per_change": 1}}]
    rc_hr = lambda role: {"roles": {role: {"preferred_class": "high-reasoning", "fallback_class": "high-reasoning"}}}
    expect("строгий судья + conditional-в-классе -> всё равно НЕ resolved (qualified обязателен)",
           resolve("security_review", rc_hr("security_review"), q_cond("security_review"), ms)["resolved"] is False)
    expect("эконом-ревью (code_review) + conditional -> resolved",
           resolve("code_review", rc_hr("code_review"), q_cond("code_review"), ms)["resolved"] is True)
    q_fg = [{"role": "implementation", "status": "conditional", "model_id": "m-cond", "provider": "x",
             "revision": "r", "corpus_version": "t", "metrics": {"false_green": 1, "cost_per_change": 1}}]
    expect("false_green>0 -> НЕ resolved даже для writer (safety-first)",
           resolve("implementation", rc_hr("implementation"), q_fg, ms)["resolved"] is False)

    # синтетика: две qualified модели -> берётся дешевле + вторая в fallback
    q2 = [{"role": "implementation", "status": "qualified", "model_id": "kimi-k3", "provider": "kimi",
           "revision": "kimi-k3", "corpus_version": "t", "metrics": {"false_green": 0, "cost_per_change": 1.4}},
          {"role": "implementation", "status": "qualified", "model_id": "kimi-k2.7-code-highspeed",
           "provider": "kimi", "revision": "hs", "corpus_version": "t", "metrics": {"false_green": 0, "cost_per_change": 0.9}}]
    rc = {"roles": {"implementation": {"preferred_class": "balanced", "fallback_class": "high-reasoning"}},
          "escalation_policy": {"triggers": ["reviewer_abstain"], "max_targeted_retries": 1, "escalate_scope": "review_only"}}
    r = resolve("implementation", rc, q2, models)
    expect("две qualified -> cheapest + fallback вторая",
           r["model_id"] == "kimi-k2.7-code-highspeed" and r["fallback"]["model_id"] == "kimi-k3")

    # escalation: abstain -> retry -> escalate review_only
    expect("abstain, attempt0, max1 -> retry", escalation_decision("code_review", 0, "reviewer_abstain", roles_cfg)["action"] == "retry")
    d = escalation_decision("code_review", 1, "reviewer_abstain", roles_cfg)
    expect("abstain после ретраев -> escalate review_only (не вся задача)",
           d["action"] == "escalate" and d["scope"] == "review_only")
    expect("ok -> proceed", escalation_decision("code_review", 0, "ok", roles_cfg)["action"] == "proceed")

    # plan_run: bundle всех ролей для RunReport
    plan = plan_run(roles_cfg, quals, models)
    expect("plan_run несёт все 4 роли", all(r in plan for r in ALL_ROLES))
    expect("plan_run: implementation resolved, security_review НЕ resolved",
           plan["implementation"]["resolved"] is True and plan["security_review"]["resolved"] is False)

    # v3.8.3 CONFLICT-AWARE writer≠judge: судья и writer сошлись -> writer перерезолвлен без модели судьи
    _rc = {"roles": {"implementation": {"preferred_class": "balanced"}, "security_review": {"preferred_class": "balanced"}},
           "role_constraints": {"security_review": {"must_differ_from": "implementation"}}}
    _ms = {"J": {"classes": ["balanced"], "cost_class": "low"}, "W": {"classes": ["balanced"], "cost_class": "mid"}}
    _ec = lambda c: {"input_tokens_per_change": 1, "output_tokens_per_change": 1, "tokens_per_verified_change": 1,
                     "input_price_per_mtok": c, "output_price_per_mtok": c, "currency": "USD",
                     "price_snapshot_at": "2026-07-30", "price_source": "test", "total_cost_per_verified_change": 2 * c / 1e6}
    _q = [  # J дешевле и qualified и для security_review, и для implementation -> конфликт
        {"role": "security_review", "status": "qualified", "model_id": "J", "provider": "p", "revision": "J",
         "corpus_version": "c", "metrics": {"false_green": 0}, "economics": _ec(0.1)},
        {"role": "implementation", "status": "qualified", "model_id": "J", "provider": "p", "revision": "J",
         "corpus_version": "c", "metrics": {"false_green": 0, "success_rate": 0.9}, "economics": _ec(0.1)},
        {"role": "implementation", "status": "conditional", "model_id": "W", "provider": "p", "revision": "W",
         "corpus_version": "c", "metrics": {"false_green": 0, "success_rate": 0.6}, "economics": _ec(1.0)},
    ]
    _p2 = plan_run(_rc, _q, _ms)
    expect("conflict-aware: security_review судья фиксирован = J",
           _p2["security_review"]["resolved"] and _p2["security_review"]["model_id"] == "J")
    expect("conflict-aware: implementation перерезолвлен НЕ в J (writer≠judge) -> W",
           _p2["implementation"]["resolved"] and _p2["implementation"]["model_id"] == "W")
    expect("conflict-aware: применение записано + writer_ne_judge",
           bool(_p2.get("role_constraints_applied")) and _p2["implementation"]["model_id"] != _p2["security_review"]["model_id"])

    # v3.9.0 complexity-aware routing: тир writer'а по классу задачи
    expect("writer_tier QUICK -> cheap-api (money-mode)",
           writer_tier({"task_type": "QUICK"})["tier"] == "cheap-api")
    expect("writer_tier ENGINEERING -> strong-executor (claude-cli) сразу",
           writer_tier({"task_type": "ENGINEERING"})["tier"] == "strong-executor"
           and writer_tier({"task_type": "ENGINEERING"})["provider_hint"] == "claude-cli")
    expect("writer_tier PRODUCT -> strong-executor",
           writer_tier({"task_type": "PRODUCT"})["tier"] == "strong-executor")
    expect("writer_tier QUICK+risk critical -> strong-executor (risk override)",
           writer_tier({"task_type": "QUICK", "risk": "critical"})["tier"] == "strong-executor")
    expect("plan_run со signals несёт preferred_writer_tier",
           isinstance(plan_run(roles_cfg, quals, models, signals={"task_type": "ENGINEERING"}).get("preferred_writer_tier"), dict)
           and plan_run(roles_cfg, quals, models, signals={"task_type": "ENGINEERING"})["preferred_writer_tier"]["tier"] == "strong-executor")
    print("model_router selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    res = resolve(args[0])
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("resolved") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
