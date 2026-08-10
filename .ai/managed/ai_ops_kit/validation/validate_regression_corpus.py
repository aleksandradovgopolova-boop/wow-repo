#!/usr/bin/env python3
"""Проверка Regression Corpus (regression-corpus/RC-*.yaml) — v3.5.

RegressionCase (schemas/regression-case.schema.json) — постоянная находка с таксономией слоя.
Валидатор:

  1. schema_version=1, kind=RegressionCase; failure_id формата RC-NNN; текстовые поля непусты;
  2. affected_layer ∈ 11-enum; status ∈ open|fixed|mitigated|wontfix; severity ∈ 4-enum;
  3. ЧЕСТНОСТЬ: status=fixed требует непустой fixed_version И regression_test (починка без
     охраняющего теста — не починка); status=open требует fixed_version=null;
  4. реестр: failure_id уникальны, имя файла == failure_id.

Использование:  validate_regression_corpus.py [regression-corpus] [--json] | --selftest
"""
import json
import re
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
SCHEMA = PKG / "schemas" / "regression-case.schema.json"
DEFAULT_DIR = PKG / "regression-corpus"
LAYERS = {"engine", "provider", "model", "reviewer", "policy", "context", "environment",
          "security", "delivery", "fixture", "process"}
STATUS = {"open", "fixed", "mitigated", "wontfix"}
SEVERITY = {"low", "medium", "high", "critical"}
_ID = re.compile(r"^RC-[0-9]{3,}$")


def check(data: dict):
    e = []
    if not isinstance(data, dict):
        return ["RegressionCase не объект"]
    if data.get("schema_version") != 1 or data.get("kind") != "RegressionCase":
        e.append("schema_version=1 + kind=RegressionCase обязательны")
    if not (isinstance(data.get("failure_id"), str) and _ID.match(data["failure_id"])):
        e.append("failure_id должен быть формата RC-NNN")
    for f in ("title", "trigger", "expected_behavior", "observed_behavior", "first_seen_version",
              "regression_test"):
        if not (isinstance(data.get(f), str) and data[f].strip()):
            e.append(f"{f} обязателен и непуст")
    if data.get("affected_layer") not in LAYERS:
        e.append(f"affected_layer ∉ {sorted(LAYERS)}")
    status = data.get("status")
    if status not in STATUS:
        e.append(f"status ∉ {sorted(STATUS)}")
    if data.get("severity") not in SEVERITY:
        e.append(f"severity ∉ {sorted(SEVERITY)}")
    fixed_v = data.get("fixed_version")
    if status == "fixed" and not (isinstance(fixed_v, str) and fixed_v.strip()):
        e.append("status=fixed требует непустой fixed_version (починка без версии)")
    if status == "open" and fixed_v is not None:
        e.append("status=open требует fixed_version=null")
    # Вывод 3 «дефектов одной сессии»: повтор класса (occurrences>=2) -> рекомендации мало, нужен
    # перевод в КОНСТРУКЦИЮ. Правило поверх правил, вычислимо: occurrences>=2 обязывает
    # on_repeat=escalate_to_structural + непустой structural_fix (иначе «правило есть, но не сработало»).
    occ = data.get("occurrences", 1)
    on_repeat = data.get("on_repeat")
    if occ is not None and not (isinstance(occ, int) and occ >= 1):
        e.append("occurrences должен быть целым >=1")
    if on_repeat is not None and on_repeat not in ("none", "escalate_to_structural"):
        e.append("on_repeat ∉ {none, escalate_to_structural}")
    if isinstance(occ, int) and occ >= 2:
        if on_repeat != "escalate_to_structural":
            e.append("повтор класса (occurrences>=2) требует on_repeat=escalate_to_structural "
                     "(рекомендацию мало — переведи в конструкцию)")
        if not (isinstance(data.get("structural_fix"), str) and data["structural_fix"].strip()):
            e.append("on_repeat=escalate_to_structural требует непустой structural_fix "
                     "(какая КОНСТРУКЦИЯ заменила рекомендацию)")
    return e


