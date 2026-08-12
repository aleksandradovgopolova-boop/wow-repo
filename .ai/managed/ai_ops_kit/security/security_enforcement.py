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

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
from ai_ops_kit.shared import _bootstrap  # noqa: E402,F401 — кладёт validation/ в sys.path
                                         # ДО плоских импортов ниже; повторный импорт того же
                                         # модуля ниже удалён ревизией 2026-08-11 как дубль
from ai_ops_kit.validation import validate_memory_governance as _mgp   # noqa: E402


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


def main(argv):
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
