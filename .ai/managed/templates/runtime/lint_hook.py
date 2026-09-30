#!/usr/bin/env python3
"""Линт дочки: в момент правки агента (хук Claude Code) и шагом её CI.

ЗАЧЕМ (#1183, работа `lint-runs-at-edit-and-in-child-ci`). Правило в CLAUDE.md агент исполнять не
обязан — это контекст, а не принуждение. А CI-шаблон дочки гонял только валидаторы кита: всё, что
написано мимо `ai-ops run` (человеком или агентом в обычной сессии), линтом дочки не проверялось
нигде. Здесь два входа одного правила «линтер дочки исполняется»:

  * `hook` — PostToolUse-хук Claude Code на `Write|Edit|MultiEdit`. Читает JSON из stdin
    (`tool_input.file_path`), гоняет линтер дочки ТОЛЬКО по этому файлу и при нарушениях выходит
    с кодом 2: Claude Code отдаёт stderr агенту, и тот правит сразу, а не на ревью.
  * `ci` — шаг child-CI (`templates/ci/ai-ops-lint.yml`): команды линта и формата из профиля
    репозитория целиком; нет линтера — сказано прямо, а не зелёным молчанием.

ПРОФИЛЬ СТИЛЯ AI OPS (#1183, `lint-profile-enforced-everywhere`). Если в дочке включён профиль
(`./ai-ops lint-profile --apply`), оба входа исполняют и его — иначе он держался бы одной ручной
командой `check`. `hook` — только на изменённом файле, против замороженной линии (существующее не в
счёт, новое сверх линии — код 2); `ci` — ровно команду `lint-profile check`. Сравнение с линией не
повторяется здесь: модули профиля берутся из кода кита рядом с хуком (`_kit`), как детектор ниже.
Профиль не включён — всё как прежде.

ЧЕСТНОСТЬ. Хук НИКОГДА не блокирует, если проверить нечем (линтер не установлен, профиля нет,
команда линта общерепозиторная): выход 0 и одна строка агенту через `additionalContext` — «правка
НЕ проверена, потому что …». Пропуск не выдаётся за пройденную проверку ни здесь, ни в CI.

Откуда команда линта. Не детектируем сами: берём профиль репозитория единственной точкой детекции
кита (`ai_ops_kit.shared.project_detector.load_or_detect` из managed-слоя — кеш
`.ai/repository-profile.yaml`, если он свежий), а при недоступности движка читаем кеш напрямую.
В CI профиль передаётся файлом (`--profile`): кеш в git дочки не хранится.

Файл едет в дочку в managed-слое (`.ai/managed/templates/runtime/lint_hook.py`) и регистрируется
установщиком в `.claude/settings.json` (installer/lint_hook_setup.py). Только stdlib; pyyaml — если
есть (он и так единственная зависимость кита).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Байткод в checksummed managed-слой дочки не пишем (тот же довод, что в ai-ops-entry.sh).
sys.dont_write_bytecode = True

PROFILE_REL = ".ai/repository-profile.yaml"
TIMEOUT_S = 45                      # хук зарегистрирован с timeout 60: успеваем сказать «не успел»
MAX_LINES = 40
MAX_CHARS = 3000
# Не код владельца: служебное кита, зависимости, сборка, сгенерированное.
SKIP_PARTS = frozenset({".ai", ".git", ".claude", "node_modules", ".venv", "venv", "__pycache__",
                        "dist", "build", ".next", "target", "vendor", "coverage", "generated"})
LANG_BY_EXT = {".py": "python", ".pyi": "python",
               ".js": "node", ".jsx": "node", ".ts": "node", ".tsx": "node", ".mjs": "node",
               ".cjs": "node", ".mts": "node", ".cts": "node", ".vue": "node", ".svelte": "node",
               ".go": "go", ".rs": "rust"}
# Выключатели `.ai-ops.yaml -> standard.lint_hook` (YAML 1.1 читает голое `off` как False).
OFF_VALUES = (False, 0, "off", "false", "no", "disabled")
# Команда формата, которая ПЕРЕПИСЫВАЕТ файлы, в CI бессмысленна: она «пройдёт», ничего не проверив.
_WRITE_FLAGS = ("--write", "--fix", "-w")


# ── профиль и конфиг ─────────────────────────────────────────────────────────────────────────────

def _read_structured(path: Path):
    """JSON или YAML-файл -> объект | None. YAML без pyyaml -> None (честно «не прочитал»)."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    try:
        import yaml
    except ImportError:
        return None
    return yaml.safe_load(text)


