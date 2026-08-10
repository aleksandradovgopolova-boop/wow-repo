#!/usr/bin/env python3
"""Adaptive Spec-First -> SpecCoverage (v2.98, эпик Context Engineering, этап 2).

Не требовать полной спецификации для мелкой задачи, но не начинать сложное изменение без достаточного
описания. Глубина спецификации = f(масштаб, риск, неопределённость, необратимость):

  L0 QUICK       — цель, scope, ожидаемое поведение, критерии приёмки, ограничения, файлы;
  L1 ENGINEERING — + требования, acceptance scenarios, контракты, зависимости, edge cases,
                   архитектурные ограничения, план, write scope, verification strategy;
  L2 PRODUCT     — + проблема, пользователи+JTBD, ценность, текущий/целевой сценарий, гипотезы,
                   метрики, UX-состояния, аналитика, rollout, риски;
  L3 CRITICAL    — + threat model, rollback, migration, failure modes, audit, approvals,
                   compliance, disaster recovery.

Правила (инварианты честности):
  * уровень выбирается детерминированно из сигналов; видно ПОЧЕМУ;
  * уровень МОЖНО повысить при риске/необратимости; НЕЛЬЗЯ понизить молча (запрос ниже расчётного ->
    остаётся расчётный + note об эскалации);
  * у каждого обязательного раздела статус: complete | not_applicable | declined | needs_human | missing;
  * declined ТРЕБУЕТ объяснения;
  * реализация не должна начинаться, пока есть блокирующие (missing) разделы.

Использование:
  spec_levels.py classify --signals '{...}'
  spec_levels.py --selftest
Возврат 0 — ок, 1 — ошибка.
"""

import argparse
import json
import sys
from pathlib import Path

# Уровень по типу задачи (базовый), далее возможна эскалация.
TASK_TYPE_LEVEL = {"QUICK": 0, "ENGINEERING": 1, "PRODUCT": 2, "CRITICAL": 3,
                   "RESEARCH": 1, "AI_FEATURE": 2}
LEVEL_NAME = {0: "L0 QUICK", 1: "L1 ENGINEERING", 2: "L2 PRODUCT", 3: "L3 CRITICAL"}

# Разделы по уровням (КУМУЛЯТИВНО: уровень N включает разделы всех уровней <= N).
LEVEL_SECTIONS = {
    0: ["goal", "scope", "expected_behavior", "acceptance_criteria", "constraints", "affected_files"],
    1: ["requirements", "acceptance_scenarios", "contracts", "dependencies", "edge_cases",
        "architectural_constraints", "implementation_plan", "write_scope", "verification_strategy"],
    2: ["problem", "users_jtbd", "value", "current_scenario", "target_scenario", "hypotheses",
        "success_metrics", "ux_states", "analytics", "rollout", "risks"],
    3: ["threat_model", "rollback_plan", "migration_plan", "failure_modes", "audit_requirements",
        "human_approvals", "compliance_constraints", "disaster_recovery"],
}
SECTION_STATUSES = {"complete", "not_applicable", "declined", "needs_human", "missing"}


