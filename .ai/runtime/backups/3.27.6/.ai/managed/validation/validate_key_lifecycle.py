#!/usr/bin/env python3
"""Validate KeyLifecyclePolicy (security-долг #3, OWASP ASI).

Инварианты: каждый ключ имеет TTL>0 (дедлайн ротации) и env_ref (значение — только из env, НЕ в файле);
в политике НЕТ значений ключей (эвристика на секреты); per_agent_identity объявлена ЧЕСТНО —
supported=true требует непустого evidence (нельзя заявлять полноценную идентичность без доказательства).

  validate_key_lifecycle.py [examples/key-lifecycle-demo/KLP-001.yaml] | --selftest
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
DEFAULT = PKG / "examples" / "key-lifecycle-demo" / "KLP-001.yaml"
# эвристика «похоже на секрет-значение» (не имя env, а сам ключ)
_SECRETISH = re.compile(r"sk-[A-Za-z0-9]{12,}|gho_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN")


def check(data):
    e = []
    if not isinstance(data, dict):
        return ["KeyLifecyclePolicy не объект"]
    if data.get("schema_version") != 1:
        e.append("schema_version должен быть 1")
    if data.get("kind") != "KeyLifecyclePolicy":
        e.append("kind должен быть KeyLifecyclePolicy")
    if not str(data.get("policy_id", "")).startswith("KLP-"):
        e.append("policy_id должен быть KLP-NNN")
    keys = data.get("keys")
    if not isinstance(keys, list) or not keys:
        e.append("keys непустой список обязателен")
    else:
        seen = set()
        for k in keys:
            if not isinstance(k, dict):
                e.append("key не объект"); continue
            nm = k.get("name") or "?"
            if nm in seen:
                e.append(f"дубликат ключа: {nm}")
            seen.add(nm)
            if not k.get("env_ref"):
                e.append(f"{nm}: нет env_ref (ключ должен браться из env, не из файла)")
            ttl = k.get("ttl_days")
            if not (isinstance(ttl, int) and ttl > 0):
                e.append(f"{nm}: ttl_days должен быть > 0 (дедлайн ротации)")
            if k.get("rotation_owner") not in ("human", "automated"):
                e.append(f"{nm}: rotation_owner ∈ [human, automated]")
            # значение ключа НЕ должно попадать в политику
            blob = " ".join(str(v) for v in k.values())
            if _SECRETISH.search(blob):
                e.append(f"{nm}: похоже на ЗНАЧЕНИЕ секрета в политике — только env_ref, не сам ключ")
    pai = data.get("per_agent_identity")
    if not isinstance(pai, dict):
        e.append("нет per_agent_identity {supported, note}")
    else:
        if not isinstance(pai.get("supported"), bool):
            e.append("per_agent_identity.supported должен быть bool")
        if not pai.get("note"):
            e.append("per_agent_identity.note обязателен (честная фиксация состояния)")
        if pai.get("supported") is True and not (pai.get("evidence")):
            e.append("per_agent_identity.supported=true БЕЗ evidence — нельзя заявлять идентичность без доказательства")
    return e


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    base = {"schema_version": 1, "kind": "KeyLifecyclePolicy", "policy_id": "KLP-001",
            "keys": [{"name": "anthropic", "env_ref": "ANTHROPIC_API_KEY", "ttl_days": 90, "rotation_owner": "human"}],
            "per_agent_identity": {"supported": False, "note": "единый ключ на движок; per-agent identity пока нет"}}
    expect("валидная KLP проходит", check(base) == [])
    expect("ttl_days<=0 -> ошибка",
           any("ttl_days" in x for x in check({**base, "keys": [
               {"name": "k", "env_ref": "K", "ttl_days": 0, "rotation_owner": "human"}]})))
    expect("нет env_ref -> ошибка",
           any("env_ref" in x for x in check({**base, "keys": [
               {"name": "k", "env_ref": "", "ttl_days": 30, "rotation_owner": "human"}]})))
    expect("значение секрета в политике -> ошибка",
           any("ЗНАЧЕНИЕ секрета" in x for x in check({**base, "keys": [
               {"name": "k", "env_ref": "K", "ttl_days": 30, "rotation_owner": "human", "note": "sk-abcdefghijklmnop"}]})))
    expect("per_agent_identity.supported=true без evidence -> ошибка",
           any("без доказательства" in x for x in check({**base, "per_agent_identity": {"supported": True, "note": "есть"}})))
    expect("supported=true с evidence -> ок",
           check({**base, "per_agent_identity": {"supported": True, "note": "mTLS per agent", "evidence": ["spiffe-id"]}}) == [])
    expect("нет per_agent_identity -> ошибка", any("per_agent_identity" in x for x in check({k: v for k, v in base.items() if k != "per_agent_identity"})))

    if DEFAULT.exists():
        errs = check(yaml.safe_load(DEFAULT.read_text(encoding="utf-8")))
        expect(f"реальный {DEFAULT.name} валиден", errs == [])
        for x in errs:
            print("   -", x)

    print("validate_key_lifecycle selftest:", "PASS" if ok else "FAIL")
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
        print(f"KEY-LIFECYCLE {path.name}: ошибки:")
        for x in errs:
            print(f"  - {x}")
        return 1
    print(f"KEY-LIFECYCLE-OK: {path.name} валиден.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