def load_profile(root: Path, profile_path: Path | None = None):
    """Профиль репозитория -> (profile | None, причина | None)."""
    if profile_path is not None:
        try:
            return _read_structured(profile_path), None
        except (OSError, ValueError) as exc:
            return None, f"профиль {profile_path} не прочитан ({exc.__class__.__name__})"
    fallback_note = None
    managed = root / ".ai" / "managed"
    if (managed / "ai_ops_kit" / "shared" / "project_detector.py").is_file():
        if str(managed) not in sys.path:
            sys.path.insert(0, str(managed))
        try:
            from ai_ops_kit.shared import project_detector
            return project_detector.load_or_detect(root, write=False), None
        except Exception as exc:  # noqa: BLE001 — движок недоступен -> читаем кеш напрямую ниже
            fallback_note = f"детектор кита не загрузился ({exc.__class__.__name__})"
    cache = root / PROFILE_REL
    if not cache.is_file():
        return None, fallback_note or "профиль стека не собран (запустите `./ai-ops onboard`)"
    try:
        prof = _read_structured(cache)
    except (OSError, ValueError) as exc:
        return None, f"профиль {PROFILE_REL} не прочитан ({exc.__class__.__name__})"
    if prof is None:
        return None, "профиль не прочитан: нет pyyaml у этого python"
    return prof, None


def hook_enabled(root: Path) -> bool:
    """Владелец выключил хук (`standard.lint_hook: off`)? Нечитаемый конфиг — хук включён."""
    cfg_path = root / ".ai-ops.yaml"
    if not cfg_path.is_file():
        return True
    try:
        cfg = _read_structured(cfg_path)
    except (OSError, ValueError):
        return True
    std = cfg.get("standard") if isinstance(cfg, dict) else None
    val = std.get("lint_hook", True) if isinstance(std, dict) else True
    return (val.lower() if isinstance(val, str) else val) not in OFF_VALUES


def _stack(profile, lang: str):
    for s in (profile or {}).get("stacks") or []:
        if isinstance(s, dict) and s.get("language") == lang:
            return s
    return None


# ── команда линта для одного файла ───────────────────────────────────────────────────────────────

def _find_bin(tool: str, root: Path, start: Path | None = None):
    """Где лежит исполняемый линтер -> (путь, каталог-cwd) | (None, None).

    Node: ближайший `node_modules/.bin/<tool>` вверх от файла до корня (монорепо — у пакета свой).
    Python: `.venv/bin`, `venv/bin` дочки, затем PATH. Прочее — PATH."""
    if start is not None:
        d = start
        while True:
            cand = d / "node_modules" / ".bin" / tool
            if cand.is_file():
                return str(cand), d
            if d == root or root not in d.parents:
                break
            d = d.parent
        return None, None
    for venv in (".venv", "venv"):
        cand = root / venv / "bin" / tool
        if cand.is_file():
            return str(cand), root
    found = shutil.which(tool)
    return (found, root) if found else (None, None)


def _node_tool(root: Path, lint_cmd: str):
    """Какой линтер стоит за командой node-стека: `eslint` | `biome` | None (общерепозиторная)."""
    tokens = shlex.split(lint_cmd)
    text = lint_cmd
    if tokens and tokens[0] in ("npm", "yarn", "pnpm"):
        try:
            pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pkg = {}
        text = str(((pkg.get("scripts") or {}).get(tokens[-1])) or "")
    if "biome" in text:
        return "biome"
    if "eslint" in text or "next lint" in text:
        return "eslint"
    return None


def _as_path_arg(rel: str) -> str:
    """Путь, который линтер не примет за флаг: файл `--fix.js` или `-rf.py` агент создать может.

    `./`, а не `--`: префикс `./` — просто путь, его одинаково понимает любой из четырёх линтеров
    (ruff, flake8, eslint, biome), а разбор `--` у них не одинаков и полагаться на него не стоит."""
    return f"./{rel}" if rel.startswith("-") else rel


