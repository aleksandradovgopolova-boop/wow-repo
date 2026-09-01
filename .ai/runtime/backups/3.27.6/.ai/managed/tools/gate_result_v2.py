#!/usr/bin/env python3
"""gate_result_v2.py (v3.1.8) — GateResult v2 + миграционный адаптер v2<->v1.

Калиброванное UI-enforcement вводит два состояния, которых нет в gate-result v1
(status ∈ pass|warn|fail): `not_applicable` (гейт не применялся) и `abstain` (ревьюер воздержался /
субъективное сомнение, которое калибровка НЕ считает блоком). Схема v1 — публичный контракт;
молча расширять её enum нельзя (breaking для потребителей, делающих exhaustive match). Поэтому
v2 — отдельный файл (schemas/gate-result-v2.schema.json), а этот модуль даёт:

  - validate(v2)      — структурная проверка v2;
  - to_v1(v2)         — деградация для СТАРЫХ потребителей: not_applicable -> None (опустить гейт);
                        abstain -> warn (консервативно: старый потребитель остаётся fail-closed —
                        калибровку понимают только v2-осведомлённые потребители);
  - calibrated_view(...) — построить v2-результат из исходного v1-гейта + решения политики +
                        калиброванного действия (для отчёта: почему enforcement изменился).

Только stdlib.  CLI: gate_result_v2.py <result.json> [--json] | --selftest
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

STATUS_V2 = {"pass", "warn", "fail", "not_applicable", "abstain"}
STATUS_CHECK = {"pass", "warn", "fail"}
APPLICABILITY = {"applicable", "not_applicable"}
ENFORCEMENT = {"advisory", "blocking"}
EVIDENCE_MODE = {"deterministic", "ai_review", "hybrid", "human"}
REVIEW_MODE = {"read-only", "writer"}

RESOLUTION = {"resolved", "pending_human"}
REVIEWER_OUTCOME = {"pass", "warn", "fail", "abstain", None}

_ALLOWED = {"schema_version", "gate", "status", "blocking", "applicability", "enforcement",
            "evidence_mode", "human_signoff", "scope", "checks", "blockers", "warnings", "evidence",
            "calibration_reason", "affected_files", "tested_revision", "owner", "review_mode",
            "created_at", "expires_at", "override",
            "reviewer_outcome", "resolution", "delivery_allowed", "human_handoff"}
_REQUIRED = ("schema_version", "gate", "status", "blocking", "applicability", "enforcement",
             "owner", "review_mode")


def check(data: dict):
    e = []
    if not isinstance(data, dict):
        return ["gate-result-v2 не объект"]
    for k in data:
        if k not in _ALLOWED:
            e.append(f"лишний ключ: {k}")
    for k in _REQUIRED:
        if k not in data:
            e.append(f"нет обязательного ключа: {k}")
    if data.get("schema_version") != 2:
        e.append("schema_version должен быть 2")
    if data.get("status") not in STATUS_V2:
        e.append(f"status ∉ {sorted(STATUS_V2)}")
    if not isinstance(data.get("blocking"), bool):
        e.append("blocking должен быть bool")
    if data.get("applicability") not in APPLICABILITY:
        e.append(f"applicability ∉ {sorted(APPLICABILITY)}")
    if data.get("enforcement") not in ENFORCEMENT:
        e.append(f"enforcement ∉ {sorted(ENFORCEMENT)}")
    if "evidence_mode" in data and data["evidence_mode"] not in EVIDENCE_MODE:
        e.append(f"evidence_mode ∉ {sorted(EVIDENCE_MODE)}")
    if "review_mode" in data and data["review_mode"] not in REVIEW_MODE:
        e.append(f"review_mode ∉ {sorted(REVIEW_MODE)}")
    for c in data.get("checks", []) or []:
        if not isinstance(c, dict) or not c.get("id") or c.get("status") not in STATUS_CHECK:
            e.append("check требует id + status∈[pass,warn,fail]")
    if "resolution" in data and data["resolution"] not in RESOLUTION:
        e.append(f"resolution ∉ {sorted(RESOLUTION)}")
    if "reviewer_outcome" in data and data["reviewer_outcome"] not in REVIEWER_OUTCOME:
        e.append("reviewer_outcome ∉ [pass,warn,fail,abstain,null]")
    if "delivery_allowed" in data and not isinstance(data["delivery_allowed"], bool):
        e.append("delivery_allowed должен быть bool")
    if "human_handoff" in data and not isinstance(data["human_handoff"], bool):
        e.append("human_handoff должен быть bool")
    # согласованность калиброванной семантики:
    st = data.get("status")
    enf = data.get("enforcement")
    if st == "not_applicable" and data.get("applicability") != "not_applicable":
        e.append("status=not_applicable требует applicability=not_applicable")
    # v3.6.7: abstain теперь ДВУХ видов — reviewer_outcome отделён от enforcement.
    #   advisory-abstain = субъективный warn, калибровка сняла блок -> resolved, доставка разрешена;
    #   blocking-abstain = ревьюер воздержался по БЛОКИРУЮЩЕМУ гейту -> pending_human, доставка ЗАПРЕЩЕНА.
    if st == "abstain":
        res = data.get("resolution", "resolved")
        da = data.get("delivery_allowed", enf == "advisory")
        hh = data.get("human_handoff", enf == "blocking")
        if enf == "advisory":
            if res != "resolved" or da is not True or hh is not False:
                e.append("advisory-abstain требует resolution=resolved, delivery_allowed=true, human_handoff=false")
        elif enf == "blocking":
            if res != "pending_human" or da is not False or hh is not True:
                e.append("blocking-abstain требует resolution=pending_human, delivery_allowed=false, human_handoff=true (решение НЕ принято -> человек)")
        else:
            e.append(f"abstain: enforcement ∉ {sorted(ENFORCEMENT)}")
    if st == "fail":
        if not data.get("blockers"):
            e.append("status=fail требует непустой blockers (симметрия честности с v1)")
        if data.get("delivery_allowed") is True:
            e.append("status=fail несовместим с delivery_allowed=true")
    return e


def to_v1(v2: dict):
    """Деградация v2 -> v1 для старых потребителей. Возвращает v1-словарь ИЛИ None (гейт опустить)."""
    st = v2.get("status")
    if st == "not_applicable":
        return None                              # гейт не применялся -> нет v1-записи
    # v3.6.7: blocking-abstain (pending_human) — это НЕ мягкий warn, а НЕпринятое решение,
    # блокирующее доставку -> для старого v1-потребителя деградируем в fail (fail-closed),
    # чтобы advisory-семантика не была принята за разрешение продолжать. advisory-abstain -> warn.
    blocking_abstain = st == "abstain" and v2.get("enforcement") == "blocking"
    if blocking_abstain:
        v1_status = "fail"
    elif st == "abstain":
        v1_status = "warn"
    else:
        v1_status = st
    out = {"schema_version": 1, "gate": v2.get("gate"), "status": v1_status,
           "blocking": bool(v2.get("blocking")), "owner": v2.get("owner", "unknown"),
           "review_mode": v2.get("review_mode", "read-only")}
    for k in ("scope", "checks", "blockers", "warnings", "evidence", "affected_files",
              "tested_revision", "created_at", "expires_at", "override"):
        if k in v2:
            out[k] = v2[k]
    if blocking_abstain and not out.get("blockers"):
        out["blockers"] = [f"human handoff required @ {v2.get('gate')} (reviewer abstain на блокирующем гейте)"]
    return out


def calibrated_view(gate: str, orig_blocking: bool, decision: dict, reviewer_status: str,
                    action: str, reason: str, owner="unknown", review_mode="read-only",
                    tested_revision=None, blockers=None, evidence=None) -> dict:
    """Построить GateResult v2 из исходного гейта + решения gate_policy + калиброванного действия.

    action ('block'|'advisory') из gate_policy.effective_review_outcome. status:
      - block  + reviewer fail  -> fail (несёт blockers);
      - block  (иначе)          -> fail (fail-closed блок);
      - advisory                -> abstain (субъективное сомнение, не блокирует).
    """
    appl = decision.get("applicability", "applicable")
    if appl == "not_applicable":
        status, enforcement = "not_applicable", "advisory"
    elif action == "advisory":
        status, enforcement = "abstain", "advisory"
    else:
        status, enforcement = "fail", "blocking"
    # v3.6.7: reviewer_outcome отделён от enforcement; delivery_allowed/resolution выведены явно.
    delivery_allowed = status in ("not_applicable", "abstain")   # advisory-abstain/na не блокируют
    out = {"schema_version": 2, "gate": gate, "status": status, "blocking": bool(orig_blocking),
           "applicability": appl, "enforcement": enforcement,
           "evidence_mode": decision.get("evidence_mode", "ai_review"),
           "human_signoff": bool(decision.get("human_signoff")),
           "reviewer_outcome": reviewer_status, "resolution": "resolved",
           "delivery_allowed": delivery_allowed, "human_handoff": False,
           "calibration_reason": reason, "owner": owner, "review_mode": review_mode,
           "tested_revision": tested_revision, "created_at": None, "expires_at": None,
           "override": None, "warnings": [], "evidence": evidence or []}
    if status == "fail":
        out["blockers"] = list(blockers or [f"reviewer {reviewer_status} @ {gate}"])
    else:
        out["blockers"] = []
        if status == "abstain":
            out["warnings"] = [f"reviewer {reviewer_status}, но {reason}"]
    return out


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    valid_abstain = {"schema_version": 2, "gate": "visual_regression", "status": "abstain",
                     "blocking": True, "applicability": "applicable", "enforcement": "advisory",
                     "evidence_mode": "deterministic", "owner": "r", "review_mode": "read-only",
                     "blockers": [], "warnings": ["w"]}
    expect("валидный abstain (advisory) проходит", check(valid_abstain) == [])
    expect("abstain(blocking) БЕЗ pending_human-полей -> ошибка (нельзя выдать advisory за разрешение)",
           any("blocking-abstain" in x for x in check({**valid_abstain, "enforcement": "blocking"})))

    # v3.6.7: КОРРЕКТНЫЙ blocking-abstain (pending_human) валиден и запрещает доставку
    blocking_abstain = {"schema_version": 2, "gate": "accessibility_review", "status": "abstain",
                        "blocking": True, "applicability": "applicable", "enforcement": "blocking",
                        "evidence_mode": "hybrid", "owner": "a11y", "review_mode": "read-only",
                        "reviewer_outcome": "abstain", "resolution": "pending_human",
                        "delivery_allowed": False, "human_handoff": True,
                        "blockers": ["reviewer abstain x2 -> человек"], "warnings": []}
    expect("blocking-abstain(pending_human, delivery_allowed=false, handoff=true) валиден",
           check(blocking_abstain) == [])
    expect("blocking-abstain c delivery_allowed=true -> ошибка",
           any("blocking-abstain" in x for x in check({**blocking_abstain, "delivery_allowed": True})))
    v1ba = to_v1(blocking_abstain)
    expect("to_v1(blocking-abstain) -> v1 FAIL с blockers (не мягкий warn)",
           v1ba["status"] == "fail" and v1ba["blockers"])

    na = {"schema_version": 2, "gate": "ux_review", "status": "not_applicable", "blocking": True,
          "applicability": "not_applicable", "enforcement": "advisory", "owner": "r",
          "review_mode": "read-only"}
    expect("валидный not_applicable проходит", check(na) == [])
    expect("not_applicable при applicability=applicable -> ошибка",
           any("not_applicable" in x for x in check({**na, "applicability": "applicable"})))

    fail_v2 = {"schema_version": 2, "gate": "ux_review", "status": "fail", "blocking": True,
               "applicability": "applicable", "enforcement": "blocking", "owner": "r",
               "review_mode": "read-only", "blockers": ["нет состояний экрана"]}
    expect("валидный fail c blockers проходит", check(fail_v2) == [])
    expect("fail без blockers -> ошибка",
           any("blockers" in x for x in check({**fail_v2, "blockers": []})))
    expect("лишний ключ -> ошибка", any("лишний" in x for x in check({**fail_v2, "junk": 1})))

    # адаптер v2 -> v1
    expect("to_v1(not_applicable) -> None (гейт опущен)", to_v1(na) is None)
    v1a = to_v1(valid_abstain)
    expect("to_v1(abstain) -> v1 warn (консервативно, старый потребитель fail-closed)",
           v1a["status"] == "warn" and v1a["schema_version"] == 1)
    v1f = to_v1(fail_v2)
    expect("to_v1(fail) -> v1 fail с blockers", v1f["status"] == "fail" and v1f["blockers"])

    # calibrated_view из решения политики
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import gate_policy  # noqa: E402
    dec_adv = {d["gate"]: d for d in gate_policy.candidate_policy(
        {"ui_changed": True, "ui_impact": "internal"})}["ux_review"]
    view_adv = calibrated_view("ux_review", True, dec_adv, "warn", "advisory",
                               "internal low-risk -> advisory")
    expect("calibrated_view(advisory) -> abstain + валиден",
           view_adv["status"] == "abstain" and check(view_adv) == [])
    dec_block = {d["gate"]: d for d in gate_policy.candidate_policy(
        {"ui_changed": True, "ui_impact": "user_facing"})}["ux_review"]
    view_block = calibrated_view("ux_review", True, dec_block, "warn", "block",
                                 "fail-closed", blockers=["no evidence"])
    expect("calibrated_view(block) -> fail + валиден + blockers",
           view_block["status"] == "fail" and check(view_block) == [] and view_block["blockers"])

    # drift-guard против схемы
    try:
        sch = json.loads((Path(__file__).resolve().parents[1] / "schemas"
                          / "gate-result-v2.schema.json").read_text(encoding="utf-8"))
        enum = set(sch["properties"]["status"]["enum"])
        expect("enum status совпадает со схемой (нет дрейфа)", enum == STATUS_V2)
    except Exception as ex:
        expect(f"схема читается ({ex})", False)

    print("gate_result_v2 selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    data = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    errors = check(data)
    if "--json" in argv:
        print(json.dumps({"errors": errors}, ensure_ascii=False, indent=2))
    elif errors:
        print("GATE-RESULT-V2: ошибки:")
        for x in errors:
            print(f"  - {x}")
    else:
        print("GATE-RESULT-V2-OK: структура валидна.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
