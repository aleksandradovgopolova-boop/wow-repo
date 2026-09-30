# -*- coding: utf-8 -*-
"""Серверные HTTP-маршруты Phoenix (Elixir, вид поверхности `route`): DSL модуля router.ex.

ВАЖНО: как и Ruby on Rails (`ruby_rails`), Go (`go_web`), Java Spring (`java_spring`) и JS/TS-бэкенд
(`js_server`), Elixir — НЕ Python, stdlib `ast` тут неприменим. Разбор `.ex` ДЕТЕРМИНИРОВАННЫЙ и БЕЗ
ИСПОЛНЕНИЯ: паттерный скан `parsed.source` (компилятор/BEAM Elixir не зовётся, сети нет,
needs_ast=False).

ЧЕСТНОСТЬ СИЛЫ. Это НЕ доказательный AST того же класса, что питон-экстракторы маршрутов: текстовый
разбор Phoenix-router-DSL — эвристика, а не грамматика Elixir. Поэтому честный дефолт —
**confidence: inferred** (как rails-routes / go_web / spring-web / js_server: verified только
доказуемое точным AST). Phoenix-route-поверхности видны аналитику и попадают в каталог, но в W4 НЕ
блокируют прогон. Развёртка `resources` в стандартные RESTful-маршруты — тоже inferred: это
КОНВЕНЦИЯ Phoenix (роутер разворачивает их в рантайме), а не доказанный в исходнике литерал каждого
URL.

ГРАНИЦЫ РАЗБОРА (что берётся, что пропускается):
  * `get "/path", Controller, :action` / `post` / `put` / `patch` / `delete` с ЛИТЕРАЛЬНЫМ строковым
    путём → route (метод берётся из глагола DSL: `GET /path`);
  * `resources "/orders", OrderController` → 7 стандартных RESTful-маршрутов Phoenix
    (index/new/create/show/edit/update/delete): `GET /orders`, `GET /orders/new`, `POST /orders`,
    `GET /orders/:id`, `GET /orders/:id/edit`, `PATCH /orders/:id`, `DELETE /orders/:id`;
  * `scope "/api" do …` / `scope "/api", AppWeb.Api do …` / `scope path: "/api" do …` дают префикс
    пути `/api`, который склеивается с путями внутри блока; `scope "/", AppWeb do` даёт пустой сегмент.
Путь-НЕ-литерал (переменная `get some_path, …`, атом-параметр, интерполяция `"/u/#{id}"`, склейка)
в двойные кавычки не попадает или содержит `#{` → пропуск.

Файл сужён к ИНДИКАТОРУ Phoenix: `Phoenix.Router` в тексте (`use Phoenix.Router`) ЛИБО макрос
`use <App>Web, :router` ЛИБО конвенциональное имя файла `router.ex`. Иначе одноимённый чужой
`get`/`resources` в стороннем Elixir-коде дал бы ложный route.

Битый/непарсибельный/не-utf-8 .ex скан не роняет: грамматику Elixir мы не разбираем, стек блоков не
уходит в минус (лишний `end` игнорируется), нечитаемый файл отсекается ядром обхода до вызова
экстрактора.
"""
from __future__ import annotations

import re

from ai_ops_kit.checks.surface_extractors._common import ParsedFile, Surface

# ─── индикатор Phoenix-роутинга ────────────────────────────────────────────────────────────────────
# Файл считается таблицей маршрутов Phoenix только при явном индикаторе: подстрока `Phoenix.Router`
# (`use Phoenix.Router`), макрос `use <App>Web, :router` ИЛИ конвенциональное имя файла router.ex.
# Симметрия с import-сужением rails-/go-/java-экстракторов: без этого чужой `get`/`resources` в
# обычном .ex дал бы ложь.
_PHOENIX_TEXT_INDICATOR = "Phoenix.Router"
# `use MyAppWeb, :router` — стандартный носитель роутера в приложении Phoenix (в тексте нет строки
# `Phoenix.Router`, но `, :router`-макрос однозначно вводит роутер).
_ROUTER_USE_RE = re.compile(r'\buse\s+\w[\w.]*\s*,\s*:router\b')

# HTTP-глаголы Phoenix-DSL: `get "/x", Controller, :action`. Глагол — В НАЧАЛЕ логической строки
# (после отступа), путь — двойные кавычки сразу за ним (Elixir: строки в "…"; '…' — charlist, не путь).
_VERB_RE = re.compile(r'^\s*(get|post|put|patch|delete)\s+"([^"\n]*)"')

# `resources "/orders", OrderController[, …]` — путь ресурса из литерала (первый аргумент).
_RESOURCES_RE = re.compile(r'^\s*resources\s+"([^"\n]*)"')

# `scope "/api" do` / `scope "/api", AppWeb do` — позиционный литеральный путь-префикс.
_SCOPE_POS_RE = re.compile(r'^\s*scope\s+"([^"\n]*)"')
# `scope path: "/api", alias: AppWeb do` — путь в именованном аргументе path:.
_SCOPE_PATH_KW_RE = re.compile(r'^\s*scope\b[^\n]*\bpath:\s*"([^"\n]*)"')
# Любой `scope` (в т.ч. `scope alias: AppWeb do` / `scope host: "…" do` без сегмента пути).
_SCOPE_RE = re.compile(r'^\s*scope\b')

# Строка ОТКРЫВАЕТ блок Elixir, если заканчивается на `do` (после снятия комментария). `do:`-инлайн
# (keyword-форма) блок НЕ открывает и сюда не попадает (за `do` идёт `:`).
_BLOCK_OPEN_RE = re.compile(r'\bdo\s*$')


