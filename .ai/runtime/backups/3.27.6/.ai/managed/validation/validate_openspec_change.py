#!/usr/bin/env python3
"""Проверка OpenSpec change-пакетов + сторож параллельного слияния (Фаза 5).

OpenSpec — spec-протокол (specs = источник истины, changes = дельты). Детерминированные
validate/archive/sync выполняет сам OpenSpec CLI. ДОПОЛНИТЕЛЬНО этот валидатор:

  1. структурно проверяет change-пакеты (наш fallback + гейт, независим от бинаря openspec):
     - у каждого un-archived change есть proposal.md и tasks.md;
     - каждый delta-spec содержит секцию ADDED/MODIFIED/REMOVED/RENAMED Requirements;
     - каждое требование в дельте имеет SHALL/MUST в теле и хотя бы один "#### Scenario:";
  2. ЗАКРЫВАЕТ ИЗВЕСТНЫЙ БАГ OpenSpec (parallel-merge data-loss): если два разных
     un-archived change трогают ОДНО И ТО ЖЕ требование (capability + имя требования) —
     это блокируется (иначе archive второго молча затирает первое).

Формат корня: <root>/openspec/{specs,changes}. root по умолчанию — пример-фикстура.
Требует pyyaml не нужен — только стандартная библиотека.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path.cwd()
DEFAULT_ROOT = REPO_ROOT

SECTION_RE = re.compile(r"^##\s+(ADDED|MODIFIED|REMOVED|RENAMED)\s+Requirements\s*$", re.M | re.I)
REQ_RE = re.compile(r"^###\s+Requirement:\s+(.+?)\s*$", re.M)
SCEN_RE = re.compile(r"^####\s+Scenario:", re.M)
NORMATIVE_RE = re.compile(r"\b(SHALL|MUST)\b")

errors = []


def fail(where, msg):
    errors.append(f"{where}: {msg}")


def split_requirement_blocks(text):
    """Возвращает список (name, body) для каждого '### Requirement:' блока."""
    blocks = []
    matches = list(REQ_RE.finditer(text))
    for i, m in enumerate(matches):
        name = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks.append((name, text[start:end]))
    return blocks


def check_change(change_dir):
    rel = change_dir.name
    if not (change_dir / "proposal.md").exists():
        fail(rel, "нет proposal.md")
    if not (change_dir / "tasks.md").exists():
        fail(rel, "нет tasks.md")

    touched = []  # (capability, requirement_name)
    specs_dir = change_dir / "specs"
    if specs_dir.exists():
        for spec in specs_dir.rglob("spec.md"):
            capability = spec.parent.name
            text = spec.read_text(encoding="utf-8")
            if not SECTION_RE.search(text):
                fail(f"{rel}/specs/{capability}", "нет секции ADDED/MODIFIED/REMOVED/RENAMED Requirements")
            for name, body in split_requirement_blocks(text):
                touched.append((capability, name))
                # REMOVED/RENAMED допускают отсутствие сценария; для ADDED/MODIFIED — обязателен
                is_removed = re.search(r"REMOVED Requirements[\s\S]*?" + re.escape(name), text, re.I)
                if not SCEN_RE.search(body) and not is_removed:
                    fail(f"{rel}/specs/{capability}", f"требование '{name}' без '#### Scenario:'")
                if not NORMATIVE_RE.search(body) and not is_removed:
                    fail(f"{rel}/specs/{capability}", f"требование '{name}' без SHALL/MUST в теле")
    return touched


def main(argv):
    root = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_ROOT
    os_dir = root / "openspec"
    if not os_dir.exists():
        print(f"OpenSpec-корень не найден: {os_dir} — пропуск (OpenSpec выключен).")
        return 0

    changes_dir = os_dir / "changes"
    active = []
    if changes_dir.exists():
        for d in sorted(changes_dir.iterdir()):
            if d.is_dir() and d.name != "archive":
                active.append(d)

    # 1. структурная проверка + сбор затронутых требований
    touched_by_change = {}
    for ch in active:
        touched_by_change[ch.name] = check_change(ch)

    # 2. parallel-merge guard: одно требование трогают >1 un-archived change
    seen = {}
    for cname, reqs in touched_by_change.items():
        for key in reqs:
            seen.setdefault(key, []).append(cname)
    for key, changes in seen.items():
        if len(set(changes)) > 1:
            cap, req = key
            fail("parallel-merge-guard",
                 f"требование '{req}' (capability '{cap}') трогают несколько un-archived changes: "
                 f"{sorted(set(changes))} — риск потери данных при archive. Сначала sync, затем по одному.")

    if errors:
        print(f"НАЙДЕНЫ ПРОБЛЕМЫ В OPENSPEC CHANGE-ПАКЕТАХ ({len(errors)}):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"OK: OpenSpec change-пакеты валидны ({len(active)} active change, parallel-merge guard чист).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
