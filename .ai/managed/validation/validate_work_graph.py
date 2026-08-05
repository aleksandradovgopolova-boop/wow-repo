#!/usr/bin/env python3
"""Проверка parallel-architecture триады — v3.3.2 Operational Architecture Backbone.

Покрывает три контракта одним валидатором (они взаимозависимы: PSD и IP выводятся из WorkGraph):
  - WorkGraph (schemas/work-graph.schema.json);
  - ParallelSafetyDecision (schemas/parallel-safety-decision.schema.json);
  - IntegrationPlan (schemas/integration-plan.schema.json).

Структура + семантика + КРОСС-консистентность:
  1. depends_on резолвятся в id пакетов; нет само-зависимости; integration_order — перестановка
     пакетов И топологична (пакет после всех своих depends_on) => DAG (цикл невозможен);
  2. PSD/IP ссылаются на этот WorkGraph (work_graph == wg.id);
  3. PSD: классификация safe=true для ≥2 пакетов ТРЕБУЕТ непересекающихся write_scope и отсутствия
     зависимости между ними (иначе parallel_safe непоследователен с WorkGraph);
  4. IP: requires_new_integration_sha ОБЯЗАН быть true (инвариант: package-SHA не доказывают всю
     работу — после fan-in новый integration-SHA + повтор проверок); integration_order топологичен.

Использование:  validate_work_graph.py <dir с work-graph.yaml/psd/ip> [--json] | --selftest
"""
import json
import re
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
DEMO = PKG / "examples" / "work-graph-demo"
MODE = {"single", "sequential", "parallel", "hybrid"}
VERDICT = {"parallel_safe", "needs_sequential", "hybrid"}
_WG = re.compile(r"^WG-[0-9]{3,}$")
_PSD = re.compile(r"^PSD-[0-9]{3,}$")
_IP = re.compile(r"^IP-[0-9]{3,}$")


def _prefix(glob: str) -> str:
    return (glob or "").split("*")[0].rstrip("/")


def _overlap(a_scopes, b_scopes) -> bool:
    for a in a_scopes or []:
        aa = _prefix(a) + "/"
        for b in b_scopes or []:
            bb = _prefix(b) + "/"
            if aa.startswith(bb) or bb.startswith(aa):
                return True
    return False


def check_wg(wg: dict):
    e = []
    if wg.get("schema_version") != 1 or wg.get("kind") != "WorkGraph":
        e.append("WorkGraph: schema_version=1 + kind=WorkGraph обязательны")
    if not (isinstance(wg.get("id"), str) and _WG.match(wg["id"])):
        e.append("WorkGraph.id должен быть WG-NNN")
    if not (isinstance(wg.get("feature"), str) and wg["feature"].strip()):
        e.append("WorkGraph.feature непуст")
    if wg.get("execution_mode") not in MODE:
        e.append(f"WorkGraph.execution_mode ∉ {sorted(MODE)}")
    pkgs = wg.get("packages")
    ids = []
    if not isinstance(pkgs, list) or not pkgs:
        e.append("WorkGraph.packages непуст")
        pkgs = []
    for p in pkgs:
        if not isinstance(p, dict) or not p.get("id"):
            e.append("package требует id")
            continue
        ids.append(p["id"])
        if not (isinstance(p.get("write_scope"), list) and p["write_scope"]):
            e.append(f"package {p['id']}: write_scope непуст")
    if len(set(ids)) != len(ids):
        e.append("WorkGraph: дубликаты id пакетов")
    idset = set(ids)
    for p in pkgs:
        if not isinstance(p, dict):
            continue
        for d in p.get("depends_on", []) or []:
            if d == p.get("id"):
                e.append(f"package {p['id']}: само-зависимость")
            elif d not in idset:
                e.append(f"package {p['id']}.depends_on -> несуществующий {d}")
    order = wg.get("integration_order") or []
    if set(order) != idset:
        e.append("WorkGraph.integration_order должен быть перестановкой пакетов")
    else:
        pos = {pid: i for i, pid in enumerate(order)}
        for p in pkgs:
            for d in p.get("depends_on", []) or []:
                if d in pos and pos[d] >= pos[p["id"]]:
                    e.append(f"integration_order не топологичен: {p['id']} раньше своей зависимости {d}")
    if not (isinstance(wg.get("aggregate_verification"), list) and wg["aggregate_verification"]):
        e.append("WorkGraph.aggregate_verification непуст")
    return e


