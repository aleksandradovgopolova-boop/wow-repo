#!/usr/bin/env python3
"""Проверка постуры безопасности (v2.20) — governance/security-posture.yaml.

Постура — машиночитаемая карта по 13 областям безопасности со статусом и evidence.
Валидатор проверяет ФОРМУ и что постура не расходится с репозиторием (drift):

  1. каждая область имеет id/title/status/severity/evidence;
  2. status ∈ {implemented, partial, declared, roadmap}; severity ∈ {critical, high, medium, low};
  3. id уникальны;
  4. КАЖДЫЙ evidence-путь резолвится в реальный файл/каталог пакета (иначе постура врёт);
  5. отчёт покрытия: сколько областей implemented/partial/declared/roadmap.

Использование:  validate_security_posture.py [--json] | --selftest
Возврат 0 — форма валидна и все evidence резолвятся, 1 — ошибка.
"""

import json
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
POSTURE = PKG / "governance" / "security-posture.yaml"
STATUS = {"implemented", "partial", "declared", "roadmap"}
SEVERITY = {"critical", "high", "medium", "low"}


def check(data: dict, root: Path):
    errors = []
    areas = data.get("areas") or []
    if not areas:
        return ["постура пуста: нет areas"], {}
    seen = set()
    tally = {s: 0 for s in STATUS}
    for a in areas:
        aid = a.get("id")
        if aid in seen:
            errors.append(f"дублирующийся id области: {aid}")
        seen.add(aid)
        for f in ("id", "title", "status", "severity", "evidence"):
            if not a.get(f):
                errors.append(f"область {aid}: нет поля {f}")
        if a.get("status") not in STATUS:
            errors.append(f"область {aid}: status '{a.get('status')}' не в {STATUS}")
        else:
            tally[a["status"]] += 1
        if a.get("severity") not in SEVERITY:
            errors.append(f"область {aid}: severity '{a.get('severity')}' не в {SEVERITY}")
        for ev in (a.get("evidence") or []):
            if not (root / ev).exists():
                errors.append(f"область {aid}: evidence '{ev}' не резолвится (постура расходится с репо)")
    return errors, tally


def run(as_json=False):
    if not POSTURE.exists():
        print(f"постура не найдена: {POSTURE}")
        return 1
    data = yaml.safe_load(POSTURE.read_text(encoding="utf-8")) or {}
    errors, tally = check(data, PKG)
    if as_json:
        print(json.dumps({"schema_version": 1, "kind": "security-posture-report",
                          "errors": errors, "tally": tally}, ensure_ascii=False, indent=2))
    elif errors:
        print(f"SECURITY-POSTURE: {len(errors)} ошибок:")
        for e in errors:
            print(f"  - {e}")
    else:
        n = len(data.get("areas") or [])
        print(f"SECURITY-POSTURE-OK: {n} областей; evidence резолвится. "
              f"implemented={tally['implemented']}, partial={tally['partial']}, "
              f"declared={tally['declared']}, roadmap={tally['roadmap']}.")
    return 1 if errors else 0


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    data = yaml.safe_load(POSTURE.read_text(encoding="utf-8"))
    e, tally = check(data, PKG)
    expect("реальная постура: форма валидна и evidence резолвится", e == [])
    expect("покрыты все 13 областей", len(data.get("areas") or []) == 13)

    bad = {"areas": [{"id": "x", "title": "t", "status": "выдумка", "severity": "high",
                      "evidence": ["nope/missing.md"]}]}
    e2, _ = check(bad, PKG)
    expect("невалидный status -> ошибка", any("status" in x for x in e2))
    expect("битый evidence-путь -> ошибка (drift)", any("не резолвится" in x for x in e2))
    print("security-posture selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    return run(as_json="--json" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
