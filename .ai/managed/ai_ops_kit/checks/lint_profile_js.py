"""ESLint-фрагмент профиля стиля (JS/TS) — спутник `lint_profile` (#1183).

ПОЧЕМУ ПРАВИЛА ПОД СВОИМ ИМЕНЕМ `ai-ops/…`. ESLint не складывает настройки одного правила из двух
объектов конфига — последний ЗАМЕНЯЕТ предыдущий целиком. У ии-среды уже есть свой
`no-restricted-imports` для двух нижних слоёв (и точечное «off» для пяти тестов): фрагмент под тем же
именем либо затёр бы её правило, либо был бы затёрт им. Поэтому встроенные правила ESLint
регистрируются вторым именем (`builtinRules` из `eslint/use-at-your-own-risk`) — пакетов не
добавляется, и оба набора работают независимо.

ИМЕНА ЛАТИНИЦЕЙ. `id-match` с шаблоном `^[A-Za-z_$][A-Za-z0-9_$]*$`, `properties: true`,
`onlyDeclarations: true`: считается ОБЪЯВЛЕНИЕ имени (переменная, функция, параметр, импорт, ключ
объекта без кавычек, поле класса), а не каждое его употребление — рост счётчика значит «появилось
новое имя», а не «старое имя употребили ещё раз». Ключ В КАВЫЧКАХ (`{'активная': 1}`) правило не
видит по построению (это Literal, не Identifier) — и это решение, а не пропуск: строковый ключ —
данные (поле API, подпись), а не имя в коде. Типы TypeScript `id-match` при `onlyDeclarations` не
видит, их закрывает `naming-convention` typescript-eslint (только если он у дочки стоит).

ГРАНИЦЫ СЛОЁВ — ТОЧНЫМ ПРЕФИКСОМ, А НЕ `**/features/**`. Встроенный `no-restricted-imports` сверяет
СТРОКУ импорта, путь он не разрешает. Глоб `**/app/**` ловил бы и `firebase/app`, а соседнюю фичу,
импортированную как `../material-management/model`, не ловил бы вовсе — в строке нет имени слоя.
Поэтому объект конфига строится на каждую ГЛУБИНУ файла внутри слоя (и на каждый срез, если объявлен
запрет соседей): с известной глубины относительный путь до чужого слоя однозначен (`../../app`), и
регулярка проверяет именно его плюс объявленный алиас (`@/app`). Импорт через barrel/ре-экспорт и
неописанный алиас остаются пропуском, не ложной тревогой (та же граница, что у
`architecture_invariants`, §5.2).
"""
from __future__ import annotations

import json
import posixpath
from pathlib import Path

from ai_ops_kit.checks import lint_profile as lp

OUT = "eslint.ai-ops.config.mjs"
SUPPRESSIONS = f"{lp.LINT_DIR}/eslint-suppressions.json"
TEMPLATE = "eslint"
LATIN = "^[A-Za-z_$][A-Za-z0-9_$]*$"
JS_EXT = ("js", "jsx", "mjs", "cjs")
TS_EXT = ("ts", "tsx", "mts", "cts")
BASE_CONFIGS = ("eslint.config.js", "eslint.config.mjs", "eslint.config.cjs",
                "eslint.config.ts", "eslint.config.mts", "eslint.config.cts")
# Откуда берётся naming-convention: мета-пакет typescript-eslint или сам плагин.
_TS_SOURCES = (
    ("typescript-eslint", 'import tseslint from "typescript-eslint";',
     'tseslint.plugin.rules["naming-convention"]'),
    ("@typescript-eslint/eslint-plugin", 'import tsPlugin from "@typescript-eslint/eslint-plugin";',
     'tsPlugin.rules["naming-convention"]'),
)
_RE_SPECIAL = set("^$\\.*+?()[]{}|")


