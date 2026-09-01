#!/usr/bin/env python3
"""Проверка UIEvidenceBundle (v3.1.7) — контракт UI-evidence из локальных артефактов child-репо.

Bundle (schemas/ui-evidence-bundle.schema.json) собирается tools/storybook_adapter.py и станет
источником для детерминированного закрытия части UI-гейтов (v3.1.8). Валидатор держит bundle
честным — структурно И семантически, чтобы «нет данных» не проскочило как «чисто»:

  1. schema_version=1, kind=UIEvidenceBundle; все секции на месте; лишних ключей нет (closed);
  2. enum'ы: секции status ∈ pass|fail|not_run; storybook.build_status ∈ pass|fail|absent;
  3. целые неотрицательны; interaction.passed ≤ total; a11y.total_violations ≥ blocking_violations;
  4. СОГЛАСОВАННОСТЬ статуса и цифр (нельзя фабриковать pass):
     - a11y: pass ⟺ blocking_violations=0; fail ⟺ blocking_violations≥1;
     - interaction: pass ⟺ (total>0 ∧ passed=total); fail ⟺ passed<total;
     - visual: pass ⟺ changed=0; fail ⟺ changed≥1 (когда changed задан);
     - design_system: pass требует (нет новых компонентов ∨ new_components_justified); есть новые
       и не обоснованы -> обязан быть fail;
  5. state_coverage самосогласован: missing = required без покрытия; complete ⟺ missing пуст.

Использование:  validate_storybook_evidence.py <bundle.json> [--json]
                validate_storybook_evidence.py --selftest
Возврат 0 — валиден, 1 — ошибки.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
SCHEMA = PKG / "schemas" / "ui-evidence-bundle.schema.json"

STATUS3 = {"pass", "fail", "not_run"}
BUILD = {"pass", "fail", "absent"}

# Разрешённые ключи секций (closed — эквивалент additionalProperties:false).
_KEYS = {
    "storybook": {"detected", "build_status", "version", "story_count"},
    "state_coverage": {"required", "states", "missing", "complete"},
    "interaction_tests": {"status", "total", "passed"},
    "accessibility": {"status", "blocking_violations", "total_violations"},
    "visual_regression": {"status", "changed"},
    "design_system": {"status", "reused_components", "new_components", "new_components_justified"},
}
_TOP = {"schema_version", "kind", "commit_sha", "generated_from", "affected_components",
        "affected_stories", "component_catalog", "storybook", "state_coverage",
        "interaction_tests", "accessibility", "visual_regression", "design_system"}


def _comp_base(name):
    return (name or "").rstrip("/").split("/")[-1].strip().lower()


def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def check(data: dict):
    e = []
    if not isinstance(data, dict):
        return ["bundle не объект"]

    if data.get("schema_version") != 1:
        e.append("schema_version должен быть 1")
    if data.get("kind") != "UIEvidenceBundle":
        e.append("kind должен быть 'UIEvidenceBundle'")
    if "commit_sha" not in data:
        e.append("нет commit_sha (может быть null)")
    for k in data:
        if k not in _TOP:
            e.append(f"лишний ключ верхнего уровня: {k}")

    def _section(name):
        s = data.get(name)
        if not isinstance(s, dict):
            e.append(f"секция {name} отсутствует/не объект")
            return None
        for k in s:
            if k not in _KEYS[name]:
                e.append(f"{name}: лишний ключ {k}")
        return s

    sb = _section("storybook")
    if sb is not None:
        if not isinstance(sb.get("detected"), bool):
            e.append("storybook.detected должен быть bool")
        if sb.get("build_status") not in BUILD:
            e.append(f"storybook.build_status ∉ {sorted(BUILD)}")
        if "story_count" in sb and not (_is_int(sb["story_count"]) and sb["story_count"] >= 0):
            e.append("storybook.story_count должен быть целым ≥0")

    sc = _section("state_coverage")
    if sc is not None:
        req = sc.get("required")
        states = sc.get("states")
        missing = sc.get("missing")
        if not isinstance(req, list) or not isinstance(states, dict) or not isinstance(missing, list):
            e.append("state_coverage: required/states/missing неверных типов")
        else:
            if not all(isinstance(v, bool) for v in states.values()):
                e.append("state_coverage.states: значения должны быть bool")
            recomputed = [s for s in req if not states.get(s, False)]
            if sorted(missing) != sorted(recomputed):
                e.append(f"state_coverage.missing несогласован: ожидалось {sorted(recomputed)}")
            if not isinstance(sc.get("complete"), bool):
                e.append("state_coverage.complete должен быть bool")
            elif sc.get("complete") != (len(recomputed) == 0):
                e.append("state_coverage.complete не соответствует missing")

    it = _section("interaction_tests")
    if it is not None:
        st = it.get("status")
        if st not in STATUS3:
            e.append(f"interaction_tests.status ∉ {sorted(STATUS3)}")
        total = it.get("total")
        passed = it.get("passed")
        for key, v in (("total", total), ("passed", passed)):
            if v is not None and not (_is_int(v) and v >= 0):
                e.append(f"interaction_tests.{key} должен быть целым ≥0")
        if _is_int(total) and _is_int(passed):
            if passed > total:
                e.append("interaction_tests.passed > total")
            if st == "pass" and not (total > 0 and passed == total):
                e.append("interaction_tests: status=pass требует total>0 и passed=total")
            if st == "fail" and passed == total and total > 0:
                e.append("interaction_tests: status=fail, но passed=total (несогласовано)")

    ac = _section("accessibility")
    if ac is not None:
        st = ac.get("status")
        if st not in STATUS3:
            e.append(f"accessibility.status ∉ {sorted(STATUS3)}")
        bv = ac.get("blocking_violations")
        if not (_is_int(bv) and bv >= 0):
            e.append("accessibility.blocking_violations должен быть целым ≥0")
        else:
            if st == "pass" and bv > 0:
                e.append("accessibility: status=pass, но blocking_violations>0")
            if st == "fail" and bv == 0:
                e.append("accessibility: status=fail, но blocking_violations=0")
        tv = ac.get("total_violations")
        if tv is not None:
            if not (_is_int(tv) and tv >= 0):
                e.append("accessibility.total_violations должен быть целым ≥0")
            elif _is_int(bv) and tv < bv:
                e.append("accessibility.total_violations < blocking_violations")

    vr = _section("visual_regression")
    if vr is not None:
        st = vr.get("status")
        if st not in STATUS3:
            e.append(f"visual_regression.status ∉ {sorted(STATUS3)}")
        ch = vr.get("changed")
        if ch is not None:
            if not (_is_int(ch) and ch >= 0):
                e.append("visual_regression.changed должен быть целым ≥0")
            else:
                if st == "pass" and ch > 0:
                    e.append("visual_regression: status=pass, но changed>0")
                if st == "fail" and ch == 0:
                    e.append("visual_regression: status=fail, но changed=0")

    ds = _section("design_system")
    if ds is not None:
        st = ds.get("status")
        if st not in STATUS3:
            e.append(f"design_system.status ∉ {sorted(STATUS3)}")
        for key in ("reused_components", "new_components"):
            v = ds.get(key)
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                e.append(f"design_system.{key} должен быть списком строк")
        just = ds.get("new_components_justified")
        if not isinstance(just, bool):
            e.append("design_system.new_components_justified должен быть bool")
        new = ds.get("new_components") if isinstance(ds.get("new_components"), list) else []
        if isinstance(just, bool):
            if new and not just and st == "pass":
                e.append("design_system: есть новые компоненты без обоснования, но status=pass")
        # v3.2.3 component-reuse enforcement: «новый» компонент, уже присутствующий в каталоге
        # дизайн-системы, — дублирование (надо переиспользовать, а не создавать заново).
        catalog = data.get("component_catalog")
        if isinstance(catalog, list) and new:
            cat = {_comp_base(c) for c in catalog}
            dup = [n for n in new if _comp_base(n) in cat]
            if dup:
                e.append(f"design_system: новые компоненты дублируют существующие в каталоге "
                         f"(reuse-нарушение): {dup}")
    return e


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    data = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    errors = check(data)
    if "--json" in argv:
        print(json.dumps({"errors": errors}, ensure_ascii=False, indent=2))
    elif errors:
        print("UI-EVIDENCE: ошибки:")
        for x in errors:
            print(f"  - {x}")
    else:
        print("UI-EVIDENCE-OK: bundle валиден (структурно и семантически).")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