def check_registry(d: Path):
    errors, ids = [], set()
    for f in sorted(Path(d).glob("RC-*.yaml")) if Path(d).is_dir() else []:
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
        except yaml.YAMLError as ex:
            errors.append(f"{f.name}: не парсится YAML ({ex})")
            continue
        for x in check(data):
            errors.append(f"{f.name}: {x}")
        fid = (data or {}).get("failure_id")
        if isinstance(fid, str):
            if f.stem != fid:
                errors.append(f"{f.name}: имя файла != failure_id ({fid})")
            if fid in ids:
                errors.append(f"дубликат failure_id {fid}")
            ids.add(fid)
    return errors, ids


def taxonomy(d: Path):
    """Сводка по слоям/статусам (для observability)."""
    by_layer, by_status = {}, {}
    repeated, structural = 0, 0
    for f in sorted(Path(d).glob("RC-*.yaml")) if Path(d).is_dir() else []:
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        by_layer[data.get("affected_layer")] = by_layer.get(data.get("affected_layer"), 0) + 1
        by_status[data.get("status")] = by_status.get(data.get("status"), 0) + 1
        if isinstance(data.get("occurrences"), int) and data["occurrences"] >= 2:
            repeated += 1
        if data.get("on_repeat") == "escalate_to_structural":
            structural += 1
    return {"by_layer": by_layer, "by_status": by_status,
            "repeated_classes": repeated, "escalated_to_structural": structural}


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    ex = json.loads(SCHEMA.read_text(encoding="utf-8"))["examples"][0]
    expect("пример RC из схемы валиден", check(ex) == [])
    reg_errs, ids = check_registry(DEFAULT_DIR)
    expect(f"реальный regression-corpus целостен ({len(ids)} кейсов)", reg_errs == [])
    expect("status=fixed без fixed_version -> ошибка",
           any("fixed_version" in x for x in check({**ex, "status": "fixed", "fixed_version": None})))
    expect("status=fixed без regression_test -> ошибка",
           any("regression_test" in x for x in check({**ex, "regression_test": " "})))
    expect("status=open с fixed_version -> ошибка",
           any("open" in x for x in check({**ex, "status": "open", "fixed_version": "3.0.19"})))
    expect("неизвестный layer -> ошибка",
           any("affected_layer" in x for x in check({**ex, "affected_layer": "vibes"})))
    expect("битый failure_id -> ошибка", any("failure_id" in x for x in check({**ex, "failure_id": "RC1"})))
    # Вывод 3: повтор класса требует конструкцию (правило СРАБАТЫВАЕТ)
    expect("occurrences>=2 без on_repeat -> ошибка (рекомендации мало)",
           any("occurrences>=2" in x for x in check({**ex, "occurrences": 2})))
    expect("escalate_to_structural без structural_fix -> ошибка",
           any("structural_fix" in x for x in check({**ex, "occurrences": 2, "on_repeat": "escalate_to_structural"})))
    expect("повтор + escalate + конструкция -> валиден",
           check({**ex, "occurrences": 2, "on_repeat": "escalate_to_structural",
                  "structural_fix": "единственная функция apply_token + запрет прямого вызова линтером"}) == [])
    expect("occurrences=1 без on_repeat -> валиден (правило не срабатывает на одиночном)",
           check({**ex, "occurrences": 1}) == [])
    expect("битый on_repeat -> ошибка", any("on_repeat" in x for x in check({**ex, "on_repeat": "maybe"})))
    # таксономия непуста на реальном корпусе + несёт счётчики повторов
    tx = taxonomy(DEFAULT_DIR)
    expect("таксономия по слоям непуста", bool(tx["by_layer"]))
    expect("таксономия несёт repeated/structural счётчики",
           "repeated_classes" in tx and "escalated_to_structural" in tx)

    print("validate_regression_corpus selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    d = Path(args[0]) if args else DEFAULT_DIR
    errors, ids = check_registry(d)
    if "--json" in argv:
        print(json.dumps({"count": len(ids), "taxonomy": taxonomy(d), "errors": errors},
                         ensure_ascii=False, indent=2))
    elif errors:
        print(f"REGRESSION-CORPUS: {len(errors)} нарушений:")
        for x in errors:
            print(f"  - {x}")
    else:
        tx = taxonomy(d)
        print(f"REGRESSION-CORPUS-OK: {len(ids)} кейсов; слои={tx['by_layer']}; статусы={tx['by_status']}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
