#!/usr/bin/env python3
"""Настоящий read-only review действующей ветки (v2.116, `ai-ops review`).

Аудит: `review` не был настоящим intent — падал в preview; движок реально запускался только для
`run --execute`. Здесь — независимый ревью УЖЕ существующей ветки ai-ops/<wid>: без tool loop, без
правок и коммитов. Ревьюер гоняется под READ-ONLY политикой над worktree ветки и выносит вердикты по
ai-review гейтам плана (writer ≠ judge). Диф ветки против базы — контекст ревью.

Использование (программно): review(child_root, wid, reviewer_proposer, base=None) -> отчёт.
База: не задана -> АВТОПОДБОР (`pipeline_git._resolve_base`: текущая ветка -> upstream ->
remote default), как и обещает справка CLI; не подобралась -> причина названа в `base_note`,
и ревью продолжается без дифа (это контекст, а не условие вердикта). Хардкода 'main' нет.
CLI-обёртка (реальный ревьюер — через ai-ops): ai_ops_kit/devtools/branch_review_cli.py.

Живёт в `engine` (а не в `delivery`): review() ЗАПУСКАЕТ движковые ревью-гейты (_run_reviews,
_reviewable_gates), подбирает базу (pipeline_git._resolve_base) и worktree — это исполнение, а не
доставка. Раньше файл лежал в delivery и тянул эти движковые функции ВВЕРХ через __import__, пряча
взаимную пару delivery<->engine (K3). Переезд в engine делает импорты обычными внутрипакетными, а
delivery перестаёт зависеть от engine. Сам акт доставки (draft PR) остаётся в delivery/pr_open.
"""
from __future__ import annotations

from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402,F401


def _git(root, *a):
    from ai_ops_kit.shared import gitio
    return gitio.git(root, *a)   # v3.0.13 (блок C): единый git-хелпер с таймаутом


def _load_plan(child_root, wid):
    import yaml
    p = Path(child_root) / "features" / str(wid) / "run-plan.yaml"
    if p.is_file():
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {}


def _base_for_review(child_root, base, branch):
    """База сравнения для дифа ветки. -> {base, source, resolved, reason?}.

    ПОЛЕ 17-18.08.2026 (заявка #136, ИИ-Среда): `review` без `--base` РОНЯЛ
    `TypeError: expected str … not NoneType` — `--base` в CLI имеет default `None`, и этот `None`
    перекрывал дефолт функции, уходя аргументом в `git rev-parse --verify`. Контроль тем же
    замером: с базой — вердикт `needs-reviewer`, диф считается. ПРИ ЭТОМ СПРАВКА CLI ОБЕЩАЛА
    автоподбор («по умолчанию auto: upstream/remote-default/текущая»), которого на этом пути не
    существовало: обещание печаталось человеку и не исполнялось.

    Автоподбор УЖЕ НАПИСАН — `pipeline_git._resolve_base` (текущая ветка -> upstream ->
    remote default), тот же, что у `run`/`resume`. Здесь он ровно применён, поэтому справка
    становится правдой, а не вторым источником истины.
    ЧЕГО НЕ ДЕЛАЕМ: не подставляем `main` молча — в чужом репозитории её может не быть (ветка по
    умолчанию бывает `master`/`trunk`), и такой дефолт как раз и прятал отсутствие базы."""
    if base:
        rc, _, _ = _git(child_root, "rev-parse", "--verify", base)
        if rc == 0:
            return {"base": base, "source": "explicit", "resolved": True}
        return {"base": base, "source": "explicit", "resolved": False,
                "reason": f"явная база '{base}' не найдена в репозитории — диф не считан"}
    from ai_ops_kit.engine import pipeline_git as _pg   # внутрипакетно (review_branch теперь в engine)
    r = _pg._resolve_base(child_root, None)
    if r.get("resolved"):
        if r.get("base_ref") == branch:
            # авто дало саму ревьюируемую ветку (человек стоит на ней) — диф против себя пуст.
            # Молча отдать пустой список значило бы «изменений нет» вместо «база не выбрана».
            return {"base": None, "source": r.get("source"), "resolved": False,
                    "reason": f"авто-база совпала с ревьюируемой веткой {branch} — "
                              f"диф против себя пуст; задай --base <ветка>"}
        return {"base": r["base_ref"], "source": r.get("source"), "resolved": True}
    return {"base": None, "source": "auto", "resolved": False,
            "reason": r.get("reason") or "база не определена автоматически"}


