#!/usr/bin/env python3
"""Настоящий read-only review действующей ветки (v2.116, `ai-ops review`).

Аудит: `review` не был настоящим intent — падал в preview; движок реально запускался только для
`run --execute`. Здесь — независимый ревью УЖЕ существующей ветки ai-ops/<wid>: без tool loop, без
правок и коммитов. Ревьюер гоняется под READ-ONLY политикой над worktree ветки и выносит вердикты по
ai-review гейтам плана (writer ≠ judge). Диф ветки против базы — контекст ревью.

Использование (программно): review(child_root, wid, reviewer_proposer, base="main") -> отчёт.
CLI: review_branch.py <child_root> <wid> [--base main] [--json]  (реальный ревьюер — через ai-ops).
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / "tools"))

import execution_pipeline as _ep   # noqa: E402
import worktree as _wt             # noqa: E402


def _git(root, *a):
    import gitio
    return gitio.git(root, *a)   # v3.0.13 (блок C): единый git-хелпер с таймаутом


def _load_plan(child_root, wid):
    import yaml
    p = Path(child_root) / "features" / str(wid) / "run-plan.yaml"
    if p.is_file():
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {}


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
              "readiness": rep.get("readiness"),
              "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
    path = fdir / "branch-review.yaml"
    path.write_text(yaml.safe_dump(record, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return str(path.relative_to(Path(child_root))) if path.is_relative_to(Path(child_root)) else str(path)


def review(child_root, wid, reviewer_proposer, base="main", budget=None, persist=True):
    """Read-only ревью ветки ai-ops/<wid>. -> {kind, workitem_id, revision, reviewable, reviews[],
    verdict, readiness, evidence_path?, changed_files, note?}. НЕ создаёт правок/коммитов (reviewer
    под read-only политикой), но ФИКСИРУЕТ вердикт как артефакт (persist=True) — это lifecycle-событие."""
    child_root = Path(child_root)
    branch = f"ai-ops/{wid}"
    wp = child_root / ".ai" / "worktrees" / wid

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
    # изменённые файлы ветки против базы (для контекста ревью; base может не резолвиться — не падаем)
    changed = []
    rc_b, _, _ = _git(child_root, "rev-parse", "--verify", base)
    if rc_b == 0:
        rc_d, out, _ = _git(wp, "diff", "--name-only", f"{base}...{branch}")
        if rc_d == 0:
            changed = [ln for ln in out.splitlines() if ln.strip()]

    plan = _load_plan(child_root, wid)
    gate_ids = plan.get("gates") or ["code_review"]
    signals = {"task_type": plan.get("base_workflow", "QUICK")}
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

    rep = {"kind": "BranchReview", "workitem_id": wid, "branch": branch, "revision": revision,
           "reattached_worktree": reattached, "reviewable": reviewable, "reviews": reviews,
           "verdict": verdict, "readiness": _readiness_for(verdict), "changed_files": changed}
    if persist:
        rep["evidence_path"] = _persist_review(child_root, wid, rep)
    return rep


def selftest():
    import tempfile
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    import ai_ops_run
    import io
    import contextlib

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        for a in (("init", "-q"), ("config", "user.email", "t@t"), ("config", "user.name", "t"),
                  ("add", "-A"), ("commit", "-q", "-m", "i")):
            _git(root, *a)
        cur = _git(root, "rev-parse", "--abbrev-ref", "HEAD")[1]

        # сначала — реальный прогон, создающий ветку ai-ops/rv + план (ENGINEERING -> code_review reviewable)
        it = iter([{"op": "write", "path": "src/rv.py", "content": "x=1\n"}, {"done": True}])
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            # v2.121: heavy требует спеку ДО tool loop -> запускаем с author=True (движок авторизует спеку)
            ai_ops_run.run("рефактор", {"task_type": "ENGINEERING", "size": "small", "risk": "low",
                                        "affected_areas": ["core"], "decomposition_confirmed": True},
                           root, engine="pipeline", proposer=lambda c: next(it), execute=True,
                           feature="rv", install_deps=False, author=True)

        # нет ветки -> честный no-branch
        r_nb = review(root, "never", reviewer_proposer=lambda p: '{"kind":"reviewer-result","status":"pass"}', base=cur)
        expect("review: нет ветки -> verdict=no-branch (нечего ревьюить)", r_nb["verdict"] == "no-branch")

        # существующая ветка + reviewer pass -> verdict pass, БЕЗ нового коммита (read-only)
        _, sha_before, _ = _git(root / ".ai" / "worktrees" / "rv", "rev-parse", "HEAD")
        # v3.0.11: ревьюер читает изменённый файл ПЕРЕД pass (иначе блокирующий code_review не закрывается
        # по 0-read рубер-стампу).
        def passrev(p):
            if "--- src/rv.py ---" in p:
                return '{"kind":"reviewer-result","status":"pass","checks":[{"id":"ok","status":"pass"}]}'
            return '{"op":"read","path":"src/rv.py"}'
        r_ok = review(root, "rv", reviewer_proposer=passrev, base=cur)
        _, sha_after, _ = _git(root / ".ai" / "worktrees" / "rv", "rev-parse", "HEAD")
        expect("review: reviewer pass -> verdict=pass, есть вердикт code_review",
               r_ok["verdict"] == "pass" and any(rv["gate"] == "code_review" and rv["status"] == "pass"
                                                 for rv in r_ok["reviews"]))
        expect("review: read-only — ветка НЕ получила новый коммит", sha_before == sha_after)

        # reviewer fail -> verdict needs-changes (не pass)
        failrev = lambda p: '{"kind":"reviewer-result","status":"fail","checks":[{"id":"x","status":"fail"}],"blockers":["плохо"]}'
        r_bad = review(root, "rv", reviewer_proposer=failrev, base=cur)
        expect("review: reviewer fail -> verdict=needs-changes", r_bad["verdict"] == "needs-changes")

        # v2.121 (P1.3): вердикт пере-считывает готовность и фиксируется артефактом (lifecycle, не диагностика)
        expect("v2.121 review: pass -> readiness.ready_for_merge=True",
               (r_ok.get("readiness") or {}).get("ready_for_merge") is True)
        expect("v2.121 review: needs-changes -> ready_for_merge=False",
               (r_bad.get("readiness") or {}).get("ready_for_merge") is False)
        ev = root / "features" / "rv" / "branch-review.yaml"
        expect("v2.121 review: вердикт зафиксирован артефактом features/rv/branch-review.yaml", ev.is_file())
        if ev.is_file():
            import yaml as _y
            rec = _y.safe_load(ev.read_text(encoding="utf-8"))
            # последний прогон был needs-changes -> артефакт отражает актуальный вердикт + метку времени
            expect("v2.121 review: артефакт хранит вердикт + created_at",
                   rec.get("verdict") == "needs-changes" and bool(rec.get("created_at")))

        # v2.121 (P1.3): needs-reviewer (нет живого ревьюера) -> готовность НЕ подтверждена
        r_nr = review(root, "rv", reviewer_proposer=None, base=cur)
        expect("v2.121 review: needs-reviewer -> ready_for_merge=False (вердикт не вынесен)",
               r_nr["verdict"] == "needs-reviewer"
               and (r_nr.get("readiness") or {}).get("ready_for_merge") is False)

    print("review_branch selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    ap = argparse.ArgumentParser(prog="review_branch.py")
    ap.add_argument("child_root"); ap.add_argument("wid")
    ap.add_argument("--base", default="main"); ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    # без живого провайдера здесь ревьюер не подставляется (CLI-обёртка ai-ops даёт провайдер);
    # печатаем, что ревьюируемо и какова ветка (verdict=needs-reviewer).
    rep = review(Path(a.child_root), a.wid, reviewer_proposer=None, base=a.base)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(f"BRANCH-REVIEW {a.wid}: verdict={rep['verdict']} · ревьюируемо={rep.get('reviewable')} "
              f"· ready_for_merge={(rep.get('readiness') or {}).get('ready_for_merge')}")
        if rep.get("note"):
            print(f"  · {rep['note']}")
    # v2.121 (P1.3): needs-reviewer -> НЕ ok. Вердикт не вынесен = готовность не подтверждена.
    return 0 if (rep.get("readiness") or {}).get("ready_for_merge") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
