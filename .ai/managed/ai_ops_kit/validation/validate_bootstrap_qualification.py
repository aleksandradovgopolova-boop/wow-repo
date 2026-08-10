#!/usr/bin/env python3
"""Validate BootstrapQualificationPlan (v3.8.0 — Product Bootstrap & Readiness Qualification).

План 3.8 — ДАННЫЕ, не живой прогон. Валидатор стережёт инварианты, чтобы Product Bootstrap не пропускал
стадии и не срезал доверие, И чтобы план строился НАД существующими контрактами (не новая подсистема):

  1. форма (kind/plan_id/phase; schema-mirror, только stdlib+pyyaml);
  2. builds_over непуст И каждый ref РЕАЛЬНО существует (нет фантомной «новой подсистемы»);
  3. reference_children: >=2, есть и greenfield, и real-brownfield (с нуля + реальная система);
  4. bootstrap_stages покрывают ВЕСЬ обязательный упорядоченный набор (repository_profile..delivery_pr);
  5. readiness_scenarios покрывают ВЕСЬ обязательный набор (single..product_learning);
  6. exit_criteria покрывают ВЕСЬ обязательный набор критериев выпуска 3.8;
  7. vertical_feature_rule и models заданы (сквозная функция; человек-судья до Bench v2);
  8. blocked_by непуст (внешний гейт живой квалификации объявлен явно).

  validate_bootstrap_qualification.py [qualification/bootstrap/v3.8.0-plan.yaml] | --selftest
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
DEFAULT_PLAN = PKG / "qualification" / "bootstrap" / "v3.8.0-plan.yaml"

REQUIRED_STAGES = ["repository_profile", "problem_jtbd", "architecture_adr", "data_schema", "backend",
                   "frontend_storybook", "ci", "observability", "vertical_feature", "delivery_pr"]
REQUIRED_SCENARIOS = {"single", "sequential", "concurrent_parallel", "resume", "fix_loop",
                      "provider_fallback", "human_security_approval", "base_moved", "release",
                      "post_release_readout", "product_learning"}
REQUIRED_EXIT = {"greenfield_zero_to_feature", "feature_released_or_ready", "fullstack_integrated",
                 "single_sequential_parallel_proven", "stack_aware_integration",
                 "resume_and_fixloop_on_real_task", "no_checkout_damage", "zero_false_green",
                 "evidence_exact_sha_bound", "external_delivery_intent_and_receipt",
                 "cost_per_verified_change", "post_release_readout_created", "result_to_feature_learning"}


def check(data, pkg=PKG):
    e = []
    if not isinstance(data, dict) or data.get("kind") != "BootstrapQualificationPlan":
        return ["kind должен быть BootstrapQualificationPlan"]
    if not data.get("plan_id") or data.get("phase") != "v3.8":
        e.append("нужны plan_id и phase=v3.8")
    # 2. builds_over непуст + каждый ref существует (не новая подсистема)
    bo = data.get("builds_over")
    if not isinstance(bo, list) or not bo:
        e.append("builds_over непуст обязателен (строимся НАД существующими контрактами)")
    else:
        for item in bo:
            ref = item.get("ref") if isinstance(item, dict) else None
            if not ref:
                e.append(f"builds_over: запись без ref ({item})")
            elif not (pkg / ref).exists():
                e.append(f"builds_over: ref '{ref}' не существует (фантомная ссылка)")
    # 3. reference_children >=2, greenfield + real-brownfield
    rc = data.get("reference_children") or []
    kinds = {c.get("kind") for c in rc if isinstance(c, dict)}
    if len(rc) < 2:
        e.append("reference_children: нужно >=2 (greenfield + реальная система)")
    if "greenfield" not in kinds:
        e.append("reference_children: нет greenfield-репо")
    if not (kinds & {"real-brownfield", "real", "brownfield"}):
        e.append("reference_children: нет реального (brownfield) репо")
    # 4. bootstrap_stages — весь упорядоченный набор
    stages = [s.get("id") for s in (data.get("bootstrap_stages") or []) if isinstance(s, dict)]
    miss_st = [s for s in REQUIRED_STAGES if s not in stages]
    if miss_st:
        e.append(f"bootstrap_stages: не хватает {miss_st}")
    if stages and [s for s in stages if s in REQUIRED_STAGES] != REQUIRED_STAGES:
        e.append("bootstrap_stages: порядок обязательных стадий нарушен (repository_profile..delivery_pr)")
    # 5/6. сценарии и критерии — весь набор
    scen = {s.get("id") for s in (data.get("readiness_scenarios") or []) if isinstance(s, dict)}
    if REQUIRED_SCENARIOS - scen:
        e.append(f"readiness_scenarios: не хватает {sorted(REQUIRED_SCENARIOS - scen)}")
    exit_c = set(data.get("exit_criteria") or [])
    if REQUIRED_EXIT - exit_c:
        e.append(f"exit_criteria: не хватает {sorted(REQUIRED_EXIT - exit_c)}")
    # 7. vertical_feature_rule + models
    if not (isinstance(data.get("vertical_feature_rule"), str) and data["vertical_feature_rule"].strip()):
        e.append("vertical_feature_rule обязателен (сквозная функция, не отдельно-зелёные слои)")
    models = data.get("models") or {}
    if models.get("security_integration_judge") != "human":
        e.append("models.security_integration_judge должен быть 'human' до Bench v2 (нет qualified автосудьи)")
    # 8. blocked_by непуст
    if not (isinstance(data.get("blocked_by"), list) and data["blocked_by"]):
        e.append("blocked_by непуст обязателен (внешний гейт живой квалификации объявлен явно)")
    return e


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    if DEFAULT_PLAN.exists():
        errs = check(yaml.safe_load(DEFAULT_PLAN.read_text(encoding="utf-8")))
        expect("реальный v3.8.0-plan валиден", errs == [])
        for x in errs:
            print("   -", x)
        base = yaml.safe_load(DEFAULT_PLAN.read_text(encoding="utf-8"))
    else:
        expect("v3.8.0-plan существует", False); return 1

    # негативы: срезанная стадия / сценарий / критерий / фантомный builds_over / нет greenfield
    import copy
    d1 = copy.deepcopy(base); d1["bootstrap_stages"] = [s for s in d1["bootstrap_stages"] if s["id"] != "vertical_feature"]
    expect("нет vertical_feature -> ошибка", any("bootstrap_stages" in x for x in check(d1)))
    d2 = copy.deepcopy(base); d2["readiness_scenarios"] = [s for s in d2["readiness_scenarios"] if s["id"] != "provider_fallback"]
    expect("нет provider_fallback сценария -> ошибка", any("readiness_scenarios" in x for x in check(d2)))
    d3 = copy.deepcopy(base); d3["exit_criteria"] = [c for c in d3["exit_criteria"] if c != "zero_false_green"]
    expect("нет zero_false_green в exit -> ошибка", any("exit_criteria" in x for x in check(d3)))
    d4 = copy.deepcopy(base); d4["builds_over"] = [{"name": "Ghost", "ref": "tools/ghost_subsystem.py"}]
    expect("фантомный builds_over ref -> ошибка (не новая подсистема)", any("не существует" in x for x in check(d4)))
    d5 = copy.deepcopy(base); d5["reference_children"] = [c for c in d5["reference_children"] if c.get("kind") != "greenfield"]
    expect("нет greenfield child -> ошибка", any("greenfield" in x for x in check(d5)))
    d6 = copy.deepcopy(base); d6["models"] = {"security_integration_judge": "kimi"}
    expect("автосудья security до Bench v2 -> ошибка (нужен human)", any("security_integration_judge" in x for x in check(d6)))

    print("validate_bootstrap_qualification selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    path = Path(args[0]) if args else DEFAULT_PLAN
    errs = check(yaml.safe_load(path.read_text(encoding="utf-8")))
    if errs:
        print(f"BOOTSTRAP-QUALIFICATION {path.name}: ошибки:")
        for x in errs:
            print(f"  - {x}")
        return 1
    print(f"BOOTSTRAP-QUALIFICATION-OK: {path.name} — план строится над существующими контрактами, "
          "стадии/сценарии/критерии полны, внешний гейт объявлен.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
