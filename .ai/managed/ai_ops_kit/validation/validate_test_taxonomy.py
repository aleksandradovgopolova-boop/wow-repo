#!/usr/bin/env python3
"""Ратчет доли ПОВЕДЕНЧЕСКИХ тестов против СТРУКТУРНЫХ в tests/.

ЗАЧЕМ. Перекос «читаем свои файлы вместо того, чтобы исполнять поведение» рос молча: числа не
было. Тест, который только открывает yaml/md/py по пути и утверждает на их СТРУКТУРЕ, не ловит
ни одной регрессии продукта — он проверяет форму репозитория, а не работу кода. Половина набора
такова, и без метрики эта половина могла тихо расти. Здесь метрика заведена и защищена ратчетом
вниз — ровно как потолки размера функций: доля поведенческих тестов не может незаметно упасть.

КЛАССИФИКАЦИЯ (по AST каждого `tests/**/test_*.py`, а не по хрупкому regex):

  ПОВЕДЕНЧЕСКИЙ = файл (1) ИМПОРТИРУЕТ продуктовый код кита И (2) ЗОВЁТ импортированный символ —
  то есть реально исполняет поведение. Учитываются три механизма подключения продукта:
    * пакетный импорт        `from ai_ops_kit.<пакет> import f` / `import ai_ops_kit...`;
    * шим совместимости      `from tools.x import ...`, `import installer...`, плоское
                             `import validate_x` (уходящий слой алиасов, AGENTS.md);
    * динамическая загрузка   `importlib.util.spec_from_file_location(..., <путь к коду кита>)`,
                             `importlib.import_module(...)`, `runpy.run_path(...)` и запуск
                             валидатора через `subprocess` — когда аргумент ссылается на файл кита.
  Сигнал «зовёт» — узел Call: либо прямой вызов импортированного имени `f(...)`, либо вызов
  атрибута на алиасе продукта `mod.f(...)` (включая модуль, который вернула pytest-фикстура,
  грузящая продукт), либо запуск кода кита через subprocess.

  СТРУКТУРНЫЙ = всё остальное: файл только открывает файлы/манифесты репозитория и утверждает на
  их содержимом/структуре, продуктовый код не зовёт. Определяется как ДОПОЛНЕНИЕ к поведенческому:
  если поведение не исполняется — тест структурный, что бы он ни читал.

ЧЕСТНАЯ ГРАНИЦА (эвристика НЕ идеальна — называем, а не переоцениваем):
  * Смещение — в сторону СТРУКТУРНОГО: при сомнении файл считается структурным. Переоценка
    поведенческих завысила бы долю и ОСЛАБИЛА бы ратчет (ложный green), поэтому неуверенность
    трактуется консервативно.
  * Параметрический selftest-раннер, который в рантайме НАХОДИТ файлы (`parametrize` над `glob`) и
    прогоняет над каждым продуктовый валидатор, по букве определения зовёт продуктовый код — но
    ПОВЕДЕНИЯ не называет (имя теста — «прогнать над всеми»). Такой файл может попасть в
    поведенческие; это известная пограничная зона, а не точная классификация.
  * Кросс-функциональный поток данных отслеживается только для pytest-фикстур, грузящих продукт.
    Модуль, загруженный в одной функции и переданный в другую не через фикстуру, может быть
    не распознан как продуктовый алиас (уклон снова в структурную сторону).

ADVISORY (вторичный функциональный счётчик). Единица энфорсимой метрики — ФАЙЛ, поэтому
структурные assert, дописанные в уже поведенческий файл, дают 0 к знаменателю: долю можно тихо
«разбавить». Чтобы это было ВИДНО (не меняя энфорсмент), рядом печатается функциональная доля —
единицей берётся каждая `def test_*` (см. `measure_functions`). Строка помечена «advisory, не
энфорсится» и НЕ влияет на exit code; расхождение с файловой долей и есть мера разбавления.

РАТЧЕТ ходит ТОЛЬКО ВНИЗ по СТРУКТУРНОСТИ: доля поведенческих не может упасть ниже baseline.
Сравнение точное, без плавающей арифметики (перекрёстное умножение целых):
    current_behavioral * baseline_total  >=  baseline_behavioral * current_total
Файлы добавлять можно; уронить долю поведенческих — нельзя. Осознанное снижение baseline
записывается в `packages/test-taxonomy-baseline.yaml` (лента `raises` с обоснованием).

Использование:
  validate_test_taxonomy.py              # проверить долю против baseline (0 — ОК, 1 — пробой)
  validate_test_taxonomy.py --report     # печать классификации по каждому файлу без проверки
  validate_test_taxonomy.py --baseline   # записать baseline текущим замером

Возврат 0 — доля поведенческих не ниже baseline, 1 — упала (или baseline не читается).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
           Path(__file__).resolve().parents[1])
TESTS_DIR = PKG / "tests"
BASELINE_FILE = PKG / "packages" / "test-taxonomy-baseline.yaml"

# Корни продуктового кода кита. Импорт/загрузка из них = подключение продукта.
# `installer` и `tools` — точки входа и уходящий слой алиасов (AGENTS.md), тоже продукт.
PRODUCT_IMPORT_ROOTS = ("ai_ops_kit", "tools", "installer")


def _flat_product_modules(pkg_root: Path = PKG) -> frozenset[str]:
    """Плоские имена-модули, которые импортируют ПРОДУКТ через шим (AGENTS.md).

    Уходящий слой совместимости даёт плоские алиасы: `import context_engine` резолвится в
    `ai_ops_kit/context/context_engine.py`, `import validate_x` — в валидатор. Отличить такой импорт от stdlib
    можно ФАКТОМ: существует ли файл `tools/<имя>.py` или `ai_ops_kit/validation/<имя>.py`. Это
    честнее списка-памяти — множество считается из самого репозитория.
    """
    stems: set[str] = set()
    for sub in ("tools", "ai_ops_kit/validation"):
        d = pkg_root / sub
        if d.is_dir():
            for p in d.glob("*.py"):
                if not p.name.startswith("_"):
                    stems.add(p.stem)
    return frozenset(stems)


PRODUCT_FLAT_MODULES = _flat_product_modules()
# Токены путей, по которым узнаём динамическую загрузку кода кита (в аргументе spec_from_file_location
# / import_module / run_path / subprocess). Ищутся в развёрнутом исходнике выражения-аргумента.
PRODUCT_PATH_TOKENS = ("ai_ops_kit", "installer", "validation", "tools", "ai_ops.py")
# Динамические загрузчики продукта: их вызов с аргументом-путём кита = импорт продукта.
DYNAMIC_LOADERS = ("spec_from_file_location", "import_module", "run_path", "load_module",
                   "run_module")
# Запуск кода кита процессом = исполнение поведения.
SUBPROCESS_RUNNERS = ("run", "check_output", "check_call", "Popen", "call")


def _expr_src(node: ast.AST) -> str:
    """Развёрнутый исходник выражения (для поиска токенов пути). Пустая строка при неудаче."""
    try:
        return ast.unparse(node)
    except (ValueError, AttributeError, RecursionError, TypeError):
        return ""


def _refs_product_path(node: ast.AST) -> bool:
    """Ссылается ли выражение на путь к коду кита (по токенам)."""
    src = _expr_src(node)
    return any(tok in src for tok in PRODUCT_PATH_TOKENS)


def _is_fixture(fn: ast.AST) -> bool:
    """Помечена ли функция как pytest-фикстура (`@pytest.fixture` / `@fixture`)."""
    for dec in getattr(fn, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = target.attr if isinstance(target, ast.Attribute) else \
            (target.id if isinstance(target, ast.Name) else "")
        if name == "fixture":
            return True
    return False


def _collect_product_bindings(tree: ast.AST) -> tuple[set[str], set[str], bool]:
    """Собрать имена, привязанные к продукту кита.

    -> (product_funcs, product_aliases, imports_product):
       product_funcs   — имена, вызов которых напрямую `f(...)` = вызов продукта;
       product_aliases — имена-алиасы, вызов атрибута `x.f(...)` = вызов продукта;
       imports_product — виден ли вообще импорт/загрузка продукта.
    """
    product_funcs: set[str] = set()
    product_aliases: set[str] = set()
    imports_product = False

    def note_dynamic_assign(targets, value):
        """Присваивание вида `mod = module_from_spec(...)` / `import_module(<путь кита>)`."""
        if not isinstance(value, ast.Call):
            return False
        callee = value.func
        cname = callee.attr if isinstance(callee, ast.Attribute) else \
            (callee.id if isinstance(callee, ast.Name) else "")
        # module_from_spec(spec) — продукт, если рядом был spec к пути кита (проверяем по всему файлу
        # через imports_product ниже); import_module/run_path — по аргументу.
        is_loader = cname in ("module_from_spec", *DYNAMIC_LOADERS)
        if not is_loader:
            return False
        if cname == "module_from_spec" or _refs_product_path(value):
            for t in targets:
                if isinstance(t, ast.Name):
                    product_aliases.add(t.id)
            return True
        return False

    for node in ast.walk(tree):
        # --- статические импорты ---
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            top = mod.split(".")[0]
            if top in PRODUCT_IMPORT_ROOTS or top in PRODUCT_FLAT_MODULES:
                imports_product = True
                for a in node.names:
                    # Имя может оказаться и функцией (вызов `f(...)`), и подмодулем/классом (вызов
                    # атрибута `mod.f(...)`) — например `from ai_ops_kit.engine import acceptance_verify
                    # as av` с последующим `av.run(...)`. Кладём в оба множества.
                    bound = a.asname or a.name
                    product_funcs.add(bound)
                    product_aliases.add(bound)
        elif isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                if top in PRODUCT_IMPORT_ROOTS or top in PRODUCT_FLAT_MODULES:
                    imports_product = True
                    product_aliases.add(a.asname or top)
        # --- динамические загрузчики (spec_from_file_location / import_module / run_path) ---
        elif isinstance(node, ast.Call):
            callee = node.func
            cname = callee.attr if isinstance(callee, ast.Attribute) else \
                (callee.id if isinstance(callee, ast.Name) else "")
            if cname in DYNAMIC_LOADERS and _refs_product_path(node):
                imports_product = True
            if cname == "import_module" and _refs_product_path(node):
                imports_product = True
        # --- присваивания от динамической загрузки ---
        elif isinstance(node, ast.Assign):
            note_dynamic_assign(node.targets, node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            note_dynamic_assign([node.target], node.value)

    # --- фикстуры, грузящие продукт: имя фикстуры = продуктовый алиас для атрибутных вызовов ---
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_fixture(node):
            if _fixture_loads_product(node):
                product_aliases.add(node.name)
                imports_product = True

    return product_funcs, product_aliases, imports_product


def _fixture_loads_product(fn: ast.AST) -> bool:
    """Грузит ли фикстура продуктовый код (динамический загрузчик по пути кита или импорт продукта)."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            callee = node.func
            cname = callee.attr if isinstance(callee, ast.Attribute) else \
                (callee.id if isinstance(callee, ast.Name) else "")
            if cname in ("module_from_spec", *DYNAMIC_LOADERS) and \
                    (cname == "module_from_spec" or _refs_product_path(node)):
                return True
        if isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if top in PRODUCT_IMPORT_ROOTS or top in PRODUCT_FLAT_MODULES:
                return True
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                if top in PRODUCT_IMPORT_ROOTS or top in PRODUCT_FLAT_MODULES:
                    return True
    return False


