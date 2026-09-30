#!/usr/bin/env python3
"""Gate runners — детерминированные раннеры валидаторов (сателлит gate_executor).

Здесь живут гейт-проверки, которые кит РЕАЛЬНО исполняет офлайн (`deterministic_run` и её
под-раннеры) плюс well-formedness `evidence_schema` (`validate_evidence_schemas`). Одностороннее
ребро вниз к `gate_evidence` (за `load_gates`); фасад `gate_executor` не импортируется — обратного
ребра нет.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from ai_ops_kit.gates.gate_evidence import load_gates

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
# v3.34: валидаторы переехали в пакет. Путь один на модуль — чтобы следующий перенос правился
# в одном месте, а не в каждом вызове подпроцесса.
VALIDATION = PKG / "ai_ops_kit" / "validation"


def _run_validator(*args) -> bool:
    """Запустить package-валидатор офлайн; True при rc==0."""
    r = subprocess.run([sys.executable, str(VALIDATION / args[0]), *args[1:]],
                       capture_output=True, text=True)
    return r.returncode == 0


def deterministic_run(validator):
    """(status, checks, provided) для валидаторов, которые РЕАЛЬНО запускаемы офлайн;
    None — если валидатор символический (напр. validate-intake) и требует внешнего evidence.
    Так gate executor не выдумывает вердикт: он либо честно исполняет проверку, либо ждёт evidence."""
    if validator == "validate-references + validate-claims":
        refs, claims = _run_validator("validate_references.py"), _run_validator("validate_claims.py")
        checks = [{"id": "references_resolve", "status": "pass" if refs else "fail"},
                  {"id": "claims_hold", "status": "pass" if claims else "fail"}]
        status = "pass" if refs and claims else "fail"
        return status, checks, [c["id"] for c in checks if c["status"] == "pass"]
    if validator == "validate-freshness":
        return _freshness_run()
    if validator == "validate-deploy-readiness":
        return _deploy_readiness_run()
    if validator == "validate-documentation-updated":
        return _documentation_updated_run()
    if validator == "validate-feature-decisions":
        return _feature_decisions_run()
    return None


def _feature_decisions_run(base=None):
    """#541: гейт `feature_decision_quality` — каждое решение kind=feature-decision несёт ВАЛИДНЫЙ
    feature_target (baseline/target/guardrails). Проверяет МЕХАНИЗМ, а не декларация: обходит каталог
    решений репозитория (.ai/project/decisions) и краснит фичу, объявленную целью без измеримого
    обязательства, называя, чего не хватает.

    Проверяющая логика лежит НИЖЕ по слоям — в `ai_ops_kit.checks.feature_decision` (primitives):
    `gates` (ядро) не вправе импортировать `intelligence`, поэтому зовём вниз. Отсутствие каталога
    решений — не ошибка (фич-решений просто нет) -> pass. Наличие неполного feature-decision -> fail
    с блокером на каждый дефект."""
    from ai_ops_kit.checks.feature_decision import gate_feature_decisions
    b = Path(base) if base else Path.cwd()
    errors = gate_feature_decisions(b / ".ai" / "project" / "decisions")
    if errors:
        checks = [{"id": f"feature_target_incomplete:{e}", "status": "fail"} for e in errors]
        return "fail", checks, []
    return "pass", [{"id": "feature_targets_valid", "status": "pass"}], ["feature_target_declared"]


def _documentation_updated_run(base=None):
    """v3.37 (C3): гейт `documentation_updated` переведён из самозаявления в машинный.

    Оба его доказательства — факты о дифе (документация тронута; запись для CHANGELOG добавлена),
    и спрашивать о них стадию, которая работу и сделала, было лишним. Непроверяемое даёт warn с
    причиной, а не pass: гейт advisory, и «нечем проверить» не равно «в порядке»."""
    from ai_ops_kit.gates import documentation_evidence
    rep = documentation_evidence.assess(Path(base) if base else Path.cwd())
    ev = documentation_evidence.gate_evidence(rep)
    provided = ev.get("provided", [])
    return ev["status"], ev.get("checks", []), provided


def _deploy_readiness_run(base=None):
    """v3.20.0 EngOps срез 2: детерминированная зрелость поставки текущего репозитория.

    Сюда попадаем ТОЛЬКО когда гейт применим (required_when уже отфильтровал неприменимость в
    evaluate_gate), поэтому `configured`/`absent` здесь — честный fail: изменение поставки заявлено,
    а исполняемого пути нет. Недоступность инструмента -> warn, а НЕ pass (бездоказательного pass
    не существует)."""
    b = Path(base) if base else Path.cwd()
    try:
        from ai_ops_kit.gates import deploy_readiness
    except Exception as e:  # noqa: BLE001 — нет инструмента -> warn с причиной, не тихий pass
        return "warn", [{"id": f"deploy_readiness_tool_unavailable:{e}", "status": "warn"}], []
    rep = deploy_readiness.assess(b)
    status, note = deploy_readiness.gate_status(rep["deploy_maturity"])
    checks = [{"id": f"deploy_maturity:{rep['deploy_maturity']}", "status": status},
              {"id": "rollback_declared", "status": "pass" if rep["rollback_declared"] else "fail"}]
    for f in rep["findings"]:
        if f["rule"] in ("detected_not_declared", "no_rollback_declared", "records_without_path"):
            checks.append({"id": f"{f['rule']}:{f.get('environment', '')}".rstrip(":"),
                           "status": "fail"})
    if any(c["status"] == "fail" for c in checks):
        status = "fail"
    provided = ([c["id"].split(":")[0] for c in checks if c["status"] == "pass"]
                + (["deploy_maturity"] if status == "pass" else []))
    return status, checks, sorted(set(provided))


def _freshness_run(base=None):
    """v3.12.0 Startup Context Budget: freshness-гейт проверяет контекст РЕПОЗИТОРИЯ
    (.ai/project/context, + .ai/custom/context), а НЕ --selftest самого кита (тот остаётся отдельной
    проверкой в CI кита). Протухший volatile / нет reviewed_at у размеченного документа -> WARN с
    именами файлов и сроками (имена вшиты в id проверки, чтобы попасть в machine-readable отчёт).
    Отсутствие контекста репозитория -> WARN (пробел виден, не молчаливый pass)."""
    b = Path(base) if base else Path.cwd()
    roots = [b / rel for rel in (".ai/project/context", ".ai/custom/context")]
    roots = [r for r in roots if r.is_dir()]
    if not roots:
        checks = [{"id": "repo_context_present:.ai/project/context отсутствует", "status": "warn"}]
        return "warn", checks, []
    checks = []
    for r in roots:
        proc = subprocess.run([sys.executable, str(VALIDATION / "validate_freshness.py"),
                               str(r), "--json"], capture_output=True, text=True)
        try:
            rep = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            continue
        for item in rep.get("results", []):
            if item["status"] == "stale":
                checks.append({"id": f"stale:{item['path']} — {item['detail']}", "status": "warn"})
            elif item["status"] == "no-review-date":
                checks.append({"id": f"no_review_date:{item['path']} — {item['detail']}", "status": "warn"})
    if not checks:
        checks = [{"id": "no_stale_volatile_docs", "status": "pass"}]
    status = "warn" if any(c["status"] == "warn" for c in checks) else "pass"
    return status, checks, [c["id"] for c in checks if c["status"] == "pass"]


_EVIDENCE_TYPES = {"string", "integer", "number", "boolean", "git_sha", "path"}


def validate_evidence_schemas(gates=None) -> list:
    """v2.33: well-formedness gate.evidence_schema — типы полей из словаря, структура вложена."""
    gates = gates or load_gates()
    errs = []
    for gid, g in gates.items():
        es = g.get("evidence_schema")
        if es is None:
            continue
        if not isinstance(es, dict):
            errs.append(f"{gid}.evidence_schema должен быть mapping"); continue
        for group, fields in es.items():
            if not isinstance(fields, dict):
                errs.append(f"{gid}.evidence_schema.{group} должен быть mapping поле->тип"); continue
            for fname, ftype in fields.items():
                if ftype not in _EVIDENCE_TYPES:
                    errs.append(f"{gid}.evidence_schema.{group}.{fname}: тип '{ftype}' вне {sorted(_EVIDENCE_TYPES)}")
    return errs
