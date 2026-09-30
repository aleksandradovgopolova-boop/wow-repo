"""Извлечение наблюдаемых ПОВЕРХНОСТЕЙ продукта из кода дочки (W2 feature-registry-coverage).

Кит выводит наблюдаемые точки контакта продукта (маршруты/эндпоинты, CLI-команды, экраны UI,
публичный API) прямо из ИСХОДНИКА дочки — детерминированно, без сети и без вызова модели — и
возвращает список записей `surface` строго по контракту схемы реестра фич
(`registry/feature-registry/feature-registry.schema.yaml`): `{kind, ref: "file:line|symbol",
confidence, extractor}`. Дальше судья охвата (W3) сверит эти поверхности с реестром фич, а честная
сила по `confidence` (W4) решит, блокировать прогон или только предупреждать.

ПОЧЕМУ ЗДЕСЬ, В `checks`. Извлечение — ЧИСТОЕ, read-only чтение исходника дочки на stdlib (`ast`) без
импорта чего-либо из ai_ops_kit выше foundation. Ровно контракт пакета `checks` (слой primitives):
проверяющую/аналитическую логику держим НИЖЕ entrypoints, чтобы её звали ВНИЗ по слоям, а не тянули
`validation` вверх. Судья охвата (W3) импортирует `extract_surfaces` отсюда как библиотеку в свой
слой; CLI-обёртка живёт в `devtools` (точка входа), она и проводит модуль в контур.

ЧЕСТНОСТЬ СИЛЫ = ЧЕСТНОСТЬ `confidence` (FEAT-003/004). Поверхность, доказанная точным разбором AST
(декоратор-литерал в исходнике), помечается `confidence: verified` — её потом вправе блокировать.
Всё, что выведено эвристикой/предположением, обязано быть `inferred` (только предупреждает). Поднимать
уверенность ради усиления блокировки запрещено стандартом FEAT.

РАСШИРЯЕМОСТЬ БЕЗ ФОРКА ЯДРА. Каждый стек покрывает отдельный ЭКСТРАКТОР-адаптер (`Extractor`),
объявляющий, к каким файлам применим и какую уверенность даёт. Ядро обхода (`extract_surfaces`)
парсит исходники и раздаёт их применимым экстракторам; добавить новый стек — значит написать функцию
и зарегистрировать её записью, а не править обход. САМИ функции-экстракторы разложены по стекам в
подпакете `surface_extractors/` (python_web / python_cli / js_ui / js_server / go_web / java_spring / graphql_api / grpc_proto / trpc_router / ruby_rails / dotnet_aspnet / openapi_spec / phoenix_router / laravel_routes / ktor_routing / asyncapi_spec, общие примитивы — в `_common`),
чтобы модуль-вход не рос монолитом; этот файл держит контракт (`Surface`, `ParsedFile`, `Extractor`),
сборку `DEFAULT_EXTRACTORS` и ядро обхода. Реализованные экстракторы честно продекларированы в
`registry/feature-registry/surface-extractors.yaml` (инвариант честных capability-деклараций).
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence

from ai_ops_kit.checks.surface_extractors._common import ParsedFile, Surface
from ai_ops_kit.checks.surface_extractors.asyncapi_spec import extract_asyncapi_operations
from ai_ops_kit.checks.surface_extractors.dotnet_aspnet import extract_aspnet_routes
from ai_ops_kit.checks.surface_extractors.go_web import extract_go_web_routes
from ai_ops_kit.checks.surface_extractors.graphql_api import extract_graphql_operations
from ai_ops_kit.checks.surface_extractors.grpc_proto import extract_grpc_rpcs
from ai_ops_kit.checks.surface_extractors.java_spring import extract_spring_routes
from ai_ops_kit.checks.surface_extractors.laravel_routes import extract_laravel_routes
from ai_ops_kit.checks.surface_extractors.js_server import (
    extract_express_routes,
    extract_nest_routes,
    extract_next_routes,
)
from ai_ops_kit.checks.surface_extractors.js_ui import (
    extract_angular_router_screens,
    extract_react_router_screens,
    extract_vue_router_screens,
)
from ai_ops_kit.checks.surface_extractors.ktor_routing import extract_ktor_routes
from ai_ops_kit.checks.surface_extractors.openapi_spec import extract_openapi_routes
from ai_ops_kit.checks.surface_extractors.phoenix_router import extract_phoenix_routes
from ai_ops_kit.checks.surface_extractors.python_cli import (
    extract_console_scripts,
    extract_python_cli_argparse,
    extract_python_cli_click,
)
from ai_ops_kit.checks.surface_extractors.python_web import (
    extract_aiohttp_routes,
    extract_django_urls,
    extract_drf_router,
    extract_python_web_routes,
)
from ai_ops_kit.checks.surface_extractors.ruby_rails import extract_rails_routes
from ai_ops_kit.checks.surface_extractors.trpc_router import extract_trpc_operations

# `Surface` и `ParsedFile` живут в подпакете (`_common`), но ОСТАЮТСЯ публичными именами этого
# модуля-входа: внешний контракт `surface_extraction.Surface` / `.ParsedFile` не меняется.
__all__ = [
    "Surface", "ParsedFile", "Extractor", "extract_surfaces", "DEFAULT_EXTRACTORS",
]

# Служебные/чужие каталоги — не код продукта. Обход в них не заходит (шум и чужой исходник).
_SKIP_DIRS = frozenset({
    ".git", ".ai", ".hg", ".svn", "node_modules", "venv", ".venv", "env", ".env",
    "__pycache__", ".tox", ".nox", "build", "dist", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "site-packages", ".eggs",
})

# Расширения фронтенда, к которым применимы screen-экстракторы (только UI-роуты). Это МЕТАДАННЫЕ
# реестра (к каким файлам применим экстрактор), а не логика разбора — потому живут у сборки
# Extractor'ов, а не в js_ui.py (там разбор идёт по содержимому, не по расширению).
_SCREEN_JS_SUFFIXES = frozenset({".jsx", ".tsx", ".js", ".ts"})
_SCREEN_VUE_SUFFIXES = frozenset({".vue", ".js", ".ts"})
# Angular объявляет маршруты-экраны в TypeScript (const routes: Routes / RouterModule.forRoot) —
# исходник это .ts (не .tsx/.js: Angular-роутинг живёт в TS-модулях). Метаданные реестра (к каким
# файлам применим экстрактор) держим у сборки Extractor'ов, а разбор идёт по содержимому (js_ui.py).
_SCREEN_ANGULAR_SUFFIXES = frozenset({".ts"})
# Серверные JS/TS-бэкенды (route). Express — обычный JS/TS; Nest — TS-декораторы (+ .tsx); Next —
# файловый роутинг по .js/.ts/.jsx/.tsx. Это МЕТАДАННЫЕ реестра (к каким файлам применим экстрактор),
# потому живут у сборки Extractor'ов, а разбор идёт по содержимому/раскладке (js_server.py).
_SERVER_JS_SUFFIXES = frozenset({".js", ".ts", ".mjs", ".cjs"})
_NEST_JS_SUFFIXES = frozenset({".ts", ".js", ".mjs", ".cjs", ".tsx"})
_NEXT_JS_SUFFIXES = frozenset({".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"})
# Серверные Go-бэкенды (route). Разбор ТЕКСТОМ по .go (stdlib ast к Go неприменим) — net/http, gin,
# chi, echo, gorilla/mux. Метаданные реестра (к каким файлам применим экстрактор) держим у сборки
# Extractor'ов, а разбор идёт по содержимому/импортам (go_web.py).
_SERVER_GO_SUFFIXES = frozenset({".go"})
# Серверные Spring-бэкенды (route). Разбор ТЕКСТОМ по .java (stdlib ast к Java неприменим) —
# аннотации @GetMapping/@PostMapping/…/@RequestMapping + class-префикс @RestController/@Controller.
# Метаданные реестра (к каким файлам применим экстрактор) держим у сборки Extractor'ов, а разбор идёт
# по содержимому/аннотациям (java_spring.py).
_SERVER_JAVA_SUFFIXES = frozenset({".java"})
# GraphQL-схемы (SDL, вид api). Разбор ТЕКСТОМ по .graphql/.gql (не GraphQL-парсер, без исполнения) —
# поля корневых типов Query/Mutation/Subscription (+ extend). Метаданные реестра (к каким файлам
# применим экстрактор) держим у сборки Extractor'ов, а разбор идёт по содержимому (graphql_api.py).
_GRAPHQL_SDL_SUFFIXES = frozenset({".graphql", ".gql"})
# gRPC-контракты (Protocol Buffers, вид api). Разбор ТЕКСТОМ по .proto (не protobuf-парсер, без
# исполнения) — объявления rpc внутри блоков service. Метаданные реестра (к каким файлам применим
# экстрактор) держим у сборки Extractor'ов, а разбор идёт по содержимому (grpc_proto.py).
_GRPC_PROTO_SUFFIXES = frozenset({".proto"})
# tRPC-роутеры (TypeScript, вид api). Разбор ТЕКСТОМ по .ts (не TS-парсер, без исполнения) —
# процедуры .query/.mutation/.subscription как ключи объекта роутера createTRPCRouter/t.router.
# .ts УЖЕ в union суффиксов (js-экстракторы), новый суффикс не нужен. Метаданные реестра (к каким
# файлам применим экстрактор) держим у сборки Extractor'ов, а разбор идёт по содержимому (trpc_router.py).
_TRPC_TS_SUFFIXES = frozenset({".ts"})
# Серверные Rails-бэкенды (route). Разбор ТЕКСТОМ по .rb (stdlib ast к Ruby неприменим) — DSL
# config/routes.rb: get/post/… + resources/resource + root + namespace/scope-префиксы. Метаданные
# реестра (к каким файлам применим экстрактор) держим у сборки Extractor'ов, а разбор идёт по
# содержимому/индикатору (ruby_rails.py).
_RUBY_RB_SUFFIXES = frozenset({".rb"})
# Серверные ASP.NET Core-бэкенды (route). Разбор ТЕКСТОМ по .cs (stdlib ast к C# неприменим) —
# attribute routing (@[HttpGet]/…/[Route] + class-[Route]-префикс с токеном [controller]) и Minimal
# APIs (app.MapGet/…). Метаданные реестра (к каким файлам применим экстрактор) держим у сборки
# Extractor'ов, а разбор идёт по содержимому/индикатору (dotnet_aspnet.py).
_DOTNET_CS_SUFFIXES = frozenset({".cs"})
# OpenAPI/Swagger-спеки (route). Разбор СТРУКТУРНЫЙ по тексту .yaml/.yml/.json (yaml.safe_load /
# json.loads, БЕЗ исполнения и сети) — пары путь+метод из секции `paths`. Эти расширения ОЧЕНЬ
# распространены (package.json, конфиги), потому экстрактор сужен индикатором top-level
# `openapi:`/`swagger:` в самом ПОДМОДУЛЕ (openapi_spec.py), а не расширением. Метаданные реестра (к
# каким файлам применим экстрактор) держим у сборки Extractor'ов; сам разбор — по содержимому.
_OPENAPI_SPEC_SUFFIXES = frozenset({".yaml", ".yml", ".json"})
# Серверные Phoenix-бэкенды (route). Разбор ТЕКСТОМ по .ex (stdlib ast к Elixir неприменим) — DSL
# модуля router.ex: get/post/… + resources + scope-префиксы. Метаданные реестра (к каким файлам
# применим экстрактор) держим у сборки Extractor'ов, а разбор идёт по содержимому/индикатору
# (phoenix_router.py).
_PHOENIX_EX_SUFFIXES = frozenset({".ex"})
# Серверные Laravel-бэкенды (route). Разбор ТЕКСТОМ по .php (stdlib ast к PHP неприменим) — фасад
# `Route::get/post/…`, `Route::resource`/`apiResource` и `prefix`-группы в routes/*.php. Метаданные
# реестра (к каким файлам применим экстрактор) держим у сборки Extractor'ов, а разбор идёт по
# содержимому/индикатору (laravel_routes.py).
_LARAVEL_PHP_SUFFIXES = frozenset({".php"})
# Серверные Ktor-бэкенды (route). Разбор ТЕКСТОМ по .kt (stdlib ast к Kotlin неприменим) — DSL
# `routing { … }`: method-вызовы get/post/… с литеральным путём + блоки `route("/prefix") { … }`
# (вложенность — балансом скобок). Метаданные реестра (к каким файлам применим экстрактор) держим у
# сборки Extractor'ов, а разбор идёт по содержимому/индикатору (ktor_routing.py).
_KTOR_KT_SUFFIXES = frozenset({".kt"})
# AsyncAPI-спеки (api). Разбор СТРУКТУРНЫЙ по тексту .yaml/.yml/.json (yaml.safe_load / json.loads,
# БЕЗ исполнения и сети) — событийные операции: пары канал+publish/subscribe (2.x) и top-level
# `operations` (3.x). Те же расширения, что и у openapi-spec, ОЧЕНЬ распространены — потому экстрактор
# сужен индикатором top-level `asyncapi:` в самом ПОДМОДУЛЕ (asyncapi_spec.py), а не расширением (это
# разводит его и с OpenAPI, у которого индикатор `openapi:`/`swagger:`). Метаданные реестра (к каким
# файлам применим экстрактор) держим у сборки Extractor'ов; сам разбор — по содержимому.
_ASYNCAPI_SPEC_SUFFIXES = frozenset({".yaml", ".yml", ".json"})


# Экстрактор = адаптер под ОДИН стек. Регистрация записью делает добавление стека вопросом
# «функция + запись», а не форком обхода — точка расширения кита.
@dataclass(frozen=True)
class Extractor:
    id: str                                   # id из surface-extractors.yaml
    suffixes: frozenset                       # расширения файлов, к которым применим ({".py"})
    surface_kinds: tuple                      # какие kind порождает (для честной декларации)
    confidence: str                           # уверenность метода (verified | inferred)
    extract: Callable[[ParsedFile], list]     # (ParsedFile) -> list[Surface]
    needs_ast: bool = True                    # True — экстрактору нужен разобранный AST (.py);
    #                                           False — работает по сырому тексту (TOML/INI и т.п.)


# Реестр реализованных экстракторов. Порядок ключей — порядок применения (детерминизм). Добавление
# нового стека = ещё одна запись здесь + честная декларация в surface-extractors.yaml.
PYTHON_WEB_ROUTES = Extractor(
    id="python-web-routes",
    suffixes=frozenset({".py"}),
    surface_kinds=("route",),
    confidence="verified",
    extract=extract_python_web_routes,
)

PYTHON_CLI_ARGPARSE = Extractor(
    id="python-cli-argparse",
    suffixes=frozenset({".py"}),
    surface_kinds=("cli",),
    confidence="verified",
    extract=extract_python_cli_argparse,
)

PYTHON_CLI_CLICK = Extractor(
    id="python-cli-click",
    suffixes=frozenset({".py"}),
    surface_kinds=("cli",),
    confidence="verified",
    extract=extract_python_cli_click,
)

PYTHON_CONSOLE_SCRIPTS = Extractor(
    id="python-console-scripts",
    suffixes=frozenset({".toml", ".cfg"}),
    surface_kinds=("cli",),
    confidence="verified",
    extract=extract_console_scripts,
    needs_ast=False,
)

DJANGO_URLS = Extractor(
    id="django-urls",
    suffixes=frozenset({".py"}),
    surface_kinds=("route",),
    confidence="verified",
    extract=extract_django_urls,
)

DRF_ROUTER = Extractor(
    id="drf-router",
    suffixes=frozenset({".py"}),
    surface_kinds=("route",),
    confidence="verified",
    extract=extract_drf_router,
)

AIOHTTP_ROUTES = Extractor(
    id="aiohttp-routes",
    suffixes=frozenset({".py"}),
    surface_kinds=("route",),
    confidence="verified",
    extract=extract_aiohttp_routes,
)

REACT_ROUTER_SCREENS = Extractor(
    id="react-router",
    suffixes=_SCREEN_JS_SUFFIXES,
    surface_kinds=("screen",),
    confidence="inferred",   # текстовый JS-разбор — эвристика паттерна, не доказательный AST
    extract=extract_react_router_screens,
    needs_ast=False,
)

VUE_ROUTER_SCREENS = Extractor(
    id="vue-router",
    suffixes=_SCREEN_VUE_SUFFIXES,
    surface_kinds=("screen",),
    confidence="inferred",   # текстовый JS-разбор — эвристика паттерна, не доказательный AST
    extract=extract_vue_router_screens,
    needs_ast=False,
)

ANGULAR_ROUTER_SCREENS = Extractor(
    id="angular-router",
    suffixes=_SCREEN_ANGULAR_SUFFIXES,
    surface_kinds=("screen",),
    confidence="inferred",   # текстовый TS-разбор — эвристика паттерна, не доказательный AST
    extract=extract_angular_router_screens,
    needs_ast=False,
)

EXPRESS_ROUTES = Extractor(
    id="express",
    suffixes=_SERVER_JS_SUFFIXES,
    surface_kinds=("route",),
    confidence="inferred",   # текстовый JS-разбор — эвристика паттерна, не доказательный AST
    extract=extract_express_routes,
    needs_ast=False,
)

NEST_ROUTES = Extractor(
    id="nest",
    suffixes=_NEST_JS_SUFFIXES,
    surface_kinds=("route",),
    confidence="inferred",   # текстовый разбор декораторов — эвристика паттерна, не AST
    extract=extract_nest_routes,
    needs_ast=False,
)

NEXT_ROUTES = Extractor(
    id="next",
    suffixes=_NEXT_JS_SUFFIXES,
    surface_kinds=("route",),
    confidence="inferred",   # структурный вывод из раскладки файлов — не доказательный AST
    extract=extract_next_routes,
    needs_ast=False,
)

GO_WEB_ROUTES = Extractor(
    id="go-web",
    suffixes=_SERVER_GO_SUFFIXES,
    surface_kinds=("route",),
    confidence="inferred",   # текстовый разбор Go — эвристика паттерна, не доказательный AST
    extract=extract_go_web_routes,
    needs_ast=False,
)

SPRING_ROUTES = Extractor(
    id="spring-web",
    suffixes=_SERVER_JAVA_SUFFIXES,
    surface_kinds=("route",),
    confidence="inferred",   # текстовый разбор Java-аннотаций — эвристика паттерна, не доказательный AST
    extract=extract_spring_routes,
    needs_ast=False,
)

GRAPHQL_OPERATIONS = Extractor(
    id="graphql-schema",
    suffixes=_GRAPHQL_SDL_SUFFIXES,
    surface_kinds=("api",),
    confidence="inferred",   # текстовый разбор SDL — не полноценный GraphQL-парсер/AST
    extract=extract_graphql_operations,
    needs_ast=False,
)

GRPC_RPCS = Extractor(
    id="grpc-proto",
    suffixes=_GRPC_PROTO_SUFFIXES,
    surface_kinds=("api",),
    confidence="inferred",   # текстовый разбор .proto — не полноценный protobuf-парсер/AST
    extract=extract_grpc_rpcs,
    needs_ast=False,
)

TRPC_OPERATIONS = Extractor(
    id="trpc-router",
    suffixes=_TRPC_TS_SUFFIXES,
    surface_kinds=("api",),
    confidence="inferred",   # текстовый разбор TS — не полноценный TS-парсер/AST
    extract=extract_trpc_operations,
    needs_ast=False,
)

RAILS_ROUTES = Extractor(
    id="rails-routes",
    suffixes=_RUBY_RB_SUFFIXES,
    surface_kinds=("route",),
    confidence="inferred",   # текстовый разбор Ruby-DSL — эвристика паттерна, не доказательный AST
    extract=extract_rails_routes,
    needs_ast=False,
)

ASPNET_ROUTES = Extractor(
    id="aspnet-routes",
    suffixes=_DOTNET_CS_SUFFIXES,
    surface_kinds=("route",),
    confidence="inferred",   # текстовый разбор C# — эвристика паттерна, не доказательный AST
    extract=extract_aspnet_routes,
    needs_ast=False,
)

OPENAPI_ROUTES = Extractor(
    id="openapi-spec",
    suffixes=_OPENAPI_SPEC_SUFFIXES,
    surface_kinds=("route",),
    confidence="inferred",   # ЗАЯВЛЕННЫЙ контракт спеки (может расходиться с кодом) + структурный разбор ≠ AST
    extract=extract_openapi_routes,
    needs_ast=False,
)

PHOENIX_ROUTES = Extractor(
    id="phoenix-router",
    suffixes=_PHOENIX_EX_SUFFIXES,
    surface_kinds=("route",),
    confidence="inferred",   # текстовый разбор Elixir Phoenix-DSL — эвристика паттерна, не доказательный AST
    extract=extract_phoenix_routes,
    needs_ast=False,
)

LARAVEL_ROUTES = Extractor(
    id="laravel-routes",
    suffixes=_LARAVEL_PHP_SUFFIXES,
    surface_kinds=("route",),
    confidence="inferred",   # текстовый разбор фасада PHP — эвристика паттерна, не доказательный AST
    extract=extract_laravel_routes,
    needs_ast=False,
)

KTOR_ROUTES = Extractor(
    id="ktor-routing",
    suffixes=_KTOR_KT_SUFFIXES,
    surface_kinds=("route",),
    confidence="inferred",   # текстовый разбор Kotlin-DSL — эвристика паттерна, не доказательный AST
    extract=extract_ktor_routes,
    needs_ast=False,
)

ASYNCAPI_OPERATIONS = Extractor(
    id="asyncapi-spec",
    suffixes=_ASYNCAPI_SPEC_SUFFIXES,
    surface_kinds=("api",),
    confidence="inferred",   # ЗАЯВЛЕННЫЙ контракт спеки (может расходиться с кодом) + структурный разбор ≠ AST
    extract=extract_asyncapi_operations,
    needs_ast=False,
)

DEFAULT_EXTRACTORS: tuple = (
    PYTHON_WEB_ROUTES,
    PYTHON_CLI_ARGPARSE,
    PYTHON_CLI_CLICK,
    PYTHON_CONSOLE_SCRIPTS,
    DJANGO_URLS,
    DRF_ROUTER,
    AIOHTTP_ROUTES,
    REACT_ROUTER_SCREENS,
    VUE_ROUTER_SCREENS,
    ANGULAR_ROUTER_SCREENS,
    EXPRESS_ROUTES,
    NEST_ROUTES,
    NEXT_ROUTES,
    GO_WEB_ROUTES,
    SPRING_ROUTES,
    GRAPHQL_OPERATIONS,
    GRPC_RPCS,
    TRPC_OPERATIONS,
    RAILS_ROUTES,
    ASPNET_ROUTES,
    OPENAPI_ROUTES,
    PHOENIX_ROUTES,
    LARAVEL_ROUTES,
    KTOR_ROUTES,
    ASYNCAPI_OPERATIONS,
)


# ─── Ядро обхода ─────────────────────────────────────────────────────────────────────────────────

def _iter_source_files(root: Path, suffixes: frozenset) -> Iterator[Path]:
    """Детерминированный обход исходников дочки с нужными расширениями, минуя служебные каталоги."""
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        yield path


def extract_surfaces(child_root, extractors: Sequence[Extractor] | None = None) -> list:
    """Извлечь поверхности продукта из кода дочки. -> список dict строго по схеме surface.

    Детерминированно, только stdlib (`ast`), без сети и без вызова модели. Читает исходники под
    `child_root`, парсит один раз на файл и раздаёт AST применимым экстракторам (по расширению).
    Возврат отсортирован (ref, kind, extractor) — один и тот же вход даёт один и тот же выход.

    extractors — набор экстракторов; по умолчанию DEFAULT_EXTRACTORS (реализованные и честно
    задекларированные). Файл, который не парсится (SyntaxError чужого/битого исходника), молча
    пропускается: извлечение — не линтер, оно не обязано разбирать некорректный код.
    """
    root = Path(child_root)
    active = tuple(DEFAULT_EXTRACTORS if extractors is None else extractors)
    wanted: frozenset = frozenset().union(*(e.suffixes for e in active)) if active else frozenset()
    surfaces: list = []
    for path in _iter_source_files(root, wanted):
        rel = path.relative_to(root).as_posix()
        applicable = [ex for ex in active if path.suffix in ex.suffixes]
        if not applicable:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue   # нечитаемый или не-utf-8 файл (частая грабля не-Python исходников) — пропуск
        tree = None
        if any(ex.needs_ast for ex in applicable):
            try:
                tree = ast.parse(source, filename=rel)
            except (SyntaxError, ValueError):
                tree = None   # не Python/битый .py — AST-экстракторы просто не позовутся
        parsed = ParsedFile(rel_path=rel, source=source, tree=tree)
        for ex in applicable:
            if ex.needs_ast and parsed.tree is None:
                continue   # AST-экстрактору нечего дать: файл не разобрался
            surfaces.extend(ex.extract(parsed))
    surfaces.sort(key=lambda s: (s.ref, s.kind, s.extractor))
    return [s.as_record() for s in surfaces]
