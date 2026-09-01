#!/usr/bin/env python3
"""parallel_planner.py (v3.6.5) — bounded parallel-2 execution planner (детерминированный, offline).

Планирует исполнение WorkGraph по решениям ParallelSafetyDecision и правилам безопасности, и решает
fan-in. Живой parallel-run (реальные worktrees + модель + PR) — квалификация v3.6.6/v3.8 (нужен ключ);
здесь — детерминированный ПЛАН и РЕШЕНИЯ (то, что проверяют обязательные сценарии владельца).

Правила параллельности (bounded: максимум 2 пакета одновременно):
  - два пакета параллельны, только если нет depends_on между ними И непересекающиеся write_scope;
  - общий контракт -> контракт фиксируется ПЕРВЫМ (contract_first); иначе — блок;
  - пересечение write_scope -> сериализация.
Решение fan-in (integration_decision):
  - любой пакет не pass -> fan-in НЕ начинается;
  - конфликт слияния -> block;
  - base сдвинулась -> revalidation;
  - все pass, нет конфликта, база стабильна -> нужен НОВЫЙ integration-SHA; PR открывается только
    при зелёной aggregate-проверке (иначе PR не открывается).

CLI: parallel_planner.py [examples/work-graph-demo] [--json] | --selftest
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
WG_DEMO = PKG / "examples" / "work-graph-demo" / "work-graph.yaml"
MAX_PARALLEL = 2


def _prefix(glob):
    """Нормализованный каталог-префикс write-scope глоба. ПУСТАЯ строка означает ГЛОБАЛЬНЫЙ scope
    (`**`, `*`, `.`, `/`) — покрывает ВЕСЬ репозиторий. Раньше `**` давал prefix '' -> 'aa=/',
    и `'src/b/'.startswith('/')` == False -> глобальный scope НЕ ловился как пересечение (баг v3.6.5)."""
    p = (glob or "").split("*")[0].strip().rstrip("/")
    return "" if p in ("", ".") else p


def _overlap(a_scopes, b_scopes):
    for a in a_scopes or []:
        pa = _prefix(a)
        for b in b_scopes or []:
            pb = _prefix(b)
            if pa == "" or pb == "":     # глобальный scope пересекается с любым -> fail-closed
                return True
            aa, bb = pa + "/", pb + "/"
            if aa.startswith(bb) or bb.startswith(aa):
                return True
    return False


def can_parallel(pa, pb):
    """(bool, reason): два пакета безопасно параллельны? Fail-closed: незадекларированный write_scope
    трактуется как неизвестный -> сериализация (нельзя доказать непересечение)."""
    ia, ib = pa.get("id"), pb.get("id")
    if ib in (pa.get("depends_on") or []) or ia in (pb.get("depends_on") or []):
        return False, f"depends_on между {ia},{ib} -> сериализация"
    if not (pa.get("write_scope")) or not (pb.get("write_scope")):
        return False, f"незадекларированный write_scope у {ia}/{ib} -> сериализация (fail-closed)"
    if _overlap(pa.get("write_scope"), pb.get("write_scope")):
        return False, f"пересечение write_scope {ia},{ib} -> сериализация"
    return True, "непересекающиеся, независимые -> параллельно"


def plan(wg: dict) -> dict:
    raw = (wg or {}).get("packages", []) or []
    # v3.6.7d: несловарный пакет НЕ должен ронять planner до формирования valid=false
    non_dict = [p for p in raw if not isinstance(p, dict)]
    pkgs = [p for p in raw if isinstance(p, dict)]
    id_list = [p.get("id") for p in pkgs if p.get("id")]
    dup_ids = sorted({i for i in id_list if id_list.count(i) > 1})
    by_id = {p["id"]: p for p in pkgs if p.get("id")}
    ids = list(by_id)
    independent = [p for p in pkgs if not (p.get("depends_on"))]
    dependent = [p for p in pkgs if p.get("depends_on")]

    # contract-first: контракт, разделяемый >= 2 пакетами
    contract_count = {}
    for p in pkgs:
        for c in p.get("shared_contracts") or []:
            contract_count[c] = contract_count.get(c, 0) + 1
    contract_first = sorted([c for c, n in contract_count.items() if n >= 2])

    # bounded parallel-2 группы среди независимых с непересекающимися scope
    groups, used = [], set()
    for i, pa in enumerate(independent):
        if pa["id"] in used:
            continue
        group = [pa["id"]]
        used.add(pa["id"])
        for pb in independent[i + 1:]:
            if pb["id"] in used or len(group) >= MAX_PARALLEL:
                continue
            okp, _ = can_parallel(pa, pb)
            if okp:
                group.append(pb["id"])
                used.add(pb["id"])
        groups.append(group)

    has_parallel = any(len(g) >= 2 for g in groups)
    if len(pkgs) <= 1:
        mode = "single"
    elif has_parallel and dependent:
        mode = "hybrid"
    elif has_parallel:
        mode = "parallel"
    else:
        mode = "sequential"

    # integration_order — топологический (пакет после depends_on)
    order, placed = [], set()
    guard = 0
    remaining = list(ids)
    while remaining and guard < len(ids) + 2:
        guard += 1
        for pid in list(remaining):
            deps = by_id[pid].get("depends_on") or []
            if all(d in placed for d in deps):
                order.append(pid)
                placed.add(pid)
                remaining.remove(pid)

    # v3.6.7: планировщик САМ блокирует невалидный WorkGraph (перед превращением в executor)
    errors = []
    if non_dict:
        errors.append(f"пакет(ы) не являются объектом: {len(non_dict)} шт -> WorkGraph невалиден")
    if dup_ids:
        errors.append(f"дубликаты package id: {dup_ids} -> WorkGraph невалиден")
    if any(not p.get("id") for p in pkgs):
        errors.append("есть пакет без id")
    for p in pkgs:
        if not isinstance(p, dict):
            continue
        for d in (p.get("depends_on") or []):
            if d not in by_id:
                errors.append(f"{p.get('id')}: depends_on ссылается на несуществующий пакет {d}")
    if len(order) < len(ids):   # неполный топо-порядок -> цикл/битая зависимость
        errors.append(f"неполный integration_order (цикл/битая зависимость): placed={order} из ids={ids}")

    return {"kind": "parallel-plan", "mode": mode, "max_parallel": MAX_PARALLEL,
            "packages": ids, "parallel_groups": groups, "dependent": [p["id"] for p in dependent],
            "contract_first": contract_first, "integration_order": order,
            "fan_in_required": len(pkgs) > 1, "valid": not errors, "errors": errors}


def integration_decision(package_results: dict, conflicts=0, base_moved=False, aggregate_ok=True):
    """package_results: {pkg_id: 'pass'|'fail'|...}. Решение о fan-in/интеграции/PR.
    v3.6.7 fail-closed: ПУСТОЙ набор результатов -> block (раньше any([])==False проходил как all-pass)."""
    if not package_results:
        return {"proceed": False, "integration_sha_required": False, "open_pr": False,
                "reason": "пустой набор результатов пакетов -> fan-in НЕ начинается (fail-closed)"}
    if any(v != "pass" for v in package_results.values()):
        failed = [k for k, v in package_results.items() if v != "pass"]
        return {"proceed": False, "integration_sha_required": False, "open_pr": False,
                "reason": f"пакет(ы) не pass {failed} -> fan-in НЕ начинается"}
    if conflicts and conflicts > 0:
        return {"proceed": False, "integration_sha_required": False, "open_pr": False,
                "reason": "merge conflict при fan-in -> block"}
    if base_moved:
        return {"proceed": False, "integration_sha_required": True, "open_pr": False,
                "revalidation_required": True, "reason": "base сдвинулась -> integration revalidation"}
    # все pass, нет конфликта, база стабильна
    return {"proceed": True, "integration_sha_required": True, "open_pr": bool(aggregate_ok),
            "reason": ("aggregate green -> новый integration-SHA + один draft PR" if aggregate_ok
                       else "aggregate FAIL -> integration-SHA есть, но PR НЕ открывается")}


def _block(errors, integration_sha_required=False):
    return {"proceed": False, "integration_sha_required": integration_sha_required, "open_pr": False,
            "errors": list(errors), "reason": "fail-closed: " + "; ".join(errors)}


def _sha_like(s):
    """Похоже ли на реальный git commit/blob SHA (hex, >= 7 симв) — не просто непустая строка."""
    return isinstance(s, str) and len(s) >= 7 and all(c in "0123456789abcdefABCDEF" for c in s)


def integration_gate(expected_packages, package_results, shared_contracts=None, contract_shas=None,
                     conflicts=0, base_moved=False, aggregate=None, integration_sha=None):
    """v3.6.7 РАНТАЙМ-СТРОГОЕ fan-in решение (перед живым parallel-executor).

    Отличия от `integration_decision` (fail-closed по всем краям, найденным ревью):
      - результаты обязаны ПОКРЫВАТЬ ТОЧНЫЙ набор пакетов WorkGraph (нет пропущенных/лишних);
      - пустой набор -> block;
      - каждый результат ДОКАЗАТЕЛЬНЫЙ: dict со status + SHA пакета + gate_report(all_pass), и
        (v3.6.7d) `gate_report.tested_revision == package.sha` (evidence привязан к ревизии пакета);
      - общий контракт обязан иметь ЗАФИКСИРОВАННЫЙ contract SHA, ПОХОЖИЙ на реальный commit/blob
        (v3.6.7d: не просто непустая строка);
      - aggregate — РЕАЛЬНЫЙ GateReport (dict с all_pass), и (v3.6.7d) `aggregate.tested_revision ==
        integration_sha` — иначе PR не открывается (evidence не относится к integration-SHA).
    """
    expected = set(expected_packages or [])
    got = dict(package_results or {})
    errors = []
    if not expected:
        errors.append("пустой WorkGraph (нет ожидаемых пакетов)")
    missing = expected - set(got)
    extra = set(got) - expected
    if missing:
        errors.append(f"неполный набор результатов: нет пакетов {sorted(missing)}")
    if extra:
        errors.append(f"лишние результаты вне WorkGraph: {sorted(extra)}")
    # каждый результат должен быть доказательным (SHA + gate_report, привязанный к ревизии пакета)
    for pid, r in got.items():
        if not isinstance(r, dict):
            errors.append(f"{pid}: результат не доказательный (нет SHA/gate_report, голое '{r}')")
            continue
        if not r.get("sha"):
            errors.append(f"{pid}: нет package SHA")
        gr = r.get("gate_report")
        if not (isinstance(gr, dict) and "all_pass" in gr):
            errors.append(f"{pid}: нет gate_report(all_pass)")
        else:
            if r.get("status") == "pass" and gr.get("all_pass") is not True:
                errors.append(f"{pid}: status=pass, но gate_report.all_pass != true")
            if r.get("sha") and gr.get("tested_revision") != r.get("sha"):
                errors.append(f"{pid}: gate_report.tested_revision != package sha (evidence не на ревизии пакета)")
    # общий контракт обязан быть зафиксирован реальным contract SHA
    for c in shared_contracts or []:
        cs = (contract_shas or {}).get(c)
        if not cs:
            errors.append(f"общий контракт {c} НЕ зафиксирован (нет contract SHA)")
        elif not _sha_like(cs):
            errors.append(f"общий контракт {c}: contract SHA '{cs}' не похож на реальный commit/blob SHA")
    if errors:
        return _block(errors)

    # ниже — набор полон, доказателен, контракты зафиксированы
    not_pass = [pid for pid, r in got.items() if r.get("status") != "pass"]
    if not_pass:
        return _block([f"пакет(ы) не pass {sorted(not_pass)} -> fan-in НЕ начинается"])
    if conflicts and conflicts > 0:
        return _block(["merge conflict при fan-in"])
    if base_moved:
        return {"proceed": False, "integration_sha_required": True, "open_pr": False,
                "revalidation_required": True, "errors": [],
                "reason": "base сдвинулась -> integration revalidation (fresh run от новой базы)"}
    if not isinstance(aggregate, dict):
        return {"proceed": True, "integration_sha_required": True, "open_pr": False, "errors": [],
                "reason": "aggregate не РЕАЛЬНЫЙ GateReport (голый bool/None) -> integration-SHA есть, PR НЕ открывается"}
    # aggregate evidence обязан относиться к integration-SHA
    rev_ok = integration_sha is None or aggregate.get("tested_revision") == integration_sha
    agg_ok = aggregate.get("all_pass") is True and rev_ok
    if not rev_ok:
        return {"proceed": True, "integration_sha_required": True, "open_pr": False, "errors": [],
                "reason": "aggregate.tested_revision != integration_sha -> evidence не на integration-SHA, PR НЕ открывается"}
    return {"proceed": True, "integration_sha_required": True, "open_pr": bool(agg_ok), "errors": [],
            "reason": ("aggregate GateReport green на integration-SHA -> новый integration-SHA + один draft PR"
                       if agg_ok else "aggregate GateReport НЕ green -> integration-SHA есть, PR НЕ открывается")}


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    # реальный demo WG-001 (api||ui + wiring)
    if WG_DEMO.exists():
        wg = yaml.safe_load(WG_DEMO.read_text(encoding="utf-8"))
        p = plan(wg)
        expect("WG-001: api,ui в одной параллельной группе (непересекающиеся)",
               any(set(g) == {"api", "ui"} for g in p["parallel_groups"]))
        expect("WG-001: mode=hybrid (parallel + зависимый wiring)", p["mode"] == "hybrid")
        expect("WG-001: contract_first содержит OrderContract (общий у api,ui)",
               "OrderContract" in p["contract_first"])
        expect("WG-001: integration_order топологичен (wiring последним)", p["integration_order"][-1] == "wiring")
        expect("WG-001: fan_in_required", p["fan_in_required"] is True)

    # сценарий: непересекающиеся независимые -> parallel
    okp, _ = can_parallel({"id": "a", "write_scope": ["src/a/**"]},
                          {"id": "b", "write_scope": ["src/b/**"]})
    expect("непересекающиеся независимые -> parallel", okp is True)
    # пересечение write_scope -> serialize
    okp, r = can_parallel({"id": "a", "write_scope": ["src/**"]},
                          {"id": "b", "write_scope": ["src/b/**"]})
    expect("пересечение write_scope -> сериализация", okp is False and "write_scope" in r)
    # depends_on -> serialize
    okp, r = can_parallel({"id": "a", "write_scope": ["x/**"]},
                          {"id": "b", "write_scope": ["y/**"], "depends_on": ["a"]})
    expect("depends_on -> сериализация", okp is False and "depends_on" in r)

    # общий контракт -> contract_first
    wg2 = {"packages": [{"id": "a", "write_scope": ["a/**"], "shared_contracts": ["C"]},
                        {"id": "b", "write_scope": ["b/**"], "shared_contracts": ["C"]}]}
    expect("общий контракт C -> contract_first", plan(wg2)["contract_first"] == ["C"])

    # integration decision — обязательные сценарии
    expect("один пакет fail -> fan-in НЕ начинается",
           integration_decision({"a": "pass", "b": "fail"})["proceed"] is False)
    d = integration_decision({"a": "pass", "b": "pass"}, conflicts=0, base_moved=False, aggregate_ok=True)
    expect("оба pass, нет конфликта, aggregate green -> integration-SHA + PR",
           d["proceed"] and d["integration_sha_required"] and d["open_pr"])
    expect("merge conflict -> block",
           integration_decision({"a": "pass", "b": "pass"}, conflicts=1)["proceed"] is False)
    dbm = integration_decision({"a": "pass", "b": "pass"}, base_moved=True)
    expect("base moved -> revalidation, PR не открыт",
           dbm.get("revalidation_required") is True and dbm["open_pr"] is False)
    dagg = integration_decision({"a": "pass", "b": "pass"}, aggregate_ok=False)
    expect("aggregate fail -> integration-SHA есть, PR НЕ открывается",
           dagg["integration_sha_required"] is True and dagg["open_pr"] is False)

    # single / sequential режимы
    expect("один пакет -> single", plan({"packages": [{"id": "a", "write_scope": ["a/**"]}]})["mode"] == "single")
    seq = plan({"packages": [{"id": "a", "write_scope": ["a/**"]},
                             {"id": "b", "write_scope": ["b/**"], "depends_on": ["a"]}]})
    expect("цепочка зависимостей -> sequential", seq["mode"] == "sequential")

    # === v3.6.7 hardening (fail-closed перед превращением planner -> executor) ===
    # (баг v3.6.5) глобальный write_scope ** ДОЛЖЕН пересекаться с любым конкретным
    okp, r = can_parallel({"id": "a", "write_scope": ["**"]},
                          {"id": "b", "write_scope": ["src/b/**"]})
    expect("глобальный ** пересекается с src/b/** -> сериализация (fix баг overlap)",
           okp is False and "write_scope" in r)
    okp, r = can_parallel({"id": "a", "write_scope": ["src/a/**"]}, {"id": "b"})
    expect("незадекларированный write_scope -> сериализация (fail-closed)",
           okp is False and "write_scope" in r)
    # WorkGraph validity: цикл -> plan.valid=False
    cyc = plan({"packages": [{"id": "a", "write_scope": ["a/**"], "depends_on": ["b"]},
                             {"id": "b", "write_scope": ["b/**"], "depends_on": ["a"]}]})
    expect("цикл зависимостей -> plan.valid=False + ошибка неполного order",
           cyc["valid"] is False and any("integration_order" in e for e in cyc["errors"]))
    # битая зависимость -> невалиден
    brk = plan({"packages": [{"id": "a", "write_scope": ["a/**"], "depends_on": ["ghost"]}]})
    expect("битый depends_on -> plan.valid=False", brk["valid"] is False)
    # валидный WG-001 -> plan.valid=True
    if WG_DEMO.exists():
        expect("валидный WG-001 -> plan.valid=True", plan(wg)["valid"] is True)

    # integration_decision: ПУСТОЙ набор -> block (раньше проходил как all-pass)
    expect("integration_decision({}) -> block (fail-closed на пустоте)",
           integration_decision({})["proceed"] is False)

    # integration_gate: доказательный happy-path (evidence привязан к SHA пакета и integration-SHA)
    INT = "1234567abc"
    CSHA = "c0ffee0abc"
    good_results = {
        "api": {"status": "pass", "sha": "aaa1110", "gate_report": {"all_pass": True, "tested_revision": "aaa1110"}},
        "ui": {"status": "pass", "sha": "bbb2220", "gate_report": {"all_pass": True, "tested_revision": "bbb2220"}}}
    g = integration_gate(["api", "ui"], good_results, shared_contracts=["OrderContract"],
                         contract_shas={"OrderContract": CSHA},
                         aggregate={"all_pass": True, "tested_revision": INT}, integration_sha=INT)
    expect("integration_gate: доказательный набор + контракт-SHA + aggregate на integration-SHA -> PR",
           g["proceed"] and g["integration_sha_required"] and g["open_pr"])
    # неполный / лишний набор -> block
    expect("integration_gate: пропущен пакет ui -> block",
           integration_gate(["api", "ui"], {"api": good_results["api"]})["proceed"] is False)
    expect("integration_gate: лишний пакет вне WG -> block",
           integration_gate(["api"], good_results)["proceed"] is False)
    # голая строка / нет SHA / gate_report не green -> block
    expect("integration_gate: голая строка 'pass' -> block",
           integration_gate(["api"], {"api": "pass"})["proceed"] is False)
    expect("integration_gate: нет package SHA -> block",
           integration_gate(["api"], {"api": {"status": "pass", "gate_report": {"all_pass": True}}})["proceed"] is False)
    expect("integration_gate: status=pass но gate_report не green -> block",
           integration_gate(["api"], {"api": {"status": "pass", "sha": "aaa1110",
                            "gate_report": {"all_pass": False, "tested_revision": "aaa1110"}}})["proceed"] is False)
    # v3.6.7d: gate_report.tested_revision != package sha -> block
    expect("integration_gate: gate_report.tested_revision != package sha -> block (evidence не на ревизии)",
           integration_gate(["api"], {"api": {"status": "pass", "sha": "aaa1110",
                            "gate_report": {"all_pass": True, "tested_revision": "WRONG99"}}})["proceed"] is False)
    # общий контракт не зафиксирован / не sha-like -> block
    expect("integration_gate: общий контракт без contract SHA -> block",
           integration_gate(["api", "ui"], good_results, shared_contracts=["OrderContract"],
                            contract_shas={})["proceed"] is False)
    expect("integration_gate: contract SHA не похож на реальный commit/blob -> block",
           integration_gate(["api", "ui"], good_results, shared_contracts=["OrderContract"],
                            contract_shas={"OrderContract": "nope"})["proceed"] is False)
    # aggregate голый bool -> proceed, PR НЕ открыт
    gbool = integration_gate(["api", "ui"], good_results, shared_contracts=["OrderContract"],
                             contract_shas={"OrderContract": CSHA}, aggregate=True)
    expect("integration_gate: aggregate голый bool -> proceed, но PR НЕ открыт",
           gbool["proceed"] is True and gbool["open_pr"] is False)
    # v3.6.7d: aggregate.tested_revision != integration_sha -> PR НЕ открыт
    gwrong = integration_gate(["api", "ui"], good_results, shared_contracts=["OrderContract"],
                              contract_shas={"OrderContract": CSHA},
                              aggregate={"all_pass": True, "tested_revision": "OTHER99"}, integration_sha=INT)
    expect("integration_gate: aggregate evidence на другом SHA -> proceed, PR НЕ открыт",
           gwrong["proceed"] is True and gwrong["open_pr"] is False)
    # merge conflict / base moved
    expect("integration_gate: merge conflict -> block",
           integration_gate(["api", "ui"], good_results, shared_contracts=["OrderContract"],
                            contract_shas={"OrderContract": CSHA}, conflicts=1)["proceed"] is False)
    gbm = integration_gate(["api", "ui"], good_results, shared_contracts=["OrderContract"],
                           contract_shas={"OrderContract": CSHA}, base_moved=True)
    expect("integration_gate: base moved -> revalidation, PR не открыт",
           gbm.get("revalidation_required") is True and gbm["open_pr"] is False)
    expect("integration_gate: пустой WorkGraph -> block",
           integration_gate([], {})["proceed"] is False)

    # v3.6.7d: duplicate package id -> plan.valid=False (раньше молча дедуплицировалось)
    dup = plan({"packages": [{"id": "a", "write_scope": ["a/**"]},
                             {"id": "a", "write_scope": ["b/**"]}]})
    expect("дубликат package id -> plan.valid=False", dup["valid"] is False
           and any("дубликат" in e for e in dup["errors"]))
    # v3.6.7d: несловарный пакет НЕ роняет planner, а даёт valid=False
    nd = plan({"packages": ["not-a-dict", {"id": "a", "write_scope": ["a/**"]}]})
    expect("несловарный пакет -> plan.valid=False (без исключения)",
           nd["valid"] is False and any("не являются объектом" in e for e in nd["errors"]))

    print("parallel_planner selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    wg_path = Path(args[0]) / "work-graph.yaml" if args else WG_DEMO
    p = plan(yaml.safe_load(Path(wg_path).read_text(encoding="utf-8")))
    print(json.dumps(p, ensure_ascii=False, indent=2) if "--json" in argv
          else f"PLAN mode={p['mode']} groups={p['parallel_groups']} order={p['integration_order']} "
               f"contract_first={p['contract_first']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
