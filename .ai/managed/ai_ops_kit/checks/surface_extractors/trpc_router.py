# -*- coding: utf-8 -*-
"""Операции продукта из tRPC-роутера (TypeScript) — вид поверхности `api`: процедуры роутера.

ВАЖНО: tRPC-роутер живёт в TypeScript, а stdlib `ast` к TS неприменим. Разбор `.ts` —
ДЕТЕРМИНИРОВАННЫЙ и БЕЗ ИСПОЛНЕНИЯ: текстовый скан `parsed.source` (node не поднимается, типы не
проверяются, сети нет, needs_ast=False).

ЧЕСТНОСТЬ СИЛЫ. Как и остальные текстовые экстракторы не-Python стеков (GraphQL SDL, .proto,
js_server): TS-объект роутера разбирается РЕГЭКСПОМ/сканом текста, а НЕ полноценным TS-парсером/AST —
это не доказательство того же класса, что Python-AST. Поэтому честный дефолт — **confidence:
inferred** (verified только доказуемое точным AST). tRPC-операции видны аналитику и попадают в
каталог, но в W4 НЕ блокируют прогон.

ГРАНИЦЫ РАЗБОРА (что берётся, что пропускается):
  * роутер объявляется вызовом `createTRPCRouter({ … })` / `t.router({ … })` / `router({ … })`, где
    первый аргумент — объектный литерал, а КЛЮЧИ верхнего уровня суть процедуры;
  * процедура = ключ, ЗНАЧЕНИЕ которого содержит вызов `.query(` / `.mutation(` / `.subscription(`
    (unary/подписка). Имя операции = ключ. Обычное поле объекта (значение без такого вызова —
    метаданные/конфиг) процедурой НЕ считается;
  * ВЛОЖЕННЫЙ роутер как значение ключа (`orders: createTRPCRouter({ … })`) даёт namespace: его
    процедуры получают префикс `orders.` (форма клиентского вызова tRPC `trpc.orders.getOrders`).
    Сам ключ вложенного роутера операцией НЕ считается — операции только у процедур внутри;
  * kind поверхности = `api`; ref = "file:line|<namespace.operation>" (line — строка ключа процедуры).

СУЖЕНИЕ К tRPC. Экстрактор срабатывает ТОЛЬКО в файле с индикатором tRPC (`initTRPC`,
`createTRPCRouter`, `publicProcedure` или импорт из `@trpc/server`), чтобы обычный TS-объект с ключом
`query`/полем `router` в чужом файле (React-компонент, конфиг) не дал ложных операций. Только
НЕдинамические литеральные ключи (идентификатор или строковый литерал); вычисляемый ключ `[expr]:` —
пропуск.

ИЗОЛЯЦИЯ ШУМА. Комментарии (`//…`, `/* … */`) и строковые литералы (`"…"`, `'…'`, backtick-строки)
МАСКИРУЮТСЯ пробелами (с сохранением позиций и переводов строк — номера строк остаются точными),
чтобы текст `.query(`/`router(` внутри них не дал ложных процедур и чтобы фигурные скобки внутри
строк не сбивали балансировку блоков. Битый/не-utf-8 .ts не роняет скан (нечитаемый файл отсекается
ядром обхода до вызова экстрактора; незакрытый объект `{` без пары просто пропускается).

ЧЕГО НЕ ВИДИМ (задекларировано в surface-extractors.yaml): склейку `mergeRouters(a, b)` в рантайме
(операции склеиваемых роутеров тут не видны); процедуры, добавленные через spread (`...otherRouter`)
или собранные из переменной/импорта, а не литеральным ключом; middleware-цепочки как отдельные
операции (берётся сам факт процедуры, не её пайплайн); глубокую вложенность роутеров, если объект
собран не литералом; code-first без явных ключей; edge-случаи backtick-интерполяции.
"""
from __future__ import annotations

import re

from ai_ops_kit.checks.surface_extractors._common import ParsedFile, Surface, _matching_brace

# Индикатор tRPC в файле — иначе чужой TS-объект с ключом `query`/полем `router` дал бы ложные
# операции (симметрия с import-сужением прочих экстракторов).
_TRPC_INDICATOR_RE = re.compile(
    r"\binitTRPC\b|\bcreateTRPCRouter\b|\bpublicProcedure\b|@trpc/server")

