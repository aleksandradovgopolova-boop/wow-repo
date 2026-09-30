#!/usr/bin/env python3
"""SR-9..13: архитектурные инварианты дочки как ДАННЫЕ, проверяемые advisory (срез 3).

ПОВОД (docs/engineering-standard-requirements.md §4, срез 3). Governance-отчёт (срез 2) уже
доставлен и показывает секции обязательных артефактов. Срез 3 добавляет самый ценный и самый
приблизительный слой — архитектурные инварианты дочки: «фронтенд не ходит в базу напрямую» и
подобные. Он идёт последним не из-за сложности, а потому что до него должны существовать место для
отчёта (SR-17..20) и правило повышения силы (§5.2): инвариант поверх JS/TS-графа рождается advisory.

ИНВАРИАНТ — ДАННЫЕ С ИДЕНТИФИКАТОРОМ, ПРАВИЛОМ, ПРИЧИНОЙ И СИЛОЙ (SR-9). Форма взята с
`packages/layering.yaml`, которым кит проверяет СЕБЯ: зоны, запреты между зонами, `reason` у каждого
правила, поимённые исключения и реестр `known_violations`, который вправе только СОКРАЩАТЬСЯ. Поэтому
`check()` здесь — та же логика, что у `validate_layering.check()`: зона запрещена другой зоне, если
правило это объявило и ребро не заморожено; исчезнувшее исключение обязано быть снято. Кит читает
СВОЁ объявление (пакеты Python), дочка — СВОЁ (зоны JS/TS), а логика одна, не две копии (SR-9).

ЗОНА ОБЪЯВЛЕНА ГЛОБАМИ И ЧИТАЕТСЯ ИЗ ОДНОГО МЕСТА (SR-10). `zone_of()` — единственный резолвер
путь→зона; тот же глоб-матчер, что у контуров (`contours._matches`), потому что зона и сигнальный
путь контура — одно понятие «какая это часть репозитория», и двух его реализаций быть не должно.

НАРУШЕНИЕ НАЗЫВАЕТ АДРЕС, А НЕ ФАКТ (SR-11). Не «инвариант нарушен», а `src/features/users/api.ts:12
-> @/db/client`: файл, строка и ребро графа. По выводу можно открыть файл и увидеть нарушение.

НЕПРОВЕРЯЕМОЕ — `not_checked` С ПРИЧИНОЙ, А НЕ `pass` (SR-12). Репозиторий без объявленных зон или
без правил не «прошёл» — его просто нечем проверить, и отчёт говорит это словом.

НОВАЯ ЗАВИСИМОСТЬ ТРЕБУЕТ ОБЪЯСНЕНИЯ (SR-13). Кит уже видит новые зависимости детерминированно
(`security_scan.new_dependencies`); здесь добавлена СВЯЗЬ: новая зависимость, не упомянутая ни в
одном ADR, — находка с адресом (манифест + имя). С ADR — тишина.

ГРАНИЦА (§5.2). Разбор импортов JS/TS — регулярными выражениями (stdlib+pyyaml, без tree-sitter):
он не видит динамический импорт, ре-экспорт через barrel и алиасы сборщика, и потому даёт ПРОПУСКИ,
не ложные тревоги. Инвариант поверх такого графа рождается `advisory` и повышается до блокирующего
ТОЛЬКО замером на живом репозитории (issue #606: «измерено число ложных»). Этот модуль собирает и
ПОКАЗЫВАЕТ находки; блокировать он не вправе.

Форму объявления задаёт schemas/architecture-invariants.schema.json.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from ai_ops_kit.planning import contours
from ai_ops_kit.security import security_scan

SCHEMA_VERSION = 1
KIND = "architecture-invariants"
STRENGTH = "advisory"                     # сила инварианта поверх JS/TS-графа (§5.2, SR-9)

# Состояния строки архитектурной проверки (свой набор — это advisory-слой поверх отчёта).
PASS = "pass"
VIOLATED = "violated"
NOT_CHECKED = "not_checked"

# Где дочка объявляет инвариант: файл в корне ИЛИ блок в .ai-ops.yaml (standard.architecture).
DECL_FILE = "architecture-invariants.yaml"
CONFIG_REL = ".ai-ops.yaml"

# Расширения исходников фронтенда/ноды, по которым строится граф импортов (regex, §5.2).
_JS_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte"}
_SKIP_DIRS = {".git", "node_modules", "dist", "build", ".next", "out", "coverage",
              "__pycache__", ".venv", "venv", "vendor", ".ai"}

# import ... from 'x' | export ... from 'x' | require('x') | import('x'). Строка нужна для адреса.
_IMPORT_RE = re.compile(
    r"""(?:import\b[^'"]*?from|export\b[^'"]*?from|require|import)\s*\(?\s*['"]([^'"]+)['"]""")

# Где живут ADR дочки (те же пути, что у сигналов контура decision/knowledge, product-operating-model).
_ADR_GLOBS = ("decisions/**", "docs/adr/**", "**/ADR-*.md", "adr/**", "research/**")


# ── Чтение объявления (SR-9). ─────────────────────────────────────────────────────────────────
def load_declaration(child_root) -> dict:
    """Объявление инварианта дочки: файл `architecture-invariants.yaml` ИЛИ `.ai-ops.yaml`.

    Приоритет у отдельного файла; при его отсутствии — блок `standard.architecture` конфига. Так
    зона объявлена в ОДНОМ месте (SR-10): два источника не складываются, второй лишь запасной.
    """
    root = Path(child_root)
    f = root / DECL_FILE
    if f.is_file():
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                data.setdefault("_source", DECL_FILE)
                return data
        except (OSError, yaml.YAMLError):
            return {}
    cfg = root / CONFIG_REL
    if cfg.is_file():
        try:
            c = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            arch = ((c.get("standard") or {}).get("architecture")) if isinstance(c, dict) else None
            if isinstance(arch, dict):
                arch = dict(arch)
                arch.setdefault("_source", f"{CONFIG_REL} standard.architecture")
                return arch
        except (OSError, yaml.YAMLError):
            return {}
    return {}


def _zones(decl: dict) -> dict:
    z = decl.get("zones") if isinstance(decl, dict) else None
    return {str(k): [str(p) for p in (v or [])] for k, v in z.items()} if isinstance(z, dict) else {}


def _aliases(decl: dict) -> dict:
    """Алиасы путей сборщика (`@/` -> `src/`): объявление, а не догадка (§5.2 — их regex не выводит)."""
    a = decl.get("aliases") if isinstance(decl, dict) else None
    return {str(k): str(v) for k, v in a.items()} if isinstance(a, dict) else {}


# ── Резолвер путь→зона: ОДИН на всех потребителей (SR-10). ────────────────────────────────────
def zone_of(rel_path: str, zones: dict) -> str | None:
    """Репо-относительный путь -> имя зоны по глобам объявления. None, если ни одна не совпала.

    Матчер — `contours._matches` (тот же `**`-глоб, что у сигнальных путей контуров): зона и
    сигнальный путь — одно понятие, и двух его реализаций в ките быть не должно (SR-10).
    """
    rel = str(rel_path).replace("\\", "/")
    for name, patterns in zones.items():
        if any(contours._matches(rel, p) for p in patterns):
            return name
    return None


# ── Граф импортов JS/TS (regex, advisory по построению — §5.2). ───────────────────────────────
def _iter_source_files(root: Path):
    for p in root.rglob("*"):
        if p.suffix not in _JS_EXT or not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        yield p


def _resolve_import(from_rel: str, spec: str, aliases: dict) -> str | None:
    """Строку импорта -> репо-относительный путь БЕЗ расширения, если её можно разрешить.

    Разрешаются относительные (`./`, `../`) и алиасные (`@/x` при объявленном алиасе) импорты.
    Голый пакет (`react`, `lodash`) не разрешается в путь — это внешняя зависимость, не ребро зон.
    Расширение и `/index` отбрасываются: зона определяется каталогом, а не файлом.
    """
    s = spec.strip()
    aliased = False
    for alias, target in aliases.items():
        if s.startswith(alias):
            s = target.rstrip("/") + "/" + s[len(alias):].lstrip("/")
            aliased = True
            break
    if s.startswith("."):                  # относительный импорт — разрешаем от каталога файла
        base = Path(from_rel).parent
        s = str((base / s)).replace("\\", "/")
    elif not aliased:
        return None                       # голый/scoped-пакет без объявленного алиаса — внешняя
                                          # зависимость или пропуск (§5.2: пропуск, не ложь)
    parts = []
    for seg in s.split("/"):
        if seg in ("", "."):
            continue
        if seg == ".." and parts:
            parts.pop()
        else:
            parts.append(seg)
    rel = "/".join(parts)
    for suf in (*_JS_EXT, ""):
        if rel.endswith(suf) and suf:
            rel = rel[: -len(suf)]
            break
    return rel or None


def build_import_graph(child_root, zones: dict, aliases: dict | None = None) -> list:
    """Рёбра графа импортов между ЗОНАМИ. -> список dict с адресом (SR-11).

    Каждое ребро: {from_file, line, import, from_zone, to_zone}. Рёбра, у которых зона источника
    или цели неизвестна, отбрасываются: инвариант говорит о зонах, а не о безымянных файлах.
    """
    root = Path(child_root)
    aliases = aliases or {}
    edges = []
    for p in sorted(_iter_source_files(root)):
        from_rel = str(p.relative_to(root)).replace("\\", "/")
        from_zone = zone_of(from_rel, zones)
        if from_zone is None:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for spec in _IMPORT_RE.findall(line):
                target = _resolve_import(from_rel, spec, aliases)
                if not target:
                    continue
                to_zone = zone_of(target, zones)
                if to_zone is None or to_zone == from_zone:
                    continue
                edges.append({"from_file": from_rel, "line": lineno, "import": spec,
                              "from_zone": from_zone, "to_zone": to_zone})
    return edges


# ── Проверка рёбер против правил — та же логика, что у validate_layering.check() (SR-9). ───────
def _known(decl: dict) -> set:
    """Замороженные рёбра `from_zone -> to_zone` (реестр вправе только сокращаться, SR-9)."""
    out = set()
    for item in decl.get("known_violations") or []:
        parts = str(item).split(":")[0].strip().split(" -> ")
        if len(parts) == 2:
            out.add((parts[0].strip(), parts[1].strip()))
    return out


def _exceptions(decl: dict) -> set:
    """Поимённые исключения `file -> zone` или `zone -> zone` (SR-9): точечно снимают находку."""
    out = set()
    for item in decl.get("exceptions") or []:
        parts = str(item).split(" -> ")
        if len(parts) == 2:
            out.add((parts[0].strip(), parts[1].strip()))
    return out


def check(decl: dict, edges: list) -> list:
    """Нарушения инварианта: ребро зон, запрещённое правилом и не снятое исключением/заморозкой.

    Возвращает список находок с адресом (SR-11). Та же форма логики, что у validate_layering.check:
    правило объявляет запрет между зонами с причиной; known_violations замораживает существующее;
    исчезнувшая заморозка — отдельная находка (реестр вправе только сокращаться).
    """
    rules = [r for r in (decl.get("rules") or []) if isinstance(r, dict)]
    known = _known(decl)
    exceptions = _exceptions(decl)
    findings = []
    seen_known = set()

    for e in edges:
        fz, tz = e["from_zone"], e["to_zone"]
        rule = next((r for r in rules
                     if (r.get("forbid") or {}).get("from") in (None, fz)
                     and (r.get("forbid") or {}).get("to") in (None, tz)
                     and ((r.get("forbid") or {}).get("from") or (r.get("forbid") or {}).get("to"))), None)
        if rule is None:
            continue
        if (e["from_file"], tz) in exceptions or (fz, tz) in exceptions:
            continue
        if (fz, tz) in known:
            seen_known.add((fz, tz))
            continue
        addr = f"{e['from_file']}:{e['line']} -> {e['import']}"
        findings.append({
            "requirement": f"architecture::{rule.get('id', fz + '->' + tz)}",
            "status": VIOLATED, "closed_by": "validator", "strength": STRENGTH,
            "address": addr,
            "detail": f"{fz} -> {tz}: {rule.get('reason', 'запрещённое ребро зон')}"})

    for stale in sorted(known - seen_known):
        findings.append({
            "requirement": f"architecture::known::{stale[0]}->{stale[1]}",
            "status": VIOLATED, "closed_by": "validator", "strength": STRENGTH,
            "address": f"{DECL_FILE} known_violations",
            "detail": f"заморозка {stale[0]} -> {stale[1]} исчезла из кода — снять из "
                      "known_violations (реестр вправе только сокращаться)"})
    return findings


# ── Новая зависимость без ADR — находка с адресом (SR-13). ────────────────────────────────────
def _adr_blob(root: Path) -> str:
    """Слитый текст ADR дочки (best-effort): по нему судим, объяснена ли новая зависимость."""
    chunks = []
    for p in root.rglob("*.md"):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root)).replace("\\", "/")
        if any(part in _SKIP_DIRS for part in Path(rel).parts):
            continue
        if any(contours._matches(rel, g) for g in _ADR_GLOBS):
            try:
                chunks.append(p.read_text(encoding="utf-8", errors="replace").lower())
            except OSError:
                continue
    return "\n".join(chunks)