def classify(signals):
    """-> {level, level_name, reason, escalated_from, requested_level}. Детерминированно."""
    signals = dict(signals or {})
    tt = signals.get("task_type", "QUICK")
    base = TASK_TYPE_LEVEL.get(tt, 0)
    level, reasons = base, [f"task_type={tt} -> базовый {LEVEL_NAME[base]}"]

    risk = (signals.get("risk") or "").lower()
    # эскалация: критический/высокий риск, необратимость, hotfix/incident/security -> L3 CRITICAL
    escalate_to_critical = (
        risk in ("critical", "high")
        or signals.get("irreversible") is True
        or signals.get("destructive") is True
        or signals.get("secret_boundary") is True
        or tt in ("hotfix", "incident-fix", "security-fix", "critical-change"))
    if escalate_to_critical and level < 3:
        reasons.append(f"эскалация до L3 CRITICAL: risk={risk or '-'}, "
                       f"irreversible={signals.get('irreversible', False)}, "
                       f"secret_boundary={signals.get('secret_boundary', False)}")
        level = 3
    # неопределённость/продуктовость поднимает как минимум до PRODUCT
    elif signals.get("measurable_behavior") and signals.get("user_facing_change") and level < 2:
        reasons.append("эскалация до L2 PRODUCT: измеримое пользовательское изменение")
        level = 2

    # запрошенный уровень нельзя понизить молча
    requested = signals.get("requested_level")
    escalated_from = None
    if isinstance(requested, int) and requested < level:
        escalated_from = requested
        reasons.append(f"запрошен L{requested}, но сигналы требуют {LEVEL_NAME[level]} — "
                       f"понижение отклонено (нельзя понизить молча)")
    elif isinstance(requested, int) and requested > level:
        reasons.append(f"запрошен более высокий L{requested} — принят (повышение разрешено)")
        level = requested

    return {"level": level, "level_name": LEVEL_NAME[level], "reason": reasons,
            "escalated_from": escalated_from, "requested_level": requested}


def required_sections(level):
    """Кумулятивный список обязательных разделов для уровня."""
    out = []
    for lv in range(0, level + 1):
        out += LEVEL_SECTIONS.get(lv, [])
    return out


def assess(signals, provided=None):
    """Собрать SpecCoverage. provided: {section_id: {"status": ..., "note": ...}} — что уже описано.
    Отсутствующие обязательные разделы -> missing (блокируют). declined без note -> ошибка формы."""
    cls = classify(signals)
    level = cls["level"]
    req = required_sections(level)
    provided = provided or {}
    sections, blocking_missing, form_errors = [], [], []
    for sid in req:
        entry = provided.get(sid)
        if not entry:
            status, note = "missing", None
        else:
            status = entry.get("status", "missing")
            note = entry.get("note")
            if status not in SECTION_STATUSES:
                form_errors.append(f"{sid}: неизвестный статус '{status}'")
                status = "missing"
            if status == "declined" and not note:
                form_errors.append(f"{sid}: declined без объяснения (note обязателен)")
        sections.append({"id": sid, "status": status, "note": note})
        if status == "missing":
            blocking_missing.append(sid)
    needs_human = [s["id"] for s in sections if s["status"] == "needs_human"]
    return {
        "schema_version": 1, "kind": "SpecCoverage",
        "level": level, "level_name": cls["level_name"], "level_reason": cls["reason"],
        "escalated_from": cls["escalated_from"],
        "sections": sections,
        "blocking_missing": blocking_missing,
        "needs_human": needs_human,
        "ready_to_implement": (not blocking_missing) and (not form_errors),
        "form_errors": form_errors,
    }


# v2.110 Real Spec-First: разделы, которые ЗАСЧИТЫВАЮТСЯ реальными артефактами прогона (а не только
# текстом spec.yaml). Ключ — id раздела; значение — (относительный путь артефакта, статус-если-есть).
# requirements.yaml/plan.yaml пишет authoring в .ai/runplan/<wid>/; openspec-change — в openspec/changes/<wid>.
_SECTION_ARTIFACT_CREDIT = {
    "requirements": (".ai/runplan/{wid}/requirements.yaml", "complete"),
    "implementation_plan": (".ai/runplan/{wid}/plan.yaml", "complete"),
    "verification_strategy": (".ai/runplan/{wid}/plan.yaml", "complete"),
    "contracts": ("openspec/changes/{wid}", "complete"),
    "acceptance_scenarios": ("openspec/changes/{wid}", "complete"),
}


def _spec_path(child_root, wid):
    return Path(child_root) / "features" / str(wid) / "spec.yaml"


