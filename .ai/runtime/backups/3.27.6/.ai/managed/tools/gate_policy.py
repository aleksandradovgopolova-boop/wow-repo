#!/usr/bin/env python3
"""gate_policy.py (v3.1.6) — риск-калиброванная UI-gate applicability + SHADOW-политика.

Находка Phase B (см. bench_lite): reviewer-false-fail сконцентрирован в 4 UI-review-гейтах,
которые трек VISUAL (registry/tracks.yaml) вешает разом по ОДНОМУ грубому сигналу `ui_changed`,
причём все четыре — blocking. Корень не в движке (engine_floor = 0 ложных блоков) и не в «плохой
модели», а во взаимодействии слишком общей gate-policy с неопределённостью ревьюера: warn /
сомнение / молчание по любому из четырёх гейтов блокирует всю правку.

Этот модуль вводит контекстную политику БЕЗ изменения боевого fail-closed:
  - таксономия сигналов: ui_impact (none/internal/user_facing/critical) и ui_change_kind;
  - current_policy(signals)  — что движок делает СЕЙЧАС (ui_changed -> все 4 гейта blocking);
  - candidate_policy(signals)— риск-калиброванная политика (applicability/enforcement/evidence_mode);
  - shadow_diff(signals)     — сравнение current vs candidate (что БЫ изменилось), без побочек.

SHADOW-режим: движок продолжает решать по current_policy. candidate только СЧИТАЕТСЯ рядом, чтобы
измерить проектируемое снижение false-fail и доказать безопасность ДО того, как менять enforcement
(это отдельный будущий инкремент + GateResult v2, т.к. схема v1 не знает not_applicable/abstain).

Инвариант безопасности (проверяется selftest): candidate НИКОГДА не мягче current для user_facing
и critical; ослабление допускается ТОЛЬКО в tier=internal и ТОЛЬКО для не-safety гейтов
(ux_review, visual_regression, design_system_usage). accessibility_review остаётся blocking всегда
(в internal — только автоматическая критическая часть; субъективное ревью — advisory по evidence_mode).

Только stdlib. selftest не требует сети/тулчейна.
"""
from __future__ import annotations

import argparse
import json
import sys

# 4 UI-review-гейта трека VISUAL (registry/tracks.yaml).
UI_GATES = ("ux_review", "design_system_usage", "accessibility_review", "visual_regression")
# accessibility остаётся blocking всегда: автоматические критические a11y-нарушения не ослабляем.
SAFETY_UI_GATES = ("accessibility_review",)

UI_IMPACT = ("none", "internal", "user_facing", "critical")
UI_CHANGE_KIND = ("token", "primitive", "component", "screen", "flow")

APPLICABILITY = ("applicable", "not_applicable")
ENFORCEMENT = ("advisory", "blocking")
EVIDENCE_MODE = ("deterministic", "ai_review", "hybrid", "human")


def derive_ui_impact(signals: dict) -> str:
    """Уровень UI-воздействия задачи с обратной совместимостью.

    Приоритет — явный ui_impact. Иначе legacy-путь: ui_changed=true БЕЗ явного уровня трактуется
    консервативно как user_facing (тождественно текущему поведению -> legacy-вызовы в shadow не
    затрагиваются). Нет UI -> none.
    """
    imp = signals.get("ui_impact")
    if imp in UI_IMPACT:
        return imp
    if signals.get("ui_changed"):
        return "user_facing"
    return "none"


def _decision(gate: str, applicability: str, enforcement: str, evidence_mode: str,
              reason: str, human_signoff: bool = False) -> dict:
    return {"kind": "GatePolicyDecision", "gate": gate, "applicability": applicability,
            "enforcement": enforcement, "evidence_mode": evidence_mode,
            "human_signoff": bool(human_signoff), "reason": reason}


def current_policy(signals: dict) -> list[dict]:
    """Что движок делает СЕЙЧАС: трек VISUAL по ui_changed вешает все 4 гейта как blocking."""
    on = bool(signals.get("ui_changed")) or derive_ui_impact(signals) != "none"
    out = []
    for g in UI_GATES:
        if on:
            out.append(_decision(g, "applicable", "blocking", "ai_review",
                                 "трек VISUAL: ui_changed -> обязательный blocking-гейт (текущая политика)"))
        else:
            out.append(_decision(g, "not_applicable", "advisory", "deterministic",
                                 "UI не затронут -> гейт не применяется"))
    return out


