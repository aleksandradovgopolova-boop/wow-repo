#!/usr/bin/env python3
"""SR-17..23: governance-отчёт соответствия ПРОДУКТА дочки инженерному стандарту (срез 2).

ПОВОД. `ai-ops-validate.yml` уже стоит в CI дочки, но проверяет только ЗДОРОВЬЕ УСТАНОВКИ кита
(версии согласованы, managed-слой цел, копия не отстала). Соответствие самого ПРОДУКТА стандарту —
артефакты, секции, override — он не смотрит. Этот модуль собирает такой отчёт; он добавляется в ту
же джобу дочки отдельным шагом (SR-17), а не заводит вторую джобу того же смысла.

ЧЕТЫРЕ ЧИСЛА, И СУММА РАВНА ОБЩЕМУ (docs/engineering-standard-requirements.md §3.2). Отчёт называет
`pass` / `violated` / `not_applicable` (с причиной) / `not_checked` (с причиной). Две колонки
«прошло/нарушено» скрыли бы третий ответ — «проверить не удалось»; `not_checked` объявлен отдельно
и в вердикт не сворачивается, как `unknown` у product_audit.

КТО ЗАКРЫЛ СТРОКУ (SR-20). У каждой строки стоит `closed_by` из того же словаря, что у гейтов кита
(`quality/gates.yaml -> closed_by`): `validator | judge | writer | human`. Секционная проверка —
машинный факт (`validator`); активный override ставит человек (`human`).

OVERRIDE (SR-21..23). Отключить требование можно только с причиной (пустая строка не проходит,
SR-21) и со сроком/условием снятия (SR-22): просроченный override становится находкой, а не
тишиной. Активные override видны в каждом отчёте вместе с причиной, и число `not_applicable` равно
числу активных override (SR-23). Override без причины/без срока/просроченный НЕ применяется —
требование остаётся в силе, а сам override всплывает отдельной находкой.

ГРАНИЦА (issue #605, §5). Отчёт СОБИРАЕТСЯ и ПОКАЗЫВАЕТСЯ; блокировать вправе только проверка с
измеренным нулём ложных на живом репозитории, всё остальное advisory. Этот срез — доставка отчёта,
не его блокирующая сила: наполнение и повышение силы идут следующими срезами (§7).

Использование: python3 -m ai_ops_kit.planning.governance_report [<repo_root>] [--json] [--out FILE]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

import yaml

from ai_ops_kit.planning import architecture_invariants, artifact_sections, drift, standard

# ── Четыре состояния строки отчёта (сумма равна общему числу, §3.2). ──
PASS = "pass"
VIOLATED = "violated"
NOT_APPLICABLE = "not_applicable"
NOT_CHECKED = "not_checked"

# ── Кто закрыл строку — тот же словарь, что у гейтов кита (quality/gates.yaml -> closed_by), SR-20.
CLOSED_BY = ("validator", "judge", "writer", "human")

SCHEMA_VERSION = 1
REPORT_VERSION = 1                 # версия ФОРМЫ отчёта (SR-18); растёт при смене структуры
KIND = "governance-report"
CONFIG_REL = ".ai-ops.yaml"


def _read_config(root: Path) -> dict:
    path = Path(root) / CONFIG_REL
    if not path.is_file():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _overrides(cfg: dict) -> list:
    """Список override стандарта из `.ai-ops.yaml -> standard.overrides` (SR §вопрос 3: живёт в дочке)."""
    std = cfg.get("standard") if isinstance(cfg, dict) else None
    ovs = (std or {}).get("overrides") if isinstance(std, dict) else None
    return [o for o in (ovs or []) if isinstance(o, dict)]


def _parse_date(value) -> _dt.date | None:
    """ISO-дата (YYYY-MM-DD) -> date; всё непарсящееся -> None (обрабатывается как отсутствие срока)."""
    if isinstance(value, _dt.date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return _dt.date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def classify_override(ov: dict, today: _dt.date) -> tuple[str, str]:
    """Судьба одного override. -> (kind, human_reason).

    kind: active | no_reason (SR-21) | no_expiry (SR-22) | expired (SR-22). Только `active`
    применяется — остальные не отключают требование и всплывают находкой.
    """
    reason = str(ov.get("reason") or "").strip()
    if not reason:
        return "no_reason", "override без причины не применяется (SR-21)"
    review_by = _parse_date(ov.get("review_by"))
    remove_when = str(ov.get("remove_when") or "").strip()
    if review_by is None and not remove_when:
        return "no_expiry", "у override нет срока (review_by) и условия снятия (remove_when) (SR-22)"
    if review_by is not None and review_by < today:
        return "expired", f"override просрочен: review_by {review_by.isoformat()} уже наступил (SR-22)"
    return "active", reason


def _base_rows(child_root: Path, model: dict | None = None) -> list:
    """Строки из секционной проверки обязательных артефактов (SR-5/6) + not_checked, где секций нет."""
    rows = []
    seen_paths = set()

    secrep = artifact_sections.report(child_root, model=model)
    for art in secrep.get("artifacts") or []:
        seen_paths.add(art["path"])
        for sec in art.get("sections") or []:
            req = f"{art['path']}::{sec['name']}"
            state = sec.get("state")
            if state == artifact_sections.FILLED:
                rows.append({"requirement": req, "status": PASS, "closed_by": "validator",
                             "detail": "секция есть и заполнена"})
            elif state == artifact_sections.EMPTY:
                rows.append({"requirement": req, "status": VIOLATED, "closed_by": "validator",
                             "address": req, "detail": "секция есть, но пуста (заголовок без содержания)"})
            else:  # MISSING
                rows.append({"requirement": req, "status": VIOLATED, "closed_by": "validator",
                             "address": req, "detail": "обязательной секции нет в артефакте"})

    # not_checked: обязательный артефакт БЕЗ объявленных секций — секционную проверку применить не к
    # чему, и это не «pass», а «не проверено» с причиной (SR §3.2, §5.4).
    from ai_ops_kit.planning import contours as _contours
    m = model or _contours.load_model()
    for c in m.get("contours") or []:
        for s in c.get("source_of_truth") or []:
            if not s.get("required"):
                continue
            path = s.get("path")
            if not path or path in seen_paths or s.get("required_sections"):
                continue
            seen_paths.add(path)
            rows.append({"requirement": path, "status": NOT_CHECKED, "closed_by": "validator",
                         "reason": "обязательные секции для артефакта не объявлены — "
                                   "секционная проверка неприменима"})
    return rows


def _apply_overrides(rows: list, overrides: list, today: _dt.date) -> tuple[list, list]:
    """Наложить override на строки. -> (rows, overrides_echo).

    Активный override гасит совпадающие строки требования и ставит РОВНО одну строку
    not_applicable — так `not_applicable` == число активных override (SR-23). Неприменимый
    (без причины/срока/просроченный) требование не отключает и всплывает находкой (SR-21/22).
    """
    echo = []
    active_targets = []
    finding_rows = []

    for ov in overrides:
        req = str(ov.get("requirement") or "").strip() or "<без-требования>"
        kind, detail = classify_override(ov, today)
        entry = {"requirement": req, "active": kind == "active"}
        if ov.get("reason") is not None:
            entry["reason"] = str(ov.get("reason") or "")
        if ov.get("review_by") is not None:
            entry["review_by"] = str(ov.get("review_by"))
        if ov.get("remove_when") is not None:
            entry["remove_when"] = str(ov.get("remove_when"))
        if kind == "active":
            active_targets.append((req, detail))
        else:
            entry["problem"] = detail
            finding_rows.append({"requirement": req, "status": VIOLATED, "closed_by": "validator",
                                 "address": f"{CONFIG_REL} standard.overrides", "detail": detail})
        echo.append(entry)

    # Погасить строки требований, отключённых АКТИВНЫМ override (по точному имени или префиксу path::).
    suppress = {req for req, _ in active_targets}

    def _covered(requirement: str) -> bool:
        return any(requirement == t or requirement.startswith(t + "::") for t in suppress)

    kept = [r for r in rows if not _covered(r["requirement"])]

    # Ровно одна строка not_applicable на каждый активный override (гарантирует равенство SR-23).
    na_rows = [{"requirement": req, "status": NOT_APPLICABLE, "closed_by": "human",
                "reason": reason} for req, reason in active_targets]

    return kept + na_rows + finding_rows, echo


def build(child_root, today: _dt.date | None = None, pkg_root: Path | None = None,
          deps: tuple | None = None, changed_files: list | None = None) -> dict:
    """Governance-отчёт продукта дочки. -> машиночитаемый dict (schemas/governance-report.schema.json).

    `deps` = (before, after) манифестов для SR-13 (новая зависимость без ADR), если контур PR даёт
    базу сравнения; иначе SR-13 объявляется not_checked с причиной, а не молча пропускается.
    `changed_files` = изменённые пути PR для блока расхождений (SR-14..16): без них блок объявляет
    not_checked, потому что расхождение меряется на диффе, а не на дереве.
    """
    root = Path(child_root)
    today = today or _dt.date.today()
    cfg = _read_config(root)

    rows = _base_rows(root)
    rows, overrides_echo = _apply_overrides(rows, _overrides(cfg), today)

    counts = {PASS: 0, VIOLATED: 0, NOT_APPLICABLE: 0, NOT_CHECKED: 0}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    counts["total"] = len(rows)

    std_ver = None
    if isinstance(cfg.get("standard"), dict):
        try:
            std_ver = int(cfg["standard"].get("version"))
        except (TypeError, ValueError):
            std_ver = None
    if std_ver is None:
        std_ver = standard.current_version(pkg_root) if pkg_root else standard.current_version()

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "report_version": REPORT_VERSION,
        "part": "standard_compliance",   # SR-17: вторая часть джобы (первая — здоровье установки)
        "standard_version": std_ver,
        "repository": root.resolve().name,
        "counts": counts,
        "overrides": overrides_echo,
        "rows": rows,
        # Архитектурные инварианты (SR-9..13) — ОТДЕЛЬНЫЙ advisory-блок: он приблизителен (§5.2),
        # его нельзя складывать с четырьмя числами структурного факта (§3.2 остаётся неизменным).
        "architecture": architecture_invariants.report(root, deps=deps),
        # Расхождение (SR-14..16) — ВТОРОЙ РОД находки, отдельный от нарушения: код и описание
        # разошлись, чинить нельзя, только показать. Меряется на диффе (SR-15), тоже advisory.
        "drift": drift.report(root, changed_files=changed_files),
    }


_MARK = {PASS: "🟢", VIOLATED: "🔴", NOT_APPLICABLE: "⚪", NOT_CHECKED: "❔"}


def render(rep: dict) -> str:
    c = rep["counts"]
    lines = [
        f"СООТВЕТСТВИЕ СТАНДАРТУ · {rep['repository']} · стандарт v{rep['standard_version']}",
        f"  pass {c[PASS]} · violated {c[VIOLATED]} · not_applicable {c[NOT_APPLICABLE]} "
        f"· not_checked {c[NOT_CHECKED]}  (всего {c['total']})",
    ]
    for r in rep["rows"]:
        if r["status"] == PASS:
            continue   # печатаем только то, что требует внимания; pass виден в числах
        tail = r.get("reason") or r.get("detail") or ""
        addr = f" [{r['address']}]" if r.get("address") else ""
        lines.append(f"  {_MARK.get(r['status'], '?')} {r['status']:14} {r['requirement']}{addr} — "
                     f"{tail}  · закрыл: {r['closed_by']}")
    if rep["overrides"]:
        lines.append("  Override (виден в каждом отчёте, SR-23):")
        for o in rep["overrides"]:
            state = "активен" if o["active"] else f"НЕ ПРИМЕНЁН — {o.get('problem', '')}"
            when = o.get("review_by") or o.get("remove_when") or "—"
            lines.append(f"    · {o['requirement']}: {o.get('reason', '')} (срок: {when}) — {state}")
    arch = rep.get("architecture")
    if arch:
        ac = arch["counts"]
        lines.append(f"  Архитектурные инварианты (advisory, SR-9..13): "
                     f"pass {ac['pass']} · violated {ac['violated']} · not_checked {ac['not_checked']}")
        for r in arch["rows"]:
            if r["status"] == PASS:
                continue
            tail = r.get("reason") or r.get("detail") or ""
            addr = f" [{r['address']}]" if r.get("address") else ""
            lines.append(f"    {_MARK.get(r['status'], '?')} {r['status']:12} {r['requirement']}"
                         f"{addr} — {tail}")
    dr = rep.get("drift")
    if dr:
        dc = dr["counts"]
        lines.append(f"  Расхождение (advisory, SR-14..16 — НЕ нарушение, показать нельзя починить): "
                     f"drift {dc['drift']} · pass {dc['pass']} · not_checked {dc['not_checked']}")
        for r in dr["rows"]:
            if r["status"] == PASS:
                continue
            tail = r.get("reason") or r.get("detail") or ""
            addr = f" [{r['address']}]" if r.get("address") else ""
            lines.append(f"    ~ {r['status']:12} {r['requirement']}{addr} — {tail}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="governance_report.py",
                                 description="Governance-отчёт соответствия продукта стандарту (SR-17..23)")
    ap.add_argument("repo_root", nargs="?", default=".")
    ap.add_argument("--json", action="store_true", help="печатать отчёт как JSON")
    ap.add_argument("--out", help="записать отчёт JSON в этот файл (для истории дочки, SR-18)")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])
    rep = build(ns.repo_root)
    if ns.out:
        Path(ns.out).write_text(json.dumps(rep, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if ns.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(render(rep))
    return 0   # advisory: доставка отчёта, не блокирующая сила (issue #605 §5)


if __name__ == "__main__":
    raise SystemExit(main())
