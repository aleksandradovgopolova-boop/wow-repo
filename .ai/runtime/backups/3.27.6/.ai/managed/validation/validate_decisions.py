#!/usr/bin/env python3
"""Проверка реестра решений (v2.10) — Decision Intelligence из team-os-toolkit.

Реестр (decisions/registry.yaml) хранит принципы (способ мышления), эпизоды
(конкретные решения) и исходы. Валидатор проверяет целостность и калибровку —
чтобы decisions не превратился в свалку личных привычек:

  1. id принципов/эпизодов уникальны; обязательные поля на месте;
  2. status ∈ {proposed, ratified, retired}; retired обязан иметь retired_reason;
  3. confidence ∈ {low, medium, high}; recurrence_count >= 0; review_date парсится;
  4. supersedes ссылается на существующий принцип (или null);
  5. derived_from ссылается на существующие эпизоды;
  6. reversibility эпизода ∈ {two-way, one-way}; date парсится;
  7. outcomes.decision ссылается на существующий эпизод;
  8. предупреждение (не ошибка): ratified-принцип с recurrence_count < 2 и без
     контрпримеров — «принцип из одного случая» (калибровка из скилла decision-support).

Использование:  validate_decisions.py [registry.yaml] [--json]   (default: decisions/registry.yaml)
                validate_decisions.py --selftest
Возврат 0 — валиден (возможны WARN), 1 — есть ошибки целостности.
"""

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
STATUS = {"proposed", "ratified", "retired"}
CONFIDENCE = {"low", "medium", "high"}
REVERSIBILITY = {"two-way", "one-way"}


