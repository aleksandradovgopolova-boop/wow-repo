#!/usr/bin/env python3
"""Недельный тренд ночного обзора: не только снимок, а «лучше или хуже за неделю» (read-only).

ПОВОД. Обзор `nightly_review` показывал СНИМОК — расхождения с последнего подтверждённого обзора
(«что изменилось со вчера»). Владелец видел сегодняшнее состояние, но не направление: качество
растёт или деградирует за неделю. Работа `nightly-review-persists-and-trends-findings` добавляет
ось времени.

ЧТО ЗДЕСЬ:
  · `record_history` — при обзоре дописывает ОДНУ запись (дата + счётчики находок по каждой
    проверке) в свой файл истории. Это СОБСТВЕННОЕ состояние обзора, как `confirmed_at`, а не
    правка продукта: граница v0 (обзор ничего не правит в продукте) цела.
  · `compute_trends` — сравнивает текущие находки с обзором ~недельной давности и называет
    направление по каждой проверке: было N → стало M: лучше / хуже / без изменений.

ЧЕСТНОСТЬ ПРЕВЫШЕ ПОЛНОТЫ. Нет записи для сравнения (первый прогон / за неделю истории ещё нет) —
тренд НЕ выдумывается: обзор прямо говорит «истории для тренда пока нет». «Нет истории» — это не
«без изменений»: сравнивать не с чем, и об этом сказано, а не подменено нулём.
"""
from __future__ import annotations

import yaml
from datetime import datetime
from pathlib import Path

# ХРАНЕНИЕ — РЯДОМ С ОСТАЛЬНЫМ СОСТОЯНИЕМ ОБЗОРА (`last-confirmed.json`, `feedback/`, `briefs/`).
# Один файл-журнал: как `confirmed_at`, это состояние самого обзора, а не продуктовые данные.
HISTORY_REL = ".ai/project/nightly-review/history.yaml"

# ОКНО ТРЕНДА — НЕДЕЛЯ. Обзор ежедневный; недельное окно показывает направление, не шум суток.
TREND_WINDOW_DAYS = 7


def finding_counts(findings) -> dict:
    """Счётчики находок по каждой проверке: {check: число доказанных расхождений (`ok is False`)}.

    В счёт идут ТОЛЬКО находки (`ok is False`) — то, что обзор действительно заявил. «Не проверено»
    (`ok is None`) находкой не считается: нельзя мерить тренд того, чего обзор не утверждал. Проверка
    без расхождения в счётчики не попадает; при сравнении её отсутствие читается как 0.
    """
    counts: dict = {}
    for f in findings or []:
        if f.get("ok") is False:
            check = f.get("check")
            counts[check] = counts.get(check, 0) + 1
    return counts


def read_history(root: Path) -> list:
    """Все записи истории обзора (по возрастанию времени). Битый/пустой файл -> [] (не роняем прогон)."""
    p = Path(root) / HISTORY_REL
    if not p.is_file():
        return []
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(doc, dict):
        return []
    entries = doc.get("entries")
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def record_history(root: Path, findings, *, at: str | None = None,
                   axis_counts: dict | None = None) -> dict:
    """Дописать ОДНУ запись за прогон: дата + счётчики находок по каждой проверке. -> запись.

    Append-only: прежние записи не переписываются, только добавляется свежая, — так копится ось
    времени. Это единственная запись обзора в дочку помимо его собственного состояния (границу v0
    не нарушает: файл истории — артефакт самого обзора, а не продуктовый код/данные).

    `axis_counts` — счётчики находок ПО ОСЯМ (nightly_dimensions.axis_finding_counts) — ложатся в
    ТУ ЖЕ запись того же журнала, а не во второй: оси питают уже готовый недельный тренд (#1053), а
    не заводят собственную историю. Не передан — запись просто без осевой строки (обратная
    совместимость со старыми записями).
    """
    entry = {"at": at or datetime.now().isoformat(), "counts": finding_counts(findings)}
    if axis_counts is not None:
        entry["axis_counts"] = dict(axis_counts)
    entries = read_history(root)
    entries.append(entry)
    p = Path(root) / HISTORY_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {"schema_version": 1, "kind": "NightlyReviewHistory", "entries": entries}
    p.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return entry


