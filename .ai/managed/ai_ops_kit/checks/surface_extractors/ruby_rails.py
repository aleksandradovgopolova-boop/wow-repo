# -*- coding: utf-8 -*-
"""Серверные HTTP-маршруты Ruby on Rails (вид поверхности `route`): DSL файла config/routes.rb.

ВАЖНО: как и Go (`go_web`), Java Spring (`java_spring`) и JS/TS-бэкенд (`js_server`), Ruby — НЕ
Python, stdlib `ast` тут неприменим. Разбор `.rb` ДЕТЕРМИНИРОВАННЫЙ и БЕЗ ИСПОЛНЕНИЯ: паттерный скан
`parsed.source` (интерпретатор Ruby не зовётся, сети нет, needs_ast=False).

ЧЕСТНОСТЬ СИЛЫ. Это НЕ доказательный AST того же класса, что питон-экстракторы маршрутов: текстовый
разбор Ruby-DSL — эвристика, а не грамматика. Поэтому честный дефолт — **confidence: inferred** (как
go_web / spring-web / js_server: verified только доказуемое точным AST). Rails-route-поверхности видны
аналитику и попадают в каталог, но в W4 НЕ блокируют прогон. Развёртка `resources`/`resource` в
стандартные RESTful-маршруты — тоже inferred: это КОНВЕНЦИЯ Rails (роутер разворачивает их в рантайме),
а не доказанный в исходнике литерал каждого URL.

ГРАНИЦЫ РАЗБОРА (что берётся, что пропускается):
  * `get "/path"` / `post` / `put` / `patch` / `delete` с ЛИТЕРАЛЬНЫМ строковым путём → route
    (метод берётся из глагола DSL: `GET /path`);
  * `root "home#index"` / `root to: "home#index"` → `GET /`;
  * `resources :orders` → 7 стандартных RESTful-маршрутов Rails (index/create/new/show/edit/update/
    destroy): `GET /orders`, `POST /orders`, `GET /orders/new`, `GET /orders/:id`,
    `GET /orders/:id/edit`, `PATCH /orders/:id`, `DELETE /orders/:id`;
  * `resource :profile` (единственное число) → 6 RESTful без index и без `:id`: new/create/show/edit/
    update/destroy;
  * `namespace :admin do … end` даёт префикс пути `/admin`; `scope "/api" do … end` (и `scope path:
    "/api"`) — префикс `/api`; префикс склеивается с путями внутри блока.
Путь-НЕ-литерал (переменная, символ `get :dashboard`, интерполяция `"/u/#{id}"`, склейка) в кавычки
не попадает или содержит `#{` → пропуск.

Файл сужён к ИНДИКАТОРУ Rails: наличие `Rails.application.routes.draw` в тексте ЛИБО имя файла
`routes.rb`. Иначе одноимённый чужой `.get`/`resources` в стороннем Ruby-коде дал бы ложный route.

Битый/непарсибельный/не-utf-8 .rb скан не роняет: грамматику Ruby мы не разбираем, стек блоков не
уходит в минус (лишний `end` игнорируется), нечитаемый файл отсекается ядром обхода до вызова
экстрактора.
"""
from __future__ import annotations

import re

from ai_ops_kit.checks.surface_extractors._common import ParsedFile, Surface

# ─── индикатор Rails-роутинга ─────────────────────────────────────────────────────────────────────
# Файл считается таблицей маршрутов Rails только при явном индикаторе: вызов `Rails.application.
# routes.draw` в тексте ИЛИ конвенциональное имя файла config/routes.rb. Симметрия с import-сужением
# питон-/go-/java-экстракторов: без этого чужой `.get(...)`/`resources ...` в обычном .rb дал бы ложь.
_DRAW_INDICATOR = "routes.draw"

# HTTP-глаголы Rails-DSL: `get "/x"` и т.д. Глагол — В НАЧАЛЕ логической строки (после отступа), путь —
# строковый литерал сразу за ним. `\b` не даёт совпасть на `delete_all`/`gets`.
_VERB_RE = re.compile(r'^\s*(get|post|put|patch|delete)\s+(["\'])([^"\'\n]*)\2')

