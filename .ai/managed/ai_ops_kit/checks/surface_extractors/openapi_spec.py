# -*- coding: utf-8 -*-
"""HTTP-эндпоинты продукта из OpenAPI/Swagger-спеки — вид поверхности `route`: пары путь+метод.

ВАЖНО: спека — это ЗАЯВЛЕННЫЙ контракт API (YAML/JSON), а не сам код обработчиков. Разбор
`.yaml`/`.yml`/`.json` ДЕТЕРМИНИРОВАННЫЙ и БЕЗ ИСПОЛНЕНИЯ: `yaml.safe_load` для YAML и `json.loads`
для JSON (`needs_ast=False`), без поднятия сервера, без сети, без резолва remote-`$ref`.

СУЖЕНИЕ ПО ИНДИКАТОРУ (критично). Файлы `.yaml`/`.json` ОЧЕНЬ распространены (package.json, tsconfig,
конфиги CI, docker-compose и т.п.). Чтобы не выдавать поверхности из чужих файлов, экстрактор
срабатывает ТОЛЬКО когда в документе есть ключ ВЕРХНЕГО уровня `openapi:` (OpenAPI 3.x) ИЛИ
`swagger:` (Swagger/OpenAPI 2.0). Нет индикатора — сразу `[]` (чужой yaml/json не сканируется).
Дешёвый текстовый предфильтр отсекает большинство файлов ДО разбора; окончательное решение — по
разобранному верхнему уровню (подстрока `openapi`/`swagger` в комментарии/значении ложным
срабатыванием не станет: проверяется именно ключ top-level-словаря).

ГРАНИЦЫ РАЗБОРА (что берётся, что пропускается):
  * эндпоинты живут в `paths:` — отображение "путь → операции": `paths: { /orders: { get: …, post: … },
    /orders/{id}: { get: … } }`. Каждая пара «путь + HTTP-метод» = ОДНА операция → поверхность вида
    `route`; берутся методы get/post/put/delete/patch;
  * путь берётся как ключ в `paths` (по контракту OpenAPI начинается с "/"); имя операции ref-символа —
    "<METHOD> <path>" (метод в верхнем регистре), ref = "file:line|<METHOD> <path>";
  * обычные секции спеки (`components`/`schemas`/`definitions`/`parameters`/…) — это МОДЕЛИ данных, а
    НЕ эндпоинты: берётся ТОЛЬКО `paths`;
  * `x-…`-расширения и не-словарные значения в `paths` пропускаются; битый/не-utf-8 файл не роняет
    скан — при ошибке разбора возвращается `[]`.

ЧЕСТНОСТЬ СИЛЫ = ЧЕСТНОСТЬ `confidence`. OpenAPI — это ЗАЯВЛЕННЫЙ контракт, который может РАСХОДИТЬСЯ
с реальным кодом (drift: эндпоинт в спеке есть, а в коде нет — или наоборот), плюс разбор структурный
по тексту спеки, а не доказательство из исполняемого исходника. Поэтому честный дефолт —
**confidence: inferred** (как остальные текстовые/структурные экстракторы: verified — только
доказуемое точным Python-AST). Эндпоинты видны аналитику и попадают в каталог, но в W4 НЕ блокируют
прогон.

ЧЕГО НЕ ВИДИМ (задекларировано в surface-extractors.yaml): расхождение спеки с кодом (drift — спека
может отставать); композицию через `$ref`/remote-refs (ссылка на path-item в другом файле не
разворачивается); `callbacks` и `webhooks` (OpenAPI 3.1) — они не в `paths`; методы head/options/trace;
серверные префиксы из `servers:`/`basePath:` (к путям не приклеиваются — виден только литерал ключа
`paths`); переопределение путей рантайм-роутером поверх спеки.
"""
from __future__ import annotations

import json

try:
    import yaml
except ImportError:  # pragma: no cover — PyYAML в поставке кита есть; дочка без него просто не даёт спек-поверхностей
    yaml = None  # type: ignore[assignment]

from ai_ops_kit.checks.surface_extractors._common import ParsedFile, Surface

# HTTP-методы операций OpenAPI, которые берём как эндпоинты (по одному route на пару путь+метод).
# head/options/trace осознанно опущены (см. honest_limits) — редко несут отдельную продуктовую
# операцию, чаще служебные.
_HTTP_METHODS = ("get", "post", "put", "delete", "patch")

# Расширения JSON — грузятся json.loads; всё прочее (.yaml/.yml) — yaml.safe_load.
_JSON_SUFFIXES = (".json",)


