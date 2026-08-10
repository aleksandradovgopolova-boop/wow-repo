#!/usr/bin/env python3
"""Gate executor — единый исполнитель quality gates (замыкание контура, v2.15).

Раньше sequential-оркестратор проводил стадии, но НЕ читал quality_gates контракта
и ставил workflow `done` при любом ответе ролей — гейты существовали только на бумаге.
Этот модуль резолвит объявленные контрактом гейты, классифицирует КАЖДЫЙ по способу
проверки и честно считает результат, БЛОКИРУЯ переход по workflow, если блокирующий
гейт не выполнен.

Три типа проверок (принцип «writer ≠ judge», честные декларации):
  - deterministic  — гейт с полем `validator` (детерминированный CLI/чек);
  - ai-review      — read-only reviewer c checklist (заключение судьи-роли);
  - human-approval — гейт с `human_approval` (ручное одобрение, в т.ч. условное).

Результат каждого гейта — machine-readable по schemas/gate-result.schema.json
(status ∈ pass|warn|fail; невыполненный блокирующий гейт → fail с blocker, а не
молчаливый pass). Evidence (заключения reviewer'ов / прогоны валидаторов) подаётся
снаружи как {gate_id: {status, checks, evidence, blockers, override}}: executor не
выдумывает вердикты, которых не было.

Использование:
  gate_executor.py <WORKFLOW> [evidence.json]   — оценить гейты (JSON-отчёт)
  gate_executor.py --selftest                    — офлайн-проверки

Требует pyyaml.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
# v3.34: валидаторы переехали в пакет. Путь один на модуль — чтобы следующий перенос правился
# в одном месте, а не в каждом вызове подпроцесса.
VALIDATION = PKG / "ai_ops_kit" / "validation"
_EVIDENCE_KEYS = {"status", "provided", "checks", "evidence", "warnings", "blockers", "override"}


def validate_evidence(evidence) -> list:
    """Мини-валидация формы evidence по schemas/gate-evidence.schema.json (stdlib, без jsonschema).
    Возвращает список ошибок (пустой = валидно)."""
    errs = []
    if not isinstance(evidence, dict):
        return ["evidence: верхний уровень должен быть объектом {gate_id: {...}}"]
    for gid, e in evidence.items():
        if not isinstance(e, dict):
            errs.append(f"{gid}: значение должно быть объектом"); continue
        if e.get("status") not in ("pass", "warn", "fail"):
            errs.append(f"{gid}.status: '{e.get('status')}' вне [pass, warn, fail]")
        for k in ("provided", "evidence", "warnings", "blockers"):
            if k in e and not (isinstance(e[k], list) and all(isinstance(x, str) for x in e[k])):
                errs.append(f"{gid}.{k}: должен быть списком строк")
        if "checks" in e:
            if not isinstance(e["checks"], list):
                errs.append(f"{gid}.checks: должен быть списком")
            else:
                for c in e["checks"]:
                    if not (isinstance(c, dict) and isinstance(c.get("id"), str)
                            and c.get("status") in ("pass", "warn", "fail")):
                        errs.append(f"{gid}.checks: элемент требует id:str + status∈[pass,warn,fail]")
        ov = e.get("override")
        if ov is not None and not (isinstance(ov, dict) and isinstance(ov.get("by"), str)
                                   and isinstance(ov.get("reason"), str)):
            errs.append(f"{gid}.override: требует by:str + reason:str")
        extra = set(e) - _EVIDENCE_KEYS
        if extra:
            errs.append(f"{gid}: неизвестные поля {sorted(extra)}")
    return errs


def load_evidence(path):
    """Загрузить evidence-файл и провалидировать по схеме; SystemExit при ошибках формы."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    errs = validate_evidence(data)
    if errs:
        raise SystemExit("evidence не соответствует schemas/gate-evidence.schema.json:\n  - "
                         + "\n  - ".join(errs))
    return data


