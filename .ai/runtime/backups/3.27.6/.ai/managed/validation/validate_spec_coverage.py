#!/usr/bin/env python3
"""Validate SpecCoverage (v2.98, эпик Context Engineering, этап 2 — Adaptive Spec-First).

Стережёт инварианты адаптивной спецификации:
  1. форма: kind=SpecCoverage, level 0..3, sections[] со статусами из допустимого набора;
  2. declined ТРЕБУЕТ note (нельзя молча отклонить раздел);
  3. blocking_missing = ровно разделы со статусом missing (согласованность);
  4. ready_to_implement=True несовместимо с непустым blocking_missing/form_errors;
  5. уровень не понижен молча: если escalated_from задан, он строго меньше level.

Использование:
  validate_spec_coverage.py <coverage.yaml|.json>
  validate_spec_coverage.py --selftest
Возврат 0 — ок, 1 — ошибки.
"""

import json
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
STATUSES = {"complete", "not_applicable", "declined", "needs_human", "missing"}


def check(cov):
    errors = []
    if not isinstance(cov, dict) or cov.get("kind") != "SpecCoverage":
        errors.append("kind должен быть 'SpecCoverage'")
        return errors
    level = cov.get("level")
    if not isinstance(level, int) or not (0 <= level <= 3):
        errors.append("level должен быть целым 0..3")
    sections = cov.get("sections")
    if not isinstance(sections, list) or not sections:
        errors.append("sections должен быть непустым списком")
        sections = []
    missing_ids = []
    for i, s in enumerate(sections):
        if not isinstance(s, dict) or not s.get("id"):
            errors.append(f"sections[{i}]: нет id"); continue
        st = s.get("status")
        if st not in STATUSES:
            errors.append(f"{s['id']}: недопустимый статус '{st}'")
        if st == "declined" and not s.get("note"):
            errors.append(f"{s['id']}: declined без note (объяснение обязательно)")
        if st == "missing":
            missing_ids.append(s["id"])
    bm = cov.get("blocking_missing")
    if not isinstance(bm, list):
        errors.append("blocking_missing должен быть списком")
    elif set(bm) != set(missing_ids):
        errors.append(f"blocking_missing не совпадает с разделами missing: {sorted(set(bm) ^ set(missing_ids))}")
    if cov.get("ready_to_implement") is True and (missing_ids or cov.get("form_errors")):
        errors.append("ready_to_implement=True при наличии missing/form_errors (несогласованно)")
    ef = cov.get("escalated_from")
    if ef is not None and isinstance(level, int) and not (isinstance(ef, int) and ef < level):
        errors.append("escalated_from должен быть строго меньше level (уровень не понижают молча)")
    return errors


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    good = {"kind": "SpecCoverage", "level": 1, "escalated_from": None,
            "sections": [{"id": "goal", "status": "complete", "note": None},
                         {"id": "scope", "status": "not_applicable", "note": "нет"}],
            "blocking_missing": [], "form_errors": [], "ready_to_implement": True}
    expect("валидный coverage -> без ошибок", check(good) == [])
    expect("не тот kind -> ошибка", any("SpecCoverage" in e for e in check({"kind": "x"})))
    bad_dec = json.loads(json.dumps(good))
    bad_dec["sections"].append({"id": "x", "status": "declined"})
    expect("declined без note -> ошибка", any("declined" in e for e in check(bad_dec)))
    bad_bm = json.loads(json.dumps(good))
    bad_bm["sections"].append({"id": "y", "status": "missing"})
    expect("missing не отражён в blocking_missing -> ошибка", any("blocking_missing" in e for e in check(bad_bm)))
    bad_ready = {"kind": "SpecCoverage", "level": 0,
                 "sections": [{"id": "goal", "status": "missing"}],
                 "blocking_missing": ["goal"], "ready_to_implement": True}
    expect("ready_to_implement=True при missing -> ошибка", any("ready_to_implement" in e for e in check(bad_ready)))
    bad_esc = json.loads(json.dumps(good)); bad_esc["escalated_from"] = 2
    expect("escalated_from >= level -> ошибка", any("escalated_from" in e for e in check(bad_esc)))

    # реальный spec_levels даёт валидный coverage
    sys.path.insert(0, str(PKG / "tools"))
    import spec_levels
    cov = spec_levels.assess({"task_type": "ENGINEERING"},
                             {s: {"status": "complete"} for s in spec_levels.required_sections(1)})
    expect("реальный SpecCoverage (полный ENGINEERING) валиден", check(cov) == [])
    cov2 = spec_levels.assess({"task_type": "QUICK"})  # всё missing
    expect("реальный SpecCoverage (пустой QUICK) валиден по форме", check(cov2) == [])

    print("validate_spec_coverage selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    if not argv:
        print("укажи путь к coverage или --selftest")
        return 1
    path = Path(argv[0])
    if not path.exists():
        print(f"SPEC-COVERAGE: файл не найден: {path}")
        return 1
    text = path.read_text(encoding="utf-8")
    cov = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    errs = check(cov)
    if errs:
        print("SPEC-COVERAGE: ошибки:")
        for e in errs:
            print(f"  - {e}")
        return 1
    print(f"SPEC-COVERAGE-OK: {path.name} — форма и инварианты соблюдены.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
