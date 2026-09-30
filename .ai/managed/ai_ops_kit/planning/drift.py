#!/usr/bin/env python3
"""SR-14..16: РАСХОЖДЕНИЕ (drift) архитектуры дочки — отдельный класс находки (срез 4).

ПОВОД (docs/engineering-standard-requirements.md §«Расхождение (drift)», срез 4; требует срезов 0 и
3). Governance-отчёт (срез 2, #605) уже доставлен, архитектурные инварианты (срез 3, #606) уже
показывают НАРУШЕНИЯ. Срез 4 добавляет к отчёту второй, принципиально иной род находки —
РАСХОЖДЕНИЕ, — и делает его измеримым.

SR-14. РАСХОЖДЕНИЕ != НАРУШЕНИЕ, И РАЗЛИЧАЮТСЯ ОНИ СЛОВАМИ, А НЕ SEVERITY. Нарушение
(`architecture_invariants`): код противоречит ОБЪЯВЛЕННОМУ правилу — известно, что не так, и это
можно, в принципе, починить. Расхождение: код и его ОПИСАНИЕ разошлись, и неизвестно, какое из двух
устарело; чинить автоматически нельзя — можно только показать. Поэтому drift — отдельный блок отчёта
со СВОИМ словом состояния `drift`, а не `violated` иной severity: severity скрыла бы, что это другой
РОД находки, а не более/менее тяжёлый случай того же рода.

SR-15. РАСХОЖДЕНИЕ МЕРЯЕТСЯ НА ДИФФЕ, А НЕ НА ДЕРЕВЕ. Существующий механизм
`contours.reconcile -> source_of_truth_behind` уже так и работает: сигнальные пути контура тронуты,
а источник истины — нет. Здесь он переиспользован (не скопирован), и к нему добавлено расхождение
СНИМКА архитектуры. Без изменённых путей блок объявляет `not_checked` («вне контекста PR»), а не
`pass`; и на унаследованном долге, которого PR не касался (архитектурные сигналы не в диффе),
проверка молчит — иначе шум сделал бы отчёт нечитаемым.

SR-16. СНИМОК АРХИТЕКТУРЫ СОХРАНЯЕТСЯ, ЧТОБЫ РАСХОЖДЕНИЕ БЫЛО ИЗМЕРИМО. `architecture_baseline` даёт
дешёвый детерминированный снимок на точном SHA, но никуда его не кладёт — и `diff_baselines`
сравнивать не с чем. Здесь снимок СОХРАНЯЕТСЯ в область дочки (`.ai-ops/architecture-baseline.json`,
где живут и прочие выгрузки продукта), а его обновление — ВИДИМЫЙ ШАГ
(`python -m ai_ops_kit.planning.drift snapshot .`), а не побочный эффект отчёта. Тронуты
архитектурные сигналы, снимок в том же PR не обновлён — снимок отстал от кода ровно так же, как
источник истины контура, и это тоже расхождение, а не нарушение.

ГРАНИЦА (issue #605 §5, #606 §5.2). Блок собирает и ПОКАЗЫВАЕТ расхождения; блокировать он не вправе
(strength advisory). Он тем более advisory, что расхождение по определению не знает, какая из сторон
права: назвать его блокирующим значило бы утверждать, что устарел именно код, чего кит не знает.

Форму блока в отчёте задаёт schemas/governance-report.schema.json (ключ `drift`).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ai_ops_kit.engops import architecture_baseline
from ai_ops_kit.planning import contours

SCHEMA_VERSION = 1
KIND = "drift"
STRENGTH = "advisory"                     # расхождение показывает, но не блокирует (SR-14, §5)
CLASS = "drift"                           # РОД находки: расхождение, не нарушение (SR-14)

# Состояния строки блока расхождений — СВОЙ набор: `drift` назван словом, отдельным от `violated`.
PASS = "pass"
DRIFT = "drift"
NOT_CHECKED = "not_checked"

# Снимок архитектуры лежит в области дочки (SR-16), рядом с прочими выгрузками продукта `.ai-ops/`.
SNAPSHOT_REL = ".ai-ops/architecture-baseline.json"


# ── Снимок архитектуры дочки: СОХРАНЕНИЕ и ЧТЕНИЕ (SR-16). ─────────────────────────────────────
def snapshot_path(child_root) -> Path:
    """Единственный путь снимка в области дочки — чтобы и запись, и сравнение брали его из одного места."""
    return Path(child_root) / SNAPSHOT_REL


def save_snapshot(child_root, sha: str | None = None) -> Path:
    """ВИДИМЫЙ ШАГ (SR-16): сохранить детерминированный снимок архитектуры дочки в её область.

    Обновление снимка — намеренное действие, а не побочный эффект отчёта: снимок фиксирует, какой
    архитектуру объявили последним осознанным решением, и сравнение с ним ловит молчаливый дрейф.
    Возвращает путь записанного файла.
    """
    root = Path(child_root)
    baseline = architecture_baseline.analyze(root, sha=sha)
    path = snapshot_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_snapshot(child_root) -> dict | None:
    """Сохранённый снимок архитектуры дочки, если он есть; иначе None (сравнивать не с чем)."""
    path = snapshot_path(child_root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ── Расхождение источника истины контура (переиспользует source_of_truth_behind, SR-15). ───────
def _contour_drift_rows(child_root: Path, changed_files: list, model: dict | None) -> list:
    """Источник истины контура отстал от кода — расхождение на диффе.

    Переиспользует `contours.reconcile`: находка `source_of_truth_behind` (сигналы контура тронуты,
    описание — нет) — это ровно расхождение. Её severity (`major`) остаётся её внутренним делом; в
    отчёт она выходит с РОДОМ `drift` и словом состояния `drift`, а не как нарушение.
    """
    rec = contours.reconcile(child_root, {}, changed_files, model)
    rows = []
    for f in rec.get("findings") or []:
        if f.get("id") != "source_of_truth_behind":
            continue
        rows.append({
            "requirement": f"drift::contour::{f['contour']}",
            "status": DRIFT, "class": CLASS, "closed_by": "validator", "strength": STRENGTH,
            "address": f["contour"],
            "detail": f["detail"] + " — это РАСХОЖДЕНИЕ, не нарушение: неизвестно, отстало ли "
                      "описание или изменение временно опередило его; починить автоматически "
                      "нельзя, можно только показать"})
    return rows


# ── Расхождение сохранённого снимка архитектуры (SR-16, на диффе — SR-15). ─────────────────────
def _snapshot_drift_rows(child_root: Path, changed_files: list) -> list:
    """Сохранённый снимок разошёлся с кодом, а обновлён в этом PR не был — расхождение.

    Меряется на диффе (SR-15): расхождение снимка объявляется ТОЛЬКО если изменение тронуло
    архитектурные сигналы (`architecture_signals_from_diff`). Изменение, не касавшееся архитектуры,
    молчит, даже если снимок давно устарел от унаследованного долга.
    """
    root = Path(child_root)
    saved = load_snapshot(root)
    if saved is None:
        return [{"requirement": "drift::architecture-snapshot", "status": NOT_CHECKED,
                 "class": CLASS, "closed_by": "validator", "strength": STRENGTH,
                 "reason": f"снимок архитектуры не сохранён ({SNAPSHOT_REL}) — сравнивать не с чем; "
                           "создайте его видимым шагом "
                           "(python -m ai_ops_kit.planning.drift snapshot .)"}]

    changed = [str(f).replace("\\", "/") for f in (changed_files or [])]
    signals = architecture_baseline.architecture_signals_from_diff(changed)
    if not signals:
        return []                       # PR не касался архитектуры — на унаследованный дрейф не краснеем
    if any(f == SNAPSHOT_REL or f.endswith("/" + SNAPSHOT_REL) for f in changed):
        return []                       # снимок обновлён в этом же PR — видимый шаг сделан

    current = architecture_baseline.analyze(root, sha=saved.get("sha"))
    delta = architecture_baseline.diff_baselines(saved, current)
    if not delta:
        return []                       # архитектурные сигналы тронуты, но оси снимка не сдвинулись
    axes = ", ".join(sorted(delta))
    return [{"requirement": "drift::architecture-snapshot", "status": DRIFT, "class": CLASS,
             "closed_by": "validator", "strength": STRENGTH, "address": SNAPSHOT_REL,
             "detail": f"изменение трогает архитектурные сигналы ({', '.join(sorted(signals))}), а "
                       f"сохранённый снимок {SNAPSHOT_REL} не обновлён; разошлись оси: {axes}. "
                       "Расхождение, не нарушение: неизвестно, отстал ли снимок — обновите его "
                       "видимым шагом (python -m ai_ops_kit.planning.drift snapshot .)"}]


# ── Сборка advisory-блока `drift` для governance-отчёта. ──────────────────────────────────────
def report(child_root, changed_files=None, model=None) -> dict:
    """Advisory-блок расхождений дочки. -> dict для ключа `drift` governance-отчёта.

    Расхождение меряется на диффе (SR-15): без `changed_files` блок объявляет `not_checked` с
    причиной, а не молча пропускает и не выдаёт `pass`. Два источника расхождения — источник истины
    контура (переиспользованный `source_of_truth_behind`) и сохранённый снимок архитектуры (SR-16) —
    сводятся в один блок с общим РОДОМ находки `drift`, отдельным от нарушений инвариантов.
    """
    root = Path(child_root)
    rows: list = []
    if not changed_files:
        rows.append({"requirement": "drift::diff", "status": NOT_CHECKED, "class": CLASS,
                     "closed_by": "validator", "strength": STRENGTH,
                     "reason": "вне контекста PR (нет изменённых путей) — расхождение меряется на "
                               "диффе, а не на дереве (SR-15); сверять нечего"})
    else:
        rows.extend(_contour_drift_rows(root, changed_files, model))
        rows.extend(_snapshot_drift_rows(root, changed_files))
        if not rows:
            rows.append({"requirement": "drift::diff", "status": PASS, "class": CLASS,
                         "closed_by": "validator", "strength": STRENGTH,
                         "detail": "изменение не разошлось с описанием контуров и снимком архитектуры"})

    counts = {PASS: 0, DRIFT: 0, NOT_CHECKED: 0}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {
        "kind": KIND, "schema_version": SCHEMA_VERSION, "strength": STRENGTH, "class": CLASS,
        "snapshot": SNAPSHOT_REL, "snapshot_saved": load_snapshot(root) is not None,
        "comparable": bool(changed_files), "counts": counts, "rows": rows,
    }


def _render(rep: dict) -> str:
    c = rep["counts"]
    lines = [f"РАСХОЖДЕНИЕ (drift, advisory, SR-14..16): "
             f"drift {c[DRIFT]} · pass {c[PASS]} · not_checked {c[NOT_CHECKED]}",
             "  (расхождение — не нарушение: код и описание разошлись, чинить нельзя, только показать)"]
    for r in rep["rows"]:
        if r["status"] == PASS:
            continue
        tail = r.get("reason") or r.get("detail") or ""
        addr = f" [{r['address']}]" if r.get("address") else ""
        lines.append(f"  ~ {r['status']:11} {r['requirement']}{addr} — {tail}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="drift.py", description="Расхождение (drift) архитектуры дочки (SR-14..16)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot",
                       help="сохранить снимок архитектуры дочки в её область (видимый шаг, SR-16)")
    s.add_argument("repo", nargs="?", default=".")
    r = sub.add_parser("report", help="показать расхождения на диффе (SR-14/15)")
    r.add_argument("repo", nargs="?", default=".")
    r.add_argument("--files", default="", help="изменённые пути через запятую (git diff --name-only)")
    r.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])

    if ns.cmd == "snapshot":
        path = save_snapshot(ns.repo)
        print(f"снимок архитектуры сохранён: {path}")
        return 0

    files = [f.strip() for f in ns.files.split(",") if f.strip()]
    rep = report(ns.repo, changed_files=files)
    print(json.dumps(rep, ensure_ascii=False, indent=2) if ns.json else _render(rep))
    return 0   # advisory (§5): показ расхождений, не блокирующая сила


if __name__ == "__main__":
    raise SystemExit(main())