def _calls_product(tree: ast.AST, product_funcs: set[str], product_aliases: set[str]) -> bool:
    """Есть ли вызов продуктового кода: прямой `f(...)`, атрибутный `alias.f(...)` или subprocess."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # прямой вызов импортированного продуктового имени
        if isinstance(func, ast.Name) and func.id in product_funcs:
            return True
        # вызов атрибута на продуктовом алиасе: alias.method(...)
        if isinstance(func, ast.Attribute):
            root = func.value
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id in product_aliases:
                return True
            # запуск кода кита процессом: subprocess.run([..., <путь кита>, ...])
            if func.attr in SUBPROCESS_RUNNERS and _refs_product_path(node):
                return True
    return False


def classify_file(path: Path) -> str:
    """Классифицировать один тест-файл. -> 'behavioral' | 'structural'.

    Синтаксически битый файл считается структурным (поведение из него не исполнить) — красным его
    сделает отдельная проверка синтаксиса, а не эта метрика.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, OSError):
        return "structural"
    product_funcs, product_aliases, imports_product = _collect_product_bindings(tree)
    if not imports_product:
        return "structural"
    if _calls_product(tree, product_funcs, product_aliases):
        return "behavioral"
    return "structural"


def iter_test_files(tests_dir: Path = TESTS_DIR) -> list[Path]:
    """Все `tests/**/test_*.py`, отсортированы для воспроизводимости."""
    return sorted(tests_dir.rglob("test_*.py"))