def per_file_command(root: Path, rel: str, lang: str, lint_cmd: str):
    """Команда линта ОДНОГО файла -> (argv, cwd, None) | (None, None, причина пропуска).

    Причина пропуска — фраза для агента: почему правка НЕ проверена."""
    tokens = shlex.split(lint_cmd)
    file_abs = root / rel
    if lang == "python":
        tool = next((t for t in ("ruff", "flake8") if any(tok.endswith(t) for tok in tokens)), None)
        if tool is None:
            return None, None, f"линт python (`{lint_cmd}`) по одному файлу не запускаю"
        exe, cwd = _find_bin(tool, root)
        if exe is None:
            return None, None, f"линтер {tool} не установлен"
        target = _as_path_arg(rel)
        argv = [exe, "check", "--force-exclude", target] if tool == "ruff" else [exe, target]
        return argv, cwd, None
    if lang == "node":
        tool = _node_tool(root, lint_cmd)
        if tool is None:
            return None, None, f"команда линта `{lint_cmd}` проверяет весь репозиторий — по файлу не запускаю"
        exe, cwd = _find_bin(tool, root, start=file_abs.parent)
        if exe is None:
            return None, None, f"линтер {tool} не установлен (нет node_modules/.bin/{tool})"
        target = _as_path_arg(os.path.relpath(file_abs, cwd))
        return ([exe, "lint", target] if tool == "biome" else [exe, target]), cwd, None
    if lang == "go":
        if "golangci-lint" not in lint_cmd:
            return None, None, f"линт go (`{lint_cmd}`) по одному пакету не запускаю"
        exe, cwd = _find_bin("golangci-lint", root)
        if exe is None:
            return None, None, "линтер golangci-lint не установлен"
        pkg_dir = Path(rel).parent.as_posix()
        return [exe, "run", "./" + pkg_dir if pkg_dir != "." else "."], cwd, None
    return None, None, f"линт `{lint_cmd}` проверяет весь проект — по файлу не запускаю"


def _cap(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    cut = lines[:MAX_LINES]
    out = "\n".join(cut)[:MAX_CHARS]
    if len(cut) < len(lines) or len(out) < len("\n".join(cut)):
        out += f"\n… (показано начало; всего строк: {len(lines)})"
    return out


def _indent(text: str) -> str:
    """Подробности под строкой отчёта CI: с отступом, чтобы сводка брала только заголовки."""
    return "\n".join("    " + ln for ln in text.splitlines())


# ── вход `hook` ──────────────────────────────────────────────────────────────────────────────────

def _note(text: str, what: str = "линтером") -> tuple:
    """Правка не проверена: выход 0 (не блокируем) + одна строка агенту."""
    payload = {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                      "additionalContext": f"AI Ops lint-hook: {text}. Правка НЕ проверена {what}."}}
    return 0, "", json.dumps(payload, ensure_ascii=False)


def _root_from_env(event: dict) -> Path:
    here = Path(__file__).resolve()
    if here.parent.as_posix().endswith(".ai/managed/templates/runtime"):
        return here.parents[4]
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or os.getcwd())


def _project_lint(root: Path, rel: str, lang: str, timeout: float) -> tuple:
    """Линтер проекта на одном файле -> ("ok" | "violation" | "note", текст)."""
    profile, why = load_profile(root)
    if profile is None:
        return "note", why
    lint_cmd = ((_stack(profile, lang) or {}).get("commands") or {}).get("lint")
    if not lint_cmd:
        return "note", f"в профиле репозитория нет команды линта для {lang}"
    argv, cwd, why = per_file_command(root, rel, lang, lint_cmd)
    if argv is None:
        return "note", why
    try:
        r = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "note", f"линтер не уложился в {TIMEOUT_S} с на {rel}"
    except OSError as exc:
        return "note", f"линтер не запустился ({exc.__class__.__name__})"
    if r.returncode == 0:
        return "ok", ""
    tool = Path(argv[0]).name
    if r.returncode != 1:                         # 1 = нарушения у ruff/flake8/eslint/biome/golangci
        first = (r.stderr or r.stdout).strip().splitlines()[:1]
        return "note", f"линтер {tool} не отработал (код {r.returncode}: {first[0] if first else '—'})"
    return "violation", (f"Линтер проекта ({tool}) нашёл нарушения в {rel}. Исправь их, прежде чем "
                         f"продолжать (правило проекта, а не кита):\n{_cap(r.stdout + chr(10) + r.stderr)}")


# ── профиль стиля AI Ops (`./ai-ops lint-profile --apply`) ───────────────────────────────────────

def _kit(root: Path):
    """Модули профиля из кода кита: рядом с хуком (managed-слой дочки или клон кита в CI), иначе
    managed-слой дочки. -> (lint_profile, lint_baseline, lint_profile_cli) | None.

    Тот же способ, что у детектора в `load_profile`: сравнение с линией — ОДНО, в коде кита, и
    хук его не повторяет."""
    for base in (Path(__file__).resolve().parents[2], root / ".ai" / "managed"):
        if (base / "ai_ops_kit" / "checks" / "lint_baseline.py").is_file():
            if str(base) not in sys.path:
                sys.path.insert(0, str(base))
            try:
                from ai_ops_kit.checks import lint_baseline, lint_profile
                from ai_ops_kit.cli import lint_profile_cli
            except Exception:  # noqa: BLE001 — старый кит без профиля: принуждать нечего
                return None
            return lint_profile, lint_baseline, lint_profile_cli
    return None


