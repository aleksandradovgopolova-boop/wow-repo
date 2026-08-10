#!/usr/bin/env python3
"""Validate + render spec-change artifact (v2.89 Product Authoring: specification).

Гейт `specification` (ENGINEERING/PRODUCT) требует evidence openspec_valid + requirements_covered.
Раньше движок его не производил -> честно блокировал. v2.89: author-модель отдаёт СТРУКТУРНОЕ
описание изменения (capability + требования + сценарии), а движок РЕНДЕРИТ его в точный OpenSpec-
markdown и прогоняет НАСТОЯЩИМ `openspec validate --strict`. Формат markdown контролирует движок
(не модель) — поэтому валидная структура надёжно проходит strict-валидацию.

check() проверяет ФОРМУ структурного описания. render() пишет OpenSpec-change (proposal/tasks/
specs) в <openspec_root>/changes/<id>/. openspec_valid даёт ТОЛЬКО реальный CLI (в движке);
requirements_covered — структурно (есть требования).

Форма (YAML) от автора:
  schema_version: 1
  kind: spec-change
  capability: pricing            # slug [a-z0-9][a-z0-9-]*
  why: "зачем"
  what_changes: ["что меняется"]
  impact: "на что влияет"        # опционально
  tasks: ["шаг 1", "шаг 2"]
  requirements:
    - name: "Price formatting"
      text: "The system SHALL ..."         # нормативное требование
      scenarios:
        - {name: "Thousands", when: "...", then: "..."}

Использование:
  validate_spec_artifact.py <artifact.yaml>
  validate_spec_artifact.py --selftest
Возврат 0 — ок, 1 — ошибки.
"""

import re
import sys
from pathlib import Path

import yaml

REQUIRED_EVIDENCE = ["openspec_valid", "requirements_covered"]
_CAP_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def check(data):
    errors = []
    if not isinstance(data, dict) or data.get("kind") != "spec-change":
        errors.append("kind должен быть 'spec-change'")
        data = data if isinstance(data, dict) else {}
    if data.get("schema_version") is None:
        errors.append("нет schema_version")
    cap = data.get("capability")
    if not (isinstance(cap, str) and _CAP_RE.match(cap or "")):
        errors.append(f"capability должен быть slug [a-z0-9-]: {cap!r}")
    if not (isinstance(data.get("why"), str) and data["why"].strip()):
        errors.append("why: непустая строка обязательна")
    wc = data.get("what_changes")
    if not (isinstance(wc, list) and wc and all(isinstance(x, str) and x.strip() for x in wc)):
        errors.append("what_changes: непустой список непустых строк")
    tasks = data.get("tasks")
    if not (isinstance(tasks, list) and tasks and all(isinstance(x, str) and x.strip() for x in tasks)):
        errors.append("tasks: непустой список непустых строк")
    reqs = data.get("requirements")
    if not (isinstance(reqs, list) and reqs):
        errors.append("requirements: непустой список")
        reqs = []
    for i, r in enumerate(reqs):
        if not isinstance(r, dict):
            errors.append(f"requirement[{i}] должен быть объектом"); continue
        rn = r.get("name", f"#{i}")
        if not (isinstance(r.get("name"), str) and r["name"].strip()):
            errors.append(f"requirement[{i}]: нет name")
        if not (isinstance(r.get("text"), str) and r["text"].strip()):
            errors.append(f"{rn}: нет text (нормативная формулировка)")
        scs = r.get("scenarios")
        if not (isinstance(scs, list) and scs):
            errors.append(f"{rn}: нужен непустой scenarios")
            scs = []
        for s in scs:
            if not (isinstance(s, dict) and isinstance(s.get("when"), str) and s["when"].strip()
                    and isinstance(s.get("then"), str) and s["then"].strip()):
                errors.append(f"{rn}: каждый scenario требует непустые when + then")
    return errors


def provided_evidence(data):
    """requirements_covered — структурный ключ (есть требования). openspec_valid добавляет ДВИЖОК
    после реального `openspec validate`. Пусто, если структура невалидна."""
    return ["requirements_covered"] if not check(data) else []


