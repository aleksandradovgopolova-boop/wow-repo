#!/usr/bin/env python3
"""Проверка событийного каталога (v2.29) — единый источник имён событий.

Класс дефектов: три «языка» именования расходятся. Контракт (domain events, прошедшее
время, точка): `task.completed`, `object.version_created`. Код (audit, настоящее время /
другой разделитель): `task.complete`, `object.version.save`. Metric catalog (третий
вариант): `catalog.publish`. Плюс концептуальная подмена: контракт описывает domain
events (envelope), а в коде persist-ится AuditEvent (actor/action/result) — разные
сущности выдаются за одно.

Каталог (в child: .ai/project/contracts/events.yaml) делает имя каноничным ОДИН раз;
audit/analytics-события обязаны ссылаться на domain-событие (maps_to) или явно быть
standalone. Валидатор:

  1. schema_version/kind; имя уникально; имя соответствует грамматике (dot-нотация,
     lowercase, ≥2 сегмента, единый разделитель — без camelCase / kebab / смеси);
  2. kind ∈ {domain, audit, analytics};
  3. КОНЦЕПТУАЛЬНАЯ РАЗДЕЛЬНОСТЬ: domain-событие не описывается audit-полями
     (actor/action/result) — иначе domain подменяют на AuditEvent;
  4. audit/analytics обязаны maps_to существующее domain-событие ЛИБО standalone+reason
     (иначе — тихий третий вариант имени);
  5. maps_to резолвится в объявленное domain-событие;
  6. WARN: два разных имени с одинаковым «стемом» без связи (вероятный drift).
  7. --scan f1,f2: best-effort — строковые литералы событий в коде, не совпадающие ни с
     одним каноничным/маппящимся именем (drift кода). Эвристика, не крах при отсутствии.

Использование:  validate_event_catalog.py [events.yaml] [--scan f1,f2] [--json]
                validate_event_catalog.py --selftest
Возврат 0 — валиден (возможны WARN), 1 — есть ошибки.
"""

import json
import re
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
KINDS = {"domain", "audit", "analytics"}
# каноничная грамматика: lowercase, dot-нотация, ≥2 сегмента, внутри сегмента [a-z0-9_]
EVENT_RE = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z0-9]+(?:_[a-z0-9]+)*)+$")
AUDIT_FIELD_MARKERS = {"actor", "actorid", "action", "result", "objecttype", "objectid"}


def _stem(name):
    # грубый «стем»: имя без последнего сегмента (концепт), для поиска расхождений
    return name.rsplit(".", 1)[0] if "." in name else name


def check(data: dict):
    errors, warns = [], []
    if data.get("schema_version") is None:
        errors.append("нет schema_version")
    if data.get("kind") != "event-catalog":
        errors.append("kind должен быть 'event-catalog'")

    events = data.get("events") or []
    if not events:
        errors.append("нет ни одного события (events пуст)")

    names = [e.get("name") for e in events]
    domain_names = {e.get("name") for e in events if e.get("kind") == "domain"}
    seen = set()
    for e in events:
        n = e.get("name", "<no-name>")
        if n in seen:
            errors.append(f"дублирующееся имя события: {n}")
        seen.add(n)
        if not EVENT_RE.match(str(n)):
            errors.append(f"имя '{n}' не по грамматике (lowercase dot-нотация, ≥2 сегмента, "
                          f"без camelCase/kebab/смеси разделителей)")
        k = e.get("kind")
        if k not in KINDS:
            errors.append(f"событие {n}: kind '{k}' не в {sorted(KINDS)}")

        # 3. концептуальная раздельность domain vs audit
        if k == "domain":
            flds = {str(f).lower() for f in (e.get("fields") or [])}
            if flds & AUDIT_FIELD_MARKERS:
                errors.append(f"domain-событие {n} описано audit-полями "
                              f"{sorted(flds & AUDIT_FIELD_MARKERS)} — domain event подменён на AuditEvent")

        # 4-5. audit/analytics -> maps_to domain (или standalone+reason)
        if k in ("audit", "analytics"):
            if e.get("standalone"):
                if not e.get("reason"):
                    errors.append(f"событие {n}: standalone требует reason")
            else:
                mt = e.get("maps_to")
                if not mt:
                    errors.append(f"{k}-событие {n}: нет maps_to на domain-событие "
                                  f"(иначе это тихий третий вариант имени; либо standalone+reason)")
                elif mt not in domain_names:
                    errors.append(f"событие {n}: maps_to '{mt}' — нет такого domain-события")

    # 6. WARN: одинаковый стем у разных имён без связи (вероятный drift)
    by_stem = {}
    for e in events:
        by_stem.setdefault(_stem(e.get("name", "")), []).append(e)
    for stem, group in by_stem.items():
        gnames = {g.get("name") for g in group}
        if len(gnames) > 1:
            # если внутри группы есть maps_to-связь между ними — ок
            linked = any(g.get("maps_to") in gnames for g in group)
            if not linked:
                warns.append(f"разные имена с общим концептом '{stem}': {sorted(gnames)} — "
                             f"возможен drift; свяжите через maps_to или унифицируйте")
    return errors, warns


