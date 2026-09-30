#!/usr/bin/env python3
"""Чтение GitHub как operational source of truth: Issues, PR, Milestones, Labels (PR-11).

БЕЗ новой зависимости. Два транспорта поверх одного и того же REST-контракта GitHub:

  * `gh`   — вызываем `gh api --paginate` через subprocess. `gh` сам держит авторизацию и
             пагинацию; предпочтителен, если он на PATH и залогинен.
  * `rest` — `urllib` напрямую к `https://api.github.com` с токеном из окружения
             (`GITHUB_TOKEN`/`GH_TOKEN`). Пагинацию тянем по заголовку `Link`. Образец подхода —
             `ai_ops_kit/providers/orchestrator_http.py`.

Оба транспорта возвращают REST-JSON одной формы, поэтому нормализатор один.

ТРЕТЬЕ СОСТОЯНИЕ. Ни один вызов не «падает молча в пустоту» и не притворяется, что задач нет:
если доступа к GitHub нет (нет `gh` и токена, не GitHub-репозиторий, сеть/API ответили ошибкой),
результат — `Availability(ok=False, reason=...)` с НАЗВАННОЙ причиной. `FetchResult.ok is False`
означает «не проверено», `ok is True and items == []` — «проверено, задач нет». Потребитель обязан
эти два случая разводить (см. `backlog_classify`, `backlog_dedup`).

CLI:
  python3 -m ai_ops_kit.integrations.github probe [<repo_or_root>] [--json]
  python3 -m ai_ops_kit.integrations.github issues <owner/repo> [--state all] [--limit N] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: F401  (правит sys.path пакета)
from ai_ops_kit.shared.gitio import git  # noqa: E402

API_ROOT = "https://api.github.com"
_TOKEN_ENV = ("GITHUB_TOKEN", "GH_TOKEN")
# owner/repo из git-remote: https, ssh (git@github.com:owner/repo.git) и голый owner/repo.
_SLUG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+$")


@dataclass
class Availability:
    """Разрешима ли работа с GitHub здесь и сейчас. `ok is False` — это НЕ «задач нет»."""
    ok: bool
    transport: str = ""     # "gh" | "rest" | "" — чем именно ходим
    repo: str = ""          # "owner/repo" | ""
    reason: str = ""        # человекочитаемая причина, когда ok is False


@dataclass
class FetchResult:
    """Итог одного чтения. `ok is False` — не проверено (reason назван); `ok is True, items=[]` —
    проверено, ничего нет. `source` — каким транспортом получено."""
    ok: bool
    items: list = field(default_factory=list)
    reason: str = ""
    source: str = ""

    def __bool__(self) -> bool:                     # noqa: D401
        # Намеренно НЕ делаем truthiness зависимой от len(items): пустой доступный ответ — истина.
        return self.ok


@dataclass
class WriteResult:
    """Итог одной ЗАПИСИ (комментарий/закрытие Issue). `ok is False` — не выполнено (reason назван).

    Запись к GitHub — отдельный тип от чтения: у неё нет `items`, и её отсутствие успеха НИКОГДА не
    выглядит как «нечего писать». Все write-операции идут через единственный шов `_gh_mutate`, чтобы
    тесты могли доказать намерение (какой issue закрыт, с каким текстом) не касаясь живого GitHub."""
    ok: bool
    number: int = 0
    action: str = ""        # "comment" | "close" | "reopen"
    reason: str = ""        # человекочитаемая причина, когда ok is False


# ── определение репозитория ─────────────────────────────────────────────────────────────────

def _git_remote_url(root: Path) -> str:
    # Единый вход к git с таймаутом (см. shared/gitio). OSError (нет git) по-прежнему -> "".
    try:
        rc, url, _ = git(root, "remote", "get-url", "origin", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return url if rc == 0 else ""


def parse_slug(url: str) -> str:
    """`owner/repo` из git-remote URL (github.com) или из голой строки `owner/repo`. Иначе ""."""
    url = (url or "").strip()
    if not url:
        return ""
    if _SLUG_RE.match(url) and "github.com" not in url:
        return url                                  # уже owner/repo
    if "github.com" not in url:
        return ""                                   # чужой хост — не наш source of truth
    tail = url.split("github.com", 1)[1].lstrip(":/")
    if tail.endswith(".git"):
        tail = tail[:-4]
    tail = tail.rstrip("/")
    parts = tail.split("/")
    if len(parts) >= 2 and parts[0] and parts[1]:
        return f"{parts[0]}/{parts[1]}"
    return ""


def resolve_repo(repo_or_root: str = ".") -> str:
    """Разрешить `owner/repo`. Аргумент — либо уже слаг, либо путь к репозиторию (берём origin)."""
    slug = parse_slug(repo_or_root)
    if slug:
        return slug
    root = Path(repo_or_root)
    if root.exists():
        return parse_slug(_git_remote_url(root))
    return ""


def _token() -> str:
    for name in _TOKEN_ENV:
        val = os.environ.get(name)
        if val:
            return val
    return ""


def _has_gh() -> bool:
    from shutil import which
    return which("gh") is not None


# ── клиент ──────────────────────────────────────────────────────────────────────────────────

class GitHubClient:
    """Тонкий клиент над одним из двух транспортов. Всё сетевое/subprocess сосредоточено в
    `_gh_api` и `_rest_get` — тесты подменяют ровно их и живого GitHub не касаются."""

    def __init__(self, repo: str = "", transport: str = "", token: str = "", timeout: int = 30):
        self.repo = repo
        self.transport = transport          # "gh" | "rest" | "" (авто)
        self.token = token or _token()
        self.timeout = timeout

    # -- доступность --------------------------------------------------------------------------

    def availability(self) -> Availability:
        if not self.repo:
            return Availability(False, reason="репозиторий не определён: нет git-remote на github.com")
        transport = self.transport or ("gh" if _has_gh() else ("rest" if self.token else ""))
        if not transport:
            return Availability(
                False, repo=self.repo,
                reason="нет доступа к GitHub: `gh` не найден и не задан GITHUB_TOKEN/GH_TOKEN",
            )
        return Availability(True, transport=transport, repo=self.repo)

    # -- транспорты (единственное, что ходит наружу) ------------------------------------------

    def _gh_api(self, path: str, params: "dict | None" = None) -> list:
        """`gh api --paginate` — gh сам держит токен и пагинацию. Возвращает список объектов."""
        endpoint = path.lstrip("/")
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items())
            endpoint = f"{endpoint}?{query}"
        cmd = ["gh", "api", "--paginate", "-H", "Accept: application/vnd.github+json", endpoint]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout * 4)
        if proc.returncode != 0:
            raise GitHubError((proc.stderr or proc.stdout or "gh api failed").strip())
        # --paginate склеивает страницы как несколько JSON-массивов подряд; разбираем по одному.
        return _decode_json_stream(proc.stdout)

    def _rest_get(self, path: str, params: "dict | None" = None) -> list:
        """urllib напрямую к api.github.com, пагинация по заголовку Link (образец — orchestrator_http)."""
        import urllib.error
        import urllib.parse
        import urllib.request

        endpoint = path.lstrip("/")
        query = urllib.parse.urlencode(params or {})
        url = f"{API_ROOT}/{endpoint}" + (f"?{query}" if query else "")
        headers = {"Accept": "application/vnd.github+json",
                   "User-Agent": "ai-ops-kit",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        items: list = []
        while url:
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    payload = json.loads(r.read().decode("utf-8"))
                    link = r.headers.get("Link", "")
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = json.loads(e.read().decode("utf-8")).get("message", "")
                except Exception:                    # noqa: BLE001 — тело ошибки необязательно JSON
                    detail = ""
                raise GitHubError(f"HTTP {e.code} {detail}".strip()) from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                raise GitHubError(f"сеть: {e}") from e
            if isinstance(payload, list):
                items.extend(payload)
            else:
                return [payload]                     # одиночный объект (не список) — как есть
            url = _next_link(link)
        return items

    def _get(self, path: str, params: "dict | None" = None) -> "tuple[list, str]":
        """Выбрать транспорт и вернуть (items, source). Ошибки транспорта → GitHubError."""
        av = self.availability()
        if not av.ok:
            raise GitHubError(av.reason)
        if av.transport == "gh":
            return self._gh_api(path, params), "gh"
        return self._rest_get(path, params), "rest"

    def _fetch(self, path: str, params: "dict | None", normalize) -> FetchResult:
        try:
            raw, source = self._get(path, params)
        except GitHubError as e:
            return FetchResult(False, reason=str(e))
        return FetchResult(True, items=[normalize(x) for x in raw], source=source)

    # -- предметные чтения --------------------------------------------------------------------

    def issues(self, state: str = "open", limit: int = 0) -> FetchResult:
        """Открытые/все Issues БЕЗ pull request'ов (REST кладёт PR в тот же эндпоинт — фильтруем)."""
        params = {"state": state, "per_page": 100}
        if limit and limit < 100:
            params["per_page"] = limit
        res = self._fetch("repos/%s/issues" % self.repo, params, _norm_issue)
        if res.ok:
            res.items = [i for i in res.items if not i["is_pull"]]
            if limit:
                res.items = res.items[:limit]
        return res

    def pulls(self, state: str = "open", limit: int = 0) -> FetchResult:
        params = {"state": state, "per_page": 100}
        res = self._fetch("repos/%s/pulls" % self.repo, params, _norm_pull)
        if res.ok and limit:
            res.items = res.items[:limit]
        return res

    def milestones(self, state: str = "all") -> FetchResult:
        return self._fetch("repos/%s/milestones" % self.repo,
                           {"state": state, "per_page": 100}, _norm_milestone)

    def labels(self) -> FetchResult:
        return self._fetch("repos/%s/labels" % self.repo, {"per_page": 100}, _norm_label)

    # -- предметные ЗАПИСИ (approval-gated: выполняются только по явному одобрению человека) -------
    #
    # Единственный write-путь — `_gh_mutate` (gh api -X POST/PATCH). Тесты подменяют ровно его и
    # живого GitHub не касаются. GitHub НЕ умеет «слить» два Issue: конвенция — закрыть ДУБЛЬ с
    # кросс-ссылкой на канонический (комментарий + state=closed, reason=not_planned), канонический
    # оставить открытым. Операция ОБРАТИМА: закрытый issue переоткрывается (`reopen_issue`).

    def _gh_mutate(self, method: str, path: str, fields: "dict | None" = None) -> dict:
        """`gh api -X METHOD path -f k=v` — единственный write-транспорт. -> распарсенный ответ (dict).

        Только через gh: у gh уже есть аутентификация с нужным scope; rest-путь (urllib) писать не
        разрешаем, чтобы у записи был ровно один шов. Ошибка транспорта -> GitHubError."""
        endpoint = path.lstrip("/")
        cmd = ["gh", "api", "-X", method, "-H", "Accept: application/vnd.github+json", endpoint]
        for k, v in (fields or {}).items():
            cmd += ["-f", f"{k}={v}"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout * 2)
        if proc.returncode != 0:
            raise GitHubError((proc.stderr or proc.stdout or "gh api write failed").strip())
        try:
            return json.loads(proc.stdout) if proc.stdout.strip() else {}
        except ValueError:
            return {}

    def _writable(self) -> "WriteResult | None":
        """Проверка доступности записи. -> WriteResult(ok=False) если нельзя, иначе None.

        Запись возможна ТОЛЬКО через gh: rest-токен-путь для мутаций мы сознательно не открываем."""
        av = self.availability()
        if not av.ok:
            return WriteResult(False, reason=av.reason)
        if av.transport != "gh":
            return WriteResult(False, reason="запись к GitHub идёт только через `gh` "
                                             "(rest-путь для мутаций не открыт); установите gh")
        return None

    def comment_issue(self, number: int, body: str) -> WriteResult:
        """Оставить комментарий на Issue. Approval-gated: вызывается только из execute_merge по
        одобренной человеком паре."""
        blocked = self._writable()
        if blocked:
            return WriteResult(False, number=number, action="comment", reason=blocked.reason)
        try:
            self._gh_mutate("POST", f"repos/{self.repo}/issues/{number}/comments", {"body": body})
        except GitHubError as e:
            return WriteResult(False, number=number, action="comment", reason=str(e))
        return WriteResult(True, number=number, action="comment")

    def close_issue(self, number: int, reason: str = "not_planned") -> WriteResult:
        """Закрыть Issue (reason: completed|not_planned). Дубль закрывается как not_planned. Обратимо."""
        blocked = self._writable()
        if blocked:
            return WriteResult(False, number=number, action="close", reason=blocked.reason)
        try:
            self._gh_mutate("PATCH", f"repos/{self.repo}/issues/{number}",
                            {"state": "closed", "state_reason": reason})
        except GitHubError as e:
            return WriteResult(False, number=number, action="close", reason=str(e))
        return WriteResult(True, number=number, action="close")

    def reopen_issue(self, number: int) -> WriteResult:
        """Переоткрыть Issue — обратная операция к close (обратимость слияния дублей)."""
        blocked = self._writable()
        if blocked:
            return WriteResult(False, number=number, action="reopen", reason=blocked.reason)
        try:
            self._gh_mutate("PATCH", f"repos/{self.repo}/issues/{number}", {"state": "open"})
        except GitHubError as e:
            return WriteResult(False, number=number, action="reopen", reason=str(e))
        return WriteResult(True, number=number, action="reopen")


