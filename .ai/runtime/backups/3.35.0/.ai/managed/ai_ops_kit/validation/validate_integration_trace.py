#!/usr/bin/env python3
"""Проверка IntegrationTrace + parallel-vs-sequential анализ — v3.5 Observability.

IntegrationTrace (schemas/integration-trace.schema.json) — трейс fan-in. Валидатор + анализатор:

  1. структура; packages непусты, id уникальны, sha и wall_seconds заданы;
  2. wall_seconds_actual ≥ max(package wall) (нельзя закончить раньше самого долгого пакета);
     sequential_baseline_seconds == сумма времён пакетов;
  3. ИНВАРИАНТ integration-SHA (v3.3.2): completed=true ТРЕБУЕТ integration_sha ≠ null И ≠ любого
     package_sha (новый SHA) И new_sha=true И aggregate_checks_rerun=true И fan_in_conflicts=0;
  4. КОНФЛИКТ=BLOCK: fan_in_conflicts>0 ТРЕБУЕТ completed=false;
  5. АНАЛИЗ: speedup = baseline/actual; coordination_overhead = actual - max(package wall).

Использование:  validate_integration_trace.py [<it.(yaml|json)>|<dir>] [--json] | --selftest
"""
import json
import re
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
SCHEMA = PKG / "schemas" / "integration-trace.schema.json"
DEMO = PKG / "examples" / "integration-trace-demo"
MODE = {"single", "sequential", "parallel", "hybrid"}
_ID = re.compile(r"^IT-[0-9]{3,}$")
_WG = re.compile(r"^WG-[0-9]{3,}$")
_IP = re.compile(r"^IP-[0-9]{3,}$")


def analyze(trace: dict) -> dict:
    pkgs = trace.get("packages") or []
    waits = [p.get("wall_seconds", 0) for p in pkgs]
    actual = trace.get("wall_seconds_actual")
    baseline = trace.get("sequential_baseline_seconds")
    speedup = round(baseline / actual, 3) if actual else None
    coordination = round(actual - max(waits), 3) if (waits and isinstance(actual, (int, float))) else None
    return {"speedup": speedup, "coordination_overhead": coordination,
            "beneficial": bool(speedup and speedup > 1.1)}


def check(data: dict):
    e = []
    if not isinstance(data, dict):
        return ["IntegrationTrace не объект"]
    if data.get("schema_version") != 1 or data.get("kind") != "IntegrationTrace":
        e.append("schema_version=1 + kind=IntegrationTrace обязательны")
    if not (isinstance(data.get("id"), str) and _ID.match(data["id"])):
        e.append("id должен быть формата IT-NNN")
    if not (isinstance(data.get("work_graph"), str) and _WG.match(data["work_graph"])):
        e.append("work_graph должен быть WG-NNN")
    if not (isinstance(data.get("integration_plan"), str) and _IP.match(data["integration_plan"])):
        e.append("integration_plan должен быть IP-NNN")
    if data.get("execution_mode") not in MODE:
        e.append(f"execution_mode ∉ {sorted(MODE)}")

    pkgs = data.get("packages")
    shas, waits = [], []
    if not (isinstance(pkgs, list) and pkgs):
        e.append("packages — непустой список")
        pkgs = []
    for p in pkgs:
        if not isinstance(p, dict) or not p.get("id") or not p.get("package_sha"):
            e.append("package требует id + package_sha")
            continue
        shas.append(p["package_sha"])
        w = p.get("wall_seconds")
        if not isinstance(w, (int, float)) or isinstance(w, bool) or w < 0:
            e.append(f"package {p['id']}.wall_seconds — число ≥0")
        else:
            waits.append(w)
    if len(set(p.get("id") for p in pkgs if isinstance(p, dict))) != len(pkgs):
        e.append("дубликаты id пакетов")

    actual = data.get("wall_seconds_actual")
    baseline = data.get("sequential_baseline_seconds")
    if isinstance(actual, (int, float)) and waits and actual < max(waits):
        e.append("wall_seconds_actual < max(package wall) — нельзя закончить раньше самого долгого пакета")
    if isinstance(baseline, (int, float)) and waits and abs(baseline - sum(waits)) > 1e-6:
        e.append(f"sequential_baseline_seconds ({baseline}) != сумма времён пакетов ({sum(waits)})")

    ig = data.get("integration")
    if not isinstance(ig, dict):
        e.append("integration обязателен")
        ig = {}
    completed = ig.get("completed")
    conflicts = ig.get("fan_in_conflicts")
    if not isinstance(completed, bool):
        e.append("integration.completed должен быть bool")
    if not (isinstance(conflicts, int) and not isinstance(conflicts, bool) and conflicts >= 0):
        e.append("integration.fan_in_conflicts — целое ≥0")
    if completed is True:
        isha = ig.get("integration_sha")
        if not (isinstance(isha, str) and isha.strip()):
            e.append("completed=true требует непустой integration_sha")
        elif isha in shas:
            e.append("ИНВАРИАНТ: integration_sha совпадает с package_sha — нужен НОВЫЙ integration-SHA")
        if ig.get("new_sha") is not True:
            e.append("completed=true требует new_sha=true")
        if ig.get("aggregate_checks_rerun") is not True:
            e.append("ИНВАРИАНТ: completed=true требует aggregate_checks_rerun=true (повтор проверок после fan-in)")
        if conflicts not in (0, None) and isinstance(conflicts, int) and conflicts > 0:
            e.append("completed=true требует fan_in_conflicts=0")
    if isinstance(conflicts, int) and conflicts > 0 and completed is True:
        e.append("КОНФЛИКТ=BLOCK: fan_in_conflicts>0 требует completed=false")
    return e


