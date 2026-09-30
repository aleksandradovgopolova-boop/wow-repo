"""Верифицированные экстракторы CLI-команд продукта (вид поверхности `cli`).

Команда продукта, объявленная в исходнике ЛИТЕРАЛЬНО (строка-имя подкоманды/декоратора или ключ
console_scripts), доказуема точным разбором → confidence: verified. Имя, собранное динамически
(переменная/f-строка/prog из переменной), verified НЕ становится — оно просто пропускается, чтобы
сила блокировки не опиралась на недоказанное (FEAT-003/004). Три стека:
  * argparse       — subparsers.add_parser + ArgumentParser(prog=...);
  * click          — @click.command()/@click.group() и @<group>.command();
  * console_scripts — [project.scripts]/[tool.poetry.scripts] (pyproject.toml) и
                      console_scripts/gui_scripts в [options.entry_points] (setup.cfg), ТЕКСТОВЫЙ
                      разбор без `tomllib` (он 3.11+, а пол кита — 3.9).
"""
from __future__ import annotations

import ast
import re

from ai_ops_kit.checks.surface_extractors._common import (
    ParsedFile,
    Surface,
    _imports_module,
    _is_str_literal,
)


def _keyword_str_literal(call: ast.Call, name: str) -> str | None:
    """Значение строкового-литерала именованного аргумента `name=` вызова, иначе None."""
    for kw in call.keywords:
        if kw.arg == name and _is_str_literal(kw.value):
            return kw.value.value
    return None


def extract_python_cli_argparse(parsed: ParsedFile) -> list:
    """CLI-команды argparse из исходника (subparsers.add_parser + ArgumentParser(prog=...)).

    Точный разбор AST в файле, который импортирует `argparse` → confidence: verified. Опознаётся:
      * `<subparsers>.add_parser("<name>", ...)` — имя подкоманды из строкового литерала;
      * `ArgumentParser(prog="<name>")` — объявленное имя программы (по возможности, тоже литерал).
    Имя-НЕ-литерал (переменная/f-строка) пропускается: verified без доказательства запрещён.
    """
    tree = parsed.tree
    if tree is None or not _imports_module(tree, "argparse"):
        return []
    out: list = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "add_parser":
            if node.args and _is_str_literal(node.args[0]):
                name = node.args[0].value
                out.append(Surface(kind="cli", ref=f"{parsed.rel_path}:{node.lineno}|{name}",
                                   confidence="verified", extractor="python-cli-argparse"))
            continue
        is_parser = (isinstance(func, ast.Name) and func.id == "ArgumentParser") or \
                    (isinstance(func, ast.Attribute) and func.attr == "ArgumentParser")
        if is_parser:
            prog = _keyword_str_literal(node, "prog")
            if prog is not None:
                out.append(Surface(kind="cli", ref=f"{parsed.rel_path}:{node.lineno}|{prog}",
                                   confidence="verified", extractor="python-cli-argparse"))
    return out


# Атрибуты-декораторы click, объявляющие команду: @click.command/@click.group, @cli.command и т.п.
_CLICK_COMMAND_ATTRS = frozenset({"command", "group"})


def _click_command_name(dec: ast.expr, func_name: str) -> str | None:
    """Имя click-команды из декоратора над функцией, либо None если это не command/group-декоратор
    ИЛИ имя задано явно, но недоказуемо.

    Явное литеральное имя (строка первым позиционным аргументом или `name="..."`) → берётся как есть.
    Явное, но НЕ литеральное имя (переменная в аргументе или `name=<переменная>`) → None: click в
    рантайме взял бы это значение, а не имя функции, поэтому доказать имя нельзя — пропускаем, как
    argparse (FEAT-003/004: verified только доказуемое). Имя из функции берётся ТОЛЬКО когда явного
    имени нет вовсе: click образует его детерминированно (проверено на click 8.5.0) —
    `lower()`+`_`→`-`, затем срез хвостового суффикса `command/cmd/group/grp` (build_command→build);
    хвостовой одиночный `_` НЕ срезается (list_→"list-").
    """
    if isinstance(dec, ast.Call):
        target, args, keywords = dec.func, dec.args, dec.keywords
    else:
        target, args, keywords = dec, [], []
    is_cmd = (isinstance(target, ast.Attribute) and target.attr in _CLICK_COMMAND_ATTRS) or \
             (isinstance(target, ast.Name) and target.id in _CLICK_COMMAND_ATTRS)
    if not is_cmd:
        return None
    name_kw = next((kw for kw in keywords if kw.arg == "name"), None)
    explicit = bool(args) or name_kw is not None
    if args and _is_str_literal(args[0]):
        return args[0].value
    if name_kw is not None and _is_str_literal(name_kw.value):
        return name_kw.value.value
    if explicit:
        return None  # явное имя задано, но не литерал — доказать нельзя, пропускаем (симметрия с argparse)
    # Без явного имени click образует имя из имени функции ДЕТЕРМИНИРОВАННО (проверено на click 8.5.0):
    # `name.lower().replace("_","-")`, затем срезает хвостовой суффикс command/cmd/group/grp по `-`.
    # Хвостовой `_` при этом НЕ срезается (list_ -> "list-"). Воспроизводим точно, чтобы symbol под
    # verified совпадал с реальной командой click.
    name = func_name.lower().replace("_", "-")
    left, sep, suffix = name.rpartition("-")
    if sep and suffix in {"command", "cmd", "group", "grp"}:
        name = left
    return name


