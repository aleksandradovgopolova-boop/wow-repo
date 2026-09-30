"""Зависимости из манифестов: какие имена объявлены и какие из них НОВЫЕ.

Сателлит `security_scan` (фасад импортирует отсюда `new_dependencies`,
`new_dependencies_detailed` и `DEP_MANIFESTS` и реэкспортирует их — публичная поверхность не
переезжает: под этими именами разбор читают `security_pack.run_pack` и
`planning.architecture_invariants`).

Вынесен, когда фасад подошёл к порогу монолита в 700 строк вплотную: разбор манифестов — цельный
кусок, не связанный ни с секретами, ни с injection-флагами, и единственное, что он берёт извне, —
`re`, `json` и `Path`. Перенос ДОСЛОВНЫЙ: ни одно правило разбора здесь не менялось.

Своей точки входа у сателлита нет осознанно: он обслуживает сканер, а не запускается сам.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# ─── зависимости из TOML ──────────────────────────────────────────────────────────────────────
#
# ПОЧЕМУ ЗДЕСЬ ОТДЕЛЬНЫЙ РАЗБОР, А НЕ РЕГУЛЯРКА ПО ВСЕМУ ФАЙЛУ. Прежде имена искались по всему
# тексту образцами `"имя" =` и `имя = "`, без оглядки на секцию. На собственном репозитории кита
# это давало 18 «новых зависимостей», и ВСЕ 18 были ключами настроек: `name`, `version`, `license`,
# `edition`, `target-version`, `addopts`, `requires-python`, `tag_format`…
#
# Цена измерена: `security` — один из восьми блокирующих гейтов MVP, и проверка, ложная на 100% в
# одной из трёх своих категорий, учит игнорировать себя ЦЕЛИКОМ. Ложная тревога дороже молчания:
# молчание не притворяется работой.
#
# Секции объявлены СПИСКОМ: «всё, что похоже на пару имя-значение» — это не про зависимости.

# pyproject.toml: где действительно живут зависимости.
_PY_ARRAY_KEYS = (("project", "dependencies"), ("build-system", "requires"))
# Секции-ТАБЛИЦЫ, где ключ и есть имя пакета. `project.optional-dependencies` сюда НЕ входит:
# там ключ — имя группы (`dev`, `test`), а зависимости лежат в массиве-значении.
_PY_TABLE_SECTIONS = ("tool.poetry.dependencies", "tool.poetry.dev-dependencies")
# Cargo.toml: секции-таблицы, где ключ — имя крейта.
_CARGO_SECTIONS = ("dependencies", "dev-dependencies", "build-dependencies")

_PEP508_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _requirement_name(spec: str) -> str:
    """`pyyaml>=6.0,<7` -> `pyyaml`; `serde = { version = "1" }` уже разобран вызывающим."""
    m = _PEP508_NAME.match(str(spec))
    return m.group(1).lower() if m else ""


def _is_dep_section(name: str, section: str) -> bool:
    """Секция таблицы, в которой КЛЮЧ — это имя зависимости."""
    if name == "Cargo.toml":
        # `[dependencies]`, `[dev-dependencies]`, `[target.'cfg(...)'.dependencies]`
        return section in _CARGO_SECTIONS or section.split(".")[-1] in _CARGO_SECTIONS
    if section in _PY_TABLE_SECTIONS:
        return True
    # `[tool.poetry.group.<имя>.dependencies]`
    return section.startswith("tool.poetry.group.") and section.endswith(".dependencies")


def _toml_dep_names(text: str, manifest_name: str = "pyproject.toml") -> set:
    """Имена зависимостей из TOML. Секции объявлены, всё прочее не считается зависимостью.

    РАЗБОР ОДИН, БЕЗ `tomllib`. Он появился в stdlib только с 3.11, а объявленный пол кита — 3.9
    (`requires-python`), и собственный `validate_python_compat` этот импорт отклоняет. Два пути
    разбора означали бы ещё и два поведения: на 3.11 один ответ, на 3.9 другой — ровно тот класс
    «у меня работает», против которого стоит охват `compatibility-matrix`.

    Первая версия этой правки имела оба пути; расхождение между ними тест поймал сразу (фолбэк
    принимал имя ГРУППЫ `dev`/`test` за пакет). Это и есть довод: сверять два разбора дешевле не
    получается, а один разбор сверять не с чем — он просто один.
    """
    return _toml_dep_names_scanned(text, manifest_name)


_SECTION = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]\s*$")
_ARRAY_KEY = re.compile(r"^\s*([A-Za-z0-9._-]+)\s*=\s*\[")
_TABLE_KEY = re.compile(r"^\s*([A-Za-z0-9._\"'-]+)\s*=")
_QUOTED = re.compile(r"[\"']([^\"']+)[\"']")


def _toml_dep_names_scanned(text: str, manifest_name: str) -> set:
    """Фолбэк для Python 3.9/3.10 и для битого TOML: тот же ответ, построчным сканером.

    Секция отслеживается, потому что именно её отсутствие и было дефектом: ключ `name` в
    `[project]` — это имя проекта, а не пакет. Две формы записи различаются, и это не мелочь:
    в `[project.optional-dependencies]` ключ — имя ГРУППЫ (`dev`, `test`), а зависимости лежат
    в массиве-значении. Считать ключ именем пакета значило бы заменить одни ложные находки
    другими.
    """
    deps, section, in_array = set(), "", False

    def take_specs(fragment):
        for q in _QUOTED.findall(fragment):
            deps.add(_requirement_name(q))

    for raw in text.splitlines():
        line = "" if raw.strip().startswith("#") else raw.split("#", 1)[0]
        m = _SECTION.match(line)
        if m:
            section, in_array = m.group(1).strip().strip("\"'"), False
            continue
        if in_array:
            take_specs(line)
            if "]" in line:
                in_array = False
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().strip("\"'")
        if value.lstrip().startswith("["):
            # Массив спецификаций — только в объявленных местах.
            if (section, key) in _PY_ARRAY_KEYS or section == "project.optional-dependencies":
                take_specs(value)
                in_array = "]" not in value
            continue
        if _is_dep_section(manifest_name, section):
            deps.add(key.lower())
    return deps - {""}


def _dep_names(path, text):
    """Множество имён зависимостей из манифеста (по типу файла). Best-effort, детерминированно."""
    name = Path(path).name
    deps = set()
    if name == "package.json":
        try:
            data = json.loads(text)
            for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                deps |= set((data.get(key) or {}).keys())
        except json.JSONDecodeError:
            pass
    elif name == "requirements.txt":
        for ln in text.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                # maxsplit=1 по имени: позиционная передача объявлена устаревшей в Python 3.13
                # и подлежит удалению. Пол объявлен (3.9), потолка у requires-python нет —
                # значит кит однажды поедет на интерпретаторе, где это TypeError.
                deps.add(re.split(r"[<>=!~\[ ]", ln, maxsplit=1)[0].strip().lower())
    elif name == "go.mod":
        # обе формы: однострочная `require github.com/x/y v1.2.3` и блок `require ( ... )`
        for m in re.finditer(r"^\s*(?:require\s+)?([\w][\w./\-]+)\s+v\d", text, re.M):
            if m.group(1) != "require":
                deps.add(m.group(1))
    elif name in ("pyproject.toml", "Cargo.toml"):
        deps |= _toml_dep_names(text, name)
    return deps


def new_dependencies(before, after):
    """before/after: {manifest_path: content}. -> отсортированный список НОВЫХ имён зависимостей."""
    added = set()
    for path, after_text in after.items():
        before_names = _dep_names(path, before.get(path, ""))
        added |= (_dep_names(path, after_text) - before_names)
    return sorted(added)


def _dep_specs(path, text):
    """{name: version|None} из манифеста (версия best-effort: requirements '==', package.json значение)."""
    name = Path(path).name
    specs = {}
    if name == "package.json":
        try:
            data = json.loads(text)
            for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                for k, v in (data.get(key) or {}).items():
                    specs[k] = str(v)
        except json.JSONDecodeError:
            pass
    elif name == "requirements.txt":
        for ln in text.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                nm = re.split(r"[<>=!~\[ ]", ln, maxsplit=1)[0].strip().lower()  # см. выше про 3.13
                mv = re.search(r"==\s*([0-9][\w.\-]*)", ln)
                specs[nm] = mv.group(1) if mv else None
    else:
        for nm in _dep_names(path, text):
            specs[nm] = None
    return specs


def new_dependencies_detailed(before, after):
    """v3.0-rc5 (P1.2): НОВЫЕ зависимости с деталями для fingerprint approval.
    -> [{name, version, manifest, operation:'add'}] (отсортировано по manifest, name)."""
    out = []
    for path in sorted(after):
        b, a = _dep_specs(path, before.get(path, "")), _dep_specs(path, after[path])
        for nm in sorted(set(a) - set(b)):
            out.append({"name": nm, "version": a.get(nm), "manifest": Path(path).name, "operation": "add"})
    return out


DEP_MANIFESTS = ("package.json", "requirements.txt", "go.mod", "pyproject.toml", "Cargo.toml")