def _parse_iso(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def trend_baseline_entry(history, *, now: datetime | None = None,
                         window_days: int = TREND_WINDOW_DAYS):
    """Запись, с которой честно сравнивать «за неделю». -> (entry, age_days) | (None, None).

    ПРАВИЛО (называется в брифе): берём САМУЮ СВЕЖУЮ запись возрастом ≥ недели — она ближе всего к
    ровно неделе снизу. Если такой ещё нет, но записи за неделю есть — берём САМУЮ СТАРУЮ из них
    (наибольший доступный размах) и называем её настоящий возраст, а не выдаём за неделю. Записей
    нет вовсе -> (None, None): тренд не с чем считать.
    """
    now = now or datetime.now()
    dated = []
    for e in history:
        dt = _parse_iso(e.get("at"))
        if dt is None:
            continue
        age = (now - dt).total_seconds() / 86400.0
        if age >= 0:
            dated.append((age, e))
    if not dated:
        return None, None
    at_least_week = [(age, e) for age, e in dated if age >= window_days]
    if at_least_week:
        age, e = min(at_least_week, key=lambda t: t[0])  # ближайшая к неделе сверху
        return e, age
    age, e = max(dated, key=lambda t: t[0])              # иначе — самая старая в пределах недели
    return e, age


def trends_from_counts(history, current: dict, counts_key: str, *,
                       now: datetime | None = None) -> dict:
    """Тренд по любому срезу счётчиков относительно обзора ~недельной давности (общая машинерия).

    -> {"has_history", "age_days", "rows": [{check, was, now, direction}], "reason"}.
    `counts_key` — под каким ключом лежат счётчики в записи истории (`counts` — по проверкам,
    `axis_counts` — по осям). `direction` ∈ {"лучше", "хуже", "без изменений"} по числу находок
    (меньше = лучше). Строки только там, где находки были тогда или сейчас (обе 0 — сравнивать не о чём).
    """
    base, age = trend_baseline_entry(history, now=now)
    if base is None:
        reason = ("истории для тренда пока нет — это первый обзор либо записей за неделю ещё "
                  "не накопилось; сравнивать не с чем")
        return {"has_history": False, "age_days": None, "rows": [], "reason": reason}
    old = base.get(counts_key) or {}
    rows = []
    for check in sorted(set(old) | set(current)):
        was = int(old.get(check, 0))
        now_count = int(current.get(check, 0))
        if now_count == was:
            direction = "без изменений"
        elif now_count > was:
            direction = "хуже"
        else:
            direction = "лучше"
        rows.append({"check": check, "was": was, "now": now_count, "direction": direction})
    reason = (f"сравнение с обзором возрастом ~{age:.0f} дн. (ближайший к неделе); "
              f"направление по числу находок")
    return {"has_history": True, "age_days": age, "rows": rows, "reason": reason}


def compute_trends(history, current_findings, *, now: datetime | None = None) -> dict:
    """Тренд по каждой ПРОВЕРКЕ относительно обзора ~недельной давности."""
    return trends_from_counts(history, finding_counts(current_findings), "counts", now=now)


def compute_axis_trends(history, axis_counts, *, now: datetime | None = None) -> dict:
    """Тренд по ОСЯМ — та же машинерия и ТОТ ЖЕ журнал, что у `compute_trends`, не второй.

    Счётчики находок по осям (nightly_dimensions.axis_finding_counts) сравниваются с осевыми
    счётчиками записи ~недельной давности из общей истории обзора.
    """
    return trends_from_counts(history, dict(axis_counts or {}), "axis_counts", now=now)


def _human_age(age_days: float | None) -> str:
    if age_days is None:
        return "неизвестной давности"
    days = round(age_days)
    if days <= 0:
        return "менее суток"
    return f"{days} дн."


def format_trends(trend: dict) -> list[str]:
    """Строки раздела «Тренд за неделю» для брифа. «Нет истории» остаётся «нет истории»."""
    if not trend.get("has_history"):
        return [f"Тренд за неделю: {trend.get('reason', 'истории пока нет')}.",
                "«Нет истории» — это не «без изменений»: направление появится, когда накопятся записи."]
    L = [f"Сравнение с обзором {_human_age(trend.get('age_days'))} назад "
         f"(ближайший к неделе):"]
    rows = trend.get("rows") or []
    if not rows:
        L.append("- находок не было ни тогда, ни сейчас — сравнивать нечего.")
        return L
    for r in rows:
        L.append(f"- **{r['check']}**: было {r['was']} (тогда) → стало {r['now']} (сейчас): "
                 f"{r['direction']}.")
    return L
