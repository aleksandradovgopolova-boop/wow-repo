#!/usr/bin/env python3
"""bench_performance.py (v3.28.0 R19) — performance benchmarks для критических модулей.

Замеряет время выполнения ключевых операций кита и сравнивает с baseline:
  - orchestrator: startup + mock provider call
  - preflight: assess() на типовых сигналах
  - security_scan: scan_secrets + scan_injection на N файлах
  - tool_loop: parse_action() на различных входах
  - usage_ledger: aggregate() на N записей
  - context_compiler: bundle assembly
  - lifecycle_store: durable_write + load_guarded

Baseline хранится в tools/.bench-baseline.json. Если benchmark медленнее baseline >2x —
предупреждение. Если >5x — ошибка (регрессия).

Честность:
  - Каждый benchmark запускается N раз, берётся медиана (не минимум — устойчивее к шуму)
  - Первый прогон — warmup (не считается)
  - Пустой baseline = первый прогон создаёт его (не падает)
  - --update-baseline обновляет baseline текущими результатами

CLI:
  bench_performance.py [--iterations N] [--threshold X] [--update-baseline] [--json]
  bench_performance.py --selftest
"""
from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
from ai_ops_kit.shared import _bootstrap  # noqa: E402

BASELINE_PATH = PKG / "tools" / ".bench-baseline.json"
DEFAULT_ITERATIONS = 5
DEFAULT_THRESHOLD = 2.0  # warning if >2x baseline
REGRESSION_THRESHOLD = 5.0  # error if >5x baseline


def _time_fn(fn, iterations=DEFAULT_ITERATIONS, warmup=1):
    """Замерить время выполнения fn(). -> {median_ms, min_ms, max_ms, iterations}."""
    # Warmup
    for _ in range(warmup):
        fn()
    times = []
    for _ in range(iterations):
        t0 = time.monotonic()
        fn()
        elapsed = (time.monotonic() - t0) * 1000  # ms
        times.append(elapsed)
    return {
        "median_ms": round(statistics.median(times), 3),
        "min_ms": round(min(times), 3),
        "max_ms": round(max(times), 3),
        "iterations": iterations,
    }


def _bench_orchestrator_startup():
    """Benchmark: import orchestrator + make_provider('mock')."""
    import importlib
    from ai_ops_kit.providers import orchestrator
    importlib.reload(orchestrator)
    orchestrator.make_provider("mock")


def _bench_preflight_assess():
    """Benchmark: preflight.assess() на типовых сигналах."""
    from ai_ops_kit.gates import preflight
    with tempfile.TemporaryDirectory() as tmpdir:
        preflight.assess({"task_type": "ENGINEERING"}, tmpdir, "bench-wid")


def _bench_security_scan():
    """Benchmark: scan_secrets + scan_injection на 100 файлах."""
    from ai_ops_kit.security import security_scan
    files = {}
    for i in range(100):
        files[f"file_{i}.py"] = f"x = {i}\ny = 'hello_{i}'\n"
    security_scan.scan_secrets(files)
    security_scan.scan_injection(files)


def _bench_tool_loop_parse():
    """Benchmark: parse_action() на 100 различных входах."""
    from ai_ops_kit.engine import tool_loop
    inputs = [
        '{"op": "read", "path": "test.py"}',
        '{"done": true, "summary": "ok"}',
        '{"error": "bad-json"}',
        'some text {"op": "write"} more text',
        'no json here',
    ] * 20
    for text in inputs:
        tool_loop.parse_action(text)


def _bench_usage_aggregate():
    """Benchmark: usage_ledger.aggregate() на 1000 записей."""
    from ai_ops_kit.providers import usage_ledger
    records = [
        {"run_id": f"r{i}", "role": "implementation", "provider": "mock",
         "model": "test", "input_tokens": 100, "output_tokens": 50,
         "usage_status": "measured", "cost": 0.001, "cost_status": "measured",
         "latency": 0.1, "trigger": "initial"}
        for i in range(1000)
    ]
    usage_ledger.aggregate(records)


def _bench_lifecycle_store():
    """Benchmark: durable_write + load_guarded на временном файле."""
    from ai_ops_kit.lifecycle import lifecycle_store
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "test.yaml"
        data = {"kind": "test", "value": 1, "items": list(range(100))}
        for _ in range(10):
            lifecycle_store.durable_write(str(path), data, require_keys=["kind"])
            lifecycle_store.load_guarded(path, kind="test")


def _bench_context_compiler():
    """Benchmark: context_compiler.assemble() на минимальном child."""
    from ai_ops_kit.context import context_compiler
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / ".ai").mkdir(exist_ok=True)
        (root / "features" / "wid").mkdir(parents=True, exist_ok=True)
        try:
            context_compiler.assemble(str(root), "wid", signals={"task_type": "QUICK"})
        except Exception:
            pass  # some deps may not be set up; we measure the attempt


