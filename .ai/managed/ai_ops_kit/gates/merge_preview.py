#!/usr/bin/env python3
"""Merge-preview footprint — ратчет объёма против ИТОГА СЛИЯНИЯ, а не ветки PR (фордж-нейтрально).

Работа `gate-measures-merge-result` (04.09.2026).

ПОВОД — ЗАМЕР. Гейты объёма меряют ВЕТКУ PR. Код, который параллельные ленты складывают в main,
дрейфует выше порога незаметно и всплывает падением на СЛЕДУЮЩЕМ PR, а не на том, кто дрейф создал
(footprint main тихо ушёл 490 -> 498 файлов между слияниями). Это тот же класс, что «мерим очередь,
а не размер»: наказан следующий пришедший, а не виновник.

GitHub merge queue строит временный merge-коммит и меряет пороги НА НЁМ — но она недоступна для
личных репозиториев и привязала бы кит к фиче форджа. Механизм ниже держит тот же инвариант САМ, на
чистом git: считает ДЕРЕВО СЛИЯНИЯ base+head без коммита через `git merge-tree --write-tree`
(git 2.38+), материализует его и меряет объём ИТОГА, а не ветки.

ЧЕСТНАЯ ГРАНИЦА ОХВАТА. Здесь суммируются байты ВСЕГО дерева слияния (кроме .git) — это надмножество
доставляемого footprint'а (managed_set в installer), а не он сам: модуль в слое gates/ не вправе
тянуть installer/validation/delivery вверх, поэтому измерение самодостаточно (обход файлов + сумма
st_size), а не переиспользует расчёт поставки. Поэтому механизм — СИГНАЛ дрейфа против итога слияния,
не точный учёт поставки; на пути PR он advisory, промоутнёт его владелец отдельным решением.

FAIL-CLOSED. Не смогли посчитать итог (конфликт слияния, ошибка git, git < 2.38) -> НЕ зелёный:
breached считается непройденным. «Посчитать не смогли» никогда не выдаётся за «в пределах».

git — ТОЛЬКО через `ai_ops_kit.shared.gitio.git` (таймаут + rc-контракт), tar — через stdlib
`tarfile`; бинарный tar пишется в файл (`archive -o`), а не гонится через text-обёртку gitio.

CLI:  python3 -m ai_ops_kit.gates.merge_preview --base origin/main --head HEAD [--root .]
      exit 0 — итог слияния в пределах потолка; exit 1 — пробой ИЛИ превью не удалось.
"""
from __future__ import annotations

import argparse
import os
import sys
import tarfile
import tempfile
from pathlib import Path

import yaml

from ai_ops_kit.shared import gitio

BUDGET_REL = "quality/delivery-budget.yaml"
MIN_GIT = (2, 38)   # --write-tree у merge-tree появился в git 2.38


def _git_version(root) -> tuple | None:
    """(major, minor) установленного git или None, если не разобрали."""
    rc, out, _ = gitio.git(root, "version")
    if rc != 0:
        return None
    for tok in out.split():
        parts = tok.split(".")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return int(parts[0]), int(parts[1])
    return None


def merge_preview_tree(root, base_ref: str, head_ref: str) -> dict:
    """Дерево слияния base_ref+head_ref БЕЗ коммита. -> {"ok": True, "tree": sha} | {"ok": False, ...}.

    Чистое слияние -> ok с SHA дерева верхнего уровня. Конфликт или ошибка git -> ok=False с причиной
    (fail-closed). git < 2.38 (нет --write-tree) -> ok=False с явной причиной, без падения.
    """
    ver = _git_version(root)
    if ver is not None and ver < MIN_GIT:
        return {"ok": False, "reason": "нужен git 2.38+ для merge-preview"}
    rc, out, err = gitio.git(root, "merge-tree", "--write-tree", "--no-messages",
                             base_ref, head_ref)
    if rc == 0:
        tree = out.splitlines()[0].strip() if out else ""
        if not tree:
            return {"ok": False, "reason": "git merge-tree не вернул дерево"}
        return {"ok": True, "tree": tree}
    if rc == 1:
        # rc=1 у merge-tree --write-tree — это КОНФЛИКТ (дерево есть, но со stage'ами).
        return {"ok": False, "reason": "конфликт слияния: итог не является чистым деревом"}
    reason = err or f"git merge-tree завершился с кодом {rc}"
    if "--write-tree" in reason or "usage" in reason.lower():
        reason = "нужен git 2.38+ для merge-preview"
    return {"ok": False, "reason": reason}


def merge_preview_entries(root, tree: str) -> list:
    """Файлы дерева-итога слияния `tree` со своими размерами, БЕЗ материализации архива.

    Читает дерево через `git ls-tree -r --long -z <tree>`: плоский рекурсивный обход, размер каждого
    blob'а из индекса git, NUL-разделение записей. `-z` обязателен, а не украшение — при core.quotePath
    (включён по умолчанию) не-ASCII путь пришёл бы в escape-кавычках и не совпал бы с относительным
    путём managed_set; NUL отдаёт имена как есть, и путь с переводом строки/запятой не распадается.

    -> список (relative_path, size_bytes) для каждого blob'а дерева. Gitlink'и/подмодули (у них в
    поле размера стоит `-`, а не число) пропускаются.

    Пустой список означает ЛИБО пустое дерево, ЛИБО что git не смог его прочитать — вызыватель обязан
    отличать «нет доставляемых файлов» от «превью не удалось» ПО РЕЗУЛЬТАТУ `merge_preview_tree`
    (fail-closed решается там, где считается итог), а не по длине этого списка.
    """
    rc, out, _ = gitio.git(root, "ls-tree", "-r", "--long", "-z", tree)
    if rc != 0 or not out:
        return []
    entries = []
    for record in out.split("\0"):
        if not record or "\t" not in record:
            continue
        meta, path = record.split("\t", 1)
        fields = meta.split()
        # Формат --long: `<mode> <type> <sha> <size>` — размер только у blob'ов, у gitlink'ов `-`.
        if len(fields) < 4 or fields[1] != "blob" or not fields[3].isdigit():
            continue
        entries.append((path, int(fields[3])))
    return entries


