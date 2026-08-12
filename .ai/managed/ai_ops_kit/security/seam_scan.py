#!/usr/bin/env python3
"""seam_scan.py (v3.7) — детектор «дефекта шва» по дифу (contract-between-layers, тихий отказ).

Класс дефекта (находка владельца): слой A пишет, слой B читает, тесты ОБОИХ зелёные, а контракт между
ними сломан. Три условия совпадают: (1) изменение затрагивает ШОВ (новая пара пишет/читает или смена
сквозного условия — напр. включили гейт); (2) тесты проверяют СЛОИ, а не переход (автор написал обе
стороны с одним заблуждением); (3) отказ ТИХИЙ (проглоченный 401, пустая карточка). Типы молчат
(несогласие в условиях, не в типах); ревью видит один PR, а шов ломается МЕЖДУ PR; юнит-тесты с
подменённой базой дают зелёное там, где живая база показала бы правду.

КОРЕНЬ (без смягчения): автор проверяет тот слой, который сам только что написал, и подмену, которую
сам же придумал. Это не про количество тестов — все они смотрят с ОДНОЙ стороны.

Шесть признаков, вычислимых прямо из дифа (без понимания предметной области):
  1. write_without_roundtrip           — запись без чтения-обратно/проверки round-trip;
  2. endpoint_precondition_change      — смена предусловия эндпоинта без аудита вызывающих;
  3. catch_without_happy_path          — новый проглатывающий catch/except без теста happy-path;  [ГЕЙТ]
  4. optional_field_in_shared_contract — новое необязательное поле в общем контракте;             [ГЕЙТ]
  5. external_stub_without_real_run    — новая подмена внешней системы без прогона против настоящей.[ГЕЙТ]
  6. surface_wiring_drift              — новый маршрут ядра / вызов клиента к пути (core<->wrapper<->
                                         client wiring drift, «/api/x умеет ядро, но обёртка не знает»).
Три из шести (#3/#4/#5) проверяются почти механически -> gate. #1/#2/#6 — advisory (эвристика диффа;
полная сверка core⊆обёртки⊆client — в validate_surface_wiring по манифесту поверхности).

Только stdlib. CLI: seam_scan.py <diff-file> [--json] | --selftest   (или diff из stdin: seam_scan.py -)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

GATEABLE = {"catch_without_happy_path", "optional_field_in_shared_contract", "external_stub_without_real_run"}

_WRITE = re.compile(r"\b(write_text|writeFileSync|writeFile|\.save\(|\.commit\(|INSERT\s+INTO|\.insert\(|"
                    r"requests\.post|\.post\(|fetch\([^)]*method\s*[:=]\s*['\"]POST|localStorage\.setItem)\b", re.I)
_READBACK = re.compile(r"\b(read_text|readFile|\.get\(|SELECT\s|requests\.get|assert|expect\(|toEqual|toBe\b|"
                       r"getItem|read_back|round.?trip|reload|refetch)\b", re.I)
_PRECOND = re.compile(r"(require[_-]?auth|@login_required|@requires?_|raise\s+\w*(Permission|Unauthorized|Forbidden)|"
                      r"if\s+not\s+\w*(authorized|allowed|permitted)|required\s*=\s*True|"
                      r"status_code\s*=\s*40[13]|abort\(40[13]|blocking\s*[:=]\s*true|gate.*enabled)", re.I)
# широкий/проглатывающий catch: bare except, except Exception/BaseException, JS catch(...){ / catch{
# (тело pass/return часто на СЛЕДУЮЩЕЙ строке дифа — ловим саму широкую ветку; happy-path тест = контр-довод)
_CATCH = re.compile(r"(\bexcept\s*:|\bexcept\s+(Exception|BaseException)\b|"
                    r"(\}\s*)?\bcatch\s*(\([^)]*\))?\s*\{)")
_OPT_TS = re.compile(r"^\s*[\w'\"]+\?\s*:")                       # TS optional field:  foo?: T
_OPT_PY = re.compile(r":\s*Optional\[|=\s*None\s*(#|$)")          # python Optional / default None
_STUB = re.compile(r"\b(mock|stub|fake|MagicMock|monkeypatch|jest\.mock|vi\.mock|responses\.add|"
                   r"unittest\.mock|patch\(|nock\(|msw|createStub|FakeClient)\b", re.I)
_REALRUN = re.compile(r"(integration|live|e2e|@pytest\.mark\.(integration|live|e2e)|real[_-]?(base|db|api|remote)|"
                      r"against.*(real|live)|no[_-]?mock|sha_verified|remote_sha)", re.I)
_TESTFILE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(_test\.|\.test\.|\.spec\.|test_.*\.py$)", re.I)
_HAPPY = re.compile(r"\b(def test_|it\(|test\(|describe\(|assert|expect\()", re.I)
_SHARED_CONTRACT = re.compile(r"(schema.*\.json$|\.schema\.|contract|interface\s|\btypes?\.(ts|py)$|\.proto$|openapi|dto)", re.I)
# #6 surface wiring: объявление маршрута в ядре и вызов клиента к пути. Полная сверка (core⊆обёртки,
# client⊆обёртки) — в validate_surface_wiring; здесь diff-эвристика двух твоих триггеров.
_ROUTE = re.compile(r"((@(app|router|blueprint|api)\.(get|post|put|delete|patch|route))\s*\(\s*['\"]/|"
                    r"\b(app|router|api|server)\.(get|post|put|delete|patch|use|add_route)\s*\(\s*['\"]/|"
                    r"\b(path|url|re_path|route)\s*\(\s*r?['\"]/|@(get|post|put|delete|patch)\s*\(\s*['\"]/)", re.I)
_CLIENT_CALL = re.compile(r"(\bfetch\s*\(\s*['\"]/|axios\.(get|post|put|delete|patch|request)\s*\(\s*['\"]/|"
                          r"requests\.(get|post|put|delete|patch)\s*\(\s*['\"][^'\"]*/|http\.(get|post)\s*\(\s*['\"]/|"
                          r"\bapi(Client|Fetch)?\.(get|post|put|delete|request)\s*\(\s*['\"]/)", re.I)
_ROUTES_REGISTRY = re.compile(r"(routes?\.(mjs|js|ts|py)$|endpoints?\.(mjs|js|ts|py)$|urls\.py$|"
                              r"domain-(routes|endpoints)|(^|/)router\.)", re.I)


def _added_lines(diff_text):
    """[(file, added_line)] из unified diff (только + строки, без +++ заголовков)."""
    out, cur = [], None
    for ln in diff_text.splitlines():
        if ln.startswith("+++ "):
            cur = ln[4:].strip()
            cur = cur[2:] if cur.startswith(("b/", "a/")) else cur
        elif ln.startswith("+") and not ln.startswith("+++"):
            out.append((cur, ln[1:]))
    return out


def scan_diff(diff_text):
    """-> {findings:[{signal,file,line,gateable}], evidence:{...}}."""
    added = _added_lines(diff_text)
    files = {f for f, _ in added if f}
    ev = {
        "readback_added": any(_READBACK.search(a) for _, a in added),
        "test_added": any((_TESTFILE.search(f or "") or _HAPPY.search(a)) for f, a in added),
        "realrun_added": any(_REALRUN.search(a) or _TESTFILE.search(f or "") and _REALRUN.search(a) for f, a in added)
                         or any(_REALRUN.search(a) for _, a in added),
        "nonprovider_caller_changed": len({f for f in files if f and not _SHARED_CONTRACT.search(f)}) > 1,
        "routes_registry_changed": any(_ROUTES_REGISTRY.search(f or "") for f in files),
    }
    findings = []

    def add(sig, f, a):
        findings.append({"signal": sig, "file": f, "line": a.strip()[:120], "gateable": sig in GATEABLE})

    for f, a in added:
        f = f or "?"
        if _WRITE.search(a):
            add("write_without_roundtrip", f, a)
        if _PRECOND.search(a):
            add("endpoint_precondition_change", f, a)
        if _CATCH.search(a):
            add("catch_without_happy_path", f, a)
        if (_SHARED_CONTRACT.search(f) and (_OPT_TS.search(a) or _OPT_PY.search(a))):
            add("optional_field_in_shared_contract", f, a)
        if _STUB.search(a):
            add("external_stub_without_real_run", f, a)
        if _ROUTE.search(a) or _CLIENT_CALL.search(a):
            add("surface_wiring_drift", f, a)
    return {"findings": findings, "evidence": ev, "files": sorted(files)}


def gate_decision(scan):
    """Гейт по 3 механическим признакам: находка без КОНТР-доказательства перехода -> block.
    catch -> нужен happy-path тест; optional-field -> нужен тест перехода; stub -> нужен real-run."""
    ev = scan["evidence"]
    counter = {
        "catch_without_happy_path": ev["test_added"],
        "optional_field_in_shared_contract": ev["test_added"],
        "external_stub_without_real_run": ev["realrun_added"],
    }
    blockers, advisories = [], []
    seen = set()
    for fnd in scan["findings"]:
        sig = fnd["signal"]
        if sig in GATEABLE:
            if not counter.get(sig, False) and sig not in seen:
                seen.add(sig)
                blockers.append(f"{sig}: шов без доказательства перехода ({fnd['file']})")
        else:
            advisories.append(f"{sig}: {fnd['file']}")
    # #6 подсказка: новый маршрут/вызов клиента, а файл-реестр маршрутов НЕ менялся -> сильный признак
    # wiring drift (ядро умеет, обёртка не знает). Триггер #1 из «дефектов одной сессии».
    if any(f["signal"] == "surface_wiring_drift" for f in scan["findings"]) and not ev.get("routes_registry_changed"):
        advisories.append("surface_wiring_drift: маршрут/вызов добавлен, но реестр маршрутов не менялся "
                          "-> проверь core⊆обёртки⊆client (validate_surface_wiring)")
    return {"block": bool(blockers), "blockers": blockers, "advisories": sorted(set(advisories)),
            "reason": ("шов затронут, но переход не доказан -> block" if blockers
                       else "gateable-швы имеют доказательство перехода или отсутствуют")}


def main(argv):
    args = [a for a in argv if not a.startswith("--") or a == "-"]
    if not args:
        print(__doc__)
        return 1
    # Ревизия 2026-08-11: было `open(...).read()` — дескриптор не закрывался.
    diff_text = sys.stdin.read() if args[0] == "-" else Path(args[0]).read_text(encoding="utf-8")
    scan = scan_diff(diff_text)
    dec = gate_decision(scan)
    if "--json" in argv:
        print(json.dumps({"scan": scan, "decision": dec}, ensure_ascii=False, indent=2))
    else:
        for b in dec["blockers"]:
            print(f"  BLOCK: {b}")
        for a in dec["advisories"]:
            print(f"  advisory: {a}")
        print(f"seam-scan: {'BLOCK' if dec['block'] else 'ok'} ({len(scan['findings'])} находок)")
    return 1 if dec["block"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
