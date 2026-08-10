#!/usr/bin/env python3
"""Проверка ProviderResidencyPolicy (PRP) — v3.4 Security, Permissions & Economics.

PRP (schemas/provider-residency-policy.schema.json) — enforceable data-residency/retention. Валидатор:

  1. schema_version=1, kind=PRP; id формата PRP-NNN; default_deny=true (allow-list);
  2. ПОКРЫТИЕ: правило на КАЖДЫЙ класс данных (public/internal/confidential/secret), ровно одно;
  3. allowed_provider_classes ⊆ {external-cloud,ru-cloud,on-premise,configurable}; max_retention ∈
     {zero,ephemeral,standard};
  4. ЖЁСТКИЕ ИНВАРИАНТЫ:
     - secret: allowed ⊆ {on-premise} (секреты НИКОГДА не в облако) И max_retention=zero;
     - confidential: 'external-cloud' НЕ в allowed И max_retention ∈ {zero,ephemeral}.
  5. Кросс-проверка (route_allowed) против registry/providers.yaml: маршрут data_class→provider
     разрешён только если confidentiality_class провайдера в allowed для класса.

Использование:  validate_provider_residency.py [<prp.(yaml|json)>|<dir>] [--json] | --selftest
"""
import json
import re
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
SCHEMA = PKG / "schemas" / "provider-residency-policy.schema.json"
DEMO = PKG / "examples" / "residency-demo"
PROVIDERS = PKG / "registry" / "providers.yaml"
DATA_CLASSES = {"public", "internal", "confidential", "secret"}
PROVIDER_CLASSES = {"external-cloud", "ru-cloud", "on-premise", "configurable"}
RETENTION = {"zero", "ephemeral", "standard"}
_ID = re.compile(r"^PRP-[0-9]{3,}$")


def check(data: dict):
    e = []
    if not isinstance(data, dict):
        return ["PRP не объект"]
    if data.get("schema_version") != 1:
        e.append("schema_version должен быть 1")
    if data.get("kind") != "ProviderResidencyPolicy":
        e.append("kind должен быть 'ProviderResidencyPolicy'")
    if not (isinstance(data.get("id"), str) and _ID.match(data["id"])):
        e.append("id должен быть формата PRP-NNN")
    if data.get("default_deny") is not True:
        e.append("ИНВАРИАНТ: default_deny должен быть true")

    rules = data.get("rules")
    seen = {}
    if not (isinstance(rules, list) and rules):
        e.append("rules — непустой список")
        rules = []
    for r in rules:
        if not isinstance(r, dict):
            e.append("rule не объект")
            continue
        dc = r.get("data_class")
        if dc not in DATA_CLASSES:
            e.append(f"data_class '{dc}' ∉ {sorted(DATA_CLASSES)}")
        seen[dc] = seen.get(dc, 0) + 1
        ac = r.get("allowed_provider_classes")
        if not isinstance(ac, list) or any(x not in PROVIDER_CLASSES for x in ac):
            e.append(f"rule[{dc}].allowed_provider_classes — из {sorted(PROVIDER_CLASSES)}")
            ac = []
        ret = r.get("max_retention")
        if ret not in RETENTION:
            e.append(f"rule[{dc}].max_retention ∉ {sorted(RETENTION)}")
        if not isinstance(r.get("human_approval"), bool):
            e.append(f"rule[{dc}].human_approval должен быть bool")
        # жёсткие инварианты
        if dc == "secret":
            if set(ac) - {"on-premise"}:
                e.append("ИНВАРИАНТ: secret allowed_provider_classes ⊆ {on-premise} (секреты не в облако)")
            if ret != "zero":
                e.append("ИНВАРИАНТ: secret требует max_retention=zero")
        if dc == "confidential":
            if "external-cloud" in ac:
                e.append("ИНВАРИАНТ: confidential не может уходить в external-cloud")
            if ret not in ("zero", "ephemeral"):
                e.append("ИНВАРИАНТ: confidential требует max_retention ∈ {zero, ephemeral}")
    for dc in sorted(DATA_CLASSES):
        n = seen.get(dc, 0)
        if n == 0:
            e.append(f"нет правила residency для класса '{dc}'")
        elif n > 1:
            e.append(f"класс '{dc}' имеет >1 правила")
    return e


