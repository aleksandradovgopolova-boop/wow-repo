#!/usr/bin/env python3
"""repo_graph.py (v3.26.0) — Repository Graph: детерминированный граф зависимостей для impact analysis.

Реализует первую ступень цепочки ContextArchitectureDecision (v3.3.2): repository = источник истины,
детерминированный граф ПЕРЕД любым семантическим поиском. Через stdlib `ast` (Python) и regex (JS/TS):
  - symbols: топ-уровневые функции/классы по файлам (symbol_index);
  - imports: зависимости модулей -> внутренние рёбра (файл A импортирует файл B репо);
  - tests: тест-файлы и что они импортируют;
  - impact(changed): обратный обход рёбер — какие файлы (транзитивно) зависят от изменённого
    (для automatic write-scope / relevant-test-selection в v3.6);
  - affected_tests(changed_files): какие тесты затронуты изменениями (v3.26 Progressive Verification).

Поддерживаемые языки: Python (ast), JavaScript/TypeScript (regex-based import parsing).

CLI: repo_graph.py [root] [--impact tools/x.py] [--affected-tests path1 path2] [--json] | --selftest
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
# v3.30: код движка переехал в пакеты ai_ops_kit/*. Без него граф видел только тонкие
# алиасы в tools/ и терял РЕАЛЬНЫХ импортёров — анализ влияния (--impact) молча
# недооценивал последствия правки.
DEFAULT_SUBDIRS = ("ai_ops_kit", "tools", "validation")
JS_TS_EXTENSIONS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")

# `.ai/` — ОБЛАСТЬ КИТА В ДОЧКЕ, А НЕ КОД ПРОДУКТА (F-022, корень).
#
# Прежде каталога здесь не было, и в дочке граф разбирал доставленную копию кита, ЕЁ БЭКАПЫ
# (`.ai/runtime/backups/<версия>/`) и копии дерева в `.ai/worktrees/<работа>/` наравне с продуктом.
# ЗАМЕР на niti (Next.js/Turborepo, собственного Python нет вовсе):
#   Python  4094 файла в графе, из них 4094 внутри `.ai/` — 100%: 1891 бэкапы, 1650 один worktree,
#           241 managed. То есть весь «граф кода продукта» состоял из кита, отражённого в себя;
#   JS/TS   2507 в графе, 1625 (64%) — копии в `.ai/worktrees`, то есть каждый реальный файл
#           продукта считался примерно трижды.
# Цена не косметическая: по этому графу считаются `impact()` и `affected_tests()` — «какие тесты
# задеты правкой». На таком входе ответ либо про чужие файлы, либо пустой.
# Видимым это стало через предупреждение из СТАРОЙ копии кита (`.ai/managed/tools/context_hybrid.py`
# версии 3.27.7, где докстринг ещё не был raw-строкой): `<unknown>:11: invalid escape sequence`.
# Свой докстринг кит починил 08.08 (8f6aac4), но у дочки лежала копия ДО этой правки — и он
# исправно разбирал её как код продукта.
PY_SKIP_DIRS = (".git", "node_modules", "venv", ".venv", "__pycache__", ".ai")
JS_SKIP_DIRS = ("node_modules", "dist", "build", ".next", "coverage", ".ai")


def _py_files(root: Path, subdirs=None, path_filter=None):
    """Найти Python файлы. Если subdirs=None — искать во всём репо."""
    out = []
    search_dirs = [root / sd for sd in subdirs] if subdirs else [root]
    for d in search_dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.py")):
            # v3.7.0: access pre-filter — denied-путь НЕ читается для графа
            if path_filter is not None and not path_filter(str(p.relative_to(root))):
                continue
            parts = p.relative_to(root).parts
            if any(skip in parts for skip in PY_SKIP_DIRS):
                continue
            out.append(p)
    return out


def _js_ts_files(root: Path, subdirs=None, path_filter=None):
    """v3.26.0: найти JS/TS файлы в указанных директориях (или во всём репо если subdirs=None)."""
    out = []
    search_dirs = [root / sd for sd in (subdirs or [""])] if subdirs else [root]
    for d in search_dirs:
        if not d.is_dir():
            continue
        for ext in JS_TS_EXTENSIONS:
            for p in sorted(d.rglob(f"*{ext}")):
                parts = p.relative_to(root).parts
                if any(skip in parts for skip in JS_SKIP_DIRS):
                    continue
                if path_filter is not None and not path_filter(str(p.relative_to(root))):
                    continue
                out.append(p)
    return out


def _analyze(path: Path):
    """(symbols, imported_module_stems) Python-файла через ast; все Import/ImportFrom (вкл. в функциях)."""
    try:
        # ИМЯ ФАЙЛА ОБЯЗАТЕЛЬНО (F-022). `ast.parse` без него подписывает предупреждения
        # интерпретатора как `<unknown>`, и владелец получал в вывод прогона строку
        # `<unknown>:11: invalid escape sequence`, по которой нельзя ни починить, ни осознанно
        # пропустить: непонятно даже, чей это файл — его продукта или кита.
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, OSError):
        return [], set()
    symbols = [n.name for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                mods.add(_stem(a.name.split(".")))
        elif isinstance(n, ast.ImportFrom):
            if n.module and n.level == 0:
                parts = n.module.split(".")
                # `from ai_ops_kit.<пакет> import <модуль>, ...` — модули перечислены в names,
                # а не в module. Без этой ветки все внутренние связи схлопывались бы в один узел
                # `ai_ops_kit`, и --impact терял бы РЕАЛЬНЫХ импортёров (v3.33; тот же класс,
                # что уже ловил переезд в v3.31 — тогда граф видел только алиасы).
                if parts[0] == "ai_ops_kit" and len(parts) == 2:
                    mods.update(a.name for a in n.names)
                else:
                    mods.add(_stem(parts))
    return symbols, mods


def _stem(parts):
    """Имя модуля, под которым он известен графу: последний сегмент внутри пакета кита,
    первый — для всего остального (stdlib и сторонние узнаются по верхнему имени)."""
    return parts[-1] if parts[0] == "ai_ops_kit" and len(parts) >= 3 else parts[0]


def _analyze_js(path: Path):
    """v3.26.0: (symbols, imported_module_stems) JS/TS файла через regex.
    Парсит: import ... from '...', import '...', require('...'), export function/class."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return [], set()

    # Symbols: export function/class/const
    symbols = re.findall(r'(?:export\s+)?(?:function|class|const|let|var)\s+(\w+)', content)
    symbols = list(dict.fromkeys(symbols))  # dedup preserving order

    # Imports: import ... from 'module', import 'module', require('module')
    mods = set()
    # ES modules: import ... from '...'
    for m in re.findall(r'''import\s+(?:.*?\s+from\s+)?['"]([^'"]+)['"]''', content):
        # Берём только relative imports (начинаются с . или ..) или внутренние модули
        if m.startswith(".") or (not m.startswith("/") and not m.startswith("@")):
            # Извлекаем имя модуля (без расширения и пути)
            mod_name = Path(m).stem if "/" in m else m.split("/")[0]
            mods.add(mod_name)
    # CommonJS: require('...')
    for m in re.findall(r'''require\s*\(\s*['"]([^'"]+)['"]\s*\)''', content):
        if m.startswith(".") or (not m.startswith("/") and not m.startswith("@")):
            mod_name = Path(m).stem if "/" in m else m.split("/")[0]
            mods.add(mod_name)

    return symbols, mods


