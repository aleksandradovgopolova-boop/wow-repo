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

import json
import os
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
for _p in (PKG / "tools",):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# переиспользуем разбор owner/repo и работу с REST из concurrency_preflight (без дублирования)
import concurrency_preflight as _cp   # noqa: E402
import urllib.error                    # noqa: E402
import urllib.request                  # noqa: E402


def _pr_payload(branch, title, body, base):
    """Чистая функция: тело запроса на создание draft PR (тестируется offline). base ОБЯЗАТЕЛЕН —
    не хардкодим 'main' (v2.93 finding: дефолт-ветка репо может быть master/develop/trunk)."""
    return {"title": title, "head": branch, "base": base, "body": body or "", "draft": True}


def _git(root, *args):
    import gitio
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


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    # чистая конструкция payload
    p = _pr_payload("ai-ops/x", "Заголовок", "Тело", base="main")
    expect("payload: draft=True", p["draft"] is True)
    expect("payload: head/base/title/body", p["head"] == "ai-ops/x" and p["base"] == "main"
           and p["title"] == "Заголовок" and p["body"] == "Тело")

    # без токена -> honest unavailable, сеть не трогаем, payload приложен
    import tempfile
    saved = {k: os.environ.pop(k, None) for k in ("GITHUB_TOKEN", "GH_TOKEN")}
    try:
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["git", "-C", td, "init", "-q"])
            r = open_draft_pr(td, "ai-ops/y", "T", "B")
            expect("нет токена -> unavailable (не имитируем PR)",
                   r["status"] == "unavailable" and "GITHUB_TOKEN" in r["note"])
            expect("unavailable несёт готовый payload (механизм готов)",
                   r.get("payload", {}).get("draft") is True)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v

    # v2.93: base=None -> резолвится дефолт-ветка репо (не хардкод main); существующий PR ->
    # идемпотентный 'updated' без создания дубля. Сеть стабим через _gh_request.
    import tempfile as _tf
    global _gh_request, _cp
    real_gh, real_token = _gh_request, _cp._github_token
    try:
        _cp._github_token = lambda: "tok"  # noqa: E731 (стаб на время теста)
        calls = {"post": 0}

        def fake_gh(url, token, data=None, method="GET"):
            if method == "GET" and url.endswith("/repos/o/r"):
                return {"default_branch": "develop"}, None       # дефолт-ветка НЕ main
            if "pulls?head=" in url:
                return [], None                                  # открытого PR нет
            if method == "POST":
                calls["post"] += 1
                return {"html_url": "u", "number": 7, "draft": True}, None
            return {}, None
        _gh_request = fake_gh
        with _tf.TemporaryDirectory() as td:
            subprocess.run(["git", "-C", td, "init", "-q"])
            subprocess.run(["git", "-C", td, "remote", "add", "origin", "https://github.com/o/r.git"])
            r = open_draft_pr(td, "ai-ops/z", "T", "B", push=False)
            expect("v2.93: base=None -> дефолт-ветка develop (не хардкод main)",
                   r["status"] == "opened" and r.get("base") == "develop")

        def fake_gh_existing(url, token, data=None, method="GET"):
            if "pulls?head=" in url:
                return [{"html_url": "u2", "number": 3, "draft": True}], None   # PR уже открыт
            if method == "POST":
                calls["post"] += 1
                return {"html_url": "x", "number": 99}, None
            return {"default_branch": "main"}, None
        _gh_request = fake_gh_existing
        with _tf.TemporaryDirectory() as td:
            subprocess.run(["git", "-C", td, "init", "-q"])
            subprocess.run(["git", "-C", td, "remote", "add", "origin", "https://github.com/o/r.git"])
            before = calls["post"]
            r = open_draft_pr(td, "ai-ops/z", "T", "B", base="main", push=False)
            expect("v2.93: существующий PR -> 'updated', без создания дубля (идемпотентно)",
                   r["status"] == "updated" and r["number"] == 3 and calls["post"] == before)
    finally:
        _gh_request = real_gh
        _cp._github_token = real_token

    # v3.0.17 (finding аудита P1): неоднозначный POST (сеть/timeout после отправки) -> outcome_unknown
    real_gh2, real_token2 = _gh_request, _cp._github_token
    _cap = {}
    try:
        _cp._github_token = lambda: "tok"  # noqa: E731

        def fake_gh_ambiguous(url, token, data=None, method="GET"):
            if "pulls?head=" in url:
                return [], None                     # открытого PR нет
            if method == "POST":
                _cap["body"] = (data or {}).get("body")
                return None, "URLError"             # ответ ПОСЛЕ POST потерян
            return {"default_branch": "main"}, None
        _gh_request = fake_gh_ambiguous
        with _tf.TemporaryDirectory() as td:
            subprocess.run(["git", "-C", td, "init", "-q"])
            subprocess.run(["git", "-C", td, "remote", "add", "origin", "https://github.com/o/r.git"])
            r = open_draft_pr(td, "ai-ops/z", "T", "B", base="main", push=False, delivery_id="deadbeef")
            expect("v3.0.17 P1: неоднозначный POST -> outcome_unknown (не confirmed error)",
                   r["status"] == "outcome_unknown" and r.get("repository") == "o/r")
            expect("v3.0.17: delivery_id вшит маркером в body PR",
                   "ai-ops-delivery-id: deadbeef" in (_cap.get("body") or ""))

        # v3.0.17 (P0): reconcile_delivery возвращает ФАКТЫ remote (head_sha/base_ref/repo/state)
        def fake_gh_reconcile(url, token, data=None, method="GET"):
            if "pulls?head=" in url and "state=all" in url:
                return [{"html_url": "https://x/pr/9", "number": 9, "state": "open",
                         "head": {"sha": "abc1234"}, "base": {"ref": "main"}}], None
            return {}, None
        _gh_request = fake_gh_reconcile
        with _tf.TemporaryDirectory() as td:
            subprocess.run(["git", "-C", td, "init", "-q"])
            subprocess.run(["git", "-C", td, "remote", "add", "origin", "https://github.com/o/r.git"])
            rc = reconcile_delivery(td, "ai-ops/z")
            expect("v3.0.17 P0: reconcile возвращает head_sha/base_ref/repository/state (не только имя ветки)",
                   rc["status"] == "found" and rc["head_sha"] == "abc1234"
                   and rc["base_ref"] == "main" and rc["repository"] == "o/r" and rc["pr_state"] == "open")

        def fake_gh_absent(url, token, data=None, method="GET"):
            return [], None
        _gh_request = fake_gh_absent
        with _tf.TemporaryDirectory() as td:
            subprocess.run(["git", "-C", td, "init", "-q"])
            subprocess.run(["git", "-C", td, "remote", "add", "origin", "https://github.com/o/r.git"])
            expect("v3.0.17 P0: PR отсутствует -> absent + repository",
                   reconcile_delivery(td, "ai-ops/z") == {"status": "absent", "repository": "o/r"})
    finally:
        _gh_request = real_gh2
        _cp._github_token = real_token2

    print("pr_open selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