def route_allowed(policy: dict, data_class: str, provider_conf_class: str):
    """Разрешён ли маршрут data_class -> провайдер с данным confidentiality_class."""
    for r in policy.get("rules", []):
        if r.get("data_class") == data_class:
            return provider_conf_class in (r.get("allowed_provider_classes") or [])
    return False   # deny-by-default


def _provider_classes():
    try:
        data = yaml.safe_load(PROVIDERS.read_text(encoding="utf-8"))
        provs = data.get("providers") or data
        out = {}
        if isinstance(provs, dict):
            for pid, p in provs.items():
                if isinstance(p, dict) and p.get("confidentiality_class"):
                    out[pid] = p["confidentiality_class"]
        return out
    except Exception:
        return {}


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
    if DEMO.is_dir():
        expect("реальный examples/residency-demo целостен",
               all(check(_load(f)) == [] for f in sorted(DEMO.glob("PRP-*.yaml"))))

    def _mut(dc, **over):
        rules = [dict(r) for r in ex["rules"]]
        for r in rules:
            if r["data_class"] == dc:
                r.update(over)
        return {**ex, "rules": rules}

    expect("secret в облако -> ошибка",
           any("секреты не в облако" in x for x in check(_mut("secret", allowed_provider_classes=["external-cloud"]))))
    expect("secret retention != zero -> ошибка",
           any("secret требует max_retention=zero" in x for x in check(_mut("secret",
               allowed_provider_classes=["on-premise"], max_retention="ephemeral"))))
    expect("confidential в external-cloud -> ошибка",
           any("external-cloud" in x for x in check(_mut("confidential",
               allowed_provider_classes=["external-cloud", "ru-cloud"]))))
    expect("confidential retention=standard -> ошибка",
           any("ephemeral" in x for x in check(_mut("confidential", max_retention="standard"))))
    expect("default_deny=false -> ошибка",
           any("default_deny" in x for x in check({**ex, "default_deny": False})))
    expect("класс без правила -> ошибка",
           any("нет правила residency" in x for x in check({**ex, "rules": ex["rules"][:3]})))

    # route_allowed
    expect("route: confidential -> external-cloud запрещён",
           route_allowed(ex, "confidential", "external-cloud") is False)
    expect("route: confidential -> on-premise разрешён",
           route_allowed(ex, "confidential", "on-premise") is True)
    expect("route: secret -> ru-cloud запрещён", route_allowed(ex, "secret", "ru-cloud") is False)

    # кросс-проверка против реального providers.yaml: ни один external-cloud провайдер не принимает secret
    pcs = _provider_classes()
    if pcs:
        bad = [pid for pid, cc in pcs.items()
               if cc != "on-premise" and route_allowed(ex, "secret", cc)]
        expect(f"реальные провайдеры: ни один не-on-premise не принимает secret ({len(pcs)} провайдеров)",
               bad == [])
        ext = [pid for pid, cc in pcs.items() if cc == "external-cloud"]
        expect(f"реальные external-cloud провайдеры не принимают confidential ({len(ext)})",
               all(route_allowed(ex, "confidential", pcs[p]) is False for p in ext))

    print("validate_provider_residency selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    target = Path(args[0]) if args else DEMO
    files = sorted(target.glob("PRP-*.yaml")) if target.is_dir() else [target]
    errors = []
    for f in files:
        errors += [f"{f.name}: {x}" for x in check(_load(f))]
    if "--json" in argv:
        print(json.dumps({"count": len(files), "errors": errors}, ensure_ascii=False, indent=2))
    elif errors:
        print(f"PROVIDER-RESIDENCY: {len(errors)} нарушений:")
        for x in errors:
            print(f"  - {x}")
    else:
        print(f"PROVIDER-RESIDENCY-OK: {len(files)} PRP; секреты не в облако, confidential не в external-cloud.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
