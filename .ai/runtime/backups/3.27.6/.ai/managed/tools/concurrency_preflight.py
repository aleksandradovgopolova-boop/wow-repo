#!/usr/bin/env python3
"""Concurrency preflight (v2.28) — проверка коллизий параллельной работы до старта.

Класс проблемы «concurrent-edit collision + stale premise»: два потока независимо меняют
одну поверхность; позже — merge-конфликт и переделки. Хуже — работа на устаревшей
посылке: удаляешь «мёртвый» контрол, а параллельный PR ровно его оживляет.

Реестр активных работ (tools/active_work.py) ловит это, только если оба потока в нём
зарегистрированы. Этот preflight смотрит на ФАКТИЧЕСКОЕ состояние репозитория:

  1. base_changes — коммиты в базовой ветке (origin/main), затронувшие целевые пути
     ПОСЛЕ того, как отделилась текущая ветка (merge-base..base). Непусто => премисса
     могла устареть: перепроверить против актуального main, а не базы ветки.
  2. open_prs — открытые PR, трогающие те же пути. Порядок: gh CLI (если авторизован) ->
     GitHub REST API (токен GITHUB_TOKEN/GH_TOKEN из env) -> unavailable.
  3. active_work — пересечение по зонам с реестром активных работ (если передан --areas).

Вердикт: clean | collision. collision => рекомендация (координация / rebase на актуальный
main / сузить scope / согласовать владельца по OwnershipMap).

Границы честности: git-часть (base_changes) — детерминирована, только git. Открытые PR
проверяются через gh или REST (v2.43): токен только из env, в вывод/логи не попадает; если
нет ни gh, ни токена — пункт помечается unavailable, не выдаётся за clean.

Использование:
  concurrency_preflight.py --paths a.ts,b.ts [--base origin/main] [--repo .]
                           [--areas x,y] [--active-work .ai/runtime/active-work.yaml] [--json]
  concurrency_preflight.py --selftest
Возврат 0 — выполнено (в т.ч. verdict=collision: это предупреждение стадии intake, не крах
инструмента); 1 — ошибка использования.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _git(repo, *args):
    import gitio
    return gitio.git(repo, *args)   # v3.0.13 (блок C): единый git-хелпер с таймаутом


def _parse_owner_repo(remote_url):
    """owner/repo из git remote URL (https, ssh, с .git и без). None, если не GitHub-подобный."""
    if not remote_url:
        return None
    u = remote_url.strip()
    # git@host:owner/repo(.git)  |  https://host/owner/repo(.git)  |  ssh://git@host/owner/repo
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?$", u)
    if not m:
        return None
    return m.group(1), m.group(2)


def _prs_overlap(pr_records, paths):
    """Чистая функция: из [{number,title,files:[...]}] выбрать PR, трогающие paths."""
    want = set(paths)
    hits = []
    for pr in pr_records:
        files = set(pr.get("files") or [])
        shared = sorted(want & files)
        if shared:
            hits.append({"number": pr.get("number"), "title": pr.get("title"),
                         "shared_paths": shared})
    return hits


def _github_token():
    """Токен только из env (GITHUB_TOKEN / GH_TOKEN). В логи/вывод не попадает."""
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def _gh_api_get(path, token):
    """GET к GitHub REST API. host из GITHUB_API_URL (для GHE) или api.github.com."""
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    req = urllib.request.Request(base + path, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ai-ops-preflight",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 (доверенный host из env)
        return json.loads(resp.read().decode("utf-8"))


def open_prs_via_rest(repo, paths, max_prs=30):
    """REST-фоллбэк (без gh): открытые PR, трогающие paths. Токен — из env; иначе unavailable."""
    token = _github_token()
    if not token:
        return {"status": "unavailable", "note": "нет gh и нет GITHUB_TOKEN/GH_TOKEN — открытые PR не проверены", "prs": []}
    rc, url, _ = _git(repo, "remote", "get-url", "origin")
    owner_repo = _parse_owner_repo(url) if rc == 0 else None
    if not owner_repo:
        return {"status": "unavailable", "note": "не удалось определить owner/repo из origin", "prs": []}
    owner, name = owner_repo
    try:
        prs = _gh_api_get(f"/repos/{owner}/{name}/pulls?state=open&per_page={max_prs}", token)
        records = []
        for pr in prs[:max_prs]:
            num = pr.get("number")
            files = _gh_api_get(f"/repos/{owner}/{name}/pulls/{num}/files?per_page=100", token)
            records.append({"number": num, "title": pr.get("title"),
                            "files": [f.get("filename") for f in files]})
        return {"status": "checked", "via": "rest", "prs": _prs_overlap(records, paths)}
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        # не раскрываем токен: сообщаем класс ошибки, не тело запроса
        return {"status": "unavailable", "note": f"GitHub API недоступен ({type(e).__name__})", "prs": []}


def base_changes(repo, base, paths):
    """Коммиты базовой ветки, затронувшие paths после отделения текущей ветки."""
    rc, mb, _ = _git(repo, "merge-base", "HEAD", base)
    if rc != 0 or not mb:
        return None  # база недоступна (нет ref) — честно не знаем
    rc, out, _ = _git(repo, "log", "--pretty=%h\t%s", f"{mb}..{base}", "--", *paths)
    if rc != 0:
        return None
    changes = []
    for line in out.splitlines():
        if "\t" in line:
            sha, subj = line.split("\t", 1)
            changes.append({"sha": sha, "subject": subj})
    return changes


def open_prs_via_gh(repo, paths):
    """Открытые PR через gh CLI (если установлен и авторизован). None -> gh недоступен."""
    try:
        probe = subprocess.run(["gh", "--version"], capture_output=True, text=True)
    except (OSError, FileNotFoundError):
        return None
    if probe.returncode != 0:
        return None
    try:
        r = subprocess.run(["gh", "pr", "list", "--state", "open", "--json", "number,title,files"],
                           cwd=str(repo), capture_output=True, text=True)
        if r.returncode != 0:
            return None
        records = [{"number": pr.get("number"), "title": pr.get("title"),
                    "files": [f.get("path") for f in (pr.get("files") or [])]}
                   for pr in json.loads(r.stdout or "[]")]
        return {"status": "checked", "via": "gh", "prs": _prs_overlap(records, paths)}
    except (OSError, json.JSONDecodeError):
        return None


def open_prs_overlapping(repo, paths):
    """Открытые PR, трогающие paths. Порядок: gh (авторизован) -> REST (токен из env) -> unavailable.
    unavailable НЕ выдаётся за clean — честная неизвестность."""
    via_gh = open_prs_via_gh(repo, paths)
    if via_gh is not None:
        return via_gh
    return open_prs_via_rest(repo, paths)   # REST-фоллбэк или честный unavailable


def active_work_overlap(active_work_path, areas):
    if not areas or not active_work_path:
        return []
    p = Path(active_work_path)
    if not p.exists():
        return []
    import yaml
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    want = set(areas)
    out = []
    for w in data.get("active", []):
        if w.get("status") == "done":
            continue
        shared = sorted(want & set(w.get("affected_areas") or []))
        if shared:
            out.append({"id": w.get("id"), "branch": w.get("branch"), "shared_areas": shared})
    return out


def preflight(repo, base, paths, areas=None, active_work_path=None):
    bc = base_changes(repo, base, paths)
    prs = open_prs_overlapping(repo, paths)
    aw = active_work_overlap(active_work_path, areas)

    collision = bool(bc) or bool(prs.get("prs")) or bool(aw)
    result = {
        "schema_version": 1, "kind": "concurrency-preflight",
        "base": base, "paths": list(paths),
        "base_changes": bc if bc is not None else "unknown (база недоступна)",
        "open_prs": prs, "active_work_overlap": aw,
        "verdict": "collision" if collision else "clean",
    }
    if collision:
        recs = []
        if bc:
            recs.append("премисса могла устареть — перепроверить против актуального main, не базы ветки")
        if prs.get("prs"):
            recs.append("координация с открытым PR / rebase на актуальный main / сузить scope")
        if aw:
            recs.append("пересечение с активной работой в реестре — согласовать владельца (OwnershipMap)")
        result["recommendation"] = recs
    return result


def print_human(r):
    print(f"CONCURRENCY-PREFLIGHT [{r['verdict']}] paths={', '.join(r['paths'])} base={r['base']}")
    bc = r["base_changes"]
    if isinstance(bc, list) and bc:
        print(f"  ⚠ база менялась под целевыми путями ({len(bc)} коммитов) — премисса могла устареть:")
        for c in bc[:5]:
            print(f"     {c['sha']} {c['subject']}")
    prs = r["open_prs"]
    if prs.get("status") == "unavailable":
        print(f"  · открытые PR: не проверены ({prs.get('note')})")
    elif prs.get("prs"):
        print(f"  ⚠ открытые PR по тем же путям (via {prs.get('via', '?')}): " +
              ", ".join(f"#{p['number']}" for p in prs["prs"]))
    elif prs.get("status") == "checked":
        print(f"  · открытые PR проверены (via {prs.get('via', '?')}): пересечений нет")
    for a in r["active_work_overlap"]:
        print(f"  ⚠ активная работа '{a['id']}' (ветка {a['branch']}): зоны {', '.join(a['shared_areas'])}")
    for rec in r.get("recommendation", []):
        print(f"  → {rec}")


def selftest():
    import tempfile
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@t"); _git(repo, "config", "user.name", "t")
        (repo / "f.txt").write_text("v1", encoding="utf-8")
        (repo / "other.txt").write_text("x", encoding="utf-8")
        _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "c1")
        _git(repo, "branch", "-M", "main")
        # ветка feature отделяется здесь
        _git(repo, "checkout", "-q", "-b", "feature")
        # параллельно в main меняют f.txt (как чужой смерженный PR)
        _git(repo, "checkout", "-q", "main")
        (repo / "f.txt").write_text("v2", encoding="utf-8")
        _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "parallel: f.txt live actions")
        _git(repo, "checkout", "-q", "feature")

        # preflight по f.txt против main -> collision (база менялась под путём)
        r = preflight(repo, "main", ["f.txt"])
        expect("collision: база менялась под целевым файлом",
               r["verdict"] == "collision" and isinstance(r["base_changes"], list)
               and any("parallel" in c["subject"] for c in r["base_changes"]))

        # preflight по нетронутому пути -> clean (gh недоступен -> не влияет на clean по base)
        r2 = preflight(repo, "main", ["other.txt"])
        # v3.0.13 (блок C): было тавтологичное `verdict in (clean, collision)` (всегда истинно). Нетронутый
        # путь без active-work и без открытых PR (gh unavailable != collision) -> детерминированно clean.
        expect("clean: нетронутый путь -> verdict=clean (не ложная collision)", r2["verdict"] == "clean")
        expect("clean: base_changes пуст для нетронутого пути", r2["base_changes"] == [])

        # active-work overlap по зонам
        aw = repo / "aw.yaml"
        aw.write_text("schema_version: 1\nkind: active-work\nactive:\n"
                      "  - {id: x, branch: feature/x, status: in-progress, "
                      "affected_areas: [materials-page], owner_session: s}\n", encoding="utf-8")
        r3 = preflight(repo, "main", ["other.txt"], areas=["materials-page"], active_work_path=str(aw))
        expect("collision: пересечение по зоне с реестром",
               r3["verdict"] == "collision" and any(a["id"] == "x" for a in r3["active_work_overlap"]))

        # v3.0.13 (блок C, тест-гэп): DONE-запись в той же зоне НЕ создаёт ложный overlap (stale-skip) —
        # именно тот ложный сигнал, который этот инструмент обязан подавлять.
        aw_done = repo / "aw_done.yaml"
        aw_done.write_text("schema_version: 1\nkind: active-work\nactive:\n"
                           "  - {id: olddone, branch: feature/old, status: done, "
                           "affected_areas: [materials-page], owner_session: s}\n", encoding="utf-8")
        r_done = preflight(repo, "main", ["other.txt"], areas=["materials-page"],
                           active_work_path=str(aw_done))
        expect("v3.0.13 stale-skip: done-запись в той же зоне НЕ даёт overlap (не ложный сигнал)",
               all(a.get("id") != "olddone" for a in r_done["active_work_overlap"]))

        # база недоступна -> base_changes 'unknown', не выдаём за clean молча
        r4 = preflight(repo, "origin/nonexistent", ["f.txt"])
        expect("нет базы -> base_changes unknown", isinstance(r4["base_changes"], str))

        # REST-фоллбэк без токена -> честный unavailable (сеть не трогаем)
        _saved = {k: os.environ.pop(k, None) for k in ("GITHUB_TOKEN", "GH_TOKEN")}
        try:
            rest = open_prs_via_rest(repo, ["f.txt"])
            expect("REST без токена -> unavailable (не clean молча)",
                   rest["status"] == "unavailable" and "GITHUB_TOKEN" in rest["note"])
        finally:
            for k, v in _saved.items():
                if v is not None:
                    os.environ[k] = v

    # разбор owner/repo из разных форм remote URL (чистая функция)
    expect("parse: https .git", _parse_owner_repo("https://github.com/acme/widget.git") == ("acme", "widget"))
    expect("parse: https без .git", _parse_owner_repo("https://github.com/acme/widget") == ("acme", "widget"))
    expect("parse: ssh scp-стиль", _parse_owner_repo("git@github.com:acme/widget.git") == ("acme", "widget"))
    expect("parse: мусор -> None", _parse_owner_repo("не-url") is None)

    # чистая логика пересечения PR (без сети)
    recs = [{"number": 7, "title": "A", "files": ["src/a.ts", "src/b.ts"]},
            {"number": 8, "title": "B", "files": ["docs/x.md"]}]
    hits = _prs_overlap(recs, ["src/b.ts"])
    expect("overlap: PR#7 трогает целевой путь", len(hits) == 1 and hits[0]["number"] == 7
           and hits[0]["shared_paths"] == ["src/b.ts"])
    expect("overlap: непересекающийся -> пусто", _prs_overlap(recs, ["src/c.ts"]) == [])

    print("concurrency_preflight selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    ap = argparse.ArgumentParser(prog="concurrency_preflight.py")
    ap.add_argument("--paths", required=True, help="целевые/изменённые пути через запятую")
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--areas", help="зоны для сверки с реестром активных работ")
    ap.add_argument("--active-work", dest="active_work")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    paths = [x.strip() for x in a.paths.split(",") if x.strip()]
    areas = [x.strip() for x in (a.areas or "").split(",") if x.strip()]
    r = preflight(Path(a.repo), a.base, paths, areas, a.active_work)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print_human(r)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
