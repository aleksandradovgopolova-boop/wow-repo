"""Проверяющая логика кросс-артефактной консистентности, вынесена из
`validation/validate_cross_artifacts.py` вниз (лента №5), чтобы рантайм (lifecycle.run_report) звал
её ВНИЗ, без восходящего ребра lifecycle -> validation.

check_feature читает tracking-plan и dashboard-spec ФУНКЦИИ (read-only I/O); извлечение событий из
markdown — чистые строковые функции. Всё вынесено вниз целиком: `checks` держит проверяющую логику
НИЖЕ entrypoints, зависит только от stdlib и НЕ импортирует ничего из ai_ops_kit выше foundation.
Демо-данные селфтеста (TP_OK/DS_OK/DS_BAD) и CLI остаются в обёртке `validation`.
"""
from __future__ import annotations

import re
from pathlib import Path


EVENT_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
TRACKING = "analytics/tracking-plan.md"
DASHBOARD = "analytics/dashboard-spec.md"


def md_section(text: str, title_re: str) -> str:
    """Вернуть текст раздела '## <title>' до следующего '## '."""
    m = re.search(rf"^##\s+{title_re}.*?$", text, re.MULTILINE | re.IGNORECASE)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"^##\s+", rest, re.MULTILINE)
    return rest[:nxt.start()] if nxt else rest


def declared_events(tracking_text: str):
    """События из первой колонки таблицы раздела Events tracking plan'а."""
    section = md_section(tracking_text, r"Events")
    events = set()
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        first = line.strip("|").split("|")[0].strip().strip("`")
        if re.fullmatch(EVENT_RE.pattern, first):
            events.add(first)
    return events


def used_events(dashboard_text: str):
    """snake_case-токены из колонки Source events и раздела Funnels."""
    used = set()
    for line in dashboard_text.splitlines():
        if line.strip().startswith("|") and "---" not in line:
            cells = [c.strip() for c in line.strip("|").split("|")]
            # эвристика: колонки после третьей в таблице Blocks — source events;
            # надёжнее взять токены из всех ячеек, содержащих snake_case
            for c in cells:
                used.update(EVENT_RE.findall(c))
    used.update(EVENT_RE.findall(md_section(dashboard_text, r"Funnels")))
    return used


def check_feature(feature_dir: Path):
    """-> (problems, warns, skipped_note|None)"""
    tp = feature_dir / TRACKING
    ds = feature_dir / DASHBOARD
    if not ds.exists():
        return [], [], f"{feature_dir.name}: dashboard-spec отсутствует — сверять нечего (skip)"
    if not tp.exists():
        return [f"{feature_dir.name}: dashboard-spec есть, а tracking plan ({TRACKING}) — нет"], [], None
    declared = declared_events(tp.read_text(encoding="utf-8"))
    if not declared:
        return [], [f"{feature_dir.name}: таблица Events в tracking plan не распарсилась — "
                    "сверка пропущена (проверьте формат)"], None
    used = used_events(ds.read_text(encoding="utf-8"))
    problems = [f"{feature_dir.name}: dashboard-spec использует событие '{e}', "
                f"не объявленное в tracking plan" for e in sorted(used - declared)]
    warns = []
    unused = declared - used
    if used and unused:
        warns.append(f"{feature_dir.name}: события объявлены, но не используются "
                     f"в dashboard-spec: {sorted(unused)}")
    return problems, warns, None
