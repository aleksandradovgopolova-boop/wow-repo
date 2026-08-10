#!/usr/bin/env python3
"""Четыре вопроса, на которые репозиторий обязан отвечать в любой момент (v3.35.0).

    1. Где мы сейчас?
    2. Что делаем прямо сейчас?
    3. Что блокирует работу?
    4. Что является лучшей следующей задачей?

Четвёртый — главный и самый лёгкий для подделки. `next work` — НЕ верхняя карточка бэклога.
Кандидат обязан пройти условия допуска (`next_work.admission` модели), и каждое невыполненное
условие даёт ИМЕНОВАННУЮ причину: кандидат не исчезает молча, иначе «нет готовой работы» будет
означать и «всё сделано», и «кит чего-то не увидел».

Ранжирование среди допущенных — детерминированное и ОБЪЯСНИМОЕ. Вес выбора без слов («score
0.73») человеку бесполезен, поэтому вместе со счётом возвращается причина фразой: «ARCH-01,
потому что разблокирует три задачи». Порядок весов объявлен в модели, а не зашит здесь.

Параллельность считается по тому же правилу, что ParallelSafetyDecision внутри задачи:
непересекающиеся области записи и отсутствие зависимости между элементами. Совпадение не
случайно — опасность пересечения области записи не зависит от масштаба.

Роль, а не исполнитель: ответ называет `owner_role`. Какой runtime соответствует роли СЕЙЧАС,
решает роутер в момент запуска (сложность, риск, capability, стоимость, доступность).

Использование:  next_work.py <repo> [--json] [--budget N]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from ai_ops_kit.planning import contours as _contours
from ai_ops_kit.planning import delivery_plan as _plan
from ai_ops_kit.planning import roadmap as _roadmap

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
           Path(__file__).resolve().parents[2])

_VALUE_SCORE = {"high": 2, "medium": 1, "low": 0, None: 1}


def _known_capabilities():
    """Словарь известных китy capability-измерений. Пустой словарь -> проверку не выдумываем."""
    p = PKG / "registry" / "capability-index.yaml"
    if not p.is_file():
        return set()
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return set()
    return set(data.get("capability_dimensions") or [])


def _admission(w, res, caps_known, budget_left):
    """Условия допуска кандидата. -> (allowed: bool, reasons: [{id, ok, detail}]).

    Каждое условие ИМЕНОВАНО. `within_budget` при отсутствии оценки даёт `unknown` и НЕ блокирует:
    неизвестная стоимость — не нулевая и не запретительная (инвариант 3.21 — решение по худшему
    сравнимому прогону, а при отсутствии истории `unavailable`, никогда 0).
    """
    checks = []
    st = res["status"]
    checks.append({"id": "deps_done", "ok": not res["blocked_by"],
                   "detail": ("зависимости закрыты" if not res["blocked_by"]
                              else f"не закрыты: {', '.join(res['blocked_by'])}")})
    hd = w.get("human_decision")
    checks.append({"id": "no_human_decision", "ok": not hd,
                   "detail": "решений человека не ждёт" if not hd else f"ждёт решения: {hd}"})
    need = list(w.get("capabilities") or [])
    unknown_caps = [c for c in need if caps_known and c not in caps_known]
    checks.append({"id": "capabilities_ready", "ok": not unknown_caps,
                   "detail": ("требуемых capability нет в объявленных" if unknown_caps
                              else ("capability не заявлены" if not need else "capability известны")),
                   "state": "unknown" if not caps_known else None})
    est = w.get("estimate_tokens")
    if budget_left is None or est is None:
        checks.append({"id": "within_budget", "ok": True, "state": "unknown",
                       "detail": ("оценка работы не объявлена — стоимость неизвестна, "
                                  "и это НЕ ноль и не запрет") if est is None
                                 else "бюджет не объявлен"})
    else:
        fits = est <= budget_left
        checks.append({"id": "within_budget", "ok": fits,
                       "detail": f"оценка {est} против остатка {budget_left}"})
    conf = res["conflicts_with"]
    checks.append({"id": "no_write_conflict", "ok": not conf,
                   "detail": ("конфликтов записи с активной работой нет" if not conf
                              else f"пересекается с активной работой: {', '.join(conf)}")})
    allowed = st in ("ready",) and all(c["ok"] for c in checks)
    return allowed, checks


def _plural(n):
    """«разблокирует 1 задачу / 2 задачи / 5 задач» — причина выбора читается человеком вслух."""
    if n % 10 == 1 and n % 100 != 11:
        return "задачу"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "задачи"
    return "задач"


def _rank(w, res, model, plan):
    """Счёт кандидата + причина фразой. Веса — из модели, порядок не зашит в код."""
    weights = {r["id"]: r.get("weight", 1)
               for r in ((model.get("next_work") or {}).get("ranking") or []) if r.get("id")}
    prio = _plan.goal_priority(plan)
    single = None
    gs = _plan.goals(plan)
    if len(gs) == 1:
        single = gs[0]["id"]
    gid = w.get("goal") or single
    goal_rank = prio.get(gid, len(prio))               # цель вне плана -> вниз
    wt = (model.get("work_types") or {}).get(w.get("type") or "") or {}

    unblocks = res["unblocks"]
    goal_score = max(0, len(prio) - goal_rank)
    value_score = _VALUE_SCORE.get(w.get("value"), 1)
    risk_score = 1 if (wt.get("unblocks_downstream") and unblocks > 0) else 0

    score = (weights.get("unblocks_downstream", 4) * unblocks
             + weights.get("goal_priority", 3) * goal_score
             + weights.get("product_value", 2) * value_score
             + weights.get("risk_reduction", 1) * risk_score)

    why = []
    if unblocks:
        why.append(f"разблокирует {unblocks} {_plural(unblocks)}")
    if goal_rank == 0 and len(prio) > 1:
        why.append("работает на цель первого приоритета")
    if w.get("value") == "high":
        why.append("объявленная ценность высокая")
    if risk_score:
        why.append(f"тип '{w.get('type')}' снимает риск до траты на реализацию")
    if not why:
        why.append("готова по зависимостям и никому не мешает")
    return score, why


def _parallel_set(candidates, by_id, anchor=None):
    """Что можно вести ОДНОВРЕМЕННО с лучшим кандидатом.

    `anchor` — сам лучший кандидат, и он ОБЯЗАН участвовать в сравнении. Прежде набор сверялся
    только между собой (`chosen` стартовал пустым), поэтому кит утверждал «области записи не
    пересекаются» про работу, чей scope лежит ВНУТРИ scope выбранной следующей: две сессии уходили
    писать один файл — ровно тот вред, ради которого правило непересечения и существует.

    Правило: непересекающиеся области записи И отсутствие зависимости между элементами. Элемент
    без объявленного `write_scope` в набор НЕ попадает: недоказанная безопасность — не
    безопасность, и молчаливое «наверное, не пересекаются» здесь стоило бы двух потерянных сессий.
    """
    def _pref(g):
        return (str(g) or "").split("*")[0].rstrip("/")

    def _overlap(a, b):
        for x in [_pref(s) for s in (a or []) if _pref(s)]:
            for y in [_pref(s) for s in (b or []) if _pref(s)]:
                if x == y or x.startswith(y + "/") or y.startswith(x + "/"):
                    return True
        return False

    def _related(a_id, b_id):
        seen, stack = set(), [a_id]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend((by_id.get(n) or {}).get("depends_on") or [])
        if b_id in seen:
            return True
        seen, stack = set(), [b_id]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend((by_id.get(n) or {}).get("depends_on") or [])
        return a_id in seen

    chosen, skipped = [], []
    if anchor is not None:
        chosen.append(anchor)                      # якорь занимает свою область записи первым
    for c in candidates:
        w = by_id[c["id"]]
        if not (w.get("write_scope") or []):
            skipped.append({"id": c["id"], "reason": "нет write_scope — параллельность недоказуема"})
            continue
        clash = next((s["id"] for s in chosen
                      if _overlap(w.get("write_scope"), by_id[s["id"]].get("write_scope"))
                      or _related(c["id"], s["id"])), None)
        if clash:
            skipped.append({"id": c["id"], "reason": f"пересекается или связана с {clash}"})
            continue
        chosen.append(c)
    if anchor is not None:
        chosen = [c for c in chosen if c["id"] != anchor["id"]]   # «параллельно с собой» не бывает
    return chosen, skipped


def compute(child_root, budget_left=None):
    """Ответ на четыре вопроса. -> dict (машиночитаемо; печать — в `render`).

    Отсутствие плана — законное состояние, а не ошибка: ответ называет пробел и говорит, чем его
    закрыть. Битый план — исключение выше (`PlanCorrupt`): «работы нет» и «файл не разбирается» не
    имеют права выглядеть одинаково.
    """
    child_root = Path(child_root)
    model = _contours.load_model()
    plan = _plan.load(child_root)
    rm = _roadmap.check(child_root, plan)

    if plan is None:
        return {"schema_version": 1, "plan_present": False,
                "roadmap": {"errors": rm["errors"], "warnings": rm["warnings"]},
                "where_are_we": None, "in_progress": [], "blocked": [], "ready": [],
                "next_best": None, "parallel_with": [], "not_ready": [],
                "gap": f"нет {_plan.PLAN_REL} — контур Planning & Execution не заполнен; "
                       f"шаблон: templates/planning/plan.yaml"}

    val = _plan.validate(plan, model)
    res = _plan.resolve(plan, child_root, model)
    ws = _plan.items(plan)
    by_id = {w["id"]: w for w in ws if w.get("id")}
    caps_known = _known_capabilities()

    # 1. Где мы сейчас — по целям, а не по числу закрытых карточек.
    where = []
    single = _plan.goals(plan)[0]["id"] if len(_plan.goals(plan)) == 1 else None
    for g in _plan.goals(plan):
        gw = [w for w in ws if (w.get("goal") or single) == g["id"]]
        done = [w for w in gw if res.get(w["id"], {}).get("status") == "done"]
        outcome = g.get("outcome") or {}
        where.append({"goal": g["id"], "status": g.get("status", "active"),
                      "work_total": len(gw), "work_done": len(done),
                      "outcome": outcome,
                      "outcome_reached": bool(outcome) and all(bool(v) for v in outcome.values()),
                      "in_roadmap": g["id"] in (set(rm["horizons"].get("now", {}).get("goals", []))
                                                | set(rm["horizons"].get("next_outcome", {}).get("goals", [])))})

    in_progress, blocked, ready, not_ready = [], [], [], []
    for wid, w in by_id.items():
        r = res[wid]
        row = {"id": wid, "title": w.get("title"), "type": w.get("type"),
               "owner_role": w.get("owner_role"), "status": r["status"], "source": r["source"],
               "reasons": r["reasons"], "unblocks": r["unblocks"], "drift": r["drift"]}
        if r["status"] == "in_progress":
            in_progress.append(row)
        elif r["status"] in ("blocked", "waiting"):
            row["blocked_by"] = r["blocked_by"]
            row["conflicts_with"] = r["conflicts_with"]
            blocked.append(row)
        elif r["status"] == "ready":
            allowed, checks = _admission(w, r, caps_known, budget_left)
            row["admission"] = checks
            if allowed:
                score, why = _rank(w, r, model, plan)
                row["score"], row["why"] = score, why
                ready.append(row)
            else:
                row["blocked_by_admission"] = [c["id"] for c in checks if not c["ok"]]
                not_ready.append(row)

    ready.sort(key=lambda r: (-r["score"], r["id"]))
    in_progress.sort(key=lambda r: r["id"])
    blocked.sort(key=lambda r: r["id"])

    next_best = ready[0] if ready else None
    parallel, par_skipped = ([], [])
    if len(ready) > 1:
        parallel, par_skipped = _parallel_set(ready[1:], by_id, anchor=next_best)

    return {"schema_version": 1, "plan_present": True,
            "plan_errors": val["errors"], "plan_warnings": val["warnings"],
            "roadmap": {"errors": rm["errors"], "warnings": rm["warnings"]},
            "where_are_we": where, "in_progress": in_progress, "blocked": blocked,
            "ready": ready, "next_best": next_best, "parallel_with": parallel,
            "parallel_skipped": par_skipped, "not_ready": not_ready}


def render(rep) -> str:
    """Человеческий ответ. Четыре вопроса — четыре раздела, в том же порядке, всегда."""
    L = []
    if not rep.get("plan_present"):
        L.append("ПЛАНИРОВАНИЕ: контур не заполнен")
        L.append(f"  {rep.get('gap')}")
        for e in rep["roadmap"]["errors"]:
            L.append(f"  ✗ {e}")
        return "\n".join(L)

    for e in rep["plan_errors"]:
        L.append(f"  ✗ план: {e}")
    for e in rep["roadmap"]["errors"]:
        L.append(f"  ✗ roadmap: {e}")

    L.append("1. ГДЕ МЫ СЕЙЧАС")
    for g in rep["where_are_we"]:
        mark = "✓" if g["outcome_reached"] else "·"
        rm = "" if g["in_roadmap"] else "  ⚠ цели нет в roadmap (работа без направления)"
        L.append(f"  {mark} цель {g['goal']} [{g['status']}] — работа {g['work_done']}/{g['work_total']}{rm}")
        for k, v in (g["outcome"] or {}).items():
            L.append(f"      outcome {k}: {'достигнут' if v else 'не достигнут'}")

    L.append("2. ЧТО ДЕЛАЕМ ПРЯМО СЕЙЧАС")
    if not rep["in_progress"]:
        L.append("  ничего не идёт")
    for r in rep["in_progress"]:
        L.append(f"  · {r['id']} — {r['title']} [роль {r['owner_role']}] ({r['source']})")
        for x in r["reasons"]:
            L.append(f"      {x}")

    L.append("3. ЧТО БЛОКИРУЕТ РАБОТУ")
    if not rep["blocked"]:
        L.append("  ничего не заблокировано")
    for r in rep["blocked"]:
        L.append(f"  · {r['id']} [{r['status']}] — {r['title']}")
        for x in r["reasons"]:
            L.append(f"      {x}")
    for r in rep["not_ready"]:
        L.append(f"  · {r['id']} готова по графу, но не допущена: "
                 f"{', '.join(r['blocked_by_admission'])}")
        for c in r["admission"]:
            if not c["ok"]:
                L.append(f"      {c['id']}: {c['detail']}")

    L.append("4. ЧТО ВЗЯТЬ СЛЕДУЮЩИМ")
    nb = rep["next_best"]
    if not nb:
        L.append("  готовой работы нет — см. раздел 3 (это НЕ значит «всё сделано»)")
    else:
        L.append(f"  → {nb['owner_role']}: {nb['id']} — {nb['title']}")
        L.append(f"      потому что {'; '.join(nb['why'])}")
        for r in rep["parallel_with"]:
            L.append(f"  → {r['owner_role']}: {r['id']} — {r['title']} "
                     f"(можно одновременно: области записи не пересекаются)")
        for s in rep["parallel_skipped"]:
            L.append(f"      {s['id']} одновременно НЕ брать: {s['reason']}")
    for w in rep["plan_warnings"] + rep["roadmap"]["warnings"]:
        L.append(f"  ⚠ {w}")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="next_work.py")
    ap.add_argument("repo")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--budget", type=int, default=None,
                    help="остаток бюджета в токенах (нет значения -> unknown, НЕ ноль)")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])
    try:
        rep = compute(Path(ns.repo), budget_left=ns.budget)
    except (_plan.PlanCorrupt, _contours.ModelCorrupt) as e:
        print(f"ОШИБКА: {e}")
        return 1
    if ns.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(render(rep))
    if not rep.get("plan_present") or rep.get("plan_errors") or rep["roadmap"]["errors"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
