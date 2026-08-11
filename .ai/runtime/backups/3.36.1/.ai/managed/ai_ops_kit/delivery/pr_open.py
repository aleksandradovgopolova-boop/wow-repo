#!/usr/bin/env python3
"""Открытие draft PR (v2.63, P0-эпик) — финальный шаг движка task -> проверяемый draft PR.

После того как pipeline применил изменения, закоммитил на ветке ai-ops/<id> и собрал evidence,
остаётся вынести это в draft PR для человека-ревьюера. Механизм: push ветки + POST в GitHub
REST (`/repos/{owner}/{repo}/pulls`, draft:true). Токен — ТОЛЬКО из env (GITHUB_TOKEN/GH_TOKEN),
в вывод/логи не попадает; нет токена/remote -> честный `unavailable` (не имитируем PR).

Механика (конструкция payload, разбор owner/repo, ветвление по токену) детерминирована и
тестируется offline; сам сетевой вызов — живой шаг (нужен токен + доступ к GitHub).

Использование (программно): open_draft_pr(root, branch, title, body, base) -> отчёт.
  pr_open.py --selftest
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402
# переиспользуем разбор owner/repo и работу с REST из concurrency_preflight (без дублирования)
from ai_ops_kit.gates import concurrency_preflight as _cp   # noqa: E402
import urllib.error                    # noqa: E402
import urllib.request                  # noqa: E402


def _pr_payload(branch, title, body, base):
    """Чистая функция: тело запроса на создание draft PR (тестируется offline). base ОБЯЗАТЕЛЕН —
    не хардкодим 'main' (v2.93 finding: дефолт-ветка репо может быть master/develop/trunk)."""
    return {"title": title, "head": branch, "base": base, "body": body or "", "draft": True}


def _git(root, *args):
    from ai_ops_kit.engine import gitio
    return gitio.git(root, *args)   # v3.0.13 (блок C): единый git-хелпер с таймаутом


def _api_base():
    return os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")


def _gh_request(url, token, data=None, method="GET"):
    """GitHub REST-запрос. -> (обработанный dict|list, None) или (None, класс_ошибки). Токен не
    раскрываем — при ошибке только тип исключения."""
    req = urllib.request.Request(
        url, data=(json.dumps(data).encode("utf-8") if data is not None else None),
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "Content-Type": "application/json",
                 "User-Agent": "ai-ops-pr-open"},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 (доверенный host из env)
            return json.loads(resp.read().decode("utf-8")), None
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        return None, type(e).__name__


def _default_branch(owner, name, token):
    """Дефолт-ветка репозитория из GitHub API (v2.93: не хардкодим 'main'). -> имя | None."""
    data, _err = _gh_request(f"{_api_base()}/repos/{owner}/{name}", token)
    return (data or {}).get("default_branch") if isinstance(data, dict) else None


def _find_open_pr(owner, name, branch, token):
    """Уже открытый PR для head-ветки (v2.93: идемпотентность — повтор не должен падать
    дублем). -> dict PR | None."""
    data, _err = _gh_request(
        f"{_api_base()}/repos/{owner}/{name}/pulls?head={owner}:{branch}&state=open", token)
    if isinstance(data, list) and data:
        return data[0]
    return None


def open_draft_pr(root, branch, title, body="", base=None, push=True, delivery_id=None):
    """Push ветки + создать/обновить draft PR через GitHub REST. Токен из env; иначе honest unavailable.
    v3.0.17 (finding аудита #2/P1): в body вшивается delivery_id-маркер (для сверки/реконсиляции);
    возвращается head_sha (реальный remote SHA PR), repository, base. НЕОДНОЗНАЧНЫЙ POST (сеть/timeout
    ПОСЛЕ отправки мутирующего запроса) -> status='outcome_unknown' (сервер мог создать PR), НЕ 'error'.
    Возврат: {status: opened|updated|unavailable|error|outcome_unknown, url?/number?/head_sha?/base?/repository?/note?}."""
    root = Path(root)
    token = _cp._github_token()
    if not token:
        return {"status": "unavailable",
                "note": "нет GITHUB_TOKEN/GH_TOKEN — draft PR не создан (механизм готов, нужен токен)",
                "payload": _pr_payload(branch, title, body, base or "<default-branch>")}
    rc, url, _ = _git(root, "remote", "get-url", "origin")
    owner_repo = _cp._parse_owner_repo(url) if rc == 0 else None
    if not owner_repo:
        return {"status": "unavailable", "note": "не удалось определить owner/repo из origin"}
    owner, name = owner_repo
    repository = f"{owner}/{name}"
    if base is None:
        base = _default_branch(owner, name, token)
        if not base:
            return {"status": "error",
                    "note": "не удалось определить дефолт-ветку репо (GitHub API); задай base явно"}
    if delivery_id:   # маркер для сверки/реконсиляции — вшит в тело PR
        body = f"{body}\n\n<!-- ai-ops-delivery-id: {delivery_id} -->"
    if push:
        prc, _, perr = _git(root, "push", "-u", "origin", branch)
        if prc != 0:
            return {"status": "error", "note": f"git push не удался (rc={prc}): {perr[:200]}"}
    # идемпотентность: PR для ветки уже открыт -> не создаём дубль, возвращаем его (+head_sha/base)
    existing = _find_open_pr(owner, name, branch, token)
    if existing:
        return {"status": "updated", "url": existing.get("html_url"), "number": existing.get("number"),
                "draft": existing.get("draft", True), "repository": repository,
                "head_sha": (existing.get("head") or {}).get("sha"),
                "base": (existing.get("base") or {}).get("ref") or base,
                "note": "PR для ветки уже открыт — ветка обновлена push'ем (идемпотентно)"}
    data, err = _gh_request(f"{_api_base()}/repos/{owner}/{name}/pulls", token,
                            data=_pr_payload(branch, title, body, base), method="POST")
    if err:
        # МУТИРУЮЩИЙ POST + ошибка транспорта/декода = ИСХОД НЕИЗВЕСТЕН (PR мог быть создан, ответ потерян).
        # НЕ 'error' (иначе контроллер запишет подтверждённый Receipt и реконсиляция не запустится).
        return {"status": "outcome_unknown", "repository": repository, "base": base,
                "note": f"GitHub API POST дал неоднозначный результат ({err}) — исход доставки неизвестен, "
                        "нужна сверка с remote (reconciliation)"}
    return {"status": "opened", "url": data.get("html_url"), "number": data.get("number"),
            "draft": data.get("draft", True), "base": base, "repository": repository,
            "head_sha": (data.get("head") or {}).get("sha")}


def _find_pr_for_branch(owner, name, branch, token, state="all"):
    """v3.0.17 (finding аудита P0): PR для head-ветки в ЛЮБОМ состоянии (open/closed/merged), не только
    open — иначе закрытый/смёрженный PR не отличить от 'absent'. -> dict PR (с head/base/state/merged_at)|None."""
    data, _err = _gh_request(
        f"{_api_base()}/repos/{owner}/{name}/pulls?head={owner}:{branch}&state={state}", token)
    if isinstance(data, list) and data:
        # предпочитаем самый свежий (первый) — GitHub отдаёт по убыванию created
        return data[0]
    return None


def reconcile_delivery(root, branch):
    """v3.0.16/v3.0.17 (finding аудита #2/P0): СВЕРКА фактического состояния доставки на remote для ветки.
    Возвращает ФАКТЫ (repository, head_sha, base_ref, pr_state, merged, url, number) — строгую проверку
    идентичности (head_sha==intent.commit_sha, base_ref, repository) делает контроллер, НЕ доверяя
    имени ветки. Ищет PR во ВСЕХ состояниях (open/closed/merged/absent). Идемпотентно, ничего не создаёт.
    -> {status: found|absent|unavailable, repository?, url?, number?, head_sha?, base_ref?, pr_state?, merged?}."""
    root = Path(root)
    token = _cp._github_token()
    if not token:
        return {"status": "unavailable", "note": "нет GITHUB_TOKEN/GH_TOKEN — сверка недоступна"}
    rc, url, _ = _git(root, "remote", "get-url", "origin")
    owner_repo = _cp._parse_owner_repo(url) if rc == 0 else None
    if not owner_repo:
        return {"status": "unavailable", "note": "не удалось определить owner/repo из origin"}
    owner, name = owner_repo
    pr = _find_pr_for_branch(owner, name, branch, token, state="all")
    if not pr:
        return {"status": "absent", "repository": f"{owner}/{name}"}
    return {"status": "found", "repository": f"{owner}/{name}",
            "url": pr.get("html_url"), "number": pr.get("number"),
            "head_sha": (pr.get("head") or {}).get("sha"),
            "base_ref": (pr.get("base") or {}).get("ref"),
            "pr_state": pr.get("state"), "merged": bool(pr.get("merged_at"))}


def main(argv):
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