class GitHubError(RuntimeError):
    """Ошибка транспорта: сеть, HTTP, отказ gh. Ловится в `_fetch` → «не проверено» с причиной."""


# ── нормализация REST-объектов в общую форму ──────────────────────────────────────────────────

def _norm_issue(raw: dict) -> dict:
    return {
        "number": raw.get("number"),
        "title": raw.get("title") or "",
        "body": raw.get("body") or "",
        "state": raw.get("state") or "",
        "labels": [(lb.get("name") if isinstance(lb, dict) else lb) for lb in (raw.get("labels") or [])],
        "milestone": (raw.get("milestone") or {}).get("title") if raw.get("milestone") else None,
        "assignees": [a.get("login") for a in (raw.get("assignees") or []) if isinstance(a, dict)],
        "author": (raw.get("user") or {}).get("login") or "",
        "comments": raw.get("comments") or 0,
        "created_at": raw.get("created_at") or "",
        "updated_at": raw.get("updated_at") or "",
        "url": raw.get("html_url") or "",
        "is_pull": "pull_request" in raw,
    }


def _norm_pull(raw: dict) -> dict:
    return {
        "number": raw.get("number"),
        "title": raw.get("title") or "",
        "body": raw.get("body") or "",
        "state": raw.get("state") or "",
        "labels": [(lb.get("name") if isinstance(lb, dict) else lb) for lb in (raw.get("labels") or [])],
        "milestone": (raw.get("milestone") or {}).get("title") if raw.get("milestone") else None,
        "author": (raw.get("user") or {}).get("login") or "",
        "draft": bool(raw.get("draft")),
        "merged_at": raw.get("merged_at"),
        "created_at": raw.get("created_at") or "",
        "updated_at": raw.get("updated_at") or "",
        "url": raw.get("html_url") or "",
    }