def candidate_policy(signals: dict) -> list[dict]:
    """Риск-калиброванная политика. Матрица по ui_impact (см. модульный docstring)."""
    impact = derive_ui_impact(signals)
    out = []

    if impact == "none":
        for g in UI_GATES:
            out.append(_decision(g, "not_applicable", "advisory", "deterministic",
                                 "нет UI-воздействия -> UI-гейты не применимы"))
        return out

    if impact == "internal":
        matrix = {
            "ux_review": ("applicable", "advisory", "ai_review",
                          "internal low-risk UI: субъективный UX -> advisory"),
            "visual_regression": ("applicable", "advisory", "deterministic",
                                  "internal: визуальный дифф информативен, но не блокирует"),
            "design_system_usage": ("applicable", "advisory", "hybrid",
                                    "internal: соответствие дизайн-системе -> advisory"),
            "accessibility_review": ("applicable", "blocking", "hybrid",
                                     "internal: автоматические критические a11y-нарушения блокируют; "
                                     "субъективная часть ревью -> advisory"),
        }
    elif impact == "user_facing":
        matrix = {
            "ux_review": ("applicable", "blocking", "ai_review",
                          "user-facing: состояния экрана обязательны"),
            "visual_regression": ("applicable", "blocking", "hybrid",
                                  "user-facing: визуальная регрессия блокирует"),
            "design_system_usage": ("applicable", "blocking", "hybrid",
                                    "user-facing: соответствие дизайн-системе обязательно"),
            "accessibility_review": ("applicable", "blocking", "hybrid",
                                     "user-facing: доступность обязательна"),
        }
    else:  # critical
        matrix = {
            "ux_review": ("applicable", "blocking", "human",
                          "critical flow: UX + обязательная человеческая проверка"),
            "visual_regression": ("applicable", "blocking", "deterministic",
                                  "critical: визуальная регрессия блокирует"),
            "design_system_usage": ("applicable", "blocking", "hybrid",
                                    "critical: соответствие дизайн-системе обязательно"),
            "accessibility_review": ("applicable", "blocking", "human",
                                     "critical flow: доступность + обязательная человеческая проверка"),
        }

    for g in UI_GATES:
        appl, enf, ev, reason = matrix[g]
        human = ev == "human" or (impact == "critical" and g in ("ux_review", "accessibility_review"))
        out.append(_decision(g, appl, enf, ev, reason, human_signoff=human))
    return out


def _effective(dec: dict) -> str:
    """Действующая сила решения: blocks | advises | skipped."""
    if dec["applicability"] == "not_applicable":
        return "skipped"
    return "blocks" if dec["enforcement"] == "blocking" else "advises"


def shadow_diff(signals: dict) -> dict:
    """Сравнение текущей и кандидатной политики. Чистая функция, без побочных эффектов.

    effect по каждому гейту: would_unblock | would_skip | would_apply | no_change.
    Боевой verdict определяется current_policy; candidate здесь только считается.
    """
    cur = {d["gate"]: d for d in current_policy(signals)}
    cand = {d["gate"]: d for d in candidate_policy(signals)}
    diffs = []
    for g in UI_GATES:
        ce, ne = _effective(cur[g]), _effective(cand[g])
        if ce == ne:
            effect = "no_change"
        elif ce == "blocks" and ne == "advises":
            effect = "would_unblock"
        elif ce == "blocks" and ne == "skipped":
            effect = "would_skip"
        elif ce == "skipped" and ne in ("blocks", "advises"):
            effect = "would_apply"
        else:
            effect = "changed"
        diffs.append({"gate": g, "current": ce, "candidate": ne, "effect": effect})
    return {"kind": "GatePolicyShadow", "ui_impact": derive_ui_impact(signals),
            "ui_change_kind": signals.get("ui_change_kind"),
            "gates": diffs,
            "differences": [d for d in diffs if d["effect"] != "no_change"]}


