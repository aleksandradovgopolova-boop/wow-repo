# -*- coding: utf-8 -*-
"""Серверные HTTP-маршруты Ktor (Kotlin, вид поверхности `route`): DSL `routing { … }`.

ВАЖНО: как и Go (`go_web`), Java Spring (`java_spring`), Ruby on Rails (`ruby_rails`) и ASP.NET Core
(`dotnet_aspnet`), Kotlin — НЕ Python, stdlib `ast` тут неприменим. Разбор `.kt` ДЕТЕРМИНИРОВАННЫЙ и
БЕЗ ИСПОЛНЕНИЯ: паттерный скан `parsed.source` (компилятор Kotlin / JVM не зовутся, сети нет,
needs_ast=False).

ЧЕСТНОСТЬ СИЛЫ. Это НЕ доказательный AST того же класса, что питон-экстракторы маршрутов: регэксп по
тексту — эвристика, а не грамматика. Поэтому честный дефолт — **confidence: inferred** (как go_web /
java_spring / rails-routes / aspnet-routes: verified только доказуемое точным AST). Ktor-route-
поверхности видны аналитику и попадают в каталог, но в W4 НЕ блокируют прогон.

ГРАНИЦЫ РАЗБОРА (что берётся, что пропускается):
  * method-вызовы Ktor DSL `get("/path") { … }` / `post`/`put`/`patch`/`delete`/`head`/`options` со
    строковым путём-литералом → route (метод из глагола DSL). ref = "file:line|<МЕТОД> <path>".
  * `route("/prefix") { get { … } post("/sub") { … } }` — блок `route` задаёт ПРЕФИКС, который
    склеивается с путём вложенного метода. Метод БЕЗ пути (`get { … }`) сводится к самому префиксу
    (`route("/users") { get { … } }` → `GET /users`). Сам блок `route` маршрутом НЕ считается — его
    делает эндпоинтом только вложенный method-вызов.
  * вложенность блоков `route` учитывается БАЛАНСОМ СКОБОК (`_matching_brace` из `_common`):
    `route("/a") { route("/b") { get { … } } }` → `GET /a/b`.
Путь-НЕ-литерал (переменная `get(path)`, Kotlin-интерполяция `get("/u/$id")`/`${…}`, склейка через
`+`, тип-safe `get<Resource> { }`) в литерал не попадает или несёт `$` → пропуск. Блок `route` с
НЕлитеральным префиксом делает вложенные пути неразрешимыми → они пропускаются.

Файл сужён к индикатору Ktor (импорт `io.ktor`, наличие `routing {` ЛИБО `install(Routing)`), чтобы
одноимённый чужой `get(...)`/`post(...)` (напр. `map.get { … }` в не-Ktor Kotlin) не дал ложный
маршрут (симметрия с import-сужением питон-/go-/java-/ruby-/csharp-экстракторов). Member-вызовы
`x.get { }` отсекаются (глагол не должен идти после `.`), чтобы обращение к методу объекта не сошло
за маршрут DSL.

Битый/непарсибельный/не-utf-8 .kt регэксп не роняет: грамматику Kotlin мы не разбираем, скан строки
не падает; незакрытый блок `route("/b") {` без пары (обрыв файла) просто не даёт вложенных маршрутов
(нечитаемый файл отсекается ядром обхода до вызова экстрактора).

ЧЕГО НЕ ВИДИМ (задекларировано в surface-extractors.yaml): обёртки `authenticate { … }` и иные
интерсепторы (их семантику не осмысляем — вложенный метод виден как обычный маршрут своего префикса),
`route(path)` через ПЕРЕМЕННУЮ, тип-безопасный `resources`-плагин (`get<Article> { }`), глубокую
склейку вложенных `route` в полный путь в рантайме сверх объявленных литералов сегментов, регекс-роуты
и параметрические сегменты как семантику (литерал `{id}` остаётся в пути как есть).
"""
from __future__ import annotations

import re

from ai_ops_kit.checks.surface_extractors._common import ParsedFile, Surface, _matching_brace

