#!/usr/bin/env python3
"""kit_observability.py (v3.28.0 R13) — наблюдаемость самого кита.

Агрегирует метрики эффективности AI Ops Kit по данным child-репозитория:
  - стоимость (из usage_ledger): total, by task_type, by provider, by role
  - задачи (из features/*/workitem.yaml): count, status distribution, lifecycle stages
  - доставка (из delivery receipts): success rate, SHA verification
  - модели: какие провайдеры/модели используются, доля measured vs unavailable

Честность:
  - unavailable cost НЕ считается как 0 — показывается отдельно
  - пустой репозиторий → честный "no data", не нулевые метрики
  - все метрики вычисляются детерминированно из файлов, не по впечатлению

CLI:
  kit_observability.py <child_root> [--json]
  kit_observability.py --selftest
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
from ai_ops_kit.shared import _bootstrap  # noqa: E402

from ai_ops_kit.providers import usage_ledger  # noqa: E402


def _load_workitems(child_root: Path) -> list[dict]:
    """Загрузить все WorkItem'ы из features/*/workitem.yaml."""
    import yaml
    items = []
    features_dir = child_root / "features"
    if not features_dir.is_dir():
        return items
    for wid_dir in sorted(features_dir.iterdir()):
        if not wid_dir.is_dir():
            continue
        wi_path = wid_dir / "workitem.yaml"
        if not wi_path.is_file():
            continue
        try:
            data = yaml.safe_load(wi_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data["id"] = data.get("id", wid_dir.name)
                items.append(data)
        except Exception:
            continue
    return items


def _load_delivery_receipts(child_root: Path) -> list[dict]:
    """Загрузить все DeliveryReceipt'ы из features/*/delivery-outbox/*.receipt.yaml."""
    import yaml
    receipts = []
    features_dir = child_root / "features"
    if not features_dir.is_dir():
        return receipts
    for wid_dir in sorted(features_dir.iterdir()):
        if not wid_dir.is_dir():
            continue
        outbox = wid_dir / "delivery-outbox"
        if not outbox.is_dir():
            continue
        for rp in sorted(outbox.glob("*.receipt.yaml")):
            try:
                data = yaml.safe_load(rp.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    receipts.append(data)
            except Exception:
                continue
    return receipts


def compute(child_root: str) -> dict:
    """Вычислить метрики наблюдаемости кита. -> KitObservabilityReport.

    Секции:
      kit          — общая сводка (version, child_root)
      cost         — стоимость: total, by_task_type, by_provider, by_role, honesty flags
      workitems    — задачи: count, by_status, by_lifecycle, by_workflow
      delivery     — доставка: total, sha_verified, merged, not_delivered, mismatch
      models       — модели: by_provider, by_model, measured vs unavailable
    """
    root = Path(child_root)
    report: dict = {
        "kit": {
            "child_root": str(root),
            "has_data": False,
        },
        "cost": {
            "total_cost_usd": 0.0,
            "total_calls": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "cost_complete": True,
            "cost_unavailable_count": 0,
            "by_task_type": {},
            "by_provider": {},
            "by_role": {},
            "by_writer_tier": {},
        },
        "workitems": {
            "total": 0,
            "by_status": {},
            "by_lifecycle": {},
            "by_workflow": {},
        },
        "delivery": {
            "total": 0,
            "sha_verified": 0,
            "merged": 0,
            "not_delivered": 0,
            "mismatch": 0,
            "unavailable": 0,
        },
        "models": {
            "by_provider": {},
            "by_model": {},
            "measured_calls": 0,
            "unavailable_calls": 0,
        },
    }

    # --- Cost from usage ledger ---
    records = usage_ledger.load_product(str(root))
    if records:
        report["kit"]["has_data"] = True
        agg = usage_ledger.aggregate(records)
        report["cost"]["total_cost_usd"] = agg["cost"]
        report["cost"]["total_calls"] = agg["calls"]
        report["cost"]["total_input_tokens"] = agg["input_tokens"]
        report["cost"]["total_output_tokens"] = agg["output_tokens"]
        report["cost"]["cost_complete"] = agg["cost_complete"]
        report["cost"]["cost_unavailable_count"] = agg["cost_unavailable"]
        report["cost"]["by_task_type"] = agg["by_task_type"]
        report["cost"]["by_provider"] = agg["by_provider"]
        report["cost"]["by_role"] = agg["by_role"]
        report["cost"]["by_writer_tier"] = agg["by_writer_tier"]

        # Model-level breakdown
        for r in records:
            prov = r.get("provider") or "unknown"
            model = r.get("model") or "unknown"
            report["models"]["by_provider"][prov] = report["models"]["by_provider"].get(prov, 0) + 1
            report["models"]["by_model"][model] = report["models"]["by_model"].get(model, 0) + 1
            us = r.get("usage_status")
            if us == "measured":
                report["models"]["measured_calls"] += 1
            elif us == "unavailable":
                report["models"]["unavailable_calls"] += 1

    # --- Workitems ---
    workitems = _load_workitems(root)
    if workitems:
        report["kit"]["has_data"] = True
        report["workitems"]["total"] = len(workitems)
        for wi in workitems:
            status = wi.get("status") or "unknown"
            report["workitems"]["by_status"][status] = report["workitems"]["by_status"].get(status, 0) + 1
            lc = wi.get("lifecycle_intent") or "unknown"
            report["workitems"]["by_lifecycle"][lc] = report["workitems"]["by_lifecycle"].get(lc, 0) + 1
            wf = wi.get("workflow") or wi.get("base_workflow") or "unknown"
            report["workitems"]["by_workflow"][wf] = report["workitems"]["by_workflow"].get(wf, 0) + 1

    # --- Delivery ---
    receipts = _load_delivery_receipts(root)
    if receipts:
        report["kit"]["has_data"] = True
        report["delivery"]["total"] = len(receipts)
        for rc in receipts:
            st = rc.get("status") or "unknown"
            if rc.get("sha_verified"):
                report["delivery"]["sha_verified"] += 1
            if rc.get("merged"):
                report["delivery"]["merged"] += 1
            if st == "not-delivered":
                report["delivery"]["not_delivered"] += 1
            elif st == "mismatch":
                report["delivery"]["mismatch"] += 1
            elif st == "unavailable":
                report["delivery"]["unavailable"] += 1

    # --- Derived metrics ---
    if report["cost"]["total_calls"] > 0:
        report["cost"]["avg_cost_per_call"] = round(
            report["cost"]["total_cost_usd"] / report["cost"]["total_calls"], 6
        )
    if report["workitems"]["total"] > 0 and report["cost"]["total_cost_usd"] > 0:
        report["cost"]["avg_cost_per_workitem"] = round(
            report["cost"]["total_cost_usd"] / report["workitems"]["total"], 6
        )
    if report["delivery"]["total"] > 0:
        report["delivery"]["success_rate"] = round(
            report["delivery"]["sha_verified"] / report["delivery"]["total"], 2
        )

    return report


def format_text(report: dict) -> str:
    """Человекочитаемый формат отчёта."""
    lines = ["=== AI Ops Kit — Observability Report ===", ""]

    if not report["kit"]["has_data"]:
        lines.append("Нет данных. Child-репозиторий не содержит usage/workitem/delivery данных.")
        return "\n".join(lines)

    # Cost
    c = report["cost"]
    lines.append(f"💰 Cost: ${c['total_cost_usd']:.4f} total ({c['total_calls']} calls)")
    lines.append(f"   Tokens: {c['total_input_tokens']:,} in / {c['total_output_tokens']:,} out")
    if not c.get("cost_complete", True):
        lines.append(f"   ⚠ {c['cost_unavailable_count']} calls with unknown cost (not counted as $0)")
    if c.get("avg_cost_per_call") is not None:
        lines.append(f"   Avg: ${c['avg_cost_per_call']:.4f}/call")
    if c.get("avg_cost_per_workitem") is not None:
        lines.append(f"   Avg: ${c['avg_cost_per_workitem']:.4f}/workitem")
    if c.get("by_task_type"):
        lines.append("   By task type: " + ", ".join(f"{k}={v}" for k, v in sorted(c["by_task_type"].items())))
    if c.get("by_provider"):
        lines.append("   By provider: " + ", ".join(f"{k}={v}" for k, v in sorted(c["by_provider"].items())))
    lines.append("")

    # Workitems
    w = report["workitems"]
    if w["total"] > 0:
        lines.append(f"📋 Workitems: {w['total']} total")
        if w.get("by_status"):
            lines.append("   By status: " + ", ".join(f"{k}={v}" for k, v in sorted(w["by_status"].items())))
        if w.get("by_lifecycle"):
            lines.append("   By lifecycle: " + ", ".join(f"{k}={v}" for k, v in sorted(w["by_lifecycle"].items())))
        if w.get("by_workflow"):
            lines.append("   By workflow: " + ", ".join(f"{k}={v}" for k, v in sorted(w["by_workflow"].items())))
        lines.append("")

    # Delivery
    d = report["delivery"]
    if d["total"] > 0:
        lines.append(f"🚀 Delivery: {d['total']} attempts")
        lines.append(f"   SHA-verified: {d['sha_verified']}, merged: {d['merged']}")
        if d.get("success_rate") is not None:
            lines.append(f"   Success rate: {d['success_rate']:.0%}")
        if d["not_delivered"] or d["mismatch"]:
            lines.append(f"   Issues: not_delivered={d['not_delivered']}, mismatch={d['mismatch']}")
        lines.append("")

    # Models
    m = report["models"]
    if m.get("by_provider"):
        lines.append("🤖 Models:")
        lines.append("   By provider: " + ", ".join(f"{k}={v}" for k, v in sorted(m["by_provider"].items())))
        top_models = sorted(m.get("by_model", {}).items(), key=lambda x: -x[1])[:5]
        if top_models:
            lines.append("   Top models: " + ", ".join(f"{k}({v})" for k, v in top_models))
        if m["unavailable_calls"] > 0:
            lines.append(f"   ⚠ {m['unavailable_calls']} calls with unknown usage")

    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="AI Ops Kit observability report")
    parser.add_argument("child_root", nargs="?", help="Path to child repository root")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--selftest", action="store_true", help="Run selftest")
    args = parser.parse_args()

    if not args.child_root:
        parser.error("child_root required (or --selftest)")

    report = compute(args.child_root)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(format_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
