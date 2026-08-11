#!/usr/bin/env python3
"""ROADMAP репозитория: направление продукта, а не таск-трекер (v3.35.0).

Контракт — четыре горизонта, каждый отвечает на свой вопрос:

    Сейчас              — какие проблемы и цели главные;
    Следующий результат — что должно ИЗМЕНИТЬСЯ ДЛЯ ПОЛЬЗОВАТЕЛЯ;
    Дальше              — следующие крупные capability/этапы;
    Later               — идеи, которые осознанно не берём сейчас.

Последний горизонт не декоративный: «осознанно не берём» — решение, и записанное решение отличает
roadmap от списка желаний.

ROADMAP — НЕ БЭКЛОГ. «Сделать API», «добавить кнопку» — это работа, её место в `planning/plan.yaml`.
Здесь — «пользователь может создать дашборд естественным запросом и получить проверяемый
результат». Кит не умеет отличать цель от задачи по смыслу и не делает вид, что умеет: он ловит
формальный признак — элемент, начинающийся с глагола в инфинитиве («сделать», «добавить»,
«реализовать», «починить», «настроить»), — и сообщает это ПРЕДУПРЕЖДЕНИЕМ, а не ошибкой. Признак
эвристический, поэтому и вес у него такой.

СВЯЗЬ С ПЛАНОМ. Цель объявляется id в обратных кавычках: `- `dashboard-from-prompt` — описание`.
Отсюда — две проверяемые находки: цель под «Сейчас» без работы в плане (направление без работы)
и цель плана, которой нет в roadmap (работа без направления). Обе — тихие расхождения, которые
иначе живут месяцами.

Использование:  roadmap.py check <repo> [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROADMAP_REL = "ROADMAP.md"

# Горизонты и их синонимы: репозиторий вправе писать «Next outcome» вместо «Следующий результат».
HORIZONS = [
    ("now", ["сейчас", "now"]),
    ("next_outcome", ["следующий результат", "next outcome", "next"]),
    ("later_major", ["дальше", "further", "then"]),
    ("someday", ["later", "когда-нибудь", "не берём", "не берем"]),
]

_GOAL = re.compile(r"^\s*[-*]\s*`([A-Za-z][A-Za-z0-9_-]{0,63})`")
_IMPERATIVE = re.compile(
    r"^\s*[-*]\s*(?:`[^`]*`\s*[—-]\s*)?"
    r"(сделать|добавить|реализовать|починить|исправить|настроить|внедрить|переписать|создать)\b",
    re.IGNORECASE)


def roadmap_path(child_root) -> Path:
    return Path(child_root) / ROADMAP_REL


def parse(text: str) -> dict:
    """Разбор ROADMAP.md по горизонтам. -> {horizon: {"heading": str, "items": [str], "goals": [str]}}

    Разбор построчный, а не по markdown-дереву: зависимости на парсер markdown в ките нет
    (инвариант «stdlib + pyyaml»), а заголовок уровня 2–3 распознаётся однозначно.
    """
    out = {k: {"heading": None, "items": [], "goals": []} for k, _ in HORIZONS}
    cur = None
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if line.startswith("#"):
            title = line.lstrip("#").strip().lower()
            cur = None
            for key, names in HORIZONS:
                if any(title.startswith(n) or title == n for n in names):
                    cur = key
                    out[key]["heading"] = line.lstrip("#").strip()
                    break
            continue
        if cur and line.strip().startswith(("-", "*")):
            out[cur]["items"].append(line.strip())
            m = _GOAL.match(line)
            if m:
                out[cur]["goals"].append(m.group(1))
    return out


def load(child_root):
    p = roadmap_path(child_root)
    if not p.is_file():
        return None
    return parse(p.read_text(encoding="utf-8"))


def check(child_root, plan=None):
    """Контракт roadmap + связь с планом. -> {"errors": [...], "warnings": [...], "horizons": {...}}

    Отсутствие ROADMAP.md — ошибка контура, а не мелочь: репозиторий, который не знает, куда идёт,
    не может ответить на вопрос «что важнее сейчас», и любой ответ `next` будет про порядок строк
    в бэклоге, а не про продукт.
    """
    rm = load(child_root)
    errors, warns = [], []
    if rm is None:
        return {"errors": [f"нет {ROADMAP_REL} — направление продукта не является артефактом"],
                "warnings": [], "horizons": {}}

    for key, names in HORIZONS:
        if not rm[key]["heading"]:
            sev = errors if key in ("now", "next_outcome") else warns
            sev.append(f"нет горизонта '{names[0]}' — "
                       + {"now": "неизвестно, что главное сейчас",
                          "next_outcome": "неизвестно, что должно измениться для пользователя",
                          "later_major": "не объявлены следующие крупные этапы",
                          "someday": "не записано, что осознанно НЕ берём (решение тоже артефакт)"}[key])
    if rm["now"]["heading"] and not rm["now"]["items"]:
        errors.append("горизонт 'Сейчас' пуст — направление объявлено заголовком, но не содержанием")

    for key in ("now", "next_outcome"):
        for it in rm[key]["items"]:
            if _IMPERATIVE.match(it):
                warns.append(f"горизонт '{key}': элемент похож на ЗАДАЧУ, а не на результат "
                             f"({it[:70]}…) — работа живёт в planning/plan.yaml, здесь направление")

    if plan is not None:
        from ai_ops_kit.planning import delivery_plan as _plan
        plan_goals = {g["id"] for g in _plan.goals(plan)}
        active_goals = {g["id"] for g in _plan.goals(plan) if (g.get("status") or "active") == "active"}
        rm_goals = set(rm["now"]["goals"]) | set(rm["next_outcome"]["goals"])
        work_goals = set()
        single = list(plan_goals)[0] if len(plan_goals) == 1 else None
        for w in _plan.items(plan):
            g = w.get("goal") or single
            if g:
                work_goals.add(g)
        for g in sorted(rm_goals - work_goals):
            warns.append(f"цель '{g}' объявлена в roadmap, но в плане нет ни одной работы под неё "
                         f"— направление без работы")
        for g in sorted(active_goals - rm_goals):
            warns.append(f"цель '{g}' активна в плане, но её нет в roadmap под «Сейчас»/«Следующий "
                         f"результат» — работа без направления")

    return {"errors": errors, "warnings": warns, "horizons": rm}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="roadmap.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check"); c.add_argument("repo"); c.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])
    root = Path(ns.repo)
    pl = None
    try:
        from ai_ops_kit.planning import delivery_plan as _plan
        pl = _plan.load(root)
    except Exception:                                  # noqa: BLE001 — план разбирает свой валидатор
        pl = None
    rep = check(root, pl)
    if ns.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        for e in rep["errors"]:
            print(f"  ✗ {e}")
        for w in rep["warnings"]:
            print(f"  ⚠ {w}")
        print(f"ROADMAP: ошибок {len(rep['errors'])}, предупреждений {len(rep['warnings'])}")
    return 1 if rep["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
