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

МАШИННОЕ ДОКАЗАТЕЛЬСТВО ГЕЙТА. `events_verified_live(child_root)` отображает EventArrivalReport в
одноимённое машинное доказательство гейта analytics_runtime_verification. До этого гейт был
объявлен mechanizable (quality/gate-machinability.yaml), но само доказательство `events_verified_live`
производила ДЕКЛАРАЦИЯ судьи — ровно тот разрыв, который называет исход работы: «verified in
runtime, NOT declared». Теперь оно строится из факта — сверки каталога с выгрузкой поступления.

ПРОВОДКА PRODUCER → ГЕЙТ (#424). Мало ПРОИЗВЕСТИ доказательство — его надо ПОДАТЬ гейту как
источник истины вместо слова судьи. Это делают `gate_evidence_entry` (машинный вклад в evidence
одного гейта) и `verify_analytics_runtime` (зовёт producer и оценивает гейт через
`gates.gate_executor`). Машинный вердикт АВТОРИТЕТЕН для факта `events_verified_live`:
  - met is True  — машина ПОДТВЕРЖДАЕТ приход, доказательство попадает в `provided`;
  - met is False — машина НАХОДИТ, что объявленное не доехало: гейт красный, блокер называет
                   недоехавшее событие, и это НЕ переписывается «зелёным» словом судьи;
  - met is None  — проверить нечем: `events_verified_live` НЕ фабрикуется словом судьи (снимается
                   из `provided`) — «unknown != проверено», и блокирующий гейт честно не закрыт.

НАПРАВЛЕНИЕ ЛЕГАЛЬНО ПО СЛОЯМ (packages/layering.yaml). `intelligence` лежит ВЫШЕ `capabilities`;
зависимость вниз (`intelligence` → `gates`) разрешена, вверх — запрещена. Поэтому проводку держит
`intelligence` (ему видны и producer, и форма evidence гейта), а `gates` НЕ импортирует
`intelligence` — инвариант трёх колец не нарушен, доказательство «поставляется вниз».

Использование:
    event_arrival.py [child_root] [--json]
    event_arrival.py [child_root] --evidence [--json]
    event_arrival.py [child_root] --gate [--json]     # оценить гейт машинным доказательством
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

# Имя машинного доказательства гейта analytics_runtime_verification, которое производит
# events_verified_live() (объявлено в quality/gate-machinability.yaml как mechanizable).
EVIDENCE_NAME = "events_verified_live"

# Гейт, чьё доказательство `events_verified_live` производит этот модуль.
GATE_ID = "analytics_runtime_verification"


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


def events_verified_live(child_root) -> dict:
    """Машинное доказательство `events_verified_live` из выгрузки поступления.

    Замыкает разрыв гейта analytics_runtime_verification: доказательство объявлено mechanizable,
    но до сих пор его производила ДЕКЛАРАЦИЯ судьи. Здесь оно строится из факта — сверки каталога
    с выгрузкой поступления (assess).

    -> {"name", "met": True|False|None, "reason", "detail"}.

    Инвариант `unavailable != zero` сохраняется:
      met is None  — выгрузки/каталога нет или они не читаются: проверить нечем (НЕ провал);
      met is False — выгрузка ЕСТЬ, а объявленное для аналитики событие в ней не встретилось
                     (настоящая находка);
      met is True  — выгрузка есть и все обязанные доезжать (kind=analytics) события доехали.

    Событие вне каталога (дрейф в обратную сторону) НЕ роняет доказательство: это не «объявленное
    не доехало». read-only, без сети — ровно то, что делает assess().
    """
    rep = assess(child_root)
    if not rep.get("checked"):
        return {"name": EVIDENCE_NAME, "met": None, "reason": rep.get("reason"),
                "detail": rep.get("verdict")}
    if rep["missing"]:
        return {"name": EVIDENCE_NAME, "met": False,
                "reason": "объявлено для аналитики, но в выгрузке поступления не встретилось: "
                          + ", ".join(rep["missing"]),
                "detail": rep.get("verdict")}
    m = rep.get("evidence") or {}
    where = (f"окно {m['window']}, источник {m.get('source', '—')}"
             if m.get("window") else "выгрузка поступления")
    detail = f"доехали {len(rep['arrived'])} из {rep['arrival_required']} ({where})"
    if rep["undeclared"]:
        detail += f"; вне каталога: {', '.join(rep['undeclared'])}"
    return {"name": EVIDENCE_NAME, "met": True,
            "reason": "все объявленные для аналитики события доезжают", "detail": detail}


def gate_evidence_entry(child_root, base_entry: dict = None) -> dict:
    """Машинное доказательство events_verified_live -> вклад в evidence гейта GATE_ID.

    Форма — по schemas/gate-evidence.schema.json (один гейт). `base_entry` — уже собранное
    evidence этого гейта (напр. вердикт судьи-роли); машина ДОПОЛНЯЕТ его, но для СВОЕГО факта
    `events_verified_live` она авторитетна и снимает слово судьи (иначе «not declared» осталось бы
    незакрытым — судья и дальше фабриковал бы это доказательство своим pass).

    met is True  -> events_verified_live в `provided` (машина подтвердила приход);
    met is False -> status=fail + блокер с недоехавшим событием (находка сильнее декларации);
    met is None  -> events_verified_live снят из `provided` (проверить нечем — не фабрикуем).
    """
    live = events_verified_live(child_root)
    entry = dict(base_entry or {})
    # снять слово судьи о НАШЕМ факте: его подтверждает/опровергает только машина
    provided = [p for p in (entry.get("provided") or []) if p != EVIDENCE_NAME]
    evid = list(entry.get("evidence") or [])
    src = f"{EVIDENCE_NAME} @ intelligence/event_arrival: {live.get('detail') or live['reason']}"
    if live["met"] is True:
        provided.append(EVIDENCE_NAME)
        entry["provided"] = provided
        entry.setdefault("status", "pass")
        entry["evidence"] = evid + [src]
    elif live["met"] is False:
        entry["status"] = "fail"
        entry["provided"] = provided
        entry["blockers"] = list(entry.get("blockers") or []) + [live["reason"]]
        entry["evidence"] = evid + [src]
    else:  # met is None — «unknown != проверено»: не подделываем доказательство словом судьи
        entry["provided"] = provided
        entry["evidence"] = evid + [src]
    return entry


def verify_analytics_runtime(child_root, base_evidence: dict = None, signals: dict = None,
                             tested_revision=None) -> dict:
    """Оценить гейт GATE_ID, подав ему МАШИННОЕ доказательство events_verified_live.

    Это и есть проводка producer -> гейт: результат гейта опирается на вердикт producer'а, а не
    на статичный файл или слово судьи. Импорт `gates.gate_executor` — зависимость ВНИЗ по слоям
    (intelligence -> capabilities), разрешённая packages/layering.yaml.

    -> gate-result (schemas/gate-result.schema.json).
    """
    from ai_ops_kit.gates import gate_executor
    gate = gate_executor.load_gates()[GATE_ID]
    ev = dict(base_evidence or {})
    ev[GATE_ID] = gate_evidence_entry(child_root, ev.get(GATE_ID))
    return gate_executor.evaluate_gate(GATE_ID, gate, ev, tested_revision=tested_revision,
                                       signals=signals)


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
    want_evidence = "--evidence" in argv
    want_gate = "--gate" in argv
    for a in argv[1:]:
        if not a.startswith("-"):
            root = a
            break
    if want_gate:
        res = verify_analytics_runtime(root)
        if js:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"{GATE_ID}: {res['status']}"
                  f"{' (blocking)' if res['blocking'] else ''}")
            for b in res.get("blockers") or []:
                print(f"  ✗ {b}")
            for e in res.get("evidence") or []:
                print(f"  · {e}")
        return 1 if res["status"] == "fail" else 0
    if want_evidence:
        ev = events_verified_live(root)
        if js:
            print(json.dumps(ev, ensure_ascii=False, indent=2))
        else:
            state = {True: "met", False: "НЕ met", None: "unknown"}[ev["met"]]
            print(f"{ev['name']}: {state} — {ev['reason']}")
            if ev.get("detail"):
                print(f"  {ev['detail']}")
        return 1 if ev["met"] is False else 0
    rep = assess(root)
    print(json.dumps(rep, ensure_ascii=False, indent=2) if js else render(rep))
    return 1 if rep.get("missing") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
