#!/usr/bin/env python3
"""Контуры модели продуктового репозитория: состояние источников истины и связность (v3.35.0).

Модель объявлена в `registry/product-operating-model.yaml` — здесь только код, который по ней
работает. Две задачи:

  1. СОСТОЯНИЕ КОНТУРА — есть ли у него источник истины в этом репозитории (`sot_state`).
     Отсутствие обязательного источника — пробел модели, а не мелочь: контур объявлен, а отвечать
     на свои вопросы ему нечем.

  2. СВЯЗНОСТЬ — что изменилось в diff, что WorkItem заявил в `affects`, и совпадает ли это
     (`derive_affects` + `reconcile`). Ради этого модель и существует: нельзя закрыть работу,
     обновив React-компонент, если изменилась модель данных.

ТРИ СОСТОЯНИЯ, И ТРЕТЬЕ НЕ РАВНО ВТОРОМУ:
    changed      — сигнальный путь контура попал в diff;
    not_changed  — сигнальные пути в репозитории ЕСТЬ, и ни один не попал в diff;
    unknown      — сигнальных путей у контура в этом репозитории нет. Кит не умеет его видеть.

`unknown` НИКОГДА не сворачивается в `not_changed`. Это тот же инвариант, что `usage_status`
(3.10) и `unavailable != 0` (3.19): неизвестное нельзя считать нулём, иначе зелёный цвет
перестаёт что-либо значить. Разница между «не менялось» и «не умею видеть» — разница между
утверждением и признанием; репозиторий доопределяет сигналы в `.ai-ops.yaml`, а кит их не
выдумывает.

Использование:
  contours.py state <repo> [--json]                 # состояние источников истины по контурам
  contours.py affects <repo> --files a,b [--json]    # какие контуры затронуты изменением
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
           Path(__file__).resolve().parents[2])
MODEL_PATH = PKG / "registry" / "product-operating-model.yaml"

CHANGED, NOT_CHANGED, UNKNOWN = "changed", "not_changed", "unknown"

# Куда репозиторий вправе положить свою копию документа контекста (project/custom-оверлей —
# механизм 3.12: managed-слой кита отдельно от правды репозитория).
_OVERLAYS = ("", ".ai/project/", ".ai/custom/")

# Каталоги, которые НЕ являются продуктом: внутренности кита, вендор, сборка, бэкапы. Поиск
# сигнального пути обязан их пропускать, иначе кит принимает свои же файлы за факт о продукте.
# Обкатка на wow-repo (стек node/react/astro): контур архитектуры заявлял `not_changed`, потому что
# нашёл Dockerfile в `.ai/runtime/backups/3.27.6/.ai/managed/containers/` — бэкапе СОБСТВЕННОГО
# managed-слоя. Это подмена признания утверждением: честный ответ был `unknown`. Тот же класс, что
# доказательства, указывавшие внутрь `.claude/worktrees/*`.
# `.ai/project/` и `.ai/custom/` из-под запрета выведены отдельно (см. _under_excluded): там лежит
# правда РЕПОЗИТОРИЯ, а не кита.
_NOT_PRODUCT = (".git", ".ai", ".claude", "node_modules", "dist", "build", "target", "vendor",
                ".venv", "venv", "__pycache__", ".next", ".mypy_cache", ".pytest_cache",
                ".ruff_cache")


def _under_excluded(rel: str) -> bool:
    """Лежит ли путь внутри каталога, не являющегося продуктом. project/custom-оверлей — исключение."""
    parts = [x for x in str(rel).replace("\\", "/").split("/") if x and x != "."]
    if not parts:
        return False
    if parts[:2] in (([".ai", "project"]), ([".ai", "custom"])):
        return False                                   # правда репозитория, а не кита
    return any(x in _NOT_PRODUCT for x in parts)


class ModelCorrupt(Exception):
    """Модель контуров недостоверна — по ней работают детект, гейт и doctor, догадки запрещены."""


def load_model(path: Path | None = None) -> dict:
    """Модель контуров из реестра. FAIL-CLOSED: битая модель — исключение, не пустой список.

    Пустая модель означала бы «контуров нет» -> связность проверять нечем -> любая работа
    выглядит согласованной. Это ровно тот класс дефекта, что порча `registry/tracks.yaml`,
    которую в 3.33 не ловил никто.
    """
    p = Path(path or MODEL_PATH)
    if not p.is_file():
        raise ModelCorrupt(f"модель контуров не найдена: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ModelCorrupt(f"модель контуров не разбирается ({p}): {e}") from e
    if not isinstance(data, dict) or not data.get("contours"):
        raise ModelCorrupt(f"модель контуров пуста или без ключа contours ({p})")
    return data


def contour_ids(model: dict) -> list:
    return [c["id"] for c in model.get("contours") or [] if c.get("id")]


def contour(model: dict, cid: str) -> dict | None:
    return next((c for c in model.get("contours") or [] if c.get("id") == cid), None)


# Кэш разобранной конфигурации репозитория по СОСТОЯНИЮ файла (путь, mtime, размер). Прежде
# `.ai-ops.yaml` разбирался на каждый вызов: до восьми разборов YAML за одно срабатывание гейта
# (`repo_overrides` + `repo_sot` на каждый контур).
#
# ЭТО ПРАВКА ПРОИЗВОДИТЕЛЬНОСТИ, А НЕ СОГЛАСОВАННОСТИ, и путать нельзя: mtime в ключе означает, что
# изменённый файл ПЕРЕЧИТЫВАЕТСЯ. Именно перечитывание живьём испортило один из замеров обкатки —
# конфигурацию правили, пока прогон шёл, и находки исчезали посреди выборки. Снимок обязан делать
# АНАЛИЗ (прочитать конфигурацию один раз и передать дальше), а не читатель: читатель не знает, где
# границы анализа, и молча замороженная конфигурация была бы дефектом хуже лишнего разбора.
_CFG_CACHE = {}


def _child_config(child_root) -> dict:
    """Разобранный `.ai-ops.yaml` репозитория. -> dict (пустой, если файла нет или он битый).

    Битый конфиг даёт ПУСТОЙ словарь, а не исключение: доопределение путей — не то место, где стоит
    ронять прогон, а невалидный конфиг ловят `doctor` и `validate_ai_ops_child`.
    """
    cfg = Path(child_root) / ".ai-ops.yaml"
    try:
        st = cfg.stat()
    except OSError:
        return {}
    key = (str(cfg), st.st_mtime_ns, st.st_size)
    hit = _CFG_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    _CFG_CACHE.clear()                             # состояние файла сменилось — прежнее не нужно
    _CFG_CACHE[key] = data
    return data


def _declared_contours(child_root) -> dict:
    return (_child_config(child_root).get("product_operating_model") or {}).get("contours") or {}


class ConfigInvalid(Exception):
    """Объявление репозитория недостоверно. Молча взять дефолт значило бы работать не там, где сказано."""


def declared_path(child_root, key: str, default: str) -> str:
    """Путь артефакта, объявленный репозиторием: `product_operating_model.paths.<key>`.

    ЗАЧЕМ. Кит требовал `planning/plan.yaml` и `ROADMAP.md` строго в корне. Монорепозиторий, где
    продукт живёт в `apps/web/`, не мог описать себя вовсе: `next` отвечал «плана нет» на репозиторий
    с планом (тир 3 разбора перед квалификацией).

    Абсолютный путь и выход за корень — ОШИБКА, а не повод взять дефолт: и то и другое означает, что
    кит писал бы и читал не в том месте, о котором думает владелец.
    """
    rel = ((_child_config(child_root).get("product_operating_model") or {}).get("paths")
           or {}).get(key)
    if rel is None:
        return default
    rel = str(rel).strip()
    if not rel:
        return default
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        raise ConfigInvalid(
            f".ai-ops.yaml -> product_operating_model.paths.{key} = '{rel}': путь обязан быть "
            f"относительным и внутри репозитория")
    return rel


def repo_signal_rules(child_root) -> dict:
    """Правила репозитория поверх сигналов кита. -> {cid: {"remove": [...], "replace": bool}}.

    ДОПОЛНЯТЬ КИТ БЫЛО МОЖНО, СПОРИТЬ С НИМ — НЕТ. Кит объявляет `**/entities/**` сигналом модели
    данных; в проекте на Feature-Sliced Design `entities` — слой ИНТЕРФЕЙСА, и на обкатке niti это
    дало 6 ложных находок из 9. Убрать чужой сигнал можно было только правкой самого кита — то есть
    нельзя. Теперь у владельца есть два способа: снять конкретный паттерн (`change_signals_remove`)
    или объявить свой список единственным (`change_signals_replace: true`).
    """
    out = {}
    for cid, v in (_declared_contours(child_root) or {}).items():
        v = v or {}
        rm = [str(x) for x in (v.get("change_signals_remove") or [])]
        rep = bool(v.get("change_signals_replace"))
        if rm or rep:
            out[cid] = {"remove": rm, "replace": rep}
    return out


def repo_overrides(child_root: Path) -> dict:
    """Сигналы, доопределённые репозиторием в `.ai-ops.yaml -> product_operating_model.contours`.

    Кит знает пути ТИПОВЫХ стеков, но не знает, что в этом продукте события лежат в
    `src/telemetry/`. Доопределение — способ превратить `unknown` в проверяемое состояние
    руками владельца, а не выдумкой кита.
    """
    return {k: list((v or {}).get("change_signals") or [])
            for k, v in _declared_contours(child_root).items()}


def repo_sot(child_root) -> dict:
    """Источники истины, объявленные РЕПОЗИТОРИЕМ: `.ai-ops.yaml -> …contours.<id>.source_of_truth`.

    Кит знает типовые места, но где лежит правда ЭТОГО продукта, знает только владелец. Обкатка на
    живом репозитории показала цену пробела: ADR лежали в `docs/architecture/decisions/`, сигнал их
    ловил верно, а объявленного китом `decisions/registry.yaml` в репозитории нет — и находка
    «истина не обновлена» срабатывала ВЕЧНО на контуре, который поддерживается как надо.
    Объявленное репозиторием ДОПОЛНЯЕТ дефолт кита, а не заменяет: путь кита мог существовать тоже.
    """
    return {k: list((v or {}).get("source_of_truth") or [])
            for k, v in _declared_contours(child_root).items()}


def sot_for(model: dict, cid: str, child_root=None) -> list:
    """Источники истины контура: объявленные китом + доопределённые репозиторием.

    -> список {"path", "required"}. Пути репозитория считаются обязательными: владелец назвал их
    сам, значит это и есть его правда, а не необязательное дополнение.
    """
    base = [dict(s) for s in ((contour(model, cid) or {}).get("source_of_truth") or [])]
    declared = (repo_sot(child_root) if child_root is not None else {}).get(cid) or []
    if declared:
        # ОБЪЯВЛЕНИЕ ВЛАДЕЛЬЦА СИЛЬНЕЕ ДОГАДКИ КИТА. Пути кита остаются в списке (они могли
        # существовать тоже), но перестают быть ОБЯЗАТЕЛЬНЫМИ: иначе `ok` у контура недостижим
        # никогда, и doctor вечно требует файл, которого в этом продукте не будет. Ровно этот
        # случай дала обкатка: ADR лежат не там, где кит по умолчанию их ищет.
        for s in base:
            s["required"] = False
            s["superseded_by"] = "repository"
    known = {s.get("path") for s in base}
    for rel in declared:
        if rel not in known:
            base.append({"path": rel, "required": True, "declared_by": "repository"})
    return base


def signals_for(model: dict, cid: str, overrides: dict | None = None,
                rules: dict | None = None) -> list:
    """Сигнальные пути контура: сигналы кита + доопределённые репозиторием − снятые репозиторием.

    Порядок именно такой: сначала база (или её отмена через `replace`), потом добавления владельца,
    и только потом снятия — иначе владелец не мог бы снять свой же паттерн, добавленный шаблоном.
    """
    base = list((contour(model, cid) or {}).get("change_signals") or [])
    r = (rules or {}).get(cid) or {}
    if r.get("replace"):
        base = []
    out = base + list((overrides or {}).get(cid) or [])
    remove = {str(x).replace("\\", "/") for x in (r.get("remove") or [])}
    if remove:
        out = [p for p in out if str(p).replace("\\", "/") not in remove]
    return out


def _resolve(child_root: Path, rel: str) -> Path | None:
    """Путь источника истины с учётом project/custom-оверлея. -> существующий путь или None."""
    for pre in _OVERLAYS:
        p = Path(child_root) / (pre + rel)
        if p.exists():
            return p
    return None


def sot_state(child_root: Path, model: dict | None = None) -> dict:
    """Состояние источников истины по контурам.

    -> {contour_id: {"required_missing": [...], "optional_missing": [...], "present": [...],
                     "ok": bool, "question": str}}
    `ok` — только про ОБЯЗАТЕЛЬНЫЕ источники: необязательные отсутствуют законно (не у каждого
    продукта есть runbooks), и требовать их значило бы разводить ту самую бюрократию.
    """
    model = model or load_model()
    root = Path(child_root)
    out = {}
    for c in model.get("contours") or []:
        req_missing, opt_missing, present = [], [], []
        # sot_for, а не поле контура: репозиторий вправе объявить свой источник истины, и тогда
        # догадка кита перестаёт быть обязательной (иначе `ok` недостижим никогда).
        for s in sot_for(model, c["id"], root):
            rel = s.get("path") or ""
            if _resolve(root, rel):
                present.append(rel)
            elif s.get("required"):
                req_missing.append(rel)
            else:
                opt_missing.append(rel)
        out[c["id"]] = {"question": c.get("question", ""), "title": c.get("title", c["id"]),
                        "owner_role": c.get("owner_role"), "present": present,
                        "required_missing": req_missing, "optional_missing": opt_missing,
                        "ok": not req_missing}
    return out


def unquote_git_path(path: str) -> str:
    """Путь из git как есть -> настоящее имя. Второй эшелон защиты от `core.quotePath`.

    Источник уже чинится ключом `-z` (`engine/pipeline_git.py`), но сюда пути приходят и из других
    мест (CLI `--files`, ручные вызовы, чужие обёртки). Если имя пришло в кавычках с
    octal-escape'ами, оно не совпадёт ни с одним паттерном и `changed` станет `not_changed` —
    молча. Разбираем: кавычки снимаем, восьмеричные escape'ы собираем в байты и декодируем UTF-8.
    """  # noqa: D301
    s = str(path or "")
    if not (len(s) >= 2 and s[0] == '"' and s[-1] == '"'):
        return s
    body = s[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 3 < len(body) + 1 and body[i + 1:i + 4].isdigit():
            try:
                out.append(int(body[i + 1:i + 4], 8))
                i += 4
                continue
            except ValueError:
                pass
        if ch == "\\" and i + 1 < len(body):
            out.extend({"n": b"\n", "t": b"\t", '"': b'"', "\\": b"\\"}.get(body[i + 1],
                                                                          body[i + 1].encode()))
            i += 2
            continue
        out.extend(ch.encode("utf-8"))
        i += 1
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return s


def _matches(rel_path: str, pattern: str) -> bool:
    """Соответствие пути globу. Каталог-паттерн (`decisions/`) покрывает всё под ним.

    Реализовано на fnmatch, а не на pathlib.match: нужен `**`, пересекающий несколько сегментов
    (`**/migrations/**`), которого `Path.match` не даёт. Отдельно — префиксный случай для
    паттернов-каталогов, чтобы `schemas/` не требовал писать `schemas/**`.
    """
    rel = rel_path.replace("\\", "/")
    # НЕ lstrip("./"): это снятие НАБОРА символов, а не префикса, и `.ai-ops.yaml` превращался в
    # `ai-ops.yaml`. Следствие было тяжёлым: ни один dot-путь не совпадал со своим же сигнальным
    # паттерном, поэтому изменение исполняемой части контракта (protected_paths, approvals) и
    # CI-конвейера проходило гейт связности как согласованное. Снимаем ровно префикс `./`.
    while rel.startswith("./"):
        rel = rel[2:]
    pat = (pattern or "").replace("\\", "/")
    if not pat:
        return False
    if pat.endswith("/"):
        return rel.startswith(pat)
    if fnmatch.fnmatch(rel, pat):
        return True
    # `context/product/**` обязан покрывать и сам `context/product/x.md`, и вложенное глубже.
    if pat.endswith("/**") and rel.startswith(pat[:-3] + "/"):
        return True
    # `**/models/**` для пути без ведущих сегментов (`models/user.py`).
    if pat.startswith("**/") and fnmatch.fnmatch(rel, pat[3:]):
        return True
    return False


def _product_dirs(child_root: Path):
    """Каталоги ПРОДУКТА: обход с ПОДРЕЗКОЙ на не-продуктовых каталогах.

    Подрезка, а не фильтрация после обхода. Обкатка на niti (Next.js, 488 коммитов) показала цену
    разницы: один вызов гейта тратил 12 СЕКУНД на поиск сигнальных путей, а гейт зовут на КАЖДОМ
    прогоне конвейера. Причём предыдущая правка (исключение внутренностей кита) это усугубила: до
    неё `rglob` останавливался на первом попадании — часто внутри `node_modules` — а после стала
    обходить дерево целиком, чтобы отфильтровать исключённое. `os.walk` с подрезкой `dirnames`
    в исключённые каталоги не заходит вовсе.
    """
    import os
    root = Path(child_root)
    for cur, dirnames, filenames in os.walk(root, topdown=True):
        rel = Path(cur).relative_to(root)
        # Подрезка НА МЕСТЕ: os.walk не пойдёт в удалённые из dirnames каталоги.
        dirnames[:] = [d for d in dirnames
                       if not _under_excluded(str(rel / d) if str(rel) != "." else d)]
        yield Path(cur), filenames


def _repo_has_signal(child_root: Path, patterns: list) -> bool:
    """Есть ли в репозитории ХОТЬ ОДИН путь, попадающий под сигналы контура.

    Это и есть граница между `not_changed` и `unknown`. Обход ограничен: заглядываем в объявленные
    префиксы, а не сканируем дерево целиком — на большом продукте полный обход стоил бы дороже
    самой проверки, а ответ нужен один бит.

    Для ОДНОГО контура. Когда контуров много (гейт, `model`), звать надо `signals_present`: она
    делает один обход на все, а не по обходу на каждый.
    """
    root = Path(child_root)
    unanchored = []
    for pat in patterns or []:
        pat = pat.replace("\\", "/")
        head = pat.split("*")[0].rstrip("/")
        if head:
            if _under_excluded(head):
                continue                               # сигнал, указывающий внутрь кита/вендора
            if (root / head).exists():
                return True
            # `context/product/MetricCatalog.md` мог уехать в project/custom-оверлей
            if _resolve(root, head):
                return True
            continue
        # Паттерн без якоря (`**/models.py`, `**/*.proto`, `**/migrations/**`) — ищем ограниченно.
        # Хвост берём ПОСЛЕДНИМ ЗНАЧАЩИМ сегментом: у `**/migrations/**` последний сегмент — `**`,
        # а `rglob("**")` возвращает сам корень, то есть сигнал «есть» в любом каталоге. Из-за этого
        # `unknown` сворачивался в `not_changed` — главный инвариант модели нарушался везде.
        segs = [s for s in pat.split("/") if s and s != "**"]
        if not segs:
            continue                               # паттерн из одних `**` не является сигналом
        unanchored.append(segs[-1])

    if not unanchored:
        return False
    # ОДИН подрезанный обход на все безякорные паттерны: прежде их было до восьми, и каждый гнал
    # свой полный rglob по дереву.
    import fnmatch as _fn
    try:
        for cur, filenames in _product_dirs(root):
            names = filenames + [cur.name]
            for tail in unanchored:
                if any(_fn.fnmatch(n, tail) for n in names):
                    return True
    except OSError:
        return False
    return False


def signals_present(child_root: Path, patterns_by_contour: dict) -> set:
    """У каких контуров сигнальные пути в репозитории ЕСТЬ. -> множество id. ОДИН обход дерева.

    ПОЧЕМУ ОДИН. `_repo_has_signal` звался по контуру — до восьми раз за вызов гейта, и каждый гнал
    свой обход дерева. На монорепозитории (обкатка niti: Next.js, 488 коммитов) обход стоил секунды,
    а гейт зовут на КАЖДОМ прогоне конвейера. Здесь безякорные хвосты всех контуров собираются в
    один проход, и проход прекращается, как только каждому нашёлся путь.
    """
    root = Path(child_root)
    present, pending = set(), {}
    for cid, pats in (patterns_by_contour or {}).items():
        tails, anchored = [], False
        for pat in pats or []:
            pat = str(pat).replace("\\", "/")
            head = pat.split("*")[0].rstrip("/")
            if head:
                if _under_excluded(head):
                    continue                           # сигнал, указывающий внутрь кита/вендора
                if (root / head).exists() or _resolve(root, head):
                    anchored = True
                    break
                continue
            # Хвост берём ПОСЛЕДНИМ ЗНАЧАЩИМ сегментом: у `**/migrations/**` последний сегмент —
            # `**`, и он совпал бы с любым каталогом, свернув `unknown` в `not_changed`.
            segs = [s for s in pat.split("/") if s and s != "**"]
            if segs:
                tails.append(segs[-1])
        if anchored:
            present.add(cid)
        elif tails:
            pending[cid] = tails
    if not pending:
        return present
    import fnmatch as _fn
    try:
        for cur, filenames in _product_dirs(root):
            names = filenames + [cur.name]
            for cid, tails in list(pending.items()):
                if any(_fn.fnmatch(n, t) for t in tails for n in names):
                    present.add(cid)
                    del pending[cid]
            if not pending:
                break
    except OSError:
        return present
    return present


def derive_affects(child_root: Path, changed_files: list, model: dict | None = None,
                   overrides: dict | None = None, rules: dict | None = None) -> dict:
    """Затронутые контуры — ВЫВОД из фактического изменения, а не заявление автора.

    -> {contour_id: {"state": changed|not_changed|unknown, "matched": [...], "signals": N,
                     "reason": str}}

    `changed_files` — пути относительно корня репозитория (обычно `git diff --name-only`).
    Пустой список НЕ означает `not_changed` для всех: он означает, что изменений не предъявлено,
    и такой ответ честнее пересчитывать на месте вызова, чем зеленить здесь.
    """
    model = model or load_model()
    root = Path(child_root)
    overrides = repo_overrides(root) if overrides is None else overrides
    rules = repo_signal_rules(root) if rules is None else rules
    files = [unquote_git_path(f).replace("\\", "/") for f in (changed_files or [])]

    # ПРАВИЛО СПЕЦИФИЧНОСТИ: точный путь сильнее чужого glob. `context/system/DataMap.md` — явный
    # сигнал и источник истины контура данных, но он же попадает под `context/system/**` контура
    # архитектуры; без этого правила КОРРЕКТНОЕ обновление модели данных давало ложную находку
    # `undeclared_change` по архитектуре. Гейт, выдающий шум, перестают читать — и он становится
    # хуже отсутствующего, поэтому шум здесь не косметика, а дефект. Найдено живой проверкой 3.35.
    pats_by = {cid: signals_for(model, cid, overrides, rules) for cid in contour_ids(model)}
    exact_owner = {}
    for _cid, _pats in pats_by.items():
        for _p in _pats:
            if "*" not in _p and not _p.endswith("/"):
                exact_owner.setdefault(_p.replace("\\", "/"), set()).add(_cid)

    matched_by = {}
    for cid, pats in pats_by.items():
        exact_pats = [p for p in pats if "*" not in p and not p.endswith("/")]
        matched = []
        for f in files:
            hit_exact = any(_matches(f, p) for p in exact_pats)
            if hit_exact:
                matched.append(f)
                continue
            # Точный владелец этого пути есть, и это не мы -> путь принадлежит ему, не нам.
            owners = exact_owner.get(f.lstrip("./") if f.startswith("./") else f, set())
            if owners and cid not in owners:
                continue
            if any(_matches(f, p) for p in pats):
                matched.append(f)
        matched_by[cid] = matched

    # Присутствие сигналов нужно ТОЛЬКО там, где ничего не совпало, и считается одним обходом на
    # весь вызов: прежде обход шёл по контуру, до восьми раз за гейт.
    present = signals_present(root, {cid: pats for cid, pats in pats_by.items()
                                     if pats and not matched_by[cid]})

    out = {}
    for cid, pats in pats_by.items():
        if not pats:
            out[cid] = {"state": UNKNOWN, "matched": [], "signals": 0,
                        "reason": "у контура нет сигнальных путей — состояние не определяется"}
        elif matched_by[cid]:
            out[cid] = {"state": CHANGED, "matched": matched_by[cid], "signals": len(pats),
                        "reason": f"изменение затрагивает сигнальные пути контура "
                                  f"({len(matched_by[cid])})"}
        elif cid in present:
            out[cid] = {"state": NOT_CHANGED, "matched": [], "signals": len(pats),
                        "reason": "сигнальные пути контура в репозитории есть и не затронуты"}
        else:
            out[cid] = {"state": UNKNOWN, "matched": [], "signals": len(pats),
                        "reason": "ни один сигнальный путь контура в репозитории не найден — "
                                  "кит не умеет видеть этот контур здесь "
                                  "(доопределить: .ai-ops.yaml -> product_operating_model)"}
    return out


def _sot_touched(child_root: Path, cid: str, changed_files: list, model: dict) -> list:
    """Какие источники истины контура попали в изменение (с учётом оверлея пути)."""
    c = contour(model, cid) or {}
    files = [unquote_git_path(f).replace("\\", "/") for f in (changed_files or [])]
    hit = []
    for s in sot_for(model, cid, child_root):
        rel = (s.get("path") or "").replace("\\", "/")
        if not rel:
            continue
        for f in files:
            f_norm = f
            for pre in _OVERLAYS:
                if pre and f_norm.startswith(pre):
                    f_norm = f_norm[len(pre):]
                    break
            if f_norm == rel or (rel.endswith("/") and f_norm.startswith(rel)):
                hit.append(rel)
                break
    return hit


def reconcile(child_root: Path, declared: dict, changed_files: list,
              model: dict | None = None, overrides: dict | None = None) -> dict:
    """Сверка заявленного `affects` с выведенным из изменения. -> находки гейта.

    `declared` — `affects` из WorkItem: {contour_id: bool} (или {contour_id: "changed"|...}).
    Три класса находок (имена — из `consistency.findings` модели):

      undeclared_change    — diff трогает контур, а WorkItem его не заявил. Источник истины
                             разойдётся молча, и следующая сессия прочтёт неправду.
      declared_not_updated — контур заявлен изменённым, но ни один его источник истины не
                             обновлён. Ровно случай «обновили компонент, модель данных нет».
      unknown_contour      — состояние контура не определяется. Не находка-ошибка, а признание.

    Отсутствие находок при пустом `changed_files` — не «согласовано», а «нечего сверять»:
    поле `comparable` говорит об этом прямо.
    """
    model = model or load_model()
    derived = derive_affects(child_root, changed_files, model, overrides)
    known = set(contour_ids(model))
    decl = {}
    for k, v in (declared or {}).items():
        if isinstance(v, bool):
            decl[k] = CHANGED if v else NOT_CHANGED
        elif isinstance(v, str) and v in (CHANGED, NOT_CHANGED, UNKNOWN):
            decl[k] = v
        else:
            decl[k] = UNKNOWN
    findings = []

    # ЗАЯВЛЕНИЕ ОБ ОПЕЧАТКЕ НЕ ПРОВЕРЯЕТ НИЧЕГО, И МОЛЧАТЬ ОБ ЭТОМ НЕЛЬЗЯ. `affects: {data_contract:
    # true}` (id — `data_contracts`) выглядел как заполненное поле: сверка шла по известным контурам,
    # лишний ключ игнорировался, автор считал, что связь объявлена. Тир 3: опечатка — `major`,
    # потому что она создаёт ЛОЖНУЮ уверенность, а не просто пропуск.
    for k in sorted(set(decl) - known):
        findings.append({"id": "unknown_contour_declared", "contour": k, "severity": "major",
                         "detail": f"в affects заявлен контур '{k}', которого в модели нет "
                                   f"(похоже на опечатку) — это заявление не проверяет ничего; "
                                   f"известные: {', '.join(sorted(known))}"})
    for cid in contour_ids(model):
        d = derived[cid]
        want = decl.get(cid)

        if d["state"] == UNKNOWN:
            findings.append({"id": "unknown_contour", "contour": cid, "severity": "info",
                             "detail": d["reason"]})
            continue

        # ── ГЛАВНАЯ НАХОДКА: ОПИСАНИЕ ОТСТАЛО ОТ КОДА ──────────────────────────────────────────
        # Это ФАКТ, и он не зависит от того, заполнил ли человек `affects`: сигналы контура тронуты,
        # источник истины — нет. Прежде находка требовала заявления, и на документированном пути
        # (`run` создаёт id из хеша задачи, элемента плана с таким id нет) поле было пустым ВСЕГДА —
        # гейт структурно не мог дать не-`pass`. Мутационное ревью поймало это как выжившего мутанта:
        # «файлов 5, включая миграции и CI -> verdict ok».
        if d["state"] == CHANGED:
            touched = _sot_touched(child_root, cid, changed_files, model)
            if not touched:
                sot = [s.get("path") for s in sot_for(model, cid, child_root)]
                findings.append({
                    "id": "source_of_truth_behind", "contour": cid, "severity": "major",
                    "detail": f"изменение трогает {', '.join(d['matched'][:4])}, а описание контура "
                              f"не обновлено (ожидался один из: {', '.join(sot)})"})

        # ── УЧЁТ ЗАЯВЛЕНИЯ: полезно, но это не тот дефект, ради которого гейт существует ─────────
        # `info` осознанно. Незаполненное поле не делает работу несогласованной, а шум на каждой
        # задаче гарантирует, что гейт перестанут читать — модель запрещает это прямо.
        if d["state"] == CHANGED and want != CHANGED:
            findings.append({"id": "undeclared_change", "contour": cid, "severity": "info",
                             "detail": f"контур затронут, но не заявлен в affects "
                                       f"(тронуто: {', '.join(d['matched'][:3])})"})
        if want == CHANGED and d["state"] == NOT_CHANGED:
            findings.append({"id": "declared_not_changed", "contour": cid, "severity": "info",
                             "detail": "контур заявлен изменённым, но ни один его сигнальный путь "
                                       "не тронут — заявление шире изменения"})

    return {"comparable": bool(changed_files), "derived": derived, "declared": decl,
            "findings": findings,
            "verdict": ("ok" if not [f for f in findings if f["severity"] == "major"]
                        else "inconsistent")}


def suggest_affects(model: dict, work_type: str) -> dict:
    """Подсказка `affects` по типу работы: контур, который этот тип меняет ПО ОПРЕДЕЛЕНИЮ.

    Подсказка, а не замена детекту: тип говорит о намерении, diff — о факте, и расходятся они
    регулярно (задача типа `visual` вполне может тронуть контракт API).
    """
    wt = (model.get("work_types") or {}).get(work_type or "") or {}
    cid = wt.get("contour")
    return {cid: True} if cid else {}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="contours.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("state"); s.add_argument("repo"); s.add_argument("--json", action="store_true")
    a = sub.add_parser("affects"); a.add_argument("repo")
    a.add_argument("--files", default="", help="пути через запятую (git diff --name-only)")
    a.add_argument("--json", action="store_true")
    rc = sub.add_parser("reconcile"); rc.add_argument("repo")
    rc.add_argument("--files", default="", help="пути через запятую (git diff --name-only)")
    rc.add_argument("--workitem", help="features/<id>/workitem.yaml — источник объявленного affects")
    rc.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])
    root = Path(ns.repo)
    model = load_model()

    if ns.cmd == "state":
        st = sot_state(root, model)
        if ns.json:
            print(json.dumps(st, ensure_ascii=False, indent=2)); return 0
        gaps = 0
        for cid, v in st.items():
            mark = "✓" if v["ok"] else "✗"
            print(f"{mark} {v['title']} ({cid}) · роль {v['owner_role']}")
            print(f"    {v['question']}")
            if v["required_missing"]:
                gaps += 1
                print(f"    ⚠ нет обязательного источника истины: {', '.join(v['required_missing'])}")
        print(f"\nКОНТУРЫ: {len(st) - gaps}/{len(st)} с источником истины")
        return 0

    files = [x.strip() for x in (ns.files or "").split(",") if x.strip()]

    if ns.cmd == "reconcile":
        # Объявленный `affects` берём из WorkItem'а; его отсутствие — НЕ «ничего не меняем», а
        # «ещё не определяли», и находка `undeclared_change` про это и скажет.
        declared = {}
        if ns.workitem:
            wp = Path(ns.workitem)
            if wp.is_file():
                try:
                    declared = (yaml.safe_load(wp.read_text(encoding="utf-8")) or {}).get("affects") or {}
                except yaml.YAMLError:
                    declared = {}
        rep = reconcile(root, declared, files, model)
        if ns.json:
            print(json.dumps(rep, ensure_ascii=False, indent=2)); return 0
        print(f"СВЯЗНОСТЬ КОНТУРОВ: {rep['verdict']} · сверяемо {rep['comparable']}")
        for f in rep["findings"]:
            print(f"  {f['severity']:6} {f['id']} / {f['contour']}: {f['detail']}")
        # Гейт advisory: несогласованность НЕ роняет код возврата, но и не молчит.
        return 0

    der = derive_affects(root, files, model)
    if ns.json:
        print(json.dumps(der, ensure_ascii=False, indent=2)); return 0
    for cid, v in der.items():
        print(f"{v['state']:12} {cid} — {v['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
