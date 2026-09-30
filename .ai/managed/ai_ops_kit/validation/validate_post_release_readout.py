#!/usr/bin/env python3
"""Проверка PostReleaseReadout (PRR) — v3.5 Observability.

PRR (schemas/post-release-readout.schema.json) замыкает петлю доставили→измерили→узнали. Валидатор:

  1. структура; id PRR-NNN; downstream_ci.status ∈ pass|fail|pending|not_run; product_health.band ∈
     healthy|warning|critical|not_measured; readout_decision ∈ 4-enum; learning FL-NNN|null;
  2. ИНВАРИАНТ: читаем только ВЕРИФИЦИРОВАННУЮ доставку — delivery_receipt.sha_verified=true;
  3. СОГЛАСОВАННОСТЬ решения:
     - healthy_continue ТРЕБУЕТ downstream=pass И health≠critical И promise_broken=0;
     - rollback ТРЕБУЕТ негативного сигнала (downstream=fail ∨ health=critical ∨ promise_broken>0);
     - promise_broken>0 ⟹ решение НЕ healthy_continue (сломанное обещание нельзя «продолжать как есть»).

Использование:  validate_post_release_readout.py [<prr.(yaml|json)>|<dir>] [--json] | --selftest
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
SCHEMA = PKG / "schemas" / "post-release-readout.schema.json"
DEMO = PKG / "examples" / "readout-demo"
CI = {"pass", "fail", "pending", "not_run"}
BAND = {"healthy", "warning", "critical", "not_measured"}
DECISION = {"healthy_continue", "watch", "rollback", "investigate"}
_ID = re.compile(r"^PRR-[0-9]{3,}$")
_FL = re.compile(r"^FL-[0-9]{3,}$")


def check(data: dict):
    e = []
    if not isinstance(data, dict):
        return ["PRR не объект"]
    if data.get("schema_version") != 1 or data.get("kind") != "PostReleaseReadout":
        e.append("schema_version=1 + kind=PostReleaseReadout обязательны")
    if not (isinstance(data.get("id"), str) and _ID.match(data["id"])):
        e.append("id должен быть формата PRR-NNN")

    dr = data.get("delivery_receipt")
    if not isinstance(dr, dict) or not (isinstance(dr.get("ref"), str) and dr["ref"].strip()):
        e.append("delivery_receipt.ref обязателен и непуст")
    if not (isinstance(dr, dict) and dr.get("sha_verified") is True):
        e.append("ИНВАРИАНТ: readout только по ВЕРИФИЦИРОВАННОЙ доставке (sha_verified=true)")

    ci = (data.get("downstream_ci") or {}).get("status")
    if ci not in CI:
        e.append(f"downstream_ci.status ∉ {sorted(CI)}")
    band = (data.get("product_health") or {}).get("band")
    if band not in BAND:
        e.append(f"product_health.band ∉ {sorted(BAND)}")
    ev = data.get("evolution") or {}
    pb = ev.get("promise_broken")
    if not (isinstance(pb, int) and not isinstance(pb, bool) and pb >= 0):
        e.append("evolution.promise_broken — целое ≥0")
        pb = 0
    if not (isinstance(ev.get("cost_realized"), int) and not isinstance(ev.get("cost_realized"), bool)
            and ev.get("cost_realized") >= 0):
        e.append("evolution.cost_realized — целое ≥0")
    dec = data.get("readout_decision")
    if dec not in DECISION:
        e.append(f"readout_decision ∉ {sorted(DECISION)}")
    lr = data.get("learning")
    if lr is not None and not (isinstance(lr, str) and _FL.match(lr)):
        e.append("learning должен быть FL-NNN или null")

    negative = (ci == "fail") or (band == "critical") or (pb and pb > 0)
    if dec == "healthy_continue":
        if ci != "pass":
            e.append("healthy_continue требует downstream_ci=pass")
        if band == "critical":
            e.append("healthy_continue несовместим с product_health=critical")
        if pb and pb > 0:
            e.append("healthy_continue несовместим с promise_broken>0")
    if dec == "rollback" and not negative:
        e.append("rollback требует негативного сигнала (downstream fail / health critical / promise_broken)")
    if pb and pb > 0 and dec == "healthy_continue":
        e.append("promise_broken>0 -> решение не может быть healthy_continue")
    return e


def _load(p: Path):
    t = p.read_text(encoding="utf-8")
    return yaml.safe_load(t) if p.suffix in (".yaml", ".yml") else json.loads(t)


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    target = Path(args[0]) if args else DEMO
    files = sorted(target.glob("PRR-*.yaml")) if target.is_dir() else [target]
    errors = []
    for f in files:
        errors += [f"{f.name}: {x}" for x in check(_load(f))]
    if "--json" in argv:
        print(json.dumps({"count": len(files), "errors": errors}, ensure_ascii=False, indent=2))
    elif errors:
        print(f"POST-RELEASE-READOUT: {len(errors)} нарушений:")
        for x in errors:
            print(f"  - {x}")
    else:
        print(f"POST-RELEASE-READOUT-OK: {len(files)} readout; решения согласованы с сигналами.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
