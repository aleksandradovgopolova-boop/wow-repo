#!/usr/bin/env python3
"""Atomic Planning и Context Budget -> WorkPackagePlan (v2.100, эпик Context Engineering, этап 4).

Размер рабочего пакета должен соответствовать способности модели выполнить его ДО деградации
контекста. Оцениваем пакет и предлагаем декомпозицию, когда он слишком велик.

Оценка пакета: предполагаемый объём контекста (из ContextBundle), число файлов, число системных
границ (подсистем), зависимости, ожидаемые model calls, риск, критерий завершения.

Декомпозиция предлагается, если:
  * контекст превышает бюджет;
  * затрагивается слишком много подсистем (системных границ);
  * задача помечена как несколько независимых результатов;
  * требуется больше одного логически завершённого commit;
  * план нельзя проверить одним набором критериев;
  * размер задачи large/xl.

Ограничение (инвариант): автодекомпозиция НЕ меняет продуктовый смысл — она лишь называет ОСИ
разбиения (по подсистемам / по результатам), а не выдумывает новые бизнес-решения ради удобства
модели. Итоговое разбиение подтверждает человек.

Использование:
  atomic_planner.py assess <child_root> --signals '{...}' [--budget N] [--json]
  atomic_planner.py --selftest
Возврат 0 — пакет атомарен; 1 — нужна декомпозиция (или ошибка).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402
MAX_SUBSYSTEMS = 2          # больше системных границ на пакет -> кандидат на разбиение
SIZE_FILES = {"small": 3, "medium": 8, "large": 20, "xl": 40}


def estimate(signals, child_root=None, bundle=None):
    """Оценка рабочего пакета. Детерминированно; бюджет/токены — из ContextBundle, если доступен."""
    signals = dict(signals or {})
    subsystems = sorted(set(signals.get("affected_areas") or []))
    size = (signals.get("size") or "medium").lower()

    context_tokens, budget = None, None
    if bundle is None and child_root is not None:
        try:
            from ai_ops_kit.context import context_compiler
            bundle = context_compiler.compile_bundle(signals, child_root)
        except Exception:  # noqa: BLE001
            bundle = None
    if bundle:
        context_tokens = bundle.get("estimated_tokens")
        budget = bundle.get("context_budget")

    files_estimate = len(bundle["included"]["files"]) if bundle and bundle["included"]["files"] \
        else SIZE_FILES.get(size, 8)
    return {
        "estimated_context_tokens": context_tokens,
        "context_budget": budget,
        "files_estimate": files_estimate,
        "subsystems": subsystems,
        "dependencies": list(signals.get("depends_on") or []),
        "expected_model_calls": signals.get("expected_model_calls"),
        "risk": (signals.get("risk") or "").lower() or None,
        "completion_criterion": signals.get("completion_criterion")
                                or "один проверяемый результат (уточнить)",
    }


def assess(signals, child_root=None, bundle=None, budget=None):
    """Собрать WorkPackagePlan: оценка + нужна ли декомпозиция + оси разбиения. Детерминированно."""
    signals = dict(signals or {})
    est = estimate(signals, child_root=child_root, bundle=bundle)
    size = (signals.get("size") or "medium").lower()
    reasons, axes = [], []

    eff_budget = budget or est.get("context_budget")
    tok = est.get("estimated_context_tokens")
    if tok is not None and eff_budget and tok > eff_budget:
        reasons.append(f"контекст {tok} ток. превышает бюджет {eff_budget} — разбить по объёму")
        axes.append("by-context-budget")
    if len(est["subsystems"]) > MAX_SUBSYSTEMS:
        reasons.append(f"{len(est['subsystems'])} системных границ ({', '.join(est['subsystems'])}) "
                       f"> {MAX_SUBSYSTEMS} — разбить по подсистемам")
        axes.append("by-subsystem")
    if int(signals.get("independent_results") or 1) > 1:
        reasons.append(f"{signals['independent_results']} независимых результата(ов) — разбить по результатам")
        axes.append("by-result")
    if signals.get("multiple_commits") is True:
        reasons.append("требуется больше одного логически завершённого commit — по одному пакету на commit")
        axes.append("by-commit")
    if signals.get("single_criteria_verifiable") is False:
        reasons.append("план нельзя проверить одним набором критериев — разбить до проверяемых единиц")
        axes.append("by-verifiable-unit")
    if size in ("large", "xl"):
        reasons.append(f"размер задачи {size} — кандидат на разбиение до атомарных пакетов")
        axes.append("by-size")

    should = bool(reasons)
    # уникальные оси, стабильный порядок
    seen, uniq_axes = set(), []
    for a in axes:
        if a not in seen:
            seen.add(a); uniq_axes.append(a)

    return {
        "schema_version": 1, "kind": "WorkPackagePlan",
        "estimate": est,
        "should_decompose": should,
        "decomposition_reasons": reasons,
        "decomposition_axes": uniq_axes,
        "atomic": not should,
        "constraint_note": "декомпозиция называет ОСИ разбиения, но НЕ меняет продуктовый смысл и "
                           "не принимает новых бизнес-решений ради удобства модели; итог подтверждает человек",
        "acceptance": [
            "один проверяемый результат на пакет",
            "каждый пакет — отдельный commit",
            "зависимости между пакетами явные; пакет не стартует без подтверждённой зависимости",
        ],
    }


# v2.111: приоритет оси разбиения (детерминированно берём ПЕРВУЮ применимую как основную).
_AXIS_PRIORITY = ["by-subsystem", "by-result", "by-commit", "by-verifiable-unit",
                  "by-context-budget", "by-size"]


def _scope_paths(subsystems):
    """v2.123 (P0.3): пути write-scope пакета по подсистемам (эвристика раскладки: <sub>/, src/<sub>/,
    tests/<sub>/, test_<sub>). Покрывает и flat-, и src-layout. 'unspecified'/пусто -> нет путей."""
    paths = []
    for s in (subsystems or []):
        if not s or s == "unspecified":
            continue
        paths += [f"{s}/", f"src/{s}/", f"tests/{s}/", f"test_{s}"]
    return sorted(set(paths))


def decompose(signals, wid=None, child_root=None, bundle=None, budget=None):
    """v2.111: если пакет НЕ атомарен — построить КОНКРЕТНЫЕ WorkPackages (не только назвать оси).

    Каждый пакет: {id, title, axis, scope, depends_on, acceptance, order}. Разбиение детерминированно
    по ОСНОВНОЙ оси (первая применимая по приоритету). Инвариант: не выдумываем новых бизнес-решений —
    для subsystem/result оси разбиваем по реальным данным сигналов; для остальных (size/commit/бюджет)
    даём 2 последовательных пакета part-1/part-2 c пометкой, что дробление уточняет человек.
    -> {..assess.., "work_packages": [...], "primary_axis": ..} (work_packages пуст, если атомарно)."""
    wp = assess(signals, child_root=child_root, bundle=bundle, budget=budget)
    wid = str(wid or (signals or {}).get("feature") or "wi")
    packages = []
    if wp["should_decompose"]:
        axes = wp["decomposition_axes"]
        primary = next((a for a in _AXIS_PRIORITY if a in axes), axes[0])
        subsystems = wp["estimate"]["subsystems"]
        base_acc = wp["acceptance"]
        if primary == "by-subsystem" and subsystems:
            for i, sub in enumerate(subsystems):
                packages.append({
                    "id": f"{wid}-pkg-{i+1}-{sub}", "title": f"{sub}: часть задачи по подсистеме",
                    "axis": "by-subsystem", "scope": [sub],
                    "depends_on": ([f"{wid}-pkg-{i}-{subsystems[i-1]}"] if i > 0 else []),
                    "acceptance": base_acc, "order": i + 1})
        elif primary == "by-result":
            n = int(signals.get("independent_results") or 2)
            for i in range(n):
                packages.append({
                    "id": f"{wid}-pkg-{i+1}", "title": f"независимый результат {i+1}",
                    "axis": "by-result", "scope": subsystems or ["unspecified"],
                    "depends_on": [], "acceptance": base_acc, "order": i + 1})
        else:
            # size/commit/бюджет/verifiable — 2 последовательных пакета, человек уточняет дробление
            for i in range(2):
                packages.append({
                    "id": f"{wid}-pkg-{i+1}", "title": f"часть {i+1} (ось {primary}; уточнить дробление)",
                    "axis": primary, "scope": subsystems or ["unspecified"],
                    "depends_on": ([f"{wid}-pkg-1"] if i == 1 else []),
                    "acceptance": base_acc, "order": i + 1})
        # v2.123 (P0.3): каждый пакет несёт write_scope (пути), выведенный из его подсистемного scope —
        # чтобы брокер РЕАЛЬНО ограничил пакет его каталогом, а не только репо/общей политикой. None,
        # если scope не даёт осмысленных путей (unspecified) — тогда пакет ограничен лишь репо/политикой.
        for p in packages:
            p["write_scope"] = _scope_paths(p.get("scope")) or None
        wp["primary_axis"] = primary
    else:
        wp["primary_axis"] = None
    wp["work_packages"] = packages
    wp["human_confirms"] = bool(packages)   # дробление предлагается, финал подтверждает человек
    return wp


def main(argv):
    ap = argparse.ArgumentParser(prog="atomic_planner.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a_ = sub.add_parser("assess")
    a_.add_argument("child_root", nargs="?", default=".")
    a_.add_argument("--signals", default="{}")
    a_.add_argument("--budget", type=int)
    a_.add_argument("--json", action="store_true")
    # v2.111: decompose — построить конкретные WorkPackages
    d_ = sub.add_parser("decompose", help="построить конкретные WorkPackages при необходимости разбиения")
    d_.add_argument("child_root", nargs="?", default=".")
    d_.add_argument("--wid", default="wi"); d_.add_argument("--signals", default="{}")
    d_.add_argument("--budget", type=int); d_.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "decompose":
        wp = decompose(json.loads(a.signals), wid=a.wid, child_root=Path(a.child_root), budget=a.budget)
        if a.json:
            print(json.dumps(wp, ensure_ascii=False, indent=2))
        else:
            print(f"DECOMPOSE: atomic={wp['atomic']} · основная ось {wp['primary_axis'] or '—'} · "
                  f"пакетов {len(wp['work_packages'])}")
            for p in wp["work_packages"]:
                dep = f" ← {', '.join(p['depends_on'])}" if p["depends_on"] else ""
                print(f"  [{p['order']}] {p['id']} · scope={','.join(p['scope'])}{dep}")
            if wp["human_confirms"]:
                print("  (дробление предложено; продуктовый смысл сохранён; финал подтверждает человек)")
        return 1 if wp["should_decompose"] else 0
    if a.cmd == "assess":
        wp = assess(json.loads(a.signals), child_root=Path(a.child_root), budget=a.budget)
        if a.json:
            print(json.dumps(wp, ensure_ascii=False, indent=2))
        else:
            est = wp["estimate"]
            print(f"WORK-PACKAGE: atomic={wp['atomic']} · подсистем {len(est['subsystems'])} · "
                  f"~{est['estimated_context_tokens']}/{est['context_budget']} ток. · файлов ~{est['files_estimate']}")
            for r in wp["decomposition_reasons"]:
                print(f"  ⚠ {r}")
            if wp["should_decompose"]:
                print(f"  оси разбиения: {', '.join(wp['decomposition_axes'])}")
        return 1 if wp["should_decompose"] else 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
