#!/usr/bin/env python3
"""cost_account.py (v3.4.4) — сверка расхода (Trace v0.2 run_cost) с BudgetContract по scope.

Замыкает экономику v3.4: BudgetContract объявляет границы (v3.4.0), run_cost (Trace v0.2) их меряет —
cost_account сверяет spent vs limit по каждому измерению и выдаёт verdict. Делает бюджет audit'уемым
и готовит агрегацию для model-comparison (v3.4.5).

Маппинг измерений (run_cost -> budget limit):
  calls                         -> max_model_calls
  input_tokens + output_tokens  -> max_tokens
  cost_usd_est                  -> max_cost_usd
  latency_s                     -> max_wall_seconds
  iterations (внешний параметр) -> max_iterations

Честность (как budget.py): max_cost_usd осмыслен только если провайдер вернул стоимость; если
cost_usd_est=None — измерение помечается measured=false и по нему НЕ выносится over/exhausted.

CLI:  cost_account.py <budget.(yaml|json)> <run_cost.(json|yaml)> [--iterations N] [--json]
      cost_account.py --selftest
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
_MAP = {
    "max_model_calls": "calls",
    "max_tokens": "_tokens",
    "max_cost_usd": "cost_usd_est",
    "max_wall_seconds": "latency_s",
    "max_iterations": "_iterations",
}


def _measured(run_cost: dict, iterations):
    tokens = None
    it = run_cost.get("input_tokens")
    ot = run_cost.get("output_tokens")
    if it is not None or ot is not None:
        tokens = (it or 0) + (ot or 0)
    return {"calls": run_cost.get("calls"), "_tokens": tokens,
            "cost_usd_est": run_cost.get("cost_usd_est"), "latency_s": run_cost.get("latency_s"),
            "_iterations": iterations}


def reconcile(budget: dict, run_cost: dict, iterations=None) -> dict:
    limits = budget.get("limits") or {}
    spent = _measured(run_cost or {}, iterations)
    dims = {}
    over_any = exhausted_any = False
    for dim, limit in limits.items():
        if limit is None:
            continue
        sp = spent.get(_MAP.get(dim))
        if sp is None:
            dims[dim] = {"limit": limit, "spent": None, "measured": False}
            continue
        ex = sp >= limit
        ov = sp > limit
        over_any = over_any or ov
        exhausted_any = exhausted_any or ex
        dims[dim] = {"limit": limit, "spent": sp, "remaining": round(limit - sp, 6),
                     "exhausted": ex, "over": ov, "measured": True}
    verdict = "over" if over_any else ("exhausted" if exhausted_any else "within_budget")
    return {"kind": "cost-account", "budget": budget.get("id"), "scope": budget.get("scope"),
            "scope_ref": budget.get("scope_ref"), "hard": budget.get("hard"),
            "on_exhaustion": budget.get("on_exhaustion"), "verdict": verdict, "dimensions": dims}


def cost_per_successful_change(attempt: dict) -> dict:
    """v3.7 (ADR-004): стоимость ОДНОГО успешно доставленного и ПРОВЕРЕННОГО изменения.
    Дешёвая модель бывает дорогой: повторы, битый structured output, длинные fix-loop, эскалации,
    ручное вмешательство. attempt: {calls_cost, retry_cost, reviewer_cost, escalation_cost, latency_s,
    manual_interventions, delivered_verified: bool}. Не доставлено+проверено -> cost_per_change=None
    (стоимость без результата = чистые потери, не «дёшево»)."""
    total = round(sum(float(attempt.get(k, 0) or 0)
                      for k in ("calls_cost", "retry_cost", "reviewer_cost", "escalation_cost")), 6)
    delivered = bool(attempt.get("delivered_verified"))
    return {"total_cost": total, "delivered_verified": delivered,
            "cost_per_change": (total if delivered else None),
            "latency_s": attempt.get("latency_s"), "manual_interventions": attempt.get("manual_interventions", 0),
            "note": ("успешное проверенное изменение" if delivered
                     else "нет успешного проверенного изменения -> стоимость = потери (не экономия)")}


def human_attention_cost(root=None) -> "float | None":
    """Ставка внимания человека ($/вмешательство) из дочернего `.ai-ops.yaml`; не задана -> None.

    Внимание человека — org-специфичная цена (сколько стоит один раз оторвать человека на ревью/
    подтверждение/разбор), поэтому живёт в конфиге ПРОДУКТА, а не в ките. Ключ:
    `engineering_operating_model.economics.human_attention_cost_per_intervention_usd`. None означает
    «не оценено», НЕ «бесплатно»: нагруженная стоимость тогда честно помечается нижней границей.
    """
    if root is None:
        return None
    for name in (".ai-ops.yaml", ".ai-ops.yml"):
        p = Path(root) / name
        if not p.is_file():
            continue
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return None
        econ = ((data.get("engineering_operating_model") or {}).get("economics") or {})
        rate = econ.get("human_attention_cost_per_intervention_usd")
        return float(rate) if isinstance(rate, (int, float)) and not isinstance(rate, bool) else None
    return None


def cost_per_successful_outcome(attempt: dict, *, human_attention_cost_usd=None) -> dict:
    """Нагруженная стоимость успешного ИСХОДА: AI-стоимость + внимание человека (#637).

    Расширяет `cost_per_successful_change`: к четырём монетизированным AI-компонентам добавляется
    внимание человека (`manual_interventions` * ставка). Главный экономический KPI кита — не
    token cost, а стоимость доставленного исхода, включающая человека.

    Честность как в usage_ledger (неизвестное = unavailable, НИКОГДА 0):
      * ставка не задана ИЛИ число вмешательств неизвестно -> человеческий компонент `None`
        (unavailable), нагруженная стоимость помечена нижней границей (`lower_bound=True`),
        но AI-часть не обнуляется и не выдаётся за полную;
      * инвариант «провал = чистые потери» сохранён: не доставлено+проверено -> `cost_per_outcome=None`.
    latency несётся сырьём у AI-метрики (наблюдаемость), в деньги здесь НЕ конвертируется —
    машинное время стены не равно вниманию человека.
    """
    ai = cost_per_successful_change(attempt)
    ai_total = ai["total_cost"]
    mi = attempt.get("manual_interventions")
    mi = mi if isinstance(mi, int) and not isinstance(mi, bool) and mi >= 0 else None
    rate = human_attention_cost_usd
    rate = float(rate) if isinstance(rate, (int, float)) and not isinstance(rate, bool) and rate >= 0 else None
    human = round(mi * rate, 6) if (mi is not None and rate is not None) else None
    complete = human is not None
    loaded = round(ai_total + (human or 0.0), 6)
    delivered = bool(attempt.get("delivered_verified"))
    if not delivered:
        note = "нет успешного проверенного исхода -> нагруженная стоимость = потери (не экономия)"
    elif complete:
        note = f"нагруженная стоимость исхода: AI {ai_total:g}$ + внимание человека {human:g}$"
    else:
        why = "ставка внимания не задана" if rate is None else "число вмешательств неизвестно"
        note = f"нижняя граница (только AI {ai_total:g}$): {why} -> внимание человека unavailable, не 0"
    return {"ai_cost": ai_total, "human_attention_cost": human,
            "manual_interventions": mi, "loaded_cost": loaded,
            "complete": complete, "lower_bound": not complete,
            "delivered_verified": delivered,
            "cost_per_outcome": (loaded if delivered else None), "note": note}


def roid_outcome_report(cost, latency, manual_interventions, delivered, root=None) -> dict:
    """Готовая секция `rep["roid_outcome"]` для отчёта прогона (#637) — вся сборка в providers.

    Тонкая обёртка: собирает attempt из данных прогона, берёт ставку внимания из дочернего конфига
    и возвращает уже готовый для отчёта dict. Держит логику экономики в cost_account, а не в
    движке прогона (монолит lifecycle не растёт содержательно)."""
    o = cost_per_successful_outcome(
        {"calls_cost": cost, "latency_s": latency,
         "manual_interventions": manual_interventions, "delivered_verified": bool(delivered)},
        human_attention_cost_usd=human_attention_cost(root))
    return {"cost_per_successful_outcome": o["cost_per_outcome"], "loaded_cost": o["loaded_cost"],
            "ai_cost": o["ai_cost"], "human_attention_cost": o["human_attention_cost"],
            "complete": o["complete"], "lower_bound": o["lower_bound"], "note": o["note"]}


def estimate_vs_actual(estimate: dict, actual) -> dict:
    """Пост-фактум: «работа стоила $X вместо ожидаемых $Y» — оценка ДО прогона против факта (#637).

    estimate — из `economic_preflight` (`checks["economic_budget"]`): {estimate_status, cost_median,
    cost_max}. actual — фактическая стоимость прогона: dict `rep["cost"]` (берётся `cost_usd_est`)
    ЛИБО число. Знак дельты: факт минус ожидание (median), положительная -> дороже ожидаемого.

    Честность: оценки не было (`estimate_status=unavailable` / нет числа) ИЛИ факт неизвестен ->
    `delta_usd=None`, статус `unavailable`, НЕ 0. Дельту поверх неполных данных не выдаём за факт.
    """
    est = None
    if (estimate or {}).get("estimate_status") not in (None, "unavailable"):
        m = (estimate or {}).get("cost_median")
        est = float(m) if isinstance(m, (int, float)) and not isinstance(m, bool) else None
    act = actual.get("cost_usd_est") if isinstance(actual, dict) else actual
    act = float(act) if isinstance(act, (int, float)) and not isinstance(act, bool) else None
    if est is None or act is None:
        why = "оценки до прогона не было (нет истории)" if est is None else "факт неизвестен"
        return {"estimate_usd": est, "actual_usd": act, "delta_usd": None, "delta_pct": None,
                "status": "unavailable", "note": f"дельта не считается: {why} — unavailable, не 0"}
    delta = round(act - est, 6)
    pct = round(delta / est * 100, 1) if est else None
    cmax = (estimate or {}).get("cost_max")
    cmax = float(cmax) if isinstance(cmax, (int, float)) and not isinstance(cmax, bool) else None
    over_max = bool(cmax is not None and act > cmax)
    note = (f"работа стоила ${act:g} вместо ожидаемых ${est:g} "
            f"(дельта {'+' if delta >= 0 else ''}{delta:g}$"
            f"{f', {pct:+g}%' if pct is not None else ''})"
            + ("; выше ожидаемого максимума" if over_max else ""))
    return {"estimate_usd": est, "actual_usd": act, "delta_usd": delta, "delta_pct": pct,
            "cost_max_usd": cmax, "over_max": over_max, "status": "measured", "note": note}


def compare_configs(configs) -> dict:
    """Сравнить конфигурации (reference vs economical): ранжировать ТОЛЬКО доставившие+проверенные по
    cost_per_change (безопасность важнее экономии — не-доставившие исключаются, не считаются «дешёвыми»).
    configs: [{name, attempt}]. -> {ranking, cheapest_qualified, excluded}."""
    rows = [{"name": c.get("name"), **cost_per_successful_change(c.get("attempt") or {})} for c in configs]
    qualified = sorted((r for r in rows if r["delivered_verified"]), key=lambda r: r["cost_per_change"])
    excluded = [r["name"] for r in rows if not r["delivered_verified"]]
    return {"ranking": qualified, "cheapest_qualified": (qualified[0]["name"] if qualified else None),
            "excluded_no_verified_change": excluded}


def _load(p: Path):
    t = p.read_text(encoding="utf-8")
    return yaml.safe_load(t) if p.suffix in (".yaml", ".yml") else json.loads(t)


def main(argv):
    args = [a for a in argv if not a.startswith("-")]
    if len(args) < 2:
        print(__doc__)
        return 1
    iterations = None
    if "--iterations" in argv:
        iterations = int(argv[argv.index("--iterations") + 1])
    rep = reconcile(_load(Path(args[0])), _load(Path(args[1])), iterations)
    if "--json" in argv:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(f"COST-ACCOUNT [{rep['scope']}:{rep['scope_ref']}] verdict={rep['verdict']} "
              f"(on_exhaustion={rep['on_exhaustion']})")
        for d, v in rep["dimensions"].items():
            if v.get("measured"):
                print(f"  {d}: {v['spent']}/{v['limit']} remaining={v['remaining']} "
                      f"{'OVER' if v['over'] else ('EXHAUSTED' if v['exhausted'] else 'ok')}")
            else:
                print(f"  {d}: не измерено (measured=false)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
