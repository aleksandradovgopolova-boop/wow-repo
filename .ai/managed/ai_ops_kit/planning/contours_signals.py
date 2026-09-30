#!/usr/bin/env python3
"""Фундамент контуров: матчинг путей и детект сигналов в репозитории (сателлит `contours.py`).

Низкоуровневый слой, на который опирается фасад `contours.py`: сопоставление пути с globом
(`_matches`), нормализация имён из git (`unquote_git_path`), обход продуктовых каталогов с
подрезкой (`_product_dirs`) и ответ на вопрос «есть ли в репозитории сигнальный путь контура»
(`_repo_has_signal`, `signals_present`). Фундамент НЕ зовёт верхние функции фасада: единственная
связь наверх — ленивый импорт `_resolve` из `contours` внутри функции (учёт project/custom-оверлея
источников истины), чтобы не образовать обратного ребра импорта на уровне модуля.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path

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
    from ai_ops_kit.planning.contours import _resolve   # оверлей источников истины — в фасаде
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
    from ai_ops_kit.planning.contours import _resolve   # оверлей источников истины — в фасаде
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
