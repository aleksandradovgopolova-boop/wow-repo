"""Экраны UI фронтенд-роутеров (вид поверхности `screen`): React / Vue / Angular Router по ТЕКСТУ.

ВАЖНО: фронтенд — НЕ Python, stdlib `ast` тут неприменим. Разбор JS/JSX/TS/TSX/.vue —
ДЕТЕРМИНИРОВАННЫЙ и БЕЗ ИСПОЛНЕНИЯ: паттерный скан `parsed.source` (needs_ast=False, НИКАКОГО
запуска node/сети). Именно поэтому это НЕ доказательный AST-разбор того же класса, что
питон-экстракторы: регэксп по тексту — эвристика паттерна, а не грамматика. Отсюда честный
дефолт **confidence: inferred** — screen-поверхности видны аналитику и попадают в каталог, но НЕ
блокируют прогон (FEAT-003/004: verified только доказуемое). Литеральный путь в кавычках → screen;
путь из переменной / шаблон-строки (backtick) / выражения `{...}` — НЕ литерал, пропуск. Битый или
непарсибельный фронтенд-файл регэксп не роняет: грамматику мы не разбираем, а скан строки не падает.
"""
from __future__ import annotations

import re

from ai_ops_kit.checks.surface_extractors._common import ParsedFile, Surface

# JSX-объявление экрана: <Route ... path="/x" ...> / <Route path='/x'/>. `\b` после Route отсекает
# контейнер <Routes>. Путь — строковый литерал в одинарных/двойных кавычках; `{...}`/backtick мимо.
_JSX_ROUTE_PATH_RE = re.compile(r"<Route\b[^>]*?\bpath\s*=\s*(['\"])(.*?)\1")
# Объектная форма роута: { path: "/x", element/component: ... } — общий вид у React object routes
# (createBrowserRouter/useRoutes) и у Vue Router (routes: [...]). `\b` перед path не даёт зацепить
# basePath/filepath/redirect_path (ключ должен начинаться словом `path`).
_OBJ_PATH_RE = re.compile(r"\bpath\s*:\s*(['\"])(.*?)\1")

# Индикаторы ОБЪЕКТНОГО роутинга в файле — чтобы `path:` брался лишь в файле-роутере, а не в любом
# объекте с ключом path. React: фабрики/хук роутера. Vue: createRouter/VueRouter/импорт vue-router.
_REACT_OBJ_ROUTER_HINTS = ("createBrowserRouter", "createHashRouter", "createMemoryRouter", "useRoutes")
_VUE_ROUTER_HINTS = ("createRouter", "VueRouter", "vue-router")

# Индикатор Angular-роутинга в файле — чтобы объектный `path:` брался лишь в файле-роутере Angular, а
# не в любом TS-объекте с ключом path. Ловит импорт `@angular/router`, вызовы `RouterModule.forRoot/
# forChild` и аннотацию типа `const routes: Routes = [...]`. `\b`/`:` сужают, чтобы слово Routes в
# другом смысле не считалось индикатором.
_ANGULAR_ROUTER_INDICATOR_RE = re.compile(r"@angular/router|\bRouterModule\b|:\s*Routes\b")


def _lineno(source: str, pos: int) -> int:
    """Номер строки (1-базный) байтовой позиции pos в source — по числу переводов строки до неё."""
    return source.count("\n", 0, pos) + 1


def _screen(rel_path: str, lineno: int, path_value: str, extractor: str) -> Surface:
    """Собрать inferred-запись screen. Путь идёт в symbol ref ("file:line|/path")."""
    return Surface(kind="screen", ref=f"{rel_path}:{lineno}|{path_value}",
                   confidence="inferred", extractor=extractor)


