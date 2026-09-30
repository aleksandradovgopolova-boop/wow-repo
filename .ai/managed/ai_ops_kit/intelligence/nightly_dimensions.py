#!/usr/bin/env python3
"""Оси ночного обзора: находки разложены по НАЗВАННЫМ осям, фокус РОТИРУЕТСЯ (read-only).

ПОВОД. Обзор `nightly_review` перечислял находки ОДНИМ плоским списком. По списку не видно, какие
измерения продукта вообще смотрели, а какие остались молча непросмотренными: «расхождений нет»
неотличимо от «сюда не заглядывали». Работа `nightly-review-organizes-and-rotates-dimensions`
раскладывает находки по названным осям (тесты, архитектура, безопасность, документация, продукт/UX,
аналитика) и РОТИРУЕТ фокус — каждый прогон подсвечивает СЛЕДУЮЩУЮ ось, за цикл в фокус попадают
все, ничто не остаётся без внимания навсегда.

ОСИ НАЗВАНЫ ПО КАТАЛОГУ РЕВЬЮЕРОВ `agents/quality/`, А НЕ ВЫДУМАНЫ. Каждая ось объявляет ревьюеров,
которые её закрывают (их имена — и есть имя оси), и детерминированные проверки `run_checks`, что к
ней относятся. Ревьюеры проверяемы: `declared_reviewers()` называет их поимённо, и тест сверяет, что
каждый существует в каталоге, — объявление честное, не на словах.

ЧЕСТНОСТЬ ПРЕВЫШЕ ПОЛНОТЫ. Ось без наблюдаемого В ЭТОМ РЕПОЗИТОРИИ сигнала помечается «не
наблюдается здесь», а НЕ «ок». «Нет сигнала» ≠ «всё хорошо»: это «не смотрели», и так и сказано. У
части осей (тесты, безопасность) детерминированного сигнала в этой среде нет — их полный обзор за
AI-ревьюером, а он в среде без ключа молчит (awaiting_reviewer). Это граница v0, названная прямо, а
не молчаливая дыра.

ГРАНИЦА v0: обзор НИЧЕГО НЕ ПРАВИТ В ПРОДУКТЕ. Единственная новая запись — курсор ротации фокуса,
своё состояние обзора (как `confirmed_at`/история), а не продуктовый код или данные.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# ОСИ. Порядок фиксирован — по нему идёт round-robin ротация фокуса. `reviewers` — имена файлов в
# `agents/quality/` (без `.md`); `checks` — заголовки проверок `run_checks` (nightly_collectors.CHECKS),
# что относятся к оси. Пустой `checks` = у оси НЕТ детерминированного сигнала в этой среде -> честное
# «не наблюдается здесь», полный обзор за AI-ревьюером (вне v0).
DIMENSIONS = (
    {"key": "tests", "title": "тесты",
     "reviewers": ("test-engineer", "regression-analyst"), "checks": ()},
    {"key": "architecture", "title": "архитектура",
     "reviewers": ("architecture-reviewer",), "checks": ("артефакты",)},
    {"key": "security", "title": "безопасность",
     "reviewers": ("security-reviewer", "ai-red-teamer"), "checks": ()},
    {"key": "documentation", "title": "документация",
     "reviewers": ("documentation-reviewer",),
     "checks": ("документация", "ссылки", "заявления")},
    {"key": "product-ux", "title": "продукт и UX",
     "reviewers": ("ux-reviewer", "product-reviewer", "accessibility-reviewer",
                   "design-system-reviewer"),
     "checks": ("план работы",)},
    {"key": "analytics", "title": "аналитика и наблюдаемость",
     "reviewers": ("analytics-reviewer", "observability-reviewer"),
     "checks": ("события", "поступление событий")},
)

# КУРСОР РОТАЦИИ — РЯДОМ С ОСТАЛЬНЫМ СОСТОЯНИЕМ ОБЗОРА (`last-confirmed.json`, `history.yaml`,
# `feedback/`). Это состояние самого обзора, а не продуктовые данные: граница v0 цела.
FOCUS_CURSOR_REL = ".ai/project/nightly-review/focus-cursor.json"


def axis_keys() -> list[str]:
    """Ключи осей в порядке ротации."""
    return [d["key"] for d in DIMENSIONS]


def declared_reviewers() -> set[str]:
    """Все ревьюеры, объявленные осями (для сверки, что имена не выдуманы)."""
    return {r for d in DIMENSIONS for r in d["reviewers"]}


def group_findings_by_axis(findings) -> list[dict]:
    """Находки `run_checks`, разложенные по названным осям. -> список dict в порядке DIMENSIONS.

    Каждая ось: {key, title, reviewers, has_signal_source, status, findings, clean, unknown}.
    `status`:
      · "расхождения"    — ≥1 доказанное расхождение (`ok is False`);
      · "чисто"          — относящиеся проверки шли и расхождений не нашли;
      · "не наблюдается" — наблюдаемого сигнала В ЭТОМ РЕПОЗИТОРИИ нет (у оси нет детерминированной
                           проверки ЛИБО всё, что к ней относится, не проверено — `ok is None`).
    «Не наблюдается» НЕ сворачивается в «ок»: нет сигнала — это «не смотрели», а не «всё хорошо».
    """
    by_check: dict = {}
    for f in findings or []:
        by_check.setdefault(f.get("check"), []).append(f)
    out = []
    for dim in DIMENSIONS:
        mine = [f for c in dim["checks"] for f in by_check.get(c, [])]
        bad = [f for f in mine if f.get("ok") is False]
        good = [f for f in mine if f.get("ok") is True]
        unknown = [f for f in mine if f.get("ok") is None]
        if bad:
            status = "расхождения"
        elif good:
            status = "чисто"
        else:
            status = "не наблюдается"
        out.append({
            "key": dim["key"], "title": dim["title"], "reviewers": list(dim["reviewers"]),
            "has_signal_source": bool(dim["checks"]),
            "status": status, "findings": bad, "clean": good, "unknown": unknown,
        })
    return out


def axis_finding_counts(findings) -> dict:
    """{ключ оси: число доказанных расхождений (`ok is False`) на этой оси} — вход в недельный тренд.

    В счёт идут ТОЛЬКО находки (`ok is False`), как и в `finding_counts`: «не проверено» тренду не
    считается — нельзя мерить направление того, чего обзор не утверждал. Ось без находок в счётчики
    не попадает; при сравнении её отсутствие читается как 0.
    """
    per_check: dict = {}
    for f in findings or []:
        if f.get("ok") is False:
            per_check[f.get("check")] = per_check.get(f.get("check"), 0) + 1
    counts: dict = {}
    for dim in DIMENSIONS:
        n = sum(per_check.get(c, 0) for c in dim["checks"])
        if n:
            counts[dim["key"]] = n
    return counts


def read_focus_cursor(root: Path) -> str | None:
    """Последняя подсвеченная ось (ключ) или None. Битую запись НЕ считаем отсутствием — просто
    начинаем ротацию с начала, а не роняем прогон."""
    p = Path(root) / FOCUS_CURSOR_REL
    if not p.is_file():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc.get("last_focus") if isinstance(doc, dict) else None


def rotate_focus(root: Path, *, advance: bool = True) -> str:
    """Ось в фокусе ЭТОГО прогона (round-robin по DIMENSIONS). -> ключ оси.

    `advance=True` (реальный ночной прогон) — сдвигает курсор на следующую ось и ПЕРСИСТИТ его, чтобы
    фокус двигался и за цикл прошли все оси. `advance=False` (просто показать бриф) — только
    заглядывает, состояние не трогает: показ и подтверждение обзора не должны двигать ротацию.
    """
    keys = axis_keys()
    last = read_focus_cursor(root)
    idx = (keys.index(last) + 1) % len(keys) if last in keys else 0
    focus = keys[idx]
    if advance:
        p = Path(root) / FOCUS_CURSOR_REL
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"schema_version": 1, "kind": "NightlyReviewFocusCursor",
               "last_focus": focus, "at": datetime.now().isoformat()}
        p.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return focus


def format_dimensions(groups, *, focus: str | None = None) -> list[str]:
    """Строки раздела «Что я проверил — по осям» для брифа: каждая ось своим блоком, фокус помечен."""
    L = ["Каждый прогон подсвечивает следующую ось (ротация) — за цикл в фокус попадают все.", ""]
    for g in groups:
        head = f"### {g['title']}"
        if g["key"] == focus:
            head += " — **в фокусе сегодня**"
        L.append(head)
        if g["status"] == "расхождения":
            for f in g["findings"]:
                L.append(f"- **{f['check']}** ({f.get('subject', '')}): {f.get('detail', '')}")
        elif g["status"] == "чисто":
            L.append("- проверено, расхождений не найдено.")
        elif g["has_signal_source"]:
            # Проверка для оси есть, но в этом репозитории не отработала (нет артефакта / не
            # поставлена) — это «не проверено», а не «ок». Называем, чем именно.
            why = "; ".join(u.get("detail", "") for u in g["unknown"]) or "проверить нечем"
            L.append(f"- не наблюдается здесь: {why}. Это не «ок» — это «не смотрели».")
        else:
            L.append("- не наблюдается здесь: детерминированного сигнала в этом репозитории нет. "
                     "Это не «ок» — это «не смотрели»; полный обзор оси за AI-ревьюером "
                     f"({', '.join(g['reviewers'])}), вне v0.")
        if g["status"] == "чисто" and g["unknown"]:
            why = "; ".join(u.get("detail", "") for u in g["unknown"])
            L.append(f"- часть не проверена: {why}.")
        L.append("")
    return L


def format_axis_trends(trend: dict) -> list[str]:
    """Строки тренда ПО ОСЯМ для брифа (переводит ключ оси в её название). «Нет истории» — как есть."""
    if not trend.get("has_history"):
        return ["Тренд по осям: истории для сравнения пока нет — направление появится, когда "
                "накопятся записи."]
    rows = trend.get("rows") or []
    if not rows:
        return ["Тренд по осям: находок не было ни тогда, ни сейчас — сравнивать нечего."]
    titles = {d["key"]: d["title"] for d in DIMENSIONS}
    L = ["Тренд по осям (сравнение с обзором ~недельной давности):"]
    for r in rows:
        L.append(f"- **{titles.get(r['check'], r['check'])}**: было {r['was']} → стало {r['now']}: "
                 f"{r['direction']}.")
    return L
