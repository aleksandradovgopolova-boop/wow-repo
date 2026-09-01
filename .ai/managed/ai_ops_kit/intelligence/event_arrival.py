#!/usr/bin/env python3
"""event_arrival.py — события ДОЕЗЖАЮТ, а не только объявлены.

ПОЧЕМУ ЭТО ДВА РАЗНЫХ ФАКТА. Каталог событий (`analytics/events.yaml`) отвечает на вопрос «что мы
обещали слать»; `validate_event_catalog` проверяет его форму, связность имён и дрейф кода. Ни один
из этих ответов не говорит, что событие ХОТЬ РАЗ пришло в аналитику. Цепочка продукта —
Outcome Contract → Tracking Plan → реализация → **поступление** → Product Health → инсайты — рвётся
ровно здесь, и рвётся молча: план выглядит выполненным, дашборд пустой, а узнают об этом через
неделю, когда понадобится цифра.

Инвариант тот же, что у учёта стоимости: **`unavailable` не равно нулю**. Нет доказательства
поступления — это `unknown`, а не «события не доезжают» и тем более не «всё в порядке».

ОТКУДА БЕРЁТСЯ ДОКАЗАТЕЛЬСТВО. Кит не ходит в чужую аналитику: провайдер, ключи и приватность —
territория продукта, и лезть туда значило бы просить доступ, который нельзя дать безопасно.
Дочка САМА кладёт выгрузку в конвенциональное место — тот же приём, что у UI-evidence
(`ui/storybook_adapter` читает артефакты Storybook, а не запускает браузер).

Форма выгрузки намеренно минимальна — имя события и сколько раз его видели:

    {"schema_version": 1, "kind": "EventArrivalEvidence",
     "collected_at": "2026-08-20T03:00:00Z", "window": "24h", "source": "posthog",
     "events": {"task.completed": 128, "object.version_created": 4}}

Больше кит не просит: имени и счётчика хватает, чтобы ответить на вопрос «доехало ли», а любые
свойства события — это уже данные продукта, и им незачем покидать его периметр.

Использование:
    event_arrival.py [child_root] [--json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
           Path(__file__).resolve().parents[2])

# Где дочка кладёт выгрузку. Первый существующий побеждает — те же конвенции, что у UI-evidence.
EVIDENCE_PATHS = (".ai/analytics/events-seen.json",
                  "analytics/events-seen.json",
                  "test-results/events-seen.json")

# Где лежит каталог объявленных событий.
CATALOG_PATHS = ("analytics/events.yaml", ".ai/project/analytics/events.yaml")

# Событие, объявленное для аналитики, ОБЯЗАНО доезжать. `domain` и `audit` — внутренние записи
# продукта: они могут не уходить во внешнюю аналитику вовсе, и требовать этого было бы придиркой.
ARRIVAL_REQUIRED_KINDS = ("analytics",)


def _read_yaml(p: Path):
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None


def _first(root: Path, rels) -> Path | None:
    for rel in rels:
        p = Path(root) / rel
        if p.is_file():
            return p
    return None


def declared_events(root: Path) -> dict:
    """Объявленные события. -> {"events": {имя: kind}, "path": str|None, "error": str|None}."""
    p = _first(root, CATALOG_PATHS)
    if p is None:
        return {"events": {}, "path": None, "error": "каталога событий нет"}
    doc = _read_yaml(p)
    if not isinstance(doc, dict):
        return {"events": {}, "path": str(p), "error": "каталог не разобран"}
    out = {}
    for e in doc.get("events") or []:
        if isinstance(e, dict) and e.get("name"):
            out[str(e["name"])] = str(e.get("kind") or "domain")
    return {"events": out, "path": str(p), "error": None}


def arrival_evidence(root: Path) -> dict:
    """Выгрузка поступления. -> {"events": {имя: счётчик}, "path", "meta", "error"}.

    Отсутствие выгрузки — ЗАКОННОЕ состояние (не каждый продукт её кладёт), и оно даёт `unknown`,
    а не «не доезжает»: обвинить продукт в том, что мы не смотрели, — та же ложь, только с другим
    знаком.
    """
    p = _first(root, EVIDENCE_PATHS)
    if p is None:
        return {"events": {}, "path": None, "meta": {},
                "error": "выгрузки поступления нет — положите её в " + EVIDENCE_PATHS[0]}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"events": {}, "path": str(p), "meta": {},
                "error": f"выгрузка не разобрана ({type(e).__name__}: {e})"}
    if not isinstance(doc, dict) or not isinstance(doc.get("events"), dict):
        return {"events": {}, "path": str(p), "meta": {},
                "error": "в выгрузке нет объекта `events` {имя: счётчик}"}
    meta = {k: doc.get(k) for k in ("collected_at", "window", "source") if doc.get(k)}
    return {"events": {str(k): v for k, v in doc["events"].items()}, "path": str(p),
            "meta": meta, "error": None}


def assess(child_root) -> dict:
    """Сверить объявленное с доехавшим. -> отчёт.

    Состояния события:
      arrived      — объявлено и пришло;
      missing      — объявлено, выгрузка ЕСТЬ, а события в ней нет (настоящая находка);
      undeclared   — пришло, но в каталоге его нет (дрейф кода в другую сторону);
      unknown      — выгрузки нет или она не читается: проверить нечем.
    """
    root = Path(child_root).resolve()
    decl = declared_events(root)
    ev = arrival_evidence(root)
    required = {n: k for n, k in decl["events"].items() if k in ARRIVAL_REQUIRED_KINDS}

    if ev["error"] or decl["error"]:
        reason = decl["error"] or ev["error"]
        return {"schema_version": 1, "kind": "EventArrivalReport", "root": str(root),
                "checked": False, "reason": reason,
                "declared": len(decl["events"]), "arrival_required": len(required),
                "arrived": [], "missing": [], "undeclared": [], "unknown": sorted(required),
                "evidence": ev["meta"], "verdict": f"не проверено: {reason}"}

    seen = ev["events"]
    arrived = sorted(n for n in required if seen.get(n))
    missing = sorted(n for n in required if not seen.get(n))
    undeclared = sorted(n for n in seen if n not in decl["events"])
    verdict = ("все объявленные для аналитики события доезжают"
               if not missing and not undeclared else
               f"не доезжает: {len(missing)}; вне каталога: {len(undeclared)}")
    return {"schema_version": 1, "kind": "EventArrivalReport", "root": str(root),
            "checked": True, "reason": None,
            "declared": len(decl["events"]), "arrival_required": len(required),
            "arrived": arrived, "missing": missing, "undeclared": undeclared,
            "unknown": [], "evidence": ev["meta"], "verdict": verdict}


def render(rep: dict) -> str:
    L = []
    if not rep.get("checked"):
        L.append(f"События: {rep['verdict']}")
        L.append(f"  объявлено: {rep['declared']}, из них требуют поступления: "
                 f"{rep['arrival_required']} — состояние каждого неизвестно")
        return "\n".join(L)
    m = rep.get("evidence") or {}
    window = f" (окно {m['window']}, источник {m.get('source', '—')})" if m.get("window") else ""
    L.append(f"События{window}: {rep['verdict']}")
    if rep["arrived"]:
        L.append(f"  доезжают: {len(rep['arrived'])}")
    for n in rep["missing"]:
        L.append(f"  ✗ объявлено и НЕ доезжает: {n}")
    for n in rep["undeclared"]:
        L.append(f"  · доезжает, но нет в каталоге: {n}")
    return "\n".join(L)


def main(argv):
    root = "."
    js = "--json" in argv
    for a in argv[1:]:
        if not a.startswith("-"):
            root = a
            break
    rep = assess(root)
    print(json.dumps(rep, ensure_ascii=False, indent=2) if js else render(rep))
    # Ненулевой код ТОЛЬКО на настоящей находке. «Не проверено» не отказ: иначе продукт без
    # выгрузки краснел бы вечно, и проверку выключили бы целиком.
    return 1 if rep.get("missing") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