def build_graph(root=PKG, subdirs=DEFAULT_SUBDIRS, path_filter=None, include_js=True) -> dict:
    """Построить граф зависимостей. v3.26.0: include_js=True добавляет JS/TS файлы."""
    root = Path(root)
    py_files = _py_files(root, subdirs, path_filter=path_filter)
    js_files = _js_ts_files(root, subdirs, path_filter=path_filter) if include_js else []
    all_files = py_files + js_files

    stem_to_rel = {}
    rels = []
    for f in all_files:
        rel = str(f.relative_to(root))
        rels.append((f, rel))
        stem_to_rel[f.stem] = rel   # плоское имя модуля -> путь

    file_info, symbol_index, import_edges, tests = {}, {}, {}, {}
    for f, rel in rels:
        # Выбираем анализатор по расширению
        if f.suffix in JS_TS_EXTENSIONS:
            syms, mods = _analyze_js(f)
        else:
            syms, mods = _analyze(f)
        file_info[rel] = {"symbols": syms, "imports": sorted(mods), "language": "js" if f.suffix in JS_TS_EXTENSIONS else "py"}
        for s in syms:
            symbol_index.setdefault(s, []).append(rel)
        internal = sorted({stem_to_rel[m] for m in mods if m in stem_to_rel and stem_to_rel[m] != rel})
        import_edges[rel] = internal
        stem = Path(rel).stem
        if stem.startswith("test_") or stem.endswith("_test") or stem.endswith(".test") or stem.endswith(".spec"):
            tests[rel] = internal
    return {"kind": "repository-graph", "root": str(root), "subdirs": list(subdirs) if subdirs else ["*"],
            "file_count": len(rels), "files": file_info, "symbol_index": symbol_index,
            "import_edges": import_edges, "tests": tests,
            "languages": {"python": len(py_files), "javascript": len(js_files)}}


