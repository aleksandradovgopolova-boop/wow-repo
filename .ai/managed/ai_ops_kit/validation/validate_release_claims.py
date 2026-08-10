#!/usr/bin/env python3
"""Release Truth Alignment (v3.8.1): machine-readable claims о ТЕКУЩЕМ релизе -> CI ловит ДРЕЙФ между
публичной поверхностью (README/ROADMAP/registry) и фактическим состоянием кода. Инвариант: источники
правды НЕ отстают от runtime (README v3.0.x при коде 3.8 — это дефект, который должен ловить CI).

Проверки (детерминированно, только stdlib+pyyaml):
  1. claims.version == файл VERSION;
  2. claims.checks_count == число `python3 ` команд в AGENTS.md (DERIVED — ловит устаревшие «91+»);
  3. claims.agents_count == число агентов в registry/agents.yaml (DERIVED);
  4. каждый файл из docs_must_reference_version РЕАЛЬНО содержит claims.version (публичная поверхность
     ссылается на текущую версию, а не на прошлый продукт);
  5. каждый runtime_capabilities-claim == фактический status в registry/runtimes.yaml (ловит дрейф
     заявленных возможностей адаптеров: parallel/resume и т.п.).

  validate_release_claims.py [registry/release-claims.yaml] | --selftest
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
DEFAULT = PKG / "registry" / "release-claims.yaml"


def derived_counts(pkg=PKG):
    """Фактические числа из источников (не из claims): (checks_count, agents_count)."""
    checks = 0
    try:
        for ln in (pkg / "AGENTS.md").read_text(encoding="utf-8").splitlines():
            if ln.strip().startswith("python3 "):
                checks += 1
    except OSError:
        pass
    agents = 0
    try:
        ad = yaml.safe_load((pkg / "registry" / "agents.yaml").read_text(encoding="utf-8"))
        if isinstance(ad, dict) and isinstance(ad.get("agents"), list):
            agents = len(ad["agents"])
    except OSError:
        pass
    return checks, agents


def _runtime_status(pkg, runtime, capability):
    try:
        rt = yaml.safe_load((pkg / "registry" / "runtimes.yaml").read_text(encoding="utf-8"))
    except OSError:
        return None
    runtimes = (rt or {}).get("runtimes") or {}
    return (((runtimes.get(runtime) or {}).get("capabilities") or {}).get(capability) or {}).get("status")


def check(data, pkg=PKG):
    e = []
    if not isinstance(data, dict) or data.get("registry_type") != "release-claims":
        return ["registry_type должен быть release-claims"]
    ver = str(data.get("version") or "").strip()
    vf = (pkg / "VERSION").read_text(encoding="utf-8").strip() if (pkg / "VERSION").exists() else None
    if vf and ver != vf:
        e.append(f"claims.version '{ver}' != VERSION '{vf}' (claims отстали от релиза)")
    checks, agents = derived_counts(pkg)
    if data.get("checks_count") != checks:
        e.append(f"claims.checks_count={data.get('checks_count')} != python3-проверок в AGENTS.md={checks} (устаревшее число)")
    if data.get("agents_count") != agents:
        e.append(f"claims.agents_count={data.get('agents_count')} != агентов в registry/agents.yaml={agents}")
    for name in (data.get("docs_must_reference_version") or []):
        p = pkg / name
        if not p.exists():
            e.append(f"{name}: файл из docs_must_reference_version не найден"); continue
        if ver and ver not in p.read_text(encoding="utf-8"):
            e.append(f"{name} не ссылается на текущую версию {ver} (публичная поверхность отстала от кода)")
    for rc in (data.get("runtime_capabilities") or []):
        r, cap, st = rc.get("runtime"), rc.get("capability"), rc.get("status")
        actual = _runtime_status(pkg, r, cap)
        if actual != st:
            e.append(f"runtime_capabilities {r}.{cap}: claim '{st}' != runtimes.yaml '{actual}' (дрейф заявленных возможностей)")
    # v3.9.1/v3.9.2: устаревшие маркеры «текущего статуса» НЕ должны присутствовать в публичной поверхности.
    # Сканируем НАСТРАИВАЕМЫЙ набор файлов (stale_marker_files — README/ROADMAP/NOTICE/docs/*), а не только
    # docs_must_reference_version. Так дрейф вроде «sequential-only» ловится и в docs/, не только вверху.
    _scan = list(data.get("stale_marker_files") or data.get("docs_must_reference_version") or [])
    for marker in (data.get("forbidden_stale_markers") or []):
        for name in _scan:
            p = pkg / name
            if p.exists() and marker in p.read_text(encoding="utf-8"):
                e.append(f"{name}: устаревший маркер текущего статуса '{marker}' — дрейф источника правды "
                         f"(удалить или пометить ИСТОРИЧЕСКИМ)")
    return e


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    checks, agents = derived_counts(PKG)
    vf = (PKG / "VERSION").read_text(encoding="utf-8").strip()
    # берём реальный runtime-claim, который точно есть (parallel_execution generic-orchestrator)
    st = _runtime_status(PKG, "generic-orchestrator", "parallel_execution")
    base = {"registry_type": "release-claims", "version": vf, "checks_count": checks,
            "agents_count": agents, "docs_must_reference_version": ["README.md"],
            "runtime_capabilities": [{"runtime": "generic-orchestrator",
                                      "capability": "parallel_execution", "status": st}]}
    expect("согласованные claims -> без ошибок", check(base) == [])
    expect("version != VERSION -> ошибка",
           any("claims отстали" in x for x in check({**base, "version": "0.0.0"})))
    expect("checks_count устарел -> ошибка",
           any("checks_count" in x for x in check({**base, "checks_count": 91})))
    expect("agents_count устарел -> ошибка",
           any("agents_count" in x for x in check({**base, "agents_count": 1})))
    expect("runtime capability дрейф -> ошибка",
           any("дрейф" in x for x in check({**base, "runtime_capabilities": [
               {"runtime": "generic-orchestrator", "capability": "parallel_execution", "status": "unsupported"}]})))
    # v3.9.1: forbidden_stale_markers — стейл-маркер, реально присутствующий в README -> ошибка
    expect("forbidden_stale_markers: присутствующий в README -> ошибка",
           any("устаревший маркер" in x for x in check({**base, "forbidden_stale_markers": ["Открытая"]})))
    expect("forbidden_stale_markers: отсутствующий -> без ошибки",
           not any("устаревший маркер" in x for x in check({**base, "forbidden_stale_markers": ["NONEXISTENT-STALE-XYZ-9999"]})))
    _bad_doc = {**base, "version": "vNONEXISTENT-9.9.9", "checks_count": checks, "agents_count": agents}
    # version mismatch И doc-reference оба сработают; проверяем doc-reference-ветку отдельно на фейковой версии,
    # подложив её и в VERSION-независимую проверку: используем несуществующую строку -> README её не содержит
    expect("README не ссылается на версию -> ошибка (среди прочих)",
           any("не ссылается на текущую версию" in x for x in check(_bad_doc)))

    if DEFAULT.exists():
        errs = check(yaml.safe_load(DEFAULT.read_text(encoding="utf-8")))
        expect("реальный release-claims.yaml согласован с кодом", errs == [])
        for x in errs:
            print("   -", x)

    print("validate_release_claims selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    path = Path(args[0]) if args else DEFAULT
    errs = check(yaml.safe_load(path.read_text(encoding="utf-8")))
    if errs:
        print(f"RELEASE-CLAIMS {path.name}: дрейф источников правды:")
        for x in errs:
            print(f"  - {x}")
        return 1
    print(f"RELEASE-CLAIMS-OK: {path.name} — публичная поверхность согласована с кодом.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
