#!/usr/bin/env python3
"""gate_runtime.py (v3.6.5) — GateResult v2 runtime decision-слой.

Доводит GateResult v2 (schemas/gate-result-v2.schema.json, контракт с v3.1.8) до РАБОЧЕЙ логики
принятия решения по гейту: последовательность вердиктов ревьюера (включая `abstain`) ->
итоговый v2-результат со статусом pass/fail/abstain/not_applicable, с targeted-retry на abstain и
честным human-handoff после ПОВТОРНОГО abstain (не авто-pass и не авто-fail — эскалация человеку).

Правила:
  - not_applicable (по gate_policy для UI-гейта при ui_impact=none) -> status=not_applicable;
  - первый терминальный вердикт решает: pass -> pass; fail -> blocking fail (с blockers);
    warn -> калибровка (advisory-тир/evidence=pass -> abstain(advisory, не блок); иначе blocking fail);
  - abstain -> targeted retry (до max_retries); если и после ретраев abstain -> human_handoff,
    status=abstain (advisory: НЕ закрывает и НЕ проваливает гейт сам — ждёт человека).

Offline, детерминирован. Не трогает боевой _run_reviews (wiring — отдельно, за флагом). Валидирует
свой вывод через gate_result_v2.check.

CLI: gate_runtime.py --selftest
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / "tools"))
import gate_policy       # noqa: E402
import gate_result_v2    # noqa: E402


def decide(gate, signals, verdicts, evidence_status="not_run", max_retries=1,
           blockers=None, owner="reviewer", review_mode="read-only",
           tested_revision=None, evidence=None):
    """Вернуть (v2_result, meta). v3.6.7d: результат несёт tested_revision (ревизия, на которой
    вынесен вердикт) и реальный evidence — иначе can_deliver не сможет привязать вердикт к SHA."""
    r, meta = _decide_raw(gate, signals, verdicts, evidence_status, max_retries,
                          blockers, owner, review_mode)
    r["tested_revision"] = tested_revision
    if evidence:
        r["evidence"] = list(evidence)
    return r, meta


def _decide_raw(gate, signals, verdicts, evidence_status="not_run", max_retries=1,
                blockers=None, owner="reviewer", review_mode="read-only"):
    """Вернуть (v2_result, meta) по последовательности вердиктов ревьюера."""
    ui = gate in gate_policy.UI_GATES
    dec = {d["gate"]: d for d in gate_policy.candidate_policy(signals)}.get(gate) if ui else None
    evidence_mode = (dec or {}).get("evidence_mode", "ai_review")
    applicability = (dec or {}).get("applicability", "applicable")

    # not_applicable по политике (UI-гейт вне применимого impact)
    if applicability == "not_applicable":
        return _v2(gate, "not_applicable", blocking=True, applicability="not_applicable",
                   enforcement="advisory", evidence_mode=evidence_mode, owner=owner,
                   review_mode=review_mode, reason="гейт не применяется (ui_impact=none)",
                   reviewer_outcome=None), \
            {"retries": 0, "human_handoff": False, "terminal": "not_applicable"}

    retries = 0
    for i, v in enumerate(verdicts or []):
        if v == "pass":
            return _v2(gate, "pass", True, "applicable", "blocking", evidence_mode, owner, review_mode,
                       reason="чистый pass", reviewer_outcome="pass"), \
                {"retries": retries, "human_handoff": False, "terminal": "pass"}
        if v == "fail":
            return _v2(gate, "fail", True, "applicable", "blocking", evidence_mode, owner, review_mode,
                       reason="reviewer FAIL", blockers=blockers or [f"reviewer FAIL @ {gate}"],
                       reviewer_outcome="fail"), \
                {"retries": retries, "human_handoff": False, "terminal": "fail"}
        if v == "warn":
            if ui:
                action, reason = gate_policy.effective_review_outcome(gate, signals, "warn", evidence_status)
            else:
                action, reason = "block", "warn на блокирующем не-UI гейте"
            if action == "advisory":
                # advisory-abstain: субъективный warn, калибровка сняла блок -> решение ПРИНЯТО, доставка ОК
                return _v2(gate, "abstain", True, "applicable", "advisory", evidence_mode, owner,
                           review_mode, reason=f"warn -> advisory: {reason}", reviewer_outcome="warn"), \
                    {"retries": retries, "human_handoff": False, "terminal": "abstain-advisory"}
            return _v2(gate, "fail", True, "applicable", "blocking", evidence_mode, owner, review_mode,
                       reason=reason, blockers=blockers or [f"warn(block) @ {gate}: {reason}"],
                       reviewer_outcome="warn"), \
                {"retries": retries, "human_handoff": False, "terminal": "fail"}
        if v == "abstain":
            if retries < max_retries and i < len(verdicts) - 1:
                retries += 1            # targeted retry: следующий вердикт из последовательности
                continue
            # ретраи исчерпаны и всё ещё abstain по БЛОКИРУЮЩЕМУ гейту ->
            # решение НЕ принято -> blocking-abstain(pending_human), доставка ЗАПРЕЩЕНА, нужен человек.
            return _blocking_abstain(gate, evidence_mode, owner, review_mode,
                                     "повторный abstain по блокирующему гейту -> pending_human (не авто-pass/fail)"), \
                {"retries": retries, "human_handoff": True, "terminal": "abstain-handoff"}
    # пустая/незавершённая последовательность -> человек (fail-closed на неопределённости)
    return _blocking_abstain(gate, (dec or {}).get("evidence_mode", "ai_review"), owner, review_mode,
                             "нет вынесенного вердикта по блокирующему гейту -> pending_human"), \
        {"retries": retries, "human_handoff": True, "terminal": "no-verdict-handoff"}


def _blocking_abstain(gate, evidence_mode, owner, review_mode, reason):
    """Blocking-abstain: ревьюер воздержался по БЛОКИРУЮЩЕМУ гейту. reviewer_outcome=abstain,
    enforcement=blocking, resolution=pending_human, delivery_allowed=false, human_handoff=true —
    решение НЕ принято, доставка запрещена до человека (НЕ advisory)."""
    return _v2(gate, "abstain", True, "applicable", "blocking", evidence_mode, owner, review_mode,
               reason=reason, reviewer_outcome="abstain", resolution="pending_human",
               delivery_allowed=False, human_handoff=True,
               blockers=[f"human handoff required @ {gate}: {reason}"])


def can_deliver(results, expected_revision=None):
    """AND по всем гейтам: доставка разрешена, только если КАЖДЫЙ гейт delivery_allowed. Возвращает
    (bool, blockers). Blocking-abstain(pending_human) и fail держат доставку закрытой.

    v3.6.7d: если задан expected_revision — вердикт без tested_revision или с РАСХОЖДЕНИЕМ по ревизии
    НЕ разрешает доставку (нельзя принять вердикт, вынесенный на другом/неизвестном SHA)."""
    blockers = []
    for r in results or []:
        if not r.get("delivery_allowed", False):
            tag = "pending_human" if r.get("resolution") == "pending_human" else r.get("status")
            blockers.append(f"{r.get('gate')}: {tag}")
            continue
        if expected_revision is not None:
            tr = r.get("tested_revision")
            if not tr:
                blockers.append(f"{r.get('gate')}: вердикт без tested_revision (не привязан к SHA)")
            elif tr != expected_revision:
                blockers.append(f"{r.get('gate')}: tested_revision {str(tr)[:12]} != ожидаемый "
                                f"{str(expected_revision)[:12]} (вердикт на другом SHA)")
    return (len(blockers) == 0), blockers


def _v2(gate, status, blocking, applicability, enforcement, evidence_mode, owner, review_mode,
        reason="", blockers=None, reviewer_outcome=None, resolution=None,
        delivery_allowed=None, human_handoff=False):
    if resolution is None:
        resolution = "pending_human" if (status == "abstain" and enforcement == "blocking") else "resolved"
    if delivery_allowed is None:
        delivery_allowed = (status in ("pass", "not_applicable")
                            or (status == "abstain" and enforcement == "advisory"))
    r = {"schema_version": 2, "gate": gate, "status": status, "blocking": bool(blocking),
         "applicability": applicability, "enforcement": enforcement, "evidence_mode": evidence_mode,
         "human_signoff": False, "reviewer_outcome": reviewer_outcome, "resolution": resolution,
         "delivery_allowed": bool(delivery_allowed), "human_handoff": bool(human_handoff),
         "calibration_reason": reason, "owner": owner, "review_mode": review_mode,
         "tested_revision": None, "created_at": None, "expires_at": None,
         "override": None, "warnings": [], "evidence": []}
    if status == "fail":
        r["blockers"] = list(blockers or [f"fail @ {gate}"])
    else:
        r["blockers"] = list(blockers or [])
        if status == "abstain":
            r["warnings"] = [reason]
    return r


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    uf = {"ui_changed": True, "ui_impact": "user_facing"}
    intn = {"ui_changed": True, "ui_impact": "internal"}

    def _valid(r):
        return gate_result_v2.check(r) == []

    r, m = decide("ux_review", uf, ["pass"])
    expect("pass -> v2 pass, валиден", r["status"] == "pass" and _valid(r) and not m["human_handoff"])
    r, m = decide("ux_review", uf, ["fail"])
    expect("fail -> v2 fail(blocking)+blockers, валиден",
           r["status"] == "fail" and r["enforcement"] == "blocking" and r["blockers"] and _valid(r))
    # warn на internal ux (advisory-тир) -> abstain(advisory)
    r, m = decide("ux_review", intn, ["warn"])
    expect("warn на internal ux -> abstain(advisory), валиден",
           r["status"] == "abstain" and r["enforcement"] == "advisory" and _valid(r)
           and m["terminal"] == "abstain-advisory")
    # warn на user_facing без evidence -> fail(blocking)
    r, m = decide("ux_review", uf, ["warn"], evidence_status="not_run")
    expect("warn на user_facing без evidence -> fail(blocking)", r["status"] == "fail" and _valid(r))
    # warn на user_facing + evidence=pass -> abstain(advisory)
    r, m = decide("visual_regression", uf, ["warn"], evidence_status="pass")
    expect("warn + evidence=pass -> abstain(advisory)", r["status"] == "abstain" and _valid(r))
    # abstain -> retry -> pass
    r, m = decide("ux_review", uf, ["abstain", "pass"], max_retries=1)
    expect("abstain->retry->pass: pass, retries=1", r["status"] == "pass" and m["retries"] == 1)
    # abstain, abstain (retry исчерпан) -> BLOCKING human handoff (не advisory!)
    r, m = decide("ux_review", uf, ["abstain", "abstain"], max_retries=1)
    expect("повторный abstain -> blocking-abstain(pending_human), доставка ЗАПРЕЩЕНА, не advisory",
           r["status"] == "abstain" and r["enforcement"] == "blocking"
           and r["resolution"] == "pending_human" and r["delivery_allowed"] is False
           and r["human_handoff"] is True and m["human_handoff"] is True and _valid(r))
    # to_v1(blocking-abstain) -> fail (старый потребитель не примет advisory за разрешение)
    expect("to_v1(blocking-abstain) -> v1 fail (не warn)",
           gate_result_v2.to_v1(r)["status"] == "fail")
    # no verdict -> blocking handoff
    r, m = decide("ux_review", uf, [])
    expect("нет вердикта -> blocking-abstain(pending_human), доставка запрещена (fail-closed)",
           m["human_handoff"] is True and r["enforcement"] == "blocking"
           and r["delivery_allowed"] is False and _valid(r))
    # can_deliver: пройденные + advisory-abstain разрешают; blocking-abstain держит закрытым
    p, _ = decide("ux_review", uf, ["pass"])
    adv, _ = decide("ux_review", intn, ["warn"])
    ba, _ = decide("accessibility_review", uf, ["abstain", "abstain"])
    okd, bl = can_deliver([p, adv])
    expect("can_deliver: pass + advisory-abstain -> доставка разрешена", okd is True and bl == [])
    okd2, bl2 = can_deliver([p, ba])
    expect("can_deliver: blocking-abstain среди гейтов -> доставка ЗАПРЕЩЕНА",
           okd2 is False and any("pending_human" in b for b in bl2))

    # v3.6.7d: tested_revision привязка
    rr, _ = decide("ux_review", uf, ["pass"], tested_revision="sha1", evidence=["reviewed @ sha1"])
    expect("decide стампит tested_revision + evidence",
           rr["tested_revision"] == "sha1" and rr["evidence"] == ["reviewed @ sha1"])
    okr, _ = can_deliver([rr], expected_revision="sha1")
    expect("can_deliver: tested_revision совпадает с ожидаемым -> разрешено", okr is True)
    okr2, blr = can_deliver([rr], expected_revision="sha2")
    expect("can_deliver: tested_revision != ожидаемый -> ЗАПРЕЩЕНО (вердикт на другом SHA)",
           okr2 is False and any("другом SHA" in b for b in blr))
    okr3, blr3 = can_deliver([p], expected_revision="sha1")
    expect("can_deliver: вердикт без tested_revision при заданном expected -> ЗАПРЕЩЕНО",
           okr3 is False and any("не привязан к SHA" in b for b in blr3))
    # not_applicable по политике (ui_impact=none -> UI-гейт не применяется)
    r, m = decide("ux_review", {"ui_impact": "none"}, ["warn"])
    expect("ui_impact=none -> not_applicable, валиден",
           r["status"] == "not_applicable" and r["applicability"] == "not_applicable" and _valid(r))
    # адаптер v2->v1: abstain -> warn (консервативно для старых потребителей)
    r, _ = decide("ux_review", intn, ["warn"])
    v1 = gate_result_v2.to_v1(r)
    expect("to_v1(abstain) -> warn (старый потребитель fail-closed)", v1 and v1["status"] == "warn")

    print("gate_runtime selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