def affected_tests(graph: dict, changed_files: list) -> list:
    """v3.26.0 Progressive Verification: какие тесты затронуты изменениями.
    changed_files — список относительных путей изменённых файлов.
    Возвращает список тест-файлов, которые импортируют изменённые файлы (транзитивно)."""
    if not changed_files:
        return []
    # Собираем все затронутые файлы (прямые + транзитивные через impact)
    all_affected = set(changed_files)
    for cf in changed_files:
        all_affected.update(impact(graph, cf))
    # Находим тесты, которые импортируют затронутые файлы
    tests = graph.get("tests", {})
    result = []
    for test_file, test_imports in tests.items():
        # Тест затронут, если любой из его импортов в all_affected
        if any(imp in all_affected for imp in test_imports):
            result.append(test_file)
        # Или если сам тест-файл в changed_files
        elif test_file in changed_files:
            result.append(test_file)
    return sorted(set(result))


def impact(graph: dict, changed_rel: str) -> list:
    """Файлы, которые (транзитивно) импортируют changed_rel — обратный обход рёбер импорта."""
    edges = graph.get("import_edges", {})
    reverse = {}
    for src, dsts in edges.items():
        for d in dsts:
            reverse.setdefault(d, set()).add(src)
    seen, stack = set(), [changed_rel]
    while stack:
        cur = stack.pop()
        for dep in reverse.get(cur, ()):
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return sorted(seen)


def main(argv):
    # позиционные аргументы, ИСКЛЮЧАЯ флаги и значение --impact/--affected-tests
    skip = set()
    for flag in ("--impact", "--affected-tests"):
        if flag in argv:
            skip.add(argv.index(flag) + 1)
    args = [a for i, a in enumerate(argv) if not a.startswith("--") and i not in skip]
    root = Path(args[0]) if args else PKG
    g = build_graph(root)
    if "--impact" in argv:
        changed = argv[argv.index("--impact") + 1]
        imp = impact(g, changed)
        if "--json" in argv:
            print(json.dumps({"changed": changed, "impact": imp}, ensure_ascii=False, indent=2))
        else:
            print(f"IMPACT {changed} -> {len(imp)} файлов: {imp}")
        return 0
    if "--affected-tests" in argv:
        idx = argv.index("--affected-tests")
        # Собираем все пути после --affected-tests до следующего флага или конца
        changed_files = []
        for i in range(idx + 1, len(argv)):
            if argv[i].startswith("--"):
                break
            changed_files.append(argv[i])
        at = affected_tests(g, changed_files)
        if "--json" in argv:
            print(json.dumps({"changed_files": changed_files, "affected_tests": at}, ensure_ascii=False, indent=2))
        else:
            print(f"AFFECTED-TESTS: {len(changed_files)} changed -> {len(at)} tests: {at}")
        return 0
    if "--json" in argv:
        print(json.dumps({k: v for k, v in g.items() if k != "files"}, ensure_ascii=False, indent=2))
    else:
        langs = g.get("languages", {})
        print(f"REPO-GRAPH: {g['file_count']} файлов (py={langs.get('python', 0)}, js={langs.get('javascript', 0)}); "
              f"символов={len(g['symbol_index'])}; тестов={len(g['tests'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
