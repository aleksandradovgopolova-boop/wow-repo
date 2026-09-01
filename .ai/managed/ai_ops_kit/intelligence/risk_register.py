#!/usr/bin/env python3
"""Risk Management (PR-16): риски продукта с ПРЕДЛОЖЕННЫМ ДЕЙСТВИЕМ.

Риск без mitigation — половина ответа, поэтому каждый риск несёт предложенное действие. Риски НЕ
гадаются отдельно — они ВЫВОДЯТСЯ из уже посчитанных сигналов health (product/tech/delivery) и
drift между артефактами (dp-001: не заводим второй источник правды о состоянии). Красный сигнал →
риск high, жёлтый → medium; каждый риск называет, из какого сигнала выведен.

Шесть категорий по PR-16: product, delivery, technical, dependency, resource, strategic. Отображение
сигнала в категорию — таблица ниже.

Честность: зелёный сигнал риска не даёт; UNKNOWN (не проверено) — это НЕ риск (иначе «не знаю»
превратилось бы в «плохо»), но перечисляется отдельным списком `blind_spots`, чтобы слепая зона не
выглядела благополучием. Пустые сигналы → пустой список рисков, а не выдуманные.

Использование:  python3 -m ai_ops_kit.intelligence.risk_register <repo_root> [-o report.json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ai_ops_kit.intelligence import (
    drift_artifacts,
    health_common as hc,
    health_delivery,
    health_product,
    health_tech,
)

KIND = "risk-register"

HIGH, MEDIUM = "high", "medium"
_SEVERITY_OF_BAND = {hc.RED: HIGH, hc.YELLOW: MEDIUM}

# сигнал health -> категория риска + шаблон предложенного действия.
# ключ: (scope, signal_name). Действие дополняется причиной сигнала при формировании риска.
_SIGNAL_RISK = {
    ("product", "product_metrics"): ("product", "разобрать просевшую метрику и вернуть её выше порога"),
    ("product", "product_passport"): ("product", "заполнить/обновить Product Passport"),
    ("product", "product_roadmap"): ("strategic", "актуализировать roadmap продукта"),
    ("tech", "ci"): ("technical", "починить CI до следующего релиза"),
    ("tech", "tests"): ("technical", "поднять долю прошедших тестов, разобрать упавшие"),
    ("tech", "lint"): ("technical", "устранить ошибки статического анализа"),
    ("tech", "security"): ("technical", "закрыть уязвимости, начиная с критических"),
    ("tech", "dependencies"): ("dependency", "обновить устаревшие зависимости"),
    ("delivery", "blocked_work"): ("delivery", "разблокировать работы, закрыв их зависимости"),
    ("delivery", "milestone"): ("delivery", "пересобрать план milestone или сдвинуть срок осознанно"),
}

# drift-пара -> категория риска + действие
_DRIFT_RISK = {
    "документация↔код": ("technical", "привести документацию в соответствие коду (или наоборот)"),
    "roadmap↔backlog": ("strategic", "свести roadmap с backlog"),
    "backlog↔delivery": ("delivery", "свести backlog с delivery-планом"),
    "Passport↔факт": ("product", "обновить Product Passport под фактическое состояние"),
}


def _risk(category, severity, source, description, mitigation) -> dict:
    return {"category": category, "severity": severity, "source": source,
            "description": description, "mitigation": mitigation}


def _risks_from_health(scope: str, report: dict) -> list:
    risks = []
    for sig in report.get("signals", []):
        band = sig.get("band")
        severity = _SEVERITY_OF_BAND.get(band)
        if severity is None:            # green или unknown — не риск
            continue
        cat, action = _SIGNAL_RISK.get((scope, sig["name"]), (scope, "разобрать сигнал"))
        risks.append(_risk(cat, severity, f"health:{scope}:{sig['name']}",
                           sig.get("reason", ""), action))
    return risks


def _risks_from_drift(report: dict) -> list:
    risks = []
    for pair in report.get("pairs", []):
        if pair.get("status") != drift_artifacts.DRIFT:
            continue
        cat, action = _DRIFT_RISK.get(pair["pair"], ("technical", "устранить расхождение"))
        risks.append(_risk(cat, HIGH, f"drift:{pair['pair']}", pair.get("reason", ""), action))
    return risks


def _blind_spots(reports: dict) -> list:
    spots = []
    for scope in ("product", "tech", "delivery"):
        for name in reports[scope].get("unverified", []):
            spots.append(f"health:{scope}:{name}")
    for name in reports["drift"].get("unverified", []):
        spots.append(f"drift:{name}")
    return spots


def build_reports(root: Path) -> dict:
    return {
        "product": health_product.product_health_report(root),
        "tech": health_tech.tech_health_report(root),
        "delivery": health_delivery.delivery_health_report(root),
        "drift": drift_artifacts.drift_report(root),
    }


def risk_register(root: Path, reports=None) -> dict:
    reports = reports or build_reports(root)
    risks = []
    for scope in ("product", "tech", "delivery"):
        risks.extend(_risks_from_health(scope, reports[scope]))
    risks.extend(_risks_from_drift(reports["drift"]))
    by_severity = {HIGH: 0, MEDIUM: 0}
    by_category = {}
    for r in risks:
        by_severity[r["severity"]] += 1
        by_category[r["category"]] = by_category.get(r["category"], 0) + 1
    return {
        "schema_version": 1,
        "kind": KIND,
        "root": str(root),
        "risks": risks,
        "count_by_severity": by_severity,
        "count_by_category": by_category,
        "blind_spots": _blind_spots(reports),
    }


def main(argv) -> int:
    if not argv:
        print(__doc__)
        return 1
    root = Path(argv[0])
    if not root.is_dir():
        print(f"не каталог: {root}", file=sys.stderr)
        return 2
    report = risk_register(root)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if "-o" in argv:
        out = Path(argv[argv.index("-o") + 1])
        out.write_text(text + "\n", encoding="utf-8")
        n, h = len(report["risks"]), report["count_by_severity"][HIGH]
        print(f"отчёт: {out} (рисков {n}, high {h}, слепых зон {len(report['blind_spots'])})")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
