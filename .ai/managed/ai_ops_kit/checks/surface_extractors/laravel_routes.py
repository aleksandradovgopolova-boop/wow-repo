# -*- coding: utf-8 -*-
"""Серверные HTTP-маршруты Laravel (PHP, вид поверхности `route`): фасад `Route::` файлов routes/*.php.

ВАЖНО: как и Go (`go_web`), Java Spring (`java_spring`), Ruby (`ruby_rails`) и JS/TS-бэкенд
(`js_server`), PHP — НЕ Python, stdlib `ast` тут неприменим. Разбор `.php` ДЕТЕРМИНИРОВАННЫЙ и БЕЗ
ИСПОЛНЕНИЯ: паттерный скан `parsed.source` (интерпретатор PHP не зовётся, сети нет, needs_ast=False).

ЧЕСТНОСТЬ СИЛЫ. Это НЕ доказательный AST того же класса, что питон-экстракторы маршрутов: текстовый
разбор фасада Laravel — эвристика, а не грамматика PHP. Поэтому честный дефолт — **confidence:
inferred** (как go_web / spring-web / rails-routes / js_server: verified только доказуемое точным AST).
Развёртка `Route::resource`/`Route::apiResource` в стандартные RESTful-маршруты — тоже inferred: это
КОНВЕНЦИЯ Laravel (роутер разворачивает их в рантайме), а не доказанный в исходнике литерал каждого URL.

ГРАНИЦЫ РАЗБОРА (что берётся, что пропускается):
  * `Route::get('/path', ...)` / `post` / `put` / `patch` / `delete` / `options` с ЛИТЕРАЛЬНЫМ
    строковым путём → route (метод берётся из глагола фасада: `GET /path`);
  * `Route::resource('orders', ...)` → 7 стандартных RESTful-маршрутов Laravel (index/create/store/
    show/edit/update/destroy): `GET /orders`, `GET /orders/create`, `POST /orders`, `GET /orders/{id}`,
    `GET /orders/{id}/edit`, `PUT /orders/{id}`, `DELETE /orders/{id}`;
  * `Route::apiResource('orders', ...)` → те же БЕЗ create/edit-форм (5 маршрутов: index/store/show/
    update/destroy) — API-ресурс не отдаёт HTML-формы;
  * `Route::prefix('api')->group(function () { … })` и `Route::group(['prefix' => 'admin'], …)` дают
    префикс пути, склеиваемый с путями внутри блока (при ЛИТЕРАЛЬНОМ префиксе).
Путь-НЕ-литерал (переменная `Route::get($path, …)`, двойная кавычка с интерполяцией `"/u/{$id}"`/`"$x"`,
склейка через `.`) в кавычки не попадает или содержит подстановку → пропуск.

Файл сужён к ИНДИКАТОРУ Laravel: фасад `Route::` в тексте, ЛИБО `use Illuminate\\Support\\Facades\\Route`,
ЛИБО путь файла лежит в каталоге `routes/`. Иначе одноимённый чужой `Route::`/`->get` в стороннем
PHP-коде дал бы ложный route.

Битый/непарсибельный/не-utf-8 .php скан не роняет: грамматику PHP мы не разбираем, стек блоков-групп не
уходит в минус (лишний `})` игнорируется), нечитаемый файл отсекается ядром обхода до вызова экстрактора.
"""
from __future__ import annotations

import re

from ai_ops_kit.checks.surface_extractors._common import ParsedFile, Surface

# ─── индикаторы Laravel-роутинга ──────────────────────────────────────────────────────────────────
# Файл считается таблицей маршрутов Laravel только при явном индикаторе: фасад `Route::` в тексте,
# импорт фасада `Illuminate\Support\Facades\Route`, либо расположение файла в каталоге routes/.
# Симметрия с import-сужением питон-/go-/java-экстракторов: без этого чужой `->get(...)` в обычном
# .php дал бы ложь.
_FACADE_INDICATOR = "Route::"
_USE_INDICATOR = "Illuminate\\Support\\Facades\\Route"

# HTTP-глаголы фасада Laravel: `Route::get('/x', …)` и т.д. Путь — строковый литерал сразу за `(`.
# `\b`-границы не нужны: `Route::` уже фиксирует начало вызова фасада.
_VERB_RE = re.compile(
    r'Route::(get|post|put|patch|delete|options)\s*\(\s*(["\'])(.*?)\2',
    re.IGNORECASE)

