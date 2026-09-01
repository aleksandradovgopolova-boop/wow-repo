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

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / "tools"))
import parallel_planner as pp   # noqa: E402

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


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    # WG: api ‖ ui (общий контракт), wiring зависимый
    wg = {"packages": [
        {"id": "api", "write_scope": ["api/**"], "shared_contracts": ["OrderContract"]},
        {"id": "ui", "write_scope": ["ui/**"], "shared_contracts": ["OrderContract"]},
        {"id": "wiring", "write_scope": ["wiring/**"], "depends_on": ["api", "ui"]}]}
    SHAS = {"api": "aaa1110", "ui": "bbb2220", "wiring": "ccc3330"}

    def good_runner(pkg):
        s = SHAS[pkg["id"]]
        return {"status": "pass", "sha": s, "gate_report": {"all_pass": True, "tested_revision": s}}

    def good_integration(results):
        return ("1234567", {"all_pass": True, "tested_revision": "1234567"}, 0, False)

    CS = {"OrderContract": "c0ffee0"}

    # happy path: все пакеты pass -> fan-in -> aggregate green на integration-SHA -> ОДИН PR
    r = execute_parallel(wg, good_runner, good_integration, contract_shas=CS)
    expect("happy: proceed + fan-in stage", r["proceed"] and r["stage"] == "fan-in")
    expect("happy: все 3 пакета исполнены (api,ui,wiring)", set(r["package_results"]) == {"api", "ui", "wiring"})
    expect("happy: api‖ui шли параллельно (mode=parallel в trace)",
           any(t["pkg"] in ("api", "ui") and t["mode"] == "parallel" for t in r["trace"]))
    expect("happy: ОДИН DeliveryIntent + PR на integration-SHA",
           r["delivery"]["intents"] == 1 and r["delivery"]["open_pr"] and r["delivery"]["integration_sha"] == "1234567")

    # инвариант: aggregate НЕ на integration-SHA -> PR НЕ открывается
    def wrong_agg_integration(results):
        return ("1234567", {"all_pass": True, "tested_revision": "OTHER99"}, 0, False)
    r2 = execute_parallel(wg, good_runner, wrong_agg_integration, contract_shas=CS)
    expect("aggregate на другом SHA -> PR НЕ открыт (package SHA != integrated result)",
           r2["delivery"]["open_pr"] is False)

    # один пакет fail -> fan-in НЕ начинается, integration НЕ запускается, 0 PR;
    # + dependency-aware stop: wiring (depends_on api,ui) НЕ запускается после провала ui
    ran_ids = set()
    def ui_fails(pkg):
        ran_ids.add(pkg["id"])
        if pkg["id"] == "ui":
            return {"status": "fail", "sha": "bbb2220", "gate_report": {"all_pass": False, "tested_revision": "bbb2220"}}
        return good_runner(pkg)
    called = {"n": 0}
    def counting_integration(results):
        called["n"] += 1; return good_integration(results)
    r3 = execute_parallel(wg, ui_fails, counting_integration, contract_shas=CS)
    expect("один пакет fail -> pre-fan-in block, integration НЕ вызван, 0 PR",
           r3["stage"] == "pre-fan-in" and called["n"] == 0 and r3["delivery"]["intents"] == 0)
    expect("dependency-aware stop: wiring НЕ запущен (dep ui провалилась) -> blocked-dependency",
           "wiring" not in ran_ids and r3["package_results"]["wiring"]["status"] == "blocked-dependency")

    # exception/timeout package_runner -> структурный package failure, executor НЕ крэшится
    def boom(pkg):
        if pkg["id"] == "api":
            raise RuntimeError("provider timeout")
        return good_runner(pkg)
    r_boom = execute_parallel(wg, boom, good_integration, contract_shas=CS)
    expect("exception в runner -> структурный error (не крэш executor), fan-in block",
           r_boom["package_results"]["api"]["status"] == "error"
           and "provider timeout" in r_boom["package_results"]["api"].get("error", "")
           and r_boom["proceed"] is False and r_boom["delivery"]["intents"] == 0)

    # общий контракт не зафиксирован -> block ДО пакетов
    ran = {"n": 0}
    def counting_runner(pkg):
        ran["n"] += 1; return good_runner(pkg)
    r4 = execute_parallel(wg, counting_runner, good_integration, contract_shas={})
    expect("контракт не зафиксирован -> block на contract-first, пакеты НЕ запущены",
           r4["stage"] == "contract-first" and ran["n"] == 0)

    # merge conflict / base moved на fan-in -> block/revalidation, PR не открыт
    def conflict_integration(results):
        return ("1234567", {"all_pass": True, "tested_revision": "1234567"}, 1, False)
    r5 = execute_parallel(wg, good_runner, conflict_integration, contract_shas=CS)
    expect("merge conflict на fan-in -> block, PR не открыт", r5["proceed"] is False and r5["delivery"]["open_pr"] is False)

    # невалидный WG (цикл) -> block на plan
    cyc = {"packages": [{"id": "a", "write_scope": ["a/**"], "depends_on": ["b"]},
                        {"id": "b", "write_scope": ["b/**"], "depends_on": ["a"]}]}
    r6 = execute_parallel(cyc, good_runner, good_integration, contract_shas={})
    expect("цикл в WG -> block на stage=plan", r6["stage"] == "plan" and r6["proceed"] is False)

    # пакет не доказателен (нет SHA) -> pre-fan-in block
    def no_sha_runner(pkg):
        return {"status": "pass", "gate_report": {"all_pass": True}}  # нет sha
    r7 = execute_parallel({"packages": [{"id": "a", "write_scope": ["a/**"]}]},
                          no_sha_runner, good_integration, contract_shas={})
    expect("package без SHA -> pre-fan-in block (не доказателен)", r7["proceed"] is False)

    print("parallel_executor selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
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
