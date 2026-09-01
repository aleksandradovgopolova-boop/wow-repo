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

import json
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
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


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    valid = {"schema_version": 1, "kind": "robin-duties", "owner": "team-lead",
             "duties": [{"id": "d1", "description": "x", "owner": "team-lead",
                         "trigger": {"type": "cron", "schedule": "0 9 * * MON"},
                         "inputs": ["a"], "output": {"artifact": "digest", "destination": "team-chat"}}]}
    expect("валидная декларация без ошибок", check(valid) == [])

    no_cron = {"schema_version": 1, "kind": "robin-duties", "owner": "t",
               "duties": [{"id": "d1", "description": "x", "owner": "t",
                           "trigger": {"type": "event", "event": "chat-question"},
                           "inputs": ["a"], "output": {"artifact": "answer", "destination": "team-chat"}}]}
    expect("нет cron-обязанности -> ошибка", any("минимально обязательной" in e for e in check(no_cron)))

    cron_no_sched = {"schema_version": 1, "kind": "robin-duties", "owner": "t",
                     "duties": [{"id": "d1", "description": "x", "owner": "t",
                                 "trigger": {"type": "cron"},
                                 "inputs": ["a"], "output": {"artifact": "digest", "destination": "team-chat"}}]}
    expect("cron без schedule -> ошибка", any("требует schedule" in e for e in check(cron_no_sched)))

    prod_dest = {"schema_version": 1, "kind": "robin-duties", "owner": "t",
                 "duties": [{"id": "d1", "description": "x", "owner": "t",
                             "trigger": {"type": "cron", "schedule": "0 9 * * *"},
                             "inputs": ["a"], "output": {"artifact": "x", "destination": "prod-db"}}]}
    expect("destination в prod -> ошибка (read-mostly)", any("read-mostly" in e for e in check(prod_dest)))

    curated_dest = {"schema_version": 1, "kind": "robin-duties", "owner": "t",
                    "duties": [{"id": "d1", "description": "x", "owner": "t",
                                "trigger": {"type": "cron", "schedule": "0 9 * * *"},
                                "inputs": ["a"], "output": {"artifact": "x", "destination": "promoted/knowledge"}}]}
    expect("destination в promoted-память -> ошибка", any("человек" in e for e in check(curated_dest)))

    dup = {"schema_version": 1, "kind": "robin-duties", "owner": "t",
           "duties": [{"id": "d1", "description": "x", "owner": "t",
                       "trigger": {"type": "cron", "schedule": "0 9 * * *"},
                       "inputs": ["a"], "output": {"artifact": "d", "destination": "chat"}},
                      {"id": "d1", "description": "y", "owner": "t",
                       "trigger": {"type": "event", "event": "e"},
                       "inputs": ["a"], "output": {"artifact": "d", "destination": "chat"}}]}
    expect("дублирующийся id -> ошибка", any("дублирующийся" in e for e in check(dup)))

    # реальный пример кита
    ex = PKG / "runtime" / "robin" / "duties.example.yaml"
    if ex.exists():
        expect("пример кита валиден", check(yaml.safe_load(ex.read_text(encoding="utf-8"))) == [])

    print("validate_duties selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    path = Path(args[0]).resolve() if args else (PKG / "runtime" / "robin" / "duties.example.yaml")
    return run(path, as_json="--json" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