# вердикт reviewer-стадии: строка вида "Recommendation: pass" / "status: passed" / "Вердикт: fail"
_VERDICT_PASS = re.compile(
    r"(?:^|\n)\s*(?:recommendation|verdict|вердикт|status|итог)\s*[:=]?\s*\(?\s*"
    r"(pass|passed|approved|одобрено|принято)\b", re.I)
_VERDICT_FAIL = re.compile(
    r"(?:^|\n)\s*(?:recommendation|verdict|вердикт|status|итог)\s*[:=]?\s*\(?\s*"
    r"(fail|failed|blocker|blocked|отклонено|провален)\b", re.I)


def collect_evidence(workflow_id: str, run_dir) -> dict:
    """Собрать evidence из артефактов reviewer-стадий (orchestrator --collect-evidence).
    Для каждого гейта ищем ответственную стадию (gate.stage / gate.responsible_role),
    читаем её артефакт stage-<id>.md и извлекаем вердикт. Reviewer'ский pass = доказательство
    гейта (provided := required_evidence); fail — блокер. Эвристика по структурной строке вердикта."""
    workflows, gates = load_workflows(), load_gates()
    wf = workflows.get(workflow_id, {})
    stages = wf.get("stages", [])
    run_dir = Path(run_dir)
    ev = {}
    for gid in wf.get("quality_gates", []) or []:
        g = gates.get(gid, {})
        stage_id = next((s.get("id") for s in stages
                         if s.get("id") == g.get("stage") or s.get("owner") == g.get("responsible_role")),
                        None)
        if not stage_id:
            continue
        # v2.33: структурный reviewer-result — ИСТОЧНИК ИСТИНЫ (не regex по markdown).
        # Если рядом со стадией есть stage-<id>.reviewer.json (schemas/reviewer-result.schema.json),
        # берём вердикт/blockers из него; markdown-regex остаётся фолбэком для старых артефактов.
        rjson = run_dir / f"stage-{stage_id}.reviewer.json"
        if rjson.exists():
            try:
                rr = json.loads(rjson.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                rr = None
            if isinstance(rr, dict) and rr.get("status") in ("pass", "warn", "fail"):
                if rr["status"] == "fail":
                    ev[gid] = {"status": "fail",
                               "blockers": rr.get("blockers") or [f"reviewer FAIL @ {rjson.name}"],
                               "evidence": [rjson.name]}
                else:
                    # pass/warn: та же дисциплина — required_evidence авто-даём только ai-review
                    prov = list(g.get("required_evidence", []) or []) if classify(g) == "ai-review" else []
                    ev[gid] = {"status": rr["status"], "provided": prov,
                               "checks": rr.get("checks", []), "evidence": [rjson.name]}
                continue
        art = run_dir / f"stage-{stage_id}.md"
        if not art.exists():
            continue
        text = art.read_text(encoding="utf-8")
        if _VERDICT_FAIL.search(text):
            ev[gid] = {"status": "fail", "blockers": [f"reviewer verdict FAIL @ {art.name}"],
                       "evidence": [art.name]}
        elif _VERDICT_PASS.search(text):
            # Дисциплина evidence (v2.16): «pass» ревьюера — доказательство ТОЛЬКО для
            # ai-review гейтов (судья и есть evidence). Для детерминированных/human гейтов
            # слово ревьюера НЕ фабрикует required_evidence (build_passed/tests_passed/…):
            # их закрывают реальные валидаторы/факты, иначе «evidence» снова = «поверьте на слово».
            if classify(g) == "ai-review":
                ev[gid] = {"status": "pass", "provided": list(g.get("required_evidence", []) or []),
                           "evidence": [f"reviewer verdict @ {art.name}"]}
            else:
                ev[gid] = {"status": "pass", "evidence": [f"reviewer verdict @ {art.name}"]}
                # provided пуст -> при наличии required_evidence evaluate_gate честно даст fail
    return ev


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
    return None


def _deploy_readiness_run(base=None):
    """v3.20.0 EngOps срез 2: детерминированная зрелость поставки текущего репозитория.

    Сюда попадаем ТОЛЬКО когда гейт применим (required_when уже отфильтровал неприменимость в
    evaluate_gate), поэтому `configured`/`absent` здесь — честный fail: изменение поставки заявлено,
    а исполняемого пути нет. Недоступность инструмента -> warn, а НЕ pass (бездоказательного pass
    не существует)."""
    b = Path(base) if base else Path.cwd()
    for cand in (b / ".ai" / "managed" / "tools", PKG / "tools"):
        if (cand / "deploy_readiness.py").is_file() and str(cand) not in sys.path:
            sys.path.insert(0, str(cand))
    try:
        from ai_ops_kit.engops import deploy_readiness
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

# ключи, разрешённые схемой gate-result (additionalProperties: false)
_ALLOWED_KEYS = {
    "schema_version", "gate", "status", "blocking", "scope", "checks", "blockers",
    "warnings", "evidence", "affected_files", "affected_artifacts", "tested_revision",
    "artifact_hashes", "owner", "review_mode", "created_at", "expires_at", "override",
    "suggested_next",
}


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


def load_gates():
    return yaml.safe_load((PKG / "quality" / "gates.yaml").read_text(encoding="utf-8")).get("gates", {})


def load_workflows():
    return yaml.safe_load((PKG / "registry" / "workflows.yaml").read_text(encoding="utf-8")).get("workflows", {})


def override_effective(gate: dict, override) -> bool:
    """Снимает ли override блокировку гейта — с учётом ПОЛИТИКИ гейта (v2.16).
    Раньше любой override с by+reason обходил любой блокирующий гейт, игнорируя
    `bypass_policy: forbidden` — это ломало главную гарантию. Теперь:
      - нет override / нет by+reason -> нет;
      - bypass_policy == forbidden -> НИКОГДА (обход запрещён контрактом);
      - override_policy.allowed == true -> да (с субъектом и причиной);
      - иначе (нет явного разрешения) -> нет (доказательства, а не слова)."""
    if not (isinstance(override, dict) and override.get("by") and override.get("reason")):
        return False
    if gate.get("bypass_policy") == "forbidden":
        return False
    op = gate.get("override_policy")
    return bool(isinstance(op, dict) and op.get("allowed"))


def _approval_required(gate: dict, signals: dict = None) -> bool:
    """human_approval: True -> всегда; dict {required_when:[...]} -> только если условие активно
    в сигналах задачи (finding аудита: условный approval не должен блокировать безусловно).
    Токены условий сверяются с сигналами. v2.107 (finding аудита): единый набор алиасов —
    secret_boundary_change ~ security_surface_changed ~ secret_boundary (spec_levels/security_pack
    используют secret_boundary; раньше гейт не срабатывал от него -> дрейф имён сигнала)."""
    ha = gate.get("human_approval")
    if ha is True:
        return True
    if isinstance(ha, dict):
        conds = ha.get("required_when", []) or []
        sig = signals or {}
        alias = {"secret_boundary_change": ["security_surface_changed", "secret_boundary"]}
        for c in conds:
            names = [c] + (alias.get(c) or [])
            if any(sig.get(n) for n in names):
                return True
        return False
    return bool(ha)


def classify(gate: dict, signals: dict = None) -> str:
    """Способ проверки гейта: human-approval | deterministic | ai-review | writer-check.
    Условный human_approval становится human-approval ТОЛЬКО когда условие активно (signals)."""
    if _approval_required(gate, signals):
        return "human-approval"
    if gate.get("validator"):
        return "deterministic"
    if gate.get("review_mode") == "read-only":
        return "ai-review"
    return "writer-check"


def _unmet_reason(kind: str, gate: dict) -> str:
    return {
        "deterministic": f"валидатор '{gate.get('validator')}' не запущен или evidence не предоставлен",
        "ai-review": f"нет заключения reviewer ({gate.get('responsible_role')}) — гейт не пройден",
        "human-approval": "требуется ручное одобрение — не получено",
        "writer-check": "результат ответственной стадии не предоставлен",
    }[kind]


def evaluate_gate(gate_id: str, gate: dict, evidence: dict, tested_revision=None, signals=None,
                  not_applicable=None) -> dict:
    """Один гейт -> machine-readable gate-result (schemas/gate-result.schema.json).

    Дисциплина evidence (v2.16): бездоказательного pass не существует — если гейт
    объявляет `required_evidence`, статус pass засчитывается ТОЛЬКО когда эти ключи
    подтверждены (через `provided` или passing-checks). Для детерминированных гейтов с
    реально запускаемым валидатором проверка исполняется здесь; символические валидаторы
    и reviewer/human-гейты требуют внешнего evidence."""
    kind = classify(gate, signals)
    blocking = bool(gate.get("blocking"))
    required = gate.get("required_evidence", []) or []
    ev = dict((evidence or {}).get(gate_id) or {})

    # v3.15.0 Architecture Baseline: гейт с `required_when` применим ТОЛЬКО когда активен хотя бы один
    # объявленный сигнал (напр. architecture_review — на architecture_change/new_service/…). Иначе —
    # ЧЕСТНЫЙ non-blocking skip (scope=not_applicable, записан в warnings), не тихий pass и не блок.
    rw = gate.get("required_when") or []
    if rw and not any((signals or {}).get(s) for s in rw):
        return {
            "schema_version": 1, "gate": gate_id, "status": "pass", "blocking": False,
            "scope": ["not_applicable"], "checks": [], "blockers": [],
            "warnings": [f"гейт неприменим: нет ни одного сигнала {rw} — не оценивался (honest skip)"],
            "evidence": [], "tested_revision": tested_revision,
            "owner": gate.get("responsible_role", "unknown"),
            "review_mode": gate.get("review_mode", "read-only"),
            "created_at": None, "expires_at": None, "override": None,
        }

    # авто-исполнение детерминированного валидатора, если evidence не подан
    if not ev.get("status") and kind == "deterministic":
        run = deterministic_run(gate.get("validator"))
        if run:
            st, checks, provided = run
            ev = {"status": st, "checks": checks, "provided": provided,
                  "evidence": [f"validator {gate.get('validator')} executed"]}

    status = ev.get("status")
    if status in ("pass", "warn", "fail"):
        checks = ev.get("checks", [])
        blockers = list(ev.get("blockers", [])) if status == "fail" else []
        warnings = list(ev.get("warnings", []))
        evid = ev.get("evidence", [])
        override = ev.get("override")
        # запрет бездоказательного pass: required_evidence обязан быть подтверждён.
        # v2.61 «умное ослабление»: флаг, помеченный not_applicable (инструмента нет в
        # ПОДТВЕРЖДЁННОМ стеке), считается покрытым по освобождению — но это ЗАПИСЫВАЕТСЯ в
        # warnings (не фабрикуется pass): видно, что проверку не делали, потому что нечем.
        if status == "pass" and required:
            exempt = set(not_applicable or [])
            real_covered = set(ev.get("provided", [])) | {c.get("id") for c in checks
                                                          if c.get("status") == "pass"}
            covered = real_covered | exempt
            used_exempt = [k for k in required if k in exempt and k not in real_covered]
            if used_exempt:
                warnings = warnings + [f"освобождено (нет инструмента в стеке): {', '.join(used_exempt)}"]
            missing = [k for k in required if k not in covered]
            if missing:
                msg = f"бездоказательный pass: не подтверждены required_evidence: {', '.join(missing)}"
                status = "fail" if blocking else "warn"
                if blocking:
                    blockers = [msg]
                else:
                    warnings = warnings + [msg]
    else:
        # evidence не предоставлен: честный отказ. Блокирующий -> fail, иначе advisory warn.
        reason = _unmet_reason(kind, gate)
        status = "fail" if blocking else "warn"
        checks = []
        blockers = [reason] if blocking else []
        warnings = [] if blocking else [reason]
        evid = []
        override = None

    # Вывод 1 «дефектов одной сессии»: scenario-как-evidence — ADVISORY, НИКОГДА не блок (не меняет
    # status/blockers). «тесты есть» != «тест смотрит на пользовательский сценарий, а не на слой». Для
    # applicable task_type (advisory_applicability) добавляем warning, если evidence не несёт
    # advisory_evidence-ключей. Graduation: перенос ключей в required_evidence -> станет blocking.
    adv = gate.get("advisory_evidence") or []
    if adv:
        _scope = gate.get("advisory_applicability") or gate.get("applicability") or []
        _tt = (signals or {}).get("task_type")
        if not _scope or _tt in _scope:
            _prov = set(ev.get("provided", []) or [])
            _adv_missing = [k for k in adv if not ev.get(k) and k not in _prov]
            if _adv_missing:
                warnings = warnings + [f"advisory (Вывод 1: сценарий, а не слой): не предъявлены "
                                       f"{', '.join(_adv_missing)} — «тесты есть» != «тест проходит "
                                       f"пользовательский сценарий тем же путём, что продукт»"]

    result = {
        "schema_version": 1,
        "gate": gate_id,
        "status": status,
        "blocking": blocking,
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "evidence": evid,
        "tested_revision": tested_revision,
        "owner": gate.get("responsible_role", "unknown"),
        "review_mode": gate.get("review_mode", "read-only"),
        "created_at": None,
        "expires_at": None,
        "override": override,
    }
    # инвариант: только ключи, разрешённые схемой
    assert set(result).issubset(_ALLOWED_KEYS), set(result) - _ALLOWED_KEYS
    return result


def evaluate(workflow_id: str, evidence: dict = None, tested_revision=None, gate_ids=None, signals=None, not_applicable=None) -> dict:
    """Оценить quality_gates. По умолчанию — гейты контракта; если передан gate_ids (напр.
    агрегированные гейты RunPlan: base_workflow + треки), оцениваются именно они. Так прогон
    проверяет ТО, ЧТО спланировал (finding аудита: треки планировались, но не оценивались).

    blocked=True, если хотя бы один БЛОКИРУЮЩИЙ гейт получил status=fail. override с
    полем 'by'+'reason' на fail-гейте снимает блокировку по этому гейту (records override)."""
    workflows = load_workflows()
    gates = load_gates()
    if workflow_id not in workflows:
        raise SystemExit(f"неизвестный workflow '{workflow_id}' (есть: {', '.join(workflows)})")
    gate_ids = list(gate_ids) if gate_ids is not None else (workflows[workflow_id].get("quality_gates", []) or [])

    results, kinds, unmet = [], {}, []
    for gid in gate_ids:
        gate = gates.get(gid)
        if gate is None:                      # контракт ссылается на несуществующий гейт
            raise SystemExit(f"workflow {workflow_id}: гейт '{gid}' отсутствует в quality/gates.yaml")
        kinds[gid] = classify(gate, signals)
        r = evaluate_gate(gid, gate, evidence, tested_revision, signals=signals,
                          not_applicable=(not_applicable or {}).get(gid))
        results.append(r)
        overridden = override_effective(gate, r.get("override"))
        if r["blocking"] and r["status"] == "fail" and not overridden:
            unmet.append(gid)

    return {
        "schema_version": 1,
        "workflow": workflow_id,
        "evaluated_gates": gate_ids,
        "gate_kinds": kinds,
        "gate_results": results,
        "unmet_gates": unmet,
        "blocked": bool(unmet),
    }


def main(argv):
    if len(argv) > 1:
        wf = argv[1]
        evidence = {}
        if len(argv) > 2:
            evidence = load_evidence(argv[2])
        print(json.dumps(evaluate(wf, evidence), ensure_ascii=False, indent=2))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
