"""Профиль стиля кода дочки: фрагменты линтеров ПОВЕРХ её конфига, собранные из объявлений (#1183).

ПОВОД (замер на ии-среде, 2026-09-29). ESLint дочки зелёный, а проба ASCII-правила `id-match` нашла
~6300 имён не латиницей (1188 разных, 148 файлов). Границы слоёв держались только у двух нижних слоёв
из пяти — 57 пересечений в остальных. Стиль держался на внимательности людей и агентов, а внимательности
не хватает. РЕШЕНИЕ ВЛАДЕЛЬЦА: имена латиницей — умолчание для всех дочек; отказ — только явной строкой
`.ai-ops.yaml -> standard.identifiers: any`.

ЧТО ДЕЛАЕТ МОДУЛЬ. Собирает для каждого стека дочки ФРАГМЕНТ, который ложится поверх её собственной
настройки линтера и никогда её не заменяет:
  * JS/TS — ESLint flat-config (`lint_profile_js`): `id-match` латиницей, `naming-convention` для типов
    (если стоит typescript-eslint) и границы слоёв встроенным `no-restricted-imports`;
  * Python — Ruff (`PLC2401`, `PLC2403`, `N`) + контракты import-linter (ниже в этом модуле);
  * Go — golangci-lint v2 (`asciicheck`, `revive` var-naming, `depguard`);
  * прочие языки — правило ast-grep «в имени только ASCII».

ГРАНИЦЫ СЛОЁВ НЕ ОБЪЯВЛЯЮТСЯ ВТОРОЙ РАЗ. Источник — то же объявление зон и запретов, что читает
`planning.architecture_invariants` (architecture-invariants.yaml или standard.architecture). Модуль
живёт в слое primitives и вверх не импортирует: объявление ему передаёт вызывающий (CLI) как данные.
Правило `forbid: {from: X, to: X}` (зона сама в себя) читается как запрет СОСЕДНИХ срезов внутри слоя —
так FSD запрещает фиче импортировать соседнюю фичу; проверка инвариантов такие рёбра не видит вовсе
(ребро внутри зоны она отбрасывает), поэтому второго смысла у объявления не появляется.

PYTHON. Ruff-фрагмент `extend` поверх конфига дочки и лишь ДОБАВЛЯЕТ правила: `PLC2401`/`PLC2403`
(имя и импортируемое имя не ASCII) и `N` (соглашения об именах PEP 8). `N` — про форму имени, не про
алфавит, поэтому остаётся и при `standard.identifiers: any`. Границы — контракты import-linter из того
же объявления зон: если запреты объявления в точности равны «нижний не знает о верхних» для одного
порядка, пишется один контракт `layers`; иначе — по контракту `forbidden` на запрет, чтобы не
запретить больше, чем объявлено. Запрет соседних срезов — контракт `independence`.

GO. golangci-lint v2 с `default: none`: только `asciicheck` (имена ASCII), `revive` var-naming и
`depguard` (границы: пакет-источник не импортирует пакет-цель, путь модуля из go.mod).

ПРОЧИЕ ЯЗЫКИ. Правило ast-grep «в имени нет символов вне ASCII» по видам узлов-имён грамматики.
Для языков со своим профилем (JS/TS/Python/Go) оно не пишется — один язык, одна проверка.

БАЗОВАЯ ЛИНИЯ — `lint_baseline`. Включение профиля не должно ронять main дочки: существующее
замораживается, новое краснеет, счётчик ходит только вниз.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
           Path(__file__).resolve().parents[2])
TEMPLATES = PKG / "templates" / "lint" / "profiles.yaml"

CONFIG_REL = ".ai-ops.yaml"
LINT_DIR = ".ai/project/lint"          # всё, что профиль пишет в дочке, кроме конфига ESLint
IDENTIFIERS_LATIN = "latin"
IDENTIFIERS_ANY = "any"

# Каталоги, которые не код продукта: поставка кита, зависимости, сборка.
SKIP_DIRS = {".git", "node_modules", "dist", "build", ".next", "out", "coverage", "__pycache__",
             ".venv", "venv", "vendor", ".ai", ".claude", "storybook-static", "target"}


# ── Настройка дочки. ──────────────────────────────────────────────────────────────────────────
def identifiers_policy(child_root) -> dict:
    """`standard.identifiers` из `.ai-ops.yaml`: latin (умолчание) | any (явный отказ).

    Ключа нет -> latin: решение владельца — латиница по умолчанию для всех дочек. Незнакомое значение
    не выключает правило, а остаётся latin с пометкой: опечатка не должна тихо снимать проверку.
    """
    raw = None
    cfg = Path(child_root) / CONFIG_REL
    if cfg.is_file():
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            std = data.get("standard") if isinstance(data, dict) else None
            raw = std.get("identifiers") if isinstance(std, dict) else None
        except (OSError, yaml.YAMLError):
            raw = None
    if raw is None:
        return {"value": IDENTIFIERS_LATIN, "source": "default", "note": None}
    value = str(raw).strip().lower()
    if value in (IDENTIFIERS_LATIN, IDENTIFIERS_ANY):
        return {"value": value, "source": CONFIG_REL, "note": None}
    return {"value": IDENTIFIERS_LATIN, "source": CONFIG_REL,
            "note": f"standard.identifiers: {raw!r} — незнакомое значение, оставлена латиница "
                    f"(допустимо: {IDENTIFIERS_LATIN} | {IDENTIFIERS_ANY})"}


# ── Объявление архитектуры -> каталоги зон и запреты. ─────────────────────────────────────────
def zone_dirs(globs) -> list:
    """Глобы зоны -> корневые каталоги (часть пути до первого сегмента с `*?[{`).

    `src/features/**` -> `src/features`. Глоб без корня (`**/api/**`) каталога не даёт: точную
    границу по нему не построить, и зона честно пропускается с пометкой, а не угадывается.
    """
    out = []
    for g in globs or []:
        segs = []
        for seg in str(g).replace("\\", "/").strip("/").split("/"):
            if any(ch in seg for ch in "*?[{"):
                break
            segs.append(seg)
        d = "/".join(s for s in segs if s not in ("", "."))
        if d and d not in out:
            out.append(d)
    return out


def zones(decl: dict) -> dict:
    """{зона: [каталоги]} из объявления (только зоны, у которых каталог выводится)."""
    raw = decl.get("zones") if isinstance(decl, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {str(k): zone_dirs(v if isinstance(v, list) else [v]) for k, v in raw.items()}


def forbidden(decl: dict) -> tuple:
    """Запреты объявления -> ({from: {to: reason}}, {zone: reason} запрет соседних срезов).

    Открытая сторона правила (нет `from` или `to`) раскрывается в «любая другая зона» — так же, как
    её читает `architecture_invariants.check`. Правило зоны самой в себя — запрет соседних срезов.
    """
    names = list(zones(decl))
    targets: dict = {}
    siblings: dict = {}
    for r in (decl.get("rules") or []) if isinstance(decl, dict) else []:
        if not isinstance(r, dict):
            continue
        fb = r.get("forbid") or {}
        fz, tz = fb.get("from"), fb.get("to")
        if not fz and not tz:
            continue
        reason = str(r.get("reason") or "запрещённое ребро зон").strip()
        if fz and fz == tz:
            siblings[str(fz)] = reason
            continue
        for f in ([fz] if fz else [n for n in names if n != tz]):
            for t in ([tz] if tz else [n for n in names if n != f]):
                if f in names and t in names and f != t:
                    targets.setdefault(str(f), {}).setdefault(str(t), reason)
    return targets, siblings


def zone_exceptions(decl: dict) -> tuple:
    """`exceptions` объявления -> ({(файл, зона)}, {(зона, зона)}): точечно снятые рёбра."""
    files, pairs = set(), set()
    names = set(zones(decl))
    for item in (decl.get("exceptions") or []) if isinstance(decl, dict) else []:
        parts = [p.strip() for p in str(item).split(" -> ")]
        if len(parts) != 2:
            continue
        (pairs if parts[0] in names else files).add((parts[0], parts[1]))
    return files, pairs


# ── Обход исходников. ─────────────────────────────────────────────────────────────────────────
def source_files(root, rel_dir: str, exts) -> list:
    """Репо-относительные пути исходников с расширениями `exts` под `rel_dir` (без служебных)."""
    base = Path(root) / rel_dir
    if not base.is_dir():
        return []
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in sorted(filenames):
            if fn.rsplit(".", 1)[-1] in exts:
                out.append(Path(dirpath, fn).relative_to(root).as_posix())
    return out


def extensions_present(root) -> set:
    """Расширения исходников, которые есть в дочке (один обход, служебные каталоги пропущены)."""
    found = set()
    for _dirpath, dirnames, filenames in os.walk(Path(root)):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        found.update(fn.rsplit(".", 1)[-1] for fn in filenames if "." in fn)
    return found


def has_sources(root, exts) -> bool:
    """Есть ли в дочке хотя бы один исходник с этими расширениями (вне служебных каталогов)."""
    return bool(set(exts) & extensions_present(root))


def read_template(name: str) -> str:
    """Шаблон фрагмента по ключу из `templates/lint/profiles.yaml` (один файл поставки на все)."""
    data = yaml.safe_load(TEMPLATES.read_text(encoding="utf-8")) or {}
    return data["templates"][name]


def fill(template: str, **values) -> str:
    """`{{KEY}}` -> значение. Не string.Template: в шаблонах живут `$` регулярок и JS."""
    for k, v in values.items():
        template = template.replace("{{" + k + "}}", v)
    return template


# ── Сборка профиля целиком. ───────────────────────────────────────────────────────────────────
def render(child_root, decl: dict | None) -> dict:
    """Профиль дочки: какие стеки найдены, какие файлы будут записаны, что пропущено и почему.

    -> {"identifiers": {...}, "stacks": [...], "files": {rel: text}, "tools": [...], "notes": [...]}.
    Ничего не пишет: запись — `apply()`. `decl` — объявление архитектуры (может быть пустым: тогда
    строятся только правила имён, а граница слоёв честно названа необъявленной).
    """
    from ai_ops_kit.checks import lint_profile_js
    root = Path(child_root)
    decl = decl if isinstance(decl, dict) else {}
    ident = identifiers_policy(root)
    latin = ident["value"] == IDENTIFIERS_LATIN
    out = {"identifiers": ident, "stacks": [], "files": {}, "tools": [], "notes": [],
           "architecture_declared": bool(zones(decl) and (decl.get("rules") or []))}
    if ident["note"]:
        out["notes"].append(ident["note"])
    if not out["architecture_declared"]:
        out["notes"].append("архитектура (зоны и запреты) не объявлена — границы слоёв не строятся; "
                            "объявление: architecture-invariants.yaml или .ai-ops.yaml -> "
                            "standard.architecture")
    for part in (lint_profile_js.render(root, decl, latin),
                 *_render_stacks(root, decl, latin)):
        if not part:
            continue
        out["stacks"].append(part["stack"])
        out["files"].update(part["files"])
        out["tools"].extend(part["tools"])
        out["notes"].extend(part.get("notes") or [])
    return out


def drift(child_root, profile: dict) -> list:
    """Файлы профиля, которые на диске расходятся со свежей сборкой (или отсутствуют).

    Появился новый срез или слой — сборка изменилась, а записанный профиль его ещё не видит.
    Это «проверено не всё», и вызывающий обязан это сказать, а не выдать проверку за полную.
    """
    root = Path(child_root)
    stale = []
    for rel, text in sorted(profile.get("files", {}).items()):
        p = root / rel
        try:
            if not p.is_file() or p.read_text(encoding="utf-8") != text:
                stale.append(rel)
        except OSError:
            stale.append(rel)
    return stale


def apply(child_root, profile: dict) -> list:
    """Записать файлы профиля в дочку. -> список записанных путей (только изменившиеся)."""
    root = Path(child_root)
    written = []
    for rel in drift(root, profile):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(profile["files"][rel], encoding="utf-8")
        written.append(rel)
    return written


RUFF_OUT = f"{LINT_DIR}/ruff.toml"
IMPORTLINTER_OUT = f"{LINT_DIR}/importlinter.ini"
GOLANGCI_OUT = f"{LINT_DIR}/golangci.yml"
ASTGREP_OUT = f"{LINT_DIR}/ast-grep-identifiers.yml"
RUFF_IDENTIFIER_RULES = ("PLC2401", "PLC2403")
RUFF_STYLE_RULES = ("N",)
_PY_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")
_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# расширение -> (язык ast-grep, виды узлов-имён). Для неизвестной грамматики правило не пишется.
AST_GREP_LANGS = {
    "rs": ("rust", ("identifier", "type_identifier", "field_identifier")),
    "java": ("java", ("identifier",)),
    "kt": ("kotlin", ("simple_identifier",)),
    "cs": ("csharp", ("identifier",)),
    "rb": ("ruby", ("identifier", "constant")),
    "swift": ("swift", ("simple_identifier",)),
    "php": ("php", ("name",)),
    "c": ("c", ("identifier", "type_identifier", "field_identifier")),
    "cpp": ("cpp", ("identifier", "type_identifier", "field_identifier")),
    "scala": ("scala", ("identifier",)),
    "lua": ("lua", ("identifier",)),
    "dart": ("dart", ("identifier",)),
}


# ── Python. ───────────────────────────────────────────────────────────────────────────────────
def _ruff_base(root: Path):
    for name in ("ruff.toml", ".ruff.toml"):
        if (root / name).is_file():
            return name
    pp = root / "pyproject.toml"
    try:
        if pp.is_file() and "[tool.ruff" in pp.read_text(encoding="utf-8"):
            return "pyproject.toml"
    except OSError:
        return None
    return None


def _module_of(zone_dir: str):
    """Каталог зоны -> модуль Python (`src/app/domain` -> `app.domain`); не модуль -> None."""
    parts = zone_dir.split("/")
    if parts and parts[0] == "src" and len(parts) > 1:
        parts = parts[1:]
    return ".".join(parts) if parts and all(_MODULE.match(p) for p in parts) else None


def _layer_order(pairs: set):
    """Порядок слоёв сверху вниз, если запреты в ТОЧНОСТИ равны «нижний не знает о верхних».

    pairs: {(нижний, верхний)} — «нижнему запрещён верхний». -> список сверху вниз или None.
    """
    nodes = sorted({z for p in pairs for z in p})
    above = {n: {h for lo, h in pairs if lo == n} for n in nodes}
    order = sorted(nodes, key=lambda n: len(above[n]))          # у верхнего нет верхних
    closure = {(order[j], order[i]) for i in range(len(order)) for j in range(i + 1, len(order))}
    return order if closure == pairs else None


def _import_contracts(root: Path, decl: dict) -> tuple:
    zmod = {}
    for z, dirs in zones(decl).items():
        mods = [m for d in dirs if source_files(root, d, ("py",)) for m in [_module_of(d)] if m]
        if mods:
            zmod[z] = mods[0]
    targets, siblings = forbidden(decl)
    pairs = {(f, t) for f, ts in targets.items() for t in ts if f in zmod and t in zmod}
    blocks = []
    order = _layer_order(pairs) if pairs else None
    if order:
        blocks.append("[importlinter:contract:ai-ops-layers]\nname = ai-ops: слои\ntype = layers\n"
                      "layers =\n" + "\n".join(f"    {zmod[z]}" for z in order))
    else:
        for i, (f, t) in enumerate(sorted(pairs)):
            blocks.append(f"[importlinter:contract:ai-ops-forbid-{i}]\nname = ai-ops: {f} -> {t}\n"
                          f"type = forbidden\nsource_modules =\n    {zmod[f]}\n"
                          f"forbidden_modules =\n    {zmod[t]}")
    for z in sorted(s for s in siblings if s in zmod):
        base = root / zones(decl)[z][0]
        subs = sorted(p.name for p in base.iterdir() if p.is_dir() and _MODULE.match(p.name)
                      and p.name not in SKIP_DIRS)
        if len(subs) > 1:
            blocks.append(f"[importlinter:contract:ai-ops-independent-{z}]\nname = ai-ops: срезы {z}\n"
                          "type = independence\nmodules =\n"
                          + "\n".join(f"    {zmod[z]}.{s}" for s in subs))
    roots = sorted({m.split(".")[0] for m in zmod.values()})
    return roots, blocks


def render_python(child_root, decl: dict, latin: bool) -> dict | None:
    root = Path(child_root)
    if not (any((root / m).is_file() for m in _PY_MARKERS) or has_sources(root, ("py",))):
        return None
    base = _ruff_base(root)
    select = list(RUFF_IDENTIFIER_RULES if latin else ()) + list(RUFF_STYLE_RULES)
    ruff = fill(read_template("ruff"),
                   BASE_NOTE=(f"Ваш {base} подключается целиком (`extend`)." if base
                              else "Своего конфига Ruff у репозитория нет — профиль работает один."),
                   EXTEND=(f'extend = "../../../{base}"' if base else ""),
                   SELECT="[" + ", ".join(f'"{c}"' for c in select) + "]")
    files = {RUFF_OUT: ruff}
    tools = [{"tool": "ruff", "config": RUFF_OUT, "rules": select}]
    roots, blocks = _import_contracts(root, decl)
    if blocks:
        files[IMPORTLINTER_OUT] = fill(read_template("importlinter"),
                                          ROOTS="\n".join(f"    {r}" for r in roots),
                                          CONTRACTS="\n\n".join(blocks))
        tools.append({"tool": "import-linter", "config": IMPORTLINTER_OUT})
    return {"stack": "python", "files": files, "tools": tools, "notes": []}


# ── Go. ───────────────────────────────────────────────────────────────────────────────────────
def _go_module(root: Path):
    try:
        for line in (root / "go.mod").read_text(encoding="utf-8").splitlines():
            if line.startswith("module "):
                return line.split()[1].strip()
    except (OSError, IndexError):
        return None
    return None


def _depguard(root: Path, decl: dict, module: str) -> str:
    targets, _ = forbidden(decl)
    zd = zones(decl)
    rules = []
    for f, ts in sorted(targets.items()):
        denies = [(module + "/" + d, f"{f} -> {t}: {r}")
                  for t, r in sorted(ts.items()) for d in zd.get(t, [])]
        src = [d for d in zd.get(f, []) if source_files(root, d, ("go",))]
        if not denies or not src:
            continue
        rules.append(f"        ai-ops-{f}:\n          files:\n"
                     + "".join(f'            - "**/{d}/**/*.go"\n' for d in src)
                     + "          deny:\n"
                     + "".join(f'            - pkg: "{p}"\n              desc: "{m}"\n'
                               for p, m in denies))
    return ("    depguard:\n      rules:\n" + "".join(rules)) if rules else ""


def render_go(child_root, decl: dict, latin: bool) -> dict | None:
    root = Path(child_root)
    if not (root / "go.mod").is_file():
        return None
    module = _go_module(root) or ""
    dep = _depguard(root, decl, module) if module else ""
    linters = (["asciicheck"] if latin else []) + ["revive"] + (["depguard"] if dep else [])
    text = fill(read_template("golangci"),
                   ENABLE="\n".join(f"    - {x}" for x in linters), DEPGUARD=dep.rstrip("\n"))
    return {"stack": "go", "files": {GOLANGCI_OUT: text}, "notes": [],
            "tools": [{"tool": "golangci-lint", "config": GOLANGCI_OUT, "rules": linters}]}


# ── Прочие языки: ast-grep. ───────────────────────────────────────────────────────────────────
def render_fallback(child_root, latin: bool) -> dict | None:
    if not latin:
        return None
    root = Path(child_root)
    present = extensions_present(root)
    langs = [(ext, *AST_GREP_LANGS[ext]) for ext in sorted(AST_GREP_LANGS) if ext in present]
    if not langs:
        return None
    docs = []
    for ext, lang, kinds in langs:
        docs.append(f"id: ai-ops-latin-identifiers-{lang}\nlanguage: {lang}\nseverity: error\n"
                    f"message: имя не латиницей — переименуйте (решение владельца: имена латиницей)\n"
                    "rule:\n  any:\n"
                    + "".join(f"    - kind: {k}\n      regex: '[^\\x00-\\x7F]'\n" for k in kinds))
    text = fill(read_template("ast_grep"), RULES="---\n".join(docs))
    return {"stack": "other:" + ",".join(lang for _, lang, _ in langs), "files": {ASTGREP_OUT: text},
            "notes": [], "tools": [{"tool": "ast-grep", "config": ASTGREP_OUT}]}


# ── Записанный профиль: что принуждать, когда он включён (#1183). ─────────────────────────────
GO_LINTERS = ("asciicheck", "revive", "depguard")


def applied_tools(child_root) -> list:
    """Инструменты профиля, ЗАПИСАННОГО в дочке, — по файлам на диске, без пересборки.

    Этим читают профиль три точки принуждения: хук правки, шаг CI и доказательство `lint_passed`
    прогона кита. Пересборка им не подходит: ей нужно объявление архитектуры (слой planning, гейту
    недоступен), и принуждать надо то, что владелец СОХРАНИЛ в репозитории, а не то, что собралось
    бы сейчас (расхождение сборки с диском называет `lint-profile check`). Пустой список — профиль не
    включён: точки принуждения ведут себя как прежде.
    """
    from ai_ops_kit.checks import lint_profile_js as js
    root = Path(child_root)
    latin = identifiers_policy(root)["value"] == IDENTIFIERS_LATIN
    out: list[dict] = []
    if (root / js.OUT).is_file():
        out.append({"tool": "eslint", "config": js.OUT, "suppressions": js.SUPPRESSIONS,
                    "rule_prefix": "ai-ops/"})
    if (root / RUFF_OUT).is_file():
        out.append({"tool": "ruff", "config": RUFF_OUT,
                    "rules": list(RUFF_IDENTIFIER_RULES if latin else ()) + list(RUFF_STYLE_RULES)})
    if (root / IMPORTLINTER_OUT).is_file():
        out.append({"tool": "import-linter", "config": IMPORTLINTER_OUT})
    if (root / GOLANGCI_OUT).is_file():
        out.append({"tool": "golangci-lint", "config": GOLANGCI_OUT, "rules": list(GO_LINTERS)})
    if (root / ASTGREP_OUT).is_file():
        out.append({"tool": "ast-grep", "config": ASTGREP_OUT})
    return out


def tools_for_file(tools: list, rel: str) -> list:
    """Инструменты профиля, которые судят этот файл (по расширению)."""
    from ai_ops_kit.checks import lint_profile_js as js
    ext = rel.rsplit(".", 1)[-1] if "." in rel else ""
    exts = {"eslint": js.JS_EXT + js.TS_EXT, "ruff": ("py", "pyi"), "import-linter": ("py",),
            "golangci-lint": ("go",), "ast-grep": tuple(AST_GREP_LANGS)}
    return [t for t in tools if ext in exts.get(t["tool"], ())]


def _render_stacks(child_root, decl: dict, latin: bool) -> list:
    return [render_python(child_root, decl, latin), render_go(child_root, decl, latin),
            render_fallback(child_root, latin)]
