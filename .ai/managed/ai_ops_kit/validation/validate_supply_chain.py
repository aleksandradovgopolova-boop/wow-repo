#!/usr/bin/env python3
"""Validate SupplyChainPinPolicy (security-долг перед расширением автономности, OWASP ASI).

Инвариант: внешние модели/skills/MCP ставятся ТОЛЬКО по зафиксированному revision/hash — плавающие
ссылки (latest/main/master/HEAD/edge/stable/*) запрещены; исполняемый код (skill/mcp) требует
install-верификации (sha256/signature/sigstore). Без такой политики write-capable MCP и самоизменение
включать нельзя.

  validate_supply_chain.py [examples/supply-chain-demo/SCP-001.yaml]
  validate_supply_chain.py --selftest
Возврат 0 — ок, 1 — ошибки.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
DEFAULT = PKG / "examples" / "supply-chain-demo" / "SCP-001.yaml"

FLOATING = {"latest", "main", "master", "head", "edge", "stable", "dev", "nightly", "*", ""}
KINDS = {"model", "skill", "mcp"}
PIN_TYPES = {"revision", "hash", "digest", "tag-immutable"}
VERIFY_METHODS = {"sha256", "signature", "sigstore"}
_HEXISH = re.compile(r"^[0-9a-fA-F]{7,}$")


def check(data):
    e = []
    if not isinstance(data, dict):
        return ["SupplyChainPinPolicy не объект"]
    if data.get("schema_version") != 1:
        e.append("schema_version должен быть 1")
    if data.get("kind") != "SupplyChainPinPolicy":
        e.append("kind должен быть SupplyChainPinPolicy")
    if not str(data.get("policy_id", "")).startswith("SCP-"):
        e.append("policy_id должен быть SCP-NNN")
    deps = data.get("dependencies")
    if not isinstance(deps, list) or not deps:
        return e + ["dependencies непустой список обязателен"]
    seen = set()
    for d in deps:
        if not isinstance(d, dict):
            e.append("dependency не объект"); continue
        name = d.get("name") or "?"
        if name in seen:
            e.append(f"дубликат зависимости: {name}")
        seen.add(name)
        if d.get("kind") not in KINDS:
            e.append(f"{name}: kind ∉ {sorted(KINDS)}")
        if not d.get("source"):
            e.append(f"{name}: нет source")
        pin = d.get("pinned")
        if not isinstance(pin, dict):
            e.append(f"{name}: нет pinned {{type,value}} — плавающая установка запрещена");
        else:
            if pin.get("type") not in PIN_TYPES:
                e.append(f"{name}: pinned.type ∉ {sorted(PIN_TYPES)}")
            val = str(pin.get("value", "")).strip()
            if val.lower() in FLOATING:
                e.append(f"{name}: pinned.value плавающий ('{val}') — нужен конкретный revision/hash")
            elif pin.get("type") in ("hash", "digest") and not _HEXISH.match(val.split(":")[-1]):
                e.append(f"{name}: pinned {pin.get('type')} '{val}' не похож на hex-hash")
            elif len(val) < 7:
                e.append(f"{name}: pinned.value слишком короткий (не доказывает ревизию)")
        # исполняемый код (skill/mcp) обязан иметь install-верификацию
        if d.get("kind") in ("skill", "mcp"):
            iv = d.get("install_verify")
            if not isinstance(iv, dict):
                e.append(f"{name}: {d.get('kind')} без install_verify (sha256/signature обязательна для кода)")
            elif iv.get("method") not in VERIFY_METHODS:
                e.append(f"{name}: install_verify.method ∉ {sorted(VERIFY_METHODS)}")
            elif not str(iv.get("value", "")).strip():
                e.append(f"{name}: install_verify без value")
    return e


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    good = {"schema_version": 1, "kind": "SupplyChainPinPolicy", "policy_id": "SCP-001",
            "dependencies": [
                {"name": "claude-sonnet", "kind": "model", "source": "anthropic",
                 "pinned": {"type": "revision", "value": "claude-sonnet-5-20260101"}},
                {"name": "figma-mcp", "kind": "mcp", "source": "github.com/x/figma-mcp",
                 "pinned": {"type": "hash", "value": "a1b2c3d4e5f6"},
                 "install_verify": {"method": "sha256", "value": "deadbeefdeadbeef"}}]}
    expect("валидная SCP проходит", check(good) == [])
    expect("плавающий latest -> ошибка",
           any("плавающ" in x for x in check({**good, "dependencies": [
               {"name": "m", "kind": "model", "source": "s", "pinned": {"type": "revision", "value": "latest"}}]})))
    expect("mcp без install_verify -> ошибка",
           any("install_verify" in x for x in check({**good, "dependencies": [
               {"name": "srv", "kind": "mcp", "source": "s", "pinned": {"type": "hash", "value": "abcdef1"}}]})))
    expect("hash не hex -> ошибка",
           any("hex-hash" in x for x in check({**good, "dependencies": [
               {"name": "m", "kind": "model", "source": "s", "pinned": {"type": "hash", "value": "not-a-hash!"}}]})))
    expect("нет pinned -> ошибка",
           any("pinned" in x for x in check({**good, "dependencies": [
               {"name": "m", "kind": "model", "source": "s"}]})))
    expect("дубликат зависимости -> ошибка",
           any("дубликат" in x for x in check({**good, "dependencies": good["dependencies"] + [good["dependencies"][0]]})))

    if DEFAULT.exists():
        errs = check(yaml.safe_load(DEFAULT.read_text(encoding="utf-8")))
        expect(f"реальный {DEFAULT.name} валиден", errs == [])
        if errs:
            for x in errs:
                print("   -", x)

    print("validate_supply_chain selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    path = Path(args[0]) if args else DEFAULT
    if not path.exists():
        print(f"нет файла: {path}"); return 1
    errs = check(yaml.safe_load(path.read_text(encoding="utf-8")))
    if errs:
        print(f"SUPPLY-CHAIN-PIN {path.name}: ошибки:")
        for x in errs:
            print(f"  - {x}")
        return 1
    print(f"SUPPLY-CHAIN-PIN-OK: {path.name} валиден.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