def check_psd(psd: dict):
    e = []
    if psd.get("schema_version") != 1 or psd.get("kind") != "ParallelSafetyDecision":
        e.append("PSD: schema_version=1 + kind обязательны")
    if not (isinstance(psd.get("id"), str) and _PSD.match(psd["id"])):
        e.append("PSD.id должен быть PSD-NNN")
    if not (isinstance(psd.get("work_graph"), str) and _WG.match(psd["work_graph"])):
        e.append("PSD.work_graph должен быть WG-NNN")
    if psd.get("verdict") not in VERDICT:
        e.append(f"PSD.verdict ∉ {sorted(VERDICT)}")
    cls = psd.get("classifications")
    if not (isinstance(cls, list) and cls):
        e.append("PSD.classifications непуст")
    else:
        for c in cls:
            if not isinstance(c, dict) or not isinstance(c.get("packages"), list) \
                    or not c["packages"] or not isinstance(c.get("safe"), bool) \
                    or not (c.get("reason") or "").strip():
                e.append("PSD.classification: packages[]+safe:bool+reason обязательны")
    if not (isinstance(psd.get("rationale"), str) and psd["rationale"].strip()):
        e.append("PSD.rationale непуст")
    return e


def check_ip(ip: dict):
    e = []
    if ip.get("schema_version") != 1 or ip.get("kind") != "IntegrationPlan":
        e.append("IP: schema_version=1 + kind обязательны")
    if not (isinstance(ip.get("id"), str) and _IP.match(ip["id"])):
        e.append("IP.id должен быть IP-NNN")
    if not (isinstance(ip.get("work_graph"), str) and _WG.match(ip["work_graph"])):
        e.append("IP.work_graph должен быть WG-NNN")
    if not (isinstance(ip.get("integration_order"), list) and ip["integration_order"]):
        e.append("IP.integration_order непуст")
    if not (isinstance(ip.get("fan_in"), str) and ip["fan_in"].strip()):
        e.append("IP.fan_in непуст")
    if not (isinstance(ip.get("aggregate_checks"), list) and ip["aggregate_checks"]):
        e.append("IP.aggregate_checks непуст")
    if ip.get("requires_new_integration_sha") is not True:
        e.append("IP.requires_new_integration_sha ОБЯЗАН быть true (package-SHA не доказывают всю работу)")
    if not isinstance(ip.get("new_base_binding"), bool):
        e.append("IP.new_base_binding должен быть bool")
    return e


def cross_check(wg, psd, ip):
    e = []
    wid = wg.get("id")
    if psd.get("work_graph") != wid:
        e.append(f"PSD.work_graph ({psd.get('work_graph')}) != WorkGraph.id ({wid})")
    if ip.get("work_graph") != wid:
        e.append(f"IP.work_graph ({ip.get('work_graph')}) != WorkGraph.id ({wid})")
    scope = {p["id"]: p.get("write_scope", []) for p in wg.get("packages", []) if isinstance(p, dict) and p.get("id")}
    deps = {p["id"]: set(p.get("depends_on", []) or []) for p in wg.get("packages", []) if isinstance(p, dict) and p.get("id")}
    idset = set(scope)
    for c in psd.get("classifications", []) or []:
        pk = c.get("packages", [])
        for x in pk:
            if x not in idset:
                e.append(f"PSD ссылается на пакет {x} вне WorkGraph")
        if c.get("safe") is True and len([x for x in pk if x in idset]) >= 2:
            valid = [x for x in pk if x in idset]
            for i in range(len(valid)):
                for j in range(i + 1, len(valid)):
                    a, b = valid[i], valid[j]
                    if b in deps.get(a, set()) or a in deps.get(b, set()):
                        e.append(f"PSD parallel-safe непоследователен: {a},{b} связаны depends_on")
                    if _overlap(scope.get(a), scope.get(b)):
                        e.append(f"PSD parallel-safe непоследователен: {a},{b} имеют пересекающиеся write_scope")
    if set(ip.get("integration_order", [])) != idset:
        e.append("IP.integration_order должен быть перестановкой пакетов WorkGraph")
    return e


