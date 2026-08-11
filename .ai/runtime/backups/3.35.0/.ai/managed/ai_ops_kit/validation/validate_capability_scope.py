#!/usr/bin/env python3
"""Проверка PackageCapabilityScope (PCS) — v3.4 Security, Permissions & Economics.

PCS (schemas/package-capability-scope.schema.json) привязывает пакет WorkGraph к разрешённым
permission-уровням. Валидатор:

  1. schema_version=1, kind=PCS; id формата PCS-NNN; work_graph WG-NNN; package непуст;
  2. allowed_permissions ⊆ {read-only,local,controlled-write,network,execution}, непусто;
  3. LEAST-PRIVILEGE: network -> justification.network непусто; execution -> justification.execution
     непусто (повышенные уровни нельзя выдать молча);
  4. ПОКРЫТИЕ (инвариант фазы «никакой package без capability-scope»): для набора PCS + WorkGraph —
     каждый пакет WG имеет ровно один PCS; PCS не ссылается на пакет вне WG.

Использование:  validate_capability_scope.py [<dir с PCS>] [--json] | --selftest
  (реальный прогон: PCS из каталога + покрытие против examples/work-graph-demo)
"""
import json
import re
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
SCHEMA = PKG / "schemas" / "package-capability-scope.schema.json"
DEMO = PKG / "examples" / "capability-demo"
WG_DEMO = PKG / "examples" / "work-graph-demo" / "work-graph.yaml"
PERMS = {"read-only", "local", "controlled-write", "network", "execution"}
ELEVATED = ("network", "execution")
_ID = re.compile(r"^PCS-[0-9]{3,}$")
_WG = re.compile(r"^WG-[0-9]{3,}$")


def check(data: dict):
    e = []
    if not isinstance(data, dict):
        return ["PCS не объект"]
    if data.get("schema_version") != 1:
        e.append("schema_version должен быть 1")
    if data.get("kind") != "PackageCapabilityScope":
        e.append("kind должен быть 'PackageCapabilityScope'")
    if not (isinstance(data.get("id"), str) and _ID.match(data["id"])):
        e.append("id должен быть формата PCS-NNN")
    if not (isinstance(data.get("work_graph"), str) and _WG.match(data["work_graph"])):
        e.append("work_graph должен быть WG-NNN")
    if not (isinstance(data.get("package"), str) and data["package"].strip()):
        e.append("package обязателен и непуст")
    perms = data.get("allowed_permissions")
    if not (isinstance(perms, list) and perms):
        e.append("allowed_permissions — непустой список")
        perms = []
    for p in perms:
        if p not in PERMS:
            e.append(f"allowed_permissions: неизвестный уровень '{p}'")
    just = data.get("justification") or {}
    if not isinstance(just, dict):
        e.append("justification должен быть объектом")
        just = {}
    for lvl in ELEVATED:
        if lvl in perms and not (isinstance(just.get(lvl), str) and just[lvl].strip()):
            e.append(f"least-privilege: уровень '{lvl}' требует justification.{lvl} (нельзя выдать молча)")
    ha = data.get("human_approval_for")
    if ha is not None and not (isinstance(ha, list) and all(isinstance(x, str) for x in ha)):
        e.append("human_approval_for должен быть списком строк")
    return e


def check_coverage(wg: dict, pcs_list: list):
    """Инвариант «никакой package без capability-scope»: каждый пакет WG имеет ровно один PCS."""
    e = []
    wid = wg.get("id")
    pkg_ids = {p.get("id") for p in wg.get("packages", []) if isinstance(p, dict) and p.get("id")}
    by_pkg = {}
    for pcs in pcs_list:
        if pcs.get("work_graph") != wid:
            continue
        pk = pcs.get("package")
        if pk not in pkg_ids:
            e.append(f"{pcs.get('id')} ссылается на пакет '{pk}' вне {wid}")
        by_pkg.setdefault(pk, []).append(pcs.get("id"))
    for pk in sorted(pkg_ids):
        n = len(by_pkg.get(pk, []))
        if n == 0:
            e.append(f"пакет '{pk}' в {wid} БЕЗ capability-scope (инвариант нарушен)")
        elif n > 1:
            e.append(f"пакет '{pk}' в {wid} имеет >1 PCS ({by_pkg[pk]})")
    return e


