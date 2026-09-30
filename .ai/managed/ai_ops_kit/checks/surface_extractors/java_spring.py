# -*- coding: utf-8 -*-
"""Серверные HTTP-маршруты Spring-бэкенда (Java, вид поверхности `route`): аннотации @*Mapping.

ВАЖНО: как и Go (`go_web`) и JS/TS-бэкенд (`js_server`), Java — НЕ Python, stdlib `ast` тут
неприменим. Разбор `.java` ДЕТЕРМИНИРОВАННЫЙ и БЕЗ ИСПОЛНЕНИЯ: паттерный скан `parsed.source`
(компилятор Java / JVM не зовутся, сети нет, needs_ast=False).

ЧЕСТНОСТЬ СИЛЫ. Это НЕ доказательный AST того же класса, что питон-экстракторы маршрутов: регэксп по
тексту — эвристика, а не грамматика. Поэтому честный дефолт — **confidence: inferred** (как go_web /
js_server: verified только доказуемое точным AST). Spring-route-поверхности видны аналитику и попадают
в каталог, но в W4 НЕ блокируют прогон.

ГРАНИЦЫ РАЗБОРА (что берётся, что пропускается):
  * method-аннотации `@GetMapping("/p")`, `@PostMapping`, `@PutMapping`, `@DeleteMapping`,
    `@PatchMapping` и `@RequestMapping(...)` — литеральный путь метода → route;
  * путь может быть позиционным литералом (`@GetMapping("/p")`) или именованным атрибутом
    `value = "/p"` / `path = "/p"` (в т.ч. `@RequestMapping(value="/p", method=RequestMethod.GET)`);
  * class-уровневый `@RequestMapping("/prefix")` над `@RestController`/`@Controller`-классом задаёт
    ПРЕФИКС класса — он склеивается с путём метода (оба литералы). Сам префикс маршрутом НЕ считается
    (это не эндпоинт, а основа для методов) — в отличие от Go-групп, где группа отдельный route.
Путь-НЕ-литерал (переменная / константа / склейка через `+`) в кавычки не попадает → пропуск. Массив
путей (`{"/a","/b"}`) начинается с `{`, не с `"` → пропуск (честно в honest_limits).

Файл сужён к индикатору Spring (импорт `org.springframework.web.bind.annotation` ИЛИ наличие
`@RestController`/`@Controller`), чтобы одноимённая чужая аннотация `@GetMapping` из другого фреймворка
не дала ложный route.

Битый/непарсибельный/не-utf-8 .java регэксп не роняет: грамматику мы не разбираем, скан строки не
падает (нечитаемый файл отсекается ядром обхода до вызова экстрактора).
"""
from __future__ import annotations

import re

from ai_ops_kit.checks.surface_extractors._common import ParsedFile, Surface

# ─── индикатор Spring-веба ──────────────────────────────────────────────────────────────────────
# Файл считается Spring-контроллером только при явном индикаторе — иначе одноимённая чужая аннотация
# `@GetMapping`/`@RequestMapping` другого фреймворка дала бы ложный маршрут (симметрия с import-сужением
# питон-/go-/js-экстракторов). Индикатор: импорт пакета аннотаций Spring ИЛИ сама аннотация контроллера.
_SPRING_INDICATOR_RE = re.compile(
    r"org\.springframework\.web\.bind\.annotation"
    r"|@RestController\b"
    r"|@Controller\b")

# method-аннотации маршрутов Spring. `@RequestMapping` — и на методе, и на классе (различаем позицией,
# см. class-level ниже). Остальные (@GetMapping/@PostMapping/…) — только на методах. Скобки либо
# ЗАКРЫТЫ (`(…)`), либо их нет вовсе (аннотация без аргументов): незакрытая `(` (обрыв файла) — не матч,
# чтобы битый исходник не породил фантомный корневой маршрут.
_MAPPING_RE = re.compile(
    r"@(Get|Post|Put|Delete|Patch|Request)Mapping\b"
    r"(?:\s*\(([^)]*)\)|\s*(?=[^(]|$))")

# class-уровневый `@RequestMapping(...)`: аннотация, за которой до объявления `class`/`interface`/`enum`
# нет ни `{`, ни `}`, ни `;` (то есть мы всё ещё в блоке аннотаций объявления типа, а не в теле метода).
# Метод-уровневый `@RequestMapping` до `class` не дотянется — за ним идёт сигнатура метода с `{`.
_CLASS_LEVEL_MAPPING_RE = re.compile(
    r"@RequestMapping\b\s*(?:\(([^)]*)\))?"
    r"[^{};]*?"
    r"\b(?:class|interface|enum)\b")

# путь из аргументов аннотации: позиционный строковый литерал В НАЧАЛЕ (`"…"`) либо именованный
# атрибут `value`/`path` = `"…"`. Java-строки — двойные кавычки. `{`-массив под эти паттерны не подходит.
_POS_PATH_RE = re.compile(r'^\s*"([^"\n]*)"')
_ATTR_PATH_LIT_RE = re.compile(r'\b(?:value|path)\s*=\s*"([^"\n]*)"')
_ATTR_PATH_ANY_RE = re.compile(r'\b(?:value|path)\s*=')          # слот пути есть (литерал или нет)
_NAMED_ARG_HEAD_RE = re.compile(r'^\s*\w+\s*=')                  # аргументы начинаются с `имя=`