BENCHMARKS = {
    "orchestrator_startup": _bench_orchestrator_startup,
    "preflight_assess": _bench_preflight_assess,
    "security_scan_100files": _bench_security_scan,
    "tool_loop_parse_100": _bench_tool_loop_parse,
    "usage_aggregate_1000": _bench_usage_aggregate,
    "lifecycle_store_rw_10": _bench_lifecycle_store,
    "context_compiler_assemble": _bench_context_compiler,
}


def load_baseline(path=None) -> dict:
    """Загрузить baseline. path=None -> BASELINE_PATH репозитория."""
    p = Path(path) if path else BASELINE_PATH
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_baseline(results: dict, path=None):
    """Сохранить результаты как baseline. path=None -> BASELINE_PATH репозитория.

    Путь берётся аргументом, а не подменой модульной глобали: селфтест запускается как
    `python3 tools/bench_performance.py --selftest`, то есть модулем `__main__`, и его
    `import bench_performance as bp` создавал ВТОРОЙ объект модуля. Подмена `bp.BASELINE_PATH`
    патчила копию, а save_baseline из `__main__` продолжал писать в настоящий файл репозитория —
    каждый прогон селфтеста молча переписывал baseline замерами текущей машины. Порог «медленнее
    baseline >2x» при этом переставал что-либо значить: baseline догонял любую регрессию."""
    p = Path(path) if path else BASELINE_PATH
    p.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")


def run_all(iterations=DEFAULT_ITERATIONS) -> dict:
    """Прогнать все benchmarks. -> {name: {median_ms, min_ms, max_ms, iterations}}."""
    results = {}
    for name, fn in BENCHMARKS.items():
        try:
            results[name] = _time_fn(fn, iterations=iterations)
        except Exception as e:
            results[name] = {"error": str(e), "median_ms": None}
    return results


def compare_with_baseline(results: dict, baseline: dict, threshold=DEFAULT_THRESHOLD):
    """Сравнить результаты с baseline. -> {name: {status, ratio, median_ms, baseline_ms}}."""
    comparison = {}
    for name, r in results.items():
        if r.get("median_ms") is None:
            comparison[name] = {"status": "error", "reason": r.get("error", "unknown")}
            continue
        b = baseline.get(name, {})
        b_ms = b.get("median_ms")
        if b_ms is None or b_ms == 0:
            comparison[name] = {"status": "no_baseline", "median_ms": r["median_ms"]}
            continue
        ratio = r["median_ms"] / b_ms
        if ratio > REGRESSION_THRESHOLD:
            status = "REGRESSION"
        elif ratio > threshold:
            status = "WARNING"
        else:
            status = "OK"
        comparison[name] = {
            "status": status,
            "ratio": round(ratio, 2),
            "median_ms": r["median_ms"],
            "baseline_ms": b_ms,
        }
    return comparison


def format_text(results: dict, comparison: dict = None) -> str:
    """Человекочитаемый отчёт."""
    lines = ["=== AI Ops Kit — Performance Benchmarks ===", ""]
    for name, r in sorted(results.items()):
        ms = r.get("median_ms")
        if ms is None:
            lines.append(f"  {name}: ERROR — {r.get('error', 'unknown')}")
            continue
        line = f"  {name}: {ms:.1f}ms (min={r['min_ms']:.1f}, max={r['max_ms']:.1f})"
        if comparison and name in comparison:
            c = comparison[name]
            if c["status"] == "REGRESSION":
                line += f"  ⛔ REGRESSION {c['ratio']}x baseline ({c['baseline_ms']:.1f}ms)"
            elif c["status"] == "WARNING":
                line += f"  ⚠ WARNING {c['ratio']}x baseline ({c['baseline_ms']:.1f}ms)"
            elif c["status"] == "OK":
                line += f"  ✓ {c['ratio']}x baseline"
            elif c["status"] == "no_baseline":
                line += "  (no baseline)"
        lines.append(line)
    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="AI Ops Kit performance benchmarks")
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS,
                        help=f"Number of iterations per benchmark (default: {DEFAULT_ITERATIONS})")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help=f"Warning threshold as ratio of baseline (default: {DEFAULT_THRESHOLD})")
    parser.add_argument("--update-baseline", action="store_true",
                        help="Save current results as new baseline")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--selftest", action="store_true", help="Run selftest")
    args = parser.parse_args()

    results = run_all(iterations=args.iterations)
    baseline = load_baseline()
    comparison = compare_with_baseline(results, baseline, threshold=args.threshold) if baseline else None

    if args.update_baseline:
        save_baseline(results)
        print(f"Baseline updated: {BASELINE_PATH}")

    if args.json:
        output = {"results": results, "comparison": comparison, "baseline_path": str(BASELINE_PATH)}
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        print(format_text(results, comparison))
        if not baseline:
            print(f"\nNo baseline found. Run with --update-baseline to create one.")

    # Exit code: 1 if any regression
    if comparison:
        regressions = [n for n, c in comparison.items() if c.get("status") == "REGRESSION"]
        if regressions:
            print(f"\n⛔ REGRESSIONS detected: {', '.join(regressions)}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