def _load(p: Path):
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def check_bundle(d: Path):
    wg = _load(d / "work-graph.yaml")
    psd = _load(d / "parallel-safety-decision.yaml")
    ip = _load(d / "integration-plan.yaml")
    errors = []
    errors += [f"WorkGraph: {x}" for x in check_wg(wg)]
    errors += [f"PSD: {x}" for x in check_psd(psd)]
    errors += [f"IP: {x}" for x in check_ip(ip)]
    if not errors:
        errors += cross_check(wg, psd, ip)
    return errors


def selftest():
    import copy
    import tempfile
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    # реальный демо-бандл целостен
    expect("реальный examples/work-graph-demo валиден и кросс-консистентен", check_bundle(DEMO) == [])

    wg = _load(DEMO / "work-graph.yaml")
    psd = _load(DEMO / "parallel-safety-decision.yaml")
    ip = _load(DEMO / "integration-plan.yaml")

    # топология: wiring раньше своей зависимости -> ошибка
    bad = copy.deepcopy(wg)
    bad["integration_order"] = ["wiring", "api", "ui"]
    expect("нетопологичный integration_order -> ошибка",
           any("топологичен" in x for x in check_wg(bad)))

    # depends_on на несуществующий пакет
    bad = copy.deepcopy(wg)
    bad["packages"][2]["depends_on"] = ["ghost"]
    expect("depends_on -> несуществующий пакет -> ошибка",
           any("несуществующий" in x for x in check_wg(bad)))

    # IP без integration-SHA инварианта
    expect("requires_new_integration_sha=false -> ошибка",
           any("requires_new_integration_sha" in x for x in check_ip({**ip, "requires_new_integration_sha": False})))

    # PSD parallel-safe при пересекающихся write_scope -> непоследовательно
    bad_wg = copy.deepcopy(wg)
    bad_wg["packages"][1]["write_scope"] = ["src/api/shared/**"]   # ui теперь пишет в src/api/*
    e = cross_check(bad_wg, psd, ip)
    expect("PSD parallel-safe при пересекающихся write_scope -> ошибка",
           any("пересекающиеся write_scope" in x for x in e))

    # PSD parallel-safe для зависимых пакетов -> непоследовательно
    bad_psd = copy.deepcopy(psd)
    bad_psd["classifications"] = [{"packages": ["api", "wiring"], "safe": True, "reason": "x"}]
    e = cross_check(wg, bad_psd, ip)
    expect("PSD parallel-safe для depends_on-связанных пакетов -> ошибка",
           any("связаны depends_on" in x for x in e))

    # PSD/IP ссылаются на чужой WG
    e = cross_check(wg, {**psd, "work_graph": "WG-999"}, ip)
    expect("PSD.work_graph != WorkGraph.id -> ошибка", any("PSD.work_graph" in x for x in e))

    # write_scope overlap helper
    expect("_overlap: src/api vs src/ui -> нет", _overlap(["src/api/**"], ["src/ui/**"]) is False)
    expect("_overlap: src/ vs src/api -> да", _overlap(["src/**"], ["src/api/**"]) is True)

    print("validate_work_graph selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    d = Path(args[0]) if args else DEMO
    errors = check_bundle(d)
    if "--json" in argv:
        print(json.dumps({"errors": errors}, ensure_ascii=False, indent=2))
    elif errors:
        print(f"WORK-GRAPH: {len(errors)} нарушений:")
        for x in errors:
            print(f"  - {x}")
    else:
        print("WORK-GRAPH-OK: WorkGraph + ParallelSafetyDecision + IntegrationPlan консистентны.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
