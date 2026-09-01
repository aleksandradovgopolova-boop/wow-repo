#!/usr/bin/env python3
"""Валидатор Feature Blueprint (schemas/feature-blueprint.schema.json, Ф1 roadmap).

Blueprint — паспорт функции: features/<id>/blueprint.yaml со ссылками на артефакты
жизненного цикла. Ловит то, что реально ломается:
  1. невалидный YAML / не тот kind / нет обязательных полей;
  2. current_stage или ключ artifacts вне словаря стадий;
  3. стадия не позже current_stage без единого артефакта;
  4. артефакт стадии не позже current_stage: файла нет, а status не declined;
  5. status=declined без declined_reason (отказ должен быть явным и обоснованным).

Стадии (по порядку): discovery, definition, ux, architecture, delivery, analytics,
documentation, release, monitoring, retrospective.

Использование:  python3 validation/validate_feature_blueprint.py <feature-dir> [...]
                python3 validation/validate_feature_blueprint.py --selftest
Возврат 0 — чисто, 1 — есть ошибки. Требует pyyaml.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import yaml

STAGES = ["discovery", "definition", "ux", "architecture", "delivery",
          "analytics", "documentation", "release", "monitoring", "adoption", "retrospective"]
STATUSES = {"planned", "draft", "done", "declined"}
FEATURE_STATUSES = {"planned", "in-progress", "released", "retired"}
# Профили (v2.3): скоуп стадий объявляется явно в feature.profile — это не молчаливый пропуск.
PROFILES = {
    "full": STAGES,
    "lean": ["discovery", "definition", "delivery", "analytics", "retrospective"],
}


def validate_dir(feature_dir: Path):
    errors = []

    def fail(msg):
        errors.append(f"{feature_dir.name}: {msg}")

    bp_path = feature_dir / "blueprint.yaml"
    if not bp_path.exists():
        fail("нет blueprint.yaml")
        return errors
    try:
        bp = yaml.safe_load(bp_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        fail(f"невалидный YAML: {exc}")
        return errors
    if not isinstance(bp, dict):
        fail("верхний уровень не словарь")
        return errors
    if bp.get("kind") != "feature-blueprint":
        fail(f"kind '{bp.get('kind')}' != feature-blueprint")
    if bp.get("schema_version") != 1:
        fail(f"schema_version '{bp.get('schema_version')}' != 1")

    feature = bp.get("feature")
    if not isinstance(feature, dict):
        fail("нет секции feature")
        return errors
    for f in ("id", "name", "status", "current_stage"):
        if not feature.get(f):
            fail(f"feature без поля '{f}'")
    if feature.get("status") and feature["status"] not in FEATURE_STATUSES:
        fail(f"feature.status '{feature['status']}' вне {sorted(FEATURE_STATUSES)}")
    profile = feature.get("profile", "full")
    if profile not in PROFILES:
        fail(f"feature.profile '{profile}' вне {sorted(PROFILES)}")
        return errors
    stage = feature.get("current_stage")
    if stage not in STAGES:
        fail(f"current_stage '{stage}' вне словаря стадий")
        return errors
    if profile == "lean" and stage not in PROFILES["lean"]:
        fail(f"current_stage '{stage}' вне lean-профиля {PROFILES['lean']}")
        return errors
    reached = set(STAGES[:STAGES.index(stage) + 1])
    reached_required = reached & set(PROFILES[profile])

    artifacts = bp.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        fail("нет непустого artifacts")
        return errors

    for st, entries in artifacts.items():
        if st not in STAGES:
            fail(f"artifacts: стадия '{st}' вне словаря стадий")
            continue
        if not isinstance(entries, list):
            fail(f"artifacts.{st}: не список")
            continue
        for e in entries:
            if not isinstance(e, dict) or not e.get("path"):
                fail(f"artifacts.{st}: запись без path")
                continue
            status = e.get("status", "planned")
            if status not in STATUSES:
                fail(f"artifacts.{st}.{e['path']}: status '{status}' вне {sorted(STATUSES)}")
            if status == "declined" and not e.get("declined_reason"):
                fail(f"artifacts.{st}.{e['path']}: declined без declined_reason")
            if st in reached and status != "declined":
                if not (feature_dir / e["path"]).exists():
                    fail(f"artifacts.{st}: файл '{e['path']}' не существует, "
                         f"а стадия '{st}' уже достигнута (либо пометьте declined с причиной)")

    for st in reached_required:
        entries = artifacts.get(st)
        if not entries:
            fail(f"стадия '{st}' достигнута (current_stage={stage}, profile={profile}), "
                 "но артефактов для неё нет")

    # finding обкатки 6: released ⇒ должно быть доказательство поставки. Кит не видит код
    # в произвольном репо, но честный прокси — released при НУЛЕ done-артефактов = дрейф
    # «reality/blueprint разошлись» (фича помечена выпущенной, а сделанного нет).
    # v3.27.4 WP5: feature.status=released требует SHA-verified DeliveryReceipt, а не просто done-артефакт.
    # Done-артефакт может быть problem statement из discovery — это не доказательство поставки.
    if feature.get("status") == "released":
        any_done = any(e.get("status") == "done"
                       for entries in artifacts.values() if isinstance(entries, list)
                       for e in entries if isinstance(e, dict))
        if not any_done:
            fail("feature.status=released, но ни один артефакт не 'done' — нет доказательства "
                 "поставки (reality/blueprint дрейф; пометьте реальные артефакты done или снимите released)")

        # v3.27.4 WP5: проверяем наличие SHA-verified DeliveryReceipt
        # DeliveryReceipt находится в features/<feature_id>/delivery-receipt.yaml
        # или в .ai/runtime/delivery/<workitem_id>/receipt.yaml
        receipt_paths = [
            feature_dir / "delivery-receipt.yaml",
            feature_dir.parent.parent / ".ai" / "runtime" / "delivery" / feature.get("id", "") / "receipt.yaml",
        ]
        receipt_found = False
        for rp in receipt_paths:
            if rp.exists():
                try:
                    receipt = yaml.safe_load(rp.read_text(encoding="utf-8"))
                    if receipt and receipt.get("kind") == "DeliveryReceipt" and receipt.get("sha_verified") is True:
                        receipt_found = True
                        break
                except Exception:
                    pass
        if not receipt_found:
            fail("feature.status=released, но нет SHA-verified DeliveryReceipt — "
                 "done-артефакт недостаточно для доказательства поставки. "
                 "Требуется DeliveryReceipt с sha_verified=true (PR смержён, SHA совпадает с remote).")

    return errors


def make_demo(root: Path, *, break_file=False, break_stage=False):
    """Собрать во временной папке валидный (или намеренно сломанный) blueprint."""
    fdir = root / "demo-feature"
    (fdir / "discovery").mkdir(parents=True)
    (fdir / "prd").mkdir()
    (fdir / "discovery" / "problem-statement.md").write_text("# Problem\n", encoding="utf-8")
    if not break_file:
        (fdir / "prd" / "prd.md").write_text("# PRD\n", encoding="utf-8")
    bp = {
        "schema_version": 1, "kind": "feature-blueprint",
        "feature": {"id": "demo-feature", "name": "Demo", "status": "in-progress",
                    "current_stage": "bad-stage" if break_stage else "definition"},
        "artifacts": {
            "discovery": [{"path": "discovery/problem-statement.md", "status": "done"}],
            "definition": [{"path": "prd/prd.md", "status": "draft"}],
            "analytics": [{"path": "analytics/tracking-plan.md", "status": "planned"}],
        },
    }
    (fdir / "blueprint.yaml").write_text(yaml.safe_dump(bp, allow_unicode=True), encoding="utf-8")
    return fdir


def main(argv):
    if not argv:
        print("использование: validate_feature_blueprint.py <feature-dir> [...] | --selftest")
        return 1
    all_errors = []
    for d in argv:
        all_errors += validate_dir(Path(d).resolve())
    if all_errors:
        print(f"НАЙДЕНЫ ПРОБЛЕМЫ В FEATURE BLUEPRINT ({len(all_errors)}):")
        for e in all_errors:
            print(f"  - {e}")
        return 1
    print(f"OK: feature blueprint валиден ({len(argv)} функций проверено).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
