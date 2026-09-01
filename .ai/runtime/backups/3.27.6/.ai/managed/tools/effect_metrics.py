#!/usr/bin/env python3
"""Метрики эффекта по истории прогонов (v2.5) — слой «Метрики эффекта» из внешнего ревью.

Вход: .ai/project/report-history/*.jsonl — срезы run_report --record (по файлу на фичу).
Считает ДЕТЕРМИНИРОВАННО, по накопленной истории, а не по впечатлению:
  - на фичу: число срезов, период, доля срезов с PROBLEM, последний вердикт/стадия,
    динамика покрытия (заполнено первый срез -> последний), days-in-flight
    (первый срез -> последний), продвижение по стадиям;
  - агрегат: фич/срезов всего, PROBLEM-rate, медиана days-in-flight фич,
    дошедших до retrospective.

Честность: фича с < {MIN_RUNS} срезов помечается insufficient-data и не искажает
агрегат; при < {MIN_FEATURES} фич с достаточной историей агрегат сопровождается
предупреждением (условие из memory: метрикам эффекта нужно 3-5 прогонов).

Использование:  effect_metrics.py [history-dir] [--json]   (default: .ai/project/report-history)
                effect_metrics.py --selftest
Возврат 0 всегда (отчёт — данные; решения за людьми/INSIGHTS), 1 — только при ошибке чтения.
"""

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from statistics import median

STAGES = ["discovery", "definition", "ux", "architecture", "delivery",
          "analytics", "documentation", "release", "monitoring", "adoption", "retrospective"]
MIN_RUNS = 3
MIN_FEATURES = 3


def load_history(hist_dir: Path):
    features = {}
    for f in sorted(hist_dir.glob("*.jsonl")):
        entries = []
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
        if entries:
            features[f.stem] = sorted(entries, key=lambda e: e.get("ts", ""))
    return features


def feature_metrics(entries):
    first, last = entries[0], entries[-1]
    runs = len(entries)
    problem_rate = round(sum(1 for e in entries if e.get("verdict") == "PROBLEM") / runs, 2)
    try:
        t0 = datetime.fromisoformat(first["ts"])
        t1 = datetime.fromisoformat(last["ts"])
        days = round((t1 - t0).total_seconds() / 86400, 1)
    except (KeyError, ValueError):
        days = None
    def stage_idx(e):
        s = e.get("current_stage")
        return STAGES.index(s) if s in STAGES else None
    i0, i1 = stage_idx(first), stage_idx(last)
    return {
        "runs": runs,
        "sufficient": runs >= MIN_RUNS,
        "period_days": days,
        "problem_rate": problem_rate,
        "last_verdict": last.get("verdict"),
        "last_stage": last.get("current_stage"),
        "stages_advanced": (i1 - i0) if (i0 is not None and i1 is not None) else None,
        "coverage_filled_first_to_last": [
            (first.get("coverage") or {}).get("filled"),
            (last.get("coverage") or {}).get("filled")],
        "reached_retrospective": last.get("current_stage") == "retrospective",
    }


def build(hist_dir: Path):
    features = load_history(hist_dir)
    per_feature = {fid: feature_metrics(es) for fid, es in features.items()}
    sufficient = {f: m for f, m in per_feature.items() if m["sufficient"]}
    total_runs = sum(m["runs"] for m in per_feature.values())
    flights = [m["period_days"] for m in sufficient.values()
               if m["reached_retrospective"] and m["period_days"] is not None]
    agg = {
        "features": len(per_feature),
        "features_with_sufficient_history": len(sufficient),
        "total_runs": total_runs,
        "problem_rate": (round(sum(1 for es in features.values() for e in es
                                   if e.get("verdict") == "PROBLEM") / total_runs, 2)
                         if total_runs else None),
        "median_days_to_retrospective": (round(median(flights), 1) if flights else None),
        "baseline_ready": len(sufficient) >= MIN_FEATURES,
    }
    return {"schema_version": 1, "kind": "effect-metrics-report",
            "history_dir": str(hist_dir), "per_feature": per_feature, "aggregate": agg}


def print_human(r):
    agg = r["aggregate"]
    print(f"=== Метрики эффекта ({r['history_dir']}) ===")
    for fid, m in r["per_feature"].items():
        note = "" if m["sufficient"] else f"  [insufficient-data: {m['runs']} < {MIN_RUNS} срезов]"
        print(f"  {fid}: срезов={m['runs']}, PROBLEM-rate={m['problem_rate']}, "
              f"последний={m['last_verdict']}@{m['last_stage']}, "
              f"период={m['period_days']}д, стадий пройдено={m['stages_advanced']}{note}")
    print(f"агрегат: фич={agg['features']} (с достаточной историей: "
          f"{agg['features_with_sufficient_history']}), срезов={agg['total_runs']}, "
          f"PROBLEM-rate={agg['problem_rate']}, "
          f"медиана до retrospective={agg['median_days_to_retrospective']}д")
    if not agg["baseline_ready"]:
        print(f"ВНИМАНИЕ: baseline не готов — нужно >= {MIN_FEATURES} фич с >= {MIN_RUNS} "
              "срезами; выводы по текущим числам преждевременны.")


def selftest():
    ok = True

    def expect(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"{'PASS' if good else 'FAIL'} {name}" + ("" if good else f" (got {got})"))

    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        def entry(ts, verdict, stage, filled):
            return json.dumps({"schema_version": 1, "ts": ts, "feature": "f",
                               "verdict": verdict, "current_stage": stage,
                               "coverage": {"filled": filled}, "problems": 0, "warns": 0})
        (h / "feat-a.jsonl").write_text("\n".join([
            entry("2026-07-01T10:00:00+00:00", "PROBLEM", "discovery", 1),
            entry("2026-07-04T10:00:00+00:00", "WARN", "delivery", 5),
            entry("2026-07-08T10:00:00+00:00", "OK", "retrospective", 9),
        ]) + "\n", encoding="utf-8")
        (h / "feat-b.jsonl").write_text(entry("2026-07-09T10:00:00+00:00", "OK", "definition", 3) + "\n",
                                        encoding="utf-8")
        r = build(h)
        a = r["per_feature"]["feat-a"]
        expect("feat-a: 3 среза достаточно", a["sufficient"], True)
        expect("feat-a: problem_rate 0.33", a["problem_rate"], 0.33)
        expect("feat-a: 7 дней в полёте", a["period_days"], 7.0)
        expect("feat-a: стадий пройдено 10", a["stages_advanced"], 10)
        expect("feat-b: insufficient", r["per_feature"]["feat-b"]["sufficient"], False)
        expect("медиана до retrospective = 7", r["aggregate"]["median_days_to_retrospective"], 7.0)
        expect("baseline не готов (< 3 фич с историей)", r["aggregate"]["baseline_ready"], False)
    print("effect-metrics selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    hist_dir = Path(args[0]).resolve() if args else Path(".ai/project/report-history").resolve()
    if not hist_dir.exists():
        print(f"история не найдена: {hist_dir} — запускайте run_report с --record.")
        return 1
    r = build(hist_dir)
    if "--json" in argv:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print_human(r)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
