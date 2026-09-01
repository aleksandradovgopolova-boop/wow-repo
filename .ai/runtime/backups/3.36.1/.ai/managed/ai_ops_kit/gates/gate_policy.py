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


def main(argv):
    ap = argparse.ArgumentParser(prog="gate_policy.py")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--shadow", metavar="JSON",
                    help="signals JSON -> напечатать shadow_diff (диагностика)")
    a = ap.parse_args(argv)
    if a.shadow:
        print(json.dumps(shadow_diff(json.loads(a.shadow)), ensure_ascii=False, indent=2))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