def _load(p: Path):
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def _load_dir(d: Path):
    return [_load(f) for f in sorted(Path(d).glob("PCS-*.yaml"))] if Path(d).is_dir() else []


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    ex = json.loads(SCHEMA.read_text(encoding="utf-8"))["examples"][0]
    expect("пример из схемы валиден", check(ex) == [])
    expect("network без justification -> ошибка (least-privilege)",
           any("network" in x for x in check({**ex,
               "allowed_permissions": ["read-only", "network"]})))
    expect("network c justification -> валиден",
           check({**ex, "allowed_permissions": ["read-only", "network"],
                  "justification": {"network": "внешний вызов платёжного API"}}) == [])
    expect("execution без justification -> ошибка",
           any("execution" in x for x in check({**ex,
               "allowed_permissions": ["execution"]})))
    expect("неизвестный уровень -> ошибка",
           any("неизвестный уровень" in x for x in check({**ex, "allowed_permissions": ["god-mode"]})))
    expect("битый id -> ошибка", any("id" in x for x in check({**ex, "id": "PCS1"})))

    # покрытие
    wg = {"id": "WG-001", "packages": [{"id": "api"}, {"id": "ui"}, {"id": "wiring"}]}
    full = [{"id": "PCS-001", "work_graph": "WG-001", "package": "api"},
            {"id": "PCS-002", "work_graph": "WG-001", "package": "ui"},
            {"id": "PCS-003", "work_graph": "WG-001", "package": "wiring"}]
    expect("полное покрытие пакетов -> ок", check_coverage(wg, full) == [])
    expect("пакет без PCS -> нарушение инварианта",
           any("БЕЗ capability-scope" in x for x in check_coverage(wg, full[:2])))
    expect("PCS на пакет вне WG -> ошибка",
           any("вне WG-001" in x for x in check_coverage(wg,
               full + [{"id": "PCS-009", "work_graph": "WG-001", "package": "ghost"}])))
    expect("дубль PCS на пакет -> ошибка",
           any(">1 PCS" in x for x in check_coverage(wg,
               full + [{"id": "PCS-009", "work_graph": "WG-001", "package": "api"}])))

    # реальный демо: PCS структурно валидны + покрывают WG-001
    real = _load_dir(DEMO)
    expect(f"реальные PCS в examples/capability-demo валидны ({len(real)})",
           all(check(p) == [] for p in real) and len(real) >= 1)
    if WG_DEMO.exists() and real:
        expect("реальные PCS покрывают WG-001 (каждый пакет имеет scope)",
               check_coverage(_load(WG_DEMO), real) == [])

    print("validate_capability_scope selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    d = Path(args[0]) if args else DEMO
    pcs_list = _load_dir(d)
    errors = []
    for i, p in enumerate(pcs_list):
        errors += [f"{sorted(Path(d).glob('PCS-*.yaml'))[i].name}: {x}" for x in check(p)]
    if not errors and WG_DEMO.exists():
        errors += check_coverage(_load(WG_DEMO), pcs_list)
    if "--json" in argv:
        print(json.dumps({"count": len(pcs_list), "errors": errors}, ensure_ascii=False, indent=2))
    elif errors:
        print(f"CAPABILITY-SCOPE: {len(errors)} нарушений:")
        for x in errors:
            print(f"  - {x}")
    else:
        print(f"CAPABILITY-SCOPE-OK: {len(pcs_list)} PCS; каждый пакет WorkGraph имеет scope.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
