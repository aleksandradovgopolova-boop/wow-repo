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


def main(argv):
    ap = argparse.ArgumentParser(prog="gitio.py")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