def _style_profile(root: Path, rel: str, timeout: float) -> tuple:
    """Профиль стиля на одном файле -> ("ok" | "violation" | "note" | "off", текст).

    "off" — профиль в дочке не включён (или этот файл он не судит): поведение хука прежнее."""
    kit = _kit(root)
    if kit is None:
        return "off", ""
    lp, lb, cli = kit
    tools = lp.tools_for_file(lp.applied_tools(root), rel)
    if not tools:
        return "off", ""
    if timeout < 3:
        return "note", "профиль стиля AI Ops не успел — время хука вышло на линтере проекта"
    res = lb.check(root, tools, lambda r, spec, extra=(), targets=None:
                   cli.measure(r, spec, extra, targets=targets, timeout=timeout), files=[rel])
    grown = lb.grown(res)
    if grown:
        lines = "\n".join("  " + lb.address(g) for g in grown[:MAX_LINES])
        return "violation", (f"Профиль стиля AI Ops: новое расхождение в {rel} сверх замороженного "
                             f"(существующее не в счёт, новое не проходит). Исправь:\n{lines}")
    missed = [f"{r['tool']}: {r['reason']}" for r in res["tools"] if r["status"] == lb.NOT_CHECKED]
    return ("note", "профиль стиля AI Ops проверен не весь (" + "; ".join(missed) + ")") if missed \
        else ("ok", "")


def run_hook(event: dict, root: Path | None = None) -> tuple:
    """Одна правка агента -> (код выхода, stderr, stdout). Код 2 = нарушения линта или профиля."""
    root = Path(root or _root_from_env(event)).resolve()
    raw = ((event.get("tool_input") or {}).get("file_path")) or ""
    if not raw:
        return 0, "", ""
    path = Path(raw)
    if not path.is_absolute():
        path = Path(event.get("cwd") or root) / path
    path = path.resolve()
    if root not in path.parents or not path.is_file():
        return 0, "", ""                          # вне дочки или файла уже нет — не наше
    rel = path.relative_to(root).as_posix()
    if SKIP_PARTS.intersection(Path(rel).parts[:-1]):
        return 0, "", ""                          # служебное / зависимости / сгенерированное
    lang = LANG_BY_EXT.get(path.suffix.lower())
    if lang is None or not hook_enabled(root):
        return 0, "", ""                          # не код знакомого языка или хук выключен
    started = time.monotonic()
    project = _project_lint(root, rel, lang, TIMEOUT_S)
    style = _style_profile(root, rel, TIMEOUT_S - (time.monotonic() - started))
    blocks = [text for kind, text in (project, style) if kind == "violation"]
    notes = [text for kind, text in (project, style) if kind == "note"]
    if blocks:
        return 2, "\n\n".join(blocks + [f"(также: {n})" for n in notes]), ""
    if notes:
        return _note("; ".join(notes), "линтером" if project[0] == "note" else "профилем стиля целиком")
    return 0, "", ""


# ── вход `ci` ────────────────────────────────────────────────────────────────────────────────────

def _run(argv, cwd: Path) -> tuple:
    """-> (код, вывод). Команды не нашлось -> 127, как у оболочки."""
    try:
        r = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True)
    except OSError as exc:
        return 127, f"{argv[0]}: команда не нашлась ({exc.__class__.__name__})"
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _prepare(stack: dict, root: Path, log: list) -> None:
    """--install: поставить зависимости стека, чтобы линтер дочки было ЧЕМ запускать.

    Сбой установки не валит шаг сам по себе — решает запуск линта ниже, и его отказ назовёт причину."""
    pm = stack.get("package_manager")
    if pm in ("pnpm", "yarn") and shutil.which(pm) is None and shutil.which("corepack"):
        _run(["corepack", "enable"], root)
    install = stack.get("install_command")
    if install:
        rc, out = _run(shlex.split(install), root)
        log.append(f"установка зависимостей `{install}`: " + ("ок" if rc == 0 else f"код {rc}"))
        if rc != 0:
            log.append(_indent(_cap(out)))
    lint = (stack.get("commands") or {}).get("lint") or ""
    if stack.get("language") == "python":
        for tool in ("ruff", "flake8"):
            if tool in lint and _find_bin(tool, root)[0] is None:
                rc, _ = _run([sys.executable, "-m", "pip", "install", "--quiet", tool], root)
                log.append(f"{tool} не было на раннере — поставлен тот, что объявлен командой линта: "
                           + ("ок" if rc == 0 else f"код {rc}"))


