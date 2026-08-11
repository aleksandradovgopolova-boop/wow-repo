#!/usr/bin/env python3
"""data_classification.py (v3.6.4) — авторитетная классификация данных (trust-фикс #4).

Ревью владельца: недоверенный контент НЕ должен понижать свой access-класс через inline-маркер
(`data-class: public` в README не делает файл public). Правильный приоритет:

    авторитетная policy/registry (path/module -> класс)
    -> security scanner (может только ПОВЫСИТЬ до secret)
    -> inline marker — ТОЛЬКО advisory, может только ПОВЫСИТЬ (строгий класс всегда побеждает)
    -> неизвестный класс -> deny (confidential) в strict-режиме, иначе internal.

Порядок строгости: public < internal < confidential < secret. Итог = САМЫЙ строгий из кандидатов.

Только stdlib. CLI: data_classification.py <policy.yaml> [--json] | --selftest
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
SCHEMA = PKG / "schemas" / "data-classification-policy.schema.json"
DEMO = PKG / "examples" / "data-classification-demo"
ORDER = {"public": 0, "internal": 1, "confidential": 2, "secret": 3}
CLASSES = set(ORDER)
_SECRET = re.compile(r"sk-ant-api03|sk-[A-Za-z0-9]{16,}|gho_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY")
_MARKER = re.compile(r"data-class:\s*(public|internal|confidential|secret)")


def _policy_class(policy: dict, path: str):
    """Класс по policy: самое ДЛИННОЕ совпадение path_prefix (детерминированно). None если нет."""
    best, best_len = None, -1
    for r in (policy or {}).get("rules", []) or []:
        pref = r.get("path_prefix", "")
        if path and pref and path.startswith(pref) and len(pref) > best_len:
            best, best_len = r.get("class"), len(pref)
    return best


def classify(content: str, path: str = None, policy: dict = None, strict: bool = False) -> str:
    """Авторитетный класс: policy -> default -> (strict? confidential : internal); scanner и marker
    могут только ПОВЫСИТЬ (итог — max по строгости). Недоверенный marker не понижает класс."""
    base = _policy_class(policy, path) if (policy and path) else None
    if base is None:                                       # авторитетное правило не сматчилось
        effective_strict = strict or bool((policy or {}).get("strict_unknown"))
        if effective_strict:
            base = "confidential"                          # unknown -> deny (strict побеждает default)
        else:
            base = (policy or {}).get("default_class") or "internal"
    candidates = [base]
    if _SECRET.search(content or ""):
        candidates.append("secret")                        # scanner: только вверх
    m = _MARKER.search(content or "")
    if m:
        candidates.append(m.group(1))                      # marker: advisory, только вверх (max игнорит ниже base)
    return max(candidates, key=lambda c: ORDER.get(c, 1))


def validate_policy(dcp: dict):
    e = []
    if not isinstance(dcp, dict):
        return ["policy не объект"]
    if dcp.get("schema_version") != 1 or dcp.get("kind") != "DataClassificationPolicy":
        e.append("schema_version=1 + kind=DataClassificationPolicy обязательны")
    if not (isinstance(dcp.get("id"), str) and re.match(r"^DCP-[0-9]{3,}$", dcp.get("id", ""))):
        e.append("id должен быть формата DCP-NNN")
    if dcp.get("default_class") not in CLASSES:
        e.append(f"default_class ∉ {sorted(CLASSES)}")
    if not isinstance(dcp.get("strict_unknown"), bool):
        e.append("strict_unknown должен быть bool")
    for i, r in enumerate(dcp.get("rules", []) or []):
        if not isinstance(r, dict) or not r.get("path_prefix") or r.get("class") not in CLASSES:
            e.append(f"rules[{i}]: path_prefix (непуст) + class ∈ {sorted(CLASSES)}")
    return e


def _load(p: Path):
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    errs = validate_policy(_load(Path(args[0])))
    if "--json" in argv:
        print(json.dumps({"errors": errs}, ensure_ascii=False, indent=2))
    elif errs:
        print("DATA-CLASSIFICATION: ошибки policy:")
        for x in errs:
            print(f"  - {x}")
    else:
        print("DATA-CLASSIFICATION-OK: policy валидна.")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