def _norm_milestone(raw: dict) -> dict:
    return {
        "number": raw.get("number"),
        "title": raw.get("title") or "",
        "state": raw.get("state") or "",
        "description": raw.get("description") or "",
        "open_issues": raw.get("open_issues") or 0,
        "closed_issues": raw.get("closed_issues") or 0,
        "due_on": raw.get("due_on"),
        "url": raw.get("html_url") or "",
    }


def _norm_label(raw: dict) -> dict:
    return {
        "name": raw.get("name") or "",
        "color": raw.get("color") or "",
        "description": raw.get("description") or "",
    }


# ── вспомогательное ───────────────────────────────────────────────────────────────────────────

def _next_link(link_header: str) -> str:
    """URL следующей страницы из заголовка Link (`<url>; rel="next", ...`) или ""."""
    for part in (link_header or "").split(","):
        seg = part.split(";")
        if len(seg) < 2:
            continue
        if 'rel="next"' in seg[1]:
            return seg[0].strip().lstrip("<").rstrip(">")
    return ""


def _decode_json_stream(text: str) -> list:
    """`gh api --paginate` печатает несколько JSON-массивов подряд. Склеиваем их в один список.
    Одиночный объект (не массив) заворачиваем в список из одного элемента."""
    text = (text or "").strip()
    if not text:
        return []
    dec = json.JSONDecoder()
    out: list = []
    idx = 0
    n = len(text)
    while idx < n:
        while idx < n and text[idx] in " \t\r\n":
            idx += 1
        if idx >= n:
            break
        obj, end = dec.raw_decode(text, idx)
        if isinstance(obj, list):
            out.extend(obj)
        else:
            out.append(obj)
        idx = end
    return out


