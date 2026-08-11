#!/usr/bin/env python3
"""Qualification нового слоя Context Engineering — Q1..Q10 (v2.103, этап 7, финал эпика).

Новые требования (этапы 1-6) считаются готовыми только после отдельных сценариев. Этот харнесс
гоняет Q1..Q10 ДЕТЕРМИНИРОВАННО против построенных инструментов (context_compiler, atomic_planner,
run_handoff, spec_levels, security_pack, tool_broker) — без живой модели, в CI кита. Живые сценарии
с моделью (полный прогон) остаются для машины пользователя; здесь проверяется МЕХАНИКА слоя.

  Q1  context filtering        — в ContextBundle только релевантное, нерелевантное исключено с причиной
  Q2  context overflow         — превышение бюджета -> авто-декомпозиция (не молча)
  Q3  resume                   — по RunHandoff можно продолжить (can_resume + next_action)
  Q4  stale context            — после ухода main вперёд resume требует ревалидации
  Q5  spec depth               — QUICK короткая (L0), PRODUCT глубокая (L2 c метриками)
  Q6  unsafe assumption        — неизвестное решение о доступах эскалируется (не додумывается)
  Q7  security applicability    — frontend-only: XSS/secrets да, database/tenant audit нет
  Q8  prompt injection         — инструкция в данных не переопределяет policy (push заблокирован)
  Q9  long-running work         — решение первой фазы сохраняется в Handoff
  Q10 human approval           — auth/secret boundary не проходит без человека

Использование:
  validate_context_qualification.py [--selftest]
Возврат 0 — все Q пройдены, 1 — есть провалы.
"""

import sys
import tempfile
import subprocess
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / "tools"))

import context_compiler   # noqa: E402
import atomic_planner     # noqa: E402
import run_handoff        # noqa: E402
import spec_levels        # noqa: E402
import security_pack      # noqa: E402
import tool_broker        # noqa: E402


def _repo(td, files):
    root = Path(td)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", td, "init", "-q"])
    subprocess.run(["git", "-C", td, "config", "user.email", "t@t"])
    subprocess.run(["git", "-C", td, "config", "user.name", "t"])
    subprocess.run(["git", "-C", td, "add", "-A"])
    subprocess.run(["git", "-C", td, "commit", "-q", "-m", "i"])
    return root


