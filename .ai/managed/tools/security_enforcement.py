#!/usr/bin/env python3
"""security_enforcement.py (v3.7.2) — RUNTIME enforcement трёх security-контрактов (не новые схемы).

Ревью: OWASP-ASI контракты (SupplyChainPin/MemoryGovernance/KeyLifecycle) есть как schema+validator+demo,
но НЕ подключены к runtime. Здесь — реальные примитивы принуждения, которые вызывают install-loader,
merge_memory и preflight:

  - verify_artifact(data, entry)      — СЧИТАЕТ sha256 скачанных байт и сверяет с pinned install_verify;
                                        signature/sigstore офлайн НЕ проверяемы -> block (честно);
  - enforce_memory_entry(entry, policy) — валидирует ОДНУ запись памяти по MemoryGovernancePolicy перед
                                        записью (provenance/expiry/no-self-ingestion);
  - key_preflight(klp, env, critical)  — проверяет наличие объявленных ключей в env; в critical-режиме
                                        отсутствие -> BLOCK, иначе warn; сверяет env_ref, не значения.

Никакой сети/секретов в коде. Только stdlib+pyyaml. CLI: security_enforcement.py --selftest
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / "validation"))
import validate_memory_governance as _mgp   # noqa: E402


def verify_artifact(data: bytes, entry: dict):
    """Реальная проверка скачанного артефакта против SupplyChainPin entry. -> (ok, reason)."""
    if not isinstance(entry, dict):
        return False, "entry не объект"
    iv = entry.get("install_verify")
    kind = entry.get("kind")
    if not iv:
        # исполняемый код без верификации недопустим; model (не код) — допустимо
        return (kind == "model"), ("model без install_verify — допустимо" if kind == "model"
                                   else f"{kind}: нет install_verify — установка запрещена")
    method, value = iv.get("method"), str(iv.get("value", ""))
    if method == "sha256":
        got = hashlib.sha256(data or b"").hexdigest()
        ok = bool(value) and got == value
        return ok, ("sha256 совпал" if ok else f"sha256 НЕ совпал: got {got[:12]}… != pinned {value[:12]}…")
    # signature/sigstore требуют внешнего верификатора -> офлайн НЕ доказуемо -> block (fail-closed)
    return False, f"{method}: офлайн-проверка недоступна -> block (нужен внешний verifier)"


def enforce_memory_entry(entry: dict, policy_meta=None):
    """Валидация ОДНОЙ записи памяти по MemoryGovernancePolicy перед записью. -> (ok, violations)."""
    wrap = {"schema_version": 1, "kind": "MemoryGovernancePolicy",
            "policy_id": (policy_meta or {}).get("policy_id", "MGP-000"), "entries": [entry]}
    errs = _mgp.check(wrap)
    # оставляем только ошибки уровня записи (не policy_id-обёртки)
    errs = [e for e in errs if "policy_id" not in e and "schema_version" not in e and "kind должен" not in e]
    return (not errs), errs


def key_preflight(klp: dict, env: dict, critical=False, now=None):
    """Preflight ключей ПЕРЕД provider-вызовом: (1) наличие в env объявленных ключей; critical -> block на
    отсутствии; (2) v3.7.13 TTL-ротация: если у ключа задан next_rotation_at и now > next_rotation_at —
    ротация просрочена (block при critical, иначе warning). now — ISO-дата 'YYYY-MM-DD' (детерминизм в
    тестах); None -> ротация не проверяется (только наличие). issued_at/rotated_at surfaced в checked."""
    keys = (klp or {}).get("keys") or []
    warnings, blocks, checked = [], [], []
    for k in keys:
        if not isinstance(k, dict):
            continue
        name, ref, ttl = k.get("name"), k.get("env_ref"), k.get("ttl_days")
        present = bool(ref and env.get(ref))
        nxt = k.get("next_rotation_at")
        rec = {"name": name, "env_ref": ref, "present": present, "ttl_days": ttl,
               "issued_at": k.get("issued_at"), "rotated_at": k.get("rotated_at"), "next_rotation_at": nxt}
        checked.append(rec)
        if not present:
            (blocks if critical else warnings).append(f"ключ '{name}' (env {ref}) отсутствует в окружении")
        elif now and isinstance(nxt, str) and nxt and now > nxt:
            msg = f"ключ '{name}': ротация просрочена (next_rotation_at {nxt} < now {now})"
            (blocks if critical else warnings).append(msg)
    return {"ready": not blocks, "critical": critical, "checked": checked,
            "warnings": warnings, "blocks": blocks,
            "ttl_note": "наличие + (при now) просрочка next_rotation_at; issued_at/rotated_at surfaced"}


def selftest():
    import yaml
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    # verify_artifact — реальный sha256
    data = b"skill-bytes-v1"
    digest = hashlib.sha256(data).hexdigest()
    okv, r = verify_artifact(data, {"kind": "skill", "install_verify": {"method": "sha256", "value": digest}})
    expect("verify_artifact: совпавший sha256 -> ok", okv is True)
    okv2, _ = verify_artifact(b"tampered", {"kind": "skill", "install_verify": {"method": "sha256", "value": digest}})
    expect("verify_artifact: подменённые байты -> НЕ ok (hash не совпал)", okv2 is False)
    okv3, _ = verify_artifact(data, {"kind": "mcp", "install_verify": {"method": "signature", "value": "x"}})
    expect("verify_artifact: signature офлайн -> block (fail-closed)", okv3 is False)
    okv4, _ = verify_artifact(b"", {"kind": "mcp"})
    expect("verify_artifact: код без install_verify -> block", okv4 is False)
    okv5, _ = verify_artifact(b"", {"kind": "model"})
    expect("verify_artifact: model без install_verify -> допустимо", okv5 is True)

    # enforce_memory_entry
    good = {"id": "m1", "provenance": {"origin": "user", "source_type": "human"},
            "expiry": {"mode": "ttl_days", "value": 30}, "self_ingested": False}
    okm, _ = enforce_memory_entry(good)
    expect("enforce_memory_entry: валидная запись -> ok", okm is True)
    bad = {"id": "m2", "provenance": {"origin": "agent", "source_type": "system"},
           "expiry": {"mode": "ttl_days", "value": 7}, "self_ingested": True}   # self без confirm
    okm2, viol = enforce_memory_entry(bad)
    expect("enforce_memory_entry: self-ingested без human_confirmed -> отклонена", okm2 is False and viol)

    # key_preflight
    klp = {"keys": [{"name": "a", "env_ref": "AKEY", "ttl_days": 90, "rotation_owner": "human"},
                    {"name": "b", "env_ref": "BKEY", "ttl_days": 30, "rotation_owner": "human"}]}
    rep = key_preflight(klp, {"AKEY": "x"}, critical=False)
    expect("key_preflight non-critical: отсутствие BKEY -> warning, ready=True", rep["ready"] and rep["warnings"])
    repc = key_preflight(klp, {"AKEY": "x"}, critical=True)
    expect("key_preflight critical: отсутствие ключа -> BLOCK, ready=False", repc["ready"] is False and repc["blocks"])
    repok = key_preflight(klp, {"AKEY": "x", "BKEY": "y"}, critical=True)
    expect("key_preflight critical: все ключи есть -> ready=True", repok["ready"] is True)
    # v3.7.13 TTL rotation timestamps
    klp_ttl = {"keys": [{"name": "K", "env_ref": "AKEY", "next_rotation_at": "2026-01-01"}]}
    r_over = key_preflight(klp_ttl, {"AKEY": "x"}, critical=True, now="2026-07-28")
    expect("key_preflight TTL: ротация просрочена + critical -> BLOCK",
           r_over["ready"] is False and any("ротация просрочена" in b for b in r_over["blocks"]))
    expect("key_preflight TTL: до срока -> ready", key_preflight(klp_ttl, {"AKEY": "x"}, critical=True, now="2025-12-01")["ready"] is True)
    expect("key_preflight TTL: без now ротация не проверяется", key_preflight(klp_ttl, {"AKEY": "x"}, critical=True)["ready"] is True)

    # интеграция с реальными demo-политиками
    kd = PKG / "examples" / "key-lifecycle-demo" / "KLP-001.yaml"
    if kd.exists():
        klp_real = yaml.safe_load(kd.read_text(encoding="utf-8"))
        expect("реальный KLP-001: preflight non-critical не падает",
               isinstance(key_preflight(klp_real, {}, critical=False)["checked"], list))

    print("security_enforcement selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    import os
    import yaml

    def _opt(n, d=None):
        return argv[argv.index(n) + 1] if n in argv else d

    if "--key-preflight" in argv:
        # реальный runtime-preflight: наличие объявленных ключей в env; --critical -> block на отсутствии
        klp = yaml.safe_load(Path(_opt("--key-preflight")).read_text(encoding="utf-8"))
        rep = key_preflight(klp, dict(os.environ), critical=("--critical" in argv))
        for w in rep["warnings"]:
            print(f"  WARN: {w}")
        for b in rep["blocks"]:
            print(f"  BLOCK: {b}")
        print(f"key-preflight ready={rep['ready']} (critical={rep['critical']})")
        return 0 if rep["ready"] else 1

    if "--verify-artifact" in argv:
        # реальная sha256-проверка скачанного файла против ожидаемого hash
        path, expected = _opt("--verify-artifact"), _opt("--expect-sha256")
        data = Path(path).read_bytes()
        ok, reason = verify_artifact(data, {"kind": "skill",
                                            "install_verify": {"method": "sha256", "value": expected or ""}})
        print(f"verify-artifact {path}: {'OK' if ok else 'BLOCK'} — {reason}")
        return 0 if ok else 1

    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
