# -*- coding: utf-8 -*-
"""Операции продукта из GraphQL-схемы (SDL) — вид поверхности `api`: поля корневых типов.

ВАЖНО: SDL — не Python, stdlib `ast` тут неприменим. Разбор `.graphql`/`.gql` ДЕТЕРМИНИРОВАННЫЙ и
БЕЗ ИСПОЛНЕНИЯ: текстовый скан `parsed.source` (GraphQL-сервер не поднимается, интроспекция не
делается, сети нет, needs_ast=False).

ЧЕСТНОСТЬ СИЛЫ. SDL — ФОРМАЛЬНАЯ грамматика, и поля корневого типа из чистого SDL-блока извлекаются
довольно детерминированно. НО мы разбираем регэкспом/сканом текста, а НЕ полноценным
GraphQL-парсером — это не доказательство того же класса, что Python-AST. Поэтому честный дефолт —
**confidence: inferred** (как остальные текстовые экстракторы: verified только доказуемое точным
AST). GraphQL-операции видны аналитику и попадают в каталог, но в W4 НЕ блокируют прогон.

ГРАНИЦЫ РАЗБОРА (что берётся, что пропускается):
  * поля КОРНЕВЫХ типов = операции API продукта: `type Query { orders: [Order]  order(id: ID!): Order }`
    → операции `orders`, `order`; `type Mutation { createOrder(...): Order }` → `createOrder`;
    `type Subscription { ... }` — аналогично;
  * учитываются расширения корневых типов `extend type Query { ... }` (federation/модульные схемы —
    per-file, объединение схем across-services в рантайме мы не видим);
  * ТОЛЬКО поля корневых типов (Query/Mutation/Subscription и их extend). Обычный `type Order {...}`
    — это модель данных, НЕ операции, его поля НЕ берутся;
  * имя операции — имя поля; kind поверхности = `api`; ref = "file:line|<operation>".

ИЗОЛЯЦИЯ ШУМА. Комментарии SDL (`#` до конца строки) и описания-строки (блок-строка в тройных
кавычках, обычная `"..."`, дефолтные строковые значения аргументов) МАСКИРУЮТСЯ пробелами (с
сохранением позиций и переводов
строк — номера строк остаются точными), чтобы текст в них не дал ложных полей. Битый/не-utf-8 файл
не роняет скан (нечитаемый файл отсекается ядром обхода до вызова экстрактора; незакрытый блок `{`
без пары просто не даёт полей).

ЧЕГО НЕ ВИДИМ (задекларировано в surface-extractors.yaml): схемы через SDL-строки в коде (gql`...`,
graphql-tag в .js/.ts), интроспекцию, code-first (Nexus/TypeGraphQL/Strawberry — операции заданы
декораторами/классами, а не SDL), директивы схемы, переименование корневых типов через `schema {
query: MyQuery }` (берём только конвенциональные имена Query/Mutation/Subscription), объединённую
схему federation на уровне рантайма (видны только per-file extend'ы).
"""
from __future__ import annotations

import re

from ai_ops_kit.checks.surface_extractors._common import ParsedFile, Surface, _matching_brace

# ─── имена корневых типов ────────────────────────────────────────────────────────────────────────
# Конвенциональные корневые типы GraphQL. Переименование через `schema { query: X }` НЕ разрешаем
# (честный предел — см. honest_limits): берём только эти три имени и их `extend`.
_ROOT_TYPES = ("Query", "Mutation", "Subscription")

# Заголовок блока корневого типа: `type Query { … }` или `extend type Query { … }`. Между именем типа
# и `{` допускаются директивы/`implements` (`type Query @key(...) implements Node {`) — `[^{]*` их
# пропускает (строки к этому моменту уже замаскированы, `{` внутри них не встретится).
_ROOT_BLOCK_HEAD_RE = re.compile(
    r"\b(?:extend\s+)?type\s+(?:" + "|".join(_ROOT_TYPES) + r")\b[^{]*\{")

# Идентификатор GraphQL (имя поля/типа): буква/подчёркивание, затем буквы/цифры/подчёркивания.
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _lineno(source: str, pos: int) -> int:
    """Номер строки (1-базный) позиции pos в source — по числу переводов строки до неё."""
    return source.count("\n", 0, pos) + 1


