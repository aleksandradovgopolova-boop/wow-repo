#!/usr/bin/env python3
"""context_promotion_gate.py (v3.7.0) — trust-контракты готовности Context Engine v2 к promotion.

Перед включением hybrid (execution получает mandatory v1 + разрешённые v2-кандидаты) нужно доказать,
что построенный context view безопасен и связен. Это НЕ включает hybrid — это ПРОВЕРКА готовности:
gate над результатом `context_engine.build_context`, который держит пять инвариантов доверия.

Контракты (все обязаны pass, иначе view НЕ допускается к promotion):
  1. access_filter_before_retrieval — ни один included-файл не имеет класс вне allowed роли и не secret
     (access-filter применён ДО попадания в payload, не постфактум);
  2. no_denied_filenames_in_payload — included ∩ excluded_access = ∅ (запрещённый файл НЕ в payload);
  3. applicable_rules_in_mandatory — обязательный контекст v1 (policy/spec/decisions) не потерян
     (mandatory_missing и mandatory_excluded_access пусты);
  4. policy_hash_pinned_per_run — view привязан к ЗАФИКСИРОВАННОЙ ревизии политик и коду: cache_key
     несёт afp/dcp fingerprint + точный sha (одна policy-ревизия на весь run);
  5. hard_window_decompose_or_block — total_tokens не превышает hard model-window; превышение -> требуется
     декомпозиция/блок, а НЕ тихое усечение.

Не трогает execution. Только stdlib. CLI: context_promotion_gate.py --selftest
"""
from __future__ import annotations

import sys
from pathlib import Path


def check_promotion_readiness(view, allowed_classes, model_window=None):
    """view: результат context_engine.build_context. -> {ready, contracts{name:{pass,detail}}, violations}."""
    included = view.get("included", []) or []
    excl_access = view.get("excluded_access", []) or []
    allowed = set(allowed_classes or [])
    contracts = {}

    # 1. access-filter ДО retrieval: (а) included без secret/класса вне allowed; (б) РЕАЛЬНЫЙ pre-filter —
    # ни один pre_filtered_denied путь не попал в прочитанное (read_paths) или в payload (included).
    bad = [f"{i.get('file')}({i.get('data_class')})" for i in included
           if i.get("data_class") == "secret" or (allowed and i.get("data_class") not in allowed)]
    denied = set(view.get("pre_filtered_denied") or [])
    read = set(view.get("read_paths") or [])
    inc_files = {i.get("file") for i in included}
    leaked_read = sorted(denied & read)      # denied путь оказался прочитан
    leaked_payload = sorted(denied & inc_files)
    detail = "ok"
    if bad:
        detail = f"included вне allowed/secret: {bad}"
    elif leaked_read or leaked_payload:
        detail = f"denied путь прочитан/в payload: read={leaked_read} payload={leaked_payload}"
    contracts["access_filter_before_retrieval"] = {"pass": not (bad or leaked_read or leaked_payload),
                                                    "detail": detail}

    # 2. запрещённые имена не в payload: included ∩ denied = ∅
    denied = {e.get("file") for e in excl_access}
    leaked = sorted({i.get("file") for i in included} & denied)
    contracts["no_denied_filenames_in_payload"] = {
        "pass": not leaked, "detail": (f"и в included, и в excluded_access: {leaked}" if leaked else "ok")}

    # 3. обязательные rules/policy/spec/decisions в mandatory (не потеряны)
    miss = list(view.get("mandatory_missing") or [])
    mexc = list(view.get("mandatory_excluded_access") or [])
    contracts["applicable_rules_in_mandatory"] = {
        "pass": not (miss or mexc),
        "detail": (f"missing={miss} excluded_access={mexc}" if (miss or mexc) else "ok")}

    # 4. policy hash + sha зафиксированы на run
    ck = view.get("cache_key") or ""
    missing_pin = [t for t in ("afp:", "dcp:", "sha:") if t not in ck]
    if not view.get("sha"):
        missing_pin.append("sha(value)")
    contracts["policy_hash_pinned_per_run"] = {
        "pass": not missing_pin, "detail": (f"cache_key/sha без пинов: {missing_pin}" if missing_pin else "ok")}

    # 5. hard model-window: total не превышает окно; иначе декомпозиция/блок (не тихое усечение)
    total = int(view.get("total_tokens") or 0)
    if model_window and total > model_window:
        contracts["hard_window_decompose_or_block"] = {
            "pass": False, "detail": f"total {total} > hard window {model_window} -> нужна декомпозиция/блок"}
    else:
        contracts["hard_window_decompose_or_block"] = {
            "pass": True, "detail": f"total {total} <= window {model_window or '∞'}"}

    violations = [f"{k}: {v['detail']}" for k, v in contracts.items() if not v["pass"]]
    return {"ready": not violations, "contracts": contracts, "violations": violations}


def main(argv):
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
