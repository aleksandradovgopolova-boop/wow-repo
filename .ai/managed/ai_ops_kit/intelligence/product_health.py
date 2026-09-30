#!/usr/bin/env python3
"""Product Health Score (Ф4 roadmap, v2.0) — детерминированный калькулятор.

Вход: YAML с сырыми метриками (экспорт из аналитики/мониторинга, руками или CI):
  scope: feature:express-checkout
  period: 2026-W30
  metrics:
    adoption:    {value: 0.42, target: 0.5, direction: higher-is-better, source: "dashboard X"}
    errors:      {value: 0.8,  target: 1.0, direction: lower-is-better,  source: "alerting"}
    ...
  weights: {adoption: 2, errors: 1}        # опционально; по умолчанию все = 1

Выход: machine-readable отчёт (schemas/product-health.schema.json) в stdout или файл.
Расчёт: normalized = clamp(value/target) для higher-is-better,
clamp(target/value) для lower-is-better (value=0 -> 1.0);
score = 100 * взвешенное среднее; band: >=75 healthy, >=50 warning, иначе critical.
Findings: метрики с normalized < 0.7. Интерпретация и решения — workflow INSIGHTS, не скрипт.

Использование:  product_health.py <input.yaml> [-o report.json]
                product_health.py --selftest
Возврат 0 — успех (независимо от band: отчёт — данные, решение за людьми/INSIGHTS).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

KNOWN = ["adoption", "activation", "retention", "reliability",
         "errors", "performance", "support_load", "feature_usage"]
BANDS = [(75, "healthy"), (50, "warning"), (0, "critical")]


def normalize(value: float, target: float, direction: str) -> float:
    if direction == "lower-is-better":
        if value <= 0:
            return 1.0
        ratio = target / value
    else:
        if target <= 0:
            return 1.0
        ratio = value / target
    return max(0.0, min(1.0, ratio))


def compute(inp: dict) -> dict:
    metrics = inp.get("metrics") or {}
    if not metrics:
        raise SystemExit("во входе нет metrics")
    unknown = sorted(set(metrics) - set(KNOWN))
    if unknown:
        raise SystemExit(f"неизвестные метрики {unknown}; допустимые: {KNOWN}")
    weights = inp.get("weights") or {}
    out_metrics, total_w, acc, findings = {}, 0.0, 0.0, []
    for name, m in metrics.items():
        if "value" not in m or "target" not in m:
            raise SystemExit(f"метрика '{name}' без value/target")
        direction = m.get("direction", "higher-is-better")
        norm = normalize(float(m["value"]), float(m["target"]), direction)
        w = float(weights.get(name, 1))
        out_metrics[name] = {**m, "direction": direction,
                             "normalized": round(norm, 4), "weight": w}
        total_w += w
        acc += norm * w
        if norm < 0.7:
            findings.append(f"{name}: normalized {norm:.2f} < 0.7 "
                            f"(value {m['value']}, target {m['target']})")
    score = round(100 * acc / total_w, 1)
    band = next(b for threshold, b in BANDS if score >= threshold)
    return {
        "schema_version": 1,
        "kind": "product-health-report",
        "scope": inp.get("scope", "product"),
        "period": inp.get("period", "unspecified"),
        "metrics": out_metrics,
        "health_score": {"value": score, "band": band,
                         "weights_used": {k: out_metrics[k]["weight"] for k in out_metrics}},
        "findings": findings,
        "generated_by": "ai_ops_kit/intelligence/product_health.py",
    }


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    inp = yaml.safe_load(Path(argv[0]).read_text(encoding="utf-8"))
    report = compute(inp)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if "-o" in argv:
        out = Path(argv[argv.index("-o") + 1])
        out.write_text(text + "\n", encoding="utf-8")
        print(f"отчёт: {out} (score {report['health_score']['value']}, "
              f"{report['health_score']['band']})")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
