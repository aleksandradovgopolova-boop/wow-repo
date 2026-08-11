#!/usr/bin/env python3
"""parallel_executor.py (v3.7.1) — bounded parallel-2 executor (оркестрация поверх decision-слоя).

Доводит parallel_planner + integration_gate до ИСПОЛНИТЕЛЯ: план -> изолированный прогон каждого
пакета (bounded ≤2 одновременно) -> package SHA + GateReport -> fan-in в отдельный integration-SHA ->
повтор aggregate-проверок на integration-SHA -> ОДИН DeliveryIntent/PR. Живые per-package прогоны и
слияние worktrees — ТОЧКИ ИНЪЕКЦИИ (`package_runner`, `integration_runner`): в проде это ai_ops_run в
изолированных worktrees; в тесте — детерминированные mock'и. Оркестрация и safety-инварианты
проверяемы ОФФЛАЙН, без живой модели.

ГЛАВНЫЙ ИНВАРИАНТ: успех package SHA НЕ доказывает успех интегрированного результата — aggregate
перепроверяется на НОВОМ integration-SHA, и только его зелёный результат открывает один PR.

Правила (fail-closed): невалидный WorkGraph -> block; любой пакет не pass -> fan-in НЕ начинается;
общий контракт без зафиксированного SHA -> block; aggregate не на integration-SHA -> PR НЕ открывается;
base moved / merge conflict -> block/revalidation (не «умная» авто-починка).

Только stdlib. CLI: parallel_executor.py [examples/work-graph-demo] | --selftest
"""
from __future__ import annotations

import concurrent.futures as _cf
import json
import sys
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
from ai_ops_kit.shared import _bootstrap  # noqa: E402
from ai_ops_kit.engine import parallel_planner as pp   # noqa: E402

WG_DEMO = PKG / "examples" / "work-graph-demo" / "work-graph.yaml"


def _safe_run(package_runner, pkg):
    """v3.7.1-fix (ревью #4): exception/timeout package_runner -> СТРУКТУРНЫЙ package failure,
    а не падение всего executor'а (fail-closed)."""
    try:
        r = package_runner(pkg)
    except BaseException as e:   # noqa: BLE001 — любой сбой пакета изолируем в структурный fail
        return {"status": "error", "sha": None, "gate_report": {"all_pass": False},
                "error": f"{type(e).__name__}: {e}"[:200]}
    if not isinstance(r, dict):
        return {"status": "error", "sha": None, "gate_report": {"all_pass": False},
                "error": "package_runner вернул не-dict"}
    return r


def _run_packages(plan, by_id, package_runner, max_parallel=pp.MAX_PARALLEL):
    """Прогнать пакеты: независимые группы (bounded ≤2) конкурентно; зависимые — по topo-порядку, ТОЛЬКО
    если ВСЕ depends_on уже дали доказательный pass (dependency-aware stop: провал зависимости не даёт
    стартовать downstream). Сбой runner -> структурный package failure (не крэш executor)."""
    results, trace = {}, []

    def deps_ok(pid):
        for d in (by_id[pid].get("depends_on") or []):
            if results.get(d, {}).get("status") != "pass":
                return False, d
        return True, None

    # 1) независимые параллельные группы — конкурентно (≤2 одновременно), с изоляцией сбоев
    for group in plan["parallel_groups"]:
        if len(group) == 1:
            pid = group[0]
            results[pid] = _safe_run(package_runner, by_id[pid]); trace.append({"pkg": pid, "mode": "single"})
            continue
        with _cf.ThreadPoolExecutor(max_workers=max_parallel) as ex:
            futs = {ex.submit(_safe_run, package_runner, by_id[pid]): pid for pid in group[:max_parallel]}
            for fut in _cf.as_completed(futs):
                pid = futs[fut]
                results[pid] = fut.result(); trace.append({"pkg": pid, "mode": "parallel"})
        for pid in group[max_parallel:]:   # хвост группы (если >2) — сериализуем (bounded)
            results[pid] = _safe_run(package_runner, by_id[pid]); trace.append({"pkg": pid, "mode": "serialized-tail"})
    # 2) зависимые — строго по топо-порядку; НЕ стартуем, пока все depends_on не pass
    for pid in plan["integration_order"]:
        if pid in results:
            continue
        ok, bad = deps_ok(pid)
        if not ok:
            results[pid] = {"status": "blocked-dependency", "sha": None,
                            "gate_report": {"all_pass": False}, "blocked_by": bad}
            trace.append({"pkg": pid, "mode": "blocked-dependency", "dep": bad})
            continue
        results[pid] = _safe_run(package_runner, by_id[pid]); trace.append({"pkg": pid, "mode": "dependent"})
    return results, trace