def provided_from_artifacts(child_root, wid, work_root=None):
    """v2.110: собрать provided-карту РАЗДЕЛОВ из РЕАЛЬНЫХ артефактов на диске (не из сигналов).

    Источники: features/<wid>/spec.yaml (явно описанные разделы) + засчитанные артефакты прогона
    (requirements/plan/openspec) в work_root (или child_root, если work_root не задан). Возвращает
    {section_id: {"status": ..., "note": ...}} — вход для assess(). Детерминированно, без модели.
    """
    child_root = Path(child_root)
    roots_for_credit = [Path(work_root)] if work_root else []
    roots_for_credit.append(child_root)
    provided = {}

    # 1) явный spec.yaml — источник истины по разделам (что человек/автор реально описал)
    sp = _spec_path(child_root, wid)
    if sp.is_file():
        try:
            import yaml
            doc = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
            for sid, entry in (doc.get("sections") or {}).items():
                if isinstance(entry, dict):
                    provided[sid] = {"status": entry.get("status", "missing"), "note": entry.get("note")}
                elif isinstance(entry, str):
                    provided[sid] = {"status": "complete", "note": None}
        except Exception:  # noqa: BLE001 — битый spec.yaml не должен ронять оценку
            pass

    # 2) засчитать разделы по реальным артефактам прогона (если раздел ещё не описан явно)
    for sid, (rel_tmpl, status) in _SECTION_ARTIFACT_CREDIT.items():
        if provided.get(sid, {}).get("status") in ("complete", "not_applicable"):
            continue
        rel = rel_tmpl.format(wid=wid)
        for base in roots_for_credit:
            p = base / rel
            if p.exists():
                provided[sid] = {"status": status,
                                 "note": f"засчитан по артефакту {rel}"}
                break
    return provided


def assess_from_artifacts(signals, child_root, wid, work_root=None):
    """v2.110: SpecCoverage, где provided взят из РЕАЛЬНЫХ артефактов (spec.yaml + прогон), а не пуст.
    Добавляет `spec_artifact` (есть ли явный spec.yaml) и `provided_sources` для честности отчёта."""
    provided = provided_from_artifacts(child_root, wid, work_root=work_root)
    cov = assess(signals, provided=provided)
    cov["spec_artifact"] = _spec_path(child_root, wid).is_file()
    cov["provided_sources"] = {sid: e.get("note") for sid, e in provided.items()
                               if e.get("note")}
    cov["covered_sections"] = sorted(sid for sid, e in provided.items()
                                     if e.get("status") in ("complete", "not_applicable"))
    return cov


def create_spec(child_root, wid, signals, overwrite=False):
    """v2.110: РЕАЛЬНО создать spec-артефакт нужной глубины (features/<wid>/spec.yaml) со всеми
    обязательными разделами уровня (заготовки status=missing). -> (path, created). Не перезаписывает
    существующий без overwrite (не теряем описанное)."""
    import yaml
    sp = _spec_path(child_root, wid)
    cls = classify(signals)
    level = cls["level"]
    if sp.is_file() and not overwrite:
        return sp, False
    sections = {sid: {"status": "missing", "content": "", "note": None}
                for sid in required_sections(level)}
    doc = {"schema_version": 1, "kind": "spec", "workitem_id": str(wid),
           "level": level, "level_name": cls["level_name"],
           "level_reason": cls["reason"], "sections": sections}
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return sp, True


