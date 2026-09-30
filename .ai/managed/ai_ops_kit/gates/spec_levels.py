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

Словарь уровней (важно не путать две разные оси):
  * ЗДЕСЬ L0–L3 — это ГЛУБИНА СПЕЦИФИКАЦИИ (сколько разделов нужно заполнить). Уровней
    ровно четыре: L0..L3. L4/L5 в этой оси НЕ существует — если ищете их, вы попали не сюда.
  * ЭТАПЫ ЖИЗНЕННОГО ЦИКЛА продукта (Discovery → Product Definition → UX → … → Monitoring,
    см. VISION.md) — отдельная ось; в ките они названы словами, а не номерами Ln. Любая
    нумерация «L0–L5» из черновиков (RR-018/DP-117) относилась бы к этапам цикла, а не к
    глубине спецификации, и в кодовую базу не заведена.

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

# ─── Сторож против РАСПОЛЗАНИЯ ЦЕРЕМОНИИ (направление risk-based-ceremony) ───────────────────────
# Замер продукт-ревью: «сложность растёт с риском, а не с полнотой». Объём процесса (сколько
# разделов спеки требует уровень) обязан подниматься ТОЛЬКО вместе с риском. Но LEVEL_SECTIONS —
# обычный dict: любой мог дописать раздел в L0, и форма для мелкой обратимой задачи молча
# распухла бы без всякого риска, который это оправдывает. Здесь — БЮДЖЕТ числа разделов на уровень:
# добавить раздел куда-либо можно только вместе с сознательным подъёмом потолка ЗДЕСЬ и записью в
# `LEVEL_SECTION_BUDGET_RAISES` (что и почему). Так же, как module-size-baseline держит рост модулей:
# `tests/unit/test_ceremony_budget.py` краснеет, если разделов на уровне стало больше потолка или
# уровень потерял потолок. Усыхание (раздел убрали) свободно — потолок держит рост, не размер.
LEVEL_SECTION_BUDGET = {0: 6, 1: 9, 2: 11, 3: 8}

# Лента подъёмов потолка церемонии. Каждый рост LEVEL_SECTION_BUDGET обязан оставить здесь запись:
# дата, уровень, старый->новый потолок, ПОЧЕМУ риск оправдывает лишний раздел. Пусто = потолки
# держатся на исходной фиксации (счётчики LEVEL_SECTIONS на момент заведения сторожа).
LEVEL_SECTION_BUDGET_RAISES = [
    {"date": "2026-09-21", "level": "L0..L3",
     "note": "исходная фиксация текущих счётчиков разделов (L0=6, L1=9, L2=11, L3=8) как потолков; "
             "рост любого — только вместе с записью здесь, объясняющей риск"},
]


