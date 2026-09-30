"""Верифицированные экстракторы HTTP-маршрутов питон-веба (вид поверхности `route`, разбор AST).

Покрывает Flask/FastAPI (декоратор-маршруты), Django urlpatterns (path/re_path), DRF-роутеры
(router.register) и aiohttp (add_route/add_get/web.get). У всех граница `verified` одна: путь/префикс
обязан быть строковым ЛИТЕРАЛОМ в исходнике — переменная/f-строка/`include(...)`/динамика не доказаны
разбором и пропускаются (FEAT-003/004: verified только доказуемое). Экстракторы, кроме Flask/FastAPI
(его доказывает связка «декоратор-Call + метод-атрибут + путь-литерал»), СУЖЕНЫ к файлам,
импортирующим свой фреймворк (django.urls / rest_framework / aiohttp), иначе одноимённый чужой вызов
дал бы ложный verified. Разбор изолирован ядром обхода: битый файл молча пропускается.
"""
from __future__ import annotations

import ast

from ai_ops_kit.checks.surface_extractors._common import (
    ParsedFile,
    Surface,
    _imports_module,
    _is_str_literal,
)

# ── Декораторы HTTP-маршрутов известных питон-фреймворков (Flask 2.x, FastAPI, APIRouter). ────────
# Метод-атрибут декоратора: Flask `@app.route`, Flask 2.0+ / FastAPI `@app.get/@router.post/...`,
# `@app.api_route`, `@app.websocket`. Сам по себе этот набор поверхности не доказывает — доказывает
# связка «декоратор-Call + атрибут из набора + первый арг = строковый путь, начинающийся с '/'».
_HTTP_DECORATOR_ATTRS = frozenset({
    "route", "api_route", "websocket",
    "get", "post", "put", "delete", "patch", "head", "options", "trace",
})


def _is_path_literal(node: ast.expr) -> bool:
    """Первый аргумент декоратора — строковый ЛИТЕРАЛ пути, начинающийся с '/'.

    Это и есть граница verified: путь взят из исходника буквально, а не выведен из переменной или
    f-строки. Литерал-путь + метод-атрибут из набора однозначно опознаёт объявление маршрута и
    отсекает случайный `x.get(var)`/`obj.post(payload)`, где первый арг — не путь.
    """
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("/")


