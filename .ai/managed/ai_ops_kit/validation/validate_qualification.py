#!/usr/bin/env python3
"""Validate qualification scenario pack (v2.84 Qualification).

Пакет живых сценариев (qualification/scenarios.yaml) — не декоративный список: он должен быть
согласован с реальностью движка. Валидатор стережёт:
  1. форма каждого сценария (id, title, task, task_type, acceptance[], proves);
  2. task_type сценария существует в registry/workflows.yaml (нельзя квалифицировать по
     несуществующему классу);
  3. флаги сценария — из известного набора CLI ai-ops run / qual_run (нет опечаток-«призраков»);
  4. os_stack_matrix присутствует (os[], stacks[]).

Инвариант честности: сценарий приёмки ссылается на РЕАЛЬНЫЕ поля отчёта/классы, а не на выдумку.

Использование:
  validate_qualification.py [qualification/scenarios.yaml]
  validate_qualification.py --selftest
Возврат 0 — ок, 1 — ошибки.
"""

import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]

# известные флаги сценариев (ai-ops run / qual_run). Значения флагов с аргументом (--task-type X)
# пишутся как два токена; проверяем имя флага.
KNOWN_FLAGS = {
    "--sandbox", "--review", "--author", "--require-fix", "--baseline-diff", "--strict-green",
    "--open-pr", "--discard", "--task-type", "--max-steps", "--execute", "--json",
}
REQUIRED_FIELDS = ("id", "title", "task", "task_type", "acceptance", "proves")


def _workflow_ids():
    try:
        wf = yaml.safe_load((PKG / "registry" / "workflows.yaml").read_text(encoding="utf-8"))
        return set((wf or {}).get("workflows", {}))
    except OSError:
        return set()


def check(data, workflow_ids=None):
    errors = []
    if not isinstance(data, dict) or data.get("kind") != "qualification-scenarios":
        errors.append("kind должен быть 'qualification-scenarios'")
        data = data if isinstance(data, dict) else {}
    wf_ids = workflow_ids if workflow_ids is not None else _workflow_ids()
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        errors.append("scenarios должен быть непустым списком")
        scenarios = []
    seen = set()
    for i, s in enumerate(scenarios):
        if not isinstance(s, dict):
            errors.append(f"scenario[{i}] должен быть объектом"); continue
        sid = s.get("id", f"#{i}")
        for f in REQUIRED_FIELDS:
            if not s.get(f):
                errors.append(f"{sid}: нет поля '{f}'")
        if s.get("id") in seen:
            errors.append(f"дублирующийся id сценария: {s.get('id')}")
        seen.add(s.get("id"))
        tt = s.get("task_type")
        if tt and wf_ids and tt not in wf_ids:
            errors.append(f"{sid}: task_type '{tt}' отсутствует в registry/workflows.yaml")
        if s.get("acceptance") is not None and not isinstance(s.get("acceptance"), list):
            errors.append(f"{sid}: acceptance должен быть списком")
        for tok in s.get("flags", []) or []:
            if tok.startswith("--") and tok not in KNOWN_FLAGS:
                errors.append(f"{sid}: неизвестный флаг '{tok}' (не в CLI ai-ops run/qual_run)")
    matrix = data.get("os_stack_matrix")
    if not isinstance(matrix, dict) or not matrix.get("os") or not matrix.get("stacks"):
        errors.append("os_stack_matrix: нужны непустые os[] и stacks[]")
    return errors


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    wf = {"QUICK", "ENGINEERING", "PRODUCT"}
    good = {"kind": "qualification-scenarios",
            "scenarios": [{"id": "s1", "title": "t", "task": "do", "task_type": "QUICK",
                           "acceptance": ["a"], "proves": "p", "flags": ["--sandbox"]}],
            "os_stack_matrix": {"os": ["macOS"], "stacks": ["node"]}}
    expect("валидный пакет -> без ошибок", check(good, wf) == [])
    bad_tt = {"kind": "qualification-scenarios",
              "scenarios": [{"id": "s1", "title": "t", "task": "d", "task_type": "NOPE",
                             "acceptance": ["a"], "proves": "p"}],
              "os_stack_matrix": {"os": ["macOS"], "stacks": ["node"]}}
    expect("неизвестный task_type -> ошибка", any("NOPE" in e for e in check(bad_tt, wf)))
    bad_flag = {"kind": "qualification-scenarios",
                "scenarios": [{"id": "s1", "title": "t", "task": "d", "task_type": "QUICK",
                               "acceptance": ["a"], "proves": "p", "flags": ["--ghost"]}],
                "os_stack_matrix": {"os": ["macOS"], "stacks": ["node"]}}
    expect("неизвестный флаг -> ошибка", any("--ghost" in e for e in check(bad_flag, wf)))
    expect("нет обязательного поля -> ошибка",
           any("proves" in e for e in check(
               {"kind": "qualification-scenarios",
                "scenarios": [{"id": "s1", "title": "t", "task": "d", "task_type": "QUICK",
                               "acceptance": ["a"]}],
                "os_stack_matrix": {"os": ["m"], "stacks": ["n"]}}, wf)))
    expect("нет матрицы -> ошибка",
           any("os_stack_matrix" in e for e in check(
               {"kind": "qualification-scenarios",
                "scenarios": [{"id": "s1", "title": "t", "task": "d", "task_type": "QUICK",
                               "acceptance": ["a"], "proves": "p"}]}, wf)))
    # реальный поставляемый пакет проходит
    real = PKG / "qualification" / "scenarios.yaml"
    if real.exists():
        expect("поставляемый qualification/scenarios.yaml валиден",
               check(yaml.safe_load(real.read_text(encoding="utf-8"))) == [])

    print("validate_qualification selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    path = Path(argv[0]) if argv else (PKG / "qualification" / "scenarios.yaml")
    if not path.exists():
        print(f"QUALIFICATION: файл не найден: {path}")
        return 1
    errs = check(yaml.safe_load(path.read_text(encoding="utf-8")))
    if errs:
        print("QUALIFICATION: ошибки:")
        for e in errs:
            print(f"  - {e}")
        return 1
    print(f"QUALIFICATION-OK: {path.name} согласован (сценарии/матрица/флаги/классы).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
