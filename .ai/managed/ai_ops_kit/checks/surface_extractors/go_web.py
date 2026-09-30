# -*- coding: utf-8 -*-
"""Серверные HTTP-маршруты Go-бэкенда (вид поверхности `route`): net/http / gin / chi / echo / gorilla.

ВАЖНО: как и JS/TS-бэкенд (`js_server`), Go — НЕ Python, stdlib `ast` тут неприменим. Разбор `.go`
ДЕТЕРМИНИРОВАННЫЙ и БЕЗ ИСПОЛНЕНИЯ: паттерный скан `parsed.source` (компилятор Go не зовётся, сети
нет, needs_ast=False).

ЧЕСТНОСТЬ СИЛЫ. Это НЕ доказательный AST того же класса, что питон-экстракторы маршрутов: регэксп по
тексту — эвристика, а не грамматика. Поэтому честный дефолт — **confidence: inferred** (как js_server:
FEAT-003/004 — verified только доказуемое точным AST). Go-route-поверхности видны аналитику и попадают
в каталог, но в W4 НЕ блокируют прогон.

ГРАНИЦЫ РАЗБОРА (что берётся, что пропускается):
  * net/http — `http.HandleFunc("/path", h)` / `mux.HandleFunc("/path", h)` / `http.Handle("/path", h)`;
  * gin      — `r.GET("/path", …)` / POST/PUT/DELETE/PATCH/HEAD/OPTIONS и `router.Group("/prefix")`;
  * chi      — `r.Get("/path", …)` (Get/Post/… в CamelCase) и `r.Route("/prefix", …)` / `r.Mount(...)`;
  * echo     — `e.GET("/path", …)` (те же заглавные глаголы, что и gin);
  * gorilla/mux — `r.HandleFunc("/path", …).Methods("GET")` (метод из цепочки НЕ читаем — берём путь).
Путь — строковый ЛИТЕРАЛ ("…" или `…`-raw), начинающийся с "/". За литералом должен идти `,` или `)`:
конкатенация (`"/v1" + version`), `fmt.Sprintf(...)`, переменная и шаблон в кавычки не попадают → пропуск.

Файл сужён к индикатору Go-веба (импорт net/http / gin-gonic/gin / go-chi/chi / labstack/echo /
gorilla/mux), чтобы одноимённый чужой `.Get(...)`/`.HandleFunc(...)` не дал ложный route.

Битый/непарсибельный/не-utf-8 .go регэксп не роняет: грамматику мы не разбираем, скан строки не падает
(нечитаемый файл отсекается ядром обхода до вызова экстрактора).
"""
from __future__ import annotations

import re

from ai_ops_kit.checks.surface_extractors._common import ParsedFile, Surface

# ─── индикатор Go-веба ──────────────────────────────────────────────────────────────────────────
# Файл считается Go-веб-бэкендом только при явном импорте одного из стеков — иначе одноимённый чужой
# `.Get(...)`/`.HandleFunc(...)` (напр. `cache.Get("/k")`) дал бы ложный маршрут (симметрия с
# import-сужением питон- и js-экстракторов). Импорты Go — строковые пути в кавычках.
_GO_WEB_INDICATOR_RE = re.compile(
    r'"(?:'
    r"net/http"
    r"|[^\"\n]*gin-gonic/gin[^\"\n]*"
    r"|[^\"\n]*go-chi/chi[^\"\n]*"
    r"|[^\"\n]*labstack/echo[^\"\n]*"
    r"|[^\"\n]*gorilla/mux[^\"\n]*"
    r')"')

# ─── глаголы маршрутов и монтирования ────────────────────────────────────────────────────────────
# gin/echo — заглавные HTTP-глаголы; chi — CamelCase (Get/Post/…); Handle/HandleFunc — net/http и
# gorilla; Group/Route/Mount — префиксные группы (их литеральный путь тоже даёт route, как `.use` в
# Express: склейку префикса с дочерними путями через фигурные скобки без AST не выводим — см. limits).
_VERB = (
    r"(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS"                 # gin / echo (UPPER)
    r"|Get|Post|Put|Delete|Patch|Head|Options|Connect|Trace"    # chi (CamelCase)
    r"|HandleFunc|Handle"                                        # net/http, gorilla/mux
    r"|Group|Route|Mount)")                                      # префиксные группы (gin/chi)

# `<ident>.<Verb>( "<path>"|`<path>`  [,)] ). Путь — литерал, начинается с "/". За литералом обязан
# идти `,` (есть handler) или `)` (единственный аргумент): если дальше `+` (конкатенация) — не матч.
_GO_ROUTE_RE = re.compile(
    r"\b[A-Za-z_]\w*\." + _VERB + r"\s*\(\s*"
    r"""(?:"(/[^"\n]*)"|`(/[^`\n]*)`)"""
    r"\s*[,)]")


def _lineno(source: str, pos: int) -> int:
    """Номер строки (1-базный) позиции pos в source — по числу переводов строки до неё."""
    return source.count("\n", 0, pos) + 1


def extract_go_web_routes(parsed: ParsedFile) -> list:
    """Серверные HTTP-маршруты Go: net/http / gin / chi / echo / gorilla по ТЕКСТУ → route (inferred).

    Текстовый (не AST) разбор `parsed.source`. Берётся ТОЛЬКО в файле с индикатором Go-веба (импорт
    net/http / gin / chi / echo / gorilla). Путь — строковый литерал ("…" или raw `…`), начинающийся
    с "/"; за ним `,` или `)` (конкатенация / fmt.Sprintf / переменная / шаблон в кавычки не попадают →
    пропуск). ref = "file:line|<path>". confidence: inferred (Go-текст ≠ доказательный AST).
    """
    src = parsed.source
    if not _GO_WEB_INDICATOR_RE.search(src):
        return []
    out: list = []
    for m in _GO_ROUTE_RE.finditer(src):
        path_value = m.group(1) if m.group(1) is not None else m.group(2)
        out.append(Surface(kind="route", ref=f"{parsed.rel_path}:{_lineno(src, m.start())}|{path_value}",
                           confidence="inferred", extractor="go-web"))
    return out