def extract_python_cli_click(parsed: ParsedFile) -> list:
    """CLI-команды click из исходника (@click.command()/@click.group() и @<group>.command()).

    Точный разбор AST в файле, который импортирует `click` → confidence: verified. Имя команды —
    из декоратора (литерал/`name=`) или из имени функции по правилу именования click. symbol в ref —
    имя команды. Файлы без импорта click не трогаются (иначе чужой `.command` дал бы ложный verified).
    """
    tree = parsed.tree
    if tree is None or not _imports_module(tree, "click"):
        return []
    out: list = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            name = _click_command_name(dec, node.name)
            if name is not None:
                out.append(Surface(kind="cli", ref=f"{parsed.rel_path}:{node.lineno}|{name}",
                                   confidence="verified", extractor="python-cli-click"))
                break   # одна команда на функцию (несколько click-декораторов = одна поверхность)
    return out


# ─── console_scripts (объявленные точки входа) ───────────────────────────────────────────────────
# Имя команды в [project.scripts]/[project.gui-scripts]/[tool.poetry.scripts] (pyproject.toml) или
# в console_scripts/gui_scripts секции [options.entry_points] (setup.cfg) — ОБЪЯВЛЕННАЯ точка входа
# дистрибутива, доказуемо verified. Текстовый разбор, БЕЗ `tomllib` (он 3.11+, а пол кита — 3.9;
# собственный validate_python_compat этот импорт отклоняет): один построчный сканер, один ответ на
# всех версиях. Битый файл — пропуск (см. изоляцию в extract_surfaces).

_SECTION_RE = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]\s*$")
_TOML_SCRIPT_SECTIONS = frozenset({"project.scripts", "project.gui-scripts", "tool.poetry.scripts"})
_CFG_ENTRY_KEYS = frozenset({"console_scripts", "gui_scripts"})


def _console_scripts_pyproject(parsed: ParsedFile) -> list:
    """Ключи-имена команд из script-таблиц pyproject.toml (по одному ключу на строку `name = ...`)."""
    out: list = []
    section = ""
    for i, raw in enumerate(parsed.source.splitlines(), start=1):
        line = "" if raw.lstrip().startswith("#") else raw.split("#", 1)[0]
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1).strip().strip("\"'")
            continue
        if section not in _TOML_SCRIPT_SECTIONS or "=" not in line:
            continue
        name = line.split("=", 1)[0].strip().strip("\"'")
        if name:
            out.append(Surface(kind="cli", ref=f"{parsed.rel_path}:{i}|{name}",
                               confidence="verified", extractor="python-console-scripts"))
    return out


def _console_scripts_setupcfg(parsed: ParsedFile) -> list:
    """Имена команд из console_scripts/gui_scripts в [options.entry_points] setup.cfg (INI).

    Значение ключа — многострочный блок с отступом: каждая вложенная строка `name = target`.
    Собираем `name` вложенных строк, пока идёт отступной блок нужного ключа.
    """
    out: list = []
    section = ""
    collecting = False   # внутри блока console_scripts/gui_scripts (собираем вложенные строки)
    for i, raw in enumerate(parsed.source.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        m = _SECTION_RE.match(raw)
        if m:
            section = m.group(1).strip()
            collecting = False
            continue
        if section != "options.entry_points":
            collecting = False
            continue
        if not raw[:1].isspace():   # ключ секции на нулевой колонке
            key = raw.split("=", 1)[0].split(":", 1)[0].strip()
            collecting = key in _CFG_ENTRY_KEYS
            continue
        if collecting and "=" in raw:   # вложенная строка блока: name = target
            name = raw.split("=", 1)[0].strip()
            if name:
                out.append(Surface(kind="cli", ref=f"{parsed.rel_path}:{i}|{name}",
                                   confidence="verified", extractor="python-console-scripts"))
    return out


def extract_console_scripts(parsed: ParsedFile) -> list:
    """console_scripts / gui_scripts из pyproject.toml или setup.cfg дочки (объявленные точки входа)."""
    basename = parsed.rel_path.rsplit("/", 1)[-1]
    if basename == "pyproject.toml":
        return _console_scripts_pyproject(parsed)
    if basename == "setup.cfg":
        return _console_scripts_setupcfg(parsed)
    return []