# `root "home#index"` / `root to: "..."` → корневой GET-маршрут `/`.
_ROOT_RE = re.compile(r'^\s*root\b')

# `resources :orders` / `resource :profile` [, only: …] — имя ресурса из символа. group(1)=='s' → мн.ч.
_RESOURCE_RE = re.compile(r'^\s*resource(s)?\s+:(\w+)')

# `namespace :admin do` — сегмент префикса пути = имя namespace.
_NAMESPACE_RE = re.compile(r'^\s*namespace\s+:?(["\']?)(\w+)\1')

# `scope "/api" do` (позиционный литерал) или `scope path: "/api" do`. Иные scope (module:/as:/symbol) —
# без сегмента пути.
_SCOPE_POS_RE = re.compile(r'^\s*scope\s+(["\'])([^"\'\n]*)\1')
_SCOPE_PATH_KW_RE = re.compile(r'^\s*scope\b[^\n]*\bpath:\s*(["\'])([^"\'\n]*)\1')

# Строка ОТКРЫВАЕТ блок, если заканчивается на `do` или `do |args|`.
_BLOCK_OPEN_RE = re.compile(r'\bdo\b(?:\s*\|[^|]*\|)?\s*$')


def _strip_comment(line: str) -> str:
    """Убрать строчный комментарий `#…`, НЕ трогая `#` внутри строк и интерполяцию `#{…}`.

    Идём посимвольно, отслеживая открытую строку (`'`/`"`): `#` вне строки обрывает строку кода до
    конца (комментарий), `#` внутри двойных кавычек (в т.ч. `#{`) сохраняется — чтобы интерполяция
    осталась видимой (её потом честно отсекут как не-литерал).
    """
    out: list = []
    i, n = 0, len(line)
    quote: str | None = None
    while i < n:
        c = line[i]
        if quote is not None:
            out.append(c)
            if c == "\\" and i + 1 < n:                # экранированный символ внутри строки
                out.append(line[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
        else:
            if c == "#":                               # комментарий вне строки → до конца строки
                break
            if c in ("'", '"'):
                quote = c
            out.append(c)
            i += 1
    return "".join(out)


def _is_literal_path(value: str) -> bool:
    """Строка — доказанный литеральный путь: непустая и без интерполяции `#{…}` (динамика)."""
    return bool(value) and "#{" not in value


def _full(prefix_segs: list, raw_path: str) -> str:
    """Склеить сегменты префикса (namespace/scope) и путь в один URL с единственным ведущим "/"."""
    parts = [s.strip("/") for s in prefix_segs if s and s.strip("/")]
    rp = raw_path.strip("/")
    if rp:
        parts.append(rp)
    return "/" + "/".join(parts) if parts else "/"


def _resource_routes(prefix_segs: list, name: str, plural: bool) -> list:
    """Стандартные RESTful-маршруты Rails для `resources`/`resource` → список (method, path).

    Множественное (`resources :orders`) — 7 действий (index/create/new/show/edit/update/destroy) с
    коллекционными и member-путями (`:id`). Единственное (`resource :profile`) — 6 действий без index
    и без `:id` (Rails не генерирует их для сингулярного ресурса). Развёртка — КОНВЕНЦИЯ Rails
    (inferred), а не литералы в исходнике: `only:`/`except:`-фильтры НЕ применяются (см. honest_limits).
    """
    base = _full(prefix_segs, name)
    if plural:
        return [
            ("GET", base),                 # index
            ("POST", base),                # create
            ("GET", base + "/new"),        # new
            ("GET", base + "/:id"),        # show
            ("GET", base + "/:id/edit"),   # edit
            ("PATCH", base + "/:id"),      # update
            ("DELETE", base + "/:id"),     # destroy
        ]
    return [
        ("GET", base + "/new"),            # new
        ("POST", base),                    # create
        ("GET", base),                     # show
        ("GET", base + "/edit"),           # edit
        ("PATCH", base),                   # update
        ("DELETE", base),                  # destroy
    ]


def _block_prefix_segment(line: str) -> tuple:
    """Классифицировать открывающую блок строку → (kind, prefix_segment).

    kind ∈ {"namespace","scope","resource","other"}; prefix_segment — вклад в префикс пути (пусто, если
    блок префикса не даёт). `resource`-блок (`resources :x do …`) сам префикса пути НЕ добавляет и
    помечается "resource", чтобы вложенное содержимое (member/collection/вложенные resources) честно
    пропускалось — см. honest_limits.
    """
    m = _NAMESPACE_RE.match(line)
    if m is not None:
        return ("namespace", m.group(2))
    if _RESOURCE_RE.match(line) is not None:
        return ("resource", "")
    m = _SCOPE_POS_RE.match(line)
    if m is not None:
        return ("scope", m.group(2))
    m = _SCOPE_PATH_KW_RE.match(line)
    if m is not None:
        return ("scope", m.group(2))
    if re.match(r"^\s*scope\b", line) is not None:
        return ("scope", "")               # scope module:/as:/symbol — без сегмента пути
    return ("other", "")


def extract_rails_routes(parsed: ParsedFile) -> list:
    """Серверные HTTP-маршруты Ruby on Rails из routes.rb по ТЕКСТУ DSL → route (inferred).

    Текстовый (не AST) разбор `parsed.source`. Берётся ТОЛЬКО в файле с индикатором Rails
    (`Rails.application.routes.draw` в тексте или имя файла routes.rb). Глаголы `get/post/put/patch/
    delete` с литеральным путём, `root` (→ `GET /`) и `resources`/`resource` (развёртка в RESTful).
    namespace/scope-блоки дают префикс пути. Путь-НЕ-литерал (переменная/символ/интерполяция) →
    пропуск. ref = "file:line|<METHOD> <path>", confidence: inferred (Ruby-текст ≠ доказательный AST).
    """
    src = parsed.source
    base_name = parsed.rel_path.rsplit("/", 1)[-1]
    if _DRAW_INDICATOR not in src and base_name != "routes.rb":
        return []

    out: list = []
    # Стек фреймов блоков: [(kind, prefix_segment)]. Префикс пути = сегменты namespace/scope в стеке;
    # флаг in_resource = находимся ли внутри `resources`/`resource`-блока (там member/collection и
    # вложенные ресурсы честно НЕ разбираем — declared limit).
    stack: list = []
    for idx, raw in enumerate(src.split("\n")):
        line = _strip_comment(raw)
        stripped = line.strip()
        lineno = idx + 1

        in_resource = any(kind == "resource" for kind, _ in stack)
        prefix_segs = [seg for kind, seg in stack if kind in ("namespace", "scope") and seg]

        # 1) Эмиссия маршрутов текущей строки (вне resource-блока: его нутро — member/collection/
        #    вложенные ресурсы — declared not supported).
        if not in_resource:
            mv = _VERB_RE.match(line)
            if mv is not None and _is_literal_path(mv.group(3)) \
                    and not line[mv.end():].lstrip().startswith("+"):
                path = _full(prefix_segs, mv.group(3))
                out.append(Surface(
                    kind="route", ref=f"{parsed.rel_path}:{lineno}|{mv.group(1).upper()} {path}",
                    confidence="inferred", extractor="rails-routes"))
            elif _ROOT_RE.match(line) is not None:
                out.append(Surface(
                    kind="route", ref=f"{parsed.rel_path}:{lineno}|GET {_full(prefix_segs, '')}",
                    confidence="inferred", extractor="rails-routes"))
            else:
                mr = _RESOURCE_RE.match(line)
                if mr is not None:
                    for method, path in _resource_routes(prefix_segs, mr.group(2), mr.group(1) == "s"):
                        out.append(Surface(
                            kind="route", ref=f"{parsed.rel_path}:{lineno}|{method} {path}",
                            confidence="inferred", extractor="rails-routes"))

        # 2) Обновление стека блоков для последующих строк.
        if _BLOCK_OPEN_RE.search(line) is not None:
            stack.append(_block_prefix_segment(line))
        elif stripped == "end" and stack:
            stack.pop()

    return out