def detect(child_root) -> dict | None:
    """JS-стек дочки: есть ли package.json, её конфиг ESLint и typescript-eslint. None — не JS."""
    root = Path(child_root)
    pj = root / "package.json"
    if not pj.is_file():
        return None
    try:
        data = json.loads(pj.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
    ts = next((s for s in _TS_SOURCES if s[0] in deps), None)
    return {"eslint": "eslint" in deps, "typescript": ts,
            "base": next((n for n in BASE_CONFIGS if (root / n).is_file()), None)}


def _js_re(text: str) -> str:
    """Экранировать для RegExp с флагом `u` (ESLint компилирует так): только синтаксические символы.

    `re.escape` Питона экранирует и `-`, а `\\-` вне класса в режиме `u` — синтаксическая ошибка JS.
    """
    return "".join("\\" + c if c in _RE_SPECIAL else c for c in text)


def _rel(target_dir: str, from_dir: str) -> str:
    rel = posixpath.relpath(target_dir, from_dir)
    return rel if rel.startswith("..") else "./" + rel


def _alias_forms(target_dir: str, aliases: dict) -> list:
    """`src/app` при алиасе `@/` -> `src/` даёт `@/app`: алиас объявлен, а не угадан."""
    out = []
    for alias, prefix in aliases.items():
        p = str(prefix).strip("/")
        if target_dir == p or target_dir.startswith(p + "/"):
            out.append(str(alias).rstrip("/") + "/" + target_dir[len(p):].strip("/"))
    return [f.rstrip("/") for f in out]


def _target_pattern(zone_from, zone_to, reason, to_dirs, fdir, aliases) -> dict:
    forms = [_js_re(_rel(td, fdir)) for td in to_dirs]
    forms += [_js_re(a) for td in to_dirs for a in _alias_forms(td, aliases)]
    return {"regex": "^(?:" + "|".join(forms) + ")(?:/|$)",
            "message": f"{zone_from} -> {zone_to}: {reason}"}


def _sibling_pattern(zone, slice_name, zone_dir, fdir, aliases, reason) -> dict:
    guard = "(?!" + _js_re(slice_name) + "(?:/|$))[^./][^/]*"
    forms = [_js_re(_rel(zone_dir, fdir)) + "/" + guard]
    forms += [_js_re(a) + "/" + guard for a in _alias_forms(zone_dir, aliases)]
    return {"regex": "^(?:" + "|".join(forms) + ")(?:/|$)",
            "message": f"{zone} -> соседний срез {zone}: {reason}"}


def _units(root, zone_dir, sibling: bool, exts) -> list:
    """Единицы границы: (каталог, срез|None, глубины). Глубины — от 0 до самой глубокой на диске."""
    def depths(base, files):
        deepest = max((len(Path(f).relative_to(base).parts) - 1 for f in files), default=0)
        return list(range(deepest + 1))
    files = lp.source_files(root, zone_dir, exts)
    if not sibling:
        return [(zone_dir, None, depths(zone_dir, files))]
    units = [(zone_dir, None, [0])]
    base = Path(root) / zone_dir
    for d in sorted(p.name for p in base.iterdir() if p.is_dir() and p.name not in lp.SKIP_DIRS):
        sub = f"{zone_dir}/{d}"
        units.append((sub, d, depths(sub, [f for f in files if f.startswith(sub + "/")])))
    return units


def _file_depth(rel_file: str, base: str):
    if not rel_file.startswith(base + "/"):
        return None
    return len(Path(rel_file).relative_to(base).parts) - 1


def boundary_objects(child_root, decl: dict, exts) -> list:
    """Объекты flat-config с `ai-ops/no-restricted-imports` из объявленных зон и запретов."""
    root = Path(child_root)
    zdirs = lp.zones(decl)
    targets, siblings = lp.forbidden(decl)
    file_exc, pair_exc = lp.zone_exceptions(decl)
    aliases = {str(k): str(v) for k, v in (decl.get("aliases") or {}).items()}
    ext_glob = "{" + ",".join(exts) + "}"
    objects = []
    for zf in sorted(set(targets) | set(siblings)):
        tz = {t: r for t, r in targets.get(zf, {}).items() if (zf, t) not in pair_exc}
        for zd in zdirs.get(zf, []):
            if not lp.source_files(root, zd, exts):   # зона другого языка — не граница ESLint
                continue
            for base, slice_name, depths in _units(root, zd, zf in siblings, exts):
                for d in depths:
                    fdir = base + "/x" * d
                    pats = {t: _target_pattern(zf, t, r, zdirs.get(t, []), fdir, aliases)
                            for t, r in sorted(tz.items()) if zdirs.get(t)}
                    if slice_name:              # ключ — сама зона: `файл -> зона` снимает соседей
                        pats[zf] = _sibling_pattern(zf, slice_name, zd, fdir, aliases,
                                                               siblings[zf])
                    if not pats:
                        continue
                    name = "/".join(["ai-ops/boundaries", zf] + ([slice_name] if slice_name else [])
                                    + [str(d)])
                    obj = {"name": name, "files": [base + "/*" * d + "/*." + ext_glob],
                           "rules": {"ai-ops/no-restricted-imports":
                                     ["error", {"patterns": list(pats.values())}]}}
                    exc = sorted({f for f, t in file_exc if _file_depth(f, base) == d and t in pats})
                    if exc:
                        obj["ignores"] = exc
                    objects.append(obj)
                    for f in exc:            # исключение снимает ОДНО ребро файла, не все
                        keep = [p for t, p in pats.items() if (f, t) not in file_exc]
                        if keep:
                            objects.append({"name": f"{name}/exception", "files": [f],
                                            "rules": {"ai-ops/no-restricted-imports":
                                                      ["error", {"patterns": keep}]}})
    return objects


def identifier_objects(exts, ts: bool) -> list:
    """Правила «имена латиницей»: `id-match` для всех, `naming-convention` для типов TS."""
    objs = [{"name": "ai-ops/identifiers", "files": ["**/*.{" + ",".join(exts) + "}"],
             "rules": {"ai-ops/id-match": ["error", LATIN, {
                 "properties": True, "classFields": True, "onlyDeclarations": True,
                 "ignoreDestructuring": False}]}}]
    if ts:
        custom = {"regex": LATIN, "match": True}
        objs.append({"name": "ai-ops/identifiers-types", "files": ["**/*.{" + ",".join(TS_EXT) + "}"],
                     "rules": {"ai-ops/naming-convention": [
                         "error",
                         {"selector": "typeLike", "format": None, "custom": custom},
                         {"selector": "enumMember", "format": None, "custom": custom}]}})
    return objs


def render(child_root, decl: dict, latin: bool) -> dict | None:
    """ESLint-часть профиля: файл конфига поверх конфига дочки + описание запуска для базовой линии."""
    info = detect(child_root)
    if info is None:
        return None
    ts = info["typescript"]
    exts = JS_EXT + (TS_EXT if ts else ())
    notes = []
    objects = identifier_objects(exts, bool(ts)) if latin else []
    objects += boundary_objects(child_root, decl, exts)
    stack = "typescript" if ts else "javascript"
    if not objects:
        return {"stack": stack, "files": {}, "tools": [],
                "notes": ["JS/TS: имена разрешены любые, границы не объявлены — добавлять нечего"]}
    if not info["eslint"]:
        notes.append("JS/TS: ESLint не стоит в зависимостях — профиль записан, но проверить его нечем")
    base = info["base"]
    if base:
        base_import = f'import base from "./{base}";\n'
        base_expr = "(Array.isArray(base) ? base : [base])"
        base_note = (f"Ваш {base} подключается целиком и не меняется.")
    else:
        base_import, base_expr = "", "[]"
        base_note = "Своего конфига ESLint у репозитория нет — профиль работает один."
    text = lp.fill(lp.read_template(TEMPLATE), BASE_NOTE=base_note,
                   IMPORTS=((ts[1] + "\n") if ts and latin else "") + base_import,
                   EXTRA_RULES=(f'\n    "naming-convention": {ts[2]},' if ts and latin else ""),
                   PROFILE=json.dumps(objects, ensure_ascii=False, indent=2), BASE_EXPR=base_expr)
    return {"stack": stack, "files": {OUT: text}, "notes": notes,
            "tools": [{"tool": "eslint", "config": OUT, "suppressions": SUPPRESSIONS,
                       "rule_prefix": "ai-ops/",
                       "rules": sorted({r for o in objects for r in o["rules"]})}]}
