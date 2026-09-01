#!/usr/bin/env python3
"""Product Health (PR-13): Green / Yellow / Red С НАЗВАННЫМИ ПРИЧИНАМИ из состояния репо.

Считается сразу, без зависимости от других лент — читает продуктовые сигналы, доступные в
самом репозитории:

  1. Экспорт продуктовых метрик (`.ai-ops/product-metrics.yaml`) — если он есть, счёт делает
     СУЩЕСТВУЮЩИЙ калькулятор `product_health.compute` (dp-001: не дублируем), а band его
     healthy/warning/critical переводится в green/yellow/red.
  2. Product Passport (`.ai-ops/PRODUCT_PASSPORT.md`) — обязательный артефакт продукта (Phase 1).
  3. Roadmap (`.ai-ops/ROADMAP.md`) — направление продукта.

Инвариант (health_common): «не проверено» ≠ «в порядке». Отсутствующий сигнал даёт UNKNOWN с
причиной, а НЕ зелёный. Если ни одного продуктового сигнала прочитать нельзя — итог UNKNOWN,
а не green.

Health tech (CI/тесты/lint) и health delivery (milestone/velocity) — соседние модули; здесь
только продуктовое измерение, чтобы сигналы одного измерения не растекались в другое.

Использование:  python3 -m ai_ops_kit.intelligence.health_product <repo_root> [-o report.json]
Возврат 0 — успех (band любой: отчёт — данные, решение за человеком/риск-движком).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from ai_ops_kit.intelligence import health_common as hc
from ai_ops_kit.intelligence import product_health

KIND = "product-health-report"

# перевод band калькулятора метрик -> цвет светофора
_METRIC_BAND = {"healthy": hc.GREEN, "warning": hc.YELLOW, "critical": hc.RED}

# где в подключённом репозитории лежат продуктовые артефакты (Product Operating Layer, Phase 1)
METRICS_REL = ".ai-ops/product-metrics.yaml"
PASSPORT_REL = ".ai-ops/PRODUCT_PASSPORT.md"
ROADMAP_REL = ".ai-ops/ROADMAP.md"


def _metrics_signal(root: Path) -> hc.Signal:
    path = root / METRICS_REL
    if not path.exists():
        return hc.Signal(
            "product_metrics", hc.UNKNOWN,
            f"экспорт продуктовых метрик ({METRICS_REL}) отсутствует — "
            "здоровье по метрикам не определить",
        )
    try:
        inp = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        report = product_health.compute(inp)
    except (yaml.YAMLError, SystemExit, ValueError) as exc:
        return hc.Signal(
            "product_metrics", hc.UNKNOWN,
            f"метрики есть, но не разбираются ({exc}) — здоровье по метрикам не определить",
        )
    score = report["health_score"]["value"]
    band = _METRIC_BAND[report["health_score"]["band"]]
    findings = report.get("findings") or []
    if band == hc.GREEN:
        reason = f"продуктовые метрики: score {score}/100, все выше порога"
    else:
        worst = findings[0] if findings else "нет деталей"
        reason = f"продуктовые метрики: score {score}/100; слабое место — {worst}"
    return hc.Signal("product_metrics", band, reason,
                     detail={"score": score, "findings": findings})


def _passport_signal(root: Path) -> hc.Signal:
    path = root / PASSPORT_REL
    if not path.exists():
        return hc.Signal(
            "product_passport", hc.UNKNOWN,
            f"Product Passport ({PASSPORT_REL}) отсутствует — создаётся в Phase 1; "
            "здоровье продукта по паспорту не определить",
        )
    if not path.read_text(encoding="utf-8").strip():
        return hc.Signal(
            "product_passport", hc.YELLOW,
            "Product Passport есть, но пустой — обязательный артефакт продукта не заполнен",
        )
    return hc.Signal("product_passport", hc.GREEN,
                     "Product Passport на месте и не пустой")


def _roadmap_signal(root: Path) -> hc.Signal:
    path = root / ROADMAP_REL
    if not path.exists():
        return hc.Signal(
            "product_roadmap", hc.UNKNOWN,
            f"Roadmap ({ROADMAP_REL}) отсутствует — направление продукта не определить",
        )
    if not path.read_text(encoding="utf-8").strip():
        return hc.Signal(
            "product_roadmap", hc.YELLOW,
            "Roadmap есть, но пустой — направление продукта не описано",
        )
    return hc.Signal("product_roadmap", hc.GREEN, "Roadmap на месте и не пустой")


def collect_signals(root: Path) -> list:
    """Прочитать все продуктовые сигналы из репозитория `root`."""
    return [_metrics_signal(root), _passport_signal(root), _roadmap_signal(root)]


def product_health_report(root: Path) -> dict:
    """Собрать product-health отчёт: band + названные причины + непроверенное."""
    return hc.build_report(KIND, collect_signals(root), scope="product")


def main(argv) -> int:
    if not argv:
        print(__doc__)
        return 1
    root = Path(argv[0])
    if not root.is_dir():
        print(f"не каталог: {root}", file=sys.stderr)
        return 2
    report = product_health_report(root)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if "-o" in argv:
        out = Path(argv[argv.index("-o") + 1])
        out.write_text(text + "\n", encoding="utf-8")
        band = report["band"]
        tail = "" if report["complete"] else f", не проверено: {report['unverified']}"
        print(f"отчёт: {out} (product health: {band}{tail})")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
