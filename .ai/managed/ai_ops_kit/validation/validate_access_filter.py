#!/usr/bin/env python3
"""Проверка AccessFilterPolicy (AFP) — v3.4 Security, Permissions & Economics.

AFP (schemas/access-filter-policy.schema.json) — фильтр доступа ДО retrieval. Валидатор:

  1. schema_version=1, kind=AFP; id формата AFP-NNN; context_architecture CAD-NNN (слабая ссылка);
  2. ИНВАРИАНТ filter_stage=before_retrieval (пост-фильтрация запрещена);
  3. ИНВАРИАНТ default_deny=true (allow-list; deny-by-default);
  4. ПОКРЫТИЕ (инвариант «никакой retrieval без access-filter»): правило есть для КАЖДОЙ роли
     (planner/executor/ui_reviewer/security_reviewer/integration), ровно одно на роль;
  5. allowed_classes ⊆ объявленных data_classes;
  6. ИНВАРИАНТ секретов: класс 'secret' НЕ допускается ни в одном allowed_classes (секреты никогда
     не входят в context); secret_handling непуст.

Использование:  validate_access_filter.py [<afp.(yaml|json)>|<dir>] [--json] | --selftest
"""
import json
import re
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
SCHEMA = PKG / "schemas" / "access-filter-policy.schema.json"
DEMO = PKG / "examples" / "access-filter-demo"
ROLES = {"planner", "executor", "ui_reviewer", "security_reviewer", "integration"}
CLASSES = {"public", "internal", "confidential", "secret"}
_ID = re.compile(r"^AFP-[0-9]{3,}$")
_CAD = re.compile(r"^CAD-[0-9]{3,}$")


def check(data: dict):
    e = []
    if not isinstance(data, dict):
        return ["AFP не объект"]
    if data.get("schema_version") != 1:
        e.append("schema_version должен быть 1")
    if data.get("kind") != "AccessFilterPolicy":
        e.append("kind должен быть 'AccessFilterPolicy'")
    if not (isinstance(data.get("id"), str) and _ID.match(data["id"])):
        e.append("id должен быть формата AFP-NNN")
    if not (isinstance(data.get("context_architecture"), str) and _CAD.match(data["context_architecture"])):
        e.append("context_architecture должен быть CAD-NNN")
    dc = data.get("data_classes")
    if not (isinstance(dc, list) and dc and all(x in CLASSES for x in dc)):
        e.append(f"data_classes — непустой список из {sorted(CLASSES)}")
        dc = []
    if data.get("filter_stage") != "before_retrieval":
        e.append("ИНВАРИАНТ: filter_stage должен быть 'before_retrieval' (пост-фильтрация запрещена)")
    if data.get("default_deny") is not True:
        e.append("ИНВАРИАНТ: default_deny должен быть true (deny-by-default allow-list)")

    rules = data.get("rules")
    seen_roles = {}
    if not (isinstance(rules, list) and rules):
        e.append("rules — непустой список")
        rules = []
    for r in rules:
        if not isinstance(r, dict):
            e.append("rule не объект")
            continue
        role = r.get("role")
        if role not in ROLES:
            e.append(f"rule.role '{role}' ∉ {sorted(ROLES)}")
        seen_roles[role] = seen_roles.get(role, 0) + 1
        ac = r.get("allowed_classes")
        if not isinstance(ac, list) or any(x not in CLASSES for x in ac):
            e.append(f"rule[{role}].allowed_classes — список из {sorted(CLASSES)}")
            ac = []
        for c in ac:
            if dc and c not in dc:
                e.append(f"rule[{role}]: класс '{c}' не объявлен в data_classes")
        if "secret" in ac:
            e.append(f"ИНВАРИАНТ секретов: rule[{role}] допускает 'secret' — секреты не входят в context")
    # покрытие всех ролей ровно одним правилом
    for role in sorted(ROLES):
        n = seen_roles.get(role, 0)
        if n == 0:
            e.append(f"нет правила доступа для роли '{role}' (никакой retrieval без access-filter)")
        elif n > 1:
            e.append(f"роль '{role}' имеет >1 правила доступа")

    if not (isinstance(data.get("secret_handling"), str) and data["secret_handling"].strip()):
        e.append("secret_handling обязателен и непуст")
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
    expect("пример из схемы валиден", check(ex) == [])
    expect("реальный examples/access-filter-demo целостен",
           all(check(_load(f)) == [] for f in sorted(DEMO.glob("AFP-*.yaml"))) if DEMO.is_dir() else True)
    expect("filter_stage != before_retrieval -> ошибка",
           any("before_retrieval" in x for x in check({**ex, "filter_stage": "after"})))
    expect("default_deny=false -> ошибка",
           any("default_deny" in x for x in check({**ex, "default_deny": False})))
    # роль без правила
    less = {**ex, "rules": ex["rules"][:4]}
    expect("роль без правила доступа -> ошибка (никакой retrieval без filter)",
           any("нет правила доступа" in x for x in check(less)))
    # secret в allowed
    sec = {**ex, "rules": [{**ex["rules"][0], "allowed_classes": ["public", "secret"]}] + ex["rules"][1:]}
    expect("secret в allowed_classes -> ошибка (секреты не в context)",
           any("секреты не входят" in x for x in check(sec)))
    # класс не объявлен
    und = {**ex, "data_classes": ["public", "internal"],
           "rules": [{"role": "planner", "allowed_classes": ["confidential"]}] + [
               {"role": r, "allowed_classes": ["public"]} for r in
               ["executor", "ui_reviewer", "security_reviewer", "integration"]]}
    expect("класс вне data_classes -> ошибка",
           any("не объявлен в data_classes" in x for x in check(und)))
    expect("битый context_architecture -> ошибка",
           any("context_architecture" in x for x in check({**ex, "context_architecture": "CAD1"})))
    expect("дубль правила на роль -> ошибка",
           any(">1 правила" in x for x in check({**ex, "rules": ex["rules"] + [ex["rules"][0]]})))

    print("validate_access_filter selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    target = Path(args[0]) if args else DEMO
    files = sorted(target.glob("AFP-*.yaml")) if target.is_dir() else [target]
    errors = []
    for f in files:
        errors += [f"{f.name}: {x}" for x in check(_load(f))]
    if "--json" in argv:
        print(json.dumps({"count": len(files), "errors": errors}, ensure_ascii=False, indent=2))
    elif errors:
        print(f"ACCESS-FILTER: {len(errors)} нарушений:")
        for x in errors:
            print(f"  - {x}")
    else:
        print(f"ACCESS-FILTER-OK: {len(files)} AFP; retrieval под deny-by-default фильтром, секреты вне context.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
