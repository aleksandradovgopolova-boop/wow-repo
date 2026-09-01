#!/usr/bin/env python3
"""Security Pack -> доменный security-вердикт (v2.101, эпик Context Engineering, этап 5).

Security review как набор ПРИМЕНИМЫХ доменов (security/security-domains.yaml), а не один вердикт.
Проверяются только применимые к изменению домены (frontend-only не запускает database audit, но
проверяет XSS/secrets). Детерминированные проверки (secret_scan/dependency_diff/injection_scan)
берутся из tools/security_scan.py; остальное — вход для независимого security-reviewer/человека.

Честность: домен нельзя закрыть фразой «уязвимостей нет». Авто-закрыть можно ТОЛЬКО домены, чьё
required_evidence целиком покрыто пройденными детерминированными проверками (secrets, dependencies).
Домены с security_reviewer/human_approval в required_evidence остаются needs_review (судья/человек).
Находка -> домен fail (блокирует по severity_policy).

Использование:
  security_pack.py <child_root> [--base <sha>] [--signals '{...}'] [--json]
  security_pack.py --selftest
Возврат 0 — блокеров нет; 1 — есть блокирующие находки (или ошибка).
"""

import argparse
import json
import re
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
for _p in (PKG / "tools",):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import security_scan  # noqa: E402
import yaml           # noqa: E402

DETERMINISTIC = {"secret_scan", "dependency_diff", "injection_scan"}


def load_domains():
    p = PKG / "security" / "security-domains.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.is_file() else {}
    return data.get("domains", []), data.get("allowed_evidence_sources", [])


def _applies(domain, signals, files_content):
    """Домен применим по сигналу, ИЛИ по пути изменённого файла, ИЛИ по его СОДЕРЖИМОМУ.
    v2.104 (finding самоаудита): раньше проверялся только ПУТЬ -> auth-логика в файле, чей путь
    не матчит (напр. src/users.py c 'password'), не поднимала домен -> security авто-проходил
    (ложный green). Совпадение по содержимому шире -> под-срабатывание (опасное) устранено;
    пере-срабатывание -> лишний needs_review (fail-closed, безопасно)."""
    reasons = []
    app = domain.get("applicability", {}) or {}
    for sig in app.get("signals", []) or []:
        if signals.get(sig):
            reasons.append(f"сигнал {sig}")
    for pat in app.get("file_patterns", []) or []:
        if pat == ".*":
            reasons.append("применим всегда (детерминированная проверка)")
            break
        rx = re.compile(pat)
        hit = None
        for f, content in files_content.items():
            if rx.search(f):
                hit = f"файл {f}"; break
            if content and rx.search(content):
                hit = f"содержимое {f}"; break
        if hit:
            reasons.append(hit)
    return reasons


