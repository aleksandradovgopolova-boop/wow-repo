#!/usr/bin/env python3
"""Team Synchronization (PR-12): авто-статус команды — не собирается вручную.

Кит сам сводит текущий статус: здоровье (product/tech/delivery), риски с действиями, блокеры,
ближайшие задачи, прогресс milestone, слепые зоны. Это АГРЕГАТОР — он НЕ считает заново, а сводит
уже посчитанное (dp-001): health-отчёты, risk_register и совет next_work. Свои калькуляторы не
дублирует.

Инвариант сохраняется через источники: чего они не смогли проверить, здесь тоже «не проверено»
(milestone без выгрузки — unknown; план не читается — ближайшие задачи unknown), а не выдуманный
позитив.

Использование:  python3 -m ai_ops_kit.intelligence.team_sync <repo_root> [-o report.json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from ai_ops_kit.intelligence import health_delivery, risk_register
from ai_ops_kit.planning import next_work

KIND = "team-status"


def _health_line(report: dict) -> dict:
    return {"band": report["band"], "reasons": report["reasons"], "complete": report["complete"]}


def _plan_view(root: Path):
    """Совет next_work один раз (он же читает git/holders) -> (nw, причина-если-нет)."""
    try:
        nw = next_work.compute(root)
    except Exception as exc:                              # noqa: BLE001 — статус не обязан падать
        return None, f"план не прочитан ({exc})"
    if not nw.get("plan_present"):
        return None, "плана работ нет — состояние работ не определить"
    return nw, None


def _next_tasks(nw, err, limit: int = 5) -> dict:
    if nw is None:
        return {"status": "unknown", "reason": err, "tasks": []}
    ready = nw.get("ready") or []
    tasks = [{"id": r["id"], "title": r.get("title"), "score": r.get("score")} for r in ready[:limit]]
    return {"status": "ok", "next_best": (nw.get("next_best") or {}).get("id"), "tasks": tasks}


def _blockers(nw, err) -> dict:
    if nw is None:
        return {"status": "unknown", "reason": err, "blocked": []}
    blocked = nw.get("blocked") or []
    return {"status": "ok",
            "blocked": [{"id": b["id"], "title": b.get("title")} for b in blocked]}


def _milestone(root: Path) -> dict:
    path = root / health_delivery.SIGNALS_REL
    if not path.exists():
        return {"status": "unknown",
                "reason": f"выгрузка delivery ({health_delivery.SIGNALS_REL}) отсутствует — "
                          "прогресс milestone не определить"}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        ms = data.get("milestone") if isinstance(data, dict) else None
        if not isinstance(ms, dict) or "done" not in ms or "total" not in ms:
            raise ValueError("нет milestone.done/total")
    except (yaml.YAMLError, ValueError) as exc:
        return {"status": "unknown", "reason": f"выгрузка не даёт прогресс ({exc})"}
    return {"status": "ok", "done": ms["done"], "total": ms["total"],
            "forecast": (data.get("forecast") if isinstance(data, dict) else None)}


def team_status(root: Path) -> dict:
    reports = risk_register.build_reports(root)
    risks = risk_register.risk_register(root, reports=reports)
    nw, plan_err = _plan_view(root)
    return {
        "schema_version": 1,
        "kind": KIND,
        "root": str(root),
        "health": {
            "product": _health_line(reports["product"]),
            "tech": _health_line(reports["tech"]),
            "delivery": _health_line(reports["delivery"]),
        },
        "risks": {
            "count_by_severity": risks["count_by_severity"],
            "top": [r for r in risks["risks"] if r["severity"] == risk_register.HIGH][:5]
            or risks["risks"][:5],
        },
        "blockers": _blockers(nw, plan_err),
        "next_tasks": _next_tasks(nw, plan_err),
        "milestone": _milestone(root),
        "blind_spots": risks["blind_spots"],
    }


def _render(status: dict) -> str:
    h = status["health"]
    lines = ["СТАТУС КОМАНДЫ", ""]
    lines.append(f"Здоровье: продукт {h['product']['band']}, тех {h['tech']['band']}, "
                 f"delivery {h['delivery']['band']}")
    sev = status["risks"]["count_by_severity"]
    lines.append(f"Риски: high {sev['high']}, medium {sev['medium']}")
    for r in status["risks"]["top"]:
        lines.append(f"  • [{r['severity']}] {r['category']}: {r['mitigation']}")
    bl = status["blockers"]
    if bl["status"] == "ok":
        lines.append(f"Блокеров: {len(bl['blocked'])}")
    else:
        lines.append(f"Блокеры: не проверено ({bl['reason']})")
    nt = status["next_tasks"]
    if nt["status"] == "ok":
        lines.append("Ближайшее: " + (nt.get("next_best") or "—"))
    else:
        lines.append(f"Ближайшие задачи: не проверено ({nt['reason']})")
    ms = status["milestone"]
    if ms["status"] == "ok":
        lines.append(f"Milestone: {ms['done']}/{ms['total']}")
    else:
        lines.append(f"Milestone: не проверено ({ms['reason']})")
    if status["blind_spots"]:
        lines.append(f"Слепые зоны ({len(status['blind_spots'])}): " + ", ".join(status["blind_spots"][:6]))
    return "\n".join(lines)


def main(argv) -> int:
    if not argv:
        print(__doc__)
        return 1
    args = [a for a in argv if a != "--text"]
    root = Path(args[0])
    if not root.is_dir():
        print(f"не каталог: {root}", file=sys.stderr)
        return 2
    status = team_status(root)
    if "--text" in argv:
        print(_render(status))
        return 0
    text = json.dumps(status, indent=2, ensure_ascii=False)
    if "-o" in args:
        out = Path(args[args.index("-o") + 1])
        out.write_text(text + "\n", encoding="utf-8")
        print(f"статус: {out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
