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
выдумывает вердиктов, которых не было.

СТРУКТУРА (v2.15, разрез монолита): фасад держит ЯДРО ОЦЕНКИ (evaluate_gate/evaluate +
closure/evidence-вердикт + main), а классификация и сбор evidence вынесены в сателлит
`gate_evidence`, детерминированные раннеры — в `gate_runners`. Обе публичные поверхности
ре-экспортированы отсюда: внешний код по-прежнему зовёт `gate_executor.X`.

Использование:
  gate_executor.py <WORKFLOW> [evidence.json]   — оценить гейты (JSON-отчёт)
  gate_executor.py --selftest                    — офлайн-проверки

Требует pyyaml.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml  # noqa: F401 — исторически импортируем из gate_executor (внешние тесты берут его отсюда)

from ai_ops_kit.gates import gate_policy  # риск-калиброванная строгость под owner-флагом (#543)

# --- сателлиты: классификация + сбор evidence (фундамент) и детерминированные раннеры ---
from ai_ops_kit.gates.gate_evidence import (  # noqa: F401 — ре-экспорт публичной поверхности
    CLOSED_BY,
    CLOSED_BY_VALUES,
    EVIDENCE_SOURCE,
    EVIDENCE_SOURCE_VALUES,
    _approval_required,
    _EVIDENCE_KEYS,
    _EVIDENCE_SOURCE_KINDS,
    _last_prose_verdict,
    _REFUSAL_NEEDS_HUMAN,
    _VERDICT_FAIL,
    _VERDICT_PASS,
    _VERDICT_WARN,
    classify,
    closed_by,
    collect_evidence,
    evidence_from_judge_output,
    evidence_from_judge_refusal,
    evidence_from_markdown,
    evidence_from_no_verdict,
    evidence_from_reviewer_result,
    evidence_source,
    extract_reviewer_json,
    load_evidence,
    load_gates,
    load_workflows,
    override_effective,
    risk_calibrated_config,
    validate_evidence,
)
from ai_ops_kit.gates.gate_runners import (  # noqa: F401 — ре-экспорт публичной поверхности
    _deploy_readiness_run,
    _documentation_updated_run,
    _EVIDENCE_TYPES,
    _feature_decisions_run,
    _freshness_run,
    _run_validator,
    deterministic_run,
    validate_evidence_schemas,
)

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])

# ключи, разрешённые схемой gate-result (additionalProperties: false)
_ALLOWED_KEYS = {
    "schema_version", "gate", "status", "blocking", "awaiting_human", "scope", "checks", "blockers",
    "warnings", "evidence", "affected_files", "affected_artifacts", "tested_revision",
    "artifact_hashes", "owner", "review_mode", "created_at", "expires_at", "override",
    "suggested_next",
}


def closure_breakdown(gates: dict, signals: dict = None) -> dict:
    """Разбивка «кто закрывает» по набору гейтов -> {counts, by_gate, judged_or_human, by_source, …}.

    `judged_or_human` — то, ради чего разбивка существует: список гейтов, чьё «зелёное» является
    мнением. Человек, читающий отчёт, обязан видеть его как список, а не выводить из чисел.

    Веха 4.2 (#588): та же разбивка отвечает и на грубый вопрос доверия — `by_source` схлопывает
    четыре «кто закрывает» в три источника (deterministic | ai_judgment | human), а `ai_judgment`
    называет гейты, чьё «зелёное» — advisory-мнение, не доказательство.
    """
    by_gate = {gid: closed_by(g or {}, signals) for gid, g in (gates or {}).items()}
    counts = {v: 0 for v in CLOSED_BY_VALUES}
    for v in by_gate.values():
        counts[v] = counts.get(v, 0) + 1
    by_source = {gid: EVIDENCE_SOURCE[v] for gid, v in by_gate.items()}
    return {"counts": counts, "by_gate": by_gate,
            "machine_checked": sorted(g for g, v in by_gate.items() if v == "validator"),
            "judged_or_human": sorted(g for g, v in by_gate.items() if v != "validator"),
            # веха 4.2: тот же факт под углом доверия к доказательству
            "by_source": by_source,
            "deterministic": sorted(g for g, s in by_source.items() if s == "deterministic"),
            "ai_judgment": sorted(g for g, s in by_source.items() if s == "ai_judgment"),
            "human": sorted(g for g, s in by_source.items() if s == "human")}