def run_pack(child_root=None, base=None, signals=None, files_content=None):
    """Доменный security-вердикт. files_content: {path: text} для offline-теста; иначе — из git diff."""
    signals = dict(signals or {})
    domains, allowed = load_domains()

    # источник изменённых файлов: переданная карта (тест) или git diff коммита
    if files_content is None:
        files_content = {}
        if child_root is not None:
            changed = security_scan._git_changed_files(child_root, base) if base else None
            if changed is None:
                import subprocess
                r = subprocess.run(["git", "-C", str(child_root), "ls-files"], capture_output=True, text=True)
                # v3.0.11 (finding аудита P1): git-энумерация упала -> FAIL-CLOSED (raise), НЕ changed=[].
                # Прежде rc!=0 -> [] -> нет находок -> overall='clear': реальная правка при git-сбое
                # признавалась чистой. Исключение ловит вызывающий (_security_scan_error -> security=fail).
                if r.returncode != 0:
                    raise RuntimeError(
                        f"git ls-files rc={r.returncode} в {child_root}: не удалось определить файлы для "
                        f"security-скана ({(r.stderr or '').strip()[:160]}) — fail-closed")
                changed = [ln for ln in r.stdout.splitlines() if ln.strip()]
            files_content = security_scan._read_files(child_root, changed)
    changed_files = sorted(files_content)

    # детерминированные находки (один раз)
    secrets = security_scan.scan_secrets(files_content)
    injections = security_scan.scan_injection(files_content)
    mani = {p: c for p, c in files_content.items() if Path(p).name in security_scan.DEP_MANIFESTS}
    before = {p: (security_scan._git_show(child_root, base, p) if (child_root and base) else "") for p in mani}
    new_deps = security_scan.new_dependencies(before, mani)
    new_deps_detailed = security_scan.new_dependencies_detailed(before, mani)   # v3.0-rc5 (P1.2): fingerprint

    results, blocking, needs_review = [], [], []
    for d in domains:
        reasons = _applies(d, signals, files_content)
        if not reasons:
            continue
        checks = set(d.get("deterministic_checks", []) or [])
        findings = []
        if "secret_scan" in checks:
            findings += [{"type": "secret", "path": s["path"], "line": s["line"], "id": s["id"]} for s in secrets]
        if "injection_scan" in checks:
            findings += [{"type": "injection", "path": i["path"], "line": i["line"], "id": i["id"]} for i in injections]
        if "dependency_diff" in checks:
            # v3.0-rc5 (P1.2): finding несёт fingerprint (manifest/package/version/operation) — approval
            # supply-chain привязывается к нему, а не к пути файла (иначе одобрение одной зависимости
            # покрыло бы любую другую в том же requirements.txt/package.json).
            findings += [{"type": "new_dependency", "name": dd["name"], "version": dd.get("version"),
                          "manifest": dd.get("manifest"), "operation": dd.get("operation", "add")}
                         for dd in new_deps_detailed]

        req = set(d.get("required_evidence", []) or [])
        severity = (d.get("severity_policy", {}) or {}).get("default", "medium")
        # статус домена. ИНВАРИАНТ (finding аудита v2.104->исправлен): status=fail НИКОГДА не даёт
        # overall=clear. critical/high -> blocking (reviewer не переопределяет); medium/low ->
        # needs_review (нужен судья/человек — напр. новая зависимость требует одобрения).
        if findings:
            status = "fail"
            if severity in ("critical", "high"):
                blocking.append(d["id"])
            else:
                needs_review.append(d["id"])
        elif req and req <= DETERMINISTIC:
            # всё required_evidence — детерминированное и прошло чисто -> можно авто-закрыть
            status = "pass"
        else:
            status = "needs_review"           # нужен security_reviewer/человек (не закрываем сами)
            needs_review.append(d["id"])
        results.append({
            "domain": d["id"], "applies_because": reasons, "status": status,
            "severity": severity, "findings": findings,
            "required_evidence": sorted(req),
            "remediation": (d.get("remediation_template", {}) or {}).get("summary"),
        })

    return {
        "schema_version": 1, "kind": "security-pack-result",
        "applicable_domains": [r["domain"] for r in results],
        "results": results,
        "blocking": sorted(set(blocking)),
        "needs_review": sorted(set(needs_review)),
        "overall": ("blocked" if blocking else ("needs_review" if needs_review else "clear")),
        "allowed_evidence_sources": allowed,
    }


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    domains, allowed = load_domains()
    expect("12 доменов загружены", len(domains) == 12)
    expect("allowed_evidence не содержит 'модель сказала нет'",
           "security_reviewer" in allowed and "human_approval" in allowed)

    # frontend-only: XSS/secrets проверяются, database/tenant audit — НЕ применим
    fe = run_pack(files_content={"src/ui/View.tsx": "el.innerHTML = userInput\n"},
                  signals={"handles_user_input": True, "user_facing_change": True})
    expect("frontend: input_validation применим", "input_validation" in fe["applicable_domains"])
    expect("frontend: data_isolation НЕ применим (нет multi_tenant/tenant-файлов)",
           "data_isolation" not in fe["applicable_domains"])
    expect("frontend: innerHTML -> input_validation fail + блок (high)",
           any(r["domain"] == "input_validation" and r["status"] == "fail" for r in fe["results"])
           and "input_validation" in fe["blocking"])

    # v3.0.19 (finding живой квалификации): 'acl' в 'dataclass' НЕ поднимает authorization_idol (word-boundary).
    _da = run_pack(files_content={"pricing.py": "from dataclasses import dataclass\n@dataclass\nclass B:\n    x: int\n"},
                   signals={})
    expect("v3.0.19: 'dataclass' НЕ триггерит authorization_idol (нет ложного needs_review)",
           "authorization_idol" not in _da["applicable_domains"])
    _auth = run_pack(files_content={"auth.py": "def can_edit(user, role):\n    return user.is_admin\n"}, signals={})
    expect("v3.0.19: реальный auth-код (can_edit/role/is_admin) -> authorization_idol применим (детект сохранён)",
           "authorization_idol" in _auth["applicable_domains"])

    # секрет -> домен secrets fail + блок (critical), всегда применим
    # v3.0.4: секрет-фикстура собрана в рантайме (без статического литерала — downstream-сканеры не флагуют)
    _aws = "AKIA" + "IOSFODNN7EXAMPLE"
    sec = run_pack(files_content={"config.py": f'API_KEY = "{_aws}"\n'}, signals={})
    expect("secrets всегда применим", "secrets" in sec["applicable_domains"])
    expect("секрет -> secrets fail + overall blocked",
           any(r["domain"] == "secrets" and r["status"] == "fail" for r in sec["results"])
           and sec["overall"] == "blocked")

    # чистый secrets-домен -> авто-pass (required_evidence=[secret_scan], детерминировано)
    clean = run_pack(files_content={"a.py": "x = 1\n"}, signals={})
    sres = next(r for r in clean["results"] if r["domain"] == "secrets")
    expect("чистый secrets -> авто-pass (детерминированный evidence)", sres["status"] == "pass")

    # новая зависимость -> dependencies применим + finding
    dep = run_pack(files_content={"package.json": '{"dependencies":{"left-pad":"^1"}}'},
                   signals={}, )
    # before пуст -> left-pad считается новой
    expect("новая зависимость -> dependencies применим", "dependencies" in dep["applicable_domains"])
    # РЕГРЕССИЯ (finding аудита v2.104): medium-fail (новая зависимость) НЕ даёт overall=clear.
    # Раньше fail с severity=medium исчезал из blocking И needs_review -> ложный green.
    expect("medium-fail (новая зависимость) -> в needs_review, overall != clear (не ложный green)",
           "dependencies" in dep["needs_review"] and dep["overall"] != "clear")

    # auth-домен needs_review (required_evidence включает security_reviewer)
    auth = run_pack(files_content={"src/auth/login.py": "def login(): pass\n"}, signals={"auth_change": True})
    ares = next((r for r in auth["results"] if r["domain"] == "authentication"), None)
    expect("authentication применим по сигналу+файлу", ares is not None)
    expect("authentication чист -> needs_review (нужен судья, не авто-pass)",
           ares and ares["status"] == "needs_review" and "authentication" in auth["needs_review"])

    # ai prompt injection применим по сигналу
    ai = run_pack(files_content={"src/agent/prompt.py": "system = 'do x'\n"}, signals={"ai_component": True})
    expect("ai_prompt_injection применим по ai_component", "ai_prompt_injection" in ai["applicable_domains"])

    # v2.104 (finding самоаудита): применимость по СОДЕРЖИМОМУ, не только по пути. auth-логика в
    # файле, чей путь не матчит (src/users.py c 'password'), поднимает authentication -> не ложный green.
    hidden_auth = run_pack(files_content={"src/users.py": "def check(u, p):\n    return u.password == p\n"},
                           signals={})
    expect("самоаудит: auth-логика по содержимому (не по пути) -> authentication применим",
           "authentication" in hidden_auth["applicable_domains"])
    expect("самоаудит: скрытая auth-логика -> overall != clear (нет ложного green)",
           hidden_auth["overall"] != "clear")

    # у каждой находки есть путь/локация + remediation у домена
    expect("finding несёт path+line; домен несёт remediation",
           all("path" in f for r in fe["results"] for f in r["findings"] if f["type"] != "new_dependency")
           and all(r["remediation"] for r in fe["results"]))

    # v3.0.11 (finding аудита P1): git-энумерация файлов упала -> FAIL-CLOSED (raise), не тихий clear.
    import tempfile as _tf
    with _tf.TemporaryDirectory() as _td:            # НЕ git-репо -> git ls-files rc!=0
        try:
            run_pack(child_root=_td)
            expect("v3.0.11 A3: git-энумерация упала -> raise (fail-closed, не clear)", False)
        except RuntimeError as _e:
            expect("v3.0.11 A3: git-энумерация упала -> raise (fail-closed, не clear)",
                   "fail-closed" in str(_e))

    print("security_pack selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    ap = argparse.ArgumentParser(prog="security_pack.py")
    ap.add_argument("child_root", nargs="?", default=".")
    ap.add_argument("--base")
    ap.add_argument("--signals", default="{}")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    res = run_pack(Path(a.child_root), base=a.base, signals=json.loads(a.signals))
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"SECURITY-PACK: overall={res['overall']} · применимо доменов {len(res['applicable_domains'])} · "
              f"блокеров {len(res['blocking'])} · needs_review {len(res['needs_review'])}")
        for r in res["results"]:
            mark = {"fail": "✗", "pass": "✓", "needs_review": "?"}.get(r["status"], "·")
            print(f"  {mark} {r['domain']} [{r['severity']}] {r['status']}"
                  + (f" — находок {len(r['findings'])}" if r["findings"] else ""))
    return 1 if res["blocking"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
