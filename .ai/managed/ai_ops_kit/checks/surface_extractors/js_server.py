# -*- coding: utf-8 -*-
"""Серверные HTTP-маршруты JS/TS-бэкенда (вид поверхности `route`): Express / Nest / Next по ТЕКСТУ.

ВАЖНО: как и `js_ui` (экраны фронтенд-роутеров), бэкенд на JS/TS — НЕ Python, stdlib `ast` тут
неприменим. Разбор .js/.ts/.mjs/.cjs/.tsx — ДЕТЕРМИНИРОВАННЫЙ и БЕЗ ИСПОЛНЕНИЯ: паттерный скан
`parsed.source` (Express/Nest) или структурный вывод из РАСКЛАДКИ ФАЙЛОВ `parsed.rel_path` (Next).
Никакого запуска node и никакой сети (needs_ast=False).

ЧЕСТНОСТЬ СИЛЫ. Это НЕ доказательный AST того же класса, что питон-экстракторы маршрутов: регэксп по
тексту и вывод из имени файла — эвристика, а не грамматика. Поэтому честный дефолт для всех трёх
экстракторов — **confidence: inferred** (FEAT-003/004: verified только доказуемое точным AST).
Серверные route-поверхности видны аналитику и попадают в каталог, но в W4 НЕ блокируют прогон.

ГРАНИЦЫ РАЗБОРА (что берётся, что пропускается):
  * Express — `<obj>.<method>("/path", ...)` / `.use("/path", ...)`, где method ∈ HTTP-глаголы; путь —
    строковый ЛИТЕРАЛ, начинающийся с "/". Файл сужён к индикатору express (import/require 'express'),
    чтобы чужой `.get(...)` (напр. `res.get('Header')`) не дал ложный маршрут.
  * Nest — `@Get("/path")`/`@Post(...)`/… на методах + `@Controller("/prefix")` на классе; путь/префикс
    из строкового литерала, склеиваются в полный путь. Файл сужён к индикатору `@nestjs`.
  * Next — файловый роутинг: `pages/api/orders.ts` → `/api/orders` (Pages API) и `app/billing/route.ts`
    → `/billing` (App Router Route Handler). Маршрут выводится СТРУКТУРНО из пути файла.

Путь-НЕ-литерал (переменная / шаблон-строка backtick / выражение) в кавычки не попадает → пропуск.
Битый/непарсибельный файл регэксп не роняет: грамматику мы не разбираем, а скан строки не падает.
"""
from __future__ import annotations

import re

from ai_ops_kit.checks.surface_extractors._common import ParsedFile, Surface

# ─── общий примитив ───────────────────────────────────────────────────────────────────────────────


def _lineno(source: str, pos: int) -> int:
    """Номер строки (1-базный) позиции pos в source — по числу переводов строки до неё."""
    return source.count("\n", 0, pos) + 1


def _route(rel_path: str, lineno: int, path_value: str, extractor: str) -> Surface:
    """Собрать inferred-запись route серверного JS/TS-маршрута. Путь идёт в symbol ref."""
    return Surface(kind="route", ref=f"{rel_path}:{lineno}|{path_value}",
                   confidence="inferred", extractor=extractor)


# ─── Express ───────────────────────────────────────────────────────────────────────────────────────
# Файл считается express-бэкендом только при явном индикаторе импорта — иначе одноимённый чужой
# `.get(...)`/`.use(...)` дал бы ложный маршрут (симметрия с import-сужением питон-экстракторов).
_EXPRESS_INDICATOR_RE = re.compile(
    r"""(?:require\(\s*|from\s+)['"]express['"]|import\s+express\b""")
# `<ident>.<method>("/path", ...)`. method — HTTP-глагол Express или `use`/`all` (монтирование).
# Путь — строковый литерал в кавычках, начинающийся с "/". Backtick/переменная/выражение мимо.
_EXPRESS_ROUTE_RE = re.compile(
    r"""\b[A-Za-z_$][\w$]*\.(get|post|put|delete|patch|options|head|all|use)\s*\(\s*"""
    r"""(['"])(/[^'"]*)\2""")