def _has_indicator_text(source: str) -> bool:
    """Дешёвый предфильтр ДО разбора: есть ли в тексте вообще намёк на OpenAPI/Swagger.

    Только отсекает заведомо чужие файлы (package.json, tsconfig, обычные конфиги), чтобы не парсить
    каждый yaml/json дерева. Окончательное решение — по ключу top-level-словаря после разбора
    (`_top_level_is_spec`): подстрока в комментарии/значении сюда просочиться может, но поверхностью
    не станет.
    """
    return "openapi" in source or "swagger" in source


def _load(parsed: ParsedFile) -> object | None:
    """Разобрать спеку детерминированно и без сети: json.loads для .json, yaml.safe_load иначе.

    При любой ошибке разбора (битый файл, не тот формат) — None (пропуск, не падение). YAML —
    надмножество JSON, но по расширению .json используем строгий json.loads, как договорено.
    """
    src = parsed.source
    rel = parsed.rel_path.lower()
    try:
        if rel.endswith(_JSON_SUFFIXES):
            return json.loads(src)
        if yaml is None:
            return None
        return yaml.safe_load(src)
    except (ValueError, RecursionError):
        return None
    except Exception:  # noqa: BLE001 — yaml.YAMLError и наследники: любой сбой разбора = пропуск, не падение
        return None


def _top_level_is_spec(data: object) -> bool:
    """Документ — OpenAPI/Swagger-спека: верхний уровень словарь с ключом `openapi` ИЛИ `swagger`."""
    return isinstance(data, dict) and ("openapi" in data or "swagger" in data)


def _key_line(lines: list, key: str, start: int) -> int:
    """Индекс (0-базный) первой строки с индекса `start`, чей ключ — `key` (yaml `key:` или json "key").

    Нужен, чтобы дать эндпоинту реальный file:line: сам разбор (safe_load/json.loads) номеров строк не
    несёт. Ищем ключ и в YAML-форме (`paths:` / `get:`), и в JSON-форме (`"paths":` / `"get":`,
    одинарные кавычки — тоже). Совпадение только по началу очищенной строки и по границе ключа
    (`get:` не спутается с `getById:`; `"get"` — с `"getById"`). -1 — не найдено.
    """
    yaml_form = key + ":"
    json_dq = '"' + key + '"'
    json_sq = "'" + key + "'"
    for idx in range(start, len(lines)):
        s = lines[idx].strip()
        if s.startswith(yaml_form) or s.startswith(json_dq) or s.startswith(json_sq):
            return idx
    return -1


def extract_openapi_routes(parsed: ParsedFile) -> list:
    """Эндпоинты продукта из OpenAPI/Swagger-спеки: пары путь+метод в `paths` → route (inferred).

    Структурный (не исполняемый) разбор `parsed.source` файлов `.yaml`/`.yml`/`.json`. Срабатывает
    ТОЛЬКО при индикаторе top-level `openapi:`/`swagger:` (иначе `[]` — чужой yaml/json не трогаем).
    Каждая пара «путь (ключ `paths`) + метод (get/post/put/delete/patch)» = один route;
    ref = "file:line|<METHOD> <path>". confidence: inferred — спека это ЗАЯВЛЕННЫЙ контракт (может
    расходиться с кодом), плюс структурный разбор ≠ доказательный AST.
    """
    src = parsed.source
    if not _has_indicator_text(src):
        return []
    data = _load(parsed)
    if not _top_level_is_spec(data):
        return []
    paths = data.get("paths")  # type: ignore[union-attr] — _top_level_is_spec гарантирует dict
    if not isinstance(paths, dict):
        return []

    lines = src.splitlines()
    paths_line = _key_line(lines, "paths", 0)
    path_search_start = paths_line + 1 if paths_line >= 0 else 0

    out: list = []
    for raw_path, item in paths.items():
        if not isinstance(raw_path, str) or not raw_path.startswith("/"):
            continue  # x-расширения, $ref-ключи и не-путевые ключи в paths — не эндпоинты
        if not isinstance(item, dict):
            continue  # значение пути должно быть path-item-словарём с операциями
        path_line = _key_line(lines, raw_path, path_search_start)
        method_search_start = path_line + 1 if path_line >= 0 else path_search_start
        for method in _HTTP_METHODS:
            if method not in item:
                continue
            m_line = _key_line(lines, method, method_search_start)
            if m_line >= 0:
                lineno = m_line + 1
            elif path_line >= 0:
                lineno = path_line + 1
            else:
                lineno = 1
            symbol = f"{method.upper()} {raw_path}"
            out.append(Surface(kind="route",
                               ref=f"{parsed.rel_path}:{lineno}|{symbol}",
                               confidence="inferred", extractor="openapi-spec"))
    return out
