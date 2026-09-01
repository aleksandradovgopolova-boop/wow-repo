"""Чистая проверка + рендер СОДЕРЖИМОГО spec-change. Вынесена из
`validation/validate_spec_artifact.py` вниз (лента №5), чтобы движок (pipeline_evidence) звал её
ВНИЗ, без восходящего ребра engine -> validation.

check() проверяет ФОРМУ структурного описания. render_content() строит OpenSpec-markdown
(proposal/tasks/specs) как СПИСОК (относительный-путь, содержимое) — чистая функция без записи на
диск: запись файлов делает вызыватель (движок в своём слое, либо CLI-обёртка render()). Разрез
именно такой потому, что `checks` — слой primitives (только stdlib, никакого ввода-вывода), а
mkdir/write — это I/O, и ему не место ниже ядра.

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
      text: "The system SHALL ..."
      scenarios:
        - {name: "Thousands", when: "...", then: "..."}
"""
from __future__ import annotations

import re

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


def render_content(data, change_id):
    """Построить содержимое spec-change как список (относительный путь, текст).

    Пути ОТНОСИТЕЛЬНЫ корня openspec (<openspec_root>/changes/<change_id>/...). Запись на диск —
    забота вызывателя (см. render() в CLI-обёртке и движок). Предполагает, что data уже прошла check().
    """
    cap = data["capability"]
    base = f"changes/{change_id}"

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

    return [(f"{base}/proposal.md", proposal),
            (f"{base}/tasks.md", tasks),
            (f"{base}/specs/{cap}/spec.md", spec)]
