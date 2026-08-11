#!/usr/bin/env python3
"""Проверка декларативных presets (presets/*.yaml).

Ловит:
  1. невалидный YAML / отсутствие обязательных полей (id, agents);
  2. агент preset'а не существует в registry/agents.yaml;
  3. агент числится в двух presets одновременно (кроме core);
  4. рассинхрон с registry: у агента в registry указан preset X, а в presets/X.yaml его нет
     (и наоборот — presets полны относительно registry).

Использование:  python3 validation/validate_presets.py
Возврат 0 — чисто, 1 — есть ошибки. Требует pyyaml.
"""

import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
PRESETS_DIR = PKG / "presets"
REGISTRY = PKG / "registry" / "agents.yaml"

errors = []


def fail(where, msg):
    errors.append(f"{where}: {msg}")


def main():
    if not PRESETS_DIR.exists():
        print("presets/ отсутствует — пропуск.")
        return 0
    reg = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))["agents"]
    known = {a["id"] for a in reg}
    reg_preset = {a["id"]: a.get("preset") for a in reg if a.get("layer") == "preset"}

    seen = {}
    presets = {}
    for p in sorted(PRESETS_DIR.glob("*.yaml")):
        try:
            doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            fail(p.name, f"невалидный YAML: {e}")
            continue
        if not isinstance(doc, dict) or "id" not in doc or "agents" not in doc:
            fail(p.name, "нет обязательных полей id/agents")
            continue
        presets[doc["id"]] = set(doc["agents"])
        for a in doc["agents"]:
            if a not in known:
                fail(p.name, f"агент '{a}' не найден в registry/agents.yaml")
            if doc["id"] != "core":
                if a in seen:
                    fail(p.name, f"агент '{a}' уже в preset '{seen[a]}'")
                seen[a] = doc["id"]

    # двусторонняя сверка с registry
    for aid, pname in reg_preset.items():
        if pname and pname in presets and aid not in presets[pname]:
            fail(f"{pname}.yaml", f"агент '{aid}' указан в registry как {pname}, но отсутствует в preset")
    for pname, members in presets.items():
        if pname == "core":
            continue
        for a in members:
            if reg_preset.get(a) != pname:
                fail(f"{pname}.yaml", f"агент '{a}' в preset, но registry говорит preset='{reg_preset.get(a)}'")

    if errors:
        print(f"НАЙДЕНЫ ПРОБЛЕМЫ В PRESETS ({len(errors)}):")
        for e in errors:
            print(f"  - {e}")
        return 1
    total = sum(len(v) for k, v in presets.items() if k != "core")
    print(f"OK: presets валидны ({len(presets)} файлов, {total} preset-агентов сверено с registry).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