def _slug_tasks(tasks):
    return "\n".join(f"- [ ] 1.{i+1} {t.strip()}" for i, t in enumerate(tasks))


def render(data, openspec_root, change_id):
    """Отрендерить spec-change в OpenSpec-структуру под <openspec_root>/changes/<change_id>/.
    Возвращает список записанных файлов. Предполагает, что data уже прошла check()."""
    root = Path(openspec_root)
    change = root / "changes" / change_id
    cap = data["capability"]
    (change / "specs" / cap).mkdir(parents=True, exist_ok=True)

    what = "\n".join(f"- {x.strip()}" for x in data["what_changes"])
    impact = data.get("impact") or f"Затрагивает capability `{cap}`."
    proposal = f"## Why\n{data['why'].strip()}\n\n## What Changes\n{what}\n\n## Impact\n{impact}\n"
    tasks = f"## 1. Implementation\n{_slug_tasks(data['tasks'])}\n"

    blocks = ["## ADDED Requirements\n"]
    for r in data["requirements"]:
        blocks.append(f"### Requirement: {r['name'].strip()}\n{r['text'].strip()}\n")
        for s in r["scenarios"]:
            nm = (s.get("name") or "Scenario").strip()
            blocks.append(f"#### Scenario: {nm}\n- WHEN {s['when'].strip()}\n- THEN {s['then'].strip()}\n")
    spec = "\n".join(blocks)

    written = []
    for rel, content in ((change / "proposal.md", proposal),
                         (change / "tasks.md", tasks),
                         (change / "specs" / cap / "spec.md", spec)):
        rel.write_text(content, encoding="utf-8")
        written.append(str(rel))
    return written


def load(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def selftest():
    import tempfile
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    good = {"schema_version": 1, "kind": "spec-change", "capability": "pricing",
            "why": "нужна утилита цены", "what_changes": ["добавить formatPrice"],
            "tasks": ["реализовать", "покрыть тестом"],
            "requirements": [{"name": "Price formatting",
                              "text": "The system SHALL format an integer price with thousand separators.",
                              "scenarios": [{"name": "Thousands", "when": "formatPrice(1000)", "then": "returns 1 000"}]}]}
    expect("валидный spec-change -> без ошибок", check(good) == [])
    expect("валидный -> requirements_covered предоставлен", provided_evidence(good) == ["requirements_covered"])
    expect("плохой capability -> ошибка", any("capability" in e for e in check({**good, "capability": "Bad Cap"})))
    expect("требование без scenarios -> ошибка",
           any("scenarios" in e for e in check({**good, "requirements": [{"name": "x", "text": "SHALL y"}]})))
    expect("scenario без then -> ошибка",
           any("when + then" in e for e in check({**good, "requirements": [
               {"name": "x", "text": "SHALL y", "scenarios": [{"when": "a"}]}]})))
    expect("невалидный -> evidence пуст", provided_evidence({"kind": "spec-change"}) == [])

    # render пишет корректный OpenSpec-markdown
    with tempfile.TemporaryDirectory() as td:
        written = render(good, Path(td) / "openspec", "feat-x")
        expect("render: 3 файла (proposal/tasks/spec)", len(written) == 3)
        spec_txt = (Path(td) / "openspec" / "changes" / "feat-x" / "specs" / "pricing" / "spec.md").read_text(encoding="utf-8")
        expect("render: spec содержит '## ADDED Requirements'", "## ADDED Requirements" in spec_txt)
        expect("render: spec содержит '### Requirement:' и WHEN/THEN",
               "### Requirement: Price formatting" in spec_txt and "- WHEN" in spec_txt and "- THEN" in spec_txt)
        prop = (Path(td) / "openspec" / "changes" / "feat-x" / "proposal.md").read_text(encoding="utf-8")
        expect("render: proposal содержит Why/What Changes/Impact",
               "## Why" in prop and "## What Changes" in prop and "## Impact" in prop)

    print("validate_spec_artifact selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    if not argv:
        print(__doc__); return 1
    errs = check(load(argv[0]))
    if errs:
        print("SPEC-ARTIFACT: ошибки:")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("SPEC-ARTIFACT-OK: структура spec-change валидна (openspec_valid подтверждает CLI отдельно).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
