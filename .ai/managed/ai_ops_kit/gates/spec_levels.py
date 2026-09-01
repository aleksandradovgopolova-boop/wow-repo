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
from __future__ import annotations

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
    sections, blocking_missing, form_errors, invalid_status = [], [], [], []
    for sid in req:
        entry = provided.get(sid)
        if not entry:
            status, note = "missing", None
        else:
            status = entry.get("status", "missing")
            note = entry.get("note")
            if status not in SECTION_STATUSES:
                # F-013 (находка живой квалификации на niti): раздел БЫЛ заполнен, но со статусом
                # вне словаря (например `filled`) — и уезжал в missing, а блокер печатал
                # «не заполнено». Диагноз уводил в сторону: содержимое есть, дело в слове.
                # Называем допустимые значения и помечаем случай отдельно от пустого раздела.
                form_errors.append(
                    f"{sid}: неизвестный статус '{status}' — допустимо: "
                    f"{'/'.join(sorted(SECTION_STATUSES - {'missing'}))}")
                invalid_status.append({"id": sid, "given": status,
                                       "has_content": bool(entry.get("content"))})
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
        # F-013: разделы, у которых содержимое есть, а статус вне словаря — отдельно от пустых,
        # чтобы блокер не звал «заполнить» уже заполненное.
        "invalid_status": invalid_status,
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
        # Причина подавления (срез ратчета, 2026-08-12): битый spec.yaml не засчитывает разделы,
        # то есть оценка идёт путём «не описано» -> missing -> БЛОКИРУЕТ. Fail-closed, и он верен.
        #
        # Тип оставлен широким СОЗНАТЕЛЬНО, хотя первым порывом было сузить до
        # `(OSError, yaml.YAMLError)`: `import yaml` стоит ВНУТРИ этого же `try` (строка выше), и при
        # отсутствии pyyaml except-клауза упала бы на неразрешённом имени `yaml` — NameError вместо
        # аккуратного отказа. Сужение здесь ухудшило бы поведение, а не улучшило.
        except Exception:  # noqa: BLE001,S110 — битый spec -> missing -> блокирует; см. про import выше
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
    обязательными разделами уровня (заготовки status=missing). -> (path, created, added).
    Не перезаписывает существующий без overwrite (не теряем описанное).

    F-029 (поле 2026-08-15, дочка ai-ops-cockpit; ПОВТОР находки другой дочки): здесь стоял ранний
    выход `return sp, False` — существующий файл не трогали вовсе. Если уровень с прошлого раза
    поднялся (сигналы стали продуктовыми/рисковыми), `assess` считал разделы по НОВОМУ уровню, а в
    файле оставались разделы старого: `specify` говорил «заполнить нужно 9 разделов», а заполнять
    было нечего — их там не было, и `run` блокировался на разделах, которых шаблон не создавал.
    Теперь недостающие разделы ДОПИСЫВАЮТСЯ заготовками (`missing`), уже описанные не трогаются, а
    уровень в файле поднимается до расчётного. Понижения нет: разделы прошлого уровня не удаляются
    и `level` вниз не переписывается — уровень нельзя понизить молча (инвариант модуля)."""
    import yaml
    sp = _spec_path(child_root, wid)
    cls = classify(signals)
    level = cls["level"]
    if sp.is_file() and not overwrite:
        return _add_missing_sections(sp, cls)
    sections = {sid: {"status": "missing", "content": "", "note": None}
                for sid in required_sections(level)}
    doc = {"schema_version": 1, "kind": "spec", "workitem_id": str(wid),
           "level": level, "level_name": cls["level_name"],
           "level_reason": cls["reason"],
           # F-013: словарь статусов — прямо в файле. Спеку заполняет человек или агент без
           # контекста исходников кита; раньше допустимые значения находились только чтением
           # spec_levels.py, а угаданное слово молча превращало раздел в «не заполнено».
           "section_statuses": sorted(SECTION_STATUSES),
           "sections": sections}
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(_render_spec(doc), encoding="utf-8")
    return sp, True, {"added": sorted(sections), "error": None}


def _render_spec(doc):
    """Текст spec.yaml: шапка со словарём статусов + сам документ. Один вид у создания и дописывания."""
    import yaml
    return ("# status раздела: " + " | ".join(sorted(SECTION_STATUSES - {"missing"}))
            + "\n# declined требует note с объяснением.\n"
            + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False))


def _add_missing_sections(sp, cls):
    """F-029: дописать в существующий spec.yaml разделы, которых требует расчётный уровень.
    -> (path, created=False, {"added": [...], "error": None|str}). Заполненное не трогается;
    разделы не удаляются.

    Битый/непрочитанный файл НЕ переписываем: молча заменить описанное заготовками — потеря работы
    человека, а это дороже незакрытого гейта. Тогда added пуст, и вызывающий сообщает правду
    («дописать не удалось»), а не выдаёт непроведённую правку за проведённую."""
    import yaml
    level = cls["level"]
    try:
        doc = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 — см. про import yaml в provided_from_artifacts
        return sp, False, {"added": [], "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(doc, dict) or not isinstance(doc.get("sections"), dict):
        return sp, False, {"added": [], "error": "spec.yaml не содержит карты разделов (sections)"}
    sections = doc["sections"]
    added = [sid for sid in required_sections(level) if sid not in sections]
    if not added:
        return sp, False, {"added": [], "error": None}
    for sid in added:
        sections[sid] = {"status": "missing", "content": "", "note": None}
    # Уровень поднимаем до расчётного и говорим ПОЧЕМУ; вниз не переписываем (нельзя понизить молча).
    if int(doc.get("level") or 0) < level:
        doc["level"] = level
        doc["level_name"] = cls["level_name"]
        doc["level_reason"] = cls["reason"]
    doc["section_statuses"] = sorted(SECTION_STATUSES)
    sp.write_text(_render_spec(doc), encoding="utf-8")
    return sp, False, {"added": added, "error": None}


def pending_sections(child_root, wid, signals):
    """Каких разделов не хватает СУЩЕСТВУЮЩЕЙ спеке под уровень, посчитанный по текущим сигналам.

    ЧИСТОЕ ЧТЕНИЕ, БЕЗ ЗАПИСИ: этим вопросом решают, применять ли потолок траты, — а решение о
    применении потолка не имеет права само менять артефакт.

    ЗАЧЕМ (поле 19.08.2026, находка B2-26 второго brownfield, повтор находки 15.08). Повторный
    `specify` под поднявшийся уровень — не «ещё один заход разбора», а КОНЕЧНОЕ дописывание
    заготовок, и модель он не зовёт вовсе. Но процессный потолок видел «ещё один процессный шаг без
    кода» и отказывал ЭКОНОМИЧЕСКОЙ причиной: человек читал про деньги там, где дело было в
    разделах, а уровень в файле оставался прежним.

    -> {"spec_exists", "level_now", "level_in_file", "level_name", "missing": [...]}.
    Спеки нет — `missing` пуст: дописывать нечего, там обычный первый разбор.
    """
    import yaml
    sp = _spec_path(child_root, wid)
    cls = classify(signals)
    out = {"spec_exists": bool(sp.is_file()), "level_now": cls["level"],
           "level_name": cls["level_name"], "level_in_file": None, "missing": []}
    if not sp.is_file():
        return out
    try:
        doc = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — битый файл: чинит `create_spec`, здесь молчим, а не гадаем
        return out
    if not isinstance(doc, dict) or not isinstance(doc.get("sections"), dict):
        return out
    out["level_in_file"] = doc.get("level")
    out["missing"] = [sid for sid in required_sections(cls["level"]) if sid not in doc["sections"]]
    return out


def validate_spec(child_root, wid, signals, work_root=None):
    """v2.110: провалидировать реальный spec-артефакт против обязательных разделов уровня.
    -> SpecCoverage (assess_from_artifacts) + флаг spec_artifact. Если spec.yaml нет — честно
    отмечает отсутствие (не притворяется, что спека есть)."""
    cov = assess_from_artifacts(signals, child_root, wid, work_root=work_root)
    if not cov["spec_artifact"]:
        cov["note"] = (f"spec-артефакт features/{wid}/spec.yaml отсутствует — создай "
                       f"`ai-ops specify` (разделы засчитаны только по артефактам прогона, если есть)")
    return cov


def main(argv):
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
        sp, created, rep = create_spec(Path(a.child_root), a.wid, json.loads(a.signals),
                                       overwrite=a.overwrite)
        cov = assess_from_artifacts(json.loads(a.signals), Path(a.child_root), a.wid)
        if a.json:
            print(json.dumps({"path": str(sp), "created": created, "added": rep["added"],
                              "add_error": rep["error"], "coverage": cov},
                             ensure_ascii=False, indent=2))
        else:
            _what = "создан" if created else (
                f"дописано разделов {len(rep['added'])}" if rep["added"] else "уже существует")
            print(f"SPECIFY: {_what} {sp} · {cov['level_name']} · "
                  f"обязательных разделов {len(cov['sections'])} · не хватает {len(cov['blocking_missing'])}"
                  + (f" · ДОПИСАТЬ НЕ УДАЛОСЬ: {rep['error']}" if rep["error"] else ""))
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