# Заголовок вызова-роутера: `createTRPCRouter({`, `t.router({`, `router({`. `\b` не даёт зацепить
# `mergeRouters(` и `myrouter(` (границей слова перед `router`). За `(` сразу объектный литерал `{`.
_ROUTER_OPEN_RE = re.compile(
    r"(?:\bcreateTRPCRouter|(?:[A-Za-z_$][\w$]*\.)?\brouter)\s*\(\s*\{")

# Значение ключа — процедура, если содержит вызов builder'а .query(/.mutation(/.subscription(.
_PROCEDURE_RE = re.compile(r"\.(?:query|mutation|subscription)\s*\(")

# Идентификатор-ключ объекта (нестроковый, невычисляемый).
_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")


def _lineno(source: str, pos: int) -> int:
    """Номер строки (1-базный) позиции pos в source — по числу переводов строки до неё."""
    return source.count("\n", 0, pos) + 1


def _mask_comments_and_strings(src: str) -> str:
    """Заменить комментарии и строки TS пробелами, сохранив длину, позиции и переводы строк.

    Маскируются: строчные `//…`, блочные `/* … */`, строковые литералы `"…"`/`'…'` и backtick-строки
    `` `…` ``. Так текст `.query(`/`router(` внутри них не даёт ложных процедур, а `{`/`}` внутри строк
    не сбивают балансировку блоков. Номера строк остаются точными (замена посимвольно, `\n` сохраняются).
    backtick-строка маскируется целиком до следующего неэкранированного backtick (интерполяция `${…}`
    внутри тоже гасится — честный предел, задекларирован).
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if src.startswith("//", i):                       # строчный комментарий → до \n
            j = i
            while j < n and src[j] != "\n":
                out[j] = " "
                j += 1
            i = j
        elif src.startswith("/*", i):                     # блочный комментарий `/* … */`
            j = i + 2
            while j < n and not src.startswith("*/", j):
                j += 1
            end = min(j + 2, n)
            for k in range(i, end):
                if src[k] != "\n":
                    out[k] = " "
            i = end
        elif ch == '"' or ch == "'" or ch == "`":         # строковый/backtick литерал
            quote = ch
            out[i] = " "
            j = i + 1
            while j < n and src[j] != quote:
                if src[j] == "\\" and j + 1 < n:           # экранированный символ внутри строки
                    if src[j + 1] != "\n":
                        out[j + 1] = " "
                    out[j] = " "
                    j += 2
                    continue
                if src[j] != "\n":
                    out[j] = " "
                j += 1
            if j < n and src[j] == quote:
                out[j] = " "
                j += 1
            i = j
        else:
            i += 1
    return "".join(out)


def _skip_to_next_entry(masked: str, start: int, end: int) -> int:
    """Индекс top-level `,` (разделителя записей объекта) от start до end, либо end. Скобки/блоки
    внутри значения (`(`, `[`, `{`) увеличивают глубину — их внутренняя запятая не считается."""
    depth = 0
    i = start
    while i < end:
        c = masked[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return i
            depth -= 1
        elif c == "," and depth == 0:
            return i
        i += 1
    return end


def _next_token_pos(src: str, i: int, end: int) -> int:
    """Позиция следующего значимого токена от i до end: пропустить пробелы/`,`/`;` и комментарии.

    Читается из СЫРОГО src (не masked): строковый ключ в masked затёрт пробелами, и скан по masked
    проскочил бы его как пробел. Комментарии `//…`/`/* … */` пропускаются явно. Останавливается на
    первом реальном токене (идентификатор, кавычка строкового ключа, `[`, `:`, `}`).
    """
    while i < end:
        if src.startswith("//", i):
            while i < end and src[i] != "\n":
                i += 1
        elif src.startswith("/*", i):
            j = i + 2
            while j < end and not src.startswith("*/", j):
                j += 1
            i = min(j + 2, end)
        elif src[i] in " \t\r\n,;":
            i += 1
        else:
            return i
    return end


def _read_key(src: str, i: int, n: int):
    """Прочитать ключ записи объекта в позиции i из СЫРОГО src → (name, key_end) или (None, i).

    Ключ — идентификатор или строковый литерал (`"…"`/`'…'`). Вычисляемый `[expr]:` и любой иной
    старт → (None, i): запись пропускается (динамический/нелитеральный ключ). Ключ читается из
    исходника (не из masked), т.к. строковые ключи в masked затёрты пробелами.
    """
    c = src[i]
    if c == '"' or c == "'":
        j = i + 1
        while j < n and src[j] != c:
            if src[j] == "\\" and j + 1 < n:
                j += 2
                continue
            j += 1
        if j >= n:
            return None, i                                 # незакрытый строковый ключ
        return src[i + 1:j], j + 1
    if c == "[":
        return None, i                                     # вычисляемый ключ — пропуск
    m = _IDENT_RE.match(src, i)
    if m:
        return m.group(0), m.end()
    return None, i


def _iter_entries(masked: str, src: str, body_start: int, body_end: int):
    """Записи объектного литерала [body_start, body_end) верхнего уровня → (key, key_pos, val_start, val_end).

    Разделители/пробелы (масштаб masked) пропускаются; ключ читается из src; ожидается `:`; значение
    тянется до top-level `,` или конца тела. Записи с нелитеральным/вычисляемым ключом или без `:`
    (shorthand/spread) пропускаются.
    """
    entries: list = []
    i = body_start
    while i < body_end:
        i = _next_token_pos(src, i, body_end)
        if i >= body_end:
            break
        key_pos = i
        key, key_end = _read_key(src, i, body_end)
        if key is None:                                    # нелитеральный/вычисляемый ключ → пропуск
            i = min(_skip_to_next_entry(masked, i, body_end) + 1, body_end)
            continue
        j = _next_token_pos(src, key_end, body_end)
        if j >= body_end or masked[j] != ":":              # shorthand/spread — не key:value
            i = min(_skip_to_next_entry(masked, key_end, body_end) + 1, body_end)
            continue
        val_start = _next_token_pos(src, j + 1, body_end)
        val_end = _skip_to_next_entry(masked, val_start, body_end)
        entries.append((key, key_pos, val_start, val_end))
        i = min(val_end + 1, body_end)
    return entries


def _process_router(masked: str, src: str, open_brace_idx: int, prefix: str,
                    out: list, rel: str, consumed: list) -> None:
    """Разобрать тело роутера с `{` в open_brace_idx: процедуры → операции, вложенные роутеры → рекурсия."""
    close_idx = _matching_brace(masked, open_brace_idx)
    if close_idx < 0:
        return                                             # незакрытый объект (обрыв файла) — пропуск
    consumed.append((open_brace_idx, close_idx))
    for key, key_pos, val_start, val_end in _iter_entries(masked, src, open_brace_idx + 1, close_idx):
        value = masked[val_start:val_end]
        nested = _ROUTER_OPEN_RE.match(value)
        if nested:
            nested_brace = val_start + nested.end() - 1    # `{` вложенного роутера
            _process_router(masked, src, nested_brace, prefix + key + ".", out, rel, consumed)
        elif _PROCEDURE_RE.search(value):
            name = prefix + key
            out.append(Surface(kind="api",
                               ref=f"{rel}:{_lineno(src, key_pos)}|{name}",
                               confidence="inferred", extractor="trpc-router"))
        # иначе — обычное поле объекта (не процедура, не роутер) → пропуск


def extract_trpc_operations(parsed: ParsedFile) -> list:
    """Операции продукта из tRPC-роутера (.ts): процедуры `.query/.mutation/.subscription` → api (inferred).

    Текстовый (не TS-парсер) разбор `parsed.source`. Срабатывает ТОЛЬКО в файле с индикатором tRPC.
    Роутер = `createTRPCRouter({…})`/`t.router({…})`/`router({…})`; ключи верхнего уровня со значением,
    содержащим `.query(`/`.mutation(`/`.subscription(`, — процедуры (имя операции = ключ). Вложенный
    роутер как значение даёт namespace `<key>.` его процедурам. Комментарии/строки замаскированы (ложных
    процедур не дают). confidence: inferred (текстовый разбор TS ≠ доказательный AST).
    """
    src = parsed.source
    if not _TRPC_INDICATOR_RE.search(src):
        return []
    masked = _mask_comments_and_strings(src)
    out: list = []
    consumed: list = []
    for m in _ROUTER_OPEN_RE.finditer(masked):
        brace_idx = m.end() - 1
        if any(a <= brace_idx <= b for a, b in consumed):
            continue                                       # вложен в уже разобранный роутер — не дубль
        _process_router(masked, src, brace_idx, "", out, parsed.rel_path, consumed)
    return out