def validate_spec(child_root, wid, signals, work_root=None):
    """v2.110: провалидировать реальный spec-артефакт против обязательных разделов уровня.
    -> SpecCoverage (assess_from_artifacts) + флаг spec_artifact. Если spec.yaml нет — честно
    отмечает отсутствие (не притворяется, что спека есть)."""
    cov = assess_from_artifacts(signals, child_root, wid, work_root=work_root)
    if not cov["spec_artifact"]:
        cov["note"] = (f"spec-артефакт features/{wid}/spec.yaml отсутствует — создай "
                       f"`ai-ops specify` (разделы засчитаны только по артефактам прогона, если есть)")
    return cov


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    expect("QUICK -> L0", classify({"task_type": "QUICK"})["level"] == 0)
    expect("ENGINEERING -> L1", classify({"task_type": "ENGINEERING"})["level"] == 1)
    expect("PRODUCT -> L2", classify({"task_type": "PRODUCT"})["level"] == 2)
    expect("CRITICAL -> L3", classify({"task_type": "CRITICAL"})["level"] == 3)
    # эскалация по риску
    esc = classify({"task_type": "QUICK", "risk": "critical"})
    expect("QUICK + risk=critical -> эскалация до L3", esc["level"] == 3
           and any("эскалация" in r for r in esc["reason"]))
    esc2 = classify({"task_type": "ENGINEERING", "secret_boundary": True})
    expect("ENGINEERING + secret_boundary -> L3", esc2["level"] == 3)
    esc3 = classify({"task_type": "QUICK", "measurable_behavior": True, "user_facing_change": True})
    expect("QUICK + измеримое польз. изменение -> L2", esc3["level"] == 2)
    # нельзя понизить молча
    low = classify({"task_type": "ENGINEERING", "requested_level": 0})
    expect("запрос L0 при ENGINEERING -> остаётся L1 + note (не понизили молча)",
           low["level"] == 1 and low["escalated_from"] == 0)
    high = classify({"task_type": "QUICK", "requested_level": 2})
    expect("запрос выше -> принят (повышение разрешено)", high["level"] == 2)

    # кумулятивность разделов
    expect("L1 включает разделы L0 и L1", set(LEVEL_SECTIONS[0]) <= set(required_sections(1))
           and "verification_strategy" in required_sections(1))
    expect("L3 включает threat_model + разделы всех уровней",
           "threat_model" in required_sections(3) and "goal" in required_sections(3))

    # assess: пустой QUICK -> все missing -> не готов к реализации
    a0 = assess({"task_type": "QUICK"})
    expect("пустой QUICK -> blocking_missing непуст, ready_to_implement=False",
           a0["blocking_missing"] and a0["ready_to_implement"] is False)
    # полный QUICK -> готов
    full = {s: {"status": "complete"} for s in LEVEL_SECTIONS[0]}
    a1 = assess({"task_type": "QUICK"}, full)
    expect("полный QUICK -> ready_to_implement=True", a1["ready_to_implement"] is True
           and a1["blocking_missing"] == [])
    # not_applicable закрывает раздел (не блокирует)
    na = {s: {"status": "not_applicable", "note": "нет UI"} for s in LEVEL_SECTIONS[0]}
    expect("not_applicable не блокирует", assess({"task_type": "QUICK"}, na)["ready_to_implement"] is True)
    # declined без note -> form_error
    dec = dict(full); dec["scope"] = {"status": "declined"}
    expect("declined без объяснения -> form_error + не готов",
           assess({"task_type": "QUICK"}, dec)["form_errors"] and
           assess({"task_type": "QUICK"}, dec)["ready_to_implement"] is False)
    # needs_human фиксируется
    nh = dict(full); nh["constraints"] = {"status": "needs_human", "note": "нужен владелец"}
    expect("needs_human зафиксирован", "constraints" in assess({"task_type": "QUICK"}, nh)["needs_human"])

    # v2.110 Real Spec-First: create_spec реально создаёт артефакт нужной глубины
    import tempfile
    import yaml
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        sig_eng = {"task_type": "ENGINEERING", "affected_areas": ["core"]}
        sp, created = create_spec(root, "sf", sig_eng)
        expect("v2.110 create_spec: артефакт создан на диске", created and sp.is_file())
        doc = yaml.safe_load(sp.read_text(encoding="utf-8"))
        expect("v2.110 create_spec: разделы уровня L1 в артефакте (заготовки missing)",
               "requirements" in doc["sections"] and doc["sections"]["goal"]["status"] == "missing"
               and doc["level"] == 1)
        # повторный create без overwrite не перезаписывает (не теряем описанное)
        _, created2 = create_spec(root, "sf", sig_eng)
        expect("v2.110 create_spec: без overwrite не перезаписывает", created2 is False)

        # пустой spec (всё missing) -> assess_from_artifacts НЕ готов, spec_artifact=True
        cov0 = assess_from_artifacts(sig_eng, root, "sf")
        expect("v2.110 from_artifacts: пустой spec -> ready_to_implement=False + spec_artifact=True",
               cov0["ready_to_implement"] is False and cov0["spec_artifact"] is True
               and cov0["blocking_missing"])

        # заполнить разделы L0/L1 в spec.yaml -> покрытие растёт (из РЕАЛЬНОГО артефакта)
        for sid in doc["sections"]:
            doc["sections"][sid] = {"status": "complete", "content": "x"}
        sp.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
        cov1 = assess_from_artifacts(sig_eng, root, "sf")
        expect("v2.110 from_artifacts: заполненный spec.yaml -> ready_to_implement=True (реально из файла)",
               cov1["ready_to_implement"] is True and not cov1["blocking_missing"])

        # засчёт по артефакту прогона: requirements.yaml закрывает раздел requirements
        with tempfile.TemporaryDirectory() as td2:
            wr = Path(td2)
            (wr / ".ai" / "runplan" / "art").mkdir(parents=True)
            (wr / ".ai" / "runplan" / "art" / "requirements.yaml").write_text("k: v\n", encoding="utf-8")
            prov = provided_from_artifacts(root, "art", work_root=wr)
            expect("v2.110 credit: requirements.yaml засчитывает раздел requirements",
                   prov.get("requirements", {}).get("status") == "complete"
                   and "requirements.yaml" in (prov["requirements"].get("note") or ""))

        # validate_spec без артефакта -> честная пометка об отсутствии (не притворяется)
        cov_none = validate_spec(root, "never", sig_eng)
        expect("v2.110 validate_spec: нет spec.yaml -> spec_artifact=False + note (честно)",
               cov_none["spec_artifact"] is False and "note" in cov_none)

    print("spec_levels selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    ap = argparse.ArgumentParser(prog="spec_levels.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("classify")
    c.add_argument("--signals", default="{}")
    c.add_argument("--json", action="store_true")
    # v2.110 Real Spec-First: create/validate реального spec-артефакта
    cr = sub.add_parser("create", help="создать features/<wid>/spec.yaml нужной глубины")
    cr.add_argument("child_root"); cr.add_argument("wid")
    cr.add_argument("--signals", default="{}"); cr.add_argument("--overwrite", action="store_true")
    cr.add_argument("--json", action="store_true")
    vd = sub.add_parser("validate", help="провалидировать реальный spec-артефакт против уровня")
    vd.add_argument("child_root"); vd.add_argument("wid")
    vd.add_argument("--signals", default="{}"); vd.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "create":
        sp, created = create_spec(Path(a.child_root), a.wid, json.loads(a.signals), overwrite=a.overwrite)
        cov = assess_from_artifacts(json.loads(a.signals), Path(a.child_root), a.wid)
        if a.json:
            print(json.dumps({"path": str(sp), "created": created, "coverage": cov},
                             ensure_ascii=False, indent=2))
        else:
            print(f"SPECIFY: {'создан' if created else 'уже существует'} {sp} · {cov['level_name']} · "
                  f"обязательных разделов {len(cov['sections'])} · не хватает {len(cov['blocking_missing'])}")
        return 0
    if a.cmd == "validate":
        cov = validate_spec(Path(a.child_root), a.wid, json.loads(a.signals))
        if a.json:
            print(json.dumps(cov, ensure_ascii=False, indent=2))
        else:
            print(f"SPEC-VALIDATE: {cov['level_name']} · spec_artifact={cov['spec_artifact']} · "
                  f"ready_to_implement={cov['ready_to_implement']} · "
                  f"не хватает: {', '.join(cov['blocking_missing']) or '—'}")
            if cov.get("note"):
                print(f"  · {cov['note']}")
        return 0 if cov["ready_to_implement"] else 1
    if a.cmd == "classify":
        cov = assess(json.loads(a.signals))
        if a.json:
            print(json.dumps(cov, ensure_ascii=False, indent=2))
        else:
            print(f"SPEC-LEVEL: {cov['level_name']}")
            for r in cov["level_reason"]:
                print(f"  · {r}")
            print(f"  обязательных разделов: {len(cov['sections'])} · "
                  f"не хватает (missing): {len(cov['blocking_missing'])} · "
                  f"ready_to_implement: {cov['ready_to_implement']}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
