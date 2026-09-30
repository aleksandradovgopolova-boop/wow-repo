#!/usr/bin/env python3
"""Персистентный объект review-вердикта: «кто/что проверил функцию» как ЗАПИСЬ, а не эхо прогона.

ЗАЧЕМ. До сих пор «Проверено» существовало ТОЛЬКО в прогоне: `gate_executor.evidence_verdict` делит
пройденные гейты по источнику доверия (детерминированная машина / независимый ревьюер / человек), а
`shared.gate_dimensions` превращает их в уровни знания для readout (#985). Но как только прогон
заканчивался, вердикт исчезал — персистентного объекта не было, и нить «история жизни продукта» рвалась
на звене «фича → кто/что проверил» (замер PRODUCT-LIFE-HISTORY-CONTINUITY-2026-09-17, разрыв №4).
Привязать «проверку» к работе, которая функцию ПОСТРОИЛА, нельзя — это нарушило бы инвариант
писатель ≠ судья. Нужен отдельный объект, который пишет ПУТЬ РЕВЬЮ (независимый судья), а не писатель.

ЧТО ЭТО. Тонкий слой над УЖЕ существующим содержанием вердикта (веха 4.2, #588): запись НЕ вводит
новую модель проверки, а сохраняет `evidence_verdict` (какие гейты пройдены и КЕМ) рядом с фичей —
`features/<feature>/review/verdict.yaml`, — плюс когда и на какой ревизии. Knowledge Graph читает эту
запись и строит узел `review` + ребро `review -reviewed-> feature`; `graph trace` называет, кто проверил.

ЧЕСТНОСТЬ. Нет записи — нет узла и нет ребра: `graph trace` честно молчит «проверка не записана», а НЕ
объявляет пробел (проверки могло не быть — это не то же, что «должна была быть, но нет»). Ничего не
проверено (ни машиной, ни ревьюером, ни человеком) — `persist` НЕ пишет запись: пустой вердикт хуже
его отсутствия. Запись, ссылающаяся на несуществующую фичу, оставляет висящее ребро — и
`validate_knowledge_graph` отвергает граф целиком (сломанная запись обязана быть громкой).

Слой: `shared` (foundation) — как и `gate_dimensions`, которую переиспользует. Её вправе звать и
`engine` (путь ревью, пишет запись), и `intelligence` (Knowledge Graph, читает запись): оба импортят
ВНИЗ, инвариант слоёв не нарушен. Пишем/читаем обычный YAML — новой зависимости нет.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from ai_ops_kit.shared import gate_dimensions

RECORD_KIND = "review-verdict"

# Источник доверия -> человеко-ярлык «кем проверено». Порядок = от машины к человеку (как в readout).
_SOURCE_LABELS = (
    ("deterministic", "машина"),
    ("ai_judgment", "независимый ревьюер"),
    ("human", "человек"),
)


def record_rel(feature: str) -> str:
    """Путь записи вердикта ОТНОСИТЕЛЬНО корня репозитория (рядом с паспортом функции)."""
    return f"features/{feature}/review/verdict.yaml"


def record_path(child_root: str | Path, feature: str) -> Path:
    return Path(child_root) / "features" / str(feature) / "review" / "verdict.yaml"


def _checked_by(evidence_verdict: dict) -> dict:
    """Три списка пройденных гейтов по источнику доверия — из `evidence_verdict` (веха 4.2, #588)."""
    ev = evidence_verdict or {}
    return {"deterministic": list(ev.get("deterministic") or []),
            "ai_judgment": list(ev.get("ai_judgment") or []),
            "human": list(ev.get("human") or [])}


def _anything_checked(checked_by: dict) -> bool:
    return any(checked_by.get(k) for k, _ in _SOURCE_LABELS)


def build_record(feature: str, evidence_verdict: dict, *, revision: str | None = None,
                 at: str | None = None, outcome_verdict: str | None = None) -> dict:
    """Собрать запись review-вердикта из `evidence_verdict` — БЕЗ новой модели проверки.

    Содержание вердикта берётся как есть (какие гейты пройдены и КЕМ); запись лишь добавляет, к какой
    ФУНКЦИИ он относится, КОГДА и на какой РЕВИЗИИ вынесен. `at` по умолчанию — текущее UTC-время без
    микросекунд (без метки времени это не запись, а заметка — то же правило, что у BranchReview).
    """
    checked_by = _checked_by(evidence_verdict)
    ev = evidence_verdict or {}
    at = at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    record = {
        "schema_version": 1,
        "kind": RECORD_KIND,
        "feature": str(feature),
        "reviewed_revision": revision,
        "reviewed_at": at,
        "verified": bool(ev.get("verified")),
        "checked_by": checked_by,
        "reason": ev.get("reason"),
    }
    if outcome_verdict is not None:
        record["outcome_verdict"] = outcome_verdict
    return record


def persist(child_root: str | Path, feature: str, evidence_verdict: dict, *,
            revision: str | None = None, at: str | None = None,
            outcome_verdict: str | None = None) -> str | None:
    """Записать review-вердикт рядом с функцией. -> путь записи (относительный) или None.

    ЧЕСТНОСТЬ: если НИЧЕГО не проверено (ни машиной, ни ревьюером, ни человеком) — запись НЕ пишется и
    возвращается None: пустой вердикт населять историю не должен. Иначе пишется YAML в
    `features/<feature>/review/verdict.yaml` (перезаписывая прошлый — актуальна последняя проверка).
    Сбой записи не роняет вызывающего: возвращаем None, вердикт от наличия файла не зависит.
    """
    record = build_record(feature, evidence_verdict, revision=revision, at=at,
                          outcome_verdict=outcome_verdict)
    if not _anything_checked(record["checked_by"]):
        return None
    path = record_path(child_root, feature)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(record, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
    except OSError:
        return None
    root = Path(child_root)
    return str(path.relative_to(root)) if path.is_relative_to(root) else str(path)


def load(child_root: str | Path, feature: str) -> dict | None:
    """Прочитать запись review-вердикта функции. -> dict или None (нет файла / нечитаемо)."""
    path = record_path(child_root, feature)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) and data.get("kind") == RECORD_KIND else None


def checked_sources(record: dict) -> list[str]:
    """Человеко-ярлыки источников, которые РЕАЛЬНО что-то проверили: машина / ревьюер / человек."""
    cb = (record or {}).get("checked_by") or {}
    return [label for key, label in _SOURCE_LABELS if cb.get(key)]


def who_checked_lines(record: dict) -> list:
    """«Кем и что проверено» продуктовыми словами -> список строк (переиспользует gate_dimensions).

    `checked_by` записи имеет ту же форму, что `evidence_verdict` ждёт `run_verified_lines`, поэтому
    смысл гейтов и атрибуция источника («мнение ≠ машинная проверка») берутся оттуда, а не дублируются.
    """
    return gate_dimensions.run_verified_lines((record or {}).get("checked_by") or {})


def node_title(record: dict) -> str:
    """Короткий заголовок узла review для графа: «проверили: машина, независимый ревьюер»."""
    sources = checked_sources(record)
    if not sources:
        return "проверка (без пройденных измерений)"
    return "проверили: " + ", ".join(sources)
