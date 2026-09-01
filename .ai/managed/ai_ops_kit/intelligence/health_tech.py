#!/usr/bin/env python3
"""Tech Health (PR-14): здоровье технологий Green / Yellow / Red С НАЗВАННОЙ ПРИЧИНОЙ.

Отдельно от здоровья продукта — отвечает на другой вопрос: в порядке ли CI/CD, тесты, lint,
security и зависимости. Разделяет с health_product общий язык band+причина (health_common),
не копирует свёртку (dp-001).

Источник — выгрузка `.ai-ops/tech-health.yaml`, которую в репозиторий кладёт CI (в чужой
CI кит не ходит — тот же принцип, что у events-are-verified-on-arrival). Формат:

    ci:           {status: passing}          # passing | failing | ...
    tests:        {passed: 268, total: 271}
    lint:         {errors: 0}
    security:     {critical: 0, high: 1}
    dependencies: {outdated: 2, total: 40}

Каждый под-сигнал, которого в выгрузке нет, даёт UNKNOWN с причиной, а НЕ зелёный. Нет
выгрузки вовсе — все сигналы unknown, итог unknown (не «всё в порядке»).

Использование:  python3 -m ai_ops_kit.intelligence.health_tech <repo_root> [-o report.json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from ai_ops_kit.intelligence import health_common as hc

KIND = "tech-health-report"
EXPORT_REL = ".ai-ops/tech-health.yaml"

# пороги долей (доля прошедших тестов; доля устаревших зависимостей)
_TESTS_GREEN, _TESTS_YELLOW = 0.98, 0.80


def _ci_signal(data: dict) -> hc.Signal:
    ci = data.get("ci")
    if not isinstance(ci, dict) or "status" not in ci:
        return hc.Signal("ci", hc.UNKNOWN,
                         "статус CI в выгрузке отсутствует — не проверено")
    status = str(ci["status"]).lower()
    if status == "passing":
        return hc.Signal("ci", hc.GREEN, "последний прогон CI зелёный")
    if status == "failing":
        return hc.Signal("ci", hc.RED, "последний прогон CI красный")
    return hc.Signal("ci", hc.UNKNOWN,
                     f"статус CI '{ci['status']}' не распознан — не проверено")


def _tests_signal(data: dict) -> hc.Signal:
    t = data.get("tests")
    if not isinstance(t, dict) or "passed" not in t or "total" not in t:
        return hc.Signal("tests", hc.UNKNOWN,
                         "результаты тестов в выгрузке отсутствуют — не проверено")
    total = float(t["total"])
    if total <= 0:
        return hc.Signal("tests", hc.UNKNOWN,
                         "в выгрузке 0 тестов — доля прошедших не определена")
    ratio = float(t["passed"]) / total
    if ratio >= _TESTS_GREEN:
        band, why = hc.GREEN, f"прошло {t['passed']}/{t['total']} тестов"
    elif ratio >= _TESTS_YELLOW:
        band, why = hc.YELLOW, f"прошло только {t['passed']}/{t['total']} тестов"
    else:
        band, why = hc.RED, f"провалено много тестов: прошло {t['passed']}/{t['total']}"
    return hc.Signal("tests", band, why, detail={"ratio": round(ratio, 4)})


def _lint_signal(data: dict) -> hc.Signal:
    lint = data.get("lint")
    if not isinstance(lint, dict) or "errors" not in lint:
        return hc.Signal("lint", hc.UNKNOWN,
                         "результат lint в выгрузке отсутствует — не проверено")
    errors = int(lint["errors"])
    if errors == 0:
        return hc.Signal("lint", hc.GREEN, "lint без ошибок")
    return hc.Signal("lint", hc.RED, f"lint: {errors} ошибок")


def _security_signal(data: dict) -> hc.Signal:
    sec = data.get("security")
    if not isinstance(sec, dict) or ("critical" not in sec and "high" not in sec):
        return hc.Signal("security", hc.UNKNOWN,
                         "данные security в выгрузке отсутствуют — не проверено")
    crit = int(sec.get("critical", 0))
    high = int(sec.get("high", 0))
    if crit > 0:
        return hc.Signal("security", hc.RED, f"{crit} критических уязвимостей")
    if high > 0:
        return hc.Signal("security", hc.YELLOW, f"{high} уязвимостей уровня high")
    return hc.Signal("security", hc.GREEN, "критических и high уязвимостей нет")


def _deps_signal(data: dict) -> hc.Signal:
    dep = data.get("dependencies")
    if not isinstance(dep, dict) or "outdated" not in dep:
        return hc.Signal("dependencies", hc.UNKNOWN,
                         "данные о зависимостях в выгрузке отсутствуют — не проверено")
    outdated = int(dep["outdated"])
    if outdated == 0:
        return hc.Signal("dependencies", hc.GREEN, "устаревших зависимостей нет")
    return hc.Signal("dependencies", hc.YELLOW, f"{outdated} устаревших зависимостей")


def collect_signals(root: Path) -> list:
    path = root / EXPORT_REL
    if not path.exists():
        why = (f"выгрузка технических сигналов ({EXPORT_REL}) отсутствует — "
               "её кладёт CI; без неё tech health не определить")
        return [hc.Signal(name, hc.UNKNOWN, why)
                for name in ("ci", "tests", "lint", "security", "dependencies")]
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"ожидался mapping, получен {type(data).__name__}")
    except (yaml.YAMLError, ValueError) as exc:
        why = f"выгрузка есть, но не разбирается ({exc}) — не проверено"
        return [hc.Signal(name, hc.UNKNOWN, why)
                for name in ("ci", "tests", "lint", "security", "dependencies")]
    return [_ci_signal(data), _tests_signal(data), _lint_signal(data),
            _security_signal(data), _deps_signal(data)]


def tech_health_report(root: Path) -> dict:
    return hc.build_report(KIND, collect_signals(root), scope="tech")


def main(argv) -> int:
    if not argv:
        print(__doc__)
        return 1
    root = Path(argv[0])
    if not root.is_dir():
        print(f"не каталог: {root}", file=sys.stderr)
        return 2
    report = tech_health_report(root)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if "-o" in argv:
        out = Path(argv[argv.index("-o") + 1])
        out.write_text(text + "\n", encoding="utf-8")
        tail = "" if report["complete"] else f", не проверено: {report['unverified']}"
        print(f"отчёт: {out} (tech health: {report['band']}{tail})")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
