#!/usr/bin/env python3
"""economic_preflight.py (v3.21.0 Engineering Operating Model, WP7) — экономическая граница ДО траты.

Аудит. Экономика кита была устроена так, что деньги узнавались ПОСЛЕ того, как их потратили:
  * `ai_ops_kit/gates/preflight.py` проверял бюджет только КОНТЕКСТНЫЙ (влезает ли задача в окно), не денежный;
  * `Budget.charge_call` рвался уже ВНУТРИ прогона — на N-м вызове, когда N-1 уже оплачены;
  * `ai_ops_kit/providers/cost_account.py` сверял spent vs limit ПОСЛЕ прогона — это аудит, а не граница.
Дорогая задача узнавалась по факту. Здесь — оценка стоимости ДО первого вызова модели, по фактической
истории ЭТОГО репозитория (`usage_ledger`), и вердикт до траты.

ЧЕСТНОСТЬ (главное в модуле, важнее самой оценки):
  * нет истории -> статус `unavailable`, НИКОГДА 0. Ноль означал бы «задача бесплатна» — это ложь,
    и на такой лжи граница пропустила бы что угодно;
  * задача, в которой хоть один вызов записан с `cost_status: unavailable`, даёт НИЖНЮЮ ГРАНИЦУ, а не
    стоимость. Считать неизвестное нулём и складывать — систематически недооценивать; такая оценка
    помечается `estimated_lower_bound`, и вердикт прямо сообщает, что граница может НЕ сработать;
  * никаких выдуманных коэффициентов «сильный writer в 3 раза дороже»: в UsageRecord нет ни класса
    задачи, ни tier'а, значит разложить историю по ним НЕЧЕМ, и модуль этого не изображает. Он отдаёт
    наблюдённое распределение (min/median/max) и размер выборки, а решение принимает по ХУДШЕМУ.

Сила. Блокирует только когда ПЕССИМИСТИЧНАЯ оценка (самый дорогой сравнимый прогон) превышает жёсткий
лимит: это единственный случай, где мы что-то действительно знаем. Дорого-но-в-пределах -> запрос
подтверждения. Нет истории -> НЕ блокируем (иначе первый прогон в любом репозитории невозможен),
но говорим об этом прямо.

CLI:  economic_preflight.py <root> [--task-type ENGINEERING] [--max-cost 5] [--max-calls 40] [--json]
      economic_preflight.py --selftest
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

import yaml

try:
    from ai_ops_kit.shared import usage_ledger
except ImportError:  # pragma: no cover — путь подмешивает вызывающий
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ai_ops_kit.shared import usage_ledger

ESTIMATE_STATUS = ("measured_history", "estimated_lower_bound", "unavailable")
VERDICTS = ("proceed", "proceed_unknown", "confirm_required", "block")

DEFAULTS = {
    "enforce": "advise",             # advise | block — сила МЯГКИХ правил (жёсткий лимит блокирует всегда)
    "require_estimate": False,       # True -> отсутствие истории само по себе блокирует
    "confirm_over_cost_usd": 5,      # дороже — просить подтверждение человека ДО прогона
    "min_history_tasks": 3,          # с этого размера выборки оценка считается сравнимой
}

# Лимиты берутся из RunPlan.execution_budget; здесь только их имена (не выдумываем значений).
_LIMIT_KEYS = ("max_cost", "max_model_calls", "max_duration")


def _task_totals(records):
    """Сгруппировать UsageRecords по задаче. -> [{wid, cost, calls, complete}].

    `complete=False` означает: в задаче есть вызовы с неизвестной стоимостью, поэтому сумма —
    НИЖНЯЯ ГРАНИЦА, а не стоимость. Неизвестное НЕ складывается как ноль.
    """
    by_wid = {}
    for r in records or ():
        wid = r.get("workitem_id") or r.get("run_id") or "—"
        agg = by_wid.setdefault(wid, {"wid": wid, "cost": 0.0, "calls": 0, "complete": True})
        agg["calls"] += 1
        if r.get("cost_status") == "measured" and isinstance(r.get("cost"), (int, float)):
            agg["cost"] += float(r["cost"])
        else:
            agg["complete"] = False        # неизвестную стоимость нулём не считаем
    return [by_wid[k] for k in sorted(by_wid)]


def estimate(child_root=None, records=None):
    """Оценка стоимости следующей задачи по истории репозитория. -> EconomicEstimate (dict)."""
    if records is None:
        try:
            records = usage_ledger.load_product(child_root or ".")
        except Exception:  # noqa: BLE001 — недоступный ledger != бесплатная задача
            records = []
    totals = _task_totals(records)
    complete = [t for t in totals if t["complete"] and t["cost"] > 0]
    partial = [t for t in totals if not t["complete"]]

    if complete:
        status = "measured_history"
        sample = complete
    elif partial:
        status = "estimated_lower_bound"
        sample = partial
    else:
        return {
            "kind": "EconomicEstimate", "status": "unavailable",
            "cost_median": None, "cost_max": None, "cost_min": None,
            "calls_median": None, "calls_max": None,
            "sample_tasks": 0, "partial_tasks": len(partial), "confidence": "none",
            "note": "истории прогонов в этом репозитории нет — предсказать стоимость НЕЧЕМ "
                    "(unavailable, не ноль). После первого прогона оценка появится.",
        }

    costs = sorted(t["cost"] for t in sample)
    calls = sorted(t["calls"] for t in sample)
    n = len(sample)
    confidence = "medium" if n >= DEFAULTS["min_history_tasks"] else "low"
    note = (f"по {n} завершённым задачам этого репозитория"
            if status == "measured_history" else
            f"по {n} задачам, где часть вызовов записана с неизвестной стоимостью: это НИЖНЯЯ "
            f"ГРАНИЦА, фактическая стоимость выше — граница может НЕ сработать")
    return {
        "kind": "EconomicEstimate", "status": status,
        "cost_median": round(statistics.median(costs), 6),
        "cost_max": round(costs[-1], 6), "cost_min": round(costs[0], 6),
        "calls_median": int(statistics.median(calls)), "calls_max": calls[-1],
        "sample_tasks": n, "partial_tasks": len(partial), "confidence": confidence, "note": note,
    }


def check_economics(est, limits=None, policy=None):
    """Вердикт ДО первого вызова модели. -> EconomicVerdict (dict).

    Блок только по ПЕССИМИСТИЧНОЙ оценке против жёсткого лимита — единственный случай, где мы
    действительно что-то знаем. Остальное — подтверждение или предупреждение.
    """
    pol = dict(DEFAULTS)
    pol.update({k: v for k, v in (policy or {}).items() if k in DEFAULTS})
    lim = {k: (limits or {}).get(k) for k in _LIMIT_KEYS}
    hard, soft = [], []
    verdict = "proceed"

    if est["status"] == "unavailable":
        if pol["require_estimate"]:
            hard.append({"rule": "estimate_required",
                         "detail": "политика требует оценку до прогона (require_estimate), а истории "
                                   "в репозитории нет — прогон заблокирован осознанно"})
            verdict = "block"
        else:
            soft.append({"rule": "estimate_unavailable", "detail": est["note"]})
            verdict = "proceed_unknown"
        return {"kind": "EconomicVerdict", "allowed": verdict != "block", "verdict": verdict,
                "estimate_status": est["status"], "limits": lim,
                "violations": hard, "advisories": soft, "enforce": pol["enforce"]}

    # жёсткий лимит против ХУДШЕГО сравнимого прогона
    if lim["max_cost"] is not None and est["cost_max"] is not None and est["cost_max"] > lim["max_cost"]:
        hard.append({"rule": "cost_limit_exceeded",
                     "detail": f"худший сравнимый прогон стоил ${est['cost_max']} при лимите "
                               f"${lim['max_cost']} — трата началась бы и была бы прервана посередине"})
        verdict = "block"
    if lim["max_model_calls"] is not None and est["calls_max"] is not None \
            and est["calls_max"] > lim["max_model_calls"]:
        hard.append({"rule": "calls_limit_exceeded",
                     "detail": f"худший сравнимый прогон занял {est['calls_max']} вызовов при лимите "
                               f"{lim['max_model_calls']}"})
        verdict = "block"

    if est["status"] == "estimated_lower_bound":
        soft.append({"rule": "lower_bound_only",
                     "detail": est["note"] + " — вердикт мягче, чем следовало бы"})

    if verdict != "block":
        thr = pol["confirm_over_cost_usd"]
        if thr is not None and est["cost_median"] is not None and est["cost_median"] > thr:
            soft.append({"rule": "confirm_recommended",
                         "detail": f"медианная стоимость сравнимой задачи ${est['cost_median']} выше "
                                   f"порога подтверждения ${thr} — подтвердите ДО прогона"})
            verdict = "confirm_required"
        if est["confidence"] == "low":
            soft.append({"rule": "low_confidence",
                         "detail": f"выборка {est['sample_tasks']} задач меньше "
                                   f"{pol['min_history_tasks']} — оценка ориентировочная"})

    allowed = not hard and not (pol["enforce"] == "block" and verdict == "confirm_required")
    return {"kind": "EconomicVerdict", "allowed": allowed, "verdict": verdict,
            "estimate_status": est["status"], "limits": lim,
            "violations": hard, "advisories": soft, "enforce": pol["enforce"]}


def policy_from_config(root):
    """`engineering_operating_model.economics` из .ai-ops.yaml. Нет/битый -> {}."""
    for name in (".ai-ops.yaml", ".ai-ops.yml"):
        cfg = Path(root) / name
        if not cfg.is_file():
            continue
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            return {}
        e = (data.get("engineering_operating_model") or {}).get("economics") or {}
        return e if isinstance(e, dict) else {}
    return {}


def assess(child_root, limits=None):
    """Оценка + вердикт по политике репозитория."""
    est = estimate(child_root)
    return est, check_economics(est, limits, policy_from_config(child_root))


def _fmt(est, v):
    lines = [f"экономика ДО прогона: {v['verdict']} (оценка: {est['status']}, enforce={v['enforce']})"]
    if est["status"] == "unavailable":
        lines.append(f"  стоимость: unavailable — {est['note']}")
    else:
        lines.append(f"  стоимость сравнимой задачи: медиана ${est['cost_median']}, "
                     f"худшая ${est['cost_max']}, выборка {est['sample_tasks']} "
                     f"(уверенность {est['confidence']})")
        lines.append(f"  вызовов: медиана {est['calls_median']}, худший {est['calls_max']}")
    for x in v["violations"]:
        lines.append(f"  ✗ {x['rule']}: {x['detail']}")
    for x in v["advisories"]:
        lines.append(f"  ⚠ {x['rule']}: {x['detail']}")
    return "\n".join(lines)


def summary_line(child_root):
    """Строка для doctor. unavailable не выдаём за ноль."""
    try:
        est = estimate(child_root)
    except Exception as e:  # noqa: BLE001 — оценка не роняет doctor
        return f"экономика (оценка до прогона): недоступно ({e})"
    if est["status"] == "unavailable":
        return ("экономика (оценка до прогона): unavailable — истории прогонов нет "
                "(не ноль; появится после первого прогона)")
    tail = " ⚠ нижняя граница" if est["status"] == "estimated_lower_bound" else ""
    return (f"экономика (оценка до прогона): медиана ${est['cost_median']}, худшая ${est['cost_max']} "
            f"по {est['sample_tasks']} задачам{tail}")


def _rec(wid, cost, status="measured"):
    return {"workitem_id": wid, "cost": cost, "cost_status": status}


def main(argv):
    root = argv[0] if argv and not argv[0].startswith("--") else "."
    limits = {}
    it = iter(argv)
    for a in it:
        if a == "--max-cost":
            limits["max_cost"] = float(next(it, 0) or 0)
        elif a == "--max-calls":
            limits["max_model_calls"] = int(next(it, 0) or 0)
    est, v = assess(root, limits or None)
    print(json.dumps({"estimate": est, "verdict": v}, ensure_ascii=False, indent=2)
          if "--json" in argv else _fmt(est, v))
    return 0 if v["allowed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
