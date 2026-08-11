#!/usr/bin/env python3
"""Проверка BudgetContract — v3.4 Security, Permissions & Economics.

BudgetContract (schemas/budget-contract.schema.json) — декларативный бюджет scope с привязкой к
рантайм-энфорсеру. Валидатор:

  1. schema_version=1, kind=BudgetContract; id формата BUD-NNN; scope ∈ 4-enum;
  2. ИНВАРИАНТ «никакой scope без бюджета»: limits несут ХОТЯ БЫ ОДНУ границу (не-null), все положительны;
  3. on_exhaustion ∈ fail_closed|escalate|degrade; hard=true НЕСОВМЕСТИМ с degrade (жёсткий потолок не
     может тихо «деградировать-продолжать» — только стоп/эскалация);
  4. scope_ref согласован со scope: loop -> LP-NNN, work_graph -> WG-NNN (слабая ссылка формата);
  5. accounting и enforced_by непусты (экономика ДОЛЖНА быть enforced, не только объявлена);
  6. реестр examples/budget-demo (если каталог): id уникальны, имя файла == id.

Использование:  validate_budget_contract.py [<file|dir>] [--json] | --selftest
"""
import json
import re
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
SCHEMA = PKG / "schemas" / "budget-contract.schema.json"
DEMO = PKG / "examples" / "budget-demo"
SCOPE = {"run", "loop", "package", "work_graph"}
ON_EXH = {"fail_closed", "escalate", "degrade"}
_ID = re.compile(r"^BUD-[0-9]{3,}$")
_LP = re.compile(r"^LP-[0-9]{3,}$")
_WG = re.compile(r"^WG-[0-9]{3,}$")
_INT_LIMITS = ("max_model_calls", "max_iterations", "max_tokens", "max_wall_seconds")


def check(data: dict):
    e = []
    if not isinstance(data, dict):
        return ["BudgetContract не объект"]
    if data.get("schema_version") != 1:
        e.append("schema_version должен быть 1")
    if data.get("kind") != "BudgetContract":
        e.append("kind должен быть 'BudgetContract'")
    if not (isinstance(data.get("id"), str) and _ID.match(data["id"])):
        e.append("id должен быть формата BUD-NNN")
    scope = data.get("scope")
    if scope not in SCOPE:
        e.append(f"scope ∉ {sorted(SCOPE)}")
    ref = data.get("scope_ref")
    if not (isinstance(ref, str) and ref.strip()):
        e.append("scope_ref обязателен и непуст")
    else:
        if scope == "loop" and not _LP.match(ref):
            e.append("scope=loop требует scope_ref формата LP-NNN")
        if scope == "work_graph" and not _WG.match(ref):
            e.append("scope=work_graph требует scope_ref формата WG-NNN")

    lim = data.get("limits")
    if not isinstance(lim, dict):
        e.append("limits обязателен")
    else:
        vals = [lim.get(k) for k in _INT_LIMITS] + [lim.get("max_cost_usd")]
        if all(v is None for v in vals):
            e.append("limits: нужна хотя бы одна граница (никакой scope без бюджета)")
        for k in _INT_LIMITS:
            v = lim.get(k)
            if v is not None and not (isinstance(v, int) and not isinstance(v, bool) and v >= 1):
                e.append(f"limits.{k} должен быть целым ≥1 или null")
        c = lim.get("max_cost_usd")
        if c is not None and not (isinstance(c, (int, float)) and not isinstance(c, bool) and c >= 0):
            e.append("limits.max_cost_usd должен быть числом ≥0 или null")

    oe = data.get("on_exhaustion")
    if oe not in ON_EXH:
        e.append(f"on_exhaustion ∉ {sorted(ON_EXH)}")
    if not isinstance(data.get("hard"), bool):
        e.append("hard должен быть bool")
    elif data.get("hard") is True and oe == "degrade":
        e.append("hard=true несовместим с on_exhaustion=degrade (жёсткий потолок не деградирует-продолжает)")

    for f in ("accounting", "enforced_by"):
        if not (isinstance(data.get(f), str) and data[f].strip()):
            e.append(f"{f} обязателен и непуст (экономика должна быть enforced, не только объявлена)")
    return e


def check_registry(d: Path):
    errors, ids = [], set()
    for f in sorted(Path(d).glob("BUD-*.yaml")) if Path(d).is_dir() else []:
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
        except yaml.YAMLError as ex:
            errors.append(f"{f.name}: не парсится YAML ({ex})")
            continue
        for x in check(data):
            errors.append(f"{f.name}: {x}")
        aid = (data or {}).get("id")
        if isinstance(aid, str):
            if f.stem != aid:
                errors.append(f"{f.name}: имя файла != id ({aid})")
            if aid in ids:
                errors.append(f"дубликат id {aid}")
            ids.add(aid)
    return errors, ids


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    ex = json.loads(SCHEMA.read_text(encoding="utf-8"))["examples"][0]
    expect("пример из схемы валиден", check(ex) == [])
    expect("реальный examples/budget-demo целостен",
           check_registry(DEMO)[0] == [])
    expect("limits без единой границы -> ошибка",
           any("хотя бы одна граница" in x for x in check({**ex,
               "limits": {"max_model_calls": None, "max_iterations": None, "max_tokens": None,
                          "max_cost_usd": None, "max_wall_seconds": None}})))
    expect("hard=true + degrade -> ошибка",
           any("degrade" in x for x in check({**ex, "hard": True, "on_exhaustion": "degrade"})))
    expect("hard=false + degrade -> валиден",
           check({**ex, "hard": False, "on_exhaustion": "degrade"}) == [])
    expect("scope=loop с не-LP ref -> ошибка",
           any("LP-NNN" in x for x in check({**ex, "scope": "loop", "scope_ref": "loop1"})))
    expect("scope=work_graph требует WG-NNN",
           any("WG-NNN" in x for x in check({**ex, "scope": "work_graph", "scope_ref": "x"})))
    expect("пустой enforced_by -> ошибка (экономика должна быть enforced)",
           any("enforced_by" in x for x in check({**ex, "enforced_by": " "})))
    expect("битый id -> ошибка", any("id" in x for x in check({**ex, "id": "BUD1"})))
    expect("enum on_exhaustion == схема",
           set(json.loads(SCHEMA.read_text(encoding="utf-8"))["properties"]["on_exhaustion"]["enum"]) == ON_EXH)

    print("validate_budget_contract selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    target = Path(args[0]) if args else DEMO
    if target.is_dir():
        errors, ids = check_registry(target)
        label = f"{len(ids)} BudgetContract"
    else:
        errors = check(yaml.safe_load(target.read_text(encoding="utf-8")))
        label = target.name
    if "--json" in argv:
        print(json.dumps({"errors": errors}, ensure_ascii=False, indent=2))
    elif errors:
        print(f"BUDGET-CONTRACT: {len(errors)} нарушений:")
        for x in errors:
            print(f"  - {x}")
    else:
        print(f"BUDGET-CONTRACT-OK: {label}, инвариант бюджета соблюдён.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
