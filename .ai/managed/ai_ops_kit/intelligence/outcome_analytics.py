#!/usr/bin/env python3
"""Outcome Analytics — сводная аналитика результатов прогонов.

Объединяет данные из:
- effect_metrics.py (метрики эффекта: дни в работе, стадии, PROBLEM-rate)
- usage_ledger.py (затраты: токены, стоимость, латентность)

Формирует отчёт:
1. Общая стоимость (токены, деньги) за период
2. Средняя стоимость задачи
3. Эффективность (сравнение с baseline, если есть)
4. Топ задач по стоимости
5. Распределение по провайдерам/моделям

Использование:
    outcome_analytics.py <child_root> [--period 7d|30d|all] [--json]
    outcome_analytics.py --selftest

Возврат 0 — успех (отчёт — данные, решение за людьми).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path


def _load_usage_ledger(root: Path) -> list[dict]:
    """Load all usage records from product ledger."""
    ledger_path = root / ".ai" / "usage" / "product-ledger.jsonl"
    if not ledger_path.exists():
        return []

    records = []
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _load_effect_metrics(root: Path) -> dict:
    """Load effect metrics from history."""
    hist_dir = root / ".ai" / "project" / "report-history"
    if not hist_dir.exists():
        return {"features": 0, "runs": 0}

    # Import effect_metrics module
    try:
        from ai_ops_kit.intelligence import effect_metrics
        result = effect_metrics.build(hist_dir)
        return result
    # Узкий тип: модуль может не импортироваться, история — не читаться, запись — быть битой.
    # Причина НАЗЫВАЕТСЯ; голое "effect_metrics failed" не давало понять, чинить установку или
    # данные. ГРАНИЦА, КОТОРУЮ НАДО ЗНАТЬ: `features`/`runs` здесь остаются нулями, то есть сбор
    # неотличим от «ничего не было» по самим числам — читать `error` обязательно.
    except (ImportError, OSError, ValueError, KeyError, TypeError) as e:
        return {"features": 0, "runs": 0,
                "error": f"метрики эффекта не собраны ({type(e).__name__}: {e})"}


def _filter_by_period(records: list[dict], period: str) -> list[dict]:
    """Filter records by time period."""
    if period == "all":
        return records

    # Parse period (7d, 30d, etc.)
    try:
        days = int(period.rstrip("d"))
        cutoff = datetime.now() - timedelta(days=days)
    except (ValueError, AttributeError):
        return records

    filtered = []
    for r in records:
        ts = r.get("timestamp") or r.get("ts")
        if not ts:
            continue
        try:
            # Try ISO format
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.replace(tzinfo=None) >= cutoff:
                filtered.append(r)
        except (ValueError, AttributeError):
            continue
    return filtered


def _aggregate_costs(records: list[dict]) -> dict:
    """Aggregate cost metrics from usage records."""
    total_cost = 0.0
    total_input_tokens = 0
    total_output_tokens = 0
    measured_costs = 0
    measured_tokens = 0

    for r in records:
        cost = r.get("cost")
        cost_status = r.get("cost_status")
        input_tokens = r.get("input_tokens")
        output_tokens = r.get("output_tokens")
        usage_status = r.get("usage_status")

        if cost is not None and cost_status == "measured":
            total_cost += float(cost)
            measured_costs += 1

        if usage_status == "measured":
            if input_tokens is not None:
                total_input_tokens += int(input_tokens)
                measured_tokens += 1
            if output_tokens is not None:
                total_output_tokens += int(output_tokens)

    return {
        "total_cost_usd": round(total_cost, 4),
        "measured_costs": measured_costs,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_tokens": total_input_tokens + total_output_tokens,
        "measured_tokens": measured_tokens,
    }


def _top_tasks(records: list[dict], limit: int = 5) -> list[dict]:
    """Get top tasks by cost."""
    task_costs = {}
    for r in records:
        wid = r.get("workitem_id") or "unknown"
        cost = r.get("cost") or 0
        task_costs[wid] = task_costs.get(wid, 0) + float(cost)

    sorted_tasks = sorted(task_costs.items(), key=lambda x: x[1], reverse=True)
    return [{"workitem_id": wid, "cost_usd": round(cost, 4)} for wid, cost in sorted_tasks[:limit]]


def _provider_distribution(records: list[dict]) -> dict:
    """Get cost distribution by provider."""
    providers = {}
    for r in records:
        provider = r.get("provider") or "unknown"
        cost = r.get("cost") or 0
        providers[provider] = providers.get(provider, 0) + float(cost)

    return {k: round(v, 4) for k, v in sorted(providers.items(), key=lambda x: x[1], reverse=True)}


def _model_distribution(records: list[dict]) -> dict:
    """Get cost distribution by model."""
    models = {}
    for r in records:
        model = r.get("model") or "unknown"
        cost = r.get("cost") or 0
        models[model] = models.get(model, 0) + float(cost)

    return {k: round(v, 4) for k, v in sorted(models.items(), key=lambda x: x[1], reverse=True)}


def collect_analytics(root: Path, period: str = "all") -> dict:
    """Collect all outcome analytics."""
    records = _load_usage_ledger(root)
    records = _filter_by_period(records, period)

    effect = _load_effect_metrics(root)
    costs = _aggregate_costs(records)
    top_tasks = _top_tasks(records)
    providers = _provider_distribution(records)
    models = _model_distribution(records)

    # Calculate averages
    num_tasks = len(set(r.get("workitem_id") for r in records if r.get("workitem_id")))
    avg_cost = costs["total_cost_usd"] / num_tasks if num_tasks > 0 else 0

    return {
        "schema_version": 1,
        "kind": "outcome-analytics",
        "period": period,
        "generated_at": datetime.now().isoformat(),
        "summary": {
            "total_tasks": num_tasks,
            "total_runs": len(records),
            "total_cost_usd": costs["total_cost_usd"],
            "avg_cost_per_task_usd": round(avg_cost, 4),
            "total_tokens": costs["total_tokens"],
        },
        "effect_metrics": {
            "features": effect.get("features", 0),
            "runs": effect.get("runs", 0),
            "problem_rate": effect.get("problem_rate"),
        },
        "cost_breakdown": costs,
        "top_tasks_by_cost": top_tasks,
        "cost_by_provider": providers,
        "cost_by_model": models,
    }


def format_report(analytics: dict) -> str:
    """Format analytics into human-readable report."""
    lines = []
    summary = analytics.get("summary", {})

    lines.append("# Outcome Analytics Report\n")
    lines.append(f"**Period:** {analytics.get('period', 'all')}\n")
    lines.append(f"**Generated:** {analytics.get('generated_at', '?')}\n")

    lines.append("\n## Summary\n")
    lines.append(f"- **Total tasks:** {summary.get('total_tasks', 0)}")
    lines.append(f"- **Total runs:** {summary.get('total_runs', 0)}")
    lines.append(f"- **Total cost:** ${summary.get('total_cost_usd', 0):.4f}")
    lines.append(f"- **Avg cost per task:** ${summary.get('avg_cost_per_task_usd', 0):.4f}")
    lines.append(f"- **Total tokens:** {summary.get('total_tokens', 0):,}")

    effect = analytics.get("effect_metrics", {})
    if effect.get("features"):
        lines.append("\n## Effect Metrics\n")
        lines.append(f"- Features tracked: {effect['features']}")
        lines.append(f"- Total runs: {effect['runs']}")
        if effect.get("problem_rate") is not None:
            lines.append(f"- Problem rate: {effect['problem_rate']:.1%}")

    top_tasks = analytics.get("top_tasks_by_cost", [])
    if top_tasks:
        lines.append("\n## Top Tasks by Cost\n")
        for i, task in enumerate(top_tasks, 1):
            lines.append(f"{i}. `{task['workitem_id']}`: ${task['cost_usd']:.4f}")

    providers = analytics.get("cost_by_provider", {})
    if providers:
        lines.append("\n## Cost by Provider\n")
        for provider, cost in providers.items():
            lines.append(f"- {provider}: ${cost:.4f}")

    models = analytics.get("cost_by_model", {})
    if models:
        lines.append("\n## Cost by Model\n")
        for model, cost in list(models.items())[:5]:  # Top 5
            lines.append(f"- {model}: ${cost:.4f}")

    return "\n".join(lines)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Outcome Analytics — сводная аналитика результатов")
    ap.add_argument("root", nargs="?", default=".", help="Repository root")
    ap.add_argument("--period", default="all", help="Time period: 7d, 30d, all (default: all)")
    ap.add_argument("--json", action="store_true", help="Output as JSON")
    ap.add_argument("--selftest", action="store_true", help="Run self-test")
    args = ap.parse_args()

    if args.selftest:
        # ЧЕСТНЫЙ --selftest (фаза 0, 19.08.2026). Здесь печаталась строка о пройденном
        # селфтесте и три строки «... : OK» — без единого вызова проверяемых функций. То есть
        # модуль УТВЕРЖДАЛ проверку, которой не было: ровно класс «объявлено, но не
        # исполняется», против которого стоит весь кит (ср. R-31/R-32 — две фиктивные проверки
        # в валидаторах). Образец честной формы — devtools/mutation_probe.py: модуль объясняет
        # себя и называет, где лежат его настоящие проверки. Правило репозитория (AGENTS.md):
        # тест модуля живёт в tests/, а не в продакшн-модуле, который едет в child-репозиторий.
        print(__doc__)
        print("Проверки модуля — в tests/unit/ (AGENTS.md: selftest не живёт в продакшн-модуле).")
        return 0

    root = Path(args.root).resolve()
    analytics = collect_analytics(root, args.period)

    if args.json:
        print(json.dumps(analytics, indent=2, ensure_ascii=False))
    else:
        print(format_report(analytics))

    return 0


if __name__ == "__main__":
    sys.exit(main())
