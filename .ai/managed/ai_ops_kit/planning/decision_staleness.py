"""Продюсер устаревания решений (#635): решение с истёкшими предпосылками помечается stale + причина.

Прямое продолжение evidence: не «старое = плохое», а «предпосылки решения изменились — не рекомендую
автоматически продолжать его использовать». Два сигнала: ВОЗРАСТ (дни от даты решения) И ИЗМЕНЕНИЕ
связанных файлов после неё (git). Advisory, не блок; решение остаётся за человеком.

Честность (как во всём ките): связь «решение -> файлы» объявляется ОПЦИОНАЛЬНО
(`episode.related_files`). Не объявлена -> число изменений `unavailable`, а не «не менялось»:
устаревшим тогда НЕ помечаем, потому что доказательства сдвига предпосылок нет. Помечаем только когда
и возраст выше порога, И связанные файлы реально менялись — тогда предпосылки могли уйти.

Нового реестра не заводит: читает `decisions/registry.yaml` (у дочки — под `.ai/project/`), git — через
`shared.gitio`. Формат находки — факт, как в `planning/staleness.py`. Потребитель уже есть:
`engine/run_handoff.py` читает `handoff.decisions[].stale`; сурфейс — раздел «чего не спрашивали» в `next`.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import yaml

from ai_ops_kit.shared import gitio

# Реестры решений: сначала корневой (кит/форк), затем зона дочки.
REGISTRY_RELS = ("decisions/registry.yaml", ".ai/project/decisions/registry.yaml")
# Порог возраста — как у doc-staleness (repo_audit.STALE_AFTER_DAYS): ниже него решение не кандидат.
DECISION_STALE_AFTER_DAYS = 180


def _load_episodes(root) -> list:
    for rel in REGISTRY_RELS:
        p = Path(root) / rel
        if not p.is_file():
            continue
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return []
        eps = data.get("episodes")
        return eps if isinstance(eps, list) else []
    return []


def _parse_date(s):
    try:
        return _dt.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _changes_since(root, path: str, since_iso: str):
    """Сколько коммитов тронуло `path` начиная с даты решения. None — git не дал ответа (unavailable)."""
    rc, out, _ = gitio.git(root, "rev-list", "--count", f"--since={since_iso}", "HEAD", "--", path)
    if rc != 0:
        return None
    try:
        return int((out or "0").strip())
    except ValueError:
        return None


def assess_decisions(root, today=None) -> list[dict]:
    """Список устаревших решений (advisory). Каждый элемент:
    {id, age_days, files_changed, related_files, stale: True, reason}.

    Помечаем stale ТОЛЬКО когда возраст > порога И связанные файлы реально менялись (предпосылки
    могли уйти). Нет `related_files` или git не дал числа -> изменения unavailable -> НЕ помечаем.
    """
    today = today or _dt.date.today()
    out: list[dict] = []
    for ep in _load_episodes(root):
        if not isinstance(ep, dict):
            continue
        d = _parse_date(ep.get("date"))
        if d is None:
            continue
        age = (today - d).days
        if age <= DECISION_STALE_AFTER_DAYS:
            continue                                   # молодое решение — не кандидат
        related = ep.get("related_files")
        if not isinstance(related, list) or not related:
            continue                                   # связь не объявлена -> unavailable, не помечаем
        changed, measured = 0, False
        for f in related:
            n = _changes_since(root, str(f), d.isoformat())
            if n is not None:
                changed += n
                measured = True
        if not measured or changed <= 0:               # git молчит (unavailable) или файлы не менялись
            continue
        out.append({
            "id": ep.get("id"), "age_days": age, "files_changed": changed,
            "related_files": [str(f) for f in related], "stale": True,
            "reason": (f"принято {age} дн. назад, связанные файлы изменились {changed} раз(а) с тех "
                       "пор — предпосылки могли измениться, стоит пересмотреть"),
        })
    return out