def run_ci(root: Path, profile_path: Path | None = None, install: bool = False) -> tuple:
    """Линт (+ формат, если объявлен) всей дочки -> (код выхода, строки отчёта).

    0 = объявленные команды прошли (или линтера нет — сказано предупреждением); 1 = провал."""
    root = Path(root).resolve()
    profile, why = load_profile(root, profile_path)
    if profile is None:
        return 1, [f"::error::Профиль репозитория не получен: {why}. Линт не исполнялся."]
    lines, failed, ran = [], False, 0
    for stack in profile.get("stacks") or []:
        lang = stack.get("language", "?")
        cmds = stack.get("commands") or {}
        todo = [(slot, cmds.get(slot)) for slot in ("lint", "format") if cmds.get(slot)]
        if not todo:
            lines.append(f"::warning::{lang}: линтер в репозитории не найден — код этого стека не "
                         f"проверяется ничем. Это не «проверка пройдена».")
            continue
        if install:
            _prepare(stack, root, lines)
        for slot, cmd in todo:
            if slot == "format" and any(f in shlex.split(cmd) for f in _WRITE_FLAGS):
                lines.append(f"::warning::{lang}: команда формата `{cmd}` переписывает файлы — в CI "
                             f"нужен режим проверки (например `--check`); формат НЕ проверен.")
                continue
            rc, out = _run(shlex.split(cmd), root)
            ran += 1
            if rc == 0:
                lines.append(f"{lang}: {slot} `{cmd}` — пройдено")
                continue
            failed = True
            reason = "команда не нашлась на раннере" if rc == 127 else f"код {rc}"
            lines.append(f"::error::{lang}: {slot} `{cmd}` — НЕ пройдено ({reason})")
            lines.append(_indent(_cap(out)))
    if ran == 0 and not failed:
        lines.append("::warning::Линт дочки НЕ исполнялся: ни у одного стека нет объявленного "
                     "линтера. Шаг зелёный, но код не проверен — подключите линтер.")
    failed = _ci_style_profile(root, lines) or failed
    return (1 if failed else 0), lines


def _ci_style_profile(root: Path, lines: list) -> bool:
    """Профиль стиля AI Ops в CI — та же команда, что `./ai-ops lint-profile check`. -> провал?

    Профиль не включён — ничего лишнего, одна простая строка (не предупреждение: это выбор дочки).
    Рост, «проверить нечем» или устаревший профиль — красный с ответом команды в журнале."""
    kit = _kit(root)
    if kit is None or not kit[0].applied_tools(root):
        lines.append("профиль стиля AI Ops не включён — проверяется только линтер проекта "
                     "(включить: ./ai-ops lint-profile --apply)")
        return False
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = kit[2].run_lint_profile(root, verb="check")
    if code == 0:
        lines.append("профиль стиля AI Ops: `lint-profile check` — пройдено")
        return False
    lines.append(f"::error::профиль стиля AI Ops: `lint-profile check` — НЕ пройдено (код {code})")
    lines.append(_indent(_cap(buf.getvalue())))
    return True


def _summary(lines: list) -> None:
    dst = os.environ.get("GITHUB_STEP_SUMMARY")
    if not dst:
        return
    try:
        with open(dst, "a", encoding="utf-8") as fh:
            heads = [ln.split("::")[-1] for ln in lines if not ln.startswith("    ")]
            fh.write("## Линт дочки\n\n" + "\n".join(f"- {h}" for h in heads) + "\n")
    except OSError as exc:
        print(f"сводку в GITHUB_STEP_SUMMARY записать не удалось: {exc}", file=sys.stderr)


def main(argv: list) -> int:
    mode = argv[0] if argv else "hook"
    args = argv[1:]

    def opt(name):
        return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else None

    root = opt("--root")
    if mode == "hook":
        try:
            event = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            return 0                              # не наш вход — не мешаем агенту
        code, err, out = run_hook(event if isinstance(event, dict) else {}, Path(root) if root else None)
        if out:
            print(out)
        if err:
            print(err, file=sys.stderr)
        return code
    if mode == "ci":
        prof = opt("--profile")
        code, lines = run_ci(Path(root or "."), Path(prof) if prof else None, "--install" in args)
        print("\n".join(lines))
        _summary(lines)
        return code
    print(f"lint_hook: неизвестный режим {mode!r} (hook | ci)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