def parse_date(s):
    try:
        datetime.strptime(str(s), "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


def check(data: dict):
    errors, warns = [], []
    principles = data.get("principles") or []
    episodes = data.get("episodes") or []
    outcomes = data.get("outcomes") or []
    ep_ids = {e.get("id") for e in episodes}
    pr_ids = {p.get("id") for p in principles}

    seen = set()
    for e in episodes:
        eid = e.get("id")
        if eid in seen:
            errors.append(f"эпизод: дублирующийся id {eid}")
        seen.add(eid)
        for f in ("id", "question", "decision", "reason", "reversibility", "date"):
            if not e.get(f):
                errors.append(f"эпизод {eid}: нет поля {f}")
        if e.get("reversibility") not in REVERSIBILITY:
            errors.append(f"эпизод {eid}: reversibility '{e.get('reversibility')}' не в {REVERSIBILITY}")
        if not parse_date(e.get("date")):
            errors.append(f"эпизод {eid}: date не парсится (YYYY-MM-DD)")

    seen = set()
    for p in principles:
        pid = p.get("id")
        if pid in seen:
            errors.append(f"принцип: дублирующийся id {pid}")
        seen.add(pid)
        for f in ("id", "principle", "scope", "status", "confidence", "recurrence_count", "review_date", "derived_from"):
            if p.get(f) in (None, ""):
                errors.append(f"принцип {pid}: нет поля {f}")
        if p.get("status") not in STATUS:
            errors.append(f"принцип {pid}: status '{p.get('status')}' не в {STATUS}")
        if p.get("confidence") not in CONFIDENCE:
            errors.append(f"принцип {pid}: confidence '{p.get('confidence')}' не в {CONFIDENCE}")
        if not isinstance(p.get("recurrence_count"), int) or p.get("recurrence_count", -1) < 0:
            errors.append(f"принцип {pid}: recurrence_count должен быть int >= 0")
        if not parse_date(p.get("review_date")):
            errors.append(f"принцип {pid}: review_date не парсится (YYYY-MM-DD)")
        if p.get("status") == "retired" and not p.get("retired_reason"):
            errors.append(f"принцип {pid}: status retired требует retired_reason")
        sup = p.get("supersedes")
        if sup and sup not in pr_ids:
            errors.append(f"принцип {pid}: supersedes '{sup}' — нет такого принципа")
        for d in (p.get("derived_from") or []):
            if d not in ep_ids:
                errors.append(f"принцип {pid}: derived_from '{d}' — нет такого эпизода")
        # калибровка (WARN)
        if p.get("status") == "ratified" and isinstance(p.get("recurrence_count"), int) \
                and p["recurrence_count"] < 2 and not (p.get("counterexamples")):
            warns.append(f"принцип {pid}: ratified при recurrence_count<2 — принцип из одного случая?")

    for o in outcomes:
        if o.get("decision") not in ep_ids:
            errors.append(f"outcome: decision '{o.get('decision')}' — нет такого эпизода")

    return errors, warns


def run(reg: Path, as_json=False):
    if not reg.exists():
        print(f"реестр решений не найден: {reg} — нечего проверять (это не ошибка).")
        return 0
    data = yaml.safe_load(reg.read_text(encoding="utf-8")) or {}
    errors, warns = check(data)
    if as_json:
        print(json.dumps({"schema_version": 1, "kind": "decisions-report",
                          "file": str(reg), "errors": errors, "warns": warns},
                         ensure_ascii=False, indent=2))
    else:
        for w in warns:
            print(f"  WARN {w}")
        if errors:
            print(f"DECISIONS: {len(errors)} ошибок целостности:")
            for e in errors:
                print(f"  - {e}")
        else:
            n = len(data.get('principles') or [])
            print(f"DECISIONS-OK: реестр валиден ({n} принципов, "
                  f"{len(data.get('episodes') or [])} эпизодов)" +
                  (f", {len(warns)} предупреждений по калибровке." if warns else "."))
    return 1 if errors else 0


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    valid = {"schema_version": 1, "kind": "decisions-registry",
             "episodes": [{"id": "ep-1", "question": "q", "decision": "d", "reason": "r",
                           "reversibility": "two-way", "date": "2026-07-13"}],
             "principles": [{"id": "dp-1", "principle": "p", "scope": ["s"], "status": "ratified",
                             "confidence": "high", "recurrence_count": 3, "review_date": "2026-12-01",
                             "derived_from": ["ep-1"]}],
             "outcomes": [{"decision": "ep-1", "outcome": "ok"}]}
    e, w = check(valid)
    expect("валидный реестр без ошибок", e == [])

    e, _ = check({"principles": [{"id": "dp-x", "principle": "p", "scope": ["s"], "status": "retired",
                                  "confidence": "low", "recurrence_count": 1, "review_date": "2026-01-01",
                                  "derived_from": []}], "episodes": []})
    expect("retired без retired_reason -> ошибка", any("retired_reason" in x for x in e))

    e, _ = check({"principles": [{"id": "dp-y", "principle": "p", "scope": ["s"], "status": "ratified",
                                  "confidence": "high", "recurrence_count": 2, "review_date": "2026-01-01",
                                  "derived_from": ["ep-nope"]}], "episodes": []})
    expect("derived_from на несуществующий эпизод -> ошибка", any("ep-nope" in x for x in e))

    e, _ = check({"principles": [], "episodes": [
        {"id": "ep-z", "question": "q", "decision": "d", "reason": "r",
         "reversibility": "maybe", "date": "2026-07-13"}]})
    expect("невалидный reversibility -> ошибка", any("reversibility" in x for x in e))

    _, w = check({"principles": [{"id": "dp-w", "principle": "p", "scope": ["s"], "status": "ratified",
                                  "confidence": "high", "recurrence_count": 1, "review_date": "2026-12-01",
                                  "derived_from": []}], "episodes": []})
    expect("ratified из одного случая -> WARN калибровки", any("одного случая" in x for x in w))

    # реальный реестр кита
    reg = PKG / "decisions" / "registry.yaml"
    if reg.exists():
        e, _ = check(yaml.safe_load(reg.read_text(encoding="utf-8")))
        expect("реестр кита валиден", e == [])
    print("validate_decisions selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    reg = Path(args[0]).resolve() if args else (PKG / "decisions" / "registry.yaml")
    return run(reg, as_json="--json" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
