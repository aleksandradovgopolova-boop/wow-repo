# -*- coding: utf-8 -*-
"""Серверные HTTP-маршруты ASP.NET Core (C#, вид поверхности `route`): attribute routing + Minimal APIs.

ВАЖНО: как и Go (`go_web`), Java Spring (`java_spring`), Ruby on Rails (`ruby_rails`) и JS/TS-бэкенд
(`js_server`), C# — НЕ Python, stdlib `ast` тут неприменим. Разбор `.cs` ДЕТЕРМИНИРОВАННЫЙ и БЕЗ
ИСПОЛНЕНИЯ: паттерный скан `parsed.source` (компилятор Roslyn / .NET runtime не зовутся, сети нет,
needs_ast=False).

ЧЕСТНОСТЬ СИЛЫ. Это НЕ доказательный AST того же класса, что питон-экстракторы маршрутов: регэксп по
тексту — эвристика, а не грамматика. Поэтому честный дефолт — **confidence: inferred** (как go_web /
java_spring / rails-routes: verified только доказуемое точным AST). ASP.NET-route-поверхности видны
аналитику и попадают в каталог, но в W4 НЕ блокируют прогон.

ГРАНИЦЫ РАЗБОРА (что берётся, что пропускается):
  * ATTRIBUTE ROUTING (контроллеры): method-атрибуты `[HttpGet("/p")]`, `[HttpPost]`, `[HttpPut]`,
    `[HttpDelete]`, `[HttpPatch]` (а также `[HttpHead]`/`[HttpOptions]`) и `[Route("/p")]` на методе —
    литеральный путь метода → route; class-уровневый `[Route("api/[controller]")]` над классом задаёт
    ПРЕФИКС, который склеивается с путём метода (оба литералы). Сам префикс маршрутом НЕ считается.
    Токен `[controller]` РАЗРЕШАЕТСЯ детерминированно: он заменяется на имя класса-контроллера без
    суффикса `Controller` (регистр как в объявлении класса, `UsersController` → `Users`). Токен
    `[action]` (и прочие токены рантайма) НЕ разрешается — остаётся как есть (честный предел).
  * MINIMAL APIs: `app.MapGet("/p", …)` / `MapPost`/`MapPut`/`MapDelete`/`MapPatch` — литеральный путь
    первого аргумента → route. Приёмник (`app`/`endpoints`/…) не важен, важен индикатор ASP.NET файла.
Путь-НЕ-литерал (переменная, константа, C#-интерполяция `$"…"`/`$@"…"`, склейка через `+`) в кавычки
не попадает или начинается с `$` → пропуск. Атрибут без литерального пути (`[HttpGet]`, `[HttpGet(
Name="x")]`) сводится к префиксу класса / корню.

Файл сужён к индикатору ASP.NET (импорт/квалификатор `Microsoft.AspNetCore`, ЛИБО наличие
`[ApiController]`/`[HttpGet]`/…, ЛИБО вызов `MapGet(`/`MapPost(`/…), чтобы одноимённый чужой атрибут
или метод другого фреймворка не дал ложный route (симметрия с import-сужением питон-/go-/java-/ruby-
экстракторов).

Битый/непарсибельный/не-utf-8 .cs регэксп не роняет: грамматику C# мы не разбираем, скан строки не
падает; незакрытый атрибут (обрыв файла, `[HttpGet("/b"` без `)]`) не матчится → фантомного корневого
маршрута не порождает (нечитаемый файл отсекается ядром обхода до вызова экстрактора).
"""
from __future__ import annotations

import re

from ai_ops_kit.checks.surface_extractors._common import ParsedFile, Surface

# ─── индикатор ASP.NET Core ───────────────────────────────────────────────────────────────────────
# Файл считается ASP.NET-бэкендом только при явном индикаторе — иначе одноимённый чужой `[HttpGet]`
# или `.MapGet(` другого фреймворка/своего кода дал бы ложный маршрут. Индикатор: квалификатор
# Microsoft.AspNetCore (using/тип), аннотация контроллера/маршрута, либо вызов Minimal-API Map*.
_INDICATOR_RE = re.compile(
    r"Microsoft\.AspNetCore"
    r"|\[ApiController\b"
    r"|\[Http(?:Get|Post|Put|Delete|Patch|Head|Options)\b"
    r"|\bMap(?:Get|Post|Put|Delete|Patch)\s*\(")

