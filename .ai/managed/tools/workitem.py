#!/usr/bin/env python3
"""WorkItem — единая сущность продуктового изменения (v2.17).

Раньше было два несвязанных контура: прогон workflow (orchestrator TaskState) и
Feature Blueprint (features/<id>/) — с разными id и разными «статусами». Пользователь
не понимал, какой из них главный и кто их синхронизирует. WorkItem связывает их ОДНИМ id
и даёт ОДИН понятный итог:

    запрос -> классификация/routing -> WorkItem(id) -> Feature Blueprint + прогон workflow
    -> gates проверяют артефакты -> ЕДИНЫЙ статус.

Единый статус (ровно 4 действия для человека):
  - done                  — блокирующих незакрытых гейтов нет, blueprint не PROBLEM;
  - blocked               — блокирующий гейт реально провален или blueprint PROBLEM (чинить);
  - needs_human_decision  — незакрыт human-approval гейт (нужно решение человека);
  - needs_more_evidence   — гейт не закрыт только из-за отсутствия доказательств (дать evidence).

Приоритет: реальный провал > решение человека > нехватка доказательств > готово.
Статус выводится ДЕТЕРМИНИРОВАННО из gate_executor + run_report; WorkItem ничего не
выдумывает. Живую модель WorkItem не запускает (это orchestrator/runtime) — он связывает
и подводит итог.

Использование:
  workitem.py start <features-dir> <feature-id> --task "…" [--task-type T] [--risk R]
  workitem.py status <features-dir> <feature-id> [--run-dir DIR] [--evidence e.json] [--json]
  workitem.py --selftest
"""

import json
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / "tools"))
sys.path.insert(0, str(PKG / "validation"))
import gate_executor          # noqa: E402
import run_report             # noqa: E402
import ai_route               # noqa: E402
import lifecycle_intent       # noqa: E402  v3.27.0 WP1

STATUS_ACTION = {
    "done": "готово — блокирующих гейтов нет, blueprint в порядке",
    "blocked": "заблокировано — чинить провал гейта/консистентность артефактов",
    "needs_human_decision": "требуется решение человека (human-approval гейт)",
    "needs_more_evidence": "требуется доказательство (evidence для гейта)",
    "draft": "черновик — прогон ещё не оценивался",
}


def wi_path(features_dir, fid):
    return Path(features_dir) / fid / "workitem.yaml"