def _mask_comments_and_strings(src: str) -> str:
    """Заменить комментарии и строки SDL пробелами, сохранив длину, позиции и переводы строк.

    Маскируются: блок-строки `\"\"\"…\"\"\"` (описания), обычные строки `"…"` (описания и дефолтные
    строковые значения аргументов) и строчные комментарии `#…` до конца строки. Так текст внутри них
    не может дать ложное поле, а номера строк остаются точными (замена посимвольно, `\n` сохраняются).
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch == "#":                                    # строчный комментарий → до \n
            j = i
            while j < n and src[j] != "\n":
                out[j] = " "
                j += 1
            i = j
        elif src.startswith('"""', i):                   # блок-строка (описание)
            j = i + 3
            while j < n and not src.startswith('"""', j):
                j += 1
            end = min(j + 3, n)
            for k in range(i, end):
                if src[k] != "\n":
                    out[k] = " "
            i = end
        elif ch == '"':                                  # обычная строка (описание / дефолт-значение)
            out[i] = " "
            j = i + 1
            while j < n and src[j] != '"' and src[j] != "\n":
                if src[j] == "\\" and j + 1 < n:         # экранированная кавычка внутри строки
                    out[j] = " "
                    j += 1
                out[j] = " "
                j += 1
            if j < n and src[j] == '"':
                out[j] = " "
                j += 1
            i = j
        else:
            i += 1
    return "".join(out)



def _field_names(body: str) -> list:
    """Имена полей корневого типа в теле блока → список (name, offset_в_body).

    Поле — идентификатор на ГЛУБИНЕ 0 (не внутри списка аргументов `(...)` и не внутри вложенного
    `{...}` дефолт-значения аргумента), за которым (после пробелов) идёт `(` (поле с аргументами) или
    `:` (поле без аргументов). Имена аргументов лежат на глубине parens>0 и не берутся; имя типа
    после `:` за `:` не следует, потому полем не становится (напр. `order(id: ID!): Order` → только
    `order`). Тело уже замаскировано от строк/комментариев.
    """
    out: list = []
    paren = 0
    brace = 0
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c == "(":
            paren += 1
            i += 1
        elif c == ")":
            paren = max(0, paren - 1)
            i += 1
        elif c == "{":
            brace += 1
            i += 1
        elif c == "}":
            brace = max(0, brace - 1)
            i += 1
        elif paren == 0 and brace == 0 and (c.isalpha() or c == "_"):
            m = _IDENT_RE.match(body, i)
            name = m.group(0)
            j = m.end()
            while j < n and body[j] in " \t\r\n":         # пропуск пробелов до `(`/`:`
                j += 1
            if j < n and body[j] in "(:":
                out.append((name, m.start()))
            i = m.end()
        else:
            i += 1
    return out


def extract_graphql_operations(parsed: ParsedFile) -> list:
    """Операции продукта из GraphQL SDL: поля корневых типов Query/Mutation/Subscription → api (inferred).

    Текстовый (не GraphQL-парсер) разбор `parsed.source` файлов `.graphql`/`.gql`. Берутся ТОЛЬКО поля
    корневых типов (`type Query|Mutation|Subscription { … }` и их `extend type …`); обычные `type` —
    модель данных, их поля НЕ операции. Имя операции — имя поля; ref = "file:line|<operation>".
    Комментарии/описания замаскированы (ложных полей не дают). confidence: inferred (текстовый разбор
    SDL ≠ доказательный AST).
    """
    src = parsed.source
    masked = _mask_comments_and_strings(src)
    out: list = []
    for head in _ROOT_BLOCK_HEAD_RE.finditer(masked):
        open_idx = head.end() - 1                          # позиция `{` заголовка блока
        close_idx = _matching_brace(masked, open_idx)
        if close_idx < 0:
            continue                                       # незакрытый блок (обрыв файла) — пропуск
        body_start = open_idx + 1
        body = masked[body_start:close_idx]
        for name, off in _field_names(body):
            pos = body_start + off
            out.append(Surface(kind="api",
                               ref=f"{parsed.rel_path}:{_lineno(src, pos)}|{name}",
                               confidence="inferred", extractor="graphql-schema"))
    return out
