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

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
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


def main(argv):
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