# `Route::resource('orders', …)` / `Route::apiResource('orders', …)` — имя ресурса из литерала.
# group(1) == 'api' (без учёта регистра) → API-ресурс (без create/edit-форм).
_RESOURCE_RE = re.compile(
    r'Route::(api)?resource\s*\(\s*(["\'])(.*?)\2',
    re.IGNORECASE)

# Открытие группы: `->group(function () {` (fluent) или `Route::group([...], function () {` (array).
# Требуем `{` на той же строке — это тело-замыкание; форму `->group(base_path('...'))` без `{` (группа
# файлом, не замыканием) не открываем, чтобы не осталось незакрытого фрейма.
_GROUP_OPEN_RE = re.compile(r'(?:->group\s*\(|Route::group\s*\()')

# Литеральный префикс группы: fluent `->prefix('api')` / `Route::prefix('api')` ИЛИ
# array `['prefix' => 'admin']`. Иные ключи/варианты (middleware/name/domain) префикса пути не дают.
_PREFIX_FLUENT_RE = re.compile(r'(?:->|Route::)prefix\s*\(\s*(["\'])(.*?)\1')
_PREFIX_ARRAY_RE = re.compile(r'["\']prefix["\']\s*=>\s*(["\'])(.*?)\1')


def _in_routes_dir(rel_path: str) -> bool:
    """Файл лежит в каталоге routes/ (сегмент пути) — конвенциональное место таблиц маршрутов Laravel."""
    return "routes" in rel_path.split("/")[:-1]