def make_client(repo_or_root: str = ".", transport: str = "") -> GitHubClient:
    """Клиент по слагу или пути к репозиторию. Доступность проверяется отдельно — `availability()`."""
    return GitHubClient(repo=resolve_repo(repo_or_root), transport=transport)


# ── CLI ───────────────────────────────────────────────────────────────────────────────────────

def _cmd_probe(ns) -> int:
    client = make_client(ns.target)
    av = client.availability()
    if ns.json:
        print(json.dumps(av.__dict__, ensure_ascii=False, indent=2))
    elif av.ok:
        print(f"GitHub доступен: {av.repo} (транспорт {av.transport})")
    else:
        print(f"GitHub НЕ проверен: {av.reason}")
    return 0 if av.ok else 2


def _cmd_issues(ns) -> int:
    client = make_client(ns.target)
    res = client.issues(state=ns.state, limit=ns.limit)
    if ns.json:
        print(json.dumps({"ok": res.ok, "reason": res.reason, "source": res.source,
                          "count": len(res.items), "items": res.items},
                         ensure_ascii=False, indent=2))
    elif not res.ok:
        print(f"НЕ проверено: {res.reason}")
    else:
        print(f"Issues ({res.source}): {len(res.items)}")
        for i in res.items:
            print(f"  #{i['number']} [{','.join(i['labels']) or '—'}] {i['title']}")
    return 0 if res.ok else 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="github.py", description="Чтение GitHub (Issues/PR/Milestones/Labels)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe", help="доступен ли GitHub здесь")
    p.add_argument("target", nargs="?", default=".", help="owner/repo или путь к репозиторию")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_probe)
    q = sub.add_parser("issues", help="список Issues (без PR)")
    q.add_argument("target", nargs="?", default=".")
    q.add_argument("--state", default="open", choices=("open", "closed", "all"))
    q.add_argument("--limit", type=int, default=0)
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=_cmd_issues)
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])
    return ns.func(ns)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
