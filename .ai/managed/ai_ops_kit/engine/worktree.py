#!/usr/bin/env python3
"""Изоляция работы по WorkItem через git worktree (v2.24) — параллельные сессии.

Чтобы несколько сессий не мешали друг другу в одном рабочем дереве, каждая работа
получает свой git worktree (свой рабочий каталог + своя ветка), а не работает в main.
Это реальная git-операция (не поведение рантайма): инструмент выполняет
`git worktree add/list/remove`. Само «сессия автоматически берёт свой worktree» —
шаг рантайма (через ai-start-task), а изоляция файлов — здесь и сейчас.

Каталог по умолчанию: <root>/.ai/worktrees/<id> (в .gitignore держите .ai/worktrees/).

Использование:
  worktree.py add    <id> --branch B [--base HEAD] [--root .] [--dir .ai/worktrees] [--json]
  worktree.py list   [--root .] [--json]
  worktree.py remove <id> [--root .] [--dir .ai/worktrees] [--force]
  worktree.py --selftest
Возврат 0 — ок, 1 — ошибка.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _git(root, *args):
    from ai_ops_kit.shared import gitio
    return gitio.git(root, *args)   # v3.0.13 (блок C): единый git-хелпер с таймаутом


def _safe_target(root, wt_dir, wid):
    """Резолвит <root>/<wt_dir>/<wid> и требует, чтобы результат лежал ВНУТРИ root.

    finding аудита (P1.1): wid и wt_dir доходят до пути. `../`, абсолютный путь,
    разделители — traversal за пределы репо. Возвращает (target, None) при успехе
    или (None, сообщение_об_ошибке).
    """
    root = Path(root).resolve()
    base = (root / wt_dir).resolve()
    target = (base / str(wid)).resolve()
    # Требуем, чтобы worktree лежал СТРОГО внутри <root>/<wt_dir> — не только внутри root.
    # Так `../escape` (уводит в root/.ai/escape) тоже отвергается, а не только уход за root.
    for anchor, label in ((base, f"каталога worktree {base}"), (root, f"корня {root}")):
        try:
            target.relative_to(anchor)
        except ValueError:
            return None, (f"недопустимый worktree-путь {target} вне {label} "
                          f"(id={wid!r}, dir={wt_dir!r}): traversal запрещён")
    if target in (root, base):
        return None, f"worktree-путь совпадает с корнем/базой ({target}): id/dir не должны быть пустыми"
    return target, None


def _branch_exists(root, branch):
    rc, _, _ = _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    return rc == 0


def add(root, wid, branch, base="HEAD", wt_dir=".ai/worktrees", as_json=False):
    root = Path(root).resolve()
    target, err = _safe_target(root, wt_dir, wid)
    if err:
        print(f"ОШИБКА: {err}", file=sys.stderr)
        return 1
    if target.exists():
        print(f"ОШИБКА: каталог worktree уже есть: {target}", file=sys.stderr)
        return 1
    if not branch:
        print("ОШИБКА: нужна ветка (--branch); работа не ведётся в main.", file=sys.stderr)
        return 1
    if branch in ("main", "master"):
        print("ОШИБКА: worktree для main/master не создаём — задайте рабочую ветку.", file=sys.stderr)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    if _branch_exists(root, branch):
        rc, out, err = _git(root, "worktree", "add", str(target), branch)
    else:
        rc, out, err = _git(root, "worktree", "add", str(target), "-b", branch, base)
    if rc != 0:
        print(f"ОШИБКА git worktree add: {err or out}", file=sys.stderr)
        return 1
    rel = target.relative_to(root)
    if as_json:
        print(json.dumps({"id": wid, "branch": branch, "path": str(rel)}, ensure_ascii=False))
    else:
        # операционный прогресс -> stderr, чтобы --json оставался машиночитаемым (stdout = только данные)
        print(f"WORKTREE: '{wid}' -> {rel} (ветка {branch}). "
              f"Работайте в этом каталоге; main не трогается.", file=sys.stderr)
    return 0


def _parse_list(porcelain):
    trees, cur = [], {}
    for line in porcelain.splitlines():
        if not line.strip():
            if cur:
                trees.append(cur); cur = {}
            continue
        if line.startswith("worktree "):
            cur["path"] = line[len("worktree "):]
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch "):].replace("refs/heads/", "")
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD "):]
    if cur:
        trees.append(cur)
    return trees


def list_cmd(root, as_json=False):
    rc, out, err = _git(root, "worktree", "list", "--porcelain")
    if rc != 0:
        print(f"ОШИБКА git worktree list: {err or out}", file=sys.stderr)
        return 1
    trees = _parse_list(out)
    if as_json:
        print(json.dumps({"schema_version": 1, "kind": "worktree-list", "worktrees": trees},
                         ensure_ascii=False, indent=2))
        return 0
    print(f"WORKTREE: {len(trees)} рабочих деревьев:", file=sys.stderr)
    for t in trees:
        print(f"  - {t.get('path')} (ветка {t.get('branch', '?')})", file=sys.stderr)
    return 0


def remove(root, wid, wt_dir=".ai/worktrees", force=False):
    root = Path(root).resolve()
    target, err = _safe_target(root, wt_dir, wid)
    if err:
        print(f"ОШИБКА: {err}", file=sys.stderr)
        return 1
    args = ["worktree", "remove", str(target)]
    if force:
        args.append("--force")
    rc, out, err = _git(root, *args)
    if rc != 0:
        print(f"ОШИБКА git worktree remove: {err or out}", file=sys.stderr)
        return 1
    print(f"WORKTREE: '{wid}' удалён ({target.relative_to(root)}). Ветка сохранена.", file=sys.stderr)
    return 0


def resolve_worktree_path(root, wid, wt_dir=".ai/worktrees"):
    """Путь worktree для wid внутри root. -> (Path, None) | (None, ошибка). Тонкая обёртка _safe_target,
    чтобы оркестратору не парсить stdout `add`."""
    return _safe_target(root, wt_dir, wid)


def apply_fixes_in_worktree(root, branch, fixers, *, base="HEAD", verify=None,
                            wt_dir=".ai/worktrees", max_files=50,
                            commit_message="chore: nightly autofix (class A)", cleanup=True):
    """Применить ДЕТЕРМИНИРОВАННЫЕ фиксеры в ИЗОЛИРОВАННОМ worktree на не-main ветке -> один коммит.

    Чистый git-примитив: не импортирует intelligence, ничего не знает о ночном ревью. Фиксеры
    ПЕРЕДАЮТСЯ (не импортируются) — список dict `{"key": str, "apply": callable(Path)->list[str]}`,
    где apply правит файлы в worktree и возвращает изменённые относительные пути. Каждый фиксер
    ОБЯЗАН быть класса A (детерминированный, обратимый, не меняющий поведение).

    После каждого фиксера, если задан `verify(worktree_path)->bool`, он ДОЛЖЕН вернуть True — иначе
    изменения этого фиксера ОТКАТЫВАЮТСЯ (git checkout -- <пути>), фиксер помечается skipped. Так
    «не прошло проверку» не доезжает до PR. Ничего не удаляет; никогда не пишет в main (add это
    запрещает). Пустой результат -> worktree снят, {status:"no_changes"}. Жёсткий сбой -> worktree
    снят force, {status:"rolled_back"}. Иначе -> коммит + точные base/head SHA.

    Возврат: {status, branch, base_sha?, head_sha?, applied, skipped, changed_files, reason?}."""
    root = Path(root).resolve()
    wid = str(branch).replace("/", "-")
    wt_path, err = _safe_target(root, wt_dir, wid)
    if err:
        return {"status": "error", "branch": branch, "reason": err,
                "applied": [], "skipped": [], "changed_files": []}
    rc = add(root, wid, branch, base=base, wt_dir=wt_dir)
    if rc != 0:
        return {"status": "error", "branch": branch, "reason": "git worktree add не удался",
                "applied": [], "skipped": [], "changed_files": []}
    applied, skipped, changed_all = [], [], []
    try:
        base_rc, base_out, _ = _git(wt_path, "rev-parse", base)
        base_sha = base_out.strip() if base_rc == 0 else None
        for f in fixers:
            key = f.get("key", "fixer")
            try:
                touched = list(f["apply"](wt_path) or [])
            except Exception as e:  # noqa: BLE001 — фиксер = произвольный код; сбой любого рода откатываем, не роняя прогон
                _git(wt_path, "checkout", "--", ".")
                skipped.append({"key": key, "reason": f"фиксер упал ({type(e).__name__}: {e})"})
                continue
            if not touched:
                continue
            if len(changed_all) + len(touched) > max_files:
                _git(wt_path, "checkout", "--", *touched)
                skipped.append({"key": key, "reason": f"потолок файлов ({max_files}) — фиксер пропущен"})
                continue
            if verify is not None and not verify(wt_path):
                # проверка не прошла -> откат ИМЕННО его изменений, фиксер skipped (не в PR)
                _git(wt_path, "checkout", "--", *touched)
                skipped.append({"key": key, "reason": "проверка после фиксера не прошла — откат"})
                continue
            _git(wt_path, "add", *touched)
            applied.append({"key": key, "files": sorted(touched)})
            changed_all.extend(touched)
        if not changed_all:
            if cleanup:
                remove(root, wid, wt_dir=wt_dir, force=True)
            return {"status": "no_changes", "branch": branch, "base_sha": base_sha,
                    "applied": applied, "skipped": skipped, "changed_files": []}
        crc, cout, cerr = _git(wt_path, "commit", "-m", commit_message)
        if crc != 0:
            remove(root, wid, wt_dir=wt_dir, force=True)
            return {"status": "rolled_back", "branch": branch, "reason": f"commit не удался: {cerr or cout}",
                    "applied": applied, "skipped": skipped, "changed_files": sorted(set(changed_all))}
        hrc, hout, _ = _git(wt_path, "rev-parse", "HEAD")
        head_sha = hout.strip() if hrc == 0 else None
    except Exception as e:  # noqa: BLE001 — safety net: любой жёсткий сбой -> полный откат worktree, не осиротить
        remove(root, wid, wt_dir=wt_dir, force=True)
        return {"status": "rolled_back", "branch": branch, "reason": f"{type(e).__name__}: {e}",
                "applied": applied, "skipped": skipped, "changed_files": sorted(set(changed_all))}
    if cleanup:
        remove(root, wid, wt_dir=wt_dir, force=True)     # ветка с коммитом остаётся, каталог убран
    return {"status": "committed", "branch": branch, "base_sha": base_sha, "head_sha": head_sha,
            "applied": applied, "skipped": skipped, "changed_files": sorted(set(changed_all))}


def main(argv):
    ap = argparse.ArgumentParser(prog="worktree.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add")
    a.add_argument("id"); a.add_argument("--branch", required=True)
    a.add_argument("--base", default="HEAD"); a.add_argument("--root", default=".")
    a.add_argument("--dir", default=".ai/worktrees"); a.add_argument("--json", action="store_true")

    l = sub.add_parser("list")
    l.add_argument("--root", default="."); l.add_argument("--json", action="store_true")

    r = sub.add_parser("remove")
    r.add_argument("id"); r.add_argument("--root", default=".")
    r.add_argument("--dir", default=".ai/worktrees"); r.add_argument("--force", action="store_true")

    ns = ap.parse_args(argv)
    if ns.cmd == "add":
        return add(ns.root, ns.id, ns.branch, ns.base, ns.dir, ns.json)
    if ns.cmd == "list":
        return list_cmd(ns.root, ns.json)
    if ns.cmd == "remove":
        return remove(ns.root, ns.id, ns.dir, ns.force)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
