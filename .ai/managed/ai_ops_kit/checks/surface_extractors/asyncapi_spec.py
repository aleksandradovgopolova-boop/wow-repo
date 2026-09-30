# -*- coding: utf-8 -*-
"""Операции продукта из AsyncAPI-спеки — вид поверхности `api`: событийные операции каналов.

ВАЖНО: AsyncAPI-спека — это ЗАЯВЛЕННЫЙ контракт событийного API (YAML/JSON), а не сам код
продюсеров/консьюмеров. Разбор `.yaml`/`.yml`/`.json` ДЕТЕРМИНИРОВАННЫЙ и БЕЗ ИСПОЛНЕНИЯ:
`yaml.safe_load` для YAML и `json.loads` для JSON (`needs_ast=False`), без брокера, без сети, без
резолва remote-`$ref`. Это ровно тот же приём, что у openapi_spec (структурный разбор спеки), только
носитель контракта — AsyncAPI, а вид поверхности — `api` (событийные операции), а не `route`.

СУЖЕНИЕ ПО ИНДИКАТОРУ (критично). Файлы `.yaml`/`.yml`/`.json` ОЧЕНЬ распространены (package.json,
tsconfig, конфиги CI, docker-compose и т.п.) — и сам OpenAPI-спека тоже `.yaml`/`.json`. Чтобы не
выдавать поверхности из чужих файлов и не путать с OpenAPI, экстрактор срабатывает ТОЛЬКО когда в
документе есть ключ ВЕРХНЕГО уровня `asyncapi:` (AsyncAPI 2.x/3.x). Нет индикатора — сразу `[]`
(обычный yaml/json и OpenAPI-спека `openapi:`/`swagger:` не сканируются). Дешёвый текстовый
предфильтр отсекает большинство файлов ДО разбора; окончательное решение — по разобранному верхнему
уровню (подстрока `asyncapi` в комментарии/значении ложным срабатыванием не станет: проверяется
именно ключ top-level-словаря).

ГРАНИЦЫ РАЗБОРА (что берётся, что пропускается) — учтены ОБЕ версии AsyncAPI:
  * AsyncAPI 2.x: операции живут парами «канал + operation» в `channels:` —
    `channels: { orderCreated: { publish: {…}, subscribe: {…} } }`. Каждая пара «канал + operation»
    (publish/subscribe) = ОДНА операция → поверхность вида `api`; имя операции — "<канал> <publish|
    subscribe>", ref = "file:line|<канал> <publish|subscribe>";
  * AsyncAPI 3.x: операции вынесены в top-level `operations:` —
    `operations: { sendOrder: { action: send, channel: {…} } }`. Каждая операция = ОДНА поверхность
    вида `api`; имя операции — её ключ (`sendOrder`), ref = "file:line|<имя-операции>";
  * `components`/`messages`/`schemas` — это МОДЕЛИ данных/сообщения, а НЕ операции: они не берутся;
  * не-словарные значения каналов/операций пропускаются; битый/не-utf-8 файл не роняет скан — при
    ошибке разбора возвращается `[]`.

ЧЕСТНОСТЬ СИЛЫ = ЧЕСТНОСТЬ `confidence`. AsyncAPI — это ЗАЯВЛЕННЫЙ контракт, который может РАСХОДИТЬСЯ
с реальным кодом (drift: операция в спеке есть, а в коде нет — или наоборот), плюс разбор структурный
по тексту спеки, а не доказательство из исполняемого исходника. Поэтому честный дефолт —
**confidence: inferred** (как openapi_spec и остальные текстовые/структурные экстракторы: verified —
только доказуемое точным Python-AST). Операции видны аналитику и попадают в каталог, но в W4 НЕ
блокируют прогон.

ЧЕГО НЕ ВИДИМ (задекларировано в surface-extractors.yaml): расхождение спеки с кодом (drift — спека
может отставать); композицию через `$ref`/remote-refs (операция по ссылке в другом файле не
разворачивается); `components`/`messages` как модели данных (это не операции); `bindings`
(транспортные детали, не факт операции); спеки, встроенные строкой В КОД (YAML/JSON в .js/.ts/.py).
"""
from __future__ import annotations

import json

try:
    import yaml
except ImportError:  # pragma: no cover — PyYAML в поставке кита есть; дочка без него просто не даёт спек-поверхностей
    yaml = None  # type: ignore[assignment]

from ai_ops_kit.checks.surface_extractors._common import ParsedFile, Surface

# operation-ключи AsyncAPI 2.x внутри channel-item: канал + одна из них = операция.
_V2_OPERATIONS = ("publish", "subscribe")