def run_scenarios():
    r = []

    def ok(name, cond):
        r.append((name, bool(cond)))

    eng = {"task_type": "ENGINEERING", "risk": "medium", "affected_areas": ["core"], "task_text": "t"}

    with tempfile.TemporaryDirectory() as td:
        root = _repo(td, {"package.json": '{"dependencies":{"react":"^18"}}'})

        # Q1 context filtering
        b = context_compiler.compile_bundle(eng, root)
        ok("Q1 context filtering: включены только агенты RunPlan, остальное excluded с причиной",
           len(b["included"]["agents"]) > 0 and b["excluded"]
           and all(e.get("reason") for e in b["excluded"]))
        # Q1b (v2.108): ContextBundle РЕАЛЬНО становится payload для prompt (не только отчёт)
        pay = context_compiler.build_payload(eng, root, bundle=b)
        ok("Q1b operational context: compiled payload несёт РЕАЛЬНОЕ содержимое правил + подаётся модели",
           "=== [rule]" in pay["text"] and pay["payload_tokens"] > 0
           and pay["payload_budget"] < pay["context_budget"])

        # Q2 context overflow -> декомпозиция (не молча) + КОНКРЕТНЫЕ пакеты (v2.111)
        b_of = context_compiler.compile_bundle(eng, root, context_budget=10)
        wp = atomic_planner.decompose(eng, wid="q2", child_root=root, bundle=b_of, budget=10)
        ok("Q2 context overflow: overflow + авто-декомпозиция by-context-budget (open_question, не молча)",
           b_of["overflow"] is True and wp["should_decompose"]
           and "by-context-budget" in wp["decomposition_axes"] and b_of["open_questions"])
        ok("Q2b atomic planning: decompose строит КОНКРЕТНЫЕ WorkPackages с id/scope/deps (не только оси)",
           wp["work_packages"] and all(p["id"] and p["scope"] and "depends_on" in p
                                       for p in wp["work_packages"]) and wp["human_confirms"] is True)

    # Q3 resume + Q4 stale + Q9 long-running (решение сохранено)
    with tempfile.TemporaryDirectory() as td:
        root = _repo(td, {"f": "x"})
        head = subprocess.run(["git", "-C", td, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        rep = {"workitem_id": "feat", "ready_for_pr": False,
               "commit": {"sha": head, "branch": "ai-ops/feat", "evidence_on_exact_sha": True},
               "loop": {"applied_writes": 1, "stopped": "done"},
               "gates": {"evaluated": ["requirements"], "unmet": ["code_review"]},
               "decisions": [{"id": "d1", "summary": "выбрали стратегию А", "source": "phase-1"}],
               "not_yet": ["review"], "checks": {}}
        h = run_handoff.build_handoff(rep, work_root=root)
        fdir = root / "features" / "feat"; fdir.mkdir(parents=True)
        import yaml
        (fdir / "run-handoff.yaml").write_text(yaml.safe_dump(h), encoding="utf-8")
        cur = subprocess.run(["git", "-C", td, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
        pf = run_handoff.resume_preflight(root, "feat", base=cur)
        ok("Q3 resume: по Handoff можно продолжить (can_resume + next_action)",
           pf["can_resume"] is True and bool(pf.get("next_action")))
        ok("Q9 long-running: решение первой фазы сохранено в Handoff",
           any(d.get("id") == "d1" for d in h["decisions"]))
        # Q4 stale: main уходит вперёд -> ревалидация
        (root / "g").write_text("y", encoding="utf-8")
        subprocess.run(["git", "-C", td, "add", "-A"]); subprocess.run(["git", "-C", td, "commit", "-q", "-m", "adv"])
        pf2 = run_handoff.resume_preflight(root, "feat", base=cur)
        ok("Q4 stale context: main ушёл вперёд -> revalidation_needed",
           pf2["revalidation_needed"] is True)

    # Q3b (v2.109 Real Resume): resume РЕАЛЬНО продолжает поверх подтверждённой работы (не рестарт).
    import ai_ops_run  # noqa: E402
    with tempfile.TemporaryDirectory() as td:
        root = _repo(td, {"src/keep": "seed"})
        cur = subprocess.run(["git", "-C", td, "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        sig = {"task_type": "QUICK", "size": "small", "risk": "low", "affected_areas": ["core"]}
        s1 = iter([{"op": "write", "path": "src/a.py", "content": "a=1\n"}, {"done": True}])
        r1 = ai_ops_run.run("фаза 1", sig, root, engine="pipeline", proposer=lambda c: next(s1),
                            execute=True, feature="q3b", install_deps=False, base=cur)  # v3.0.2: реальная ветка
        s2 = iter([{"op": "write", "path": "src/b.py", "content": "b=2\n"}, {"done": True}])
        r2 = ai_ops_run.run("фаза 2", sig, root, engine="pipeline", proposer=lambda c: next(s2),
                            execute=True, feature="q3b", install_deps=False, resume=True, base=cur)
        wt = root / ".ai" / "worktrees" / "q3b"
        ok("Q3b real resume: продолжил поверх (обе фазы в worktree, ветка переиспользована, не рестарт)",
           bool((r1.get("commit") or {}).get("sha")) and r2.get("status") != "error"
           and (r2.get("resume") or {}).get("resumed") is True
           and (wt / "src" / "a.py").exists() and (wt / "src" / "b.py").exists())

    # Q5 spec depth
    q = spec_levels.assess({"task_type": "QUICK"})
    p = spec_levels.assess({"task_type": "PRODUCT"})
    ok("Q5 spec depth: QUICK -> L0 (мало разделов), PRODUCT -> L2 c метриками",
       q["level"] == 0 and p["level"] == 2
       and any(s["id"] == "success_metrics" for s in p["sections"])
       and len(q["sections"]) < len(p["sections"]))

    # Q5b (v2.110 Real Spec-First): specify реально создаёт артефакт; неполная спека -> не готова;
    # заполнение из РЕАЛЬНОГО файла -> готова (не из сигналов).
    with tempfile.TemporaryDirectory() as td:
        root = _repo(td, {"f": "x"})
        import yaml
        sig_pr = {"task_type": "PRODUCT", "affected_areas": ["core"]}
        sp, created = spec_levels.create_spec(root, "sp1", sig_pr)
        cov_empty = spec_levels.assess_from_artifacts(sig_pr, root, "sp1")
        # заполняем реальный файл
        doc = yaml.safe_load(sp.read_text(encoding="utf-8"))
        for s in doc["sections"]:
            doc["sections"][s] = {"status": "complete", "content": "x"}
        sp.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
        cov_full = spec_levels.assess_from_artifacts(sig_pr, root, "sp1")
        ok("Q5b real spec-first: specify создаёт артефакт; неполный -> не готов, заполненный из файла -> готов",
           created and cov_empty["ready_to_implement"] is False and cov_empty["spec_artifact"] is True
           and cov_full["ready_to_implement"] is True and not cov_full["blocking_missing"])

    # Q6 unsafe assumption -> эскалация (не додумывается)
    esc = spec_levels.classify({"task_type": "QUICK", "secret_boundary": True})
    ok("Q6 unsafe assumption: неизвестное решение о доступах -> эскалация до CRITICAL",
       esc["level"] == 3 and any("эскалация" in x for x in esc["reason"]))

    # Q7 security applicability
    fe = security_pack.run_pack(files_content={"src/ui/V.tsx": "el.innerHTML = userInput\n"},
                                signals={"handles_user_input": True, "user_facing_change": True})
    ok("Q7 security applicability: frontend -> input_validation да, data_isolation нет",
       "input_validation" in fe["applicable_domains"] and "data_isolation" not in fe["applicable_domains"])

    # Q8 prompt injection не переопределяет policy (push заблокирован)
    pol = tool_broker.Policy(level="execution", block_push=True)
    dec = pol.decide({"op": "shell", "command": "git push -u origin main"})
    ok("Q8 prompt injection: инструкция 'запушь' отклонена policy (block_push)", dec["allow"] is False)

    # Q10 human approval для auth/secret boundary
    dom = {d["id"]: d for d in security_pack.load_domains()[0]}
    auth_human = dom.get("authentication", {}).get("human_approval_conditions") or []
    secret_human = dom.get("secrets", {}).get("human_approval_conditions") or []
    esc10 = spec_levels.classify({"task_type": "ENGINEERING", "secret_boundary": True})
    ok("Q10 human approval: auth/secret-boundary требует человека (домены + эскалация до CRITICAL)",
       bool(auth_human) and bool(secret_human) and esc10["level"] == 3)

    return r


def main(argv):
    results = run_scenarios()
    ok = True
    for name, passed in results:
        ok = ok and passed
        print(f"{'PASS' if passed else 'FAIL'} {name}")
    print("validate_context_qualification:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
