# -*- coding: utf-8 -*-
"""Операции продукта из Protocol Buffers (.proto) — вид поверхности `api`: RPC-методы gRPC-сервисов.

ВАЖНО: .proto — не Python, stdlib `ast` тут неприменим. Разбор `.proto` ДЕТЕРМИНИРОВАННЫЙ и БЕЗ
ИСПОЛНЕНИЯ: текстовый скан `parsed.source` (protoc не вызывается, code-gen stubs не читаются,
reflection не делается, сети нет, needs_ast=False).

ЧЕСТНОСТЬ СИЛЫ. Как и GraphQL SDL (E8), .proto — ФОРМАЛЬНАЯ грамматика, и объявления `rpc` внутри
`service {…}` извлекаются довольно детерминированно. НО мы разбираем регэкспом/сканом текста, а НЕ
полноценным protobuf-парсером/AST — это не доказательство того же класса, что Python-AST. Поэтому
честный дефолт — **confidence: inferred** (verified только доказуемое точным AST). gRPC-операции
видны аналитику и попадают в каталог, но в W4 НЕ блокируют прогон.

ГРАНИЦЫ РАЗБОРА (что берётся, что пропускается):
  * КАЖДЫЙ `rpc <Name>(<Req>) returns (<Resp>);` внутри блока `service <Svc> { … }` — операция API
    продукта. Имя операции = `<Svc>/<Name>` (префикс сервиса разводит одноимённые rpc разных
    сервисов и совпадает с формой пути gRPC `/package.Service/Method`);
  * `stream` (client/server streaming: `rpc Watch(Req) returns (stream Ev);`) — это тот же rpc,
    факт операции не меняет;
  * обычные `message`/`enum` — это ТИПЫ ДАННЫХ, НЕ операции: их поля/значения НЕ берутся (сканируем
    только тела `service`-блоков);
  * kind поверхности = `api`; ref = "file:line|<Svc>/<Name>" (line — строка объявления rpc).

ИЗОЛЯЦИЯ ШУМА. Комментарии proto (`//` до конца строки и блочные `/* … */`) и строковые литералы
(`"…"`, `'…'` — значения опций/дефолтов) МАСКИРУЮТСЯ пробелами (с сохранением позиций и переводов
строк — номера строк остаются точными), чтобы текст в них не дал ложных rpc. Битый/не-utf-8 .proto
не роняет скан (нечитаемый файл отсекается ядром обхода до вызова экстрактора; незакрытый блок `{`
без пары просто не даёт операций).

ЧЕГО НЕ ВИДИМ (задекларировано в surface-extractors.yaml): сгенерированные из .proto stubs (code-gen
в .py/.go/.java и т.п. — иной носитель, не .proto), server reflection в рантайме, REST-mapping через
аннотации `google.api.http` (option (google.api.http) — это HTTP-биндинг, а не сам факт rpc), а также
переименование/пакеты как таковые (берём имя сервиса и rpc буквально из объявления).
"""
from __future__ import annotations

import re

from ai_ops_kit.checks.surface_extractors._common import ParsedFile, Surface, _matching_brace

# Заголовок блока сервиса: `service OrderService { … }`. Имя сервиса — идентификатор proto.
_SERVICE_HEAD_RE = re.compile(r"\bservice\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{")

# Объявление RPC внутри сервиса: `rpc <Name> (` — имя метода перед списком запроса. `stream` в
# скобках/после returns на детекцию имени не влияет (оно идёт ПОСЛЕ этой позиции).
_RPC_RE = re.compile(r"\brpc\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _lineno(source: str, pos: int) -> int:
    """Номер строки (1-базный) позиции pos в source — по числу переводов строки до неё."""
    return source.count("\n", 0, pos) + 1


def _mask_comments_and_strings(src: str) -> str:
    """Заменить комментарии и строки proto пробелами, сохранив длину, позиции и переводы строк.

    Маскируются: строчные комментарии `//…` до конца строки, блочные `/* … */` и строковые литералы
    (`"…"` / `'…'` — значения опций и дефолтов). Так текст внутри них не даёт ложного rpc, а номера
    строк остаются точными (замена посимвольно, `\n` сохраняются).
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
        elif ch == '"' or ch == "'":                      # строковый литерал (опция / дефолт)
            quote = ch
            out[i] = " "
            j = i + 1
            while j < n and src[j] != quote and src[j] != "\n":
                if src[j] == "\\" and j + 1 < n:          # экранированный символ внутри строки
                    out[j] = " "
                    j += 1
                out[j] = " "
                j += 1
            if j < n and src[j] == quote:
                out[j] = " "
                j += 1
            i = j
        else:
            i += 1
    return "".join(out)



def extract_grpc_rpcs(parsed: ParsedFile) -> list:
    """Операции продукта из .proto: RPC-методы gRPC-сервисов → api (inferred).

    Текстовый (не protobuf-парсер) разбор `parsed.source` файлов `.proto`. Берутся ТОЛЬКО объявления
    `rpc` внутри блоков `service <Svc> { … }`; обычные `message`/`enum` — модель данных, их поля НЕ
    операции. Имя операции — `<Svc>/<Name>`; ref = "file:line|<Svc>/<Name>". Комментарии/строки
    замаскированы (ложных rpc не дают). confidence: inferred (текстовый разбор .proto ≠ доказательный
    AST protobuf).
    """
    src = parsed.source
    masked = _mask_comments_and_strings(src)
    out: list = []
    for head in _SERVICE_HEAD_RE.finditer(masked):
        svc = head.group(1)
        open_idx = head.end() - 1                          # позиция `{` заголовка блока сервиса
        close_idx = _matching_brace(masked, open_idx)
        if close_idx < 0:
            continue                                       # незакрытый блок (обрыв файла) — пропуск
        body_start = open_idx + 1
        body = masked[body_start:close_idx]
        for m in _RPC_RE.finditer(body):
            pos = body_start + m.start()
            out.append(Surface(kind="api",
                               ref=f"{parsed.rel_path}:{_lineno(src, pos)}|{svc}/{m.group(1)}",
                               confidence="inferred", extractor="grpc-proto"))
    return out