def dependency_findings(child_root, before: dict, after: dict) -> list:
    """Новые зависимости (`security_scan.new_dependencies`), не упомянутые в ADR — находки (SR-13).

    before/after: {manifest_path: content} до/после изменения (в CI — база PR и рабочее дерево).
    Возврат — список находок с адресом «манифест :: имя»; зависимость, названная в ADR, тиха.
    """
    new_deps = security_scan.new_dependencies(before or {}, after or {})
    if not new_deps:
        return []
    blob = _adr_blob(Path(child_root))
    manifests = ", ".join(sorted(after or {})) or "манифест"
    findings = []
    for dep in new_deps:
        if dep and dep.lower() in blob:
            continue
        findings.append({
            "requirement": f"architecture::new-dependency::{dep}",
            "status": VIOLATED, "closed_by": "validator", "strength": STRENGTH,
            "address": f"{manifests} :: {dep}",
            "detail": f"новая внешняя зависимость '{dep}' не объяснена ни одним ADR "
                      "(decisions/**, docs/adr/**, **/ADR-*.md)"})
    return findings


# ── Сборка advisory-блока для governance-отчёта. ──────────────────────────────────────────────
def report(child_root, deps=None) -> dict:
    """Advisory-блок архитектурных инвариантов дочки. -> dict для ключа `architecture` отчёта.

    deps: (before, after) манифестов для SR-13, если контур PR даёт базу сравнения; иначе SR-13
    объявляется `not_checked` с причиной, а не молча пропускается.
    Блок отделён от четырёх чисел отчёта (§3.2 остаётся неизменным): архитектурный слой advisory и
    приблизительный, его нельзя складывать со структурным фактом секций.
    """
    root = Path(child_root)
    decl = load_declaration(root)
    zones = _zones(decl)
    rules = [r for r in (decl.get("rules") or []) if isinstance(r, dict)]

    rows: list = []
    if not zones or not rules:
        reason = ("зоны архитектуры не объявлены — нечем разрешать пути в зоны"
                  if not zones else "правила между зонами не объявлены — нечего проверять")
        rows.append({"requirement": "architecture::zones", "status": NOT_CHECKED,
                     "closed_by": "validator", "strength": STRENGTH, "reason": reason})
    else:
        edges = build_import_graph(root, zones, _aliases(decl))
        rows.extend(check(decl, edges))
        if not rows:
            rows.append({"requirement": "architecture::zones", "status": PASS,
                         "closed_by": "validator", "strength": STRENGTH,
                         "detail": f"граф импортов ({len(edges)} межзонных рёбер) не нарушил "
                                   f"{len(rules)} правил"})

    if deps is not None:
        rows.extend(dependency_findings(root, deps[0], deps[1]) or [
            {"requirement": "architecture::new-dependency", "status": PASS,
             "closed_by": "validator", "strength": STRENGTH,
             "detail": "новых внешних зависимостей нет"}])
    else:
        rows.append({"requirement": "architecture::new-dependency", "status": NOT_CHECKED,
                     "closed_by": "validator", "strength": STRENGTH,
                     "reason": "нет базы сравнения (вне контекста PR) — новые зависимости не выводятся"})

    counts = {PASS: 0, VIOLATED: 0, NOT_CHECKED: 0}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {
        "kind": KIND, "schema_version": SCHEMA_VERSION, "strength": STRENGTH,
        "declared": bool(zones and rules), "source": decl.get("_source"),
        "counts": counts, "rows": rows,
    }
