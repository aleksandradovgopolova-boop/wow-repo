#!/usr/bin/env python3
"""Проверка LoopTrace + анализ прогресса — v3.5 Observability.

LoopTrace (schemas/loop-trace.schema.json) — трейс итераций governed-цикла. Валидатор + анализатор:

  1. schema_version=1, kind=LoopTrace; id LT-NNN; loop_policy LP-NNN; progress_direction ∈ 2-enum;
     iterations непусты, n возрастает от 1; outcome ∈ continue|blocked|success; stopped_reason ∈ 5-enum;
  2. АНАЛИЗ: no_progress (≥2 подряд итерации без улучшения progress_value по direction) и
     repeated_failure (одна signature в ≥2 подряд blocked-итерациях);
  3. СОГЛАСОВАННОСТЬ (fail-closed):
     - stopped_reason=success ⟺ последняя итерация outcome=success;
     - stopped_reason=no_progress требует детекта no_progress; =repeated_failure требует детекта;
     - если детектнут no_progress/repeated_failure -> stopped_reason НЕ может быть success (loop не
       имел права «успешно» закончиться, застряв).

Использование:  validate_loop_trace.py [<lt.(yaml|json)>|<dir>] [--json] | --selftest
"""
import json
import re
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
SCHEMA = PKG / "schemas" / "loop-trace.schema.json"
DEMO = PKG / "examples" / "loop-trace-demo"
DIRECTION = {"lower_is_better", "higher_is_better"}
OUTCOME = {"continue", "blocked", "success"}
STOP = {"success", "budget_exhausted", "no_progress", "repeated_failure", "escalated"}
_ID = re.compile(r"^LT-[0-9]{3,}$")
_LP = re.compile(r"^LP-[0-9]{3,}$")


def _improved(prev, cur, direction):
    return cur < prev if direction == "lower_is_better" else cur > prev


def analyze(trace: dict) -> dict:
    its = trace.get("iterations") or []
    direction = trace.get("progress_direction")
    vals = [it.get("progress_value") for it in its]
    # длиннейшая серия подряд идущих НЕ-улучшений
    streak = maxstreak = 0
    for i in range(1, len(vals)):
        if not _improved(vals[i - 1], vals[i], direction):
            streak += 1
            maxstreak = max(maxstreak, streak)
        else:
            streak = 0
    no_progress = maxstreak >= 2
    # repeated_failure: одна signature в ≥2 подряд blocked
    rep = False
    prev_sig = None
    run = 1
    for it in its:
        sig = it.get("signature") if it.get("outcome") == "blocked" else None
        if sig is not None and sig == prev_sig:
            run += 1
            if run >= 2:
                rep = True
        else:
            run = 1
        prev_sig = sig
    last = its[-1] if its else {}
    if last.get("outcome") == "success":
        verdict = "converged"
    elif rep:
        verdict = "repeated_failure"
    elif no_progress:
        verdict = "no_progress"
    else:
        verdict = "progressing"
    return {"no_progress": no_progress, "repeated_failure": rep, "verdict": verdict,
            "progress_series": vals}


def check(data: dict):
    e = []
    if not isinstance(data, dict):
        return ["LoopTrace не объект"]
    if data.get("schema_version") != 1 or data.get("kind") != "LoopTrace":
        e.append("schema_version=1 + kind=LoopTrace обязательны")
    if not (isinstance(data.get("id"), str) and _ID.match(data["id"])):
        e.append("id должен быть формата LT-NNN")
    if not (isinstance(data.get("loop_policy"), str) and _LP.match(data["loop_policy"])):
        e.append("loop_policy должен быть LP-NNN")
    if data.get("progress_direction") not in DIRECTION:
        e.append(f"progress_direction ∉ {sorted(DIRECTION)}")
    its = data.get("iterations")
    if not (isinstance(its, list) and its):
        e.append("iterations — непустой список")
        its = []
    for i, it in enumerate(its):
        if not isinstance(it, dict):
            e.append(f"iteration[{i}] не объект")
            continue
        if it.get("n") != i + 1:
            e.append(f"iteration[{i}].n должен быть {i + 1} (возрастает от 1)")
        if not isinstance(it.get("progress_value"), (int, float)) or isinstance(it.get("progress_value"), bool):
            e.append(f"iteration[{i}].progress_value должен быть числом")
        if it.get("outcome") not in OUTCOME:
            e.append(f"iteration[{i}].outcome ∉ {sorted(OUTCOME)}")
    sr = data.get("stopped_reason")
    if sr not in STOP:
        e.append(f"stopped_reason ∉ {sorted(STOP)}")

    if its and all(isinstance(it, dict) for it in its) \
            and data.get("progress_direction") in DIRECTION \
            and all(isinstance(it.get("progress_value"), (int, float)) for it in its):
        a = analyze(data)
        last_ok = its[-1].get("outcome") == "success"
        if sr == "success" and not last_ok:
            e.append("stopped_reason=success, но последняя итерация не success")
        if last_ok and sr != "success":
            e.append("последняя итерация success, но stopped_reason != success")
        if sr == "no_progress" and not a["no_progress"]:
            e.append("stopped_reason=no_progress, но no-progress не детектится")
        if sr == "repeated_failure" and not a["repeated_failure"]:
            e.append("stopped_reason=repeated_failure, но repeated-failure не детектится")
        if (a["no_progress"] or a["repeated_failure"]) and sr == "success":
            e.append("детектнут no_progress/repeated_failure, но stopped_reason=success (loop застрял, не успех)")
    return e