# Расширения JSON — грузятся json.loads; всё прочее (.yaml/.yml) — yaml.safe_load.
_JSON_SUFFIXES = (".json",)


def _load(parsed: ParsedFile) -> object | None:
    """Разобрать спеку детерминированно и без сети: json.loads для .json, yaml.safe_load иначе.

    При любой ошибке разбора (битый файл, не тот формат) — None (пропуск, не падение). YAML —
    надмножество JSON, но по расширению .json используем строгий json.loads.
    """
    rel = parsed.rel_path.lower()
    try:
        if rel.endswith(_JSON_SUFFIXES):
            return json.loads(parsed.source)
        if yaml is None:
            return None
        return yaml.safe_load(parsed.source)
    except Exception:  # noqa: BLE001 — json/yaml любой сбой разбора = пропуск, не падение
        return None


def _key_line(lines: list, key: str, start: int) -> int:
    """Индекс (0-базный) первой строки с индекса `start`, чей ключ — `key` (yaml `key:` или json "key").

    Нужен, чтобы дать операции реальный file:line: сам разбор (safe_load/json.loads) номеров строк не
    несёт. Ищем ключ и в YAML-форме (`operations:` / `publish:`), и в JSON-форме (`"operations":`,
    одинарные кавычки — тоже). Совпадение только по началу очищенной строки. -1 — не найдено.
    """
    prefixes = (key + ":", '"' + key + '"', "'" + key + "'")
    for idx in range(start, len(lines)):
        if lines[idx].strip().startswith(prefixes):
            return idx
    return -1


def _top_level_is_asyncapi(data: object) -> bool:
    """Документ — AsyncAPI-спека: верхний уровень словарь с ключом `asyncapi`."""
    return isinstance(data, dict) and "asyncapi" in data


def _surface(parsed: ParsedFile, lineno: int, symbol: str) -> Surface:
    """Запись поверхности вида `api` для одной операции AsyncAPI (всегда inferred)."""
    return Surface(kind="api",
                   ref=f"{parsed.rel_path}:{lineno}|{symbol}",
                   confidence="inferred", extractor="asyncapi-spec")


def extract_asyncapi_operations(parsed: ParsedFile) -> list:
    """Операции продукта из AsyncAPI-спеки → api (inferred). Учтены версии 2.x и 3.x.

    Структурный (не исполняемый) разбор `parsed.source` файлов `.yaml`/`.yml`/`.json`. Срабатывает
    ТОЛЬКО при индикаторе top-level `asyncapi:` (иначе `[]` — чужой yaml/json и OpenAPI не трогаем).
    2.x: каждая пара «канал + publish/subscribe» из `channels` → api "<канал> <publish|subscribe>";
    3.x: каждая операция из top-level `operations` → api "<имя-операции>". confidence: inferred —
    спека это ЗАЯВЛЕННЫЙ контракт (может расходиться с кодом), плюс структурный разбор ≠ AST.
    """
    if "asyncapi" not in parsed.source:
        return []
    data = _load(parsed)
    if not _top_level_is_asyncapi(data):
        return []

    lines = parsed.source.splitlines()
    out: list = []

    # AsyncAPI 2.x — операции как пары канал+operation в `channels`.
    channels = data.get("channels")  # type: ignore[union-attr] — _top_level_is_asyncapi гарантирует dict
    if isinstance(channels, dict):
        ch_section = _key_line(lines, "channels", 0)
        ch_start = ch_section + 1 if ch_section >= 0 else 0
        for ch_name, item in channels.items():
            if not isinstance(ch_name, str) or not isinstance(item, dict):
                continue
            name_line = _key_line(lines, ch_name, ch_start)
            op_start = name_line + 1 if name_line >= 0 else ch_start
            for action in _V2_OPERATIONS:
                if not isinstance(item.get(action), dict):
                    continue  # у канала нет этой operation (или она не словарь) — пропуск
                a_line = _key_line(lines, action, op_start)
                if a_line >= 0:
                    lineno = a_line + 1
                elif name_line >= 0:
                    lineno = name_line + 1
                else:
                    lineno = 1
                out.append(_surface(parsed, lineno, f"{ch_name} {action}"))

    # AsyncAPI 3.x — операции вынесены в top-level `operations`.
    operations = data.get("operations")  # type: ignore[union-attr]
    if isinstance(operations, dict):
        ops_section = _key_line(lines, "operations", 0)
        ops_start = ops_section + 1 if ops_section >= 0 else 0
        for op_name, item in operations.items():
            if not isinstance(op_name, str) or not isinstance(item, dict):
                continue
            o_line = _key_line(lines, op_name, ops_start)
            lineno = o_line + 1 if o_line >= 0 else 1
            out.append(_surface(parsed, lineno, op_name))

    return out