def candidate_blocking_gates(signals: dict) -> set:
    """Множество гейтов, которые ОСТАЛИСЬ БЫ blocking под кандидатной политикой (для проекции)."""
    return {d["gate"] for d in candidate_policy(signals) if _effective(d) == "blocks"}


def effective_review_outcome(gate: str, signals: dict, reviewer_status: str,
                             evidence_status: str = "not_run") -> tuple:
    """КАЛИБРОВАННОЕ enforcement (v3.1.8): как политика трактует вердикт ревьюера по UI-гейту.

    Возвращает (action, reason), action ∈ {'block', 'advisory'}. Вызывается ТОЛЬКО когда ревьюер
    вынес не-чистый вердикт (fail или warn на блокирующем гейте) — то есть в ситуации, которая СЕЙЧАС
    безусловно блокирует. Калибровка решает, блокировать ли по-прежнему.

    Правила (порядок важен — safety вперёд):
      1. evidence_status == 'fail'  -> BLOCK: детерминированное evidence показывает РЕАЛЬНУЮ регрессию/
         дефект (визуальный дифф / a11y-нарушение). Никогда не ослабляем.
      2. reviewer_status == 'fail'  -> BLOCK: жёсткий вердикт с конкретными blockers. Не трогаем.
      3. enforcement == 'advisory'  -> ADVISORY: internal low-risk не-safety гейт — субъективный warn
         не блокирует (accessibility в internal остаётся blocking -> сюда не попадёт).
      4. evidence_status == 'pass'  -> ADVISORY: механика подтверждена детерминированным evidence ->
         субъективный warn ревьюера не блокирует (evidence сильнее мнения).
      5. иначе (blocking-тир, нет evidence) -> BLOCK: fail-closed. Текущее строгое поведение сохранено.

    Не-UI гейты сюда не передаются (safety-гейты не трогаются). Легаси-путь: ui_changed без ui_impact
    -> user_facing + evidence not_run -> правило 5 -> BLOCK == сегодняшнее поведение (no-op).
    """
    if gate not in UI_GATES:
        return ("block", "не UI-гейт: калибровка не применяется")
    if evidence_status == "fail":
        return ("block", "детерминированное evidence: реальная регрессия/дефект")
    if reviewer_status == "fail":
        return ("block", "reviewer FAIL (жёсткий вердикт с blockers)")
    dec = {d["gate"]: d for d in candidate_policy(signals)}[gate]
    if dec.get("human_signoff"):
        return ("block", "critical flow: обязателен human-signoff — evidence не заменяет человека")
    if dec["enforcement"] == "advisory":
        return ("advisory", f"internal low-risk: гейт {gate} advisory -> субъективный warn не блокирует")
    if evidence_status == "pass":
        return ("advisory", "детерминированное evidence pass -> субъективный warn ревьюера не блокирует")
    return ("block", "blocking-тир без evidence -> fail-closed: warn блокирует")


