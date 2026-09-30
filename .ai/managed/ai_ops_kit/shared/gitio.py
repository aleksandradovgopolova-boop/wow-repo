#!/usr/bin/env python3
"""Единый git-хелпер (v3.0.13, блок C самоаудита) — ОДИН источник вызова git для `ai_ops_kit/`.

Прежде идентичная функция `_git` (rc, stdout.strip(), stderr.strip()) была скопирована в 7 модулях,
и НИ ОДНА не задавала timeout: зависший git-субпроцесс (сеть/lock/hook) вешал весь прогон навсегда.
Здесь — один вызов с таймаутом по умолчанию; при таймауте возвращается rc=124 (соглашение GNU timeout)
и понятный stderr, а не блокировка.

Плоский слой `tools/` снят в 4.0 — единственный вход к git теперь пакет `ai_ops_kit/`, а инвариант
«ни одного неограниченного git-субпроцесса» держит структурный тест `tests/contracts/test_no_unbounded_git.py`.

`git(root, *args)` — форма `git -C <root> <args...>`; `run(args, cwd=…)` — остальные формы (clone,
команды с `cwd=` вместо `-C`), которым `-C <root>` не подходит. Обе стягивают stdout/stderr через
`.strip()`; где вывод нужен ДОСЛОВНО (`git show <ref>:<path>`) или значима ведущая колонка
(`git status --porcelain`), вызывай git raw с явным `timeout=` — так задумано, тест это допускает.

CLI: gitio.py --selftest
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

GIT_TIMEOUT_DEFAULT = 90   # сек: обычные plumbing-команды завершаются мгновенно; потолок против зависаний


def run(args: list[str], *, cwd: str | Path | None = None, timeout: int = GIT_TIMEOUT_DEFAULT) -> tuple[int, str, str]:
    """git <args...> (без подстановки `-C`) -> (returncode, stdout.strip(), stderr.strip()).

    Для форм, не сводимых к `git -C <root>`: `git clone <src> <dst>` и команды, задающие рабочий
    каталог через `cwd=`, а не `-C`. Таймаут по умолчанию тот же, что у `git`; при таймауте —
    (124, '', reason), а не блокировка."""
    try:
        r = subprocess.run(["git", *args], cwd=(str(cwd) if cwd is not None else None),
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", f"git timeout {timeout}s: {' '.join(str(a) for a in args)[:120]}"
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def git(root: str | Path, *args: str, timeout: int = GIT_TIMEOUT_DEFAULT) -> tuple[int, str, str]:
    """git -C <root> <args...> -> (returncode, stdout.strip(), stderr.strip()). Таймаут -> (124, '', reason)."""
    return run(["-C", str(root), *args], timeout=timeout)


def committed_changed_files(root: str | Path, sha: str) -> list[str]:
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


def resolve_base(root: str | Path, base_ref: str | None) -> dict:
    """Разрешение base-ветки. ТОЛЬКО ветка (локальная/origin), не tag/SHA.

    ПЕРЕЕХАЛА СЮДА 2026-09-10 из `engine/pipeline_git.py` (`_resolve_base`, приватная): её звали
    по приватному имени ЧЕРЕЗ границу пакета и engine (review), и lifecycle (active_work) — та же
    неявная связность, что описана у `committed_changed_files`. Резолвер базы — чистый git-запрос,
    его место рядом с остальными обёртками. `pipeline_git._resolve_base` оставлен алиасом для
    внутренних вызовов engine (K5-развязка engine<->lifecycle).

    Два режима:
    * base_ref=None -> AUTO: текущая ветка -> upstream (@{u}) -> remote default (origin/HEAD).
      Никакого хардкода 'main'. Порядок «текущая ветка первой» — F-014: работа продолжается с того
      места, где стоит пользователь. Не резолвится только detached HEAD без upstream/origin/HEAD.
    * base_ref задан -> EXPLICIT: обязана существовать (refs/heads/<ref> или origin/<ref>); иначе
      resolved=False (вызывающий обязан заблокировать прогон ДО модели — не выполнять от HEAD).
    -> {base_ref, base_sha, source, mode, resolved, reason}."""
    if git(root, "rev-parse", "--is-inside-work-tree")[0] != 0:
        return {"base_ref": base_ref, "resolved": False, "mode": "explicit" if base_ref else "auto",
                "reason": "не git-репозиторий"}
    if base_ref:   # EXPLICIT — строго ветка
        rc_l, sha_l, _ = git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{base_ref}")
        if rc_l == 0 and (sha_l or "").strip():
            return {"base_ref": base_ref, "base_sha": sha_l.strip(), "source": "explicit-local",
                    "mode": "explicit", "resolved": True}
        rc_r, sha_r, _ = git(root, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base_ref}")
        if rc_r == 0 and (sha_r or "").strip():
            return {"base_ref": base_ref, "base_sha": sha_r.strip(), "source": "explicit-remote",
                    "mode": "explicit", "resolved": True}
        return {"base_ref": base_ref, "resolved": False, "mode": "explicit",
                "reason": f"явная base '{base_ref}' не найдена ни локально (refs/heads), ни в origin"}
    # AUTO: текущая ветка -> upstream -> remote default (F-014: ветка пользователя первой).
    rc_c, cur, _ = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    cur = (cur or "").strip()
    if rc_c == 0 and cur and cur != "HEAD":      # 'HEAD' = detached: имя ветки не получено
        rc_h, head, _ = git(root, "rev-parse", "--verify", "--quiet", "HEAD")
        if rc_h == 0 and (head or "").strip():
            return {"base_ref": cur, "base_sha": head.strip(), "source": "current-branch",
                    "mode": "auto", "resolved": True}
    rc_u, up, _ = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if rc_u == 0 and (up or "").strip():
        ref = up.strip()
        rc_s, sha, _ = git(root, "rev-parse", "--verify", "--quiet", ref)
        if rc_s == 0 and (sha or "").strip():
            br = ref.split("origin/", 1)[1] if ref.startswith("origin/") else ref
            return {"base_ref": br, "base_sha": sha.strip(), "source": "upstream",
                    "mode": "auto", "resolved": True}
    rc_d, dref, _ = git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if rc_d == 0 and (dref or "").strip():
        rc_s, sha, _ = git(root, "rev-parse", "--verify", "--quiet", dref.strip())
        if rc_s == 0 and (sha or "").strip():
            br = dref.strip().split("refs/remotes/origin/", 1)[-1]
            return {"base_ref": br, "base_sha": sha.strip(), "source": "remote-default",
                    "mode": "auto", "resolved": True}
    # сюда попадаем только при detached HEAD без upstream и без origin/HEAD. Честный отказ:
    # 'HEAD' не ветка, а контракт резолвера требует именно ветку.
    if cur == "HEAD":
        return {"base_ref": None, "resolved": False, "mode": "auto",
                "reason": "detached HEAD без upstream и origin/HEAD — база не ветка; "
                          "переключись на ветку или задай --base <ветка>"}
    return {"base_ref": None, "resolved": False, "mode": "auto",
            "reason": "не удалось определить base автоматически (нет текущей ветки/upstream/remote-default)"}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="gitio.py")
    ap.add_argument("--selftest", action="store_true")
    ap.parse_args(argv)          # разбор ради проверки аргументов; результат не нужен
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
