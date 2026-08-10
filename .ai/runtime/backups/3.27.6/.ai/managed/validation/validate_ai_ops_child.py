#!/usr/bin/env python3
"""Проверка установки AI-first системы в child-репозиторий (Фаза 8).

Проверяет живую установку в корне репозитория (первый existing-repo pilot):
  1. `.ai-ops.yaml` существует, валиден, kind корректен, есть parent/presets/adapters/providers;
  2. credentials_ref — только secret-reference (env:/secret:), литеральные ключи запрещены;
  3. версии согласованы: .ai-ops.yaml parent.installed_version == .provenance.json
     installed_version == manifest.package_version (managed-копия) == VERSION пакета;
  4. зоны .ai/{managed,project,custom,generated,runtime} существуют;
  5. .provenance.json и .checksums.json существуют; целостность managed-слоя
     подтверждается (переиспользует ai_managed_checksums.verify);
  6. providers из .ai-ops.yaml известны реестру registry/providers.yaml;
  7. blocking gates из .ai-ops.yaml известны quality/gates.yaml.

Использование:  python3 02_tools/ci/validate_ai_ops_child.py
Возврат 0 — чисто, 1 — есть ошибки; пропуск, если .ai-ops.yaml нет (репо не child).
Требует pyyaml.
"""

import json
import re
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
REPO_ROOT = Path.cwd()
CI = PKG / "validation"
sys.path.insert(0, str(CI))

CHILD_CONFIG = REPO_ROOT / ".ai-ops.yaml"
AI_DIR = REPO_ROOT / ".ai"
MANAGED = AI_DIR / "managed"
# PKG определён выше (корень пакета)

CRED_RE = re.compile(r"^(env:|secret:)")

errors = []


def fail(where, msg):
    errors.append(f"{where}: {msg}")


def main():
    if not CHILD_CONFIG.exists():
        print("нет .ai-ops.yaml — репозиторий не является child; пропуск.")
        return 0

    # 1-2. конфиг
    try:
        cfg = yaml.safe_load(CHILD_CONFIG.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        print(f".ai-ops.yaml: невалидный YAML: {e}")
        return 1
    if cfg.get("kind") != "ai-ops-child-config":
        fail(".ai-ops.yaml", f"kind '{cfg.get('kind')}' != ai-ops-child-config")
    for block in ("parent", "presets", "adapters", "providers"):
        if block not in cfg:
            fail(".ai-ops.yaml", f"нет блока '{block}'")

    for p in (cfg.get("providers", {}) or {}).get("configured", []) or []:
        for key in ("credentials_ref", "scope_ref", "base_url_ref"):
            v = p.get(key)
            if v and not CRED_RE.match(str(v)):
                fail(".ai-ops.yaml", f"provider '{p.get('id')}': {key} '{v}' не secret-reference (env:/secret:)")

    # 3. версии
    inst = str((cfg.get("parent") or {}).get("installed_version", ""))
    prov_file = MANAGED / ".provenance.json"
    if prov_file.exists():
        try:
            prov = json.loads(prov_file.read_text(encoding="utf-8"))
            if str(prov.get("installed_version")) != inst:
                fail(".provenance.json", f"installed_version '{prov.get('installed_version')}' != .ai-ops.yaml '{inst}'")
        except json.JSONDecodeError as e:
            fail(".provenance.json", f"невалидный JSON: {e}")
    else:
        fail(".ai/managed", "нет .provenance.json")

    man_file = MANAGED / "manifest" / "ai-ops-manifest.yaml"
    if man_file.exists():
        man = yaml.safe_load(man_file.read_text(encoding="utf-8"))
        pv = str((man.get("ai_ops") or {}).get("package_version", ""))
        if pv != inst:
            fail("managed/manifest", f"package_version '{pv}' != installed_version '{inst}'")
    else:
        fail(".ai/managed", "нет manifest/ai-ops-manifest.yaml")

    ver_file = PKG / "VERSION"
    if ver_file.exists():
        pkg_v = ver_file.read_text(encoding="utf-8").strip()
        if pkg_v != inst:
            fail("VERSION", f"версия пакета '{pkg_v}' != установленной '{inst}' (обнови managed-слой)")

    # 4. зоны
    for zone in ("managed", "project", "custom", "generated", "runtime"):
        if not (AI_DIR / zone).exists():
            fail(".ai", f"нет зоны {zone}/")

    # 5. целостность managed
    if not (MANAGED / ".checksums.json").exists():
        fail(".ai/managed", "нет .checksums.json")
    else:
        try:
            import ai_managed_checksums as amc
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = amc.verify(MANAGED)
            if rc != 0:
                fail(".ai/managed", "drift: managed-слой изменён вручную (см. ai_managed_checksums verify .ai/managed)")
        except Exception as e:
            fail(".ai/managed", f"не удалось проверить целостность: {e}")

    # 6. провайдеры известны реестру
    prov_reg_file = PKG / "registry" / "providers.yaml"
    if prov_reg_file.exists():
        known = set(yaml.safe_load(prov_reg_file.read_text(encoding="utf-8")).get("providers", {}))
        for p in (cfg.get("providers", {}) or {}).get("configured", []) or []:
            if p.get("id") not in known:
                fail(".ai-ops.yaml", f"provider '{p.get('id')}' не найден в registry/providers.yaml")
        for p in (cfg.get("providers", {}) or {}).get("fallback_chain", []) or []:
            if p not in known:
                fail(".ai-ops.yaml", f"fallback provider '{p}' не найден в реестре")

    # 7. blocking gates известны
    gates_file = PKG / "quality" / "gates.yaml"
    if gates_file.exists():
        known_gates = set(yaml.safe_load(gates_file.read_text(encoding="utf-8")).get("gates", {}))
        for g in (cfg.get("quality_gates", {}) or {}).get("blocking", []) or []:
            if g not in known_gates:
                fail(".ai-ops.yaml", f"blocking gate '{g}' не найден в quality/gates.yaml")

    if errors:
        print(f"НАЙДЕНЫ ПРОБЛЕМЫ В CHILD-УСТАНОВКЕ ({len(errors)}):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"OK: child-установка валидна (версия {inst}, managed-слой целостен, providers/gates согласованы).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
