#!/usr/bin/env python3
"""Project Detector -> RepositoryProfile (v2.41, P0#5 аудита — stack-aware evidence).

После подключения кита система должна САМА определить стек и команды build/lint/typecheck/
test, а не спрашивать. Детектор читает манифесты/файлы (детерминированно, без догадок) и
строит RepositoryProfile (schemas/repository-profile.schema.json). Он — stack-часть
онбординга (repo-onboarding) и основа для stack-aware evidence collectors: gate
implementation_verification знает, ЧЕМ собирать/тестировать именно этот репозиторий.

Инвариант честности: что не определено — в `undetermined`, а не выдумано. Каждая выведенная
команда имеет файл-источник в `command_evidence`/`evidence_source`; команда без источника
снимается в None (выдуманная команда опаснее отсутствующей — она делает гейт зелёным вхолостую).
status: draft — источник истины подтверждает человек (writer != judge).

Единая точка детекции — `load_or_detect(root)`: читает `.ai/repository-profile.yaml`, если он
не протух (сверка `manifest_hash` — хеша состава/содержимого манифестов), иначе детектит заново
и обновляет файл. Протухший кеш НЕ отдаётся: иначе прогон пойдёт по устаревшему стеку.

Использование:  project_detector.py detect [root] [--json]
                project_detector.py --selftest
Возврат 0 — ок, 1 — ошибка.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

# Профиль в child-репозитории (его пишет `ai-ops onboard` и обновляет load_or_detect).
PROFILE_REL = Path(".ai") / "repository-profile.yaml"

# Манифесты, от которых зависит РЕЗУЛЬТАТ детекции: изменился состав или содержимое любого —
# профиль протух (failure mode #3: прогон по устаревшему стеку после правки манифестов).
_WATCHED_FILES = (
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "pnpm-workspace.yaml",
    "lerna.json", "turbo.json", "nx.json",
    "pyproject.toml", "requirements.txt", "requirements-dev.txt", "uv.lock",
    "setup.cfg", "pytest.ini", "tox.ini", "mypy.ini", ".mypy.ini", "pyrightconfig.json",
    ".ruff.toml", "ruff.toml", ".flake8", ".pre-commit-config.yaml", "Makefile", "makefile",
    "go.mod", ".golangci.yml", ".golangci.yaml", ".golangci.toml",
    "pom.xml", "mvnw", "build.gradle", "build.gradle.kts", "gradlew",
    "Cargo.toml", ".gitlab-ci.yml",
)
_WATCHED_GLOBS = ("*/package.json", "apps/*/package.json", "packages/*/package.json",
                  ".github/workflows/*.yml", ".github/workflows/*.yaml")


def _read_json(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _text(p):
    """Текст файла или '' — детектор не падает на нечитаемом манифесте."""
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _has_py_tests(d: Path):
    """Есть ли в репозитории python-тесты (test_*.py / *_test.py в tests|test или в корне)."""
    for base in (d / "tests", d / "test"):
        if base.is_dir():
            for pat in ("test_*.py", "*_test.py"):
                for _ in base.rglob(pat):
                    return True
    for pat in ("test_*.py", "*_test.py"):
        for _ in d.glob(pat):
            return True
    return False


def manifest_fingerprint(root):
    """sha256 по составу и содержимому манифестов, участвующих в детекции.

    Дёшево (манифесты маленькие, каталоги не обходятся целиком) и честно: хеш меняется ровно
    тогда, когда может измениться результат detect(). Пишется в профиль как `manifest_hash`
    и сверяется при чтении кеша."""
    root = Path(root)
    h = hashlib.sha256()
    h.update(b"repository-profile/manifest-hash/v1\n")

    def _feed(rel, path):
        h.update(rel.encode("utf-8")); h.update(b"=")
        try:
            h.update(hashlib.sha256(path.read_bytes()).digest())
        except OSError:
            h.update(b"?")
        h.update(b"\n")

    for rel in _WATCHED_FILES:
        p = root / rel
        if p.is_file():
            _feed(rel, p)
        else:
            h.update(rel.encode("utf-8")); h.update(b"=-\n")
    for pattern in _WATCHED_GLOBS:
        for p in sorted(root.glob(pattern)):
            if p.is_file():
                _feed(p.relative_to(root).as_posix(), p)
    # каталоги: на детект влияет сам факт наличия тестов (а не поимённый состав)
    h.update(b"has_py_tests=%d\n" % int(_has_py_tests(root)))
    h.update(b"tests_dir=%d\n" % int((root / "tests").is_dir()))
    h.update(b"workflows_dir=%d\n" % int((root / ".github" / "workflows").is_dir()))
    return h.hexdigest()


class _Slots:
    """Копилка команд с ОБЯЗАТЕЛЬНЫМ файлом-источником: первое доказательство выигрывает.

    put() без источника не проходит — так команда физически не может появиться в профиле
    «из воздуха» (failure mode #4)."""

    def __init__(self):
        self.cmd, self.src = {}, {}

    def put(self, slot, command, source):
        if command and source and not self.cmd.get(slot):
            self.cmd[slot] = command
            self.src[slot] = source


def _has_section(txt, section):
    """Есть ли в ini/toml-тексте секция [section] (объявлена явно, а не упомянута в комментарии)."""
    return re.search(r"(?m)^\s*\[" + re.escape(section) + r"\]", txt or "") is not None


def _precommit_hooks(d: Path):
    """Реально объявленные хуки .pre-commit-config.yaml -> множество id (ruff, mypy, black, …)."""
    txt = _text(d / ".pre-commit-config.yaml")
    if not txt:
        return set()
    try:
        data = yaml.safe_load(txt) or {}
    except yaml.YAMLError:
        return set()
    ids = set()
    for repo in (data.get("repos") or []) if isinstance(data, dict) else []:
        for hook in (repo.get("hooks") or []) if isinstance(repo, dict) else []:
            if isinstance(hook, dict) and hook.get("id"):
                ids.add(str(hook["id"]).lower())
    return ids


def _make_targets(d: Path):
    """Явные цели Makefile -> (множество целей, имя файла|None). Присваивания VAR := x не цели."""
    for name in ("Makefile", "makefile"):
        txt = _text(d / name)
        if txt:
            return {m.group(1) for m in re.finditer(r"(?m)^([A-Za-z0-9_.\-]+)\s*:(?!=)", txt)}, name
    return set(), None


# Что РЕАЛЬНО запускается в CI. Строка берётся как есть и только если она С НЕЁ начинается —
# `cd x && pytest` или `ruff check . || true` не считаются доказательством команды гейта.
_CI_PATTERNS = {
    "python": {
        "test": (r"(?:python3?\s+-m\s+)?pytest\b", r"tox\b"),
        "lint": (r"(?:python3?\s+-m\s+)?ruff\s+check\b", r"(?:python3?\s+-m\s+)?flake8\b"),
        "typecheck": (r"(?:python3?\s+-m\s+)?mypy\b", r"pyright\b"),
        "build": (r"python3?\s+-m\s+build\b",),
    },
    "node": {
        "build": (r"(?:npm\s+run|yarn|pnpm(?:\s+run)?)\s+build\b",),
        "lint": (r"(?:npm\s+run|yarn|pnpm(?:\s+run)?)\s+lint\b",),
        "typecheck": (r"(?:npm\s+run|yarn|pnpm(?:\s+run)?)\s+(?:typecheck|type-check)\b",),
        "test": (r"npm\s+(?:run\s+)?test\b", r"(?:yarn|pnpm)\s+(?:run\s+)?test\b"),
    },
    "go": {"build": (r"go\s+build\b",), "test": (r"go\s+test\b",),
           "typecheck": (r"go\s+vet\b",), "lint": (r"golangci-lint\s+run\b",)},
    "rust": {"build": (r"cargo\s+build\b",), "test": (r"cargo\s+test\b",),
             "typecheck": (r"cargo\s+check\b",), "lint": (r"cargo\s+clippy\b",)},
}


def _ci_commands(root: Path):
    """Команды из GitHub Actions -> {язык: {slot: (команда, файл-источник)}}.

    Берём только шаги, падение которых что-то значит: continue-on-error и `|| true` пропускаем,
    иначе гейт «пройдёт» на команде, которой разрешено падать. Шаблоны ${{ }} не берём — их
    нельзя выполнить вне GitHub."""
    out = {}
    wf_dir = root / ".github" / "workflows"
    if not wf_dir.is_dir():
        return out
    for wf in sorted(list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml"))):
        try:
            data = yaml.safe_load(_text(wf)) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        src = wf.relative_to(root).as_posix()
        for job in (data.get("jobs") or {}).values():
            if not isinstance(job, dict) or job.get("continue-on-error"):
                continue
            for step in job.get("steps") or []:
                if not isinstance(step, dict) or step.get("continue-on-error"):
                    continue
                run = step.get("run")
                if not isinstance(run, str):
                    continue
                for line in run.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "||" in line or "${{" in line:
                        continue
                    for lang, slots in _CI_PATTERNS.items():
                        for slot, pats in slots.items():
                            if out.get(lang, {}).get(slot):
                                continue
                            if any(re.match(p, line) for p in pats):
                                out.setdefault(lang, {})[slot] = (line, src)
    return out


_MAKE_ALIASES = {"build": ("build",), "lint": ("lint",),
                 "typecheck": ("typecheck", "type-check", "types"), "test": ("test", "tests")}


def _finalize(stack, root: Path, ci_for_lang, make_targets, make_file):
    """Добить пустые слоты общерепозиторными фактами (Makefile, CI) и запечатать инвариант
    честности: команда без файла-источника снимается в None."""
    cmds = dict(stack.get("commands") or {})
    ev = dict(stack.pop("_command_evidence", None) or {})
    for slot in ("build", "lint", "typecheck", "test"):
        if not cmds.get(slot):
            for target in _MAKE_ALIASES[slot]:
                if target in make_targets and make_file:
                    cmds[slot], ev[slot] = f"make {target}", make_file
                    break
        if not cmds.get(slot) and (ci_for_lang or {}).get(slot):
            cmds[slot], ev[slot] = ci_for_lang[slot]
        # инвариант честности: нет файла-источника -> команда выдумана, снимаем
        if cmds.get(slot) and not ev.get(slot):
            cmds[slot] = None
        cmds.setdefault(slot, None)
        if not cmds.get(slot):
            ev.pop(slot, None)
    stack["commands"] = {k: cmds.get(k) for k in ("build", "lint", "typecheck", "test")}
    stack["command_evidence"] = {k: ev[k] for k in sorted(ev)}
    srcs = list(stack.get("evidence_source") or [])
    for s in sorted(set(ev.values())):
        if s not in srcs:
            srcs.append(s)
    stack["evidence_source"] = srcs
    return stack


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

    slots = _Slots()

    def cmd(slot, *names):
        # источник — package.json: команда взята из реально объявленного scripts-таргета
        for n in names:
            if n in scripts:
                slots.put(slot, f"{run} {n}", "package.json")
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
            "build": cmd("build", "build"),
            "lint": cmd("lint", "lint"),
            "typecheck": cmd("typecheck", "typecheck", "tsc", "type-check"),
            "test": cmd("test", "test"),
        },
        "evidence_source": ["package.json"] + ([f"{pm}-lock"] if pm else []),
        "_command_evidence": slots.src,
    }