def _tree_bytes(root, tree: str) -> int:
    """Сумма байтов всех файлов дерева `tree` (кроме .git). Материализует дерево во временный
    каталог через `git archive -o <tar>` + stdlib tarfile, обходит файлы, суммирует st_size."""
    with tempfile.TemporaryDirectory(prefix="ai-ops-merge-preview-") as tmp:
        tar_path = os.path.join(tmp, "tree.tar")
        rc, _, err = gitio.git(root, "archive", "--format=tar", "-o", tar_path, tree)
        if rc != 0:
            raise RuntimeError(err or f"git archive завершился с кодом {rc}")
        dest = os.path.join(tmp, "materialized")
        os.makedirs(dest, exist_ok=True)
        with tarfile.open(tar_path) as t:
            t.extractall(dest)   # noqa: S202 — дерево из СВОЕГО git-репозитория, не внешний архив
        total = 0
        for cur, _dirs, files in os.walk(dest):
            if cur == os.path.join(dest, ".git") or (os.sep + ".git") in cur:
                continue
            for name in files:
                fp = os.path.join(cur, name)
                if os.path.isfile(fp) and not os.path.islink(fp):
                    total += os.path.getsize(fp)
        return total


def _volume_ceiling(root) -> int | None:
    """Потолок volume_bytes из quality/delivery-budget.yaml (ДАННЫЕ, не импорт). None — не прочитан."""
    budget = Path(root) / BUDGET_REL
    try:
        data = yaml.safe_load(budget.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    ceiling = (data or {}).get("ceilings", {}).get("volume_bytes")
    return ceiling if isinstance(ceiling, int) else None


def measure_merge_footprint(root, base_ref: str, head_ref: str) -> dict:
    """Объём ИТОГА слияния base_ref+head_ref против потолка volume_bytes.

    -> {"ok": bool, "merged_bytes": int|None, "ceiling": int|None, "breached": bool, "reason": str}.

    FAIL-CLOSED: превью не удалось (конфликт/ошибка git/старый git), потолок не прочитан или архив не
    материализовался -> ok=False и breached=True (посчитать итог не смогли — значит НЕ зелёный).
    """
    ceiling = _volume_ceiling(root)
    if ceiling is None:
        return {"ok": False, "merged_bytes": None, "ceiling": None, "breached": True,
                "reason": f"не прочитан потолок volume_bytes из {BUDGET_REL}"}
    preview = merge_preview_tree(root, base_ref, head_ref)
    if not preview.get("ok"):
        return {"ok": False, "merged_bytes": None, "ceiling": ceiling, "breached": True,
                "reason": preview.get("reason", "merge-preview не удалось")}
    try:
        merged_bytes = _tree_bytes(root, preview["tree"])
    except (RuntimeError, OSError, tarfile.TarError) as exc:
        return {"ok": False, "merged_bytes": None, "ceiling": ceiling, "breached": True,
                "reason": f"не удалось материализовать дерево слияния: {exc}"}
    breached = merged_bytes > ceiling   # СЕРДЦЕ ПРОВЕРКИ: итог слияния против потолка
    return {"ok": True, "merged_bytes": merged_bytes, "ceiling": ceiling,
            "breached": breached, "reason": ""}


def _format_line(result: dict, base_ref: str, head_ref: str) -> str:
    """Человекочитаемая строка вывода CLI."""
    if not result.get("ok"):
        return (f"MERGE-PREVIEW: посчитать итог слияния {base_ref}+{head_ref} НЕ УДАЛОСЬ — "
                f"{result.get('reason')}. Fail-closed: не зелёный.")
    merged, ceiling = result["merged_bytes"], result["ceiling"]
    reserve = ceiling - merged
    verdict = "ПРОБОЙ" if result["breached"] else "в пределах"
    return (f"MERGE-PREVIEW ({base_ref}+{head_ref}): итог слияния {merged} Б, потолок {ceiling} Б, "
            f"запас {reserve} Б — {verdict}. Меряется ДЕРЕВО СЛИЯНИЯ, а не ветка PR.")


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="merge_preview.py",
                                 description="Ратчет объёма против итога слияния (git merge-tree).")
    ap.add_argument("--base", required=True, help="базовая ветка (например origin/main)")
    ap.add_argument("--head", default="HEAD", help="ветка PR (по умолчанию HEAD)")
    ap.add_argument("--root", default=".", help="корень репозитория (по умолчанию .)")
    args = ap.parse_args(argv)
    result = measure_merge_footprint(args.root, args.base, args.head)
    print(_format_line(result, args.base, args.head))
    # exit 1 при пробое ИЛИ если превью не удалось (fail-closed); 0 — итог в пределах.
    return 0 if (result.get("ok") and not result.get("breached")) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
