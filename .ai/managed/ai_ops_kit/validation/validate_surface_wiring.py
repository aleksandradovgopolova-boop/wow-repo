#!/usr/bin/env python3
"""Validate surface-wiring (Вывод 2 «дефектов одной сессии»: core<->wrapper<->client wiring drift).

Дефект `/api/catalog`: путь умеет ЯДРО, покрыт тестами ядра И тестами клиента (клиент мокал fetch,
ядро звали напрямую) — оба слоя зелёные, а запрос до эндпоинта не доходит, т.к. маршрут не смонтирован
ни в одной ОБЁРТКЕ (прод/dev/serverless). Класс тот же, что `event_contract_consistency` (naming drift),
другая сущность — МАРШРУТ. Механически, без предметной области, по манифесту поверхности:
  (1) core ⊆ КАЖДАЯ обёртка       — путь ядра обязан быть смонтирован в каждом контуре;
  (2) client ⊆ union(обёртки)     — путь, который вызывает клиент, кем-то обслуживается;
  (3) union(обёртки) \\ client     — смонтировано, но никто не вызывает: мусор/незаконченная работа (advisory).
(1)+(2) -> ERROR (drift); (3) -> advisory. Пока гейт advisory (blocking:false до обкатки).

  validate_surface_wiring.py [manifest.json] | --selftest
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _covered(path, routes):
    """path обслуживается набором routes (точное совпадение или префиксный маунт)."""
    for r in routes:
        rb = r.rstrip("*").rstrip("/")
        if path == r or path == rb or path.startswith(rb + "/"):
            return True
    return False


def check(manifest):
    """manifest = {core:[route], wrappers:{name:[route]}, client:[path]}.
    -> {errors:[...], advisories:[...]} (errors=drift; пусто = поверхность согласована)."""
    if not isinstance(manifest, dict):
        return {"errors": ["manifest не объект"], "advisories": []}
    errors, advisories = [], []
    core = list(manifest.get("core") or [])
    wrappers = dict(manifest.get("wrappers") or {})
    client = list(manifest.get("client") or [])
    if not wrappers:
        errors.append("нет обёрток (wrappers) — нечему обслуживать маршруты ядра")
    # (1) core ⊆ каждая обёртка
    for name, mounted in wrappers.items():
        mset = set(mounted or [])
        for r in core:
            if r not in mset:
                errors.append(f"маршрут ядра '{r}' НЕ смонтирован в обёртке '{name}' (core<->wrapper drift)")
    # (2) client ⊆ union(обёртки)
    union = set()
    for mounted in wrappers.values():
        union |= set(mounted or [])
    for p in client:
        if not _covered(p, union):
            errors.append(f"клиент вызывает '{p}', но путь не обслуживается ни одной обёрткой (client<->wrapper drift)")
    # (3) смонтировано, но никто не вызывает -> advisory
    for r in sorted(union):
        if not any(_covered(c, [r]) for c in client):
            advisories.append(f"смонтировано, но никто не вызывает: '{r}' (забытый мусор или незаконченная работа)")
    return {"errors": errors, "advisories": advisories}


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 0
    res = check(json.loads(Path(args[0]).read_text(encoding="utf-8")))
    for x in res["errors"]:
        print(f"  DRIFT: {x}")
    for a in res["advisories"]:
        print(f"  advisory: {a}")
    print(f"surface-wiring: {'DRIFT' if res['errors'] else 'ok'}")
    return 1 if res["errors"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