# Результаты разбора аргументов метод-аннотации: "lit" — доказанный литеральный путь; "prefix" — пути
# нет, маршрут сводится к префиксу класса/корню; "skip" — слот пути есть, но путь НЕ литерал → пропуск.
_PREFIX = ("prefix", "")


def _lineno(source: str, pos: int) -> int:
    """Номер строки (1-базный) позиции pos в source — по числу переводов строки до неё."""
    return source.count("\n", 0, pos) + 1


def _class_prefix(args: str | None) -> str:
    """Литеральный префикс class-@RequestMapping, либо "" (нет литерала / массив / переменная).

    Позиционный литерал (`@RequestMapping("/p")`) или именованный `value=`/`path=` (`@RequestMapping(
    value="/p")`). Массив префиксов / переменная / константа → "" (префикс не прибавляем — честный предел).
    """
    if args is None:
        return ""
    m = _POS_PATH_RE.match(args)
    if m is not None:
        return m.group(1)
    m = _ATTR_PATH_LIT_RE.search(args)
    return m.group(1) if m is not None else ""


def _method_path(args: str | None) -> tuple:
    """Разобрать аргументы метод-аннотации → ("lit", path) | ("prefix", "") | ("skip", "").

    ("lit", path) — доказанный литеральный путь (позиционный `"…"` или `value=`/`path="…"`).
    ("prefix", "") — литерального пути НЕТ, но и слота пути нет (нет аргументов, `@GetMapping()`, либо
      только прочие атрибуты вроде `produces=`): маршрут = префикс класса / корень.
    ("skip", "") — слот пути ЕСТЬ, но путь НЕ литерал (позиционная константа/переменная, `value=CONST`,
      массив `{…}`): доказать нельзя → пропуск (не выдаём префикс за доказанный маршрут).
    """
    if args is None:
        return _PREFIX                                   # @PostMapping — нет скобок → префикс
    m = _ATTR_PATH_LIT_RE.search(args)
    if m is not None:
        return ("lit", m.group(1))                       # value="/p"/path="/p"
    if _ATTR_PATH_ANY_RE.search(args):
        return ("skip", "")                              # value=CONST / value={…} — путь не литерал
    s = args.strip()
    if not s:
        return _PREFIX                                   # @GetMapping() — пустые скобки → префикс
    if s[0] == '"':
        m = _POS_PATH_RE.match(args)
        return ("lit", m.group(1)) if m is not None else ("skip", "")
    if _NAMED_ARG_HEAD_RE.match(args):
        return _PREFIX                                   # только produces=/consumes=/… → префикс
    return ("skip", "")                                  # позиционная константа/массив → не литерал


def _join(prefix: str, sub: str) -> str:
    """Склеить префикс класса и путь метода в один URL с единственным ведущим "/"."""
    segs = [s.strip("/") for s in (prefix, sub) if s and s.strip("/")]
    return "/" + "/".join(segs) if segs else "/"


def extract_spring_routes(parsed: ParsedFile) -> list:
    """Серверные HTTP-маршруты Spring: @*Mapping (+ class-@RequestMapping-префикс) по ТЕКСТУ → inferred.

    Текстовый (не AST) разбор `parsed.source`. Берётся ТОЛЬКО в файле с индикатором Spring (импорт
    `org.springframework.web.bind.annotation` или `@RestController`/`@Controller`). Путь метода и
    префикс класса — строковые литералы (позиционный или `value=`/`path=`), склеиваются в полный URL;
    для каждого method-декоратора берётся ближайший ПРЕДШЕСТВУЮЩИЙ class-@RequestMapping. Метод без
    литерального пути (`@PostMapping`, `@GetMapping()`) сводится к префиксу/корню. Путь-НЕ-литерал
    (переменная/константа/склейка `+`/массив) → пропуск. ref = "file:line|<path>", confidence: inferred.
    """
    src = parsed.source
    if not _SPRING_INDICATOR_RE.search(src):
        return []
    # (позиция → префикс) всех class-уровневых @RequestMapping — чтобы привязать метод к ближайшему до
    # него (несколько контроллеров в одном файле разводятся по позиции). Сам префикс — не route.
    class_levels = [(m.start(), _class_prefix(m.group(1)))
                    for m in _CLASS_LEVEL_MAPPING_RE.finditer(src)]
    class_starts = {pos for pos, _ in class_levels}
    out: list = []
    for m in _MAPPING_RE.finditer(src):
        if m.start() in class_starts:
            continue   # это class-@RequestMapping (префикс), а не эндпоинт-метод
        kind, sub = _method_path(m.group(2))
        if kind == "skip":
            # слот пути есть, но путь НЕ литерал (переменная/константа/массив) → не выдаём префикс за
            # доказанный маршрут там, где реальный путь скрыт за нелитералом.
            continue
        prefix = ""
        for pos, pfx in class_levels:
            if pos < m.start():
                prefix = pfx
            else:
                break
        path_value = _join(prefix, sub)
        out.append(Surface(kind="route", ref=f"{parsed.rel_path}:{_lineno(src, m.start())}|{path_value}",
                           confidence="inferred", extractor="spring-web"))
    return out
