#!/usr/bin/env python3
"""Детерминированный security-scan для гейта security (v2.95, аудит 2.95 — ENGINEERING evidence).

Гейт security требует evidence [no_secrets, no_injection_surface, deps_approved]. Раньше в pipeline
НЕ было производителя этого evidence -> ENGINEERING честно, но всегда упирался в security. Этот
модуль даёт ДЕТЕРМИНИРОВАННУЮ часть:
  * no_secrets        — сканер секретов по изменённым файлам (regex известных форматов);
  * deps_approved     — аудит зависимостей: НОВЫЕ зависимости в манифестах против базы;
  * injection-surface — ФЛАГИ рискованных мест (eval/exec, shell=True, pickle, yaml.load, SQL f-string,
                        dangerouslySetInnerHTML, child_process). Это ВХОД для судьи, не автоприёмка.

Честная граница: сканер может ДОКАЗАТЬ отсутствие известных секретов и отсутствие НОВЫХ зависимостей
(детерминированные факты) и закрыть no_secrets/deps_approved, когда чисто. no_injection_surface —
СУЖДЕНИЕ (эвристика лишь флагит места) -> его закрывает независимый security-reviewer/человек
(writer ≠ judge), сканер только поставляет флаги. Находки -> гейт остаётся блокирующим (fail-closed).

Использование:
  security_scan.py <root> [--base <sha>]   # скан изменений против базы (или всего дерева)
  security_scan.py --selftest
Возврат 0 — ок, 1 — ошибка/находки.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# Секреты: известные форматы + generic key-in-quotes. Плейсхолдеры (xxxx/${...}/env) отсеиваем.
SECRET_PATTERNS = [
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("github_pat", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("aws_secret_access_key", re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+]{40}\b")),
    ("generic_secret_assignment",
     re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|access[_-]?key)\b\s*[:=]\s*"
                r"['\"]([A-Za-z0-9/+_\-]{16,})['\"]")),
]
# Плейсхолдеры/ссылки на env — НЕ секрет (снижаем ложные срабатывания generic-паттерна).
_PLACEHOLDER = re.compile(r"(?i)(x{6,}|\$\{?[a-z_]+\}?|<[a-z_ -]+>|your[_-]?|example|changeme|placeholder|env\[)")

INJECTION_PATTERNS = [
    ("eval_or_exec", re.compile(r"\b(?:eval|exec)\s*\(")),
    ("subprocess_shell_true", re.compile(r"(?:subprocess\.\w+|Popen)\s*\([^)]*shell\s*=\s*True")),
    ("os_system", re.compile(r"\bos\.system\s*\(")),
    ("pickle_loads", re.compile(r"\bpickle\.loads?\s*\(")),
    ("yaml_unsafe_load", re.compile(r"\byaml\.load\s*\((?![^)]*Loader)")),
    ("sql_fstring_execute", re.compile(r"(?i)\bexecute(?:many)?\s*\(\s*f['\"]")),
    ("react_dangerous_html", re.compile(r"dangerouslySetInnerHTML")),
    ("node_child_process", re.compile(r"require\(['\"]child_process['\"]\)|from ['\"]child_process['\"]")),
    ("dom_innerhtml_assign", re.compile(r"\.innerHTML\s*=")),
]


def _scan(text, patterns):
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for pid, rx in patterns:
            m = rx.search(line)
            if not m:
                continue
            if pid == "generic_secret_assignment" and _PLACEHOLDER.search(m.group(1)):
                continue
            out.append({"id": pid, "line": lineno})
    return out


def scan_secrets(files):
    """files: {path: content} -> список находок секретов [{path, id, line}]."""
    res = []
    for path, text in files.items():
        for f in _scan(text, SECRET_PATTERNS):
            res.append({"path": path, **f})
    return res


def scan_injection(files):
    """Флаги injection-surface (ВХОД для судьи, не автоприёмка) -> [{path, id, line}]."""
    res = []
    for path, text in files.items():
        for f in _scan(text, INJECTION_PATTERNS):
            res.append({"path": path, **f})
    return res


def _dep_names(path, text):
    """Множество имён зависимостей из манифеста (по типу файла). Best-effort, детерминированно."""
    name = Path(path).name
    deps = set()
    if name == "package.json":
        try:
            data = json.loads(text)
            for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                deps |= set((data.get(key) or {}).keys())
        except json.JSONDecodeError:
            pass
    elif name == "requirements.txt":
        for ln in text.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                deps.add(re.split(r"[<>=!~\[ ]", ln, 1)[0].strip().lower())
    elif name == "go.mod":
        # обе формы: однострочная `require github.com/x/y v1.2.3` и блок `require ( ... )`
        for m in re.finditer(r"^\s*(?:require\s+)?([\w][\w./\-]+)\s+v\d", text, re.M):
            if m.group(1) != "require":
                deps.add(m.group(1))
    elif name in ("pyproject.toml", "Cargo.toml"):
        # строки вида name = "..." или "name>=x" в секциях зависимостей — грубо, но детерминированно
        for m in re.finditer(r'["\']([A-Za-z0-9._\-]+)["\']\s*[:=]', text):
            deps.add(m.group(1).lower())
        for m in re.finditer(r'^\s*([A-Za-z0-9._\-]+)\s*=\s*["\{]', text, re.M):
            deps.add(m.group(1).lower())
    return deps


def new_dependencies(before, after):
    """before/after: {manifest_path: content}. -> отсортированный список НОВЫХ имён зависимостей."""
    added = set()
    for path, after_text in after.items():
        before_names = _dep_names(path, before.get(path, ""))
        added |= (_dep_names(path, after_text) - before_names)
    return sorted(added)


def _dep_specs(path, text):
    """{name: version|None} из манифеста (версия best-effort: requirements '==', package.json значение)."""
    name = Path(path).name
    specs = {}
    if name == "package.json":
        try:
            data = json.loads(text)
            for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                for k, v in (data.get(key) or {}).items():
                    specs[k] = str(v)
        except json.JSONDecodeError:
            pass
    elif name == "requirements.txt":
        for ln in text.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                nm = re.split(r"[<>=!~\[ ]", ln, 1)[0].strip().lower()
                mv = re.search(r"==\s*([0-9][\w.\-]*)", ln)
                specs[nm] = mv.group(1) if mv else None
    else:
        for nm in _dep_names(path, text):
            specs[nm] = None
    return specs


def new_dependencies_detailed(before, after):
    """v3.0-rc5 (P1.2): НОВЫЕ зависимости с деталями для fingerprint approval.
    -> [{name, version, manifest, operation:'add'}] (отсортировано по manifest, name)."""
    out = []
    for path in sorted(after):
        b, a = _dep_specs(path, before.get(path, "")), _dep_specs(path, after[path])
        for nm in sorted(set(a) - set(b)):
            out.append({"name": nm, "version": a.get(nm), "manifest": Path(path).name, "operation": "add"})
    return out


def security_evidence(secrets, injections, new_deps):
    """Собрать gate_ev-совместимый вердикт по частям security. Детерминированно закрываем ТОЛЬКО
    no_secrets и deps_approved (факты). no_injection_surface оставляем судье (даём флаги как вход)."""
    ev = {}
    ev["no_secrets"] = {"status": "pass" if not secrets else "fail",
                        "findings": secrets}
    ev["deps_approved"] = {"status": "pass" if not new_deps else "fail",
                           "new_dependencies": new_deps}
    # no_injection_surface НЕ закрываем автоматически: эвристика лишь флагит. Судья (security-reviewer/
    # человек) выносит вердикт. Отдаём флаги + статус "needs_review" (чисто) или "fail" (есть флаги).
    ev["no_injection_surface"] = {"status": "needs_review" if not injections else "fail",
                                  "flags": injections,
                                  "note": "детерминированный сканер не закрывает injection-surface — "
                                          "нужен независимый security-reviewer (--review) или человек"}
    return ev


def _git_changed_files(root, base):
    r = subprocess.run(["git", "-C", str(root), "diff", "--name-only", f"{base}..HEAD"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def _read_files(root, rels):
    out = {}
    for rel in rels:
        p = Path(root) / rel
        if p.is_file():
            try:
                out[rel] = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                pass
    return out


def _git_show(root, ref, rel):
    r = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{rel}"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


DEP_MANIFESTS = ("package.json", "requirements.txt", "go.mod", "pyproject.toml", "Cargo.toml")


def scan_repo(root, base=None):
    """Скан изменений против базы (или всего дерева, если base=None/не git). -> отчёт + evidence."""
    root = Path(root)
    changed = _git_changed_files(root, base) if base else None
    if changed is None:
        # не git / нет базы: сканируем отслеживаемые текстовые файлы целиком (best-effort)
        r = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True)
        changed = [ln for ln in r.stdout.splitlines() if ln.strip()] if r.returncode == 0 else []
    files = _read_files(root, changed)
    secrets = scan_secrets(files)
    injections = scan_injection(files)
    # зависимости: сравниваем манифесты после (рабочее дерево) против базы (git show base:)
    after_mani = {p: c for p, c in files.items() if Path(p).name in DEP_MANIFESTS}
    if not after_mani:  # манифесты могли не измениться — прочитаем текущие для полноты
        after_mani = _read_files(root, [m for m in DEP_MANIFESTS if (root / m).is_file()])
    before_mani = {p: (_git_show(root, base, p) if base else "") for p in after_mani}
    new_deps = new_dependencies(before_mani, after_mani)
    ev = security_evidence(secrets, injections, new_deps)
    return {"schema_version": 1, "kind": "security-scan",
            "scanned_files": len(files), "secrets": secrets,
            "injection_flags": injections, "new_dependencies": new_deps,
            "evidence": ev}


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    # секреты. v3.0.4: фикстуры-«секреты» СОБИРАЮТСЯ в рантайме из фрагментов, чтобы в исходнике НЕ
    # было статического секрет-подобного литерала (иначе downstream секрет-сканеры (gitleaks/trufflehog)
    # ложно флагуют тесты САМОГО детектора и блокируют PR). Детектор получает полную строку -> тест валиден.
    _aws = "AKIA" + "IOSFODNN7EXAMPLE"                         # канонический AWS-пример (собран, не литерал)
    _hex = "abcdef0123456789" + "ABCDEF"
    _pem = "-----BEGIN RSA " + "PRIVATE KEY-----\n"
    s = scan_secrets({"a.py": f'AWS="{_aws}"\napi_key = "{_hex}"\n'})
    expect("secret: AKIA-ключ найден", any(f["id"] == "aws_access_key_id" for f in s))
    expect("secret: generic api_key в кавычках найден", any(f["id"] == "generic_secret_assignment" for f in s))
    expect("secret: чистый файл -> нет находок", scan_secrets({"b.py": "x = 1\n"}) == [])
    expect("secret: плейсхолдер/env НЕ секрет",
           scan_secrets({"c.py": 'api_key = "${API_KEY}"\ntoken = "your-token-here"\n'}) == [])
    expect("secret: private key блок найден",
           any(f["id"] == "private_key_block" for f in scan_secrets({"k": _pem})))

    # injection
    inj = scan_injection({"a.py": "eval(user_input)\nsubprocess.run(cmd, shell=True)\n"})
    expect("injection: eval флагнут", any(f["id"] == "eval_or_exec" for f in inj))
    expect("injection: shell=True флагнут", any(f["id"] == "subprocess_shell_true" for f in inj))
    expect("injection: yaml.load без Loader флагнут",
           any(f["id"] == "yaml_unsafe_load" for f in scan_injection({"a.py": "yaml.load(data)\n"})))
    expect("injection: yaml.load с SafeLoader НЕ флагнут",
           scan_injection({"a.py": "yaml.load(data, Loader=yaml.SafeLoader)\n"}) == [])
    expect("injection: чистый файл -> нет флагов", scan_injection({"b.py": "return a + b\n"}) == [])

    # новые зависимости
    before = {"package.json": '{"dependencies":{"react":"^18"}}'}
    after = {"package.json": '{"dependencies":{"react":"^18","left-pad":"^1"}}'}
    expect("deps: новая зависимость left-pad обнаружена", new_dependencies(before, after) == ["left-pad"])
    expect("deps: без новых -> пусто", new_dependencies(after, after) == [])
    expect("deps: requirements.txt новая строка",
           new_dependencies({"requirements.txt": "flask\n"}, {"requirements.txt": "flask\nrequests\n"}) == ["requests"])
    expect("deps: go.mod новый require",
           "github.com/x/y" in new_dependencies({"go.mod": "module m\n"}, {"go.mod": "module m\nrequire github.com/x/y v1.2.3\n"}))

    # evidence: закрываем no_secrets/deps_approved только когда чисто; injection -> судье
    ev = security_evidence([], [], [])
    expect("evidence: чисто -> no_secrets pass", ev["no_secrets"]["status"] == "pass")
    expect("evidence: без новых deps -> deps_approved pass", ev["deps_approved"]["status"] == "pass")
    expect("evidence: injection чисто -> needs_review (НЕ авто-pass; судья закрывает)",
           ev["no_injection_surface"]["status"] == "needs_review")
    ev2 = security_evidence([{"path": "a", "id": "x", "line": 1}], [{"path": "a", "id": "eval_or_exec", "line": 2}], ["left-pad"])
    expect("evidence: секрет -> no_secrets fail", ev2["no_secrets"]["status"] == "fail")
    expect("evidence: новые deps -> deps_approved fail", ev2["deps_approved"]["status"] == "fail")
    expect("evidence: injection-флаг -> no_injection_surface fail", ev2["no_injection_surface"]["status"] == "fail")

    print("security_scan selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    ap = argparse.ArgumentParser(prog="security_scan.py")
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--base", help="git-ревизия базы для diff (иначе — все отслеживаемые файлы)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rep = scan_repo(a.root, a.base)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(f"SECURITY-SCAN: файлов {rep['scanned_files']} · секретов {len(rep['secrets'])} · "
              f"injection-флагов {len(rep['injection_flags'])} · новых зависимостей {len(rep['new_dependencies'])}")
        for s in rep["secrets"]:
            print(f"  СЕКРЕТ {s['id']} — {s['path']}:{s['line']}")
        for d in rep["new_dependencies"]:
            print(f"  НОВАЯ ЗАВИСИМОСТЬ {d} (нужно одобрение)")
    # ненулевой код при находках секретов/новых зависимостей (injection-флаги — не фейл сами по себе)
    return 1 if (rep["secrets"] or rep["new_dependencies"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
