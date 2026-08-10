#!/usr/bin/env python3
"""Проверка workflow-контрактов и gate-реестра AI-first системы (registry/workflows.yaml, quality/gates.yaml).

Ловит то, что реально ломается при ручных правках контрактов:
  1. невалидный YAML;
  2. стадия workflow ссылается на несуществующего агента (нет в registry/agents.yaml);
  3. workflow ссылается на gate, которого нет в quality/gates.yaml;
  4. отсутствие обязательных полей workflow / gate;
  5. responsible_role gate не является известным агентом;
  6. MVP: число blocking-gate'ов > 8 ИЛИ gate из mvp_blocking отсутствует;
  7. writer/judge: стадия review_mode=read-only не должна быть writer той же стадии.

Использование:  python3 02_tools/ci/validate_ai_first_workflows.py
Возврат 0 — чисто, 1 — есть ошибки. Требует pyyaml.
"""

import sys
from pathlib import Path

import yaml

PKG_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path.cwd()
SYS_DIR = PKG_ROOT
AGENTS_REG = SYS_DIR / "registry" / "agents.yaml"
WORKFLOWS = SYS_DIR / "registry" / "workflows.yaml"
GATES = SYS_DIR / "quality" / "gates.yaml"

WF_REQUIRED = ["id", "purpose", "stages", "quality_gates", "preferred_execution_mode", "minimum_execution_mode"]
GATE_REQUIRED = ["id", "responsible_role", "blocking"]
MVP_BLOCKING_MAX = 8

errors = []


def fail(where, msg):
    errors.append(f"{where}: {msg}")


def load(path):
    if not path.exists():
        fail(path.name, "файл отсутствует")
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        fail(path.name, f"невалидный YAML: {exc}")
        return None
    if not isinstance(data, dict):
        fail(path.name, "верхний уровень не словарь")
        return None
    return data


def known_agent_ids():
    data = load(AGENTS_REG)
    if not data or not isinstance(data.get("agents"), list):
        fail("agents.yaml", "не удалось получить список агентов для сверки")
        return set()
    return {e.get("id") for e in data["agents"] if isinstance(e, dict)}


def check_gates(agent_ids):
    data = load(GATES)
    if data is None:
        return set()
    gates = data.get("gates")
    if not isinstance(gates, dict) or not gates:
        fail("gates.yaml", "нет непустого gates")
        return set()
    gate_ids = set()
    blocking_ids = set()
    for gid, g in gates.items():
        if not isinstance(g, dict):
            fail("gates.yaml", f"gate {gid} не словарь")
            continue
        gate_ids.add(gid)
        for f in GATE_REQUIRED:
            if f not in g:
                fail("gates.yaml", f"gate '{gid}' без поля '{f}'")
        if g.get("id") != gid:
            fail("gates.yaml", f"gate '{gid}': поле id='{g.get('id')}' не совпадает с ключом")
        role = g.get("responsible_role")
        if role and role not in agent_ids:
            fail("gates.yaml", f"gate '{gid}': responsible_role '{role}' не найден в реестре агентов")
        if g.get("blocking") is True:
            blocking_ids.add(gid)

    mvp = data.get("mvp_blocking_gates", [])
    if len(mvp) > MVP_BLOCKING_MAX:
        fail("gates.yaml", f"MVP blocking gates = {len(mvp)} > {MVP_BLOCKING_MAX}")
    for gid in mvp:
        if gid not in gate_ids:
            fail("gates.yaml", f"mvp_blocking_gates: '{gid}' отсутствует в gates")
    return gate_ids


def check_workflows(agent_ids, gate_ids):
    data = load(WORKFLOWS)
    if data is None:
        return
    wfs = data.get("workflows")
    if not isinstance(wfs, dict) or not wfs:
        fail("workflows.yaml", "нет непустого workflows")
        return
    for wid, w in wfs.items():
        if not isinstance(w, dict):
            fail("workflows.yaml", f"workflow {wid} не словарь")
            continue
        for f in WF_REQUIRED:
            if f not in w:
                fail("workflows.yaml", f"workflow '{wid}' без поля '{f}'")
        for s in w.get("stages", []):
            if not isinstance(s, dict):
                fail("workflows.yaml", f"workflow '{wid}': стадия не словарь")
                continue
            sid = s.get("id")
            for role_key in ("owner", "writer"):
                v = s.get(role_key)
                if v and v not in agent_ids:
                    fail("workflows.yaml", f"workflow '{wid}' стадия '{sid}': {role_key} '{v}' не найден в реестре агентов")
            # writer/judge: read-only стадия не должна одновременно быть writer
            if s.get("review_mode") == "read-only" and s.get("writer"):
                fail("workflows.yaml", f"workflow '{wid}' стадия '{sid}': read-only стадия не может иметь writer (writer/judge separation)")
        for g in w.get("quality_gates", []):
            if g not in gate_ids:
                fail("workflows.yaml", f"workflow '{wid}': gate '{g}' отсутствует в quality/gates.yaml")


def main():
    if not WORKFLOWS.exists() and not GATES.exists():
        print(f"workflow/gate контракты не найдены в {SYS_DIR.relative_to(REPO_ROOT)} — пропуск.")
        return 0
    agent_ids = known_agent_ids()
    gate_ids = check_gates(agent_ids)
    check_workflows(agent_ids, gate_ids)

    if errors:
        print(f"НАЙДЕНЫ ПРОБЛЕМЫ В WORKFLOW/GATE КОНТРАКТАХ ({len(errors)}):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("OK: workflow-контракты и gate-реестр AI-first системы валидны.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