def extract_express_routes(parsed: ParsedFile) -> list:
    """Серверные маршруты Express из исходника: `app.get("/x", ...)` / `router.post(...)` / `.use(...)`.

    Текстовый (не AST) разбор `parsed.source` → confidence: inferred. Берётся ТОЛЬКО в файле с
    индикатором express (import/require 'express'); путь — строковый литерал, начинающийся с "/"
    (переменная / шаблон-строка / выражение в кавычки не попадают → пропуск). ref = "file:line|<path>".
    """
    src = parsed.source
    if not _EXPRESS_INDICATOR_RE.search(src):
        return []
    out: list = []
    for m in _EXPRESS_ROUTE_RE.finditer(src):
        path_value = m.group(3)
        out.append(_route(parsed.rel_path, _lineno(src, m.start()), path_value, "express"))
    return out


# ─── Nest ────────────────────────────────────────────────────────────────────────────────────────
# Nest-контроллеры узнаём по импорту из `@nestjs/...`. `@Controller("prefix")` задаёт префикс класса,
# `@Get("/x")`/`@Post(...)`/… — путь метода; полный маршрут = префикс + путь метода.
_NEST_INDICATOR = "@nestjs"
_NEST_CONTROLLER_RE = re.compile(r"@Controller\s*\(\s*(?:(['\"])(.*?)\1)?")
_NEST_METHOD_RE = re.compile(
    r"@(Get|Post|Put|Delete|Patch|Options|Head|All)\s*\(\s*(?:(['\"])(.*?)\2)?")


def _join_nest_path(prefix: str, sub: str) -> str:
    """Склеить префикс контроллера и путь метода в один URL с единственным ведущим "/"."""
    segs = [s.strip("/") for s in (prefix, sub) if s and s.strip("/")]
    return "/" + "/".join(segs) if segs else "/"


def extract_nest_routes(parsed: ParsedFile) -> list:
    """Серверные маршруты NestJS: `@Get("/x")` + `@Controller("/prefix")` → route (inferred).

    Текстовый (не AST) разбор `parsed.source`. Берётся ТОЛЬКО в файле с индикатором `@nestjs`. Путь
    метода и префикс контроллера — строковые литералы; для каждого method-декоратора берётся ближайший
    ПРЕДШЕСТВУЮЩИЙ `@Controller` (несколько контроллеров в файле разводятся по позиции). Декоратор без
    литерала (`@Get()`, `@Controller()`) даёт пустой сегмент — маршрут сводится к префиксу/корню.
    ref = "file:line|<path>". Динамический аргумент (переменная/шаблон) в кавычки не попадает → пропуск.
    """
    src = parsed.source
    if _NEST_INDICATOR not in src:
        return []
    # (позиция → префикс) всех @Controller в файле, чтобы привязать метод к ближайшему до него.
    controllers = [(m.start(), (m.group(2) or "")) for m in _NEST_CONTROLLER_RE.finditer(src)]
    out: list = []
    for m in _NEST_METHOD_RE.finditer(src):
        sub = m.group(3) or ""
        prefix = ""
        for pos, pfx in controllers:
            if pos < m.start():
                prefix = pfx
            else:
                break
        path_value = _join_nest_path(prefix, sub)
        out.append(_route(parsed.rel_path, _lineno(src, m.start()), path_value, "nest"))
    return out


# ─── Next ────────────────────────────────────────────────────────────────────────────────────────
# Next выводит серверный маршрут из РАСКЛАДКИ ФАЙЛОВ, а не из содержимого: экстрактор смотрит
# `parsed.rel_path`. Два серверных источника: Pages API (`pages/api/**`) и App Router Route Handler
# (`app/**/route.ext`). UI-страницы (`pages/index.tsx`, `app/**/page.tsx`) — это НЕ серверный route,
# их здесь нет намеренно (иной вид поверхности). Разбор структурный → confidence: inferred.
_NEXT_ROUTE_SUFFIXES = frozenset({".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"})
_NEXT_ROUTE_HANDLER_STEMS = frozenset({"route"})