# ─── индикатор Ktor ───────────────────────────────────────────────────────────────────────────────
# Файл считается Ktor-бэкендом только при явном индикаторе — иначе одноимённый чужой `get(...)`/
# `post(...)` другого кода дал бы ложный маршрут. Индикатор: импорт io.ktor, DSL-точка `routing {`
# ЛИБО установка фичи маршрутизации `install(Routing)`.
_INDICATOR_RE = re.compile(
    r"\bimport\s+io\.ktor\b"
    r"|\brouting\s*\{"
    r"|\binstall\s*\(\s*Routing\b")

# HTTP-глаголы Ktor DSL. Совпадают с именами функций-расширений Route: get/post/put/patch/delete/
# head/options. `(?<![.\w])` отсекает member-вызовы (`x.get {`) и хвосты слов (`forget`), `\b` — начало.
_METHOD_RE = re.compile(
    r"(?<![.\w])(get|post|put|patch|delete|head|options)\b\s*"
    r"(\([^(){}]*\))?\s*\{")

# Блок-префикс `route("/prefix") { … }`. Как и у методов, `(?<![.\w])` отсекает member-вызов. Тело
# скобок (`[^(){}]*`) — путь-литерал (или переменная/пусто), без вложенных `(`/`{` (строки к моменту
# скана уже замаскированы `_mask_kotlin`, так что их скобки/кавычки в счёт не идут).
_ROUTE_BLOCK_RE = re.compile(r"(?<![.\w])route\b\s*\(([^(){}]*)\)\s*\{")

# Ведущий строковый литерал в аргументах: сырой `"""…"""` ПЕРЕД обычным `"…"` (три кавычки жаднее).
_LEAD_RAW_RE = re.compile(r'\s*"""(.*?)"""', re.DOTALL)
_LEAD_STR_RE = re.compile(r'\s*"((?:[^"\\]|\\.)*)"')


def _lineno(source: str, pos: int) -> int:
    """Номер строки (1-базный) позиции pos в source — по числу переводов строки до неё."""
    return source.count("\n", 0, pos) + 1


def _mask_kotlin(src: str) -> str:
    """Заменить комментарии и строки Kotlin пробелами, сохранив длину, позиции и переводы строк.

    Маскируются: строчные `//…` до конца строки, ВЛОЖЕННЫЕ блочные `/* … /* … */ … */`, сырые строки
    `\"\"\"…\"\"\"` (без экранирования внутри) и обычные `"…"` (с `\\`-экранированием). Так текст и —
    главное — фигурные скобки `{`/`}` и кавычки внутри строк/комментариев не сбивают баланс
    `_matching_brace` и не дают ложных методов; номера строк остаются точными (замена посимвольно,
    `\n` сохраняются).
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        if src.startswith("//", i):                        # строчный комментарий → до \n
            j = i
            while j < n and src[j] != "\n":
                out[j] = " "
                j += 1
            i = j
        elif src.startswith("/*", i):                      # блочный комментарий (Kotlin — вложенный)
            depth = 1
            out[i] = out[i + 1] = " "
            j = i + 2
            while j < n and depth > 0:
                if src.startswith("/*", j):
                    depth += 1
                    out[j] = out[j + 1] = " "
                    j += 2
                elif src.startswith("*/", j):
                    depth -= 1
                    out[j] = out[j + 1] = " "
                    j += 2
                else:
                    if src[j] != "\n":
                        out[j] = " "
                    j += 1
            i = j
        elif src.startswith('"""', i):                     # сырая строка `"""…"""`
            out[i] = out[i + 1] = out[i + 2] = " "
            j = i + 3
            while j < n and not src.startswith('"""', j):
                if src[j] != "\n":
                    out[j] = " "
                j += 1
            if j < n:
                out[j] = out[j + 1] = out[j + 2] = " "
                j += 3
            i = j
        elif src[i] == '"':                                # обычная строка `"…"`
            out[i] = " "
            j = i + 1
            while j < n and src[j] != '"' and src[j] != "\n":
                if src[j] == "\\" and j + 1 < n:            # экранированный символ внутри строки
                    out[j] = " "
                    j += 1
                    if j < n and src[j] != "\n":
                        out[j] = " "
                    j += 1
                    continue
                out[j] = " "
                j += 1
            if j < n and src[j] == '"':
                out[j] = " "
                j += 1
            i = j
        else:
            i += 1
    return "".join(out)


