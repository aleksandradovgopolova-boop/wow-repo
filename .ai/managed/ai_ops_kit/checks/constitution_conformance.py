#!/usr/bin/env python3
"""Проверка соответствия кода репозитория Архитектурной конституции + рекомендации (#845).

ЗАЧЕМ. #820 доставил конституцию в дочку как машинный реестр, но проверки были parent-only: дочка
правила ПОЛУЧАЛА, но по ним себя не проверяла. У каждой дочки своя кодовая база и история — жёсткий
ратчет-блок чужого легаси неправилен. Поэтому здесь — **проверка соответствия → РЕКОМЕНДАЦИИ
владельцу** (advisory, не блок): «где код расходится со статьёй, что стоит рассмотреть».

УНИВЕРСАЛЬНОСТЬ. Сканируется код ЛЮБОЙ дочки (авто-корень, не хардкод `ai_ops_kit/`). Проверки —
переносимые эвристики уровня кода (длинная функция, глубокая вложенность, структурный дубль, крупный
модуль). Кит-специфичные статьи (слои→DAG, built≠wired завязаны на устройство кита) НЕ входят.

СВЯЗЬ С РЕЕСТРОМ. Находка цитирует стабильный `article_id`, а заголовок/уровень берутся из
ДОСТАВЛЕННОГО `standards/architecture/rules.yaml` (в дочке — `.ai/managed/...`). Статьи нет в
доставленной версии — находки по ней не выдаются (версия конституции дочки — источник истины).

Порог — не ратчет, а разумный дефолт для РЕКОМЕНДАЦИИ. Read-only: ничего не пишет.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

# Разумные дефолты для advisory-рекомендаций (НЕ per-repo ратчет — просто «стоит присмотреться»).
LONG_FUNCTION_LINES = 60
DEEP_NESTING = 5
LONG_MODULE_LINES = 500
DUP_MIN_STMTS = 10

# Каталоги, которые не считаются исходным кодом продукта.
_SKIP_DIRS = {".git", ".ai", ".ai-ops", "node_modules", "venv", ".venv", "__pycache__",
              "build", "dist", ".mypy_cache", ".pytest_cache", "tests", "test"}
_SKIP_FUNC_NAMES = {"main", "__init__", "__repr__", "__eq__", "__hash__", "__str__"}


def load_rules(rules_path: Path) -> dict:
    """{article_id: {title, level, severity}} из доставленного rules.yaml. Пусто, если файла нет."""
    p = Path(rules_path)
    if not p.is_file():
        return {}
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out = {}
    for r in doc.get("rules") or []:
        out[r["id"]] = {"title": r.get("title", ""), "level": r.get("level", ""),
                        "severity": r.get("severity", "medium")}
    return out


# Каталог конституций кита: у каждой свой реестр `standards/<area>/rules.yaml` (тот же путь едет в
# дочку под `.ai/managed`). Продуктовая стоит наравне с арх и UI/UX — механизм резолвит статьи любой.
CONSTITUTIONS = ("architecture", "uiux", "product")


def registry_path(root: Path, area: str) -> Path:
    """Реестр конституции `area`: в дочке `.ai/managed/...`, иначе — корневой `standards/...`."""
    managed = Path(root) / ".ai" / "managed" / "standards" / area / "rules.yaml"
    return managed if managed.is_file() else Path(root) / "standards" / area / "rules.yaml"


def default_rules_path(root: Path) -> Path:
    """Реестр Архитектурной конституции (обратная совместимость)."""
    return registry_path(root, "architecture")


def constitution_registries(root: Path) -> dict[str, Path]:
    """{area: path} по реально присутствующим реестрам — отсутствующий в набор не попадает."""
    return {area: p for area in CONSTITUTIONS if (p := registry_path(root, area)).is_file()}


def load_all_rules(root: Path) -> dict:
    """Слить статьи всех доступных конституций в один {id: meta}. Префиксы ID не пересекаются
    (ARCH/CODE/HON/SEC/DATA · UI-* · PROD-*), поэтому конформанс резолвит любую отсюда."""
    merged: dict = {}
    for p in constitution_registries(root).values():
        merged.update(load_rules(p))
    return merged


# ── локальные правила ДОЧКИ из её собственных уроков (#849) ──────────────────────
# Живут в protected-зоне (`.ai/project/**`) — кит их при update НЕ затирает и не навязывает другим
# дочкам. Это advisory-напоминания: выстраданное правило проекта, которое conformance-отчёт держит
# на виду рядом с находками. Пополняются из уроков дочки (руками владельца или помощником ниже).
LOCAL_RULES_REL = ".ai/project/architecture-rules.local.yaml"


def local_rules_path(root: Path) -> Path:
    return Path(root) / LOCAL_RULES_REL


def load_local_rules(root: Path) -> list[dict]:
    """Локальные правила дочки. -> [{id, title, recommendation, lesson?}]. Пусто, если файла нет."""
    p = local_rules_path(root)
    if not p.is_file():
        return []
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return []
    out = []
    for r in doc.get("rules") or []:
        if isinstance(r, dict) and r.get("id") and r.get("title"):
            out.append({"id": r["id"], "title": r["title"],
                        "recommendation": r.get("recommendation", ""), "lesson": r.get("lesson", "")})
    return out


def add_local_rule(root: Path, title: str, recommendation: str = "", lesson: str = "",
                   rule_id: str | None = None) -> str:
    """Добавить локальное правило дочки (из усвоенного урока). -> id правила. Идемпотентен по title.

    Это МЕХАНИЗМ, которым урок дочки становится правилом: добавляет запись в protected-файл, не трогая
    доставленную конституцию. Существующее правило с тем же title не дублируется.
    """
    p = local_rules_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {}
    if p.is_file():
        try:
            doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            doc = {}
    rules = doc.get("rules") or []
    for r in rules:
        if isinstance(r, dict) and r.get("title") == title:
            return r.get("id", "")                       # уже есть — не дублируем
    rid = rule_id or f"LOCAL-{len(rules) + 1:03d}"
    rules.append({"id": rid, "title": title, "recommendation": recommendation, "lesson": lesson})
    doc.update({"schema_version": 1, "kind": "architecture-rules-local", "rules": rules})
    p.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return rid


def iter_source_files(root: Path):
    """.py-файлы продукта дочки (без .git/.ai/venv/tests/…)."""
    root = Path(root)
    for p in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        yield p


def _parse(p: Path):
    try:
        return ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
    except (SyntaxError, OSError):
        return None


def _rel(p: Path, root: Path) -> str:
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return p.name


def _max_depth(node: ast.AST, _depth: int = 0) -> int:
    """Глубина вложенности управляющих конструкций внутри функции."""
    nesting = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)
    best = _depth
    for child in ast.iter_child_nodes(node):
        d = _depth + 1 if isinstance(child, nesting) else _depth
        best = max(best, _max_depth(child, d))
    return best


def _dup_signature(fn: ast.AST) -> tuple:
    return tuple(type(n).__name__ for n in ast.walk(fn))


def _files(root: Path, files=None):
    """Файлы для проверки: явный список (ревью) или весь исходник дочки (онбординг)."""
    return list(files) if files is not None else list(iter_source_files(root))


def _functions(root: Path, files=None):
    for p in _files(root, files):
        tree = _parse(p)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield p, node


# ── эвристики: article_id -> находки. files=None => всё дерево (онбординг); список => ревью по diff ─

def _long_functions(root, files=None):
    hits = []
    for p, fn in _functions(root, files):
        end = getattr(fn, "end_lineno", None)
        if end and (end - fn.lineno + 1) > LONG_FUNCTION_LINES:
            hits.append({"where": f"{_rel(p, root)}:{fn.lineno}:{fn.name}",
                         "detail": f"{end - fn.lineno + 1} строк"})
    return hits


def _deep_nesting(root, files=None):
    hits = []
    for p, fn in _functions(root, files):
        d = _max_depth(fn)
        if d >= DEEP_NESTING:
            hits.append({"where": f"{_rel(p, root)}:{fn.lineno}:{fn.name}",
                         "detail": f"вложенность {d}"})
    return hits


def _long_modules(root, files=None):
    hits = []
    for p in _files(root, files):
        try:
            n = len(p.read_text(encoding="utf-8").splitlines())
        except OSError:
            continue
        if n > LONG_MODULE_LINES:
            hits.append({"where": _rel(p, root), "detail": f"{n} строк"})
    return hits


def _duplicates(root, files=None):
    by_sig: dict[tuple, list[str]] = {}
    for p, fn in _functions(root, files):
        if fn.name in _SKIP_FUNC_NAMES:
            continue
        if sum(1 for n in ast.walk(fn) if isinstance(n, ast.stmt)) < DUP_MIN_STMTS:
            continue
        by_sig.setdefault(_dup_signature(fn), []).append(f"{_rel(p, root)}:{fn.lineno}:{fn.name}")
    hits = []
    for members in by_sig.values():
        uniq = sorted(set(members))
        if len(uniq) > 1:
            hits.append({"where": uniq[0], "detail": "дубль: " + ", ".join(uniq[1:])})
    return hits


# ── продуктовые эвристики: читают АРТЕФАКТЫ дочки (не .py), советуют по Продуктовой конституции ────
# Инвариант честности: флагуем только то, что реально ВИДИМ. Артефакта нет — молчим (unknown ≠
# нарушение). Детерминированно, без сети и модели. Всё — advisory-рекомендации, как CODE-*.

_PLACEHOLDER_MARKERS = ("todo", "tbd", "xxx", "<", "???", "n/a")


def _is_blank_or_placeholder(val) -> bool:
    """Пусто/отсутствует/плейсхолдер — значит секция не заполнена по-настоящему."""
    if not isinstance(val, str):
        return True                                      # None или не-строка = не заполнено
    s = val.strip()
    if not s:
        return True
    low = s.lower()
    return any(m in low for m in _PLACEHOLDER_MARKERS)


def _read_yaml(p: Path) -> dict:
    """Безопасно прочитать yaml-артефакт. Битый/нечитаемый файл -> {} (молчим, не падаем)."""
    try:
        doc = yaml.safe_load(Path(p).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _feature_registry(root: Path) -> Path | None:
    """Реестр фич дочки `registry/features.yaml`. Нет файла -> None (эвристика молчит)."""
    p = Path(root) / "registry" / "features.yaml"
    return p if p.is_file() else None


def _feature_dirs(root: Path):
    """Каталоги фич `features/<id>/` в корне дочки (examples/ и служебное не сканируем)."""
    base = Path(root) / "features"
    if not base.is_dir():
        return
    for d in sorted(base.iterdir()):
        if d.is_dir() and d.name not in _SKIP_DIRS:
            yield d


def _spec_markdowns(feat_dir: Path) -> list[Path]:
    """Markdown-спеки фичи: discovery/*.md, prd/*.md, definition/prd/*.md."""
    out: list[Path] = []
    for sub in ("discovery", "prd", "definition/prd"):
        p = feat_dir / sub
        if p.is_dir():
            out.extend(sorted(p.glob("*.md")))
    return out


_OUT_OF_SCOPE_RE = re.compile(r"(?mi)^#{1,6}\s+out of scope\s*$")
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+")


def _has_non_empty_out_of_scope(text: str) -> bool:
    """Есть заголовок `## Out of scope` И под ним непустое тело до следующего заголовка."""
    m = _OUT_OF_SCOPE_RE.search(text)
    if not m:
        return False
    for line in text[m.end():].splitlines():
        if _MD_HEADING_RE.match(line):
            break
        if line.strip():
            return True
    return False


def _features_missing_audience(root, files=None):
    """PROD-002: у фичи в реестре не назван who (для кого) или what (задача, JTBD)."""
    reg = _feature_registry(root)
    if reg is None:
        return []                                        # реестра нет — молчим
    hits = []
    for feat in _read_yaml(reg).get("features") or []:
        if not isinstance(feat, dict):
            continue
        where = feat.get("id") or feat.get("name") or "(фича без id)"
        desc = feat.get("description")
        desc = desc if isinstance(desc, dict) else {}
        missing = []
        if _is_blank_or_placeholder(desc.get("who")):
            missing.append("не назван who (для кого)")
        if _is_blank_or_placeholder(desc.get("what")):
            missing.append("не названа задача (what)")
        if missing:
            hits.append({"where": str(where), "detail": "; ".join(missing)})
    return hits


def _specs_missing_non_goals(root, files=None):
    """PROD-008: у фичи-спеки нет непустой секции `## Out of scope` ни в одном её markdown."""
    hits = []
    for d in _feature_dirs(root):
        mds = _spec_markdowns(d)
        if not mds:
            continue                                     # это не спека — молчим по ней
        try:
            declared = any(_has_non_empty_out_of_scope(md.read_text(encoding="utf-8"))
                           for md in mds)
        except OSError:
            declared = False
        if not declared:
            hits.append({"where": d.name, "detail": "non-goals не объявлены (## Out of scope)"})
    return hits


def _has_measured_readout(feat_dir: Path, blueprint: dict) -> bool:
    """Рядом с фичей есть измеренный исход: валидный PRR с измеренным health ИЛИ артефакт
    стадии retrospective/monitoring в blueprint.artifacts."""
    for prr in sorted(feat_dir.rglob("PRR-*.yaml")):
        doc = _read_yaml(prr)
        if doc.get("kind") == "PostReleaseReadout":
            band = (doc.get("product_health") or {}).get("band")
            if band and band != "not_measured":
                return True
    artifacts = blueprint.get("artifacts")
    if isinstance(artifacts, dict):
        for stage in ("retrospective", "monitoring"):
            items = artifacts.get(stage)
            if isinstance(items, list) and items:
                return True
    return False


def _released_features_without_readout(root, files=None):
    """PROD-010: у выпущенной (status: released) фичи нет измеренного исхода рядом."""
    hits = []
    for d in _feature_dirs(root):
        bp = d / "blueprint.yaml"
        if not bp.is_file():
            continue
        blueprint = _read_yaml(bp)
        feat = blueprint.get("feature")
        feat = feat if isinstance(feat, dict) else {}
        if feat.get("status") != "released":
            continue                                     # не выпущена — молчим (не «released без readout»)
        if _has_measured_readout(d, blueprint):
            continue
        hits.append({"where": str(feat.get("id") or d.name),
                     "detail": "выпущена без измеренного исхода"})
    return hits


# article_id -> (эвристика, шаблон рекомендации владельцу)
_HEURISTICS = {
    "CODE-001": (_long_functions,
                 "Длинные функции трудно читать и тестировать. Рассмотрите разбиение на меньшие "
                 "односмысловые функции."),
    "CODE-002": (_deep_nesting,
                 "Глубокая вложенность прячет ветки и краевые случаи. Уплощите ранними возвратами "
                 "и guard-clause, вынесите ветки в именованные хелперы."),
    "CODE-003": (_duplicates,
                 "Скопированная логика расходится при правках. Выделите общую функцию вместо копипасты."),
    "ARCH-006": (_long_modules,
                 "Крупный модуль обычно владеет слишком многим (нарушен SRP). Рассмотрите разрез по "
                 "ответственности на меньшие связные модули."),
    "PROD-002": (_features_missing_audience,
                 "Без «для кого» и какой задачи (JTBD) о ценности фичи судить нельзя. Назовите роль "
                 "пользователя и задачу, которую фича ему решает."),
    "PROD-008": (_specs_missing_non_goals,
                 "Границы честнее, когда явно сказано, чего мы НЕ делаем. Добавьте в спеку фичи "
                 "секцию «Out of scope» — что осознанно вне охвата."),
    "PROD-010": (_released_features_without_readout,
                 "Выпустили — измерьте исход. Добавьте post-release readout (или ретроспективу): "
                 "без замера не узнать, окупилась ли ставка."),
}


def conform(root: Path, rules_path: Path | None = None) -> list[dict]:
    """Проверить соответствие кода дочки конституции. -> список находок с рекомендациями.

    Находка: {article_id, title, level, severity, count, locations, recommendation}. Только по
    статьям, присутствующим в ДОСТАВЛЕННОМ реестре (версия конституции дочки — источник истины).
    """
    root = Path(root)
    rules = load_rules(rules_path) if rules_path else load_all_rules(root)
    findings = []
    for article_id, (fn, advice) in _HEURISTICS.items():
        meta = rules.get(article_id)
        if meta is None:
            continue                                    # статьи нет в доставленной версии — молчим честно
        hits = fn(root)
        if not hits:
            continue
        findings.append({
            "article_id": article_id,
            "title": meta["title"],
            "level": meta["level"],
            "severity": meta["severity"],
            "count": len(hits),
            "locations": [h["where"] for h in hits],
            "details": hits,
            "recommendation": advice,
        })
    # серьёзные статьи выше
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f["article_id"]))
    return findings


# Пофайловые статьи (атрибутируются одному файлу) — годятся для ревью по diff. Дубли — кросс-файловые
# (нужно всё дерево), поэтому в ревью по изменённым файлам не входят: их место в онбординг-скане.
_PER_FILE_ARTICLES = ("CODE-001", "CODE-002", "ARCH-006")


def conform_paths(root: Path, rel_paths, rules_path: Path | None = None) -> list[dict]:
    """Соответствие для КОНКРЕТНЫХ файлов (ревью по diff). Только пофайловые эвристики.

    `rel_paths` — пути относительно `root` (напр. изменённые файлы ветки). Возвращает те же находки,
    что `conform`, но по подмножеству файлов и без кросс-файловых дублей.
    """
    root = Path(root)
    wanted = {str(p) for p in rel_paths if str(p).endswith(".py")}
    if not wanted:
        return []
    rules = load_rules(rules_path) if rules_path else load_all_rules(root)
    # ограничиваем обход целевыми файлами
    targets = [root / rp for rp in wanted if (root / rp).is_file()]
    findings = []
    for article_id in _PER_FILE_ARTICLES:
        meta = rules.get(article_id)
        if meta is None:
            continue
        fn, advice = _HEURISTICS[article_id]
        hits = fn(root, targets)
        if not hits:
            continue
        findings.append({
            "article_id": article_id, "title": meta["title"], "level": meta["level"],
            "severity": meta["severity"], "count": len(hits),
            "locations": [h["where"] for h in hits], "details": hits, "recommendation": advice,
        })
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f["article_id"]))
    return findings


def summary(findings: list[dict]) -> str:
    """Одна строка-итог для владельца."""
    if not findings:
        return "Код соответствует автоматизируемым статьям конституции — расхождений не найдено."
    n = sum(f["count"] for f in findings)
    return (f"Нашлось {n} расхождений с конституцией по {len(findings)} статьям — "
            f"это рекомендации к рассмотрению, не блок.")


_MAX_LOCATIONS = 10


def render_report(findings: list[dict], scope: str = "весь код репозитория",
                  local: list[dict] | None = None) -> str:
    """Owner-facing отчёт соответствия (Markdown, продуктовым языком). Рекомендации, не приговор.

    `local` — локальные правила дочки (#849): выводятся отдельной секцией как напоминания из её
    собственных уроков (кит их не проверяет автоматически, но держит на виду).
    """
    lines = [
        "# Соответствие Архитектурной конституции",
        "",
        f"> {summary(findings)}",
        "",
        f"Проверено: {scope}. Это **рекомендации** — что стоит рассмотреть, а не блок на мерже. "
        "У каждого пункта — статья конституции, к которой он относится.",
        "",
    ]
    if not findings:
        lines.append("Расхождений по автоматизируемым статьям не найдено. 👍")
    for f in findings:
        lines.append(f"## {f['article_id']} · {f['title']} ({f['count']})")
        lines.append("")
        lines.append(f"**Рекомендация.** {f['recommendation']}")
        lines.append("")
        lines.append("Где посмотреть:")
        for loc in f["locations"][:_MAX_LOCATIONS]:
            lines.append(f"- `{loc}`")
        if f["count"] > _MAX_LOCATIONS:
            lines.append(f"- …ещё {f['count'] - _MAX_LOCATIONS}")
        lines.append("")
    if local:
        lines.append("## Локальные правила проекта (из ваших уроков)")
        lines.append("")
        lines.append("Эти правила добавил сам проект — кит их не проверяет автоматически, но держит "
                     "на виду при онбординге и ревью.")
        lines.append("")
        for r in local:
            lines.append(f"- **{r['id']} · {r['title']}**"
                         + (f" — {r['recommendation']}" if r.get("recommendation") else ""))
            if r.get("lesson"):
                lines.append(f"  - _урок:_ {r['lesson']}")
        lines.append("")
    return "\n".join(lines) + "\n"