def ceremony_budget_errors(level_sections=None, budget=None, required_fn=None):
    """Сторож против расползания церемонии. -> список ошибок ПРОДУКТОВЫМ языком (пусто = чисто).

    Три инварианта «сложность от риска, а не от полноты»:
      1. БЮДЖЕТ: число разделов на уровне не выше объявленного потолка (рост требует подъёма
         потолка + записи, что риск это оправдывает). Усыхание разрешено.
      2. МОНОТОННОСТЬ: обязательные разделы уровня N включают все разделы уровня N-1
         (required_fn(N) ⊇ required_fn(N-1)) — церемония только ПРИРАСТАЕТ с риском, ни один
         раздел нижнего уровня не пропадает. Проверяется НА САМОЙ функции сборки разделов
         (`required_fn`, по умолчанию `required_sections`): сегодня она кумулятивна структурно, но
         guard стережёт, чтобы будущая правка её такой и оставила — не декларация, а проверка.
      3. БЕЗ ДУБЛЕЙ: раздел объявлен ровно на одном уровне (иначе церемония «дублирует», а не
         добавляет — рост объёма без роста смысла).

    Чистая функция: level_sections/budget/required_fn можно передать искусственные (для пробы
    покраснения); по умолчанию берёт объявленные в модуле.
    """
    level_sections = LEVEL_SECTIONS if level_sections is None else level_sections
    budget = LEVEL_SECTION_BUDGET if budget is None else budget
    required_fn = required_sections if required_fn is None else required_fn
    errors = []
    levels = sorted(level_sections)
    # 1) бюджет на уровень
    for lv in levels:
        n = len(level_sections[lv])
        cap = budget.get(lv)
        if cap is None:
            errors.append(f"уровень L{lv}: {n} разделов, но потолка в бюджете нет — новый уровень "
                          f"обязан быть объявлен в LEVEL_SECTION_BUDGET с записью, что оправдывает "
                          f"его объём")
        elif n > cap:
            errors.append(f"уровень L{lv}: разделов {n} > потолок {cap} — церемония выросла без "
                          f"обоснования риском; подними потолок в LEVEL_SECTION_BUDGET и запиши в "
                          f"LEVEL_SECTION_BUDGET_RAISES, ПОЧЕМУ риск требует лишнего раздела")
    # 2) монотонность накопленного набора: N ⊇ N-1 (проверяем РЕАЛЬНО, на required_fn)
    for prev, lv in zip(levels, levels[1:]):
        lost = set(required_fn(prev)) - set(required_fn(lv))
        if lost:
            errors.append(f"уровень L{lv}: потерял разделы нижнего уровня {sorted(lost)} — "
                          f"церемония обязана ПРИРАСТАТЬ с риском, а не терять разделы (нарушена "
                          f"монотонность required_sections)")
    # 3) без дублей: идём снизу вверх, накапливая разделы
    seen = set()
    for lv in levels:
        secs = set(level_sections[lv])
        dup = seen & secs
        if dup:
            errors.append(f"уровень L{lv}: разделы {sorted(dup)} уже есть на нижнем уровне — "
                          f"церемония обязана ДОБАВЛЯТЬ с риском, а не дублировать (объём растёт "
                          f"без смысла)")
        seen |= secs
    return errors


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


# Тяжесть задачи, при которой прогон ДЕТЕРМИНИРОВАННО эскалирует незаявленный тип в ENGINEERING
# (зеркало ai_route.route: size medium+/risk medium+). Порог держим здесь же, где считаем уровень,
# чтобы раскрытие формы на `specify` опиралось ровно на тот же критерий, что эскалация на `run`.
_HEAVY_SIZES = ("medium", "large", "xl")
_HEAVY_RISKS = ("medium", "high", "critical")


def escalation_disclosure(signals):
    """Честное раскрытие ПРЕДВАРИТЕЛЬНОСТИ уровня/формы спеки — ДО того, как человек начал заполнять.

    Зеркало run_plan._escalation_disclosure, но об УРОВНЕ и РАЗДЕЛАХ формы, а не о гейтах прогона.

    Находка поля (obs 8a891ce7): когда тяжесть задачи (size/risk) НЕ заявлена, классификация даёт
    базовый L0 QUICK, и `specify` выдаёт форму на 6 разделов. Но `run`, получив явный сигнал тяжести,
    эскалирует в ENGINEERING и требует форму L1 (ещё 9 разделов). Человек заполнял НЕ ТУ форму и
    узнавал ПОСЛЕ. Раскрытие обязано быть на шаге `specify` — назвать уровень и разделы, которые
    добавит эскалация, ДО заполнения, а не задним числом.

    Предварительно = тип задачи НЕ заявлен И тяжесть НЕ заявлена И расчётный уровень базовый L0:
    при явном сигнале тяжести прогон поднимет уровень до L1 ENGINEERING. Если тяжесть заявлена (или
    тип задачи заявлен явно, или уровень уже выше L0) — форма выдаётся сразу нужного уровня, и
    раскрывать нечего (симметрично окончательному плану в #838: он раскрытие не несёт).

    -> (provisional: bool, disclosure: dict|None).
    """
    signals = dict(signals or {})
    size = (signals.get("size") or "").lower()
    risk = (signals.get("risk") or "").lower()
    heavy = size in _HEAVY_SIZES or risk in _HEAVY_RISKS
    declared_type = bool(signals.get("task_type"))
    if declared_type or heavy or classify(signals)["level"] != 0:
        return False, None
    esc_level = TASK_TYPE_LEVEL["ENGINEERING"]
    cur_sections = set(required_sections(0))
    disclosure = {
        "reason": ("тяжесть задачи (size/risk) не заявлена — уровень описания предварительный; "
                   "при явном сигнале тяжести прогон эскалирует, и форма спецификации вырастет "
                   "(не факт о коде: форма создаётся до правок)"),
        "level_if_escalated": LEVEL_NAME[esc_level],
        "sections_if_escalated": [s for s in required_sections(esc_level) if s not in cur_sections],
    }
    return True, disclosure


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


