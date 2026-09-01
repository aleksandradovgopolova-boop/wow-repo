#!/usr/bin/env python3
"""Project Detector -> RepositoryProfile (v2.41, P0#5 аудита — stack-aware evidence).

После подключения кита система должна САМА определить стек и команды build/lint/typecheck/
test, а не спрашивать. Детектор читает манифесты/файлы (детерминированно, без догадок) и
строит RepositoryProfile (schemas/repository-profile.schema.json). Он — stack-часть
онбординга (repo-onboarding) и основа для stack-aware evidence collectors: gate
implementation_verification знает, ЧЕМ собирать/тестировать именно этот репозиторий.

Инвариант честности: что не определено — в `undetermined`, а не выдумано. status: draft —
источник истины подтверждает человек (writer != judge).

Использование:  project_detector.py detect [root] [--json]
                project_detector.py --selftest
Возврат 0 — ок, 1 — ошибка.
"""

import argparse
import json
import sys
from pathlib import Path

import yaml


def _read_json(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _node_pm(d: Path):
    if (d / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (d / "yarn.lock").exists():
        return "yarn"
    if (d / "package-lock.json").exists():
        return "npm"
    return "npm"


def _node_stack(d: Path, root: Path):
    pkg = _read_json(d / "package.json")
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    scripts = pkg.get("scripts", {}) or {}
    pm = _node_pm(d if (d / "package-lock.json").exists() or (d / "yarn.lock").exists()
                 or (d / "pnpm-lock.yaml").exists() else root)
    run = {"npm": "npm run", "yarn": "yarn", "pnpm": "pnpm"}[pm]
    fw = []
    for name, label in [("next", "next"), ("react", "react"), ("vue", "vue"),
                        ("@angular/core", "angular"), ("svelte", "svelte"),
                        ("express", "express"), ("fastify", "fastify"), ("nestjs", "nestjs")]:
        if name in deps:
            fw.append(label)

    def cmd(*names):
        for n in names:
            if n in scripts:
                return f"{run} {n}"
        return None
    # install-команда: в изолированном worktree нет node_modules -> build/lint/test упадут
    # exit 127 (command not found), пока зависимости не поставлены. С lockfile — детерминированный
    # ci; без него — обычный install.
    has_lock = any((d / lf).exists() for lf in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml")) \
        or any((root / lf).exists() for lf in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml"))
    install = {"npm": "npm ci" if has_lock else "npm install",
               "yarn": "yarn install --frozen-lockfile" if has_lock else "yarn install",
               "pnpm": "pnpm install --frozen-lockfile" if has_lock else "pnpm install"}[pm]
    return {
        "language": "node",
        "package_manager": pm,
        "frameworks": fw,
        "install_command": install,
        "commands": {
            "build": cmd("build"),
            "lint": cmd("lint"),
            "typecheck": cmd("typecheck", "tsc", "type-check"),
            "test": cmd("test"),
        },
        "evidence_source": ["package.json"] + ([f"{pm}-lock"] if pm else []),
    }


def _python_stack(d: Path):
    fw, src = [], []
    deps_text = ""
    if (d / "pyproject.toml").exists():
        src.append("pyproject.toml"); deps_text += (d / "pyproject.toml").read_text(encoding="utf-8", errors="ignore")
    if (d / "requirements.txt").exists():
        src.append("requirements.txt"); deps_text += (d / "requirements.txt").read_text(encoding="utf-8", errors="ignore")
    low = deps_text.lower()
    for name in ("fastapi", "django", "flask", "starlette"):
        if name in low:
            fw.append(name)
    pm = "poetry" if "[tool.poetry]" in deps_text else ("uv" if (d / "uv.lock").exists() else "pip")
    # install-команда стека (для изолированного worktree). pip: requirements.txt приоритетнее,
    # иначе editable-install пакета, если есть pyproject; иначе None (нечего ставить).
    if pm == "poetry":
        install = "poetry install"
    elif pm == "uv":
        install = "uv sync"
    elif (d / "requirements.txt").exists():
        install = "pip install -r requirements.txt"
    elif (d / "pyproject.toml").exists():
        install = "pip install -e ."
    else:
        install = None
    # команды — по конвенции (детектор не выдумывает несуществующие таргеты)
    return {
        "language": "python",
        "package_manager": pm,
        "frameworks": fw,
        "install_command": install,
        "commands": {
            "build": None,
            "lint": "ruff check ." if "ruff" in low else ("flake8" if "flake8" in low else None),
            "typecheck": "mypy ." if "mypy" in low else None,
            "test": "pytest" if ("pytest" in low or (d / "tests").exists()) else None,
        },
        "evidence_source": src,
    }


def _simple_stack(lang, files, d: Path, commands):
    return {"language": lang, "package_manager": None, "frameworks": [],
            "commands": commands, "evidence_source": [f for f in files if (d / f).exists()]}


def _detect_monorepo(root: Path):
    """v2.84: усиленный детект монорепо -> (is_monorepo, reason|None). Кроме node workspaces —
    pnpm-workspace / lerna / turbo / nx и несколько package.json в apps|packages|подкаталогах."""
    pkg = _read_json(root / "package.json")
    if pkg.get("workspaces"):
        return True, "node workspaces в package.json"
    for marker in ("pnpm-workspace.yaml", "lerna.json", "turbo.json", "nx.json"):
        if (root / marker).exists():
            return True, marker
    sub = (list(root.glob("*/package.json")) + list(root.glob("packages/*/package.json"))
           + list(root.glob("apps/*/package.json")))
    # уникальные каталоги (glob '*/...' и 'packages/...' могут пересечься)
    dirs = {p.parent.resolve() for p in sub}
    if len(dirs) > 1:
        return True, f"{len(dirs)} package.json в подкаталогах (apps/packages/*)"
    return False, None


def detect(root):
    root = Path(root)
    stacks, undetermined = [], []
    # node
    if (root / "package.json").exists():
        stacks.append(_node_stack(root, root))
    # python
    if (root / "pyproject.toml").exists() or (root / "requirements.txt").exists():
        stacks.append(_python_stack(root))
    # go
    if (root / "go.mod").exists():
        stacks.append(_simple_stack("go", ["go.mod"], root,
                                    {"build": "go build ./...", "lint": None,
                                     "typecheck": "go vet ./...", "test": "go test ./..."}))
    # java: предпочитаем wrapper (./mvnw, ./gradlew) глобальному бинарю — иначе на машине без
    # установленного mvn/gradle сборка падает exit 127 (частый finding). Wrapper — в репо.
    if (root / "pom.xml").exists():
        mvn = "./mvnw" if (root / "mvnw").exists() else "mvn"
        src = ["pom.xml"] + (["mvnw"] if mvn == "./mvnw" else [])
        stacks.append(_simple_stack("java", src, root,
                                    {"build": f"{mvn} -q package", "lint": None,
                                     "typecheck": None, "test": f"{mvn} -q test"}))
    elif (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
        gradle = "./gradlew" if (root / "gradlew").exists() else "gradle"
        src = ["build.gradle"] + (["gradlew"] if gradle == "./gradlew" else [])
        stacks.append(_simple_stack("java", src, root,
                                    {"build": f"{gradle} build", "lint": None,
                                     "typecheck": None, "test": f"{gradle} test"}))
    # rust
    if (root / "Cargo.toml").exists():
        stacks.append(_simple_stack("rust", ["Cargo.toml"], root,
                                    {"build": "cargo build", "lint": "cargo clippy",
                                     "typecheck": "cargo check", "test": "cargo test"}))

    # monorepo (v2.84): workspaces / pnpm-workspace / lerna / turbo / nx / много package.json
    monorepo, monorepo_reason = _detect_monorepo(root)

    ci = []
    if (root / ".github" / "workflows").is_dir():
        ci.append("github-actions")
    if (root / ".gitlab-ci.yml").exists():
        ci.append("gitlab-ci")

    if not stacks:
        undetermined.append("стек не определён — нет известных манифестов (package.json/pyproject/go.mod/…)")
    for s in stacks:
        miss = [k for k, v in s["commands"].items() if v is None]
        if miss:
            undetermined.append(f"{s['language']}: не выведены команды {miss} — задать вручную/подтвердить")
    # монорепо: команды на корне могут НЕ покрывать сборку/тесты каждого пакета — честно предупреждаем,
    # чтобы evidence одного корневого прогона не считался покрытием всего репозитория.
    if monorepo:
        undetermined.append(
            f"монорепо ({monorepo_reason}): корневые команды могут не покрывать все пакеты — "
            "подтвердить per-package build/test или таргет-фильтр")

    return {
        "schema_version": 1, "kind": "repository-profile", "status": "draft",
        "monorepo": monorepo, "monorepo_reason": monorepo_reason,
        "stacks": stacks, "ci": ci, "undetermined": undetermined,
    }


def selftest():
    import tempfile
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "package.json").write_text(json.dumps({
            "dependencies": {"react": "^18", "next": "^14"},
            "devDependencies": {"typescript": "^5", "eslint": "^9"},
            "scripts": {"build": "next build", "lint": "eslint .", "test": "vitest", "typecheck": "tsc --noEmit"}}),
            encoding="utf-8")
        (root / "package-lock.json").write_text("{}", encoding="utf-8")
        (root / ".github" / "workflows").mkdir(parents=True)
        prof = detect(root)
        s = prof["stacks"][0]
        expect("node определён", s["language"] == "node" and s["package_manager"] == "npm")
        expect("frameworks: next+react", {"next", "react"} <= set(s["frameworks"]))
        expect("команды из scripts", s["commands"]["build"] == "npm run build"
               and s["commands"]["test"] == "npm run test"
               and s["commands"]["typecheck"] == "npm run typecheck")
        expect("install-команда node с lockfile -> npm ci", s.get("install_command") == "npm ci")
        expect("CI обнаружен", "github-actions" in prof["ci"])
        expect("status draft (подтверждает человек)", prof["status"] == "draft")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "pyproject.toml").write_text(
            "[tool.poetry]\nname='x'\n[tool.poetry.dependencies]\nfastapi='*'\npytest='*'\nmypy='*'\n",
            encoding="utf-8")
        (root / "tests").mkdir()
        prof = detect(root)
        s = prof["stacks"][0]
        expect("python определён (poetry)", s["language"] == "python" and s["package_manager"] == "poetry")
        expect("fastapi во frameworks", "fastapi" in s["frameworks"])
        expect("python test/typecheck выведены", s["commands"]["test"] == "pytest"
               and s["commands"]["typecheck"] == "mypy .")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        prof = detect(root)
        expect("пустой репо -> стек не определён (честно в undetermined)",
               prof["stacks"] == [] and any("стек не определён" in u for u in prof["undetermined"]))

    # v2.84: java wrapper (./gradlew, ./mvnw) предпочитается глобальному бинарю (иначе exit 127)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "build.gradle").write_text("plugins {}\n", encoding="utf-8")
        (root / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
        s = detect(root)["stacks"][0]
        expect("java: ./gradlew предпочтён глобальному gradle",
               s["commands"]["build"] == "./gradlew build" and s["commands"]["test"] == "./gradlew test")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "pom.xml").write_text("<project/>\n", encoding="utf-8")
        s = detect(root)["stacks"][0]
        expect("java: без wrapper -> глобальный mvn (fallback)",
               s["commands"]["build"] == "mvn -q package")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "pom.xml").write_text("<project/>\n", encoding="utf-8")
        (root / "mvnw").write_text("#!/bin/sh\n", encoding="utf-8")
        s = detect(root)["stacks"][0]
        expect("java: ./mvnw предпочтён глобальному mvn", s["commands"]["test"] == "./mvnw -q test")

    # v2.84: монорепо-маркеры (pnpm-workspace / turbo) + честный undetermined-note
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "package.json").write_text("{}", encoding="utf-8")
        (root / "pnpm-workspace.yaml").write_text("packages:\n  - 'packages/*'\n", encoding="utf-8")
        prof = detect(root)
        expect("monorepo: pnpm-workspace.yaml -> monorepo=True с причиной",
               prof["monorepo"] is True and "pnpm-workspace" in (prof.get("monorepo_reason") or ""))
        expect("monorepo: честный undetermined-note про покрытие пакетов",
               any("монорепо" in u for u in prof["undetermined"]))
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "apps").mkdir(); (root / "packages").mkdir()
        (root / "apps" / "package.json").write_text("{}", encoding="utf-8")
        (root / "packages" / "package.json").write_text("{}", encoding="utf-8")
        expect("monorepo: несколько package.json в apps/packages -> monorepo",
               detect(root)["monorepo"] is True)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "package.json").write_text(json.dumps({"scripts": {"build": "tsc"}}), encoding="utf-8")
        expect("не-монорепо: одиночный package.json -> monorepo=False",
               detect(root)["monorepo"] is False)

    print("project_detector selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    ap = argparse.ArgumentParser(prog="project_detector.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("detect")
    d.add_argument("root", nargs="?", default="."); d.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "detect":
        prof = detect(a.root)
        print(json.dumps(prof, ensure_ascii=False, indent=2) if a.json
              else yaml.safe_dump(prof, allow_unicode=True, sort_keys=False))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
