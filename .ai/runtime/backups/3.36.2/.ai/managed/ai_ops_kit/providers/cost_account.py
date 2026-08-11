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
