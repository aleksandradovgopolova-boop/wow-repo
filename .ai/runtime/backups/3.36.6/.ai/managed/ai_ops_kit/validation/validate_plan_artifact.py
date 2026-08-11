#!/usr/bin/env python3
"""Validate plan artifact (v2.86 Product Authoring).

Гейт `plan_readiness` (ENGINEERING/PRODUCT) требует evidence: work_packages + dependencies +
write_scope. v2.86: writer пишет план-артефакт в worktree, ЭТОТ детерминированный валидатор
подтверждает его форму -> legitimate evidence (структура, не «качество плана»).
ЧЕСТНО (v2.87): проверяется только ФОРМА. write_scope — декларация плана; движок НЕ сужает по
ней запись автоматически (это не enforcement, а поле артефакта). Качество плана судит человек —
в петле --review детерминированные гейты (requirements/plan_readiness) НЕ ревьюятся.

Форма (YAML):
  schema_version: 1
  kind: plan-artifact
  workitem_id: <slug>
  work_packages:
    - id: WP1
      summary: "добавить фильтр в контроллер каталога"
      depends_on: []            # зависимости (может быть пусто, но поле обязательно)
  write_scope: ["src/catalog/"] # непустой список путей, куда план разрешает писать

check() -> список ошибок. provided_evidence() -> закрытые required_evidence-ключи гейта.

Использование:
  validate_plan_artifact.py <artifact.yaml>
  validate_plan_artifact.py --selftest
Возврат 0 — ок, 1 — ошибки.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

REQUIRED_EVIDENCE = ["work_packages", "dependencies", "write_scope"]


def check(data):
    errors = []
    if not isinstance(data, dict) or data.get("kind") != "plan-artifact":
        errors.append("kind должен быть 'plan-artifact'")
        data = data if isinstance(data, dict) else {}
    if data.get("schema_version") is None:
        errors.append("нет schema_version")
    wps = data.get("work_packages")
    if not isinstance(wps, list) or not wps:
        errors.append("work_packages должен быть непустым списком")
        wps = []
    seen = set()
    for i, wp in enumerate(wps):
        if not isinstance(wp, dict):
            errors.append(f"work_package[{i}] должен быть объектом"); continue
        wid = wp.get("id", f"#{i}")
        _wid = wp.get("id")
        if not _wid:
            errors.append(f"work_package[{i}]: нет id")
        elif not isinstance(_wid, str):
            # author вернул id не-строкой (напр. dict) -> ЧЕСТНАЯ ошибка валидации, не краш set-membership
            errors.append(f"work_package[{i}]: id должен быть строкой, получено {type(_wid).__name__}")
        elif _wid in seen:
            errors.append(f"дублирующийся id work_package: {_wid}")
        else:
            seen.add(_wid)
        if not (isinstance(wp.get("summary"), str) and wp["summary"].strip()):
            errors.append(f"{wid}: пустой/отсутствующий summary")
        # dependencies: поле depends_on обязано присутствовать и быть списком (может быть пустым);
        # каждая зависимость должна ссылаться на существующий work_package.
        dep = wp.get("depends_on")
        if not isinstance(dep, list):
            errors.append(f"{wid}: depends_on должен быть списком (может быть пустым)")
    # проверка ссылочной целостности зависимостей (после сбора всех id) — только строковые id хешируемы
    ids = {wp.get("id") for wp in wps if isinstance(wp, dict) and isinstance(wp.get("id"), str)}
    for wp in wps:
        if isinstance(wp, dict):
            for d in wp.get("depends_on", []) or []:
                if not isinstance(d, str):
                    # author вернул зависимость не-строкой (напр. dict) -> ошибка валидации, не краш `in set`
                    errors.append(f"{wp.get('id')}: элемент depends_on должен быть строкой-id, получено {type(d).__name__}")
                elif d not in ids:
                    errors.append(f"{wp.get('id')}: depends_on ссылается на несуществующий work_package '{d}'")
    ws = data.get("write_scope")
    if not (isinstance(ws, list) and ws and all(isinstance(p, str) and p.strip() for p in ws)):
        errors.append("write_scope должен быть непустым списком путей")
    return errors


def provided_evidence(data):
    return list(REQUIRED_EVIDENCE) if not check(data) else []


def load(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def main(argv):
    if not argv:
        print(__doc__); return 1
    errs = check(load(argv[0]))
    if errs:
        print("PLAN-ARTIFACT: ошибки:")
        for e in errs:
            print(f"  - {e}")
        return 1
    print("PLAN-ARTIFACT-OK: форма подтверждена (work_packages + dependencies + write_scope).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
