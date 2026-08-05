#!/usr/bin/env python3
"""Проверка структурного результата ревьюера (v2.33, Execution Engine Фаза 1).

Reviewer возвращает не свободный markdown, а структуру (schemas/reviewer-result.schema.json):
status/checks/blockers. Она — источник истины для гейта (gate_executor.collect_evidence
читает stage-<id>.reviewer.json первым); человеческий текст генерится поверх. Валидатор
держит структуру честной:

  1. schema_version/kind/gate/status/checks на месте; status ∈ pass|warn|fail;
  2. gate резолвится в quality/gates.yaml;
  3. каждый check: id + status ∈ pass|warn|fail;
  4. status ∈ {fail, warn} ОБЯЗАН иметь blockers (иначе «заблокировано без причины»): warn на
     блокирующем гейте блокирует так же, как fail (writer≠judge, v2.85) — значит и обязан назвать
     конкретные сомнения. Честность симметрична: нельзя ни фабриковать pass, ни фабриковать блок;
  5. согласованность: если есть check со status=fail, общий status не может быть pass.

Использование:  validate_reviewer_result.py <result.json> [--json]
                validate_reviewer_result.py --selftest
Возврат 0 — валиден, 1 — ошибки.
"""

import json
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
ST = {"pass", "warn", "fail"}


def _gate_ids():
    try:
        return set(yaml.safe_load((PKG / "quality" / "gates.yaml").read_text(encoding="utf-8"))["gates"])
    except Exception:
        return set()


def check(data: dict, gate_ids=None):
    errors = []
    if data.get("schema_version") is None:
        errors.append("нет schema_version")
    if data.get("kind") != "reviewer-result":
        errors.append("kind должен быть 'reviewer-result'")
    if data.get("status") not in ST:
        errors.append(f"status '{data.get('status')}' не в {sorted(ST)}")
    gid = data.get("gate")
    if not gid:
        errors.append("нет gate")
    elif gate_ids is not None and gid not in gate_ids:
        errors.append(f"gate '{gid}' отсутствует в quality/gates.yaml")

    checks = data.get("checks")
    if not isinstance(checks, list) or not checks:
        errors.append("checks должен быть непустым списком")
        checks = []
    any_fail = False
    for c in checks:
        if not isinstance(c, dict) or not c.get("id") or c.get("status") not in ST:
            errors.append("check требует id:str + status∈[pass,warn,fail]")
            continue
        if c["status"] == "fail":
            any_fail = True

    # v3.0-rc11 (finding живого прогона kimi): warn на блокирующем гейте БЛОКИРУЕТ (v2.85) — значит
    # это тоже блокирующий вердикт и обязан назвать конкретику. Contentless warn = «блок без причины»
    # (унфальсифицируемый). Симметрия честности: fail и warn одинаково требуют непустой blockers.
    if data.get("status") in ("fail", "warn") and not (data.get("blockers")):
        errors.append(f"status={data.get('status')} требует непустой blockers (блокирующий вердикт без причины)")
    if any_fail and data.get("status") == "pass":
        errors.append("есть check со status=fail, но общий status=pass — несогласованно")
    return errors


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    gate_ids = _gate_ids()
    valid = {"schema_version": 1, "kind": "reviewer-result", "gate": "code_review",
             "reviewer": "code-reviewer", "reviewed_revision": "abc1234",
             "status": "fail",
             "checks": [{"id": "acceptance_scenario_3", "status": "fail",
                         "evidence": {"file": "src/orders/filter.ts", "lines": "74-91"}}],
             "blockers": ["Empty state for zero matching orders is missing"]}
    expect("валидный fail c blockers", check(valid, gate_ids) == [])

    expect("fail без blockers -> ошибка",
           any("blockers" in e for e in check({**valid, "blockers": []}, gate_ids)))

    # rc11: warn (блокирующий вердикт) тоже обязан нести blockers — contentless warn отвергается
    warn_no_bl = {"schema_version": 1, "kind": "reviewer-result", "gate": "code_review",
                  "status": "warn", "checks": [{"id": "c1", "status": "warn"}]}
    expect("rc11: warn без blockers -> ошибка (блок без причины)",
           any("blockers" in e for e in check(warn_no_bl, gate_ids)))
    expect("rc11: warn C blockers -> валиден",
           check({**warn_no_bl, "blockers": ["ярус 500 не покрыт тестом"]}, gate_ids) == [])

    incoherent = {**valid, "status": "pass"}
    expect("fail-check при status=pass -> ошибка",
           any("несогласованно" in e for e in check(incoherent, gate_ids)))

    expect("неизвестный gate -> ошибка",
           any("отсутствует" in e for e in check({**valid, "gate": "nope"}, gate_ids)))

    okr = {"schema_version": 1, "kind": "reviewer-result", "gate": "code_review",
           "status": "pass", "checks": [{"id": "c1", "status": "pass"}]}
    expect("валидный pass", check(okr, gate_ids) == [])

    print("validate_reviewer_result selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__); return 1
    data = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    errors = check(data, _gate_ids())
    if "--json" in argv:
        print(json.dumps({"errors": errors}, ensure_ascii=False, indent=2))
    elif errors:
        print("REVIEWER-RESULT: ошибки:")
        for e in errors:
            print(f"  - {e}")
    else:
        print("REVIEWER-RESULT-OK: структура валидна.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
