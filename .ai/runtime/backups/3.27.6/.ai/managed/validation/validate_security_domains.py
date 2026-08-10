#!/usr/bin/env python3
"""Validate security-domains.yaml (v2.101, эпик Context Engineering, этап 5 — Security Pack).

Стережёт доменный контракт security-ревью:
  1. kind=security-domains, allowed_evidence_sources непуст;
  2. у каждого домена: id, applicability (signals|file_patterns), required_evidence,
     severity_policy.default из {critical,high,medium,low}, remediation_template;
  3. required_evidence ссылается только на allowed_evidence_sources;
  4. deterministic_checks — из известного набора (secret_scan/dependency_diff/injection_scan);
  5. id уникальны; поставляемый набор покрывает 12 обязательных доменов.

Использование:
  validate_security_domains.py [security/security-domains.yaml]
  validate_security_domains.py --selftest
Возврат 0 — ок, 1 — ошибки.
"""

import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
SEVERITIES = {"critical", "high", "medium", "low"}
KNOWN_CHECKS = {"secret_scan", "dependency_diff", "injection_scan"}
REQUIRED_DOMAINS = {
    "authentication", "authorization_idol", "input_validation", "secrets", "dependencies",
    "rate_limiting", "file_upload", "network_ssrf", "logging_monitoring", "deployment_config",
    "ai_prompt_injection", "data_isolation",
}


def check(data):
    errors = []
    if not isinstance(data, dict) or data.get("kind") != "security-domains":
        errors.append("kind должен быть 'security-domains'")
        return errors
    allowed = set(data.get("allowed_evidence_sources") or [])
    if not allowed:
        errors.append("allowed_evidence_sources пуст")
    domains = data.get("domains")
    if not isinstance(domains, list) or not domains:
        errors.append("domains должен быть непустым списком")
        return errors
    seen = set()
    for i, d in enumerate(domains):
        if not isinstance(d, dict) or not d.get("id"):
            errors.append(f"domains[{i}]: нет id"); continue
        did = d["id"]
        if did in seen:
            errors.append(f"дублирующийся домен: {did}")
        seen.add(did)
        app = d.get("applicability")
        if not isinstance(app, dict) or ("signals" not in app and "file_patterns" not in app):
            errors.append(f"{did}: applicability нужен signals или file_patterns")
        req = d.get("required_evidence") or []
        if not req:
            errors.append(f"{did}: required_evidence пуст (домен не закрыть без evidence)")
        for e in req:
            if allowed and e not in allowed:
                errors.append(f"{did}: required_evidence '{e}' не из allowed_evidence_sources")
        for c in d.get("deterministic_checks", []) or []:
            if c not in KNOWN_CHECKS:
                errors.append(f"{did}: неизвестная deterministic_check '{c}'")
        sev = (d.get("severity_policy", {}) or {}).get("default")
        if sev not in SEVERITIES:
            errors.append(f"{did}: severity_policy.default должен быть из {sorted(SEVERITIES)}")
        if not (d.get("remediation_template", {}) or {}).get("summary"):
            errors.append(f"{did}: нет remediation_template.summary")
    missing = REQUIRED_DOMAINS - seen
    if missing:
        errors.append(f"не хватает обязательных доменов: {sorted(missing)}")
    return errors


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    real = PKG / "security" / "security-domains.yaml"
    if real.exists():
        expect("поставляемый security-domains.yaml валиден",
               check(yaml.safe_load(real.read_text(encoding="utf-8"))) == [])
    good = {"kind": "security-domains", "allowed_evidence_sources": ["secret_scan", "security_reviewer"],
            "domains": [{"id": d, "applicability": {"signals": [], "file_patterns": [".*"]},
                         "required_evidence": ["secret_scan"], "severity_policy": {"default": "high"},
                         "remediation_template": {"summary": "fix"}} for d in REQUIRED_DOMAINS]}
    expect("синтетический полный набор валиден", check(good) == [])
    expect("не тот kind -> ошибка", any("security-domains" in e for e in check({"kind": "x"})))
    bad_ev = {"kind": "security-domains", "allowed_evidence_sources": ["secret_scan"],
              "domains": [{"id": "secrets", "applicability": {"file_patterns": [".*"]},
                           "required_evidence": ["magic"], "severity_policy": {"default": "high"},
                           "remediation_template": {"summary": "x"}}]}
    expect("required_evidence вне allowed -> ошибка", any("magic" in e for e in check(bad_ev)))
    bad_sev = {"kind": "security-domains", "allowed_evidence_sources": ["secret_scan"],
               "domains": [{"id": "secrets", "applicability": {"file_patterns": [".*"]},
                            "required_evidence": ["secret_scan"], "severity_policy": {"default": "meh"},
                            "remediation_template": {"summary": "x"}}]}
    expect("неизвестная severity -> ошибка", any("severity_policy" in e for e in check(bad_sev)))
    expect("неполный набор доменов -> ошибка", any("не хватает" in e for e in check(bad_sev)))

    print("validate_security_domains selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    path = Path(argv[0]) if argv else (PKG / "security" / "security-domains.yaml")
    if not path.exists():
        print(f"SECURITY-DOMAINS: файл не найден: {path}")
        return 1
    errs = check(yaml.safe_load(path.read_text(encoding="utf-8")))
    if errs:
        print("SECURITY-DOMAINS: ошибки:")
        for e in errs:
            print(f"  - {e}")
        return 1
    print(f"SECURITY-DOMAINS-OK: {path.name} — доменный контракт консистентен (12 доменов).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
