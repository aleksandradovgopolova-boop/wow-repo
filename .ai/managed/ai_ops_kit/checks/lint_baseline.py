"""Базовая линия профиля стиля: существующее заморожено, новое краснеет, счётчик ходит вниз (#1183).

ЗАЧЕМ. Включение профиля на ии-среде нашло бы тысячи имён не латиницей в работающем коде. Требовать
переименовать всё в день включения — значит уронить main дочки, и профиль просто выключат. Поэтому
существующее ЗАМОРАЖИВАЕТСЯ счётчиком «файл × правило», новое сверх счётчика краснеет, исправленное
сокращает счётчик. Вырасти линия не может: рост и есть новое расхождение.

ЗАПУСК ИНСТРУМЕНТА — СНАРУЖИ. Модуль в слое primitives и процессов не запускает (AC-01): функцию
замера передаёт точка входа (`cli.lint_profile_cli.measure`), тесты — подделку.

ДВА МЕХАНИЗМА, ОДИН СМЫСЛ.
  * ESLint ≥ 9.24 — РОДНЫЕ массовые подавления (`--suppress-rule`, `--prune-suppressions`, файл
    `.ai/project/lint/eslint-suppressions.json`). Проверено на ESLint 10 ии-среды: превышение
    счётчика в файле возвращает ВСЕ находки этого правила в файле, неиспользованные подавления
    снимает `--prune-suppressions`. Родной механизм предпочтён своему: его понимают редакторы и
    сам ESLint, второй правды о тех же числах не появляется.
  * Прочие инструменты (Ruff, import-linter, golangci-lint, ast-grep) и ESLint старше 9.24 —
    файл кита `.ai/project/lint/baseline.json` той же формы (`{инструмент: {файл: {правило:
    {"count": N}}}}`) и сравнение, которое краснеет ТОЛЬКО на росте.

ТРИ ТОЧКИ ПРИНУЖДЕНИЯ — ОДНА ЛОГИКА (#1183, `lint-profile-enforced-everywhere`). Линию сверяет не только
`./ai-ops lint-profile check`: хук правки агента (`templates/runtime/lint_hook.py`, ОДИН файл —
`check(..., files=[файл])`), шаг child-CI (та же команда `check`) и доказательство `lint_passed`
прогона кита (`gates.evidence_collector`). Командную строку инструмента и разбор его вывода собирает
`run_tool` здесь же; сам процесс запускает переданный снаружи `run(argv, cwd) -> (код, вывод)` —
у команды и хука это subprocess, у гейта — Tool Broker. Третьей копии сравнения не появляется.

ПЕРЕЗАМОРОЗКА НЕ ПРОИСХОДИТ САМА. Повторное `--apply` линию не трогает: иначе рост можно было бы
«отмыть» пересборкой. Заморозить заново — осознанно, `--apply --force`, и это видно в диффе файла линии.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from ai_ops_kit.checks import lint_profile as lp

BASELINE_REL = f"{lp.LINT_DIR}/baseline.json"
KIND = "ai-ops-lint-baseline"
ESLINT_NATIVE_MIN = (9, 24)

OK, GREW, NOT_CHECKED = "ok", "grew", "not_checked"
# Инструменты, пишущие результат в файл (а не в stdout): вывод не зависит от лимита исполнителя.
FILE_OUTPUT = ("eslint", "ruff", "golangci-lint")
_EXE = {"import-linter": "lint-imports"}
RULE_RU = (("ai-ops/id-match", "имя не латиницей"),
           ("ai-ops/naming-convention", "имя типа не латиницей"),
           ("ai-ops/no-restricted-imports", "импорт через границу слоя"),
           ("PLC24", "имя не латиницей"), ("N", "имя не по соглашениям Python"),
           ("asciicheck", "имя не латиницей"), ("revive", "имя не по соглашениям Go"),
           ("depguard", "импорт через границу слоя"), ("import-linter", "импорт через границу слоя"),
           ("ai-ops-latin", "имя не латиницей"))


def rule_ru(rule: str) -> str:
    return next((ru for pfx, ru in RULE_RU if str(rule).startswith(pfx)), str(rule))


def address(g: dict) -> str:
    """Строка роста для человека: где, что и насколько выросло."""
    where = g["file"] + (":" + str(g["lines"][0]) if g.get("lines") else "")
    return f"{where} — {rule_ru(g['rule'])} (было {g['was']}, стало {g['now']})"


def grown(res: dict) -> list:
    """Все строки роста результата `check` (по всем инструментам)."""
    return [g for r in res.get("tools") or [] for g in r.get("grown") or []]


# ── Файл линии кита. ──────────────────────────────────────────────────────────────────────────
def load(child_root) -> dict:
    p = Path(child_root) / BASELINE_REL
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) and data.get("kind") == KIND else {}


def save(child_root, tools: dict) -> None:
    p = Path(child_root) / BASELINE_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    body = {"schema_version": 1, "kind": KIND,
            "note": "заморожено профилем стиля AI Ops: вправе только сокращаться "
                    "(./ai-ops lint-profile check --apply)",
            "tools": {t: _as_file(c) for t, c in sorted(tools.items())}}
    p.write_text(json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                 encoding="utf-8")


def _as_file(counts: dict) -> dict:
    return {f: {r: {"count": n} for r, n in sorted(rules.items()) if n > 0}
            for f, rules in sorted(counts.items()) if any(n > 0 for n in rules.values())}


def _from_file(section: dict) -> dict:
    out = {}
    for f, rules in (section or {}).items():
        for r, v in (rules or {}).items():
            n = v.get("count", 0) if isinstance(v, dict) else v
            if isinstance(n, int) and n > 0:
                out.setdefault(f, {})[r] = n
    return out


def counts(findings) -> dict:
    """[(файл, правило, строка)] -> {файл: {правило: число}}."""
    out: dict = {}
    for f, r, _line in findings:
        out.setdefault(f, {}).setdefault(r, 0)
        out[f][r] += 1
    return out


# ── Сравнение: краснеет только рост. ──────────────────────────────────────────────────────────
def compare(frozen: dict, current: dict, findings=()) -> dict:
    """Линия против текущего замера. -> grown/shrunk + ужатая линия (рост в неё НЕ впитывается).

    Ключ, которого в линии нет, заморожен нулём: новый файл с находкой — рост. Ужатая линия —
    минимум из двух по каждому ключу; нули выбрасываются.
    """
    lines = {}
    for f, r, line in findings:
        lines.setdefault((f, r), []).append(line)
    grown, shrunk, tight = [], [], {}
    for f in sorted(set(frozen) | set(current)):
        for r in sorted(set(frozen.get(f, {})) | set(current.get(f, {}))):
            was, now = frozen.get(f, {}).get(r, 0), current.get(f, {}).get(r, 0)
            if now > was:
                grown.append({"file": f, "rule": r, "was": was, "now": now,
                              "lines": sorted(lines.get((f, r), []))[:10]})
            elif now < was:
                shrunk.append({"file": f, "rule": r, "was": was, "now": now})
            if min(was, now) > 0:
                tight.setdefault(f, {})[r] = min(was, now)
    return {"grown": grown, "shrunk": shrunk, "tightened": tight,
            "frozen": sum(n for rs in frozen.values() for n in rs.values()),
            "now": sum(n for rs in current.values() for n in rs.values())}


# ── Разбор вывода инструментов -> [(файл, правило, строка)]. ───────────────────────────────────
def _rel(root: Path, path: str) -> str:
    p = Path(path)
    try:
        return p.resolve().relative_to(root.resolve()).as_posix() if p.is_absolute() else p.as_posix()
    except ValueError:
        return p.as_posix()


def parse_eslint(text: str, root, prefix: str = "ai-ops/") -> tuple:
    """JSON ESLint -> (неподавленные находки профиля, {(файл, правило): подавлено})."""
    root = Path(root)
    found, suppressed = [], {}
    for item in json.loads(text or "[]"):
        f = _rel(root, item.get("filePath", ""))
        for m in item.get("messages") or []:
            if str(m.get("ruleId") or "").startswith(prefix):
                found.append((f, m["ruleId"], m.get("line", 0)))
        for m in item.get("suppressedMessages") or []:
            if str(m.get("ruleId") or "").startswith(prefix):
                key = (f, m["ruleId"])
                suppressed[key] = suppressed.get(key, 0) + 1
    return found, suppressed


def parse_ruff(text: str, root, rules) -> list:
    root = Path(root)
    return [(_rel(root, d.get("filename", "")), d["code"], (d.get("location") or {}).get("row", 0))
            for d in json.loads(text or "[]")
            if d.get("code") and any(d["code"].startswith(r) for r in rules)]


def parse_golangci(text: str, root, linters) -> list:
    root = Path(root)
    data = json.loads(text or "{}") or {}
    return [(_rel(root, (i.get("Pos") or {}).get("Filename", "")), i.get("FromLinter"),
             (i.get("Pos") or {}).get("Line", 0))
            for i in data.get("Issues") or [] if i.get("FromLinter") in linters]


def parse_ast_grep(text: str, root) -> list:
    root = Path(root)
    return [(_rel(root, m.get("file", "")), m.get("ruleId", "ast-grep"),
             ((m.get("range") or {}).get("start") or {}).get("line", 0) + 1)
            for m in json.loads(text or "[]")]


def parse_import_linter(text: str) -> list:
    """Текст lint-imports -> находки: модуль-импортёр × «кто кому не разрешён» × строка."""
    found, contract = [], None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.endswith(":") and " is not allowed to import " in line:
            contract = "import-linter:" + line[:-1].replace(" is not allowed to import ", "->")
        elif contract and line.startswith("-") and " -> " in line:
            importer = line.lstrip("- ").split(" -> ")[0].strip()
            lno = line.rsplit("(l.", 1)[-1].split(",")[0].rstrip(")") if "(l." in line else "0"
            found.append((importer, contract, int(lno) if lno.isdigit() else 0))
    return found


# ── Запуск инструмента: argv и разбор здесь, сам процесс — у вызывающего. ────────────────────
def executable(root, tool: str):
    """Исполняемый файл инструмента профиля: локальный (node_modules/.bin, .venv/bin), затем PATH."""
    name = _EXE.get(tool, tool)
    for cand in (Path(root) / "node_modules" / ".bin" / name, Path(root) / ".venv" / "bin" / name):
        if cand.is_file():
            return str(cand)
    return shutil.which(name)


def _paths(targets) -> list:
    """Пути, которые инструмент не примет за флаги (`-rf.py` -> `./-rf.py`)."""
    return [f"./{t}" if str(t).startswith("-") else str(t) for t in targets] if targets else ["."]


def tool_argv(spec: dict, exe: str, out_file: str, extra=(), targets=None):
    """Командная строка инструмента профиля -> argv | None (по файлам этот инструмент не умеет).

    targets — репо-относительные файлы (None — весь проект). golangci-lint проверяет ПАКЕТ, поэтому
    файл превращается в свой каталог, а лишнее отсекает `check` фильтром по файлам."""
    tool, cfg, paths = spec["tool"], spec["config"], _paths(targets)
    if tool == "eslint":
        return [exe, "-c", cfg, "-f", "json", "-o", out_file, *extra, *paths]
    if tool == "ruff":
        return [exe, "check", "--config", cfg, "--output-format", "json", "--output-file", out_file,
                "--exit-zero", "--force-exclude", *paths]
    if tool == "golangci-lint":
        pkgs = (sorted({"./" + Path(t).parent.as_posix() for t in targets} - {"./."}
                       | ({"."} if any(Path(t).parent == Path(".") for t in targets) else set()))
                if targets else ["./..."])
        return [exe, "run", "-c", cfg, "--output.json.path", out_file, "--issues-exit-code", "0", *pkgs]
    if tool == "ast-grep":
        return [exe, "scan", "--rule", cfg, "--json=compact", *paths]
    if tool == "import-linter" and not targets:
        return [exe, "--config", cfg]
    return None


def parse_run(spec: dict, rc, text: str, root) -> tuple:
    """Код и вывод инструмента -> (находки|None, подавлено, причина-если-не-вышло)."""
    tool = spec["tool"]
    tail = (text or "").strip()[-300:]
    try:
        if tool == "eslint":
            if rc == 2 or not (text or "").strip().startswith("["):
                return None, {}, "ESLint не отработал: " + tail
            found, supp = parse_eslint(text, root, spec.get("rule_prefix", "ai-ops/"))
            return found, supp, None
        if tool == "golangci-lint":
            return ((parse_golangci(text, root, spec.get("rules") or ()), {}, None) if rc == 0
                    else (None, {}, "golangci-lint не отработал: " + tail))
        if tool == "import-linter":
            return ((parse_import_linter(text), {}, None) if rc in (0, 1)
                    else (None, {}, "lint-imports не отработал: " + tail))
        if rc != 0 and not (text or "").strip().startswith(("[", "{")):
            return None, {}, f"{tool} не отработал: " + tail
        return ((parse_ruff(text, root, spec.get("rules") or ()) if tool == "ruff"
                 else parse_ast_grep(text, root)), {}, None)
    except (ValueError, KeyError) as e:
        return None, {}, f"{tool}: вывод не разобран ({e})"


def run_tool(root, spec: dict, run, extra=(), targets=None) -> tuple:
    """Прогнать инструмент профиля -> (находки|None, подавлено, причина). Процесс — через `run`."""
    root = Path(root)
    tool = spec["tool"]
    exe = executable(root, tool)
    if not exe:
        return None, {}, f"{tool} не найден — проверить нечем"
    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        argv = tool_argv(spec, exe, tmp, extra, targets)
        if argv is None:
            return None, {}, f"{tool} проверяет проект целиком — по одному файлу не запускаю"
        rc, out = run(argv, root)
        text = out
        if tool in FILE_OUTPUT:
            try:
                text = Path(tmp).read_text(encoding="utf-8") or out
            except OSError:
                text = out
        return parse_run(spec, rc, text, root)
    finally:
        Path(tmp).unlink(missing_ok=True)


# ── Версия ESLint и состояние линии. ────────────────────────────────────────────────────────
def eslint_version(root) -> tuple:
    try:
        v = json.loads((Path(root) / "node_modules" / "eslint" / "package.json")
                       .read_text(encoding="utf-8")).get("version", "0")
        return tuple(int(x) for x in v.split(".")[:2])
    except (OSError, ValueError):
        return (0, 0)


def _native(root: Path, spec: dict) -> bool:
    return spec["tool"] == "eslint" and eslint_version(root) >= ESLINT_NATIVE_MIN


def _suppression_counts(root: Path, spec: dict) -> dict | None:
    p = root / spec["suppressions"]
    try:
        return _from_file(json.loads(p.read_text(encoding="utf-8"))) if p.is_file() else None
    except (OSError, ValueError):
        return None


# ── Заморозка и проверка. ─────────────────────────────────────────────────────────────────────
def freeze(child_root, tools: list, measure, force: bool = False) -> list:
    """Заморозить текущие находки профиля. Существующую линию без `force` не трогает (см. модуль).

    `measure(root, spec, extra) -> (находки|None, подавлено, причина)` — запуск инструмента. Он
    передаётся снаружи: процесс запускает точка входа (cli), а логика линии остаётся здесь, в
    слое без инфраструктуры (AC-01).
    """
    root = Path(child_root)
    kit = _from_kit(root)
    results = []
    for spec in tools:
        t = spec["tool"]
        if _native(root, spec):
            if _suppression_counts(root, spec) is not None and not force:
                results.append({"tool": t, "status": "kept"})
                continue
            if force:
                (root / spec["suppressions"]).unlink(missing_ok=True)
            (root / spec["suppressions"]).parent.mkdir(parents=True, exist_ok=True)
            extra = ["--suppressions-location", spec["suppressions"]]
            for r in spec.get("rules") or ["ai-ops/id-match", "ai-ops/no-restricted-imports"]:
                extra += ["--suppress-rule", r]
            found, _, why = measure(root, spec, extra)
            frozen = _suppression_counts(root, spec) or {}
            results.append({"tool": t, "status": NOT_CHECKED if found is None else "frozen",
                            "reason": why, "frozen": sum(sum(v.values()) for v in frozen.values()),
                            "native": True})
            continue
        if t in kit and not force:
            results.append({"tool": t, "status": "kept"})
            continue
        found, _, why = measure(root, spec)
        if found is None:
            results.append({"tool": t, "status": NOT_CHECKED, "reason": why})
            continue
        kit[t] = counts(found)
        results.append({"tool": t, "status": "frozen", "frozen": len(found), "native": False})
    if any(r["status"] == "frozen" and not r.get("native") for r in results):
        save(root, kit)
    return results


def _from_kit(root: Path) -> dict:
    return {t: _from_file(s) for t, s in (load(root).get("tools") or {}).items()}


def _only(files, frozen: dict, found: list, supp: dict) -> tuple:
    """Сузить линию и замер до `files`: чужие файлы не сверяются, их находки не в счёт."""
    keep = set(files)
    return ({f: v for f, v in frozen.items() if f in keep}, [x for x in found if x[0] in keep],
            {k: n for k, n in supp.items() if k[0] in keep})


def check(child_root, tools: list, measure, tighten: bool = False, files=None) -> dict:
    """Сверить текущее с линией. tighten — ужать линию под исправленное (рост не впитывается).

    files — сверить только эти файлы (хук правки): инструмент зовётся на них (`measure(...,
    targets=files)`), линия и находки сужаются до них. Ужатие при этом не делается — по части
    проекта линию не переписывают."""
    root = Path(child_root)
    kit = _from_kit(root)
    rows, kit_changed = [], False
    tighten = tighten and not files
    for spec in tools:
        t = spec["tool"]
        native = _native(root, spec)
        frozen = _suppression_counts(root, spec) if native else kit.get(t)
        if frozen is None:
            rows.append({"tool": t, "status": NOT_CHECKED,
                         "reason": "линия не заморожена — сначала ./ai-ops lint-profile --apply"})
            continue
        extra = ["--suppressions-location", spec["suppressions"],
                 "--pass-on-unpruned-suppressions"] if native else []
        found, supp, why = measure(root, spec, extra, targets=list(files)) if files \
            else measure(root, spec, extra)
        if found is None:
            rows.append({"tool": t, "status": NOT_CHECKED, "reason": why})
            continue
        if files:
            frozen, found, supp = _only(files, frozen, found, supp)
        if native:
            # Превышение счётчика ESLint возвращает ВСЕ находки правила в файле, в пределах — все
            # подавлены. Поэтому «стало» по ключу = подавленные + неподавленные.
            current: dict = {}
            for (f, r), n in supp.items():
                current.setdefault(f, {})[r] = n
            for f, rs in counts(found).items():
                for r, n in rs.items():
                    current.setdefault(f, {})[r] = current.get(f, {}).get(r, 0) + n
        else:
            current = counts(found)
        cmp = compare(frozen, current, found)
        row = {"tool": t, "status": GREW if cmp["grown"] else OK, "native": native, **cmp}
        if tighten and cmp["shrunk"] and not cmp["grown"]:
            if native:
                measure(root, spec, ["--suppressions-location", spec["suppressions"],
                                      "--prune-suppressions"])
            else:
                kit[t], kit_changed = cmp["tightened"], True
            row["tightened_applied"] = True
        rows.append(row)
    if kit_changed:
        save(root, kit)
    return {"status": _overall(rows), "tools": rows}


def _overall(rows: list) -> str:
    if any(r["status"] == GREW for r in rows):
        return GREW
    if not rows or all(r["status"] == NOT_CHECKED for r in rows):
        return NOT_CHECKED
    return "partial" if any(r["status"] == NOT_CHECKED for r in rows) else OK
