#!/usr/bin/env python3
"""Git-related helpers for the execution pipeline.

Extracted from execution_pipeline.py to keep git operations isolated.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402
def _git(root, *args):
    from ai_ops_kit.engine import gitio
    return gitio.git(root, *args)   # v3.0.13 (блок C): единый git-хелпер с таймаутом


def _committed_changed_files(root, sha):
    """Файлы, изменённые коммитом sha относительно его первого родителя. -> [path] (пусто при ошибке)."""
    if not sha:
        return []
    rc, out, _ = _git(root, "diff", "--name-only", f"{sha}~1", sha)
    if rc != 0:
        rc, out, _ = _git(root, "show", "--name-only", "--pretty=format:", sha)
    return [ln for ln in out.splitlines() if ln.strip()] if rc == 0 else []


def _commit_on_branch(root, branch, message):
    """Зафиксировать применённые изменения на рабочей ветке (не в main). -> полный commit SHA или None.

    finding аудита (P0.5): возвращаем ПОЛНЫЙ SHA (не --short) — evidence бьётся о точную ревизию,
    а короткий SHA теоретически коллизирует и не годится как надёжный идентификатор ревизии.
    """
    _git(root, "checkout", "-q", "-B", branch)   # рабочая ветка (не трогаем main)
    _git(root, "add", "-A")
    rc, _, _ = _git(root, "diff", "--cached", "--quiet")
    if rc == 0:                                   # нечего коммитить
        return None
    _git(root, "commit", "-q", "-m", message)
    rc, sha, _ = _git(root, "rev-parse", "HEAD")
    return sha if rc == 0 else None


def _tree_clean(root):
    """git status --porcelain пуст? -> рабочее дерево совпадает с HEAD (нет незакоммиченных правок).

    finding аудита (P0.5): evidence должен отражать ЗАКОММИЧЕННУЮ ревизию. Если дерево грязное
    (правки вне коммита или checks намутили артефакты), evidence не бьётся о SHA — это нужно видеть,
    а не молча объявлять ready_for_pr.
    """
    rc, out, _ = _git(root, "status", "--porcelain")
    return rc == 0 and out.strip() == ""


# v2.119 (finding живого прогона): известные тул-кэши/артефакты, которые тесты/сборка РУТИННО создают
# (pytest/npm/mypy/rust/...). В репо БЕЗ .gitignore этих путей они показываются в `git status` как
# untracked и делали дерево «грязным после проверок» (tree_after=False) -> ложный not-ready, хотя
# checks реально прошли. Их наличие как UNTRACKED-артефактов не нарушает evidence-целостность.
_TOOL_CACHE_RE = re.compile(
    r"(^|/)("
    r"__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.tox|\.nox|\.hypothesis|"
    r"htmlcov|\.eggs|[^/]+\.egg-info|\.coverage[^/]*|"
    r"node_modules|\.next|\.nuxt|\.turbo|\.parcel-cache|\.svelte-kit|"
    r"target|\.gradle|\.mvn|"
    r"dist|build|coverage|\.cache|__snapshots__"
    r")(/|$)"
    r"|\.(pyc|pyo|class)$"
)


def _tree_clean_after_checks(root):
    """v2.119: чистота дерева ПОСЛЕ проверок, игнорируя известные тул-кэши (UNTRACKED-артефакты тестов/
    сборки: __pycache__, .pytest_cache, node_modules, target, dist, ...). Модификации TRACKED-файлов и
    любой прочий untracked по-прежнему считаются грязью — evidence-целостность (P0.5) сохранена.
    Устраняет false not-ready в репозиториях без .gitignore этих кэшей. -> bool."""
    rc, out, _ = _git(root, "status", "--porcelain")
    if rc != 0:
        return False
    for ln in out.splitlines():
        if not ln.strip():
            continue
        code, path = ln[:2], ln[3:]
        # игнорируем ТОЛЬКО untracked (??) тул-кэши; tracked-правки и прочий untracked = грязь
        if code == "??" and _TOOL_CACHE_RE.search(path):
            continue
        return False
    return True


def _untracked(root):
    """Множество untracked-файлов (git status --porcelain, префикс '??'). Игнорируемые (.gitignore,
    напр. node_modules) сюда НЕ попадают — porcelain их не показывает без --ignored."""
    rc, out, _ = _git(root, "status", "--porcelain")
    if rc != 0:
        return set()
    return {ln[3:] for ln in out.splitlines() if ln.startswith("?? ")}


def _has_changes(root):
    """Есть ли ЛЮБЫЕ правки в рабочем дереве (tracked-diff ИЛИ новые untracked)? -> bool.

    v2.93 (finding аудита): раньше наличие правок считали ТОЛЬКО по успешным write-операциям петли.
    Если модель изменила код через разрешённый shell (sed/форматтер), правки реальны, но applied
    пусто -> коммит не создавался и работа не доставлялась. Считаем факт по git, а не по счётчику op."""
    return not _tree_clean(root)


def _resolve_base(root, base_ref):
    """v3.0.2/v3.0.7 (finding аудита P0): разрешение base-ветки. ТОЛЬКО ветка (локальная/origin), не tag/SHA.

    v3.0.7 BaseResolver v3 — два режима:
    * base_ref=None -> AUTO: текущая ветка -> upstream (@{u}) -> remote default (origin/HEAD).
      Никакого хардкода 'main'. Порядок «текущая ветка первой» — F-014: работа продолжается с того
      места, где стоит пользователь, поэтому последовательные задачи строятся друг на друге.
      Не резолвится только detached HEAD без upstream/origin/HEAD (база обязана быть веткой).
    * base_ref задан -> EXPLICIT: обязана существовать (refs/heads/<ref> или origin/<ref>); иначе
      resolved=False (вызывающий обязан заблокировать прогон ДО модели — не выполнять от HEAD).
    -> {base_ref, base_sha, source, mode, resolved, reason}."""
    if _git(root, "rev-parse", "--is-inside-work-tree")[0] != 0:
        return {"base_ref": base_ref, "resolved": False, "mode": "explicit" if base_ref else "auto",
                "reason": "не git-репозиторий"}
    if base_ref:   # EXPLICIT — строго ветка
        rc_l, sha_l, _ = _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{base_ref}")
        if rc_l == 0 and (sha_l or "").strip():
            return {"base_ref": base_ref, "base_sha": sha_l.strip(), "source": "explicit-local",
                    "mode": "explicit", "resolved": True}
        rc_r, sha_r, _ = _git(root, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base_ref}")
        if rc_r == 0 and (sha_r or "").strip():
            return {"base_ref": base_ref, "base_sha": sha_r.strip(), "source": "explicit-remote",
                    "mode": "explicit", "resolved": True}
        return {"base_ref": base_ref, "resolved": False, "mode": "explicit",
                "reason": f"явная base '{base_ref}' не найдена ни локально (refs/heads), ни в origin"}
    # AUTO: текущая ветка -> upstream -> remote default.
    # v3.28.x (F-014, находка живой квалификации на niti): порядок был обратный — upstream/origin/HEAD
    # шли первыми, и worktree создавался от УСТАРЕВШЕЙ базы. Пользователь стоял на ветке с уже
    # принятыми задачами, а каждый следующий прогон её не видел: вторая задача правила те же файлы
    # от старой версии (гарантированный конфликт при слиянии), и приходилось руками делать
    # `git reset --hard` в managed-worktree. Ветка, на которой стоит пользователь, — это и есть
    # заявленное намерение; удалённая база нужна для ДОСТАВКИ, и её отдельно проверяет
    # _verify_remote_base (fail-closed: расхождение с origin PR не откроет).
    rc_c, cur, _ = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    cur = (cur or "").strip()
    if rc_c == 0 and cur and cur != "HEAD":      # 'HEAD' = detached: имя ветки не получено
        rc_h, head, _ = _git(root, "rev-parse", "--verify", "--quiet", "HEAD")
        if rc_h == 0 and (head or "").strip():
            return {"base_ref": cur, "base_sha": head.strip(), "source": "current-branch",
                    "mode": "auto", "resolved": True}
    rc_u, up, _ = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if rc_u == 0 and (up or "").strip():
        ref = up.strip()
        rc_s, sha, _ = _git(root, "rev-parse", "--verify", "--quiet", ref)
        if rc_s == 0 and (sha or "").strip():
            br = ref.split("origin/", 1)[1] if ref.startswith("origin/") else ref
            return {"base_ref": br, "base_sha": sha.strip(), "source": "upstream",
                    "mode": "auto", "resolved": True}
    rc_d, dref, _ = _git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if rc_d == 0 and (dref or "").strip():
        rc_s, sha, _ = _git(root, "rev-parse", "--verify", "--quiet", dref.strip())
        if rc_s == 0 and (sha or "").strip():
            br = dref.strip().split("refs/remotes/origin/", 1)[-1]
            return {"base_ref": br, "base_sha": sha.strip(), "source": "remote-default",
                    "mode": "auto", "resolved": True}
    # сюда попадаем только при detached HEAD без upstream и без origin/HEAD. Прежний код возвращал
    # здесь base_ref='HEAD' — это не ветка, а контракт резолвера требует именно ветку: дальше по
    # цепочке такая «база» ищется как refs/heads/HEAD и доставка врёт. Честный отказ.
    if cur == "HEAD":
        return {"base_ref": None, "resolved": False, "mode": "auto",
                "reason": "detached HEAD без upstream и origin/HEAD — база не ветка; "
                          "переключись на ветку или задай --base <ветка>"}
    return {"base_ref": None, "resolved": False, "mode": "auto",
            "reason": "не удалось определить base автоматически (нет текущей ветки/upstream/remote-default)"}


def _verify_remote_base(root, base_ref, base_sha):
    """v3.0.9 (finding аудита P0): ЕДИНЫЙ fail-closed верификатор remote base для доставки (single-run И
    sequential — один контракт доверия). -> {verdict, remote_sha, reason}, где verdict:
      'verified-equal'  — remote refs/heads/<base_ref> == base_sha -> можно открывать PR;
      'verified-moved'  — существует, но SHA разошёлся -> нужна ревалидация (PR не открывать);
      'unverifiable'    — нет origin/сети/ветки/ошибка ls-remote -> доставка НЕДОСТУПНА (НЕ «успех
                          по умолчанию»; отсутствие проверки != пройденная проверка)."""
    if not (base_ref and base_sha):
        return {"verdict": "unverifiable", "reason": "нет base_ref/base_sha для сверки"}
    try:
        rc, out, err = _git(root, "ls-remote", "origin", f"refs/heads/{base_ref}")
    except Exception as e:  # noqa: BLE001
        return {"verdict": "unverifiable", "reason": f"ls-remote исключение: {e}"}
    if rc != 0:
        return {"verdict": "unverifiable", "reason": f"ls-remote rc={rc}: {(err or '').strip()[:120]}"}
    line = (out or "").strip()
    if not line:
        return {"verdict": "unverifiable", "reason": f"remote-ветка refs/heads/{base_ref} не найдена в origin"}
    remote_sha = line.split()[0].strip()
    if remote_sha == base_sha:
        return {"verdict": "verified-equal", "remote_sha": remote_sha}
    return {"verdict": "verified-moved", "remote_sha": remote_sha}


def _change_context(work_root, revision, max_chars=12000):
    """v3.0-rc9 (finding живого прогона kimi): детерминированно собрать КОНТЕКСТ ИЗМЕНЕНИЯ для
    независимого ревьюера — полный список изменённых файлов (`git show --stat`, всегда целиком) +
    ограниченный по размеру unified-дифф ревизии."""
    if not revision:
        return ""
    rc, stat, _ = _git(work_root, "show", "--stat", "--format=", revision)
    if rc != 0:
        return ""
    parts = [f"Изменённые файлы (git show --stat @ {revision[:12]}):",
             (stat.strip() or "(список пуст)")]
    rc2, diff, _ = _git(work_root, "show", "--format=", "--unified=3", revision)
    if rc2 == 0 and (diff or "").strip():
        body = diff.strip()
        if len(body) > max_chars:
            body = (body[:max_chars] + f"\n... [дифф усечён на {max_chars} симв.; полный список файлов "
                    "выше — читай их целиком через {\"op\":\"read\"} для верификации]")
        parts.append("\nUnified-дифф ревизии:\n" + body)
    return "\n".join(parts) + "\n"


def _change_context_range(work_root, base_revision, head_revision, max_chars=14000):
    """v3.0-rc16 (finding аудита P0): контекст ВСЕЙ последовательности base..head для AGGREGATE-ревью."""
    if not (base_revision and head_revision):
        return _change_context(work_root, head_revision, max_chars=max_chars)
    rng = f"{base_revision}..{head_revision}"
    rc, stat, _ = _git(work_root, "diff", "--stat", rng)
    if rc != 0:
        return _change_context(work_root, head_revision, max_chars=max_chars)
    parts = [f"ИНТЕГРИРОВАННЫЙ дифф последовательности {base_revision[:12]}..{head_revision[:12]}:",
             "git diff --stat:", (stat.strip() or "(пусто)")]
    rc_l, commits, _ = _git(work_root, "log", "--oneline", "--no-decorate", rng)
    if rc_l == 0 and commits.strip():
        parts.append("\nКоммиты диапазона (по пакетам):\n" + commits.strip())
    rc2, diff, _ = _git(work_root, "diff", "--unified=3", rng)
    if rc2 == 0 and (diff or "").strip():
        body = diff.strip()
        if len(body) > max_chars:
            body = (body[:max_chars] + f"\n... [combined-дифф усечён на {max_chars} симв.; полный список "
                    "файлов выше — читай целиком через {\"op\":\"read\"} для верификации]")
        parts.append("\nCombined unified-дифф base..head:\n" + body)
    return "\n".join(parts) + "\n"


if __name__ == "__main__":
    sys.exit(selftest())