def _split_ext(name: str) -> tuple:
    """Разбить имя файла на (stem, suffix). Многоточечные имена трактуем по ПОСЛЕДНЕЙ точке."""
    dot = name.rfind(".")
    return (name, "") if dot <= 0 else (name[:dot], name[dot:])


def _next_anchor(parts: list, anchor: str) -> int:
    """Индекс каталога `anchor` (pages|app) в parts, если он в корне или сразу под `src/`. Иначе -1."""
    for i, part in enumerate(parts):
        if part == anchor and all(p == "src" for p in parts[:i]):
            return i
    return -1


def _clean_next_segments(segments: list) -> list | None:
    """Нормализовать сегменты маршрута Next в URL-сегменты.

    Опускает route-группы `(group)` и параллельные слоты `@slot` (в URL не влияют). Возвращает None,
    если встретился catch-all `[...slug]`/`[[...slug]]` — такой маршрут честно НЕ выводим (пропуск).
    Обычный динамический сегмент `[id]` СОХРАНЯЕТСЯ как есть (он выведен из имени файла детерминированно).
    """
    out: list = []
    for seg in segments:
        if not seg:
            continue
        if "[..." in seg or "[[..." in seg:
            return None                              # catch-all — не выводим
        if seg.startswith("(") and seg.endswith(")"):
            continue                                 # route-группа не меняет URL
        if seg.startswith("@"):
            continue                                 # параллельный слот не меняет URL
        out.append(seg)
    return out


def _next_pages_api_route(parts: list) -> str | None:
    """Маршрут Pages API из `pages/api/**`: `pages/api/orders.ts` → `/api/orders`. Иначе None."""
    i = _next_anchor(parts, "pages")
    if i < 0 or i + 1 >= len(parts) or parts[i + 1] != "api":
        return None
    stem, _suf = _split_ext(parts[-1])
    if stem.startswith("_"):
        return None                                  # служебные Next-файлы (_middleware и т.п.)
    tail = parts[i + 1:-1] + ([] if stem == "index" else [stem])
    segs = _clean_next_segments(tail)
    if segs is None:
        return None
    return "/" + "/".join(segs) if segs else "/"


def _next_app_route(parts: list) -> str | None:
    """Маршрут App Router Route Handler из `app/**/route.ext`: `app/billing/route.ts` → `/billing`."""
    i = _next_anchor(parts, "app")
    if i < 0:
        return None
    stem, _suf = _split_ext(parts[-1])
    if stem not in _NEXT_ROUTE_HANDLER_STEMS:
        return None                                  # серверный обработчик — только route.ext
    segs = _clean_next_segments(parts[i + 1:-1])
    if segs is None:
        return None
    return "/" + "/".join(segs) if segs else "/"


def extract_next_routes(parsed: ParsedFile) -> list:
    """Серверные маршруты Next из РАСКЛАДКИ ФАЙЛОВ: Pages API и App Router Route Handler (inferred).

    Структурный вывод из `parsed.rel_path` (содержимое не читается): `pages/api/orders.ts` →
    `/api/orders`, `app/billing/route.ts` → `/billing`. Route-группы `(group)` и слоты `@slot`
    опускаются; catch-all `[...slug]` честно пропускается (маршрут не выводим). ref = "file:line|<path>".
    Строка ref указывает на строку 1 (маршрут выведен из имени файла, а не из места в тексте).
    """
    parts = parsed.rel_path.split("/")
    stem, suffix = _split_ext(parts[-1])
    if suffix not in _NEXT_ROUTE_SUFFIXES:
        return []
    path_value = _next_pages_api_route(parts) or _next_app_route(parts)
    if path_value is None:
        return []
    return [_route(parsed.rel_path, 1, path_value, "next")]