# Полевой замер (cockpit, 06.09.2026, фича free-tile-counter): сигналы задачи (--signals) НЕ
# переносились между шагами specify -> plan -> run. specify корректно поднимал spec до L1
# ENGINEERING, а plan без повторного --signals выдавал base_workflow=QUICK — уровень spec и workflow
# прогона считались независимо и расходились, из-за чего ENGINEERING-задача молча ехала как QUICK
# (судья code_review не запускался). Лечим у источника: specify СОХРАНЯЕТ сырые сигналы в spec.yaml
# (блок `signals:`), а plan/run их подхватывают, когда --signals на вызове не передан.
# `feature` — это сам wid (путь), `task_text` — текст конкретного вызова; к классификации уровня оба
# отношения не имеют и только зашумили бы перенос, поэтому в сохранённое не попадают.
_NONCARRY_SIGNAL_KEYS = ("feature", "task_text")


def _carryable_signals(signals):
    """Сырые сигналы задачи для сохранения в spec.yaml — без per-invocation шума (см. выше)."""
    return {k: v for k, v in dict(signals or {}).items() if k not in _NONCARRY_SIGNAL_KEYS}


def carried_signals(child_root, wid):
    """Сигналы, сохранённые предыдущим `specify` в features/<wid>/spec.yaml (блок `signals:`).

    -> dict. Пусто, если спеки нет, блока signals нет или файл битый. Fail-closed: сомнение = пустой
    перенос, поведение как раньше (QUICK по умолчанию). Репозитории без блока signals (созданные
    прежними версиями) работают как прежде — обратная совместимость.
    """
    sp = _spec_path(child_root, wid)
    if not sp.is_file():
        return {}
    try:
        import yaml
        doc = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — битый spec: перенос пуст, а не догадка
        return {}
    stored = doc.get("signals") if isinstance(doc, dict) else None
    return dict(stored) if isinstance(stored, dict) else {}


# Разделы spec.yaml, из которых складывается ЗАДАЧА писателю, когда `run`/`do` вызваны без текста
# задачи (только `--feature <wid>`). Порядок — как читает человек: сначала зачем и что, потом как
# проверим и чем ограничены. Метка на русском предваряет содержимое раздела в собранном тексте.
_TASK_SECTION_ORDER = (
    ("goal", "Цель"),
    ("scope", "Объём работ"),
    ("expected_behavior", "Ожидаемое поведение"),
    ("requirements", "Требования"),
    ("acceptance_criteria", "Критерии приёмки"),
    ("acceptance_scenarios", "Сценарии приёмки"),
    ("edge_cases", "Крайние случаи"),
    ("constraints", "Ограничения"),
    ("affected_files", "Затрагиваемые файлы"),
    ("implementation_plan", "План реализации"),
    ("write_scope", "Область записи"),
    ("verification_strategy", "Стратегия проверки"),
)


