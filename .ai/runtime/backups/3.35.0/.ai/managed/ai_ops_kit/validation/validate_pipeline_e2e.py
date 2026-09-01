#!/usr/bin/env python3
"""Канонический e2e движка на РЕАЛЬНОЙ фикстуре (v2.96, аудит 2.96 — Real Qualification).

CI кита раньше гонял в основном selftest-модули и статические валидаторы. Этот харнесс закрывает
разрыв ДЕТЕРМИНИРОВАННО, без живой модели: поднимает настоящий git-репозиторий из python-фикстуры,
прогоняет ПОЛНЫЙ путь контроллера `ai-ops run --engine pipeline --execute` со scripted-proposer
(вместо LLM) и проверяет всю цепочку в одной транзакции (v2.94):

  task -> RunPlan -> WorkItem -> active-work -> pipeline (detect -> tool-loop -> commit на ветке ->
  evidence на точном SHA -> гейты) -> run-report -> active-work закрыта.

Это НЕ замена живой матрицы с моделью (Node/Python/Go × macOS/Linux — см. qualification-runbook.md,
нужна машина пользователя): scripted-proposer проверяет МЕХАНИКУ и сходимость, а качество правок —
модель. Но канонический путь теперь прогоняется на настоящем репо в CI, а не только в юнит-selftest.

Использование:
  validate_pipeline_e2e.py            # прогнать e2e
  validate_pipeline_e2e.py --selftest # то же (для чек-листа CI)
Возврат 0 — ок, 1 — цепочка не сошлась.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / "tools"))

import ai_ops_run  # noqa: E402

FIX = PKG / "qualification" / "fixtures" / "python"


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def run_e2e():
    """-> (results[(name, ok)], report|None)."""
    r = []

    def ok(name, cond):
        r.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # реальный репозиторий из python-фикстуры (calc + падающий baseline-тест)
        for f in ("calc.py", "pyproject.toml"):
            shutil.copy(FIX / f, root / f)
        _git(root, "init", "-q"); _git(root, "config", "user.email", "t@t"); _git(root, "config", "user.name", "t")
        _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "init")

        # scripted-proposer вместо LLM: добавляет чистую функцию + тест (детерминированно)
        script = iter([
            {"op": "write", "path": "mathx.py", "content": "def clamp(x, lo, hi):\n    return max(lo, min(hi, x))\n"},
            {"op": "write", "path": "test_mathx.py",
             "content": "from mathx import clamp\n\ndef test_clamp():\n    assert clamp(5, 0, 3) == 3\n"},
            {"done": True, "summary": "clamp + тест"},
        ])
        signals = {"task_type": "QUICK", "size": "small", "risk": "low", "affected_areas": ["core"]}
        # install_deps=False: e2e детерминирован и offline (без pip); проверяем МЕХАНИКУ цепочки
        rep = ai_ops_run.run("добавить clamp с тестом", signals, root, engine="pipeline",
                             proposer=lambda c: next(script), execute=True, feature="clamp-fn",
                             install_deps=False)

        fid = rep.get("workitem_id")
        commit = rep.get("commit") or {}
        ok("e2e: отчёт движка (kind=execution-pipeline)", rep.get("kind") == "execution-pipeline")
        ok("e2e: tool-loop дошёл до done", (rep.get("loop") or {}).get("stopped") == "done")
        ok("e2e: изменения применены (2 файла)", (rep.get("loop") or {}).get("applied_writes") == 2)
        ok("e2e: коммит на ветке ai-ops/clamp-fn",
           bool(commit.get("sha")) and commit.get("branch") == "ai-ops/clamp-fn")
        ok("e2e: evidence на ТОЧНОМ зафиксированном SHA", commit.get("evidence_on_exact_sha") is True)
        ok("e2e: гейты RunPlan оценены", isinstance((rep.get("gates") or {}).get("evaluated"), list))
        # v2.94 lifecycle: WorkItem/RunPlan/active-work/run-report на диске
        ok("e2e: WorkItem записан", (root / "features" / fid / "workitem.yaml").exists())
        ok("e2e: RunPlan записан", (root / "features" / fid / "run-plan.yaml").exists())
        ok("e2e: run-report записан", (root / "features" / fid / "run-report.json").exists())
        ok("e2e: ContextBundle записан (v2.97)", (root / "features" / fid / "context-bundle.yaml").exists())
        ok("e2e: context измерен ДО модели (estimated_tokens>0)",
           isinstance(rep.get("context_bundle"), dict) and rep["context_bundle"]["estimated_tokens"] > 0)
        ok("e2e: ContextPayload записан + подан модели (v2.108)",
           (root / "features" / fid / "context-payload.yaml").exists()
           and isinstance(rep.get("context_payload"), dict) and rep["context_payload"]["fed_to_model"] is True)
        ok("e2e: SpecCoverage записан (v2.98)", (root / "features" / fid / "spec-coverage.yaml").exists())
        ok("e2e: RunHandoff записан + next_action (v2.99)",
           (root / "features" / fid / "run-handoff.yaml").exists()
           and isinstance(rep.get("handoff"), dict) and bool(rep["handoff"].get("next_action")))
        ok("e2e: WorkPackagePlan записан (v2.100)", (root / "features" / fid / "work-package.yaml").exists())
        ok("e2e: lifecycle-трейс в отчёте", isinstance(rep.get("lifecycle"), dict))
        # active-work закрыта (done)
        awp = root / ".ai" / "runtime" / "active-work.yaml"
        closed = False
        if awp.exists():
            import yaml
            data = yaml.safe_load(awp.read_text(encoding="utf-8")) or {}
            closed = any(w.get("id") == fid and w.get("status") == "done" for w in data.get("active", []))
        ok("e2e: active-work закрыта (done) по завершении", closed)
        # изоляция worktree: КОД-правка модели ушла в worktree, а не в основной checkout
        ok("e2e: код-правка (mathx.py) НЕ в основном checkout (изоляция worktree)",
           not (root / "mathx.py").exists())
        ok("e2e: код-правка есть в worktree ai-ops/clamp-fn",
           (root / ".ai" / "worktrees" / "clamp-fn" / "mathx.py").exists())
        return r, rep

    return r, None


def selftest():
    return main([])


def main(argv):
    if not FIX.is_dir():
        print(f"PIPELINE-E2E: нет python-фикстуры: {FIX}")
        return 1
    results, rep = run_e2e()
    ok = True
    for name, passed in results:
        ok = ok and passed
        print(f"{'PASS' if passed else 'FAIL'} {name}")
    if "--json" in argv and rep is not None:
        print(json.dumps({"workitem_id": rep.get("workitem_id"),
                          "ready_for_pr": rep.get("ready_for_pr"),
                          "gates_blocked": (rep.get("gates") or {}).get("blocked")}, ensure_ascii=False))
    print("validate_pipeline_e2e:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