# method-атрибуты маршрутов ASP.NET: HTTP-глаголы + `[Route]`. Скобки либо ЗАКРЫТЫ (`(…)`), либо их
# нет вовсе (атрибут без аргументов); аргументы НЕ пересекают перевод строки (`[^)\n]*`), чтобы обрыв
# файла (незакрытая `(`) не «дотянулся» до `)` следующего метода и не породил фантомный маршрут.
# group(1) — HTTP-глагол (или None для `[Route]`); group(2) — тело скобок (или None).
_METHOD_ATTR_RE = re.compile(
    r"\[(?:Http(Get|Post|Put|Delete|Patch|Head|Options)|Route)\b"
    r"(?:\s*\(([^)\n]*)\))?\s*\]")

# class-уровневый `[Route("literal")]`: атрибут с ЛИТЕРАЛЬНЫМ путём, за которым до `class` нет ни `{`,
# ни `}`, ни `;` (мы всё ещё в блоке атрибутов объявления типа, где допустимы иные атрибуты и
# модификаторы). group(1) — необязательный verbatim-`@`; group(2) — литерал пути (может нести
# `[controller]`); group(3) — имя класса (для разрешения токена `[controller]`).
_CLASS_LEVEL_ROUTE_RE = re.compile(
    r'\[Route\(\s*(@?)"([^"\n]*)"\s*\)\][^{};]*?\bclass\s+(\w+)')

# объявление класса (для привязки метода к ближайшему предшествующему классу). group(1) — имя, его
# start совпадает со start(3) в _CLASS_LEVEL_ROUTE_RE, что даёт общий ключ по позиции имени класса.
_CLASS_DECL_RE = re.compile(r"\bclass\s+(\w+)")

# Minimal API: `.MapGet("/p", …)` и семейство. Приёмник не важен (индикатор файла уже проверен).
# Путь — строковый литерал ("…" или verbatim @"…") ПЕРВЫМ аргументом; переменная/интерполяция `$"…"`
# после `(` под этот паттерн не подходят → пропуск.
_MINIMAL_MAP_RE = re.compile(
    r'\bMap(?:Get|Post|Put|Delete|Patch)\s*\(\s*(@?)"([^"\n]*)"')

# позиционный строковый литерал В НАЧАЛЕ аргументов атрибута (positional `"…"` / verbatim `@"…"`).
_POS_STR_RE = re.compile(r'^\s*@?"([^"\n]*)"')
# интерполяция `$"…"` / `$@"…"` — начинается с `$` → НЕ литерал.
_INTERP_HEAD_RE = re.compile(r'^\s*\$')
# именованное свойство атрибута первым (`Name = "…"`, `Order = 1`) — позиционного пути НЕТ → префикс.
_NAMED_ARG_HEAD_RE = re.compile(r'^\s*\w+\s*=')

# Результаты разбора аргументов метод-атрибута: "lit" — доказанный литеральный путь; "prefix" — пути
# нет, маршрут сводится к префиксу класса / корню; "skip" — путь есть, но НЕ литерал → пропуск.
_PREFIX = ("prefix", "")
_CONTROLLER_SUFFIX = "Controller"
_CONTROLLER_TOKEN_RE = re.compile(r"\[controller\]", re.IGNORECASE)


def _lineno(source: str, pos: int) -> int:
    """Номер строки (1-базный) позиции pos в source — по числу переводов строки до неё."""
    return source.count("\n", 0, pos) + 1


def _attr_path(args: str | None) -> tuple:
    """Разобрать аргументы method-атрибута → ("lit", path) | ("prefix", "") | ("skip", "").

    ("lit", path) — доказанный литеральный путь (позиционный `"…"` / verbatim `@"…"`).
    ("prefix", "") — литерального пути НЕТ (нет скобок `[HttpGet]`, пустые `()`, либо только именованное
      свойство `Name="x"`): маршрут = префикс класса / корень.
    ("skip", "") — путь ЕСТЬ, но НЕ литерал (переменная/константа/интерполяция `$"…"`): доказать нельзя.
    """
    if args is None:
        return _PREFIX                                   # [HttpGet] — нет скобок → префикс
    s = args.strip()
    if not s:
        return _PREFIX                                   # [HttpGet()] — пустые скобки → префикс
    m = _POS_STR_RE.match(args)
    if m is not None:
        return ("lit", m.group(1))                       # "…" / @"…"
    if _INTERP_HEAD_RE.match(args) is not None:
        return ("skip", "")                              # $"…" / $@"…" — интерполяция, не литерал
    if _NAMED_ARG_HEAD_RE.match(s) is not None:
        return _PREFIX                                   # Name="x"/Order=1 → позиц. пути нет → префикс
    return ("skip", "")                                  # позиционная переменная/константа → не литерал


