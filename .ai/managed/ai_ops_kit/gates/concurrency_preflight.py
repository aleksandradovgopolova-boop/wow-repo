#!/usr/bin/env python3
"""Concurrency preflight (v2.28) — проверка коллизий параллельной работы до старта.

Класс проблемы «concurrent-edit collision + stale premise»: два потока независимо меняют
одну поверхность; позже — merge-конфликт и переделки. Хуже — работа на устаревшей
посылке: удаляешь «мёртвый» контрол, а параллельный PR ровно его оживляет.

Реестр активных работ (ai_ops_kit/lifecycle/active_work.py) ловит это, только если оба потока в нём
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
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _git(repo, *args):
    from ai_ops_kit.shared import gitio
    return gitio.git(repo, *args)   # v3.0.13 (блок C): единый git-хелпер с таймаутом


# Допустимый сегмент owner/repo GitHub: буквы/цифры/точка/подчёркивание/дефис. Всё прочее
# (в т.ч. '?', '/', '#', '@', ':' и пробелы) — потенциальная инъекция квери/сегмента в URL API.
_OWNER_REPO_SEG = re.compile(r"^[A-Za-z0-9._-]+$")


def _valid_owner_repo_seg(seg):
    """Сегмент безопасен для подстановки в путь GitHub API: разрешённый набор символов и
    не чистый '.'/'..' (иначе — обход сегмента /repos/{owner}/../…)."""
    return bool(seg) and seg not in (".", "..") and _OWNER_REPO_SEG.match(seg) is not None


def _parse_owner_repo(remote_url):
    """owner/repo из git remote URL (https, ssh, с .git и без). None, если не GitHub-подобный
    ИЛИ owner/repo содержит символы вне [A-Za-z0-9._-] (fail-closed: имя из remote идёт в URL
    GitHub API, а '?'/'..'/'/' в нём подмешивают квери или сегмент — не даём выдать инъекцию
    за валидный repo)."""
    if not remote_url:
        return None
    u = remote_url.strip()
    # git@host:owner/repo(.git)  |  https://host/owner/repo(.git)  |  ssh://git@host/owner/repo
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?$", u)
    if not m:
        return None
    owner, name = m.group(1), m.group(2)
    if not (_valid_owner_repo_seg(owner) and _valid_owner_repo_seg(name)):
        return None
    return owner, name


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
    """Токен из env (GITHUB_TOKEN / GH_TOKEN); при их отсутствии — fallback на `gh auth token`
    (#402: gh и так зависимость кита, и на машине он обычно авторизован — иначе доставка молча
    деградировала до «нет токена» при живой gh-авторизации). В логи/вывод не попадает."""
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return None


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


def _gh_api_get_all(path, token, per_page=100, max_items=None, max_pages=100):
    """Все страницы list-эндпоинта GitHub через page-based пагинацию. Возвращает
    (items, truncated). truncated=True, если остановились по лимиту (max_items или max_pages)
    при ПОЛНОЙ последней странице — значит есть ещё, и «полнота» не гарантирована: усечение
    честнее выдать за partial, чем за checked. Без этого PR с >100 файлами отдавал только
    первые 100 → пересекающийся 101-й путь невидим → ложное «пересечений нет»."""
    sep = "&" if "?" in path else "?"
    items = []
    page = 1
    while page <= max_pages:
        chunk = _gh_api_get(f"{path}{sep}per_page={per_page}&page={page}", token)
        if not isinstance(chunk, list):
            break  # неожиданная форма ответа — не притворяемся, что дочитали
        items.extend(chunk)
        if max_items is not None and len(items) >= max_items:
            # ещё могло остаться за пределами лимита, если последняя страница была полной
            return items[:max_items], len(chunk) == per_page
        if len(chunk) < per_page:
            return items, False  # короткая страница => это был конец
        page += 1
    return items, True  # выбрали max_pages при полной странице => усечение


def open_prs_via_rest(repo, paths, max_prs=30):
    """REST-фоллбэк (без gh): открытые PR, трогающие paths. Токен — из env; иначе unavailable.
    Список PR и файлы каждого PR пагинируются до конца; если пришлось усечь (лимит max_prs
    или страховочный потолок страниц) — статус partial, а не checked."""
    token = _github_token()
    if not token:
        return {"status": "unavailable", "note": "нет gh и нет GITHUB_TOKEN/GH_TOKEN — открытые PR не проверены", "prs": []}
    rc, url, _ = _git(repo, "remote", "get-url", "origin")
    owner_repo = _parse_owner_repo(url) if rc == 0 else None
    if not owner_repo:
        return {"status": "unavailable", "note": "не удалось определить owner/repo из origin", "prs": []}
    owner, name = owner_repo
    # defense-in-depth: даже после валидации сегмента прогоняем через quote(safe="")
    owner_q = urllib.parse.quote(owner, safe="")
    name_q = urllib.parse.quote(name, safe="")
    try:
        prs, prs_truncated = _gh_api_get_all(
            f"/repos/{owner_q}/{name_q}/pulls?state=open", token, max_items=max_prs)
        records = []
        files_truncated = False
        for pr in prs:
            num = pr.get("number")
            files, tr = _gh_api_get_all(
                f"/repos/{owner_q}/{name_q}/pulls/{num}/files", token)
            files_truncated = files_truncated or tr
            records.append({"number": num, "title": pr.get("title"),
                            "files": [f.get("filename") for f in files]})
        truncated = prs_truncated or files_truncated
        out = {"status": "partial" if truncated else "checked", "via": "rest",
               "prs": _prs_overlap(records, paths)}
        if truncated:
            out["note"] = ("список PR или файлов усечён (пагинация не завершена) — "
                           "«пересечений нет» здесь не факт, а неполная проверка")
        return out
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
        r = subprocess.run(["gh", "pr", "list", "--state", "open", "--limit", "100",
                            "--json", "number,title,files"],
                           cwd=str(repo), capture_output=True, text=True)
        if r.returncode != 0:
            return None
        raw = json.loads(r.stdout or "[]")
        records = [{"number": pr.get("number"), "title": pr.get("title"),
                    "files": [f.get("path") for f in (pr.get("files") or [])]}
                   for pr in raw]
        # gh отдаёт максимум 100 файлов на PR и обрезает список PR по --limit; ровно 100 в любом
        # из них неотличимо от усечения → честнее partial, чем checked с «пересечений нет».
        truncated = len(records) >= 100 or any(len(rec["files"]) >= 100 for rec in records)
        out = {"status": "partial" if truncated else "checked", "via": "gh",
               "prs": _prs_overlap(records, paths)}
        if truncated:
            out["note"] = ("gh усёк список PR или файлов (потолок 100) — "
                           "«пересечений нет» здесь неполная проверка")
        return out
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
    status = prs.get("status")
    if status == "unavailable":
        print(f"  · открытые PR: не проверены ({prs.get('note')})")
    elif prs.get("prs"):
        suffix = " [частично: возможны непоказанные]" if status == "partial" else ""
        print(f"  ⚠ открытые PR по тем же путям (via {prs.get('via', '?')}){suffix}: " +
              ", ".join(f"#{p['number']}" for p in prs["prs"]))
    elif status == "partial":
        print(f"  · открытые PR проверены частично (via {prs.get('via', '?')}): "
              "в проверенной части пересечений нет — усечение, «чисто» не гарантировано")
    elif status == "checked":
        print(f"  · открытые PR проверены (via {prs.get('via', '?')}): пересечений нет")
    for a in r["active_work_overlap"]:
        print(f"  ⚠ активная работа '{a['id']}' (ветка {a['branch']}): зоны {', '.join(a['shared_areas'])}")
    for rec in r.get("recommendation", []):
        print(f"  → {rec}")


def main(argv):
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
