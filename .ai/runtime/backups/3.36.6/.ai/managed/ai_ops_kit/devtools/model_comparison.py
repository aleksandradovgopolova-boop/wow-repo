#!/usr/bin/env python3
"""model_comparison.py (v3.4.5) — сравнение моделей по task-tier (SAFETY-FIRST).

Последний инкремент v3.4. Агрегирует ModelBenchResult (качество из Bench Lite/живых прогонов +
экономика из cost_account) и ранжирует модели ПО task-tier для осознанного выбора.

ГЛАВНОЕ ПРАВИЛО (safety over economy): любая модель с false_green > 0 ДИСКВАЛИФИЦИРУЕТСЯ для этого
tier — сколь угодно дешёвая/быстрая/«качественная» модель, которая отдаёт ложный green, не
рекомендуется НИКОГДА. Среди безопасных ранжируем: pass-rate ↓, false_fail ↑, cost ↑, latency ↑.
Если все модели дисквалифицированы -> рекомендации нет (fail-closed).

Прямая опора этой сессии: DeepSeek на ENGINEERING — низкий pass-rate, но false_green=0 (безопасен,
низкий throughput); sonnet-класс нужен для green-path. Данные, а не мнение.

CLI:  model_comparison.py [<dir с MBR-*.yaml>] [--json] | --selftest
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
SCHEMA = PKG / "schemas" / "model-bench-result.schema.json"
DEMO = PKG / "examples" / "model-comparison-demo"
TIERS = {"QUICK", "ENGINEERING", "UI"}
_ID = re.compile(r"^MBR-[0-9]{3,}$")


def check(data: dict):
    e = []
    if not isinstance(data, dict):
        return ["MBR не объект"]
    if data.get("schema_version") != 1 or data.get("kind") != "ModelBenchResult":
        e.append("schema_version=1 + kind=ModelBenchResult обязательны")
    if not (isinstance(data.get("id"), str) and _ID.match(data["id"])):
        e.append("id должен быть формата MBR-NNN")
    for f in ("model", "provider", "source"):
        if not (isinstance(data.get(f), str) and data[f].strip()):
            e.append(f"{f} обязателен и непуст")
    if data.get("task_tier") not in TIERS:
        e.append(f"task_tier ∉ {sorted(TIERS)}")
    q = data.get("quality")
    if not isinstance(q, dict):
        e.append("quality обязателен")
    else:
        for k in ("total", "pass", "false_green", "false_fail", "fix_recovered"):
            v = q.get(k)
            if not (isinstance(v, int) and not isinstance(v, bool) and v >= 0):
                e.append(f"quality.{k} — целое ≥0")
        if isinstance(q.get("pass"), int) and isinstance(q.get("total"), int) and q["pass"] > q["total"]:
            e.append("quality.pass > total")
    return e


def _pass_rate(q):
    t = q.get("total") or 0
    return (q.get("pass") or 0) / t if t else 0.0


def compare(results: list) -> dict:
    by_tier = {}
    for tier in sorted({r.get("task_tier") for r in results if r.get("task_tier") in TIERS}):
        entries = [r for r in results if r.get("task_tier") == tier]
        disqualified, safe = [], []
        for r in entries:
            q = r.get("quality") or {}
            if (q.get("false_green") or 0) > 0:
                disqualified.append({"model": r.get("model"),
                                     "reason": f"false_green={q['false_green']}: небезопасен (ложный green)"})
            else:
                safe.append(r)
        # ранжирование безопасных: pass-rate ↓, false_fail ↑, cost ↑ (None в конец), latency ↑
        def _key(r):
            q = r.get("quality") or {}
            ec = r.get("economics") or {}
            cost = ec.get("cost_usd")
            lat = ec.get("latency_s")
            return (-_pass_rate(q), (q.get("false_fail") or 0),
                    (cost if cost is not None else float("inf")),
                    (lat if lat is not None else float("inf")))
        safe_sorted = sorted(safe, key=_key)
        ranked = [{"model": r.get("model"), "provider": r.get("provider"),
                   "pass_rate": round(_pass_rate(r.get("quality") or {}), 3),
                   "false_fail": (r.get("quality") or {}).get("false_fail"),
                   "cost_usd": (r.get("economics") or {}).get("cost_usd"),
                   "latency_s": (r.get("economics") or {}).get("latency_s")} for r in safe_sorted]
        by_tier[tier] = {"ranked": ranked, "disqualified": disqualified,
                         "recommended": ranked[0]["model"] if ranked else None,
                         "recommendation_basis": ("safety-first: false_green=0, затем pass-rate/cost/latency"
                                                  if ranked else "нет безопасной модели -> рекомендации нет")}
    return {"kind": "model-comparison", "tiers": by_tier}


def _load(p: Path):
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def _load_dir(d: Path):
    return [_load(f) for f in sorted(Path(d).glob("MBR-*.yaml"))] if Path(d).is_dir() else []


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    d = Path(args[0]) if args else DEMO
    results = _load_dir(d)
    errors = []
    for i, r in enumerate(results):
        errors += [f"MBR[{i}]: {x}" for x in check(r)]
    if errors:
        print("MODEL-COMPARISON: невалидные MBR:")
        for x in errors:
            print(f"  - {x}")
        return 1
    rep = compare(results)
    if "--json" in argv:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        for tier, t in rep["tiers"].items():
            print(f"[{tier}] recommended={t['recommended']} "
                  f"(ranked: {[r['model'] for r in t['ranked']]}; "
                  f"disqualified: {[d['model'] for d in t['disqualified']]})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