def execute_parallel(wg, package_runner, integration_runner, contract_shas=None, max_parallel=pp.MAX_PARALLEL):
    """Исполнить WorkGraph bounded parallel-2 + fan-in. Возвращает структурную запись исполнения.

    package_runner(pkg) -> {"status":"pass"|"fail","sha":<hex>,"gate_report":{"all_pass":bool,"tested_revision":<hex>}}
    integration_runner(results) -> (integration_sha:<hex>, aggregate:{"all_pass":bool,"tested_revision":integration_sha},
                                     conflicts:int, base_moved:bool)
    """
    plan = pp.plan(wg)
    if not plan["valid"]:
        return {"proceed": False, "stage": "plan", "reason": "WorkGraph невалиден",
                "errors": plan["errors"], "plan": plan, "delivery": {"intents": 0, "open_pr": False}}
    by_id = {p["id"]: p for p in wg.get("packages", []) if isinstance(p, dict) and p.get("id")}
    contract_shas = dict(contract_shas or {})
    shared = plan["contract_first"]

    # общий контракт обязан быть зафиксирован ПЕРЕД пакетами (contract-first)
    unfixed = [c for c in shared if not pp._sha_like(contract_shas.get(c))]
    if unfixed:
        return {"proceed": False, "stage": "contract-first",
                "reason": f"общие контракты не зафиксированы (нет реального SHA): {unfixed}",
                "plan": plan, "delivery": {"intents": 0, "open_pr": False}}

    results, trace = _run_packages(plan, by_id, package_runner, max_parallel)
    expected = plan["packages"]

    # pre-fan-in: любой пакет не pass / не доказателен -> fan-in НЕ начинается (integration НЕ запускается)
    pre = pp.integration_gate(expected, results, shared_contracts=shared, contract_shas=contract_shas)
    if not pre["proceed"]:
        return {"proceed": False, "stage": "pre-fan-in", "decision": pre, "package_results": results,
                "trace": trace, "plan": plan, "delivery": {"intents": 0, "open_pr": False}}

    # fan-in: НОВЫЙ integration-SHA + ПОВТОР aggregate-проверок на нём
    integration_sha, aggregate, conflicts, base_moved = integration_runner(results)
    final = pp.integration_gate(expected, results, shared_contracts=shared, contract_shas=contract_shas,
                                conflicts=conflicts, base_moved=base_moved,
                                aggregate=aggregate, integration_sha=integration_sha)
    open_pr = bool(final.get("open_pr"))
    return {"proceed": bool(final.get("proceed")), "stage": "fan-in", "decision": final,
            "package_results": results, "trace": trace, "plan": plan,
            "integration_sha": integration_sha, "aggregate": aggregate,
            # ОДИН DeliveryIntent — только при зелёном aggregate на integration-SHA
            "delivery": {"intents": 1 if open_pr else 0, "open_pr": open_pr,
                         "integration_sha": integration_sha if open_pr else None}}


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    wg_path = Path(args[0]) / "work-graph.yaml" if args else WG_DEMO
    wg = yaml.safe_load(Path(wg_path).read_text(encoding="utf-8"))
    p = pp.plan(wg)
    print(json.dumps({"plan_valid": p["valid"], "mode": p["mode"], "parallel_groups": p["parallel_groups"],
                      "note": "живой исполнитель требует package_runner/integration_runner (ai_ops_run в "
                              "изолированных worktrees) — CLI показывает только план"},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