def task_from_spec(child_root, wid):
    """Собрать ТЕКСТ ЗАДАЧИ из features/<wid>/spec.yaml для писателя, когда `run`/`do` вызваны без
    позиционного текста (только `--feature`).

    ПОЧЕМУ. Движок строит задачу писателю ТОЛЬКО из позиционного аргумента (`ctx = task + профиль`),
    а НЕ из spec.yaml. Вызов `./ai-ops run --execute --feature <wid>` без текста давал писателю
    пустой блок «=== ЗАДАЧА ===»: он либо не писал ничего, либо авторил спеку про «задача пуста».
    Спека же — источник истины о том, что делать. Здесь берём заполненные (`status: complete`)
    разделы и складываем их в связный текст задачи. Fail-closed: нет спеки / нет ни одного
    заполненного раздела с содержимым -> "" (поведение как раньше, писатель получит пустую задачу и
    кит честно заблокирует).
    -> str (собранная задача) или "" .
    """
    sp = _spec_path(child_root, wid)
    if not sp.is_file():
        return ""
    try:
        import yaml
        doc = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — битый spec: пустая задача, а не догадка
        return ""
    sections = doc.get("sections") if isinstance(doc, dict) else None
    if not isinstance(sections, dict):
        return ""
    parts = []
    for sid, label in _TASK_SECTION_ORDER:
        entry = sections.get(sid)
        if not isinstance(entry, dict) or entry.get("status") != "complete":
            continue
        content = entry.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        parts.append(f"{label}: {content.strip()}")
    return "\n\n".join(parts)


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
            if _artifact_credits(p):
                provided[sid] = {"status": status,
                                 "note": f"засчитан по артефакту {rel}"}
                break
    return provided


def _artifact_credits(p):
    """#405: артефакт засчитывает раздел, только если он РЕАЛЬНЫЙ, а не просто существует.

    Прежде credit ставился по `p.exists()` — пустой или недописанный `plan.yaml` (0 байт/битый
    YAML, напр. при частичной записи между двумя прогонами) молча засчитывал раздел `complete`,
    из-за чего spec-complete (а с ним вердикт readiness) флипал от прогона к прогону. Теперь:
    - каталог (openspec/changes/<wid>) — засчитывает по существованию (как раньше);
    - `.yaml`-файл — только если непустой И парсится в непустую структуру (fail-closed: битый/пустой
      файл детерминированно НЕ засчитывает, а не «то complete, то нет»).
    """
    try:
        if p.is_dir():
            return True
        if not p.is_file():
            return False
        if p.suffix.lower() not in (".yaml", ".yml"):
            return p.exists()
        text = p.read_text(encoding="utf-8")
        if not text.strip():
            return False
        import yaml
        return bool(yaml.safe_load(text))
    except Exception:  # noqa: BLE001 — битый/непрочитанный артефакт -> не засчитывает (fail-closed)
        return False


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
        return _add_missing_sections(sp, cls, signals)
    sections = {sid: {"status": "missing", "content": "", "note": None}
                for sid in required_sections(level)}
    doc = {"schema_version": 1, "kind": "spec", "workitem_id": str(wid),
           "level": level, "level_name": cls["level_name"],
           "level_reason": cls["reason"],
           # Сырые сигналы задачи -> plan/run подхватят их без повторного --signals (см. коммент
           # у _NONCARRY_SIGNAL_KEYS: цена молчаливого отката ENGINEERING в QUICK).
           "signals": _carryable_signals(signals),
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


def _add_missing_sections(sp, cls, signals=None):
    """F-029: дописать в существующий spec.yaml разделы, которых требует расчётный уровень.
    -> (path, created=False, {"added": [...], "error": None|str}). Заполненное не трогается;
    разделы не удаляются.

    Битый/непрочитанный файл НЕ переписываем: молча заменить описанное заготовками — потеря работы
    человека, а это дороже незакрытого гейта. Тогда added пуст, и вызывающий сообщает правду
    («дописать не удалось»), а не выдаёт непроведённую правку за проведённую.

    Сигналы переносятся тем же правилом, что у plan/run: сохранённое — база, переданное на этом
    `specify` — сверху (явный --signals переопределяет). Если разделов дописывать нечего, но сигналы
    изменились, файл всё равно перезаписывается — иначе перенос уровня терялся бы при повторном
    specify без роста уровня."""
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
    stored = doc.get("signals") if isinstance(doc.get("signals"), dict) else {}
    merged = {**stored, **_carryable_signals(signals)}
    if not added and merged == stored:
        return sp, False, {"added": [], "error": None}
    if merged:
        doc["signals"] = merged
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
