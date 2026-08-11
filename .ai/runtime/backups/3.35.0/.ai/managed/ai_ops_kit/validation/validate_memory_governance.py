#!/usr/bin/env python3
"""Validate MemoryGovernancePolicy (security-долг #2, OWASP ASI).

Инварианты: каждая запись памяти несёт provenance (origin + source_type); имеет expiry (ttl_days>0 /
review_date / permanent+justification); НЕ self-ingested без подтверждения человека (собственный вывод
агента не становится авторитетной памятью сам по себе); derived-запись обязана ссылаться на upstream.

  validate_memory_governance.py [examples/memory-governance-demo/MGP-001.yaml] | --selftest
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
DEFAULT = PKG / "examples" / "memory-governance-demo" / "MGP-001.yaml"
SOURCE_TYPES = {"human", "external", "derived", "system"}
EXPIRY_MODES = {"ttl_days", "review_date", "permanent"}


def check(data):
    e = []
    if not isinstance(data, dict):
        return ["MemoryGovernancePolicy не объект"]
    if data.get("schema_version") != 1:
        e.append("schema_version должен быть 1")
    if data.get("kind") != "MemoryGovernancePolicy":
        e.append("kind должен быть MemoryGovernancePolicy")
    if not str(data.get("policy_id", "")).startswith("MGP-"):
        e.append("policy_id должен быть MGP-NNN")
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        return e + ["entries непустой список обязателен"]
    seen = set()
    for en in entries:
        if not isinstance(en, dict):
            e.append("entry не объект"); continue
        eid = en.get("id") or "?"
        if eid in seen:
            e.append(f"дубликат id записи: {eid}")
        seen.add(eid)
        prov = en.get("provenance")
        if not isinstance(prov, dict) or not prov.get("origin"):
            e.append(f"{eid}: нет provenance.origin")
        elif prov.get("source_type") not in SOURCE_TYPES:
            e.append(f"{eid}: provenance.source_type ∉ {sorted(SOURCE_TYPES)}")
        elif prov.get("source_type") == "derived" and not (prov.get("upstream")):
            e.append(f"{eid}: derived-память без upstream-ссылок")
        exp = en.get("expiry")
        if not isinstance(exp, dict) or exp.get("mode") not in EXPIRY_MODES:
            e.append(f"{eid}: expiry.mode ∉ {sorted(EXPIRY_MODES)}")
        else:
            m, v = exp.get("mode"), exp.get("value")
            if m == "ttl_days" and not (isinstance(v, int) and v > 0):
                e.append(f"{eid}: expiry ttl_days требует value>0")
            if m == "review_date" and not v:
                e.append(f"{eid}: expiry review_date требует value")
            if m == "permanent" and not exp.get("justification"):
                e.append(f"{eid}: permanent-память требует justification (иначе бессрочная память без причины)")
        # no-self-ingestion: собственный вывод агента не авторитетен без подтверждения человека
        if en.get("self_ingested") is True and en.get("human_confirmed") is not True:
            e.append(f"{eid}: self_ingested=true без human_confirmed — запрет self-ingestion")
    return e


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    path = Path(args[0]) if args else DEFAULT
    if not path.exists():
        print(f"нет файла: {path}"); return 1
    errs = check(yaml.safe_load(path.read_text(encoding="utf-8")))
    if errs:
        print(f"MEMORY-GOVERNANCE {path.name}: ошибки:")
        for x in errs:
            print(f"  - {x}")
        return 1
    print(f"MEMORY-GOVERNANCE-OK: {path.name} валиден.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
