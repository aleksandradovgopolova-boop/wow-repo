#!/usr/bin/env python3
"""Проверка машиночитаемых конфигов AI-first системы (02_tools/ai-first-system/config).

Ловит то, что реально ломается при ручных правках конфигов:
  1. невалидный YAML;
  2. отсутствие обязательного `version`;
  3. рассинхрон `agents.yaml` с файлами агентов (агент в реестре без файла роли);
  4. структурные ошибки в model-routing / quality-gates / tool-permissions / protected-paths.

Использование:  python3 02_tools/ci/validate_ai_first_config.py
Возврат 0 — чисто, 1 — есть ошибки. Требует pyyaml.
"""

import sys
from pathlib import Path

import yaml

PKG_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path.cwd()
SYS_DIR = PKG_ROOT
CONFIG_DIR = SYS_DIR / "config"
AGENTS_DIR = SYS_DIR / "agents"

errors = []


def fail(where, msg):
    errors.append(f"{where}: {msg}")


def load(name):
    p = CONFIG_DIR / name
    if not p.exists():
        fail(name, "файл конфига отсутствует")
        return None
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        fail(name, f"невалидный YAML: {exc}")
        return None
    if not isinstance(data, dict):
        fail(name, "верхний уровень не словарь")
        return None
    if "version" not in data:
        fail(name, "нет поля version")
    return data


def agent_file_stems():
    return {p.stem for p in AGENTS_DIR.rglob("*.md") if p.name != "README.md"}


def check_agents(data, stems):
    groups = data.get("agent_groups")
    if not isinstance(groups, dict):
        fail("agents.yaml", "нет agent_groups (словарь)")
        return
    for group, members in groups.items():
        if not isinstance(members, list):
            fail("agents.yaml", f"группа {group} не список")
            continue
        for a in members:
            if a not in stems:
                fail("agents.yaml", f"агент '{a}' (группа {group}) не имеет файла роли в agents/")


def check_model_routing(data):
    routes = data.get("routes")
    if not isinstance(routes, dict) or not routes:
        fail("model-routing.yaml", "нет непустого routes")
        return
    for name, body in routes.items():
        if not isinstance(body, dict) or "tasks" not in body:
            fail("model-routing.yaml", f"маршрут {name} без tasks")


def check_quality_gates(data):
    gates = data.get("gates")
    if not isinstance(gates, dict) or not gates:
        fail("quality-gates.yaml", "нет непустого gates")


def check_tool_permissions(data):
    modes = data.get("modes")
    if not isinstance(modes, dict) or not modes:
        fail("tool-permissions.yaml", "нет непустого modes")
        return
    for name, body in modes.items():
        if not isinstance(body, dict) or "allowed" not in body or "denied" not in body:
            fail("tool-permissions.yaml", f"режим {name} без allowed/denied")


def check_protected_paths(data):
    pp = data.get("protected_paths")
    if not isinstance(pp, list) or not pp:
        fail("protected-paths.yaml", "нет непустого protected_paths")
        return
    for i, item in enumerate(pp):
        if not isinstance(item, dict) or "path" not in item or "approval" not in item:
            fail("protected-paths.yaml", f"элемент {i} без path/approval")


def main():
    if not CONFIG_DIR.exists():
        print(f"config-каталог не найден: {CONFIG_DIR.relative_to(REPO_ROOT)} — пропуск.")
        return 0
    stems = agent_file_stems()
    agents = load("agents.yaml")
    if agents:
        check_agents(agents, stems)
    for name, checker in [
        ("model-routing.yaml", check_model_routing),
        ("quality-gates.yaml", check_quality_gates),
        ("tool-permissions.yaml", check_tool_permissions),
        ("protected-paths.yaml", check_protected_paths),
    ]:
        data = load(name)
        if data:
            checker(data)

    if errors:
        print(f"НАЙДЕНЫ ПРОБЛЕМЫ В КОНФИГАХ ({len(errors)}):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"OK: конфиги AI-first системы валидны ({len(stems)} файлов агентов сверено).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
