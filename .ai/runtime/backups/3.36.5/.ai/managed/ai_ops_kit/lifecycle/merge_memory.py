#!/usr/bin/env python3
"""merge→memory flow (v2.25) — обновление долговременной памяти при мердже WorkItem.

Когда работа доведена и смерджена, знание не должно теряться: что изменилось, какие
решения приняты, какие уроки. Инструмент фиксирует это как запись в
`memory/lessons-learned/<дата>-<id>.md` в формате репозиторной памяти (источник, owner,
дата проверки, условие устаревания). Дальше человек/куратор памяти уточняет.

«Авто» на реальном событии мерджа — шаг рантайма/CI (хук на merge ветки); кит даёт
детерминированный инструмент записи и flow (ai-finish-task), а не сам триггер.

Использование:
  merge_memory.py record <memory-dir> <id> --summary S [--areas a,b]
                  [--decisions "d1; d2"] [--lessons "l1; l2"] [--owner O] [--at DATE]
  merge_memory.py --selftest
Возврат 0 — записано, 1 — ошибка.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _today():
    from datetime import date
    return date.today().isoformat()


def _bullets(text):
    if not text:
        return []
    return [x.strip() for x in text.split(";") if x.strip()]


def record(memory_dir, wid, summary, areas=None, decisions=None, lessons=None,
           owner="repository-memory-curator", at=None, human_confirmed=False):
    if not summary:
        print("ОШИБКА: --summary обязателен (что изменилось за задачу).")
        return 1
    at = at or _today()
    # v3.7.1 (#4) governance-БАРЬЕР: merge-memory запись self-ingested. Без подтверждения человека
    # (human_confirmed) MemoryGovernancePolicy НЕ пропускает (анти-самоотравление) -> запись НЕ создаётся
    # (было advisory). Куратор подтверждает через --human-confirmed. Ровно заявленная граница.
    # v3.7.2 FAIL-CLOSED: ошибка governance-кода/enforcement -> запись НЕ создаётся (было except: pass ->
    # писала при сбое = fail-open). Барьер должен падать в блок, а не пропускать.
    _entry = {"id": str(wid), "self_ingested": True, "human_confirmed": bool(human_confirmed),
              "provenance": {"origin": f"merge WorkItem {wid}", "source_type": "derived",
                             "upstream": [f"WorkItem:{wid}"]},
              "expiry": {"mode": "review_date", "value": at}}
    try:
        from ai_ops_kit.security import security_enforcement as _se
        _ok, _viol = _se.enforce_memory_entry(_entry)
    except Exception as _ge:  # noqa: BLE001 — FAIL-CLOSED: сбой governance = не пишем
        print(f"BLOCKED (memory governance FAIL-CLOSED): сбой enforcement -> запись НЕ создана "
              f"({type(_ge).__name__}: {_ge})")
        return 1
    if not _ok:
        print("BLOCKED (memory governance): self-ingested запись не прошла MemoryGovernancePolicy "
              "(без --human-confirmed self-ingestion запрещена). Нарушения: " + "; ".join(_viol))
        return 1
    dst_dir = Path(memory_dir) / "lessons-learned"
    dst_dir.mkdir(parents=True, exist_ok=True)
    path = dst_dir / f"{at}-{wid}.md"

    lines = [f"# Merge-memory: {wid}", ""]
    lines += [f"- **Источник:** мердж WorkItem `{wid}` (merge→memory flow).",
              f"- **Owner:** {owner}.",
              f"- **Дата проверки:** {at}.",
              "- **Условие устаревания:** изменение затронутых зон следующей задачей.",
              ""]
    lines += ["## Что изменилось", "", summary, ""]
    if areas:
        lines += ["## Затронутые зоны", ""] + [f"- {a}" for a in areas] + [""]
    dec = _bullets(decisions)
    if dec:
        lines += ["## Принятые решения", "",
                  "> Значимые/необратимые — зафиксировать эпизодом в `decisions/registry.yaml`.",
                  ""] + [f"- {d}" for d in dec] + [""]
    les = _bullets(lessons)
    if les:
        lines += ["## Уроки", ""] + [f"{i}. {l}" for i, l in enumerate(les, 1)] + [""]

    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"MERGE-MEMORY: записано {path}")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(prog="merge_memory.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("memory_dir"); r.add_argument("id")
    r.add_argument("--summary", required=True)
    r.add_argument("--areas"); r.add_argument("--decisions"); r.add_argument("--lessons")
    r.add_argument("--owner", default="repository-memory-curator"); r.add_argument("--at")
    r.add_argument("--human-confirmed", action="store_true",
                   help="куратор подтвердил self-ingested запись (governance-барьер, v3.7.1)")
    ns = ap.parse_args(argv)
    if ns.cmd == "record":
        areas = [x.strip() for x in (ns.areas or "").split(",") if x.strip()]
        return record(ns.memory_dir, ns.id, ns.summary, areas, ns.decisions, ns.lessons,
                      ns.owner, ns.at, human_confirmed=ns.human_confirmed)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