def _python_commands(d: Path, dep_src):
    """Команды python-стека ТОЛЬКО по фактам репозитория -> _Slots.

    Порядок доказательств: конфиг инструмента в манифесте > отдельный конфиг-файл >
    объявленный pre-commit-хук > упоминание в зависимостях > наличие самих тестов.
    Нет ни одного факта — слот остаётся пустым (гейт честно станет not_applicable)."""
    s = _Slots()
    pyproject = _text(d / "pyproject.toml")
    setup_cfg = _text(d / "setup.cfg")
    tox = _text(d / "tox.ini")
    hooks = _precommit_hooks(d)
    PRECOMMIT = ".pre-commit-config.yaml"

    # test
    if _has_section(pyproject, "tool.pytest.ini_options"):
        s.put("test", "pytest", "pyproject.toml")
    if (d / "pytest.ini").is_file():
        s.put("test", "pytest", "pytest.ini")
    if _has_section(setup_cfg, "tool:pytest") or _has_section(setup_cfg, "pytest"):
        s.put("test", "pytest", "setup.cfg")
    if _has_section(tox, "pytest"):
        s.put("test", "pytest", "tox.ini")
    if _has_section(tox, "tox"):
        s.put("test", "tox", "tox.ini")
    if dep_src("pytest"):
        s.put("test", "pytest", dep_src("pytest"))
    if _has_py_tests(d):
        # есть настоящие файлы test_*.py — pytest подхватывает и unittest-стиль
        s.put("test", "pytest", "tests/" if (d / "tests").is_dir() else "test_*.py")

    # lint
    if _has_section(pyproject, "tool.ruff") or _has_section(pyproject, "tool.ruff.lint"):
        s.put("lint", "ruff check .", "pyproject.toml")
    for name in (".ruff.toml", "ruff.toml"):
        if (d / name).is_file():
            s.put("lint", "ruff check .", name)
    if "ruff" in hooks:
        s.put("lint", "ruff check .", PRECOMMIT)
    if dep_src("ruff"):
        s.put("lint", "ruff check .", dep_src("ruff"))
    if _has_section(pyproject, "tool.flake8"):
        s.put("lint", "flake8", "pyproject.toml")
    if (d / ".flake8").is_file():
        s.put("lint", "flake8", ".flake8")
    if _has_section(setup_cfg, "flake8"):
        s.put("lint", "flake8", "setup.cfg")
    if _has_section(tox, "flake8"):
        s.put("lint", "flake8", "tox.ini")
    if "flake8" in hooks:
        s.put("lint", "flake8", PRECOMMIT)
    if dep_src("flake8"):
        s.put("lint", "flake8", dep_src("flake8"))

    # typecheck
    if _has_section(pyproject, "tool.mypy"):
        s.put("typecheck", "mypy .", "pyproject.toml")
    for name in ("mypy.ini", ".mypy.ini"):
        if (d / name).is_file():
            s.put("typecheck", "mypy .", name)
    if _has_section(setup_cfg, "mypy"):
        s.put("typecheck", "mypy .", "setup.cfg")
    if "mypy" in hooks:
        s.put("typecheck", "mypy .", PRECOMMIT)
    if dep_src("mypy"):
        s.put("typecheck", "mypy .", dep_src("mypy"))
    if _has_section(pyproject, "tool.pyright"):
        s.put("typecheck", "pyright", "pyproject.toml")
    if (d / "pyrightconfig.json").is_file():
        s.put("typecheck", "pyright", "pyrightconfig.json")
    if dep_src("pyright"):
        s.put("typecheck", "pyright", dep_src("pyright"))

    # build: сборку дистрибутива никто не «угадывает» — только явный Makefile/CI (см. _finalize)
    return s