def extract_react_router_screens(parsed: ParsedFile) -> list:
    """Экраны React Router из исходника: JSX `<Route path="/x"/>` и объектные роуты.

    Текстовый (не AST) разбор `parsed.source` → confidence: inferred. Две формы:
      * JSX-атрибут `<Route ... path="/x" ...>` — путь-литерал в кавычках;
      * объектные роуты `createBrowserRouter([{path:"/x", ...}])` / `useRoutes([{path:...}])` — ключ
        `path:` со строковым литералом берётся ТОЛЬКО в файле с индикатором объектного роутера
        (иначе любой объект с ключом path дал бы ложный экран).
    Путь-НЕ-литерал (переменная, шаблон-строка, `{...}`) в кавычки не попадает → пропуск.
    """
    src = parsed.source
    out: list = []
    for m in _JSX_ROUTE_PATH_RE.finditer(src):
        path_value = m.group(2)
        if path_value.strip():
            out.append(_screen(parsed.rel_path, _lineno(src, m.start()), path_value, "react-router"))
    if any(hint in src for hint in _REACT_OBJ_ROUTER_HINTS):
        for m in _OBJ_PATH_RE.finditer(src):
            path_value = m.group(2)
            if path_value.strip():
                out.append(_screen(parsed.rel_path, _lineno(src, m.start()), path_value, "react-router"))
    return out


def extract_vue_router_screens(parsed: ParsedFile) -> list:
    """Экраны Vue Router из исходника: `routes: [{ path: '/x', component: ... }]`.

    Текстовый (не AST) разбор `parsed.source` → confidence: inferred. Ключи `path:` со строковым
    литералом берутся ТОЛЬКО в файле с индикатором vue-router (createRouter/VueRouter/импорт
    vue-router), иначе любой объект с ключом path дал бы ложный экран. Путь-НЕ-литерал (переменная,
    шаблон-строка, `:id`-в-выражении) в кавычки не попадает → пропуск.
    """
    src = parsed.source
    if not any(hint in src for hint in _VUE_ROUTER_HINTS):
        return []
    out: list = []
    for m in _OBJ_PATH_RE.finditer(src):
        path_value = m.group(2)
        if path_value.strip():
            out.append(_screen(parsed.rel_path, _lineno(src, m.start()), path_value, "vue-router"))
    return out


def _angular_screen_path(path_value: str) -> str | None:
    """Нормализовать литеральный Angular-путь к единому виду ref ('/billing'); None — пропуск.

    Angular-путь обычно объявляют БЕЗ ведущего '/' (`path: 'billing'`) — приводим к '/billing' ради
    консистентности ref с react/vue. Пропускаем пустой путь ('' — дефолт/редирект-корень, не
    именованный экран) и wildcard '**' (catch-all, конкретный экран им не задаётся) — их честно не
    выводим как отдельный screen. Литеральный динамический сегмент (`users/:id`) СОХРАНЯЕТСЯ как есть.
    """
    stripped = path_value.strip()
    if not stripped or "*" in stripped:
        return None
    return "/" + stripped.lstrip("/")


def extract_angular_router_screens(parsed: ParsedFile) -> list:
    """Экраны Angular Router из TS: `RouterModule.forRoot([{path:'x'}])`/`const routes: Routes=[...]`.

    Текстовый (не AST) разбор `parsed.source` → confidence: inferred (как react/vue: регэксп по
    паттерну — эвристика, не доказательный AST). Ключ `path:` со строковым литералом берётся ТОЛЬКО в
    файле с индикатором Angular (`@angular/router` / `RouterModule` / аннотация `: Routes`), иначе
    любой TS-объект с ключом path дал бы ложный экран. Путь-НЕ-литерал (переменная, шаблон-строка,
    выражение) в кавычки не попадает → пропуск; пустой путь и wildcard '**' пропускаются
    (дефолт/catch-all конкретным экраном не считаем). ref = "file:line|/path".
    """
    src = parsed.source
    if not _ANGULAR_ROUTER_INDICATOR_RE.search(src):
        return []
    out: list = []
    for m in _OBJ_PATH_RE.finditer(src):
        path_value = _angular_screen_path(m.group(2))
        if path_value is not None:
            out.append(_screen(parsed.rel_path, _lineno(src, m.start()), path_value, "angular-router"))
    return out