# v2.121 (P1.3): review — не диагностика, а событие жизненного цикла. Вердикт ветки пере-считывает
# готовность к merge и ФИКСИРУЕТСЯ артефактом (features/<wid>/branch-review.yaml), иначе слово
# ревьюера ни на что не влияет. Готовность честная: ready_for_merge=True только когда вердикт вынесен
# и он pass (или ai-review гейтов нет). needs-reviewer/needs-changes/no-branch -> НЕ готово.
_READY_VERDICTS = ("pass", "no-ai-review-gates")


def _readiness_for(verdict):
    return {"ready_for_merge": verdict in _READY_VERDICTS,
            "reason": {"pass": "все ревьюируемые ai-review гейты получили pass",
                       "no-ai-review-gates": "у плана нет ai-review гейтов — merge не гейтится ревью",
                       "needs-reviewer": "вердикт не вынесен (нет живого ревьюера) — ready нельзя",
                       "needs-changes": "ревьюер вернул fail хотя бы по одному гейту",
                       "no-branch": "ветки нет — ревьюить нечего",
                       "error": "ревью не удалось выполнить"}.get(verdict, "неизвестный вердикт")}


def _persist_review(child_root, wid, rep):
    """Зафиксировать вердикт ревью как артефакт жизненного цикла (features/<wid>/branch-review.yaml).
    created_at обязателен — без метки времени это не запись, а заметка."""
    import yaml
    from datetime import datetime, timezone
    fdir = Path(child_root) / "features" / str(wid)
    try:
        fdir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    record = {"schema_version": 1, "kind": "BranchReview", "workitem_id": str(wid),
              "branch": rep.get("branch"), "revision": rep.get("revision"),
              "verdict": rep["verdict"], "reviewable": rep.get("reviewable"),
              "review_statuses": {r["gate"]: (r.get("status") if r.get("valid") else "invalid")
                                  for r in rep.get("reviews") or []},
              "changed_files": rep.get("changed_files") or [],
              # артефакт обязан нести базу: список изменённых файлов без неё непроверяем
              "base": rep.get("base"), "base_source": rep.get("base_source"),
              "base_note": rep.get("base_note"),
              "readiness": rep.get("readiness"),
              "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
    path = fdir / "branch-review.yaml"
    path.write_text(yaml.safe_dump(record, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return str(path.relative_to(Path(child_root))) if path.is_relative_to(Path(child_root)) else str(path)


def review(child_root, wid, reviewer_proposer, base=None, budget=None, persist=True):
    """Read-only ревью ветки ai-ops/<wid>. -> {kind, workitem_id, revision, reviewable, reviews[],
    verdict, readiness, evidence_path?, changed_files, note?}. НЕ создаёт правок/коммитов (reviewer
    под read-only политикой), но ФИКСИРУЕТ вердикт как артефакт (persist=True) — это lifecycle-событие."""
    child_root = Path(child_root)
    branch = f"ai-ops/{wid}"
    wp = child_root / ".ai" / "worktrees" / wid

    from ai_ops_kit.engine import worktree as _wt   # внутрипакетно
    if not _wt._branch_exists(child_root, branch):
        return {"kind": "BranchReview", "workitem_id": wid, "reviewable": False,
                "reviews": [], "verdict": "no-branch", "readiness": _readiness_for("no-branch"),
                "note": f"ветка {branch} не найдена — нечего ревьюить (сначала ai-ops run --execute)"}
    # worktree утерян, но ветка есть -> пере-подключаем (read-only ревью на существующих коммитах)
    reattached = False
    if not wp.is_dir():
        if _wt.add(child_root, wid, branch) != 0:
            return {"kind": "BranchReview", "workitem_id": wid, "reviewable": False, "reviews": [],
                    "verdict": "error", "readiness": _readiness_for("error"),
                    "note": f"не удалось пере-подключить worktree к {branch}"}
        reattached = True

    rc, revision, _ = _git(wp, "rev-parse", "HEAD")
    revision = revision if rc == 0 else None
    # изменённые файлы ветки против базы (контекст ревью). База подбирается или НАЗЫВАЕТСЯ причиной —
    # но не падает и не подставляется молча: `_base_for_review`.
    changed = []
    based = _base_for_review(child_root, base, branch)
    if based["resolved"]:
        rc_d, out, _ = _git(wp, "diff", "--name-only", f"{based['base']}...{branch}")
        if rc_d == 0:
            changed = [ln for ln in out.splitlines() if ln.strip()]
        else:
            based = dict(based, resolved=False,
                         reason=f"диф {based['base']}...{branch} не посчитан (git вернул ошибку)")

    plan = _load_plan(child_root, wid)
    gate_ids = plan.get("gates") or ["code_review"]
    signals = {"task_type": plan.get("base_workflow", "QUICK")}
    from ai_ops_kit.engine import execution_pipeline as _ep   # внутрипакетно
    reviewable = _ep._reviewable_gates(gate_ids, signals)

    reviews = []
    if reviewable and reviewer_proposer is not None:
        _, reviews = _ep._run_reviews(reviewer_proposer, wp, gate_ids, {}, signals, revision, budget)

    # вердикт ветки: pass только если все ревьюируемые гейты получили pass; иначе needs-changes/blocked
    statuses = {r["gate"]: (r.get("status") if r.get("valid") else "invalid") for r in reviews}
    if not reviewable:
        verdict = "no-ai-review-gates"
    elif reviewer_proposer is None:
        verdict = "needs-reviewer"
    elif all(statuses.get(g) == "pass" for g in reviewable):
        verdict = "pass"
    else:
        verdict = "needs-changes"

    # Соответствие Архитектурной конституции по ИЗМЕНЁННЫМ файлам (#847): рекомендации владельцу,
    # НЕ блок (советует, не гейтит — writer≠judge вердикт даёт ревьюер выше). Пофайловые статьи по
    # diff; кросс-файловые дубли — в онбординг-скане. Сбой советчика ревью не роняет.
    constitution_findings = []
    try:
        from ai_ops_kit.checks import constitution_conformance as _cc   # внутрипакетно
        constitution_findings = _cc.conform_paths(wp, changed)
    except (ImportError, OSError, ValueError):
        constitution_findings = []

    rep = {"kind": "BranchReview", "workitem_id": wid, "branch": branch, "revision": revision,
           "reattached_worktree": reattached, "reviewable": reviewable, "reviews": reviews,
           "verdict": verdict, "readiness": _readiness_for(verdict), "changed_files": changed,
           "constitution_findings": constitution_findings,
           # база рядом с дифом: без неё «изменённых файлов ноль» неотличимо от «база не выбрана»
           "base": based.get("base"), "base_source": based.get("source")}
    if not based["resolved"]:
        rep["base_note"] = based.get("reason")
    if persist:
        rep["evidence_path"] = _persist_review(child_root, wid, rep)
        rep["review_verdict_path"] = _persist_review_verdict(child_root, wid, reviews, signals,
                                                             revision)
    return rep


def _persist_review_verdict(child_root, wid, reviews, signals, revision):
    """Зафиксировать persistent review-вердикт функции — «кто/что проверил» как узел истории.

    ПОЧЕМУ ОТДЕЛЬНО от BranchReview: BranchReview — про готовность ВЕТКИ к merge; review-вердикт — про
    ЗНАНИЕ о функции (какие измерения приняты и КЕМ), которое читает Knowledge Graph. Содержание берём
    у `evidence_verdict` (веха 4.2, #588) — новую модель проверки не вводим: гейты-ревью здесь — это
    независимый судья (`_run_reviews` под read-only reviewer_proposer), поэтому writer≠judge держится.
    Пишется под `wid` — при ревью паспорта функции (`ai-ops review <feature>`) это её id, и Knowledge
    Graph резолвит узел review на узел feature. Ничего не проверено -> запись не пишется (persist -> None).
    Сбой не роняет ревью: вердикт от наличия файла не зависит."""
    from ai_ops_kit.gates import gate_executor
    from ai_ops_kit.shared import review_verdict
    try:
        gates_map = gate_executor.load_gates()
        gate_results = [{"gate": r.get("gate"), "status": r.get("status")}
                        for r in reviews or [] if r.get("valid")]
        subset = {r["gate"]: gates_map[r["gate"]] for r in gate_results
                  if r.get("gate") in gates_map}
        ev = gate_executor.evidence_verdict(gate_results, subset, signals)
        return review_verdict.persist(child_root, wid, ev, revision=revision)
    except (OSError, KeyError, TypeError):
        return None
