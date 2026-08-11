#!/usr/bin/env python3
"""Проверка декларации обязанностей постоянного агента Robin (v2.21).

Robin — спека (runtime/robin/ROBIN.md), не бот; обязанности объявляются
декларативно (runtime/robin/duties.example.yaml, в child — свой файл) против
контракта persistent-agent-runtime из registry/runtimes.yaml. Валидатор держит
декларацию честной и в границах контракта:

  1. schema_version/kind на месте; есть top-level owner (кому эскалировать);
  2. id обязанностей уникальны; обязательные поля (id, description, trigger,
     inputs, output, owner) присутствуют;
  3. trigger.type ∈ {cron, event}; cron требует schedule, event требует event;
  4. output.destination НЕ prod и НЕ curated/promoted-память — Robin read-mostly
     (перенос staged->promoted делает человек, см. ROBIN.md);
  5. минимально обязательная обязанность есть: хотя бы одна с trigger.type: cron
     (периодический дайджест) — иначе Robin молчит, пока его не спросят.

Использование:  validate_duties.py [duties.yaml] [--json]   (default: runtime/robin/duties.example.yaml)
                validate_duties.py --selftest
Возврат 0 — валиден, 1 — есть ошибки.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
TRIGGER_TYPES = {"cron", "event"}
REQUIRED_FIELDS = ("id", "description", "trigger", "inputs", "output", "owner")
# read-mostly: назначение обязанности не должно писать в prod или в curated-память.
FORBIDDEN_DEST_SUBSTR = ("prod", "production")
FORBIDDEN_DEST_PATHS = ("curated", "promoted")


def check(data: dict):
    errors = []
    if data.get("schema_version") is None:
        errors.append("нет schema_version")
    if data.get("kind") != "robin-duties":
        errors.append("kind должен быть 'robin-duties'")
    if not data.get("owner"):
        errors.append("нет top-level owner (кому эскалировать при сбое)")

    duties = data.get("duties") or []
    if not duties:
        errors.append("нет ни одной обязанности (duties пуст)")

    seen = set()
    has_cron = False
    for d in duties:
        did = d.get("id", "<no-id>")
        if did in seen:
            errors.append(f"обязанность: дублирующийся id {did}")
        seen.add(did)
        for f in REQUIRED_FIELDS:
            if d.get(f) in (None, "", [], {}):
                errors.append(f"обязанность {did}: нет поля {f}")

        trig = d.get("trigger") or {}
        ttype = trig.get("type")
        if ttype not in TRIGGER_TYPES:
            errors.append(f"обязанность {did}: trigger.type '{ttype}' не в {sorted(TRIGGER_TYPES)}")
        elif ttype == "cron":
            has_cron = True
            if not trig.get("schedule"):
                errors.append(f"обязанность {did}: trigger.type cron требует schedule")
        elif ttype == "event":
            if not trig.get("event"):
                errors.append(f"обязанность {did}: trigger.type event требует event")

        out = d.get("output") or {}
        if not out.get("artifact"):
            errors.append(f"обязанность {did}: output.artifact обязателен")
        dest = str(out.get("destination", "")).lower()
        if not dest:
            errors.append(f"обязанность {did}: output.destination обязателен")
        else:
            if any(s in dest for s in FORBIDDEN_DEST_SUBSTR):
                errors.append(f"обязанность {did}: destination '{dest}' пишет в prod — "
                              f"Robin read-mostly, запрещено")
            if any(p in dest for p in FORBIDDEN_DEST_PATHS):
                errors.append(f"обязанность {did}: destination '{dest}' пишет в curated/promoted "
                              f"память — перенос делает человек, запрещено")

    if duties and not has_cron:
        errors.append("нет минимально обязательной обязанности: ни одной с trigger.type cron "
                      "(периодический дайджест)")
    return errors


def run(path: Path, as_json=False):
    if not path.exists():
        print(f"файл обязанностей не найден: {path} — нечего проверять (это не ошибка).")
        return 0
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    errors = check(data)
    if as_json:
        print(json.dumps({"schema_version": 1, "kind": "duties-report",
                          "file": str(path), "errors": errors}, ensure_ascii=False, indent=2))
    elif errors:
        print(f"DUTIES: {len(errors)} ошибок:")
        for e in errors:
            print(f"  - {e}")
    else:
        n = len(data.get("duties") or [])
        print(f"DUTIES-OK: декларация валидна ({n} обязанностей).")
    return 1 if errors else 0


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    path = Path(args[0]).resolve() if args else (PKG / "runtime" / "robin" / "duties.example.yaml")
    return run(path, as_json="--json" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
