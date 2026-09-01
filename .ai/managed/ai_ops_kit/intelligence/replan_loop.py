#!/usr/bin/env python3
"""Autonomous Replanning Loop (капстоун Фазы 5) — замыкание цикла продуктовой ОС.

Цикл Product OS: Observe → Understand → Prioritize → Execute → Evaluate → Learn → **Replanning**.
Семь шагов уже построены и поставляются; не хватало замыкающего — модуля, который берёт сигналы
реальности и САМ сводит приоритеты плана к реальности, БЕЗ человеческого `--replan`. Это он.

ГРАНИЦА АВТОНОМИИ (решение владельца, вариант B, перенос уже одобренной 4-классовой модели ночного
обзора `ep-2026-08-14-nightly-review` на план):

  · ПЕРЕПРИОРИТИЗАЦИЯ — класс A: обратимая, детерминированная, СОСТАВ РАБОТ НЕ МЕНЯЕТ (ни одной не
    добавляет и не удаляет), каждый сдвиг подписан «почему». Её кит применяет САМ — пишет
    переупорядоченный порядок в машинный артефакт дочки (`.ai/project/replan/latest.yaml`), НИКОГДА
    не трогая `planning/plan.yaml`, main, и не коммитя.
  · СТРУКТУРНЫЕ ИЗМЕНЕНИЯ (закрыть работу в историю, пометить начатой, завести новую) — класс B/C:
    выносятся ПРЕДЛОЖЕНИЕМ на одобрение человека и НИКОГДА не применяются автоматически.

Почему именно так: весь кит стоит на «зелёное = проверено» и «предлагать, не сливать». Полевой факт
30.08 (ии-среда): writer РАЗРУШИТЕЛЬНО переписал план (удалил 27 из 57 работ) и получил ложный green.
Автономное УДАЛЕНИЕ работ — ровно тот провал; здесь оно невозможно по механизму (`_check_same_work_set`),
а не по обещанию. Модуль ДЕТЕРМИНИРОВАН и НЕ ЗОВЁТ МОДЕЛЬ (`Budget(max_model_calls=0)`).

ВТОРОЙ ПРАВДЫ НЕ ЗАВОДИМ (dp-001). Порядок берётся из `next_work.compute` — той же ранжировки, что
советует `ai-ops next` (она уже учитывает снятие ожидания, ценность, снижение риска, приоритет цели и
СВЕРЯЕТСЯ С git/гейтами через `resolve`). Расхождение план↔реальность — из `delivery_plan.resolve`
(поле `drift`) и `git_disagreements` (ветка в стволе, а работа открыта; `todo`, а ветка впереди).
Бэклог (GitHub Issues дочки) переприоритизируется через `backlog_prioritize`. Риск-реестр —
контекстом. Ни один сигнал не считается заново: модуль их СВОДИТ.

«НЕ ПРОВЕРЕНО» ≠ «РАСХОЖДЕНИЙ НЕТ». Если сигнал прочитать нельзя (нет плана, ствол не найден, бэклог
недоступен), он попадает в `unverified` с названной причиной, а не сворачивается в «всё согласовано».

Использование:
    replan_loop.py <child_root> [--json] [--no-backlog]         # отчёт (read-only)
    replan_loop.py <child_root> --apply [--dry-run] [--json]    # применить переприоритизацию (класс A)
    replan_loop.py --selftest
Возврат 0 — успех (расхождение и переприоритизация — это данные, а не ошибка запуска).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

KIND = "replan-report"
REPRIORITIZATION_KIND = "replan-reprioritization"

# Куда пишется переприоритизация (класс A). Машинный артефакт РАБОЧЕГО ДЕРЕВА дочки: кит его
# порождает и перезаписывает, человек видит/удаляет; он регенерируем (обратимость), помечен
# `actor: replan-loop`, и это НЕ `planning/plan.yaml` — авторский план кит не переписывает.
REPLAN_DIR_REL = ".ai/project/replan"
LATEST_REL = ".ai/project/replan/latest.yaml"

# Заглушка автономной записи (тот же инвариант, что у ночного автофикса): env или файл-сигнал.
REPLAN_OFF_ENV = "AI_OPS_REPLAN"                 # =off (любой регистр) -> запись не делается
REPLAN_OFF_SENTINEL = ".ai/project/replan/off"

# Классы предложений — те же буквы, что у 4-классовой модели ночного обзора: B — предложение к
# подготовке (кит готовит, мержит человек), C — только рекомендация. Структурная правка плана
# (закрыть/завести/пометить работу) НИКОГДА не класс A.
PROPOSAL_CLASS = {"close-to-history": "B", "mark-in-progress": "B"}


# ─── Observe ───────────────────────────────────────────────────────────────────────────────────

def observe(child_root, *, backlog: bool = True) -> dict:
    """Собрать сигналы расхождения план↔реальность из уже поставляемых источников.

    -> {"plan_present", "actionable": [...], "in_progress": [...], "plan_drift": [...],
        "git": {...}, "backlog": {...}, "risks": {...}, "unverified": [...]}.

    Каждый сигнал — либо данные, либо честный UNKNOWN в `unverified` с причиной.
    """
    child_root = Path(child_root)
    unverified: list[str] = []

    from ai_ops_kit.planning import next_work, delivery_plan

    # Битый план — исключение выше (PlanCorrupt); не глушим, «работы нет» ≠ «файл не разбирается».
    nx = next_work.compute(child_root)
    if not nx.get("plan_present"):
        unverified.append(f"план не заполнен: {nx.get('gap') or 'нет planning/plan.yaml'}")
        return {"plan_present": False, "actionable": [], "in_progress": [], "plan_drift": [],
                "git": {"measured": False}, "backlog": {"ok": False, "reason": "план отсутствует"},
                "risks": {"available": False}, "unverified": unverified,
                "gap": nx.get("gap")}
    if nx.get("plan_is_template"):
        unverified.append("план — ещё заготовка кита (пример), а не работа продукта")
        return {"plan_present": True, "plan_is_template": True, "actionable": [], "in_progress": [],
                "plan_drift": [], "git": {"measured": False},
                "backlog": {"ok": False, "reason": "план — заготовка"},
                "risks": {"available": False}, "unverified": unverified, "gap": nx.get("gap")}

    ready = list(nx.get("ready") or [])            # УЖЕ ранжировано реальностью (по score)
    in_progress = list(nx.get("in_progress") or [])
    # Порядок, ОБЪЯВЛЕННЫЙ человеком в плане (для вычисления сдвига). Только среди тех же id.
    plan = delivery_plan.load(child_root)
    declared_order = [w.get("id") for w in delivery_plan.items(plan) if w.get("id")]

    # Расхождение статуса план↔реальность (declared ≠ выведенное из гейтов/графа) — поле drift.
    plan_drift = [{"id": r["id"], "title": r.get("title"), "drift": r["drift"]}
                  for r in (ready + in_progress) if r.get("drift")]

    # Ветка↔ствол: измеренное противоречие (ветка в стволе, а работа открыта; todo, а ветка впереди).
    git = delivery_plan.git_disagreements(plan, child_root)
    if not git.get("measured"):
        unverified.append("состояние работ в git не измерено (ствол не найден) — не «согласовано»")

    signals = {
        "plan_present": True,
        "declared_order": declared_order,
        "actionable": ready,             # ранжированный реальностью список готовых работ
        "in_progress": in_progress,
        "plan_drift": plan_drift,
        "git": git,
        "risks": _risks(child_root, unverified),
        "backlog": _backlog(child_root, unverified) if backlog else {"ok": False,
                                                                     "reason": "бэклог не запрашивался"},
        "unverified": unverified,
    }
    return signals


def _risks(child_root, unverified: list) -> dict:
    """Риск-реестр как КОНТЕКСТ (drift+health уже сведены в risk_register). Недоступен — назвать."""
    try:
        from ai_ops_kit.intelligence import risk_register
        rep = risk_register.risk_register(Path(child_root))
    except Exception as e:                          # noqa: BLE001 — риски не обязаны ронять отчёт
        unverified.append(f"риск-реестр не построен ({type(e).__name__})")
        return {"available": False}
    return {"available": True, "high": rep.get("count_by_severity", {}).get("high", 0),
            "medium": rep.get("count_by_severity", {}).get("medium", 0),
            "blind_spots": len(rep.get("blind_spots") or [])}


def _backlog(child_root, unverified: list) -> dict:
    """Приоритеты бэклога (GitHub Issues дочки) через backlog_prioritize. Недоступен — честный UNKNOWN."""
    try:
        from ai_ops_kit.planning import backlog_prioritize
        rep = backlog_prioritize.prioritize_backlog(str(child_root))
    except Exception as e:                          # noqa: BLE001 — бэклог не обязан ронять отчёт
        unverified.append(f"бэклог не переприоритизирован ({type(e).__name__})")
        return {"ok": False, "reason": f"{type(e).__name__}"}
    if not rep.ok:
        unverified.append(f"бэклог недоступен: {rep.reason}")
        return {"ok": False, "reason": rep.reason}
    order = [{"number": p.number, "title": p.title, "priority": p.priority,
              "score": p.score, "why": p.explanation} for p in rep.items]
    return {"ok": True, "repo": rep.repo, "count": len(order), "order": order}


# ─── Understand → Replan ─────────────────────────────────────────────────────────────────────────

def _why_row(row: dict) -> list:
    """Почему работа стоит там, где стоит — из уже посчитанных сигналов её строки (не заново)."""
    why = []
    unblocks = row.get("unblocks") or 0
    if unblocks:
        why.append(f"снимает ожидание с {unblocks} работ(ы)")
    for w in (row.get("why") or []):               # ранжировочные причины next_work._rank
        why.append(w)
    if row.get("drift"):
        why.append(f"расхождение с реальностью: {row['drift']}")
    if not why:
        why.append("порядок по расчёту приоритета")
    return why


def understand(observed: dict) -> dict:
    """Детерминированно отобразить сигналы на замысел плана.

    -> {"reprioritization": [...], "proposals": [...]}.

    reprioritization (класс A, применяется САМ) — реальностью-ранжированный список ГОТОВЫХ работ; у
    каждой позиция «было» (порядок в плане) и «стало» (по приоритету) и подпись «почему». Состав —
    ровно те же работы, что и на входе (инвариант ii-среды).
    proposals (класс B/C, только предложение) — структурные правки из измеренного расхождения:
    ветка в стволе -> закрыть в историю; todo, а ветка впереди / drift -> пометить начатой.
    """
    actionable = observed.get("actionable") or []
    declared_order = observed.get("declared_order") or []
    declared_rank = {wid: i for i, wid in enumerate(declared_order)}

    reprioritization = []
    for new_rank, row in enumerate(actionable):
        wid = row.get("id")
        old_rank = declared_rank.get(wid)
        reprioritization.append({
            "id": wid,
            "title": row.get("title"),
            "rank": new_rank + 1,                  # 1-based позиция в новом порядке
            "score": row.get("score"),
            "declared_index": old_rank,            # позиция в объявленном плане (None — не в активном)
            "moved": (old_rank is not None and _relative_moved(wid, actionable, declared_order)),
            "why": _why_row(row),
            "reversible": True,
        })

    proposals = _structural_proposals(observed)
    return {"reprioritization": reprioritization, "proposals": proposals}


def _relative_moved(wid: str, actionable: list, declared_order: list) -> bool:
    """Сдвинулась ли работа ОТНОСИТЕЛЬНО других готовых работ (а не по абсолютному индексу плана).

    Абсолютный индекс включает закрытые/заблокированные и потому шумит; сравниваем порядок готовых
    работ между собой в плане и в новом ранжировании.
    """
    ready_ids = [r.get("id") for r in actionable]
    declared_ready = [w for w in declared_order if w in set(ready_ids)]
    return declared_ready != ready_ids and declared_ready.index(wid) != ready_ids.index(wid) \
        if wid in declared_ready else False


def _structural_proposals(observed: dict) -> list:
    """Структурные предложения из ИЗМЕРЕННОГО расхождения (класс B/C). НИКОГДА не применяются сами."""
    proposals = []
    git = observed.get("git") or {}
    for err in (git.get("errors") or []):
        low = err.lower()
        if "уже в" in low or "in-base" in low or "изменения работы в базе" in low:
            kind = "close-to-history"
        elif "работа начата" in low or "впереди" in low:
            kind = "mark-in-progress"
        else:
            kind = "review-plan-vs-git"
        proposals.append({"kind": kind, "class": PROPOSAL_CLASS.get(kind, "C"),
                          "rationale": err, "signal": "git: ветка↔ствол",
                          "requires_approval": True})
    # Расхождение статуса из resolve (drift) — если git его ещё не назвал.
    for d in (observed.get("plan_drift") or []):
        proposals.append({"kind": "reconcile-status", "class": "B", "target": d["id"],
                          "rationale": f"{d['id']}: {d['drift']}",
                          "signal": "resolve: declared≠реальность", "requires_approval": True})
    return proposals


def replan_report(child_root, *, backlog: bool = True) -> dict:
    """Полный отчёт-перепланирование (read-only). Не пишет ничего."""
    observed = observe(child_root, backlog=backlog)
    if not observed.get("plan_present") or observed.get("plan_is_template"):
        return {"schema_version": 1, "kind": KIND, "root": str(child_root),
                "autonomy": "reprioritize-auto; structure-proposed",
                "plan_present": bool(observed.get("plan_present")),
                "reprioritization": [], "proposals": [], "observed": observed,
                "complete": False, "unverified": observed.get("unverified") or [],
                "gap": observed.get("gap")}
    plan_intent = understand(observed)
    return {
        "schema_version": 1,
        "kind": KIND,
        "root": str(child_root),
        # Граница автономии НАЗВАНА в самом отчёте: переприоритизация — авто, структура — предложением.
        "autonomy": "reprioritize-auto; structure-proposed",
        "plan_present": True,
        "reprioritization": plan_intent["reprioritization"],
        "proposals": plan_intent["proposals"],
        "backlog": observed.get("backlog"),
        "risks": observed.get("risks"),
        "observed": {k: observed[k] for k in ("plan_drift", "git", "unverified") if k in observed},
        "complete": len(observed.get("unverified") or []) == 0,
        "unverified": observed.get("unverified") or [],
    }


# ─── Apply (класс A — обратимая переприоритизация, состав работ НЕ меняет) ────────────────────────

class _NoModelBudget:
    """Заявленный инвариант: переприоритизация детерминирована и НЕ зовёт модель."""
    max_model_calls = 0


def kill_switch_off(root: Path) -> str | None:
    """Заглушена ли автономная запись. -> причина (str) или None (не заглушена)."""
    val = (os.environ.get(REPLAN_OFF_ENV) or "").strip().lower()
    if val == "off":
        return f"{REPLAN_OFF_ENV}=off"
    if (Path(root) / REPLAN_OFF_SENTINEL).exists():
        return f"файл-сигнал {REPLAN_OFF_SENTINEL}"
    return None


def _check_same_work_set(declared_actionable_ids, reprioritized_ids) -> str | None:
    """ИНВАРИАНТ ii-среды: переприоритизация НЕ добавляет и НЕ удаляет работы.

    -> None (состав совпал) или строка-причина (расхождение). Автономное удаление работ — ровно тот
    провал, что дал ложный green в поле; здесь оно невозможно по механизму, а не по обещанию.
    """
    declared, repri = set(declared_actionable_ids), set(reprioritized_ids)
    dropped = declared - repri
    added = repri - declared
    if dropped or added:
        parts = []
        if dropped:
            parts.append(f"пропали работы {sorted(dropped)}")
        if added:
            parts.append(f"появились работы {sorted(added)}")
        return ("переприоритизация изменила СОСТАВ работ (" + "; ".join(parts) + ") — это структурная "
                "правка, а не переупорядочивание; класс A её не делает")
    return None


def apply_reprioritization(child_root, *, dry_run: bool = False, backlog: bool = True,
                           policy=None, now: str | None = None) -> dict:
    """Класс A: записать реальностью-ранжированный порядок в машинный артефакт дочки.

    Возврат: {status, reason, written?, order, off?, budget}. status:
      disabled  — заглушено (env/файл) либо policy=suggest;
      no_change — переприоритизировать нечего (нет готовых работ);
      blocked   — сработал инвариант состава (структурная правка не прошла как класс A);
      dry_run   — порядок посчитан, файл НЕ записан;
      applied   — артефакт `.ai/project/replan/latest.yaml` записан.

    НИКОГДА не трогает planning/plan.yaml, main, не коммитит, не зовёт модель, не меняет состав работ.
    """
    child_root = Path(child_root)
    _ = _NoModelBudget()                    # заявленный инвариант: модель не зовём
    result = {"budget": {"max_model_calls": 0, "spent": 0}, "order": []}

    off = kill_switch_off(child_root)
    if off:
        return {**result, "status": "disabled", "reason": f"автономная запись заглушена ({off})"}

    # Policy-гейт: как у ночного автофикса — suggest значит «только рекомендация, не пишу».
    if _policy_suggest_only(child_root, policy):
        return {**result, "status": "disabled",
                "reason": "policy: replan=suggest — только отчёт, автономную запись не делаю"}

    # Один прогон наблюдения: из него И множество-эталон (готовые работы), И переприоритизация.
    observed = observe(child_root, backlog=backlog)
    if not observed.get("plan_present") or observed.get("plan_is_template"):
        return {**result, "status": "no_change",
                "reason": observed.get("gap") or "план не заполнен",
                "unverified": observed.get("unverified")}
    plan_intent = understand(observed)
    repri = plan_intent.get("reprioritization") or []
    if not repri:
        return {**result, "status": "no_change",
                "reason": "готовых работ для переприоритизации нет (пусто — не «согласовано»)",
                "unverified": observed.get("unverified")}

    # ИНВАРИАНТ СОСТАВА (ii-среда): переприоритизация — ровно те же готовые работы, что наблюдение
    # дало на входе. Эталон — actionable из observe (а НЕ отфильтрованный по выходу список, иначе
    # пропажа работы прошла бы незамеченной). Расхождение -> blocked, файл не пишется.
    actionable_ids = [r.get("id") for r in (observed.get("actionable") or [])]
    order_ids = [r.get("id") for r in repri]
    breach = _check_same_work_set(actionable_ids, order_ids)
    if breach:
        return {**result, "status": "blocked", "reason": breach}

    from ai_ops_kit.planning import delivery_plan
    order = [{"id": r["id"], "rank": r["rank"], "score": r.get("score"), "why": r.get("why")}
             for r in repri]
    result["order"] = order
    if dry_run:
        return {**result, "status": "dry_run",
                "reason": "dry-run: порядок посчитан, артефакт не записан"}

    artifact = {
        "schema_version": 1,
        "kind": REPRIORITIZATION_KIND,
        "actor": "replan-loop",
        "generated_at": now or datetime.now().isoformat(),
        "autonomy": "reprioritize-auto",
        "plan_rel": delivery_plan.plan_rel(child_root),
        "provenance": ("переприоритизация из живых сигналов; СОСТАВ РАБОТ НЕ ИЗМЕНЁН; структурные "
                       "правки — в proposals отчёта, применяются только человеком"),
        "order": order,
    }
    path = _write_artifact(child_root, artifact)
    return {**result, "status": "applied", "reason": "порядок записан в машинный артефакт (не в план)",
            "written": str(path)}


def _write_artifact(child_root: Path, artifact: dict) -> Path:
    import yaml
    path = Path(child_root) / LATEST_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(artifact, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _policy_suggest_only(child_root, policy) -> bool:
    """policy: replan=suggest -> True (не пишем). Нет policy-модуля/уровня — по умолчанию НЕ suggest
    (пишем), потому что граница класса A и так строго обратима; suggest — осознанное ужесточение."""
    try:
        from ai_ops_kit.governance import policy_engine
        pol = policy if policy is not None else policy_engine.load_policy(child_root)
        return policy_engine.level_for("replan", pol) == "suggest"
    except Exception:                               # noqa: BLE001 — нет policy-слоя: класс A и так обратим
        return False


# ─── Человеческий отчёт ──────────────────────────────────────────────────────────────────────────

def format_report(report: dict) -> str:
    L = ["# Перепланирование (автономное)", ""]
    if not report.get("plan_present"):
        L.append(f"План не заполнен: {report.get('gap') or 'нет planning/plan.yaml'}.")
        return "\n".join(L)
    L.append(f"Граница: {report.get('autonomy')} — приоритеты кит меняет сам, состав работ — "
             f"предложением.")
    L.append("")
    repri = report.get("reprioritization") or []
    moved = [r for r in repri if r.get("moved")]
    L.append(f"## Приоритеты (переупорядочено под реальность: {len(moved)} сдвиг(ов) из {len(repri)})")
    for r in repri:
        mark = " ↑↓" if r.get("moved") else ""
        L.append(f"- **{r['rank']}. {r.get('title') or r['id']}**{mark} — {'; '.join(r.get('why') or [])}")
    props = report.get("proposals") or []
    L += ["", f"## Предложения (структурные — только с вашего одобрения): {len(props)}"]
    for p in props:
        L.append(f"- [{p.get('class', 'C')}] {p.get('kind')}: {p.get('rationale')}")
    if not props:
        L.append("- нет: измеренных расхождений состава плана с git не найдено")
    unv = report.get("unverified") or []
    if unv:
        L += ["", "## Не проверено (это не «расхождений нет»)"]
        for u in unv:
            L.append(f"- {u}")
    return "\n".join(L)


def main(argv) -> int:
    args = [a for a in argv if not a.startswith("-")]
    if "--selftest" in argv:
        print(__doc__)
        print("Проверки модуля — в tests/unit/ (AGENTS.md: selftest не живёт в продакшн-модуле).")
        return 0
    if not args:
        print(__doc__)
        return 1
    root = Path(args[0])
    if not root.is_dir():
        print(f"не каталог: {root}", file=sys.stderr)
        return 2
    backlog = "--no-backlog" not in argv
    as_json = "--json" in argv

    if "--apply" in argv:
        res = apply_reprioritization(root, dry_run="--dry-run" in argv, backlog=backlog)
        if as_json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"перепланирование [{res['status']}]: {res.get('reason')}")
            if res.get("written"):
                print(f"артефакт: {res['written']}")
        return 0

    report = replan_report(root, backlog=backlog)
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