def _join(prefix: str, sub: str) -> str:
    """Склеить префикс класса и путь метода в один URL с единственным ведущим "/"."""
    segs = [s.strip("/") for s in (prefix, sub) if s and s.strip("/")]
    return "/" + "/".join(segs) if segs else "/"


def _resolve_controller_token(path: str, class_name: str | None) -> str:
    """Заменить токен `[controller]` именем класса-контроллера без суффикса Controller (детерминированно).

    ASP.NET подставляет вместо `[controller]` имя класса без суффикса `Controller` (`UsersController`
    → `Users`); это выводимо из исходника, а не из рантайма. Регистр сохраняется как в объявлении.
    Токен `[action]` и прочие рантайм-токены НЕ разрешаются (честный предел) — остаются как есть.
    """
    if class_name is None or _CONTROLLER_TOKEN_RE.search(path) is None:
        return path
    token = (class_name[: -len(_CONTROLLER_SUFFIX)]
             if class_name.endswith(_CONTROLLER_SUFFIX) else class_name)
    return _CONTROLLER_TOKEN_RE.sub(token, path)


def _enclosing_class(class_starts: list, pos: int) -> tuple:
    """Ближайший ПРЕДШЕСТВУЮЩИЙ классу-декларации по позиции → (name_start, name) | (-1, None)."""
    found = (-1, None)
    for cpos, cname in class_starts:
        if cpos < pos:
            found = (cpos, cname)
        else:
            break
    return found


def extract_aspnet_routes(parsed: ParsedFile) -> list:
    """Серверные HTTP-маршруты ASP.NET Core по ТЕКСТУ .cs → route (inferred).

    Текстовый (не AST) разбор `parsed.source`. Берётся ТОЛЬКО в файле с индикатором ASP.NET
    (`Microsoft.AspNetCore`, `[ApiController]`/`[HttpGet]`/…, либо `MapGet(`/…). Attribute routing:
    method-атрибут `[HttpGet("/p")]`/…/`[Route("/p")]` склеивается с литеральным class-`[Route]`
    префиксом (токен `[controller]` разрешается именем класса без суффикса Controller); метод без
    литерального пути сводится к префиксу/корню. Minimal APIs: `MapGet("/p", …)` и семейство —
    литеральный путь → route. Путь-НЕ-литерал (переменная/константа/интерполяция) → пропуск.
    ref = "file:line|<path>", confidence: inferred (текст C# ≠ доказательный AST).
    """
    src = parsed.source
    if _INDICATOR_RE.search(src) is None:
        return []

    # Позиции имён всех классов (для привязки метода к ближайшему предшествующему) и литеральные
    # class-префиксы, привязанные к позиции имени класса.
    class_starts = sorted((m.start(1), m.group(1)) for m in _CLASS_DECL_RE.finditer(src))
    class_prefix: dict = {}
    class_route_positions: set = set()
    for m in _CLASS_LEVEL_ROUTE_RE.finditer(src):
        class_prefix[m.start(3)] = m.group(2)     # сырой литерал (токен [controller] пока не разрешён)
        class_route_positions.add(m.start())      # чтобы method-loop не принял class-Route за эндпоинт

    out: list = []

    # 1) Attribute routing: method-атрибуты (+ class-префикс). class-уровневый `[Route(...)]` пропускаем
    #    (его start в class_route_positions) — он основа для методов, а не эндпоинт.
    for m in _METHOD_ATTR_RE.finditer(src):
        if m.start() in class_route_positions:
            continue
        kind, sub = _attr_path(m.group(2))
        if kind == "skip":
            continue                              # путь есть, но НЕ литерал → не выдаём за маршрут
        cpos, cname = _enclosing_class(class_starts, m.start())
        prefix = class_prefix.get(cpos, "")
        path_value = _resolve_controller_token(_join(prefix, sub), cname)
        out.append(Surface(
            kind="route", ref=f"{parsed.rel_path}:{_lineno(src, m.start())}|{path_value}",
            confidence="inferred", extractor="aspnet-routes"))

    # 2) Minimal APIs: `MapGet("/p", …)` и семейство — литеральный путь первого аргумента как route.
    for m in _MINIMAL_MAP_RE.finditer(src):
        path_value = _join("", m.group(2))
        out.append(Surface(
            kind="route", ref=f"{parsed.rel_path}:{_lineno(src, m.start())}|{path_value}",
            confidence="inferred", extractor="aspnet-routes"))

    return out
