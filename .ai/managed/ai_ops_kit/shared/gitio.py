#!/usr/bin/env python3
"""Единый git-хелпер (v3.0.13, блок C самоаудита) — ОДИН источник вызова git для tools/.

Прежде идентичная функция `_git` (rc, stdout.strip(), stderr.strip()) была скопирована в 7 модулях,
и НИ ОДНА не задавала timeout: зависший git-субпроцесс (сеть/lock/hook) вешал весь прогон навсегда.
Здесь — один вызов с таймаутом по умолчанию; при таймауте возвращается rc=124 (соглашение GNU timeout)
и понятный stderr, а не блокировка.

CLI: gitio.py --selftest
"""
from __future__ import annotations

import argparse
import subprocess
import sys

GIT_TIMEOUT_DEFAULT = 90   # сек: обычные plumbing-команды завершаются мгновенно; потолок против зависаний


def git(root, *args, timeout=GIT_TIMEOUT_DEFAULT):
    """git -C <root> <args...> -> (returncode, stdout.strip(), stderr.strip()). Таймаут -> (124, '', reason)."""
    try:
        r = subprocess.run(["git", "-C", str(root), *args],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", f"git timeout {timeout}s: {' '.join(str(a) for a in args)[:120]}"
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def committed_changed_files(root, sha):
    """Файлы, изменённые коммитом sha относительно его первого родителя. -> [path] (пусто при ошибке).

    ПЕРЕЕХАЛА СЮДА 2026-08-12 из `engine/pipeline_git.py`, где называлась `_committed_changed_files`
    и была ПРИВАТНОЙ — а `gates/regression_evidence` импортировал её по приватному имени через
    границу пакета. Такой доступ не описан ни одним интерфейсом: он и есть та неявная связность,
    из-за которой менять кусок системы небезопасно — локального контекста не хватает, чтобы узнать,
    кто ещё зависит от твоего `_`-имени. Функция — чистый git-запрос, её место рядом с остальными
    обёртками над git.

    `-z` ОБЯЗАТЕЛЕН, а не украшение. При `core.quotePath` (включён по умолчанию) git отдаёт
    не-ASCII имена в escape-кавычках: `"context/product/\320\236..."`. Такой путь не совпадает ни с
    одним сигнальным паттерном, и гейт связности превращал `changed` в `not_changed` — утверждение
    вместо признания. Для русскоязычного продукта это отменяло гейт целиком. `-z` отдаёт имена как
    есть, разделённые NUL, и попутно снимает вторую дыру: путь с переводом строки или запятой больше
    не распадается.
    """
    if not sha:
        return []
    rc, out, _ = git(root, "diff", "--name-only", "-z", f"{sha}~1", sha)
    if rc != 0:
        rc, out, _ = git(root, "show", "--name-only", "-z", "--pretty=format:", sha)
    if rc != 0:
        return []
    return [ln for ln in out.split("\0") if ln.strip()]


def main(argv):
    ap = argparse.ArgumentParser(prog="gitio.py")
    ap.add_argument("--selftest", action="store_true")
    ap.parse_args(argv)          # разбор ради проверки аргументов; результат не нужен
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