def evidence_verdict(gate_results: list, gates: dict, signals: dict = None) -> dict:
    """Вердикт честности evidence вехи 4.2 (#588): «verified» ПРИВИЛЕГИРУЕТ детерминированные сигналы.

    Правило: `verified=True` только если ХОТЯ БЫ ОДИН пройденный (status=pass) гейт закрыт
    детерминированно (validator: тест/lint/CI/schema). Одно AI-суждение (advisory) — заключение
    судьи или самозаявление писателя — БЕЗ детерминированной опоры `verified` НЕ даёт: система не
    имеет права называть «проверено» то, что держится на её же (или соседнего AI) мнении.
    Решение человека тоже само по себе не делает вердикт детерминированным — оно названо отдельно.

    Возвращает {verified, deterministic, ai_judgment, advisory, human, reason}. `advisory` — те же
    ai_judgment-гейты под именем, которое обязано попасть в readout: их «зелёное» — мнение.
    """
    src = {}
    for r in gate_results or []:
        gid = r.get("gate")
        g = (gates or {}).get(gid)
        if g is not None:
            src[gid] = evidence_source(g, signals)
    passed = [r for r in (gate_results or []) if r.get("status") == "pass"]
    det = sorted(r["gate"] for r in passed if src.get(r.get("gate")) == "deterministic")
    aij = sorted(r["gate"] for r in passed if src.get(r.get("gate")) == "ai_judgment")
    hum = sorted(r["gate"] for r in passed if src.get(r.get("gate")) == "human")
    verified = bool(det)
    if verified:
        reason = f"есть детерминированная опора ({', '.join(det)}) — verified"
    elif aij:
        reason = ("«зелёное» держится только на AI-суждении (advisory: "
                  f"{', '.join(aij)}) — детерминированной опоры нет, verified не выставляется")
    elif hum:
        reason = f"закрыто человеком ({', '.join(hum)}); детерминированной верификации нет"
    else:
        reason = "нет пройденных гейтов — верифицировать нечего"
    return {"verified": verified, "deterministic": det, "ai_judgment": aij,
            "advisory": aij, "human": hum, "reason": reason}


def _unmet_reason(kind: str, gate: dict) -> str:
    return {
        "deterministic": f"валидатор '{gate.get('validator')}' не запущен или evidence не предоставлен",
        "ai-review": f"нет заключения reviewer ({gate.get('responsible_role')}) — гейт не пройден",
        "human-approval": "требуется ручное одобрение — не получено",
        "writer-check": "результат ответственной стадии не предоставлен",
    }[kind]