def measure(tests_dir: Path = TESTS_DIR) -> dict:
    """Классифицировать все тест-файлы. -> счётчики + доля + пофайловая раскладка."""
    behavioral, structural = [], []
    for f in iter_test_files(tests_dir):
        (behavioral if classify_file(f) == "behavioral" else structural).append(
            f.relative_to(tests_dir).as_posix())
    total = len(behavioral) + len(structural)
    share = round(100.0 * len(behavioral) / total, 2) if total else 0.0
    return {
        "behavioral_count": len(behavioral),
        "structural_count": len(structural),
        "total_count": total,
        "behavioral_share_pct": share,
        "behavioral": behavioral,
        "structural": structural,
    }


def _iter_test_defs(tree: ast.AST):
    """Все определения тест-функций (`def test_*` / `async def test_*`), включая методы тест-классов."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            yield node


def measure_functions(tests_dir: Path = TESTS_DIR) -> dict:
    """ВТОРИЧНЫЙ (advisory) счётчик по ТЕСТ-ФУНКЦИЯМ, а не по файлам.

    Файловая метрика (см. `measure`) считает единицей ФАЙЛ: структурные assert, дописанные в уже
    поведенческий файл, дают 0 к знаменателю — «разбавление» доли не видно. Здесь единица — каждая
    `def test_*`: функция поведенческая, если модуль ИМПОРТИРУЕТ продукт кита И именно тело этой
    функции ЗОВЁТ его (те же критерии Call, что в файловой метрике, но применённые к поддереву
    функции). Иначе — структурная. Синтаксически битый файл пропускается (его краснит проверка
    синтаксиса, не эта метрика). Метрика НЕ энфорсится — только печатается как advisory.
    """
    behavioral, structural = 0, 0
    for f in iter_test_files(tests_dir):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        except (SyntaxError, OSError):
            continue
        product_funcs, product_aliases, imports_product = _collect_product_bindings(tree)
        for fn in _iter_test_defs(tree):
            if imports_product and _calls_product(fn, product_funcs, product_aliases):
                behavioral += 1
            else:
                structural += 1
    total = behavioral + structural
    share = round(100.0 * behavioral / total, 2) if total else 0.0
    return {
        "behavioral_fn_count": behavioral,
        "structural_fn_count": structural,
        "total_fn_count": total,
        "behavioral_fn_share_pct": share,
    }


def render_function_advisory(fn_metrics: dict) -> str:
    """ADVISORY-строка по функциональному счётчику. НЕ влияет на вердикт/exit code."""
    return (
        f"TEST-TAXONOMY-ADVISORY: функциональная доля поведенческих "
        f"{fn_metrics['behavioral_fn_count']}/{fn_metrics['total_fn_count']} = "
        f"{fn_metrics['behavioral_fn_share_pct']}% (advisory, не энфорсится — "
        f"делает видимым «разбавление» структурными assert внутри поведенческого файла)"
    )


def load_baseline(path: Path = BASELINE_FILE) -> dict:
    """Загрузить baseline из YAML. -> dict (пустой, если файла нет)."""
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def check(current: dict, baseline: dict) -> list[str]:
    """Сверить текущую долю с baseline (перекрёстное умножение целых). -> список ошибок (пустой = ОК)."""
    b_beh = baseline.get("behavioral_count")
    b_tot = baseline.get("total_count")
    if not isinstance(b_beh, int) or isinstance(b_beh, bool) or \
            not isinstance(b_tot, int) or isinstance(b_tot, bool) or b_tot <= 0:
        return ["ратчет test-taxonomy: в baseline нет чисел behavioral_count/total_count — "
                "порога не существует"]
    c_beh = current["behavioral_count"]
    c_tot = current["total_count"]
    # доля упала: c_beh/c_tot < b_beh/b_tot  <=>  c_beh*b_tot < b_beh*c_tot
    if c_beh * b_tot < b_beh * c_tot:
        c_share = current["behavioral_share_pct"]
        b_share = round(100.0 * b_beh / b_tot, 2)
        return [
            f"ратчет test-taxonomy: доля поведенческих тестов {c_share}% "
            f"({c_beh}/{c_tot}) упала ниже baseline {b_share}% ({b_beh}/{b_tot}) — "
            f"поведенческий тест превращён в структурный или добавлены структурные сверх порога; "
            f"верните поведение либо осознанно снизьте baseline в "
            f"packages/test-taxonomy-baseline.yaml (лента raises)"
        ]
    return []


def render_report(current: dict) -> str:
    """Человекочитаемый отчёт: счётчики, доля и пофайловая классификация."""
    lines = [
        f"Тест-файлов всего: {current['total_count']}",
        f"  поведенческие: {current['behavioral_count']} ({current['behavioral_share_pct']}%)",
        f"  структурные:   {current['structural_count']}",
        "",
        "СТРУКТУРНЫЕ (только читают файлы/манифесты, продуктовый код не зовут):",
    ]
    for rel in current["structural"]:
        lines.append(f"  S  {rel}")
    lines.append("")
    lines.append("ПОВЕДЕНЧЕСКИЕ (импортируют и зовут продуктовый код кита):")
    for rel in current["behavioral"]:
        lines.append(f"  B  {rel}")
    return "\n".join(lines)


def write_baseline(current: dict, path: Path = BASELINE_FILE) -> None:
    """Записать baseline текущим замером (лента raises сохраняется, если была)."""
    prev = load_baseline(path)
    data = {
        "schema_version": 1,
        "kind": "test-taxonomy-ratchet",
        "behavioral_count": current["behavioral_count"],
        "structural_count": current["structural_count"],
        "total_count": current["total_count"],
        "behavioral_share_pct": current["behavioral_share_pct"],
        "raises": prev.get("raises", []) or [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or sys.argv[1:])
    current = measure()

    if "--report" in argv:
        print(render_report(current))
        print("")
        print(render_function_advisory(measure_functions()))
        return 0

    if "--baseline" in argv:
        write_baseline(current)
        print(f"Baseline обновлён: behavioral {current['behavioral_count']}/"
              f"{current['total_count']} = {current['behavioral_share_pct']}%")
        return 0

    baseline = load_baseline()
    errors = check(current, baseline)
    # ADVISORY: вторичный функциональный счётчик — печатается всегда, exit code НЕ трогает.
    print(render_function_advisory(measure_functions()))
    for e in errors:
        print(f"  [FAIL] {e}")
    if errors:
        print(f"TEST-TAXONOMY-FAIL: {len(errors)} нарушение(ий)")
        return 1
    b_share = round(100.0 * baseline["behavioral_count"] / baseline["total_count"], 2)
    print(f"TEST-TAXONOMY-OK: поведенческих {current['behavioral_count']}/"
          f"{current['total_count']} = {current['behavioral_share_pct']}% "
          f"(baseline {b_share}%, структурных {current['structural_count']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
