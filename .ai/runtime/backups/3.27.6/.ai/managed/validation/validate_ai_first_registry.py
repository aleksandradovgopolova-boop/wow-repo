#!/usr/bin/env python3
"""Проверка machine-readable реестра AI-first системы (registry/ + manifest/).

Ловит то, что реально ломается при ручных правках реестра/манифеста:
  1. невалидный YAML в registry/agents.yaml, capability-index.yaml, manifest;
  2. рассинхрон реестра с файлами агентов (агент без записи ИЛИ запись без файла);
  3. дубли id в реестре;
  4. отсутствие обязательных полей записи (registry-entity контракт);
  5. несогласованность review_mode с mode во frontmatter агента;
  6. агент из config/agents.yaml, не попавший в реестр;
  7. манифест: нет package_version ИЛИ он расходится с файлом VERSION.

Использование:  python3 02_tools/ci/validate_ai_first_registry.py
Возврат 0 — чисто, 1 — есть ошибки. Требует pyyaml.
"""

import re
import sys
from pathlib import Path

import yaml

PKG_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path.cwd()
SYS_DIR = PKG_ROOT
AGENTS_DIR = SYS_DIR / "agents"
REGISTRY = SYS_DIR / "registry" / "agents.yaml"
CAP_INDEX = SYS_DIR / "registry" / "capability-index.yaml"
MANIFEST = SYS_DIR / "manifest" / "ai-ops-manifest.yaml"
PERMISSION_LEVELS = SYS_DIR / "security" / "permission-levels.yaml"
VERSION_FILE = SYS_DIR / "VERSION"
CONFIG_AGENTS = SYS_DIR / "config" / "agents.yaml"

REQUIRED_FIELDS = ["schema_version", "id", "entity_type", "layer", "file", "purpose", "review_mode"]
VALID_LAYERS = {"core", "preset", "project", "custom", "generated", "parent-only"}
VALID_REVIEW = {"writer", "read-only"}

errors = []


def fail(where, msg):
    errors.append(f"{where}: {msg}")


def load_yaml(path, top_dict=True):
    if not path.exists():
        fail(path.name, "файл отсутствует")
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        fail(path.name, f"невалидный YAML: {exc}")
        return None
    if top_dict and not isinstance(data, dict):
        fail(path.name, "верхний уровень не словарь")
        return None
    return data


def parse_frontmatter(md_path):
    txt = md_path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", txt, re.S)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}


def agent_files():
    return [p for p in AGENTS_DIR.rglob("*.md") if p.name != "README.md"]


def check_registry():
    data = load_yaml(REGISTRY)
    if data is None:
        return
    entries = data.get("agents")
    if not isinstance(entries, list) or not entries:
        fail("agents.yaml", "нет непустого списка agents")
        return

    # id уникальность + обязательные поля
    seen = set()
    reg_by_id = {}
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            fail("agents.yaml", f"запись {i} не словарь")
            continue
        aid = e.get("id")
        for f in REQUIRED_FIELDS:
            if f not in e or e[f] in (None, ""):
                fail("agents.yaml", f"запись '{aid or i}' без обязательного поля '{f}'")
        if e.get("layer") not in VALID_LAYERS:
            fail("agents.yaml", f"запись '{aid}' некорректный layer '{e.get('layer')}'")
        if e.get("review_mode") not in VALID_REVIEW:
            fail("agents.yaml", f"запись '{aid}' некорректный review_mode '{e.get('review_mode')}'")
        if aid in seen:
            fail("agents.yaml", f"дубликат id '{aid}'")
        if aid:
            seen.add(aid)
            reg_by_id[aid] = e

    # соответствие файлам агентов (двусторонне) + review_mode consistency
    file_ids = {}
    for p in agent_files():
        fm = parse_frontmatter(p)
        fid = fm.get("id")
        rel = p.relative_to(SYS_DIR).as_posix()
        if not fid:
            fail(rel, "во frontmatter нет id")
            continue
        file_ids[fid] = (rel, fm)
        if fid not in reg_by_id:
            fail("agents.yaml", f"агент '{fid}' ({rel}) не зарегистрирован в реестре")
        else:
            e = reg_by_id[fid]
            if e.get("file") != rel:
                fail("agents.yaml", f"путь file '{e.get('file')}' != фактического '{rel}' для '{fid}'")
            mode = fm.get("mode")
            expected = "read-only" if mode == "read-only" else "writer"
            if e.get("review_mode") != expected:
                fail("agents.yaml", f"'{fid}': review_mode '{e.get('review_mode')}' не соответствует mode '{mode}' (ожидалось '{expected}')")

    for aid, e in reg_by_id.items():
        if aid not in file_ids:
            fail("agents.yaml", f"запись '{aid}' указывает на несуществующий файл агента")
        else:
            declared = e.get("file")
            actual = file_ids[aid][0]
            if declared and not (SYS_DIR / declared).exists():
                fail("agents.yaml", f"file '{declared}' для '{aid}' не существует")

    # config/agents.yaml ⊆ реестр
    cfg = load_yaml(CONFIG_AGENTS)
    if cfg and isinstance(cfg.get("agent_groups"), dict):
        for group, members in cfg["agent_groups"].items():
            if isinstance(members, list):
                for a in members:
                    if a not in reg_by_id:
                        fail("agents.yaml", f"агент '{a}' из config/agents.yaml ({group}) отсутствует в реестре")

    return len(entries)


def check_manifest():
    data = load_yaml(MANIFEST)
    if data is None:
        return
    ai = data.get("ai_ops")
    if not isinstance(ai, dict) or "package_version" not in ai:
        fail("ai-ops-manifest.yaml", "нет ai_ops.package_version")
        return
    pv = str(ai["package_version"])
    if VERSION_FILE.exists():
        vf = VERSION_FILE.read_text(encoding="utf-8").strip()
        if vf != pv:
            fail("VERSION", f"версия '{vf}' расходится с manifest package_version '{pv}'")
    else:
        fail("VERSION", "файл VERSION отсутствует")


def check_capability_index():
    data = load_yaml(CAP_INDEX)
    if data is None:
        return
    if "status_vocabulary" not in data or "capability_dimensions" not in data:
        fail("capability-index.yaml", "нет status_vocabulary/capability_dimensions")
    if "entries" not in data:
        fail("capability-index.yaml", "нет ключа entries (может быть пустым списком)")


def check_permission_levels():
    if not PERMISSION_LEVELS.exists():
        return
    data = load_yaml(PERMISSION_LEVELS)   # ловит YAML-синтаксис
    if data is None:
        return
    if not isinstance(data.get("levels"), dict) or not data["levels"]:
        fail("permission-levels.yaml", "нет непустого levels")


def main():
    if not REGISTRY.exists() and not MANIFEST.exists():
        print(f"реестр/манифест не найдены в {SYS_DIR.relative_to(REPO_ROOT)} — пропуск.")
        return 0
    n = check_registry()
    check_manifest()
    check_capability_index()
    check_permission_levels()

    if errors:
        print(f"НАЙДЕНЫ ПРОБЛЕМЫ В РЕЕСТРЕ/МАНИФЕСТЕ ({len(errors)}):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"OK: реестр AI-first системы валиден ({n} агентов сверено с файлами и манифестом).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