def _load(p: Path):
    t = p.read_text(encoding="utf-8")
    return yaml.safe_load(t) if p.suffix in (".yaml", ".yml") else json.loads(t)


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    ex = json.loads(SCHEMA.read_text(encoding="utf-8"))["examples"][0]
    expect("пример LoopTrace валиден (converged)", check(ex) == [])
    expect("анализ примера: converged, прогресс 1->0",
           analyze(ex)["verdict"] == "converged" and analyze(ex)["no_progress"] is False)
    if DEMO.is_dir():
        expect("реальный loop-trace-demo целостен",
               all(check(_load(f)) == [] for f in sorted(DEMO.glob("LT-*.yaml"))))

    # no-progress: 1,1,1 (lower_is_better) blocked
    np = {**ex, "id": "LT-009", "stopped_reason": "no_progress", "iterations": [
        {"n": 1, "progress_value": 1, "outcome": "blocked", "signature": "a"},
        {"n": 2, "progress_value": 1, "outcome": "blocked", "signature": "b"},
        {"n": 3, "progress_value": 1, "outcome": "blocked", "signature": "c"}]}
    a = analyze(np)
    expect("no_progress детектится (1,1,1)", a["no_progress"] and a["verdict"] == "no_progress")
    expect("no-progress trace со stopped_reason=no_progress валиден", check(np) == [])
    expect("no-progress, но stopped_reason=success -> ошибка (застрял, не успех)",
           any("застрял" in x for x in check({**np, "stopped_reason": "success",
               "iterations": np["iterations"][:-1] + [{"n": 3, "progress_value": 1, "outcome": "success"}]})))

    # repeated_failure: одна signature 2x подряд
    rf = {**ex, "id": "LT-010", "stopped_reason": "repeated_failure", "iterations": [
        {"n": 1, "progress_value": 2, "outcome": "blocked", "signature": "same"},
        {"n": 2, "progress_value": 2, "outcome": "blocked", "signature": "same"}]}
    expect("repeated_failure детектится (same 2x)", analyze(rf)["repeated_failure"] is True)
    expect("repeated_failure trace валиден", check(rf) == [])

    # progressing (не финальный): 3->2->1 continue
    pr = {**ex, "id": "LT-011", "stopped_reason": "budget_exhausted", "iterations": [
        {"n": 1, "progress_value": 3, "outcome": "blocked", "signature": "x"},
        {"n": 2, "progress_value": 2, "outcome": "blocked", "signature": "y"},
        {"n": 3, "progress_value": 1, "outcome": "blocked", "signature": "z"}]}
    expect("прогрессирующий trace -> verdict progressing", analyze(pr)["verdict"] == "progressing")

    # структурные
    expect("n не по порядку -> ошибка",
           any(".n должен быть" in x for x in check({**ex, "iterations": [
               {"n": 5, "progress_value": 0, "outcome": "success"}]})))
    expect("stopped_reason=success при не-success последней -> ошибка",
           any("success" in x for x in check({**ex, "iterations": [
               {"n": 1, "progress_value": 1, "outcome": "blocked", "signature": "a"}]})))
    expect("битый id -> ошибка", any("id" in x for x in check({**ex, "id": "LT1"})))

    print("validate_loop_trace selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    target = Path(args[0]) if args else DEMO
    files = sorted(target.glob("LT-*.yaml")) if target.is_dir() else [target]
    errors, analyses = [], {}
    for f in files:
        d = _load(f)
        errs = check(d)
        errors += [f"{f.name}: {x}" for x in errs]
        if not errs:
            analyses[f.name] = analyze(d)["verdict"]
    if "--json" in argv:
        print(json.dumps({"errors": errors, "verdicts": analyses}, ensure_ascii=False, indent=2))
    elif errors:
        print(f"LOOP-TRACE: {len(errors)} нарушений:")
        for x in errors:
            print(f"  - {x}")
    else:
        print(f"LOOP-TRACE-OK: {len(files)} трейсов; verdicts={analyses}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