def selftest() -> int:
    ok = True

    def expect(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and bool(cond)

    # --- таксономия / обратная совместимость -----------------------------------------------------
    expect("derive: явный ui_impact уважается",
           derive_ui_impact({"ui_impact": "internal"}) == "internal")
    expect("derive: legacy ui_changed=true -> user_facing (консервативно, == текущему поведению)",
           derive_ui_impact({"ui_changed": True}) == "user_facing")
    expect("derive: нет UI -> none", derive_ui_impact({"task_type": "QUICK"}) == "none")
    expect("derive: мусорный ui_impact игнорируется, падаем на legacy",
           derive_ui_impact({"ui_impact": "bogus", "ui_changed": True}) == "user_facing")

    # --- current policy: сегодня ui_changed -> все 4 blocking ------------------------------------
    cur = current_policy({"ui_changed": True})
    expect("current: ui_changed -> все 4 UI-гейта applicable+blocking",
           all(d["applicability"] == "applicable" and d["enforcement"] == "blocking" for d in cur)
           and {d["gate"] for d in cur} == set(UI_GATES))
    cur_none = current_policy({"task_type": "QUICK"})
    expect("current: нет UI -> все 4 not_applicable",
           all(d["applicability"] == "not_applicable" for d in cur_none))

    # --- candidate: none -> ничего не применяется ------------------------------------------------
    cand_none = candidate_policy({"ui_impact": "none"})
    expect("candidate none: все 4 not_applicable",
           all(d["applicability"] == "not_applicable" for d in cand_none))

    # --- candidate: internal -> 3 не-safety гейта advisory, a11y остаётся blocking ---------------
    cand_int = {d["gate"]: d for d in candidate_policy({"ui_changed": True, "ui_impact": "internal"})}
    expect("candidate internal: ux/visual/design_system -> advisory (ослаблено)",
           all(cand_int[g]["enforcement"] == "advisory"
               for g in ("ux_review", "visual_regression", "design_system_usage")))
    expect("candidate internal: accessibility остаётся blocking (safety не ослабляем)",
           cand_int["accessibility_review"]["enforcement"] == "blocking")

    # --- candidate: user_facing == current (никакого ослабления там, где риск высок) --------------
    cand_uf = candidate_policy({"ui_changed": True, "ui_impact": "user_facing"})
    expect("candidate user_facing: все 4 остаются blocking (== current, ноль ослабления)",
           all(d["enforcement"] == "blocking" and d["applicability"] == "applicable" for d in cand_uf))

    # --- candidate: critical -> blocking + human на ux и accessibility ---------------------------
    cand_cr = {d["gate"]: d for d in candidate_policy({"ui_changed": True, "ui_impact": "critical"})}
    expect("candidate critical: все 4 blocking",
           all(d["enforcement"] == "blocking" for d in cand_cr.values()))
    expect("candidate critical: ux + accessibility требуют human_signoff",
           cand_cr["ux_review"]["human_signoff"] and cand_cr["accessibility_review"]["human_signoff"])

    # --- ИНВАРИАНТ безопасности: candidate НЕ мягче current вне internal --------------------------
    def _softer(a, b):  # a мягче b?  blocks > advises > skipped
        rank = {"blocks": 2, "advises": 1, "skipped": 0}
        return rank[a] < rank[b]
    for impact in ("user_facing", "critical"):
        sig = {"ui_changed": True, "ui_impact": impact}
        cur_m = {d["gate"]: _effective(d) for d in current_policy(sig)}
        cand_m = {d["gate"]: _effective(d) for d in candidate_policy(sig)}
        expect(f"safety: candidate НЕ мягче current ни на одном гейте при impact={impact}",
               not any(_softer(cand_m[g], cur_m[g]) for g in UI_GATES))
    # в internal ослабление допускается ТОЛЬКО для не-safety гейтов
    sig_i = {"ui_changed": True, "ui_impact": "internal"}
    cur_i = {d["gate"]: _effective(d) for d in current_policy(sig_i)}
    cand_i = {d["gate"]: _effective(d) for d in candidate_policy(sig_i)}
    softened = {g for g in UI_GATES if _softer(cand_i[g], cur_i[g])}
    expect("safety: в internal ослаблены только не-safety гейты (accessibility НЕ ослаблен)",
           softened and not (softened & set(SAFETY_UI_GATES)))

    # --- shadow_diff: internal -> есть would_unblock; user_facing -> нет ослабляющих diff ---------
    sh_int = shadow_diff({"ui_changed": True, "ui_impact": "internal", "ui_change_kind": "component"})
    expect("shadow internal: есть would_unblock и ровно на 3 не-safety гейтах",
           {d["gate"] for d in sh_int["differences"] if d["effect"] == "would_unblock"}
           == {"ux_review", "visual_regression", "design_system_usage"})
    sh_uf = shadow_diff({"ui_changed": True, "ui_impact": "user_facing"})
    expect("shadow user_facing: ноль ослабляющих отличий (безопасность сохранена)",
           not [d for d in sh_uf["differences"] if d["effect"] in ("would_unblock", "would_skip")])
    sh_none = shadow_diff({"ui_impact": "none"})
    expect("shadow none: current и candidate совпадают (оба не применяют UI-гейты)",
           not sh_none["differences"])

    # --- candidate_blocking_gates для проекции bench ---------------------------------------------
    expect("blocking-set internal: accessibility остаётся, остальные ушли",
           candidate_blocking_gates(sig_i) == {"accessibility_review"})
    expect("blocking-set user_facing: все 4 остаются",
           candidate_blocking_gates({"ui_changed": True, "ui_impact": "user_facing"}) == set(UI_GATES))

    # --- effective_review_outcome (v3.1.8 калиброванное enforcement) -----------------------------
    uf = {"ui_changed": True, "ui_impact": "user_facing"}
    intn = {"ui_changed": True, "ui_impact": "internal"}
    # SAFETY: evidence fail всегда блокирует, даже если ревьюер молчал бы (warn)
    expect("eff: evidence=fail -> block (реальная регрессия), даже на internal",
           effective_review_outcome("visual_regression", intn, "warn", "fail")[0] == "block")
    # reviewer fail всегда блокирует
    expect("eff: reviewer=fail -> block (жёсткий вердикт)",
           effective_review_outcome("ux_review", uf, "fail", "not_run")[0] == "block")
    # internal не-safety + warn + нет evidence -> advisory (ослабление)
    expect("eff: internal ux + warn + no-evidence -> advisory",
           effective_review_outcome("ux_review", intn, "warn", "not_run")[0] == "advisory")
    # internal accessibility остаётся blocking (safety) -> warn без evidence блокирует
    expect("eff: internal accessibility + warn + no-evidence -> block (safety не ослаблен)",
           effective_review_outcome("accessibility_review", intn, "warn", "not_run")[0] == "block")
    # user_facing + warn + evidence pass -> advisory (механика подтверждена)
    expect("eff: user_facing + warn + evidence=pass -> advisory (evidence сильнее мнения)",
           effective_review_outcome("visual_regression", uf, "warn", "pass")[0] == "advisory")
    # user_facing + warn + нет evidence -> block (fail-closed, == сегодня)
    expect("eff: user_facing + warn + no-evidence -> block (fail-closed)",
           effective_review_outcome("ux_review", uf, "warn", "not_run")[0] == "block")
    # legacy: ui_changed без ui_impact -> user_facing -> block (no-op относительно сегодня)
    expect("eff: legacy ui_changed + warn + no-evidence -> block (no-op)",
           effective_review_outcome("ux_review", {"ui_changed": True}, "warn", "not_run")[0] == "block")
    # accessibility user_facing + warn + evidence pass -> advisory (авто-часть подтверждена)
    expect("eff: user_facing accessibility + warn + evidence=pass -> advisory",
           effective_review_outcome("accessibility_review", uf, "warn", "pass")[0] == "advisory")
    # но accessibility user_facing + warn + evidence=fail -> block (реальный дефект)
    expect("eff: user_facing accessibility + evidence=fail -> block (реальный a11y-дефект)",
           effective_review_outcome("accessibility_review", uf, "warn", "fail")[0] == "block")
    # critical ux/accessibility требуют human-signoff -> даже evidence=pass НЕ снимает warn
    crit = {"ui_changed": True, "ui_impact": "critical"}
    expect("eff: critical ux + warn + evidence=pass -> block (human-signoff обязателен)",
           effective_review_outcome("ux_review", crit, "warn", "pass")[0] == "block")
    expect("eff: critical accessibility + warn + evidence=pass -> block (human-signoff)",
           effective_review_outcome("accessibility_review", crit, "warn", "pass")[0] == "block")
    # но critical visual (без human-signoff) + evidence=pass -> advisory (механика подтверждена)
    expect("eff: critical visual + warn + evidence=pass -> advisory (нет human-signoff у visual)",
           effective_review_outcome("visual_regression", crit, "warn", "pass")[0] == "advisory")

    print("gate_policy selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    ap = argparse.ArgumentParser(prog="gate_policy.py")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--shadow", metavar="JSON",
                    help="signals JSON -> напечатать shadow_diff (диагностика)")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.shadow:
        print(json.dumps(shadow_diff(json.loads(a.shadow)), ensure_ascii=False, indent=2))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