def _python_stack(d: Path):
    fw, src = [], []
    deps_text = ""
    for rel in ("pyproject.toml", "requirements.txt", "requirements-dev.txt"):
        if (d / rel).exists():
            src.append(rel); deps_text += _text(d / rel)
    low = deps_text.lower()

    def dep_src(name):
        """В каком манифесте упомянута зависимость — честный evidence для выведенной команды."""
        for rel in ("pyproject.toml", "requirements.txt", "requirements-dev.txt"):
            if name in _text(d / rel).lower():
                return rel
        return None
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
    # команды — только по фактам репозитория (детектор не выдумывает несуществующие таргеты)
    slots = _python_commands(d, dep_src)
    return {
        "language": "python",
        "package_manager": pm,
        "frameworks": fw,
        "install_command": install,
        "commands": {k: slots.cmd.get(k) for k in ("build", "lint", "typecheck", "test")},
        "evidence_source": src,
        "_command_evidence": slots.src,
    }


def _simple_stack(lang, files, d: Path, commands, source=None):
    """Стек, команды которого заданы самим тулчейном (go/rust/maven/gradle). Источник команд —
    манифест стека: без него команда не считается выведенной."""
    src = [f for f in files if (d / f).exists()]
    source = source or (src[0] if src else None)
    return {"language": lang, "package_manager": None, "frameworks": [],
            "commands": commands, "evidence_source": src,
            "_command_evidence": {k: source for k, v in commands.items() if v and source}}


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
    # go: lint — только при объявленном конфиге golangci-lint (иначе линтера в репо просто нет)
    if (root / "go.mod").exists():
        go_stack = _simple_stack("go", ["go.mod"], root,
                                 {"build": "go build ./...", "lint": None,
                                  "typecheck": "go vet ./...", "test": "go test ./..."})
        for name in (".golangci.yml", ".golangci.yaml", ".golangci.toml"):
            if (root / name).is_file():
                go_stack["commands"]["lint"] = "golangci-lint run"
                go_stack["_command_evidence"]["lint"] = name
                break
        stacks.append(go_stack)
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

    # добить пустые слоты общерепозиторными фактами (Makefile, GitHub Actions) и запечатать
    # инвариант честности: каждая оставшаяся команда имеет файл-источник
    ci_cmds = _ci_commands(root)
    make_targets, make_file = _make_targets(root)
    for s in stacks:
        _finalize(s, root, ci_cmds.get(s["language"]), make_targets, make_file)

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
            # честная причина: не «не умеем», а «в репозитории нет доказательства команды»
            # `', '.join(...)`, а не repr списка: строка человекочитаемая, и `['build', 'lint']`
            # посреди русской фразы читается как выпавший наружу отладочный вывод (так и было).
            undetermined.append(
                f"{s['language']}: не выведены команды {', '.join(miss)} — нет доказательства в репозитории "
                "(конфиг инструмента / скрипт / Makefile-цель / шаг CI); задать вручную/подтвердить")
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
        # хеш манифестов, по которым построен профиль — по нему load_or_detect видит протухание
        "manifest_hash": manifest_fingerprint(root),
    }


def load_or_detect(root, write=True):
    """Единая точка детекции: кеш `.ai/repository-profile.yaml`, если он свежий, иначе detect().

    Свежесть — сверка `manifest_hash` с текущим состоянием манифестов. Протухший (или битый,
    или без хеша — например, от старой версии кита) профиль НЕ отдаётся: иначе прогон пойдёт
    по устаревшему стеку и по командам, которых в репозитории уже нет. Записываем обновление
    только если каталог `.ai/` уже существует (не создаём его в чужом репозитории)."""
    root = Path(root)
    prof_path = root / PROFILE_REL
    fingerprint = manifest_fingerprint(root)

    if prof_path.is_file():
        try:
            cached = yaml.safe_load(prof_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            cached = None
        if (isinstance(cached, dict) and cached.get("kind") == "repository-profile"
                and cached.get("manifest_hash") == fingerprint):
            return cached

    profile = detect(root)
    if write and prof_path.parent.is_dir():
        try:
            prof_path.write_text(yaml.safe_dump(profile, allow_unicode=True, sort_keys=False),
                                 encoding="utf-8")
        except OSError:
            pass          # кеш — оптимизация; недоступная запись не должна ронять детекцию
    return profile


def main(argv):
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