def _strip_comment(line: str) -> str:
    """Убрать строчный комментарий `#…` в Elixir, НЕ трогая `#` внутри строк и интерполяцию `#{…}`.

    Идём посимвольно, отслеживая открытую двойную/одинарную кавычку: `#` вне строки обрывает строку
    кода (комментарий), `#` внутри кавычек (в т.ч. `#{`) сохраняется, чтобы интерполяция осталась
    видимой и была честно отсечена как не-литерал. Коротко (< 10 операторов) — не структурный дубль.
    """
    quote: str | None = None
    for i, ch in enumerate(line):
        if quote is None and ch == "#":
            return line[:i]
        if ch in "\"'":
            quote = None if ch == quote else (quote or ch)
    return line


def _is_literal(value: str) -> bool:
    """Строка — доказанный литеральный путь: непустая и без интерполяции `#{…}` (динамика)."""
    return bool(value) and "#{" not in value


def _join(prefix_segs: list, raw_path: str) -> str:
    """Склеить сегменты scope-префикса и путь в один URL с единственным ведущим "/"."""
    parts = [s.strip("/") for s in prefix_segs if s and s.strip("/")]
    tail = raw_path.strip("/")
    if tail:
        parts.append(tail)
    return "/" + "/".join(parts) if parts else "/"


def _resource_routes(base: str) -> list:
    """Стандартные RESTful-маршруты Phoenix для `resources` → список (method, path).

    Семь действий index/new/create/show/edit/update/delete с коллекционными и member-путями (`:id`).
    Развёртка — КОНВЕНЦИЯ Phoenix (inferred), а не литералы в исходнике: `only:`/`except:`-фильтры НЕ
    применяются, а обновление отдаётся как PATCH (Phoenix даёт и PUT — см. honest_limits).
    """
    return [
        ("GET", base),                 # index
        ("GET", base + "/new"),        # new
        ("POST", base),                # create
        ("GET", base + "/:id"),        # show
        ("GET", base + "/:id/edit"),   # edit
        ("PATCH", base + "/:id"),      # update
        ("DELETE", base + "/:id"),     # delete
    ]


def _is_phoenix_router(source: str, base_name: str) -> bool:
    """Файл — таблица маршрутов Phoenix: имя router.ex, `Phoenix.Router` в тексте или `, :router`-макрос."""
    if base_name == "router.ex":
        return True
    if _PHOENIX_TEXT_INDICATOR in source:
        return True
    return _ROUTER_USE_RE.search(source) is not None


def _classify_block(line: str) -> tuple:
    """Классифицировать открывающую блок строку → (kind, prefix_segment).

    kind ∈ {"scope","resources","other"}; prefix_segment — вклад scope в префикс пути (пусто, если
    scope без литерального пути). Блок `resources … do` помечается "resources", чтобы его нутро
    (member/collection/вложенные resources) честно пропускалось — см. honest_limits.
    """
    m = _SCOPE_PATH_KW_RE.match(line)
    if m is not None:
        return ("scope", m.group(1))
    m = _SCOPE_POS_RE.match(line)
    if m is not None:
        return ("scope", m.group(1))
    if _SCOPE_RE.match(line) is not None:
        return ("scope", "")               # scope alias:/host: — без сегмента пути
    if _RESOURCES_RE.match(line) is not None:
        return ("resources", "")
    return ("other", "")


def _emit_line(rel_path: str, lineno: int, line: str, prefix: list) -> list:
    """Маршруты, объявленные ОДНОЙ строкой (verb или resources) → список Surface (inferred)."""
    mv = _VERB_RE.match(line)
    if mv is not None and _is_literal(mv.group(2)):
        path = _join(prefix, mv.group(2))
        return [Surface(kind="route", ref=f"{rel_path}:{lineno}|{mv.group(1).upper()} {path}",
                        confidence="inferred", extractor="phoenix-router")]
    mr = _RESOURCES_RE.match(line)
    if mr is not None and _is_literal(mr.group(1)):
        base = _join(prefix, mr.group(1))
        return [Surface(kind="route", ref=f"{rel_path}:{lineno}|{method} {p}",
                        confidence="inferred", extractor="phoenix-router")
                for method, p in _resource_routes(base)]
    return []


def extract_phoenix_routes(parsed: ParsedFile) -> list:
    """Серверные HTTP-маршруты Phoenix из router.ex по ТЕКСТУ DSL → route (inferred).

    Текстовый (не AST) разбор `parsed.source`. Берётся ТОЛЬКО в файле с индикатором Phoenix
    (`Phoenix.Router` в тексте, макрос `use <App>Web, :router` или имя файла router.ex). Глаголы
    `get/post/put/patch/delete` с литеральным путём и `resources` (развёртка в RESTful). scope-блоки
    дают префикс пути. Путь-НЕ-литерал (переменная/атом/интерполяция) → пропуск. ref =
    "file:line|<METHOD> <path>", confidence: inferred (Elixir-текст ≠ доказательный AST).
    """
    src = parsed.source
    base_name = parsed.rel_path.rsplit("/", 1)[-1]
    if not _is_phoenix_router(src, base_name):
        return []

    out: list = []
    # Стек фреймов блоков: [(kind, prefix_segment)]. Префикс пути = сегменты scope в стеке; флаг
    # внутри resources-блока (там member/collection и вложенные ресурсы честно НЕ разбираем).
    stack: list = []
    for idx, raw in enumerate(src.split("\n")):
        line = _strip_comment(raw)
        prefix = [seg for kind, seg in stack if kind == "scope" and seg]
        inside_resources = any(kind == "resources" for kind, _ in stack)

        if not inside_resources:
            out.extend(_emit_line(parsed.rel_path, idx + 1, line, prefix))

        if _BLOCK_OPEN_RE.search(line) is not None:
            stack.append(_classify_block(line))
        elif line.strip() == "end" and stack:
            stack.pop()

    return out
