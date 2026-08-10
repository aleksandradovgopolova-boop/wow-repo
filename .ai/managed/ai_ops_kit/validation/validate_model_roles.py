#!/usr/bin/env python3
"""Validate model-roles registry (Provider Independence & Cheapest Qualified Model, ADR-004).

Связывает роли рантайма с КЛАССАМИ возможностей (registry/models.yaml model_classes), НЕ с вендорами.
Инварианты (safety over economy):
  1. preferred_class / fallback_class каждой роли существуют в models.yaml model_classes;
  2. cheapest-first: preferred_class не дороже fallback_class (по cost_class моделей класса);
  3. для КАЖДОЙ роли назначенный класс имеет статус `qualified` в матрице (иначе роутить некого);
  4. security_review и integration_judge (судьи) — ТОЛЬКО класс со статусом `qualified` (не
     conditional/experimental) — safety-first;
  5. escalation: targeted-retry >=0, escalate_scope=review_only (эскалируем судью, не всю задачу),
     `cost_never_by_weakening_gates: true` (жёсткий инвариант ADR-004);
  6. матрица покрывает все роли; статусы из допустимого enum; ключи матрицы — существующие классы.

  validate_model_roles.py [registry/model-roles.yaml] | --selftest
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
DEFAULT = PKG / "registry" / "model-roles.yaml"
ROLES = ("implementation", "code_review", "security_review", "integration_judge")
JUDGE_ROLES = ("security_review", "integration_judge")
STATUS = {"qualified", "conditional", "experimental", "not_qualified"}
_COST_ORDER = {"low": 0, "medium": 1, "high": 2, None: 1}


def _model_classes_and_cost(pkg=PKG):
    """(set классов, {class: min cost rank среди моделей класса}) из registry/models.yaml."""
    try:
        d = yaml.safe_load((pkg / "registry" / "models.yaml").read_text(encoding="utf-8"))
    except OSError:
        return set(), {}
    classes = set((d.get("model_classes") or {}).keys())
    cost = {}
    for m in d.get("models", []) or []:
        rank = _COST_ORDER.get(m.get("cost_class"), 1)
        for c in m.get("classes", []) or []:
            cost[c] = min(cost.get(c, 99), rank)
    return classes, cost


def check(data, pkg=PKG):
    e = []
    if not isinstance(data, dict):
        return ["model-roles не объект"]
    if data.get("registry_type") != "model-roles":
        e.append("registry_type должен быть model-roles")
    classes, cost = _model_classes_and_cost(pkg)
    roles = data.get("roles") or {}
    matrix = data.get("qualification_matrix") or {}

    def qualified(cls, role):
        return (matrix.get(cls, {}) or {}).get(role) == "qualified"

    for role in ROLES:
        r = roles.get(role)
        if not isinstance(r, dict):
            e.append(f"роль {role} отсутствует/не объект"); continue
        pc, fc = r.get("preferred_class"), r.get("fallback_class")
        for label, cls in (("preferred", pc), ("fallback", fc)):
            if cls not in classes:
                e.append(f"{role}.{label}_class '{cls}' нет в models.yaml model_classes")
        # cheapest-first: preferred не дороже fallback
        if pc in cost and fc in cost and cost[pc] > cost[fc]:
            e.append(f"{role}: preferred_class '{pc}' дороже fallback_class '{fc}' (нарушен cheapest-first)")
        # маршрутить некого, если ни preferred, ни fallback не qualified для роли
        if not (qualified(pc, role) or qualified(fc, role)):
            e.append(f"{role}: ни preferred, ни fallback не 'qualified' в матрице — некого роутить")
        # судьи: назначенный класс должен быть qualified (не conditional/experimental)
        if role in JUDGE_ROLES and not qualified(pc, role):
            e.append(f"{role} (судья): preferred_class '{pc}' не 'qualified' — safety-first нарушен")

    esc = data.get("escalation_policy") or {}
    if not (esc.get("triggers")):
        e.append("escalation_policy.triggers непустой обязателен")
    if not isinstance(esc.get("max_targeted_retries"), int) or esc["max_targeted_retries"] < 0:
        e.append("escalation_policy.max_targeted_retries должен быть int>=0")
    if esc.get("escalate_scope") != "review_only":
        e.append("escalation_policy.escalate_scope должен быть review_only (эскалируем судью, не всю задачу)")
    if esc.get("cost_never_by_weakening_gates") is not True:
        e.append("escalation_policy.cost_never_by_weakening_gates обязан быть true (инвариант ADR-004)")

    for cls, row in matrix.items():
        if classes and cls not in classes:
            e.append(f"матрица: класс '{cls}' нет в model_classes")
        for role, st in (row or {}).items():
            if st not in STATUS:
                e.append(f"матрица {cls}.{role}: статус '{st}' ∉ {sorted(STATUS)}")
    for cls, row in matrix.items():
        missing = [r for r in ROLES if r not in (row or {})]
        if missing:
            e.append(f"матрица {cls}: не покрыты роли {missing}")
    return e


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    if DEFAULT.exists():
        errs = check(yaml.safe_load(DEFAULT.read_text(encoding="utf-8")))
        expect("реальный registry/model-roles.yaml валиден", errs == [])
        for x in errs:
            print("   -", x)

    classes, _ = _model_classes_and_cost()
    base = {"registry_type": "model-roles",
            "roles": {r: {"preferred_class": ("balanced" if r in ("implementation", "code_review") else "high-reasoning"),
                          "fallback_class": "high-reasoning"} for r in ROLES},
            "escalation_policy": {"triggers": ["reviewer_abstain"], "max_targeted_retries": 1,
                                  "escalate_scope": "review_only", "cost_never_by_weakening_gates": True},
            "qualification_matrix": {
                "balanced": {"implementation": "qualified", "code_review": "conditional",
                             "security_review": "not_qualified", "integration_judge": "not_qualified"},
                "high-reasoning": {"implementation": "qualified", "code_review": "qualified",
                                   "security_review": "qualified", "integration_judge": "qualified"}}}
    expect("синтетический базовый валиден", check(base) == [])
    expect("escalate_scope != review_only -> ошибка",
           any("review_only" in x for x in check({**base, "escalation_policy": {**base["escalation_policy"], "escalate_scope": "full"}})))
    expect("cost_never_by_weakening_gates=false -> ошибка",
           any("cost_never" in x for x in check({**base, "escalation_policy": {**base["escalation_policy"], "cost_never_by_weakening_gates": False}})))
    # судья на неквалифицированном классе -> ошибка
    bad_judge = {**base, "roles": {**base["roles"], "security_review": {"preferred_class": "balanced", "fallback_class": "balanced"}}}
    expect("security_review на 'balanced' (не qualified) -> safety-first ошибка",
           any("судья" in x for x in check(bad_judge)))
    # несуществующий класс
    expect("несуществующий класс -> ошибка",
           any("model_classes" in x for x in check({**base, "roles": {**base["roles"], "implementation": {"preferred_class": "vibes", "fallback_class": "high-reasoning"}}})))

    print("validate_model_roles selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    path = Path(args[0]) if args else DEFAULT
    errs = check(yaml.safe_load(path.read_text(encoding="utf-8")))
    if errs:
        print(f"MODEL-ROLES {path.name}: ошибки:")
        for x in errs:
            print(f"  - {x}")
        return 1
    print(f"MODEL-ROLES-OK: {path.name} валиден.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
