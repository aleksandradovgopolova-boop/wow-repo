#!/usr/bin/env python3
"""Шина внимания — durable сток, гарантирующий, что КАЖДЫЙ повод позвать человека доходит до inbox.

ЗАЧЕМ (issue #633). `inbox` уже сводит семь персистентных источников в одну очередь владельца, но он
ПРОЕКЦИЯ уже записанного состояния: повод виден, только если кто-то ДО этого положил его в durable-сток
(workitem-статус, pending-решение и т.п.). Эфемерные внутрисессионные обращения — blocked-preflight,
эскалация модели во время `do`/`run`, governance-эскалация «это решение за человеком» — сурфейсились
только в самой сессии и в inbox не попадали: у них не было durable-места приземления.

Это оно. Любой код, зовущий человека, обязан положить повод СЮДА (`record`), а `inbox` читает сток
восьмым источником (`collect`). Человек может не знать, какой агент/гейт/ревью его вызвал — агрегирует
кит. Это достройка ГАРАНТИИ ОХВАТА над существующим inbox, а не новый механизм очереди.

Идемпотентность по `key`: повторный повод того же типа для той же работы ОБНОВЛЯЕТ запись (последняя
причина/время), а не плодит дубли — resume, перезапуск, повторный `governance` не засоряют очередь.
Когда причина устранена, код зовёт `resolve(key)` — снятая запись из inbox уходит.

Живёт в `lifecycle` (ядро): и engine (эскалации прогона), и governance (граница решений), и cli
(inbox) импортируют ядро без нарушения слоёв. Хранилище — `.ai/runtime/attention.yaml`, рядом с
active-work: то же runtime-состояние координации.
"""
from __future__ import annotations

import argparse
import contextlib
import sys
from datetime import datetime, timezone
from pathlib import Path

from ai_ops_kit.shared import lifecycle_store as _ls

_REL = Path(".ai") / "runtime" / "attention.yaml"
_KIND = "attention-log"

# Типы повода — чтобы inbox отличал «нужно решение» от «работа остановлена». Значения — данные, не код.
DECISION = "decision"          # governance/боундари: решение за человеком
BLOCKED = "blocked"            # работа остановлена до человека (preflight/эскалация модели)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _store_path(child_root) -> Path:
    return Path(child_root) / _REL


def _load(path: Path) -> dict:
    """FAIL-SAFE чтение стока внимания. Отсутствует -> пусто; повреждён -> пусто (сток внимания НЕ
    роняет прогон: пропавший повод хуже, но не смертелен, и вернётся при следующем возникновении)."""
    g = _ls.load_guarded(path, kind=_KIND)
    if g["state"] != "ok":
        return {"schema_version": 1, "kind": _KIND, "records": []}
    data = g["data"]
    data.setdefault("schema_version", 1)
    data.setdefault("kind", _KIND)
    data.setdefault("records", [])
    return data


@contextlib.contextmanager
def _locked(path: Path):
    """Межпроцессная блокировка вокруг read-modify-write (как active_work): конкурентные сессии не
    теряют записи друг друга. best-effort — без fcntl деградирует до no-op."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        import fcntl
    except ImportError:
        yield
        return
    f = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(Exception):
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


def record(child_root, *, key, source, reason, kind=BLOCKED, work_id=None, at=None) -> bool:
    """Положить повод позвать человека в durable-сток. Идемпотентно по `key`. -> True если записано.

    key    — стабильный идентификатор повода (напр. f"preflight:{fid}"): один и тот же key
             ОБНОВЛЯЕТ запись, а не добавляет вторую.
    source — кто зовёт человека, человеческим языком («прогон: preflight», «граница решений»).
    reason — почему (одна фраза-следствие для человека).
    kind   — DECISION (нужно решение) | BLOCKED (работа остановлена).
    """
    if child_root is None or not (key and source and reason):
        return False
    path = _store_path(child_root)
    with _locked(path):
        data = _load(path)
        now = at or _now_iso()
        existing = next((r for r in data["records"] if r.get("key") == key), None)
        if existing is not None:
            existing.update({"source": source, "reason": reason, "kind": kind,
                             "work_id": work_id, "at": now, "status": "pending"})
        else:
            data["records"].append({"key": key, "source": source, "reason": reason, "kind": kind,
                                    "work_id": work_id, "first_seen_at": now, "at": now,
                                    "status": "pending"})
        r = _ls.durable_write(path, data, require_keys=("kind", "records"))
        return bool(r.get("ok"))


def resolve(child_root, key) -> bool:
    """Снять повод (причина устранена): ПОМЕЧАЕТ запись `status=resolved`, СОХРАНЯЯ её, а не удаляет.

    Раньше resolve удалял запись — и «сколько внимания понадобилось за прогон» посчитать было нечем
    (снятые поводы исчезали). Теперь снятый повод остаётся durable-свидетельством, что человека
    звали (#676): `attention_summary` считает и его. Для inbox ничего не меняется — `collect`
    по-прежнему фильтрует по pending, так что снятый повод из очереди уходит.
    -> True если запись была pending и снята сейчас; False если её нет или уже снята (идемпотентно)."""
    if child_root is None or not key:
        return False
    path = _store_path(child_root)
    with _locked(path):
        data = _load(path)
        rec = next((r for r in data["records"] if r.get("key") == key), None)
        if rec is None or rec.get("status") == "resolved":
            return False
        rec["status"] = "resolved"
        rec["resolved_at"] = _now_iso()
        _ls.durable_write(path, data, require_keys=("kind", "records"))
        return True


def collect(child_root) -> list:
    """READ-ONLY: поводы, ждущие человека (status pending). Ничего не пишет. Для inbox."""
    data = _load(_store_path(child_root))
    return [r for r in data["records"] if r.get("status", "pending") == "pending"]


def attention_summary(child_root) -> dict:
    """#676: сколько РАЗ кит звал человека за жизнь этой дочки — durable-замер «внимания».

    Считает ВСЕ записи (pending + resolved): снятый повод остаётся свидетельством, что внимание
    понадобилось, поэтому замер переживает resolve. Разрез по типу (decision/blocked) и статусу.
    Это НИЖНЯЯ граница внимания: ловит только то, что код провёл через `record`
    (blocked-preflight, эскалация модели, граница решений governance), а не каждую ручную правку."""
    recs = _load(_store_path(child_root)).get("records", [])
    pending = sum(1 for r in recs if r.get("status", "pending") == "pending")
    return {
        "total": len(recs),
        "decision": sum(1 for r in recs if r.get("kind") == DECISION),
        "blocked": sum(1 for r in recs if r.get("kind") == BLOCKED),
        "pending": pending,
        "resolved": len(recs) - pending,
    }


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="attention_bus.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("root"); r.add_argument("--key", required=True)
    r.add_argument("--source", required=True); r.add_argument("--reason", required=True)
    r.add_argument("--kind", default=BLOCKED); r.add_argument("--work-id")
    v = sub.add_parser("resolve"); v.add_argument("root"); v.add_argument("--key", required=True)
    lst = sub.add_parser("list"); lst.add_argument("root")
    a = ap.parse_args(argv)
    if a.cmd == "record":
        ok = record(a.root, key=a.key, source=a.source, reason=a.reason, kind=a.kind,
                    work_id=getattr(a, "work_id", None))
        return 0 if ok else 1
    if a.cmd == "resolve":
        return 0 if resolve(a.root, a.key) else 1
    if a.cmd == "list":
        import json
        print(json.dumps(collect(a.root), ensure_ascii=False, indent=2, default=str))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