def _strip_php_comments(line: str, in_block: bool) -> tuple:
    """Убрать PHP-комментарии из строки → (очищенная_строка, in_block_после).

    Снимает строчные `//`/`#` и блочные `/* … */` (в т.ч. многострочные — состояние переносится
    флагом in_block), НЕ трогая эти символы внутри строковых литералов (`'…'`/`"…"`). Идём
    посимвольно, отслеживая открытую строку и открытый блок-комментарий; текст комментария заменяется
    пустотой (позиция для номера строки роли не играет — line-based скан). Экранированный символ
    внутри строки (`\\'`) не закрывает её.
    """
    out: list = []
    i, n = 0, len(line)
    quote: str | None = None
    while i < n:
        c = line[i]
        if in_block:                                   # внутри /* … */ — ждём */
            if c == "*" and i + 1 < n and line[i + 1] == "/":
                in_block = False
                i += 2
                continue
            i += 1
            continue
        if quote is not None:                          # внутри строкового литерала
            out.append(c)
            if c == "\\" and i + 1 < n:                # экранированный символ
                out.append(line[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        # вне строки и вне блока
        if c == "/" and i + 1 < n and line[i + 1] == "/":   # строчный // → до конца строки
            break
        if c == "#":                                        # строчный # → до конца строки
            break
        if c == "/" and i + 1 < n and line[i + 1] == "*":   # начало блочного /*
            in_block = True
            i += 2
            continue
        if c in ("'", '"'):
            quote = c
        out.append(c)
        i += 1
    return "".join(out), in_block


def _is_literal_path(quote: str, value: str) -> bool:
    """Строка — доказанный литеральный путь.

    Пусто (`''`) допускается как корень. Динамику отсекаем: PHP интерполирует ТОЛЬКО двойные кавычки —
    `"$var"` / `"{$id}"`; одинарные (`'...'`) не интерполируют никогда. Поэтому в двойных кавычках
    наличие `$` (или `{`) → не литерал. Символ `\\` (экранирование в скан не попало отдельно) не
    ожидается в путях — путь остаётся как есть.
    """
    if quote == '"' and ("$" in value or "{" in value):
        return False
    return True


def _is_concatenated(rest: str) -> bool:
    """За закрывающей кавычкой сразу идёт `.` — конкатенация PHP (`'/x' . $y`) → путь не литерал."""
    return rest.lstrip().startswith(".")


def _full(prefix_segs: list, raw_path: str) -> str:
    """Склеить сегменты префикса (группы) и путь в один URL с единственным ведущим "/"."""
    parts = [s.strip("/") for s in prefix_segs if s and s.strip("/")]
    rp = raw_path.strip("/")
    if rp:
        parts.append(rp)
    return "/" + "/".join(parts) if parts else "/"


def _resource_routes(prefix_segs: list, name: str, api: bool) -> list:
    """Стандартные RESTful-маршруты Laravel для `resource`/`apiResource` → список (method, path).

    Полный ресурс (`Route::resource`) — 7 действий: index/create/store/show/edit/update/destroy.
    API-ресурс (`Route::apiResource`) — те же БЕЗ форм create/edit (5 действий: index/store/show/
    update/destroy): API не отдаёт HTML-формы создания/редактирования. Развёртка — КОНВЕНЦИЯ Laravel
    (inferred), а не литералы в исходнике: `only:`/`except:`-сужения НЕ применяются (см. honest_limits),
    выдаётся полный набор соответствующего типа.
    """
    base = _full(prefix_segs, name)
    routes = [
        ("GET", base),                     # index
        ("GET", base + "/create"),         # create (форма — только web)
        ("POST", base),                    # store
        ("GET", base + "/{id}"),           # show
        ("GET", base + "/{id}/edit"),      # edit (форма — только web)
        ("PUT", base + "/{id}"),           # update
        ("DELETE", base + "/{id}"),        # destroy
    ]
    if api:
        forms = {("GET", base + "/create"), ("GET", base + "/{id}/edit")}
        routes = [r for r in routes if r not in forms]
    return routes


def _group_prefix_segment(line: str) -> str:
    """Литеральный сегмент префикса открывающей группу строки (пусто, если префикса нет/он нелитерал).

    Fluent `->prefix('admin')` / `Route::prefix('admin')` и array `['prefix' => 'admin']`. Префикс с
    интерполяцией в двойных кавычках или переменной сюда не попадёт (regex ловит только кавычки-литерал,
    а интерполяцию отсекаем той же проверкой, что и пути).
    """
    m = _PREFIX_FLUENT_RE.search(line)
    if m is not None and _is_literal_path(m.group(1), m.group(2)):
        return m.group(2)
    m = _PREFIX_ARRAY_RE.search(line)
    if m is not None and _is_literal_path(m.group(1), m.group(2)):
        return m.group(2)
    return ""


def extract_laravel_routes(parsed: ParsedFile) -> list:
    """Серверные HTTP-маршруты Laravel из routes/*.php по ТЕКСТУ фасада → route (inferred).

    Текстовый (не AST) разбор `parsed.source`. Берётся ТОЛЬКО в файле с индикатором Laravel (фасад
    `Route::`, импорт фасада, или файл в каталоге routes/). Глаголы `get/post/put/patch/delete/options`
    с литеральным путём, `Route::resource`/`apiResource` (развёртка в RESTful). `prefix`-группы дают
    префикс пути. Путь-НЕ-литерал (переменная/интерполяция/склейка) → пропуск. ref =
    "file:line|<METHOD> <path>", confidence: inferred (PHP-текст ≠ доказательный AST).
    """
    src = parsed.source
    if (_FACADE_INDICATOR not in src
            and _USE_INDICATOR not in src
            and not _in_routes_dir(parsed.rel_path)):
        return []

    out: list = []
    # Стек литеральных префиксов открытых prefix-групп. Полный префикс пути = непустые сегменты стека.
    stack: list = []
    in_block = False
    for idx, raw in enumerate(src.split("\n")):
        line, in_block = _strip_php_comments(raw, in_block)
        stripped = line.strip()
        lineno = idx + 1
        prefix_segs = [seg for seg in stack if seg]

        # 1) Глаголы фасада с литеральным путём.
        for mv in _VERB_RE.finditer(line):
            if not _is_literal_path(mv.group(2), mv.group(3)):
                continue
            if _is_concatenated(line[mv.end():]):
                continue
            path = _full(prefix_segs, mv.group(3))
            out.append(Surface(
                kind="route", ref=f"{parsed.rel_path}:{lineno}|{mv.group(1).upper()} {path}",
                confidence="inferred", extractor="laravel-routes"))

        # 2) resource / apiResource → RESTful-развёртка.
        for mr in _RESOURCE_RE.finditer(line):
            if not _is_literal_path(mr.group(2), mr.group(3)):
                continue
            api = mr.group(1) is not None
            for method, path in _resource_routes(prefix_segs, mr.group(3), api):
                out.append(Surface(
                    kind="route", ref=f"{parsed.rel_path}:{lineno}|{method} {path}",
                    confidence="inferred", extractor="laravel-routes"))

        # 3) Обновление стека prefix-групп для последующих строк.
        if _GROUP_OPEN_RE.search(line) is not None and "{" in line:
            stack.append(_group_prefix_segment(line))
        elif stripped.startswith("})") and stack:
            stack.pop()

    return out
