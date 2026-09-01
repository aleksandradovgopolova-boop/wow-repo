#!/usr/bin/env python3
"""Непрерывный аудит продукта: машиночитаемый снимок состояния (PR-21).

`ai-ops audit product` отвечает на вопрос «в каком состоянии продуктовая операционка репозитория» —
одним машиночитаемым отчётом по осям: артефакты слоя, техническое состояние, delivery, backlog,
риски. Отчёт периодический и read-only: НИЧЕГО не меняет, только сообщает.

ТРЕТЬЕ СОСТОЯНИЕ НЕ РАВНО ВТОРОМУ. Ось, которую этот проход не умеет оценить (backlog требует
GitHub-интеграции ленты 3; реестр рисков — ленты 5), получает `unknown`, а НЕ `green`: «не проверено»
и «в порядке» — разные ответы, и подмена первого вторым зеленит непроверенное. `unknown` в вердикт
не сворачивается — он объявлен отдельно, чтобы было видно, чего аудит не знает.

Оси считаются из ФАКТОВ репозитория (состояние `.ai-ops/` по реестру, CI/тесты, история релизов),
а не из деклараций. Аудит зовут `ai-ops audit product` и (периодически) ночной обзор.

  product_audit.py <repo> [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ai_ops_kit.planning import product_templates, repo_audit

GREEN, YELLOW, RED, UNKNOWN = "green", "yellow", "red", "unknown"
_ORDER = {GREEN: 0, YELLOW: 1, RED: 2}   # для вердикта: хуже = больше; UNKNOWN вне порядка


def _artifacts_axis(repo_root: Path) -> dict:
    """Ось артефактов слоя из состояния Missing/Invalid/Outdated/Valid (PR-5)."""
    try:
        rep = product_templates.report(repo_root)
    except Exception as e:                             # noqa: BLE001 — нет реестра != всё хорошо
        return {"status": UNKNOWN, "detail": f"состояние слоя не посчитать: {type(e).__name__}: {e}"}
    c = rep["counts"]
    total = sum(c.values())
    if c["missing"] or c["invalid"]:
        status = RED
    elif c["outdated"]:
        status = YELLOW
    else:
        status = GREEN
    return {"status": status, "counts": c,
            "detail": f"Valid {c['valid']}/{total}"
                      + (f", Outdated {c['outdated']}" if c["outdated"] else "")
                      + (f", Invalid {c['invalid']}" if c["invalid"] else "")
                      + (f", Missing {c['missing']}" if c["missing"] else "")}


def _tech_axis(ev: dict) -> dict:
    """Техническое состояние из наблюдаемого: CI + тесты. unknown, если дерево нечитаемо."""
    if not ev.get("tree_readable"):
        return {"status": UNKNOWN, "detail": "дерево репозитория нечитаемо"}
    ci, tests = bool(ev.get("ci")), (ev.get("test_files") or 0) > 0
    if ci and tests:
        return {"status": GREEN, "detail": "есть CI и тесты"}
    if ci or tests:
        return {"status": YELLOW,
                "detail": "есть " + ("CI, но тестов не видно" if ci else "тесты, но CI не найден")}
    return {"status": RED, "detail": "ни CI, ни тестов не найдено"}


def _delivery_axis(ev: dict) -> dict:
    """Delivery из истории релизов. Нет тегов, но есть коммиты -> yellow; нет истории вовсе -> unknown."""
    rel = ev.get("release_history")
    if rel:
        return {"status": GREEN, "detail": f"есть история релизов ({len(rel)} последних тегов)"}
    if ev.get("commits"):
        return {"status": YELLOW,
                "detail": "коммиты есть, но релизов (тегов) нет — delivery ещё не оформлен"}
    return {"status": UNKNOWN, "detail": "истории git нет — delivery оценить нечем"}


def _backlog_axis() -> dict:
    return {"status": UNKNOWN,
            "detail": "backlog оценивается через GitHub Issues (Backlog Intelligence); в этом "
                      "проходе не запрашивался"}


def _risk_axis() -> dict:
    return {"status": UNKNOWN,
            "detail": "реестр рисков (AI Product Operations) в этом проходе не оценивался"}


def audit(repo_root: Path) -> dict:
    """Аудит продукта. -> машиночитаемый отчёт с осями и вердиктом.

    Вердикт — худшая из ОЦЕНЁННЫХ осей (green<yellow<red). `unknown`-оси в вердикт не входят, но
    перечислены: аудит, назвавший всё зелёным, скрыв неизвестное, был бы хуже отсутствующего.
    """
    root = Path(repo_root)
    ev = repo_audit.discover(root)
    dims = {
        "artifacts": _artifacts_axis(root),
        "tech": _tech_axis(ev),
        "delivery": _delivery_axis(ev),
        "backlog": _backlog_axis(),
        "risk": _risk_axis(),
    }
    evaluated = [k for k, v in dims.items() if v["status"] in _ORDER]
    unknown = [k for k, v in dims.items() if v["status"] == UNKNOWN]
    worst = max((dims[k]["status"] for k in evaluated), key=lambda s: _ORDER[s], default=GREEN)
    return {"schema_version": 1, "kind": "product-audit",
            "repository": root.resolve().name,
            "dimensions": dims, "evaluated": evaluated, "unknown": unknown,
            "verdict": worst}


_MARK = {GREEN: "🟢", YELLOW: "🟡", RED: "🔴", UNKNOWN: "⚪"}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="product_audit.py")
    ap.add_argument("repo")
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])
    rep = audit(Path(ns.repo))
    if ns.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep["verdict"] != RED else 1
    print(f"АУДИТ ПРОДУКТА · {rep['repository']} · вердикт {rep['verdict'].upper()}")
    for name, v in rep["dimensions"].items():
        print(f"  {_MARK.get(v['status'], '?')} {name:10} {v['status']:8} — {v['detail']}")
    if rep["unknown"]:
        print(f"\nНЕ ОЦЕНЕНО (unknown, не свёрнуто в green): {', '.join(rep['unknown'])}")
    return 0 if rep["verdict"] != RED else 1


if __name__ == "__main__":
    sys.exit(main())