def evaluate_gate(gate_id: str, gate: dict, evidence: dict, tested_revision=None, signals=None,
                  not_applicable=None, exempt_reason=None) -> dict:
    """Один гейт -> machine-readable gate-result (schemas/gate-result.schema.json).

    Дисциплина evidence (v2.16): бездоказательного pass не существует — если гейт
    объявляет `required_evidence`, статус pass засчитывается ТОЛЬКО когда эти ключи
    подтверждены (через `provided` или passing-checks). Для детерминированных гейтов с
    реально запускаемым валидатором проверка исполняется здесь; символические валидаторы
    и reviewer/human-гейты требуют внешнего evidence."""
    kind = classify(gate, signals)
    # v3.2 (#543) shadow -> live: под owner-флагом risk_calibrated_enforcement строгость UI-гейта
    # берётся из candidate_policy (internal low-risk -> advisory, critical/user_facing -> blocking).
    # Флаг OFF (по умолчанию) -> строгость = статическая gate.blocking, поведение НЕ меняется.
    blocking, _rc_reason = gate_policy.effective_enforcement(
        gate_id, bool(gate.get("blocking")), signals or {},
        enabled=gate_policy.risk_calibrated_enforcement_enabled(signals))
    required = gate.get("required_evidence", []) or []
    ev = dict((evidence or {}).get(gate_id) or {})

    # v3.15.0 Architecture Baseline: гейт с `required_when` применим ТОЛЬКО когда активен хотя бы один
    # объявленный сигнал (напр. architecture_review — на architecture_change/new_service/…). Иначе —
    # ЧЕСТНЫЙ non-blocking skip (scope=not_applicable, записан в warnings), не тихий pass и не блок.
    rw = gate.get("required_when") or []
    if rw and not any((signals or {}).get(s) for s in rw):
        return {
            "schema_version": 1, "gate": gate_id, "status": "pass", "blocking": False,
            # признак обязан быть у КАЖДОГО результата, включая честный пропуск: отсутствие поля
            # читается как «не знаю», а здесь это неотличимо от «нет»
            "awaiting_human": False,
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
            # Освобождение, которое evidence уже ОБЪЯСНИЛ последствием (#1183: «стиль кода никто не
            # проверяет…» в его warnings), общая строка не повторяет — иначе рядом с последствием
            # стояла бы прежняя отговорка «нет инструмента». Объяснение и флаг приходят вместе.
            _explained = set(ev.get("explained_exemptions") or []) if not exempt_reason else set()
            _generic = [k for k in used_exempt if k not in _explained]
            if _generic:
                # ПРИЧИНА ОСВОБОЖДЕНИЯ НАЗЫВАЕТСЯ, А НЕ ПОДРАЗУМЕВАЕТСЯ. Прежде текст был жёстко
                # «нет инструмента в стеке» — единственная причина, которая существовала в v2.61.
                # Освобождение по другому поводу (изменение только документации) писало бы в отчёт
                # ЧУЖУЮ причину: владелец читал бы «нет инструмента» там, где инструмент есть и
                # просто не нужен. Ложное основание хуже отсутствующего — по нему принимают решения.
                warnings = warnings + [f"освобождено ({exempt_reason or 'нет инструмента в стеке'}): "
                                       f"{', '.join(_generic)}"]
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

    # Демотия строгости по риск-калибровке (#543) — НАЗВАНА в warnings, а не молчалива: владелец
    # видит, что блокировка снята политикой под его флагом, а не потерялась. Advisory-семантика та
    # же, что у reviewer-калибровки в pipeline_evidence: fail демотированного гейта -> warn (не
    # блокирует), а его блокеры переезжают в warnings (blockers остаются только на боевом fail).
    if _rc_reason:
        warnings = warnings + [_rc_reason]
        if status == "fail":
            warnings = warnings + [f"(advisory) {b}" for b in blockers]
            blockers = []
            status = "warn"

    result = {
        "schema_version": 1,
        "gate": gate_id,
        "status": status,
        "blocking": blocking,
        # ХОД ЗА ЧЕЛОВЕКОМ — ОТДЕЛЬНЫЙ ФАКТ, А НЕ ОТТЕНОК ТЕКСТА БЛОКЕРА (наблюдение 19.08.2026,
        # работа `security-gate-closable-on-quick`). Конвейер уже ставил `pending_human` в evidence
        # гейта, но сюда признак не доезжал: результат собирается из фиксированного набора полей, и
        # всё остальное терялось при уплощении. В `run-report.json` его не было НИ РАЗУ.
        #
        # ПОЧЕМУ ЭТО НЕ КОСМЕТИКА. «Гейт нашёл дефект» и «гейт ждёт решения человека» требуют разных
        # действий: первое чинит агент, второе он не может сделать в принципе. Читая отчёт, оба
        # случая выглядели одинаково — как «работа не готова», — и ожидание человека молча
        # засчитывалось в неудачу прогона.
        #
        # ВСЕГДА bool, НИКОГДА не отсутствует: пропущенное поле читается как «не знаю», а «не знаю»
        # здесь неотличимо от «нет» — ровно та подмена, против которой стоит остальной контур.
        "awaiting_human": bool(ev.get("pending_human") or ev.get("human_handoff")),
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


def evaluate(workflow_id: str, evidence: dict = None, tested_revision=None, gate_ids=None, signals=None, not_applicable=None, exempt_reason=None) -> dict:
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
                          not_applicable=(not_applicable or {}).get(gid),
                          exempt_reason=(exempt_reason or {}).get(gid))
        results.append(r)
        overridden = override_effective(gate, r.get("override"))
        if r["blocking"] and r["status"] == "fail" and not overridden:
            unmet.append(gid)

    return {
        "schema_version": 1,
        "workflow": workflow_id,
        "evaluated_gates": gate_ids,
        "gate_kinds": kinds,
        # КТО ЗАКРЫЛ КАЖДЫЙ ГЕЙТ. Классификация существовала и раньше (`gate_kinds`), но наружу не
        # выходила: в отчёте прогона все гейты выглядели одинаково, и «зелёное» от валидатора было
        # неотличимо от «зелёного» по мнению судьи. Дочка, читающая отчёт, обязана видеть разницу.
        "closure": closure_breakdown({gid: gates[gid] for gid in gate_ids}, signals),
        # ВЕРДИКТ ЧЕСТНОСТИ EVIDENCE (веха 4.2, #588): «verified» привилегирует детерминированные
        # сигналы. Одно AI-суждение (advisory) без детерминированной опоры не даёт verified — иначе
        # система назвала бы «проверено» то, что держится на её же мнении.
        "evidence_verdict": evidence_verdict(results, {gid: gates[gid] for gid in gate_ids}, signals),
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