def extract_python_web_routes(parsed: ParsedFile) -> list:
    """Извлечь HTTP-маршруты, объявленные декоратором в исходнике (Flask/FastAPI-стиль).

    Точный разбор AST, без исполнения кода → confidence: verified. Опознаётся объявление вида
    `@<router>.<method>("/path", ...)` над (async-)функцией, где <method> ∈ _HTTP_DECORATOR_ATTRS,
    а первый позиционный аргумент — строковый путь-литерал. symbol в ref — имя функции-обработчика.
    """
    out: list = []
    for node in ast.walk(parsed.tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                continue
            if dec.func.attr not in _HTTP_DECORATOR_ATTRS:
                continue
            # приёмник декоратора — имя (app/router/bp/api) или атрибут (app.router): маршрутизатор,
            # а не произвольный объект. Реальная граница точности — литерал-путь ниже.
            if not isinstance(dec.func.value, (ast.Name, ast.Attribute)):
                continue
            if not (dec.args and _is_path_literal(dec.args[0])):
                continue
            ref = f"{parsed.rel_path}:{node.lineno}|{node.name}"
            out.append(Surface(kind="route", ref=ref,
                               confidence="verified", extractor="python-web-routes"))
            break   # один маршрут на обработчик (несколько методов на одном пути = одна поверхность)
    return out


# ─── Ещё веб-фреймворки (вид route, точный разбор AST): Django / DRF / aiohttp ────────────────────
# Проверка «строковый литерал» и «файл импортирует модуль» переиспользуют общие хелперы
# (`_is_str_literal`, `_imports_module`) — одна реализация на назначение, без дублей.


def _dotted_name(node: ast.expr | None) -> str | None:
    """Точечное имя для Name/Attribute-цепочки ("views.orders", "UserViewSet"), иначе None.

    Используется для symbol в ref: если вьюха/обработчик/ViewSet записан именем — берём его; иначе
    (лямбда, вызов, подписка) вызывающий откатывается на сам путь-литерал.
    """
    parts: list = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


def _route(rel_path: str, lineno: int, symbol: str, extractor: str) -> Surface:
    """Собрать verified-запись route. Пустой symbol → ref без "|symbol" (по паттерну схемы)."""
    ref = f"{rel_path}:{lineno}|{symbol}" if symbol else f"{rel_path}:{lineno}"
    return Surface(kind="route", ref=ref, confidence="verified", extractor=extractor)


def _call_func_name(func: ast.expr) -> str | None:
    """Имя вызываемого: id для Name, attr для Attribute (напр. `django.urls.path` → "path")."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


# ── Django urlpatterns ───────────────────────────────────────────────────────────────────────────
_DJANGO_URL_FUNCS = frozenset({"path", "re_path"})


def extract_django_urls(parsed: ParsedFile) -> list:
    """Django-маршруты: `path("orders/", view)` / `re_path(r"^...$", view)` со строковым путём-литералом.

    verified ТОЛЬКО когда первый аргумент — строковый путь-литерал (у Django он относительный, без
    ведущего "/"). Вьюха-`include(...)` (монтирование вложенного urlconf) и путь-переменная/f-строка
    → пропуск: конкретный эндпоинт разбором не доказан. symbol в ref — имя вьюхи, если она записана
    именем (Name/Attribute), иначе сам путь-литерал.
    """
    if not _imports_module(parsed.tree, "django.urls"):
        return []
    out: list = []
    for node in ast.walk(parsed.tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_func_name(node.func) not in _DJANGO_URL_FUNCS or not node.args:
            continue
        if not _is_str_literal(node.args[0]):
            continue   # путь-переменная/f-строка → не доказано
        route = node.args[0].value
        view = node.args[1] if len(node.args) > 1 else None
        if isinstance(view, ast.Call) and _call_func_name(view.func) == "include":
            continue   # include(...) — вложенный urlconf, не конкретный маршрут
        symbol = _dotted_name(view) or route
        out.append(_route(parsed.rel_path, node.lineno, symbol, "django-urls"))
    return out


# ── DRF-роутеры ──────────────────────────────────────────────────────────────────────────────────


def extract_drf_router(parsed: ParsedFile) -> list:
    """DRF-роутер: `router.register(r"prefix", ViewSet)` со строковым префиксом-литералом → verified.

    Сужен к файлам, импортирующим rest_framework, чтобы чужой `.register` (Flask blueprint, свой
    реестр) не дал ложный маршрут. Префикс-переменная → пропуск. symbol — имя ViewSet, если оно
    записано именем, иначе сам префикс.
    """
    if not _imports_module(parsed.tree, "rest_framework"):
        return []
    out: list = []
    for node in ast.walk(parsed.tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "register" or not node.args:
            continue
        if not _is_str_literal(node.args[0]):
            continue
        prefix = node.args[0].value
        viewset = node.args[1] if len(node.args) > 1 else None
        symbol = _dotted_name(viewset) or prefix
        out.append(_route(parsed.rel_path, node.lineno, symbol, "drf-router"))
    return out


# ── aiohttp ──────────────────────────────────────────────────────────────────────────────────────
# Методы-адаптеры маршрутизатора: `app.router.add_get("/p", h)` / `app.add_post("/p", h)` и т.п.
_AIOHTTP_METHOD_ADDERS = frozenset({
    "add_get", "add_post", "add_put", "add_delete", "add_patch",
    "add_head", "add_options", "add_view",
})
# Хелперы описания маршрута внутри `app.add_routes([...])`: `web.get("/p", h)` / `web.post(...)` и т.п.
_AIOHTTP_WEB_METHODS = frozenset({
    "get", "post", "put", "delete", "patch", "head", "options", "view",
})


def _aiohttp_web_route(call: ast.Call) -> tuple | None:
    """web.get("/p", h) / web.route("GET","/p",h) → (path, handler_node, lineno) при литерал-пути."""
    if not isinstance(call.func, ast.Attribute):
        return None
    attr = call.func.attr
    if attr in _AIOHTTP_WEB_METHODS and call.args and _is_path_literal(call.args[0]):
        handler = call.args[1] if len(call.args) > 1 else None
        return call.args[0].value, handler, call.lineno
    if attr == "route" and len(call.args) > 1 and _is_path_literal(call.args[1]):
        handler = call.args[2] if len(call.args) > 2 else None
        return call.args[1].value, handler, call.lineno
    return None


def extract_aiohttp_routes(parsed: ParsedFile) -> list:
    """aiohttp-маршруты со строковым путём-литералом (начинается с "/") → verified.

    Покрывает три формы: `app.router.add_route("GET", "/p", h)` (путь — 2-й арг),
    `app.router.add_get("/p", h)` и семейство add_<method> (путь — 1-й арг), и
    `app.add_routes([web.get("/p", h), web.route("GET","/p",h), ...])` (разбор списка-литерала).
    Сужен к файлам, импортирующим aiohttp. Путь-переменная / список не-литерал → пропуск. symbol —
    имя обработчика, если оно записано именем, иначе сам путь.
    """
    if not _imports_module(parsed.tree, "aiohttp"):
        return []
    out: list = []
    for node in ast.walk(parsed.tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        if attr == "add_route" and len(node.args) > 1 and _is_path_literal(node.args[1]):
            handler = node.args[2] if len(node.args) > 2 else None
            symbol = _dotted_name(handler) or node.args[1].value
            out.append(_route(parsed.rel_path, node.lineno, symbol, "aiohttp-routes"))
        elif attr in _AIOHTTP_METHOD_ADDERS and node.args and _is_path_literal(node.args[0]):
            handler = node.args[1] if len(node.args) > 1 else None
            symbol = _dotted_name(handler) or node.args[0].value
            out.append(_route(parsed.rel_path, node.lineno, symbol, "aiohttp-routes"))
        elif attr == "add_routes" and node.args and isinstance(node.args[0], ast.List):
            for elt in node.args[0].elts:
                if not isinstance(elt, ast.Call):
                    continue
                res = _aiohttp_web_route(elt)
                if res is not None:
                    path, handler, lineno = res
                    symbol = _dotted_name(handler) or path
                    out.append(_route(parsed.rel_path, lineno, symbol, "aiohttp-routes"))
    return out