def _literal_path(inner: str) -> tuple:
    """Разобрать аргументы вызова → ("lit", path) | ("empty", "") | ("skip", "").

    ("lit", path) — ведущий аргумент есть доказанный строковый литерал (обычный или сырой, тройной)
      без интерполяции; ("empty", "") — аргументов нет (`get()` / `route()` без пути) → сегмент пустой;
      ("skip", "") — путь ЕСТЬ, но НЕ литерал (переменная, интерполяция `$…`, склейка, тип-параметр).
    """
    if inner.strip() == "":
        return ("empty", "")
    m = _LEAD_RAW_RE.match(inner)
    if m is None:
        m = _LEAD_STR_RE.match(inner)
        if m is None:
            return ("skip", "")                            # переменная/выражение/тип-параметр
    value = m.group(1)
    if "$" in value:
        return ("skip", "")                                # Kotlin-интерполяция `$x` / `${…}`
    return ("lit", value)


def _full_path(prefixes: list, seg: str) -> str:
    """Склеить префиксы вложенных `route` и путь метода в один URL с единственным ведущим "/"."""
    segs: list = []
    for part in (*prefixes, seg):
        for s in part.split("/"):
            if s:
                segs.append(s)
    return "/" + "/".join(segs) if segs else "/"


def extract_ktor_routes(parsed: ParsedFile) -> list:
    """Серверные HTTP-маршруты Ktor по ТЕКСТУ .kt → route (inferred).

    Текстовый (не AST) разбор `parsed.source`. Берётся ТОЛЬКО в файле с индикатором Ktor (импорт
    `io.ktor`, `routing {` либо `install(Routing)`). method-вызовы `get("/p") { … }`/… дают маршрут;
    блоки `route("/prefix") { … }` задают префикс, склеиваемый с путём вложенного метода (вложенность
    — балансом скобок). Метод без пути сводится к префиксу. Путь-НЕ-литерал (переменная, интерполяция
    `$…`, тип-параметр) и вложение в блок с НЕлитеральным префиксом → пропуск. ref = "file:line|<МЕТОД>
    <path>", confidence: inferred (текст Kotlin ≠ доказательный AST).
    """
    src = parsed.source
    if _INDICATOR_RE.search(src) is None:
        return []
    struct = _mask_kotlin(src)

    # Блоки-префиксы: (open_brace_idx, close_brace_idx, prefix, opaque). opaque=True — префикс НЕ
    # литерал (переменная): вложенные в такой блок пути неразрешимы и пропускаются.
    blocks: list = []
    for m in _ROUTE_BLOCK_RE.finditer(struct):
        open_idx = m.end() - 1                             # позиция `{` блока route
        close_idx = _matching_brace(struct, open_idx)
        if close_idx < 0:
            continue                                       # незакрытый блок (обрыв файла) — пропуск
        kind, prefix = _literal_path(src[m.start(1):m.end(1)])
        blocks.append((open_idx, close_idx, prefix if kind == "lit" else "", kind == "skip"))

    out: list = []
    for m in _METHOD_RE.finditer(struct):
        verb = m.group(1).upper()
        if m.group(2) is None:
            seg_kind, seg = "empty", ""
        else:
            seg_kind, seg = _literal_path(src[m.start(2) + 1:m.end(2) - 1])
        if seg_kind == "skip":
            continue                                       # путь есть, но НЕ литерал → не маршрут
        pos = m.start()
        enclosing = sorted((b for b in blocks if b[0] < pos < b[1]), key=lambda b: b[0])
        if any(b[3] for b in enclosing):
            continue                                       # НЕлитеральный префикс в цепочке → пропуск
        full = _full_path([b[2] for b in enclosing], seg)
        out.append(Surface(
            kind="route", ref=f"{parsed.rel_path}:{_lineno(src, pos)}|{verb} {full}",
            confidence="inferred", extractor="ktor-routing"))
    return out