def _load(p: Path):
    t = p.read_text(encoding="utf-8")
    return yaml.safe_load(t) if p.suffix in (".yaml", ".yml") else json.loads(t)


def selftest():
    import copy
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    ex = json.loads(SCHEMA.read_text(encoding="utf-8"))["examples"][0]
    expect("пример IntegrationTrace валиден", check(ex) == [])
    a = analyze(ex)
    expect("анализ: speedup≈1.47, coordination=70, beneficial",
           a["speedup"] == round(280 / 190, 3) and a["coordination_overhead"] == 70 and a["beneficial"])
    if DEMO.is_dir():
        expect("реальный integration-trace-demo целостен",
               all(check(_load(f)) == [] for f in sorted(DEMO.glob("IT-*.yaml"))))

    def _mut(**over):
        d = copy.deepcopy(ex)
        d["integration"].update(over)
        return d

    expect("integration_sha == package_sha -> ошибка (не новый)",
           any("НОВЫЙ integration-SHA" in x for x in _chk_ig(ex, integration_sha="aaaaaaa")))
    expect("completed=true без rerun -> ошибка",
           any("aggregate_checks_rerun" in x for x in check(_mut(aggregate_checks_rerun=False))))
    expect("completed=true, new_sha=false -> ошибка",
           any("new_sha" in x for x in check(_mut(new_sha=False))))
    expect("conflicts>0 + completed=true -> ошибка (конфликт=block)",
           any("КОНФЛИКТ=BLOCK" in x for x in check(_mut(fan_in_conflicts=2))))
    # conflicts>0 + completed=false -> ок (заблокировано)
    blocked = copy.deepcopy(ex)
    blocked["integration"] = {"completed": False, "integration_sha": None, "new_sha": False,
                              "aggregate_checks_rerun": False, "fan_in_conflicts": 2}
    expect("conflicts>0 + completed=false -> валиден (fan-in заблокирован)", check(blocked) == [])
    # actual < max package
    bad_t = copy.deepcopy(ex)
    bad_t["wall_seconds_actual"] = 50
    expect("actual < max(package) -> ошибка", any("самого долгого" in x for x in check(bad_t)))
    # baseline != sum
    bad_b = copy.deepcopy(ex)
    bad_b["sequential_baseline_seconds"] = 999
    expect("baseline != сумма -> ошибка", any("сумма времён" in x for x in check(bad_b)))
    expect("битый id -> ошибка", any("id должен" in x for x in check({**ex, "id": "IT1"})))

    print("validate_integration_trace selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _chk_ig(ex, **over):
    import copy
    d = copy.deepcopy(ex)
    d["integration"].update(over)
    return check(d)


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    target = Path(args[0]) if args else DEMO
    files = sorted(target.glob("IT-*.yaml")) if target.is_dir() else [target]
    errors, metrics = [], {}
    for f in files:
        d = _load(f)
        errs = check(d)
        errors += [f"{f.name}: {x}" for x in errs]
        if not errs:
            metrics[f.name] = analyze(d)
    if "--json" in argv:
        print(json.dumps({"errors": errors, "metrics": metrics}, ensure_ascii=False, indent=2))
    elif errors:
        print(f"INTEGRATION-TRACE: {len(errors)} нарушений:")
        for x in errors:
            print(f"  - {x}")
    else:
        print(f"INTEGRATION-TRACE-OK: {len(files)} трейсов; metrics={metrics}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
