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


def selftest():
    import tempfile
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import context_engine as ce  # noqa: E402

    # --- позитив: реальный view из build_context ---
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "src").mkdir()
        (root / "src" / "pricing.py").write_text("def apply_discount(a):\n    return a*0.9  # discount\n", encoding="utf-8")
        (root / "POLICY.md").write_text("# governing policy discount\n", encoding="utf-8")
        afp = {"id": "T-AFP", "kind": "AccessFilterPolicy",
               "rules": [{"role": "executor", "allowed_classes": ["public", "internal"]}]}
        allowed = {"public", "internal"}
        v = ce.build_context(root, "discount", "executor", sha="abc123", afp=afp,
                             v1_mandatory=["POLICY.md"], budget_tokens=10000)
        r = check_promotion_readiness(v, allowed, model_window=100000)
        expect("реальный чистый view -> ready=True, все 5 контрактов pass",
               r["ready"] is True and all(c["pass"] for c in r["contracts"].values()))

    # --- негативы (синтетические view) ---
    base = {"included": [{"file": "a.py", "data_class": "internal"}],
            "excluded_access": [], "mandatory_missing": [], "mandatory_excluded_access": [],
            "cache_key": "repo:x|sha:s1|afp:A:1|dcp:D:1|allowed:h|role:executor", "sha": "s1", "total_tokens": 500}
    expect("чистый синтетический -> ready", check_promotion_readiness(base, {"internal"}, 100000)["ready"])

    leak = {**base, "included": [{"file": "secret.py", "data_class": "secret"}]}
    r = check_promotion_readiness(leak, {"internal"}, 100000)
    expect("secret в included -> access_filter контракт FAIL",
           r["ready"] is False and not r["contracts"]["access_filter_before_retrieval"]["pass"])

    denied = {**base, "included": [{"file": "b.py", "data_class": "internal"}],
              "excluded_access": [{"file": "b.py", "data_class": "confidential"}]}
    r = check_promotion_readiness(denied, {"internal"}, 100000)
    expect("файл и в included, и в excluded_access -> no_denied контракт FAIL",
           not r["contracts"]["no_denied_filenames_in_payload"]["pass"])

    miss = {**base, "mandatory_missing": ["spec.md"]}
    expect("mandatory_missing -> applicable_rules контракт FAIL",
           not check_promotion_readiness(miss, {"internal"}, 100000)["contracts"]["applicable_rules_in_mandatory"]["pass"])
    mexc = {**base, "mandatory_excluded_access": ["policy.md"]}
    expect("mandatory запрещён access -> applicable_rules контракт FAIL",
           not check_promotion_readiness(mexc, {"internal"}, 100000)["contracts"]["applicable_rules_in_mandatory"]["pass"])

    nopin = {**base, "cache_key": "repo:x|role:executor", "sha": None}
    expect("cache_key без afp/dcp/sha + нет sha -> policy_hash_pinned контракт FAIL",
           not check_promotion_readiness(nopin, {"internal"}, 100000)["contracts"]["policy_hash_pinned_per_run"]["pass"])

    big = {**base, "total_tokens": 200000}
    r = check_promotion_readiness(big, {"internal"}, model_window=100000)
    expect("total > hard window -> hard_window контракт FAIL (декомпозиция/блок)",
           not r["contracts"]["hard_window_decompose_or_block"]["pass"] and r["ready"] is False)

    print("context_promotion_gate selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
