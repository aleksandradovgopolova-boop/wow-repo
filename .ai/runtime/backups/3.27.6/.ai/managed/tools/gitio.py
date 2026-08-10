#!/usr/bin/env python3
"""Единый git-хелпер (v3.0.13, блок C самоаудита) — ОДИН источник вызова git для tools/.

Прежде идентичная функция `_git` (rc, stdout.strip(), stderr.strip()) была скопирована в 7 модулях,
и НИ ОДНА не задавала timeout: зависший git-субпроцесс (сеть/lock/hook) вешал весь прогон навсегда.
Здесь — один вызов с таймаутом по умолчанию; при таймауте возвращается rc=124 (соглашение GNU timeout)
и понятный stderr, а не блокировка.

CLI: gitio.py --selftest
"""

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


def selftest():
    import tempfile
    from pathlib import Path
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        rc, out, err = git(root, "rev-parse", "--is-inside-work-tree")
        expect("git: не-репо -> rc!=0 (кортеж (rc,out,err))", rc != 0 and isinstance(out, str) and isinstance(err, str))
        for a in (("init", "-q"), ("config", "user.email", "t@t"), ("config", "user.name", "t")):
            git(root, *a)
        (root / "f").write_text("x", encoding="utf-8")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "i")
        rc2, out2, _ = git(root, "rev-parse", "--abbrev-ref", "HEAD")
        expect("git: rev-parse ветки -> rc=0 + непустой stdout.strip()", rc2 == 0 and bool(out2))
        # таймаут: невозможная задержка отдаёт rc=124, а не висит (git не запускается на левой команде,
        # но проверяем контракт таймаута на заведомо быстрой команде с крошечным лимитом косвенно нельзя —
        # проверяем лишь, что параметр timeout принимается и обычная команда укладывается)
        rc3, _, _ = git(root, "status", "--porcelain", timeout=30)
        expect("git: timeout-параметр принят, быстрая команда укладывается (rc=0)", rc3 == 0)

    print("gitio selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    ap = argparse.ArgumentParser(prog="gitio.py")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