def scan_code(catalog_names, mapped_names, files):
    """Best-effort: строковые литералы событий в коде, не совпадающие с каноном."""
    known = set(catalog_names) | set(mapped_names)
    # ловим литералы вида "word.word" / 'word.word.word'
    lit_re = re.compile(r"""['"]([a-z][a-z0-9]*(?:[._][a-z0-9]+)+)['"]""")
    drift = []
    for f in files:
        p = Path(f)
        if not p.exists():
            continue
        for m in lit_re.finditer(p.read_text(encoding="utf-8", errors="ignore")):
            lit = m.group(1)
            # интересуют только «событие-подобные» (есть точка) и не входящие в канон
            if "." in lit and lit not in known:
                drift.append({"file": f, "literal": lit})
    return drift


def run(path: Path, scan_files=None, as_json=False):
    if not path.exists():
        print(f"каталог событий не найден: {path} — нечего проверять (это не ошибка).")
        return 0
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    errors, warns = check(data)
    drift = []
    if scan_files:
        names = [e.get("name") for e in (data.get("events") or [])]
        mapped = [e.get("maps_to") for e in (data.get("events") or []) if e.get("maps_to")]
        drift = scan_code(names, mapped, scan_files)
    if as_json:
        print(json.dumps({"schema_version": 1, "kind": "event-catalog-report",
                          "file": str(path), "errors": errors, "warns": warns, "drift": drift},
                         ensure_ascii=False, indent=2))
    else:
        for w in warns:
            print(f"  WARN {w}")
        for d in drift:
            print(f"  DRIFT {d['file']}: '{d['literal']}' — нет в каталоге событий")
        if errors:
            print(f"EVENT-CATALOG: {len(errors)} ошибок:")
            for e in errors:
                print(f"  - {e}")
        else:
            print(f"EVENT-CATALOG-OK: {len(data.get('events') or [])} событий, имена согласованы"
                  + (f", {len(warns)} WARN" if warns else "")
                  + (f", {len(drift)} drift в коде" if drift else "") + ".")
    return 1 if errors else 0


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    valid = {"schema_version": 1, "kind": "event-catalog", "events": [
        {"name": "object.version_created", "kind": "domain", "payload": ["objectId", "version"]},
        {"name": "task.completed", "kind": "domain", "payload": ["taskId"]},
        {"name": "object.version_saved", "kind": "audit", "maps_to": "object.version_created",
         "fields": ["actorId", "action", "result"]},
        {"name": "catalog.published", "kind": "analytics", "maps_to": "object.version_created"},
    ]}
    e, w = check(valid)
    expect("валидный каталог без ошибок", e == [])

    # дубль имени -> ошибка
    e, _ = check({"schema_version": 1, "kind": "event-catalog", "events": [
        {"name": "task.completed", "kind": "domain"},
        {"name": "task.completed", "kind": "domain"}]})
    expect("дубль имени -> ошибка", any("дублирующееся" in x for x in e))

    # плохая грамматика
    e, _ = check({"schema_version": 1, "kind": "event-catalog",
                  "events": [{"name": "task.Complete", "kind": "domain"}]})
    expect("camelCase/грамматика -> ошибка", any("грамматике" in x for x in e))

    # audit без maps_to
    e, _ = check({"schema_version": 1, "kind": "event-catalog", "events": [
        {"name": "task.completed", "kind": "domain"},
        {"name": "task.complete", "kind": "audit"}]})
    expect("audit без maps_to -> ошибка", any("нет maps_to" in x for x in e))

    # maps_to в несуществующее domain
    e, _ = check({"schema_version": 1, "kind": "event-catalog", "events": [
        {"name": "task.completed", "kind": "domain"},
        {"name": "task.done", "kind": "analytics", "maps_to": "task.nope"}]})
    expect("maps_to на несуществующее -> ошибка", any("нет такого domain" in x for x in e))

    # domain подменён audit-полями
    e, _ = check({"schema_version": 1, "kind": "event-catalog", "events": [
        {"name": "object.opened", "kind": "domain", "fields": ["actorId", "action", "result"]}]})
    expect("domain с audit-полями -> ошибка (подмена)", any("AuditEvent" in x for x in e))

    # standalone без reason
    e, _ = check({"schema_version": 1, "kind": "event-catalog", "events": [
        {"name": "d.happened", "kind": "domain"},
        {"name": "sys.pinged", "kind": "audit", "standalone": True}]})
    expect("standalone без reason -> ошибка", any("standalone требует reason" in x for x in e))

    # drift-скан по коду
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        code = Path(td) / "server.ts"
        code.write_text('emit("object.version.save"); emit("object.version_created");', encoding="utf-8")
        drift = scan_code(["object.version_created"], [], [str(code)])
        expect("scan: ловит незнакомый литерал", any(d["literal"] == "object.version.save" for d in drift))
        expect("scan: каноничное имя не в drift", all(d["literal"] != "object.version_created" for d in drift))

    ex = PKG / "examples" / "event-catalog-demo" / "events.yaml"
    if ex.exists():
        e, _ = check(yaml.safe_load(ex.read_text(encoding="utf-8")))
        expect("пример кита валиден", e == [])

    print("validate_event_catalog selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    scan = None
    for a in argv:
        if a.startswith("--scan="):
            scan = [x.strip() for x in a.split("=", 1)[1].split(",") if x.strip()]
    path = Path(args[0]).resolve() if args else (PKG / ".ai" / "project" / "contracts" / "events.yaml")
    return run(path, scan_files=scan, as_json="--json" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
