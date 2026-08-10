#!/usr/bin/env python3
"""Python-совместимость: PEP 604 union-аннотации без future-import ломают <3.10 (v2.69).

finding квалификационного прогона (self-audit): tools/generate_artifacts.py использовал
`str | None` в аннотации функции. На Python 3.9 (дефолт macOS CommandLineTools) аннотации
вычисляются при импорте -> TypeError, и ВЕСЬ движок не грузился (ai_ops_run -> workitem ->
run_report -> generate_artifacts). Кит заявляет широкую переносимость и ставится в child с
любым python, поэтому `X | Y` в аннотациях допустим ТОЛЬКО при `from __future__ import
annotations` (PEP 563 — делает аннотации ленивыми строками, безопасно на 3.9+).

Проверка (AST, детерминированно): для каждого .py в tools/validation/installer — если есть
union-аннотация (BinOp `|` в аннотации аргумента/возврата/AnnAssign) БЕЗ future-import ->
ERROR. Так класс «падает на 3.9» ловится в CI, а не пользователем на Mac.

Использование:  validate_python_compat.py [--json] | --selftest
Возврат 0 — совместимо, 1 — есть нарушение.
"""

import argparse
import ast
import json
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
SCAN_DIRS = ["tools", "validation", "installer"]


def _has_future_annotations(tree):
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            if any(a.name == "annotations" for a in node.names):
                return True
    return False


def _union_annotation_lines(tree):
    """Номера строк аннотаций, содержащих union через `|` (ast.BinOp / ast.BitOr)."""
    def has_bitor(ann):
        return any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)
                   for n in ast.walk(ann))

    lines = []
    for node in ast.walk(tree):
        anns = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            for arg in (list(getattr(a, "posonlyargs", [])) + list(a.args) + list(a.kwonlyargs)):
                if arg.annotation:
                    anns.append(arg.annotation)
            if a.vararg and a.vararg.annotation:
                anns.append(a.vararg.annotation)
            if a.kwarg and a.kwarg.annotation:
                anns.append(a.kwarg.annotation)
            if node.returns:
                anns.append(node.returns)
        elif isinstance(node, ast.AnnAssign) and node.annotation:
            anns.append(node.annotation)
        for ann in anns:
            if has_bitor(ann):
                lines.append(ann.lineno)
    return sorted(set(lines))


def check_source(src):
    """-> список номеров строк с проблемными union-аннотациями (пусто = ок)."""
    tree = ast.parse(src)
    if _has_future_annotations(tree):
        return []
    return _union_annotation_lines(tree)


def scan(root):
    errors = []
    for d in SCAN_DIRS:
        base = root / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            for ln in check_source(p.read_text(encoding="utf-8")):
                errors.append(f"{p.relative_to(root)}:{ln}: union-аннотация `X | Y` без "
                              f"`from __future__ import annotations` — сломает Python <3.10")
    return errors


def run(as_json=False):
    errors = scan(PKG)
    if as_json:
        print(json.dumps({"schema_version": 1, "kind": "python-compat", "errors": errors},
                         ensure_ascii=False, indent=2))
    elif errors:
        print(f"PYTHON-COMPAT: {len(errors)} нарушений (сломают Python <3.10):")
        for e in errors:
            print(f"  - {e}")
    else:
        print("PYTHON-COMPAT-OK: PEP 604 union-аннотации либо отсутствуют, либо под future-import.")
    return 1 if errors else 0


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    # union без future -> нарушение (в т.ч. return, arg, AnnAssign)
    bad_arg = "def f(x: int | None):\n    return x\n"
    bad_ret = "def g() -> str | None:\n    return None\n"
    bad_var = "y: int | str = 1\n"
    expect("union в аргументе без future -> flagged", check_source(bad_arg) == [1])
    expect("union в возврате без future -> flagged", check_source(bad_ret) == [1])
    expect("union в AnnAssign без future -> flagged", check_source(bad_var) == [1])

    # с future -> ок
    good = "from __future__ import annotations\n\ndef f(x: int | None):\n    return x\n"
    expect("union под future-import -> ок", check_source(good) == [])

    # нет union -> ок
    expect("нет union -> ок", check_source("def f(x: int):\n    return x\n") == [])

    # реальный пакет: после фикса нарушений быть не должно
    expect("реальный пакет: 0 нарушений совместимости", scan(PKG) == [])

    print("validate_python_compat selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    ap = argparse.ArgumentParser(prog="validate_python_compat.py")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    return run(as_json=a.json)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