def start(features_dir, fid, task, task_type=None, risk=None):
    inp = {}
    if task_type:
        inp["task_type"] = task_type
    if risk:
        inp["risk"] = risk
    r = ai_route.route(inp)
    wf = r["workflow"]
    fdir = Path(features_dir) / fid
    fdir.mkdir(parents=True, exist_ok=True)
    wi = {
        "schema_version": 1,
        "kind": "workitem",
        "id": fid,
        "task": task,
        "workflow": wf,
        "route_reasons": r.get("reasons", []),
        "human_approval_required": bool(r.get("human_approval_required")),
        "paths": {
            "blueprint": f"features/{fid}/blueprint.yaml",
            "run_state": f".ai/runtime/workitems/{fid}/TaskState.yaml",
            "workitem": f"features/{fid}/workitem.yaml",
        },
        "status": "draft",
        "lifecycle_intent": "discovery",  # v3.27.0 WP1: начальная стадия
    }
    wi_path(features_dir, fid).write_text(
        yaml.safe_dump(wi, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return wi


def derive_status(workflow, feature_dir, evidence):
    """Единый статус из gate_executor (гейты) + run_report (здоровье blueprint).
    v3.27.0 WP1: также вычисляет lifecycle_intent детерминированно из состояния."""
    gates = gate_executor.evaluate(workflow, evidence or {})
    kinds = gates["gate_kinds"]
    unmet = gates["unmet_gates"]
    human_unmet = [g for g in unmet if kinds.get(g) == "human-approval"]
    real_fail = [g for g in unmet if g in (evidence or {})]           # evidence подан, но fail
    evidence_missing = [g for g in unmet if g not in (evidence or {})]  # доказательств не подано

    verdict = None
    bp = Path(feature_dir) / "blueprint.yaml"
    if bp.exists():
        try:
            verdict = run_report.build_report(Path(feature_dir), None)["verdict"]
        except Exception:
            verdict = None

    if real_fail or verdict == "PROBLEM":
        status = "blocked"
    elif human_unmet:
        status = "needs_human_decision"
    elif evidence_missing:
        status = "needs_more_evidence"
    else:
        status = "done"

    # v3.27.0 WP1: вычисляем lifecycle_intent детерминированно
    has_evidence = bool(evidence) and not evidence_missing
    li = lifecycle_intent.derive(status, has_evidence=has_evidence)

    return {
        "schema_version": 1, "kind": "workitem-status",
        "workflow": workflow, "status": status, "action": STATUS_ACTION[status],
        "lifecycle_intent": li,  # v3.27.0 WP1
        "blocked": gates["blocked"], "unmet_gates": unmet,
        "real_fail": real_fail, "human_unmet": human_unmet,
        "evidence_missing": evidence_missing, "blueprint_verdict": verdict,
    }


def status_cmd(features_dir, fid, run_dir=None, evidence_file=None):
    p = wi_path(features_dir, fid)
    if not p.exists():
        raise SystemExit(f"WorkItem не найден: {p} — сначала `workitem.py start`.")
    wi = yaml.safe_load(p.read_text(encoding="utf-8"))
    wf = wi["workflow"]
    feature_dir = Path(features_dir) / fid
    evidence = {}
    if run_dir:
        evidence.update(gate_executor.collect_evidence(wf, Path(run_dir)))
    if evidence_file:
        evidence.update(gate_executor.load_evidence(evidence_file))  # явный evidence — приоритет
    res = derive_status(wf, feature_dir, evidence)
    wi["status"] = res["status"]
    wi["lifecycle_intent"] = res["lifecycle_intent"]  # v3.27.0 WP1
    p.write_text(yaml.safe_dump(wi, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return res


def selftest():
    import tempfile
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    gates = gate_executor.load_gates()

    # 1. start создаёт WorkItem с routed workflow и связями
    with tempfile.TemporaryDirectory() as td:
        wi = start(td, "demo-x", "починить опечатку в футере", task_type="bug")
        expect("start: WorkItem создан с workflow", bool(wi.get("workflow")))
        expect("start: связи blueprint+run_state+workitem заданы",
               set(wi["paths"]) == {"blueprint", "run_state", "workitem"})
        expect("start: файл workitem.yaml записан", wi_path(td, "demo-x").exists())

    # 2. QUICK без evidence -> needs_more_evidence (гейты не закрыты, доказательств нет)
    with tempfile.TemporaryDirectory() as td:
        r = derive_status("QUICK", Path(td), {})
        expect("QUICK без evidence -> needs_more_evidence", r["status"] == "needs_more_evidence")

    # 3. QUICK с полным evidence -> done
    good = {
        "intake_completeness": {"status": "pass", "provided": ["classified_type", "size", "risk"]},
        "implementation_verification": {"status": "pass",
            "provided": ["build_passed", "lint_passed", "typecheck_passed", "tests_passed", "tested_revision"]},
    }
    with tempfile.TemporaryDirectory() as td:
        r = derive_status("QUICK", Path(td), good)
        expect("QUICK с полным evidence -> done", r["status"] == "done")

    # 4. реальный провал гейта (evidence подан, fail) -> blocked
    with tempfile.TemporaryDirectory() as td:
        bad = dict(good)
        bad["implementation_verification"] = {"status": "fail", "blockers": ["tests failed"]}
        r = derive_status("QUICK", Path(td), bad)
        expect("реальный fail гейта -> blocked", r["status"] == "blocked")

    # 5. незакрытый human-approval гейт -> needs_human_decision
    hum_case = None
    for wid, w in gate_executor.load_workflows().items():
        gs = w.get("quality_gates", []) or []
        hum = [g for g in gs if gate_executor.classify(gates[g]) == "human-approval"]
        if hum:
            ev = {g: {"status": "pass", "provided": list(gates[g].get("required_evidence", []) or [])}
                  for g in gs if g not in hum}
            hum_case = derive_status(wid, Path("/nonexistent"), ev)
            break
    if hum_case is not None:
        expect("незакрытый human-approval -> needs_human_decision",
               hum_case["status"] == "needs_human_decision")
    else:
        print("SKIP: нет workflow с human-approval гейтом")

    # 6. приоритет: реальный fail важнее human-approval -> blocked
    with tempfile.TemporaryDirectory() as td:
        bad2 = dict(good)
        bad2["implementation_verification"] = {"status": "fail", "blockers": ["build failed"]}
        r = derive_status("QUICK", Path(td), bad2)
        expect("приоритет: реальный fail -> blocked (не evidence/human)", r["status"] == "blocked")

    print("workitem selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    if len(argv) >= 4 and argv[0] == "start":
        features_dir, fid = argv[1], argv[2]
        task = _opt(argv, "--task") or ""
        wi = start(features_dir, fid, task, _opt(argv, "--task-type"), _opt(argv, "--risk"))
        print(f"WorkItem '{fid}' -> workflow {wi['workflow']} "
              f"({'; '.join(wi['route_reasons'])}); blueprint {wi['paths']['blueprint']}. "
              f"Статус: draft (оцените `workitem.py status`).")
        return 0
    if len(argv) >= 3 and argv[0] == "status":
        res = status_cmd(argv[1], argv[2], _opt(argv, "--run-dir"), _opt(argv, "--evidence"))
        if "--json" in argv:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"WorkItem статус: {res['status'].upper()} — {res['action']}")
            if res["unmet_gates"]:
                print(f"  незакрытые гейты: {', '.join(res['unmet_gates'])}")
        return 0
    print(__doc__)
    return 0


def _opt(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
