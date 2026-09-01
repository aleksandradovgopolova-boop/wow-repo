#!/usr/bin/env python3
"""Product Qualification (детерминированно) — сквозные продуктовые гарантии через РЕАЛЬНЫЙ контроллер.

Аудит требовал product-qualification: «ContextBundle меняет prompt; неполная спека не пускает в
implementation; resume после сессии; auth/secret без человека не проходит; нет ложного green». Живые
сценарии с МОДЕЛЬЮ гоняются на машине пользователя (qualification/scenarios.yaml + tools/qual_run.py,
DeepSeek/стек — см. docs/qualification-runbook.md). Здесь — те же ГАРАНТИИ, но проверенные
ДЕТЕРМИНИРОВАННО в CI через `ai_ops_run.run` со scripted-proposer (механика продукта, не качество
правок модели). Это интеграционный уровень: не юнит-механика компонентов (это
validate_context_qualification), а поведение ПРОДУКТА целиком.

  PQ1 context->prompt   — compiled payload из ContextBundle реально в prompt (не только отчёт)
  PQ2 spec-first        — существующая неполная спека НЕ пускает в implementation
  PQ3 resume            — прерванную работу можно продолжить поверх коммита (не рестарт)
  PQ4 human control     — secret_boundary без человека НЕ проходит security (fail-closed)
  PQ5 atomic planning   — крупная задача -> декомпозиция на конкретные WorkPackages
  PQ6 no false-green    — dry-run НИКОГДА не ready; честный happy-path МОЖЕТ стать ready

Использование: validate_product_qualification.py [--selftest]
Возврат 0 — все PQ пройдены, 1 — есть провал.
"""

import io
import contextlib
import subprocess
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / "tools"))

import ai_ops_run       # noqa: E402
import spec_levels      # noqa: E402


def _mkrepo(files):
    td = tempfile.mkdtemp()
    for rel, content in files.items():
        p = Path(td) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    for args in (("init", "-q"), ("config", "user.email", "t@t"), ("config", "user.name", "t"),
                 ("add", "-A"), ("commit", "-q", "-m", "init")):
        subprocess.run(["git", "-C", td, *args], capture_output=True)
    return Path(td)


def _cur_branch(root):
    return subprocess.run(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def _run(*a, **k):
    """Прогнать контроллер, подавив операционный вывод (stderr active-work/worktree)."""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        return ai_ops_run.run(*a, **k)


def run_scenarios():
    r = []

    def ok(name, cond):
        r.append((name, bool(cond)))

    # PQ1: ContextBundle реально попадает в prompt модели
    root = _mkrepo({"src/keep": "x"})
    seen = {}
    def _cap(c):
        seen.setdefault("ctx", c); return {"done": True}
    rep1 = _run("PQ1_TASK_MARKER сделать", {"task_type": "QUICK", "affected_areas": ["core"]},
                root, engine="pipeline", proposer=_cap, execute=True, feature="pq1", install_deps=False)
    ctx = seen.get("ctx", "")
    ok("PQ1 context->prompt: compiled payload (=== [..) + задача реально в prompt; fed_to_model=True",
       "=== [" in ctx and "PQ1_TASK_MARKER" in ctx
       and (rep1.get("context_payload") or {}).get("fed_to_model") is True)

    # PQ2: неполная спека не пускает в РЕАЛИЗАЦИЮ (v2.115: блок ДО tool loop — ноль вызовов, ноль коммита)
    root2 = _mkrepo({"src/keep": "x"})
    spec_levels.create_spec(root2, "pq2", {"task_type": "QUICK", "affected_areas": ["core"]})  # всё missing
    calls2 = {"n": 0}
    def _prop2(c):
        calls2["n"] += 1; return {"done": True}
    rep2 = _run("PQ2 сделать", {"task_type": "QUICK", "affected_areas": ["core"]}, root2,
                engine="pipeline", proposer=_prop2, execute=True, feature="pq2",
                install_deps=False, baseline_diff=True)
    ok("PQ2 spec-first: неполная спека -> preflight blocked, tool loop НЕ запускался, коммита нет",
       rep2.get("ready_for_pr") is False and rep2.get("status") == "blocked"
       and calls2["n"] == 0 and not (rep2.get("commit") or {}).get("sha")
       and rep2.get("loop") is None
       and not (root2 / ".ai" / "worktrees" / "pq2").exists())

    # PQ3: resume поверх подтверждённой работы (не рестарт)
    root3 = _mkrepo({"src/keep": "x"})
    cur3 = _cur_branch(root3)
    s3a = iter([{"op": "write", "path": "src/p1.py", "content": "p=1\n"}, {"done": True}])
    rep3a = _run("PQ3 фаза 1", {"task_type": "QUICK", "affected_areas": ["core"]}, root3,
                 engine="pipeline", proposer=lambda c: next(s3a), execute=True, feature="pq3",
                 install_deps=False, base=cur3)  # v3.0.2: реальная ветка (консистентно с resume-фазой)
    s3b = iter([{"op": "write", "path": "src/p2.py", "content": "p=2\n"}, {"done": True}])
    rep3b = _run("PQ3 фаза 2", {"task_type": "QUICK", "affected_areas": ["core"]}, root3,
                 engine="pipeline", proposer=lambda c: next(s3b), execute=True, feature="pq3",
                 install_deps=False, resume=True, base=cur3)
    wt3 = root3 / ".ai" / "worktrees" / "pq3"
    ok("PQ3 resume: продолжение поверх коммита (обе фазы в worktree, resumed=True, не рестарт)",
       bool((rep3a.get("commit") or {}).get("sha")) and (rep3b.get("resume") or {}).get("resumed") is True
       and (wt3 / "src" / "p1.py").exists() and (wt3 / "src" / "p2.py").exists())

    # PQ4: secret_boundary без ApprovalRecord -> preflight блокирует ДО модели (человек не пройден)
    root4 = _mkrepo({"src/keep": "x"})
    calls4 = {"n": 0}
    def _prop4(c):
        calls4["n"] += 1; return {"done": True}
    rep4 = _run("PQ4 трогает секреты", {"task_type": "ENGINEERING", "affected_areas": ["core"],
                                        "secret_boundary": True},
                root4, engine="pipeline", proposer=_prop4, execute=True, feature="pq4",
                install_deps=False, baseline_diff=True)
    appr4 = ((rep4.get("preflight") or {}).get("checks") or {}).get("approvals") or {}
    ok("PQ4 human control: secret_boundary без ApprovalRecord -> preflight blocked, модель не звалась",
       rep4.get("status") == "blocked" and rep4.get("ready_for_pr") is False and calls4["n"] == 0
       and any(m["domain"] == "secrets" for m in (appr4.get("missing") or [])))

    # PQ5: крупная задача -> preflight требует подтверждения декомпозиции (не исполняет одним блобом);
    # конкретные WorkPackages реально сохранены на диске
    import yaml as _yaml
    root5 = _mkrepo({"src/keep": "x"})
    calls5 = {"n": 0}
    def _prop5(c):
        calls5["n"] += 1; return {"done": True}
    rep5 = _run("PQ5 большой рефактор", {"task_type": "ENGINEERING", "size": "large",
                                         "affected_areas": ["catalog", "orders", "billing", "search"]},
                root5, engine="pipeline", proposer=_prop5, execute=True, feature="pq5",
                install_deps=False)
    wp5_disk = _yaml.safe_load((root5 / "features" / "pq5" / "work-package.yaml").read_text(encoding="utf-8"))
    ok("PQ5 atomic planning: неатомарная задача -> preflight blocked (не блоб), конкретные WorkPackages на диске",
       rep5.get("status") == "blocked" and calls5["n"] == 0
       and wp5_disk.get("should_decompose") is True and len(wp5_disk.get("work_packages") or []) > 0
       and any("atomic-planning" in r for r in (rep5.get("preflight") or {}).get("reasons", [])))
    # выбор СУЩЕСТВУЮЩЕГО пакета из плана -> preflight по атомарности пройден; вымышленный id -> блок (v2.120)
    import atomic_planner as _ap5
    sig5b = {"task_type": "ENGINEERING", "size": "large",
             "affected_areas": ["catalog", "orders", "billing", "search"]}
    real_pid = _ap5.decompose(sig5b, wid="pq5b", child_root=root5)["work_packages"][0]["id"]
    s5b = iter([{"op": "write", "path": "src/x.py", "content": "x=1\n"}, {"done": True}])
    rep5b = _run("PQ5 один пакет", dict(sig5b, work_package_id=real_pid),
                 root5, engine="pipeline", proposer=lambda c: next(s5b), execute=True, feature="pq5b",
                 install_deps=False)
    ok("PQ5b atomic planning: выбран СУЩЕСТВУЮЩИЙ пакет из плана -> preflight по атомарности пройден",
       (rep5b.get("preflight") or {}).get("checks", {}).get("atomic", {}).get("ok") is True)
    # v2.120: ВЫМЫШЛЕННЫЙ work_package_id -> preflight блокирует (нельзя по фиктивному ID)
    rep5c = _run("PQ5 вымышленный id", dict(sig5b, work_package_id="pq5-ghost-id"),
                 root5, engine="pipeline", proposer=lambda c: {"done": True}, execute=True, feature="pq5c",
                 install_deps=False)
    ok("PQ5c atomic planning (v2.120): вымышленный work_package_id -> preflight blocked (нет обхода)",
       rep5c.get("status") == "blocked"
       and (rep5c.get("preflight") or {}).get("checks", {}).get("atomic", {}).get("selected_valid") is False)

    # PQ6: нет ложного green. dry-run (без коммита) НИКОГДА не ready. Прогон с правкой ЧЕСТНО работает
    # (реальный коммит + evidence на точном SHA + петля done), но ready_for_pr=False с НАЗВАННЫМ
    # блокером (напр. intake/spec-depth требует authoring — движок не фабрикует зелёное без evidence).
    root6 = _mkrepo({"calc.py": "def add(a,b):\n    return a+b\n"})
    s6dry = iter([{"op": "write", "path": "src/n.py", "content": "n=1\n"}, {"done": True}])
    rep6dry = _run("PQ6 dry", {"task_type": "QUICK", "affected_areas": ["core"]}, root6,
                   engine="pipeline", proposer=lambda c: next(s6dry), execute=False, feature="pq6dry",
                   install_deps=False)
    ok("PQ6a no false-green: dry-run (без коммита) НИКОГДА не ready_for_pr",
       rep6dry.get("ready_for_pr") is False)
    s6 = iter([{"op": "write", "path": "mathx.py", "content": "def clamp(x,lo,hi):\n    return max(lo,min(hi,x))\n"},
               {"done": True}])
    rep6 = _run("PQ6 честный прогон", {"task_type": "QUICK", "size": "small", "affected_areas": ["core"]},
                root6, engine="pipeline", proposer=lambda c: next(s6), execute=True, feature="pq6",
                install_deps=False)
    commit6 = rep6.get("commit") or {}
    blocked_named = bool((rep6.get("gates") or {}).get("unmet")) or bool((rep6.get("spec_depth") or {}).get("missing"))
    ok("PQ6b honesty: прогон ЧЕСТЕН — реальный коммит+evidence на SHA+петля done, но ready=False с "
       "названным блокером (не фабрикует зелёное без authoring-evidence)",
       (rep6.get("loop") or {}).get("stopped") == "done" and bool(commit6.get("sha"))
       and commit6.get("evidence_on_exact_sha") is True
       and rep6.get("ready_for_pr") is False and blocked_named)

    # PQ7: доказанный POSITIVE-GREEN — полностью корректная QUICK-задача РЕАЛЬНО достигает ready=True
    # (детерминированно, без модели). intake закрыт сигналами task_type+size+risk; тест зелёный.
    root7 = _mkrepo({"calc.py": "def add(a, b):\n    return a + b\n"})
    s7 = iter([{"op": "write", "path": "mathx.py", "content": "def clamp(x, lo, hi):\n    return max(lo, min(hi, x))\n"},
               {"op": "write", "path": "test_mathx.py",
                "content": "from mathx import clamp\n\ndef test_clamp():\n    assert clamp(5, 0, 3) == 3\n"},
               {"done": True}])
    rep7 = _run("добавить clamp с тестом", {"task_type": "QUICK", "size": "small", "risk": "low",
                                            "affected_areas": ["core"]},
                root7, engine="pipeline", proposer=lambda c: next(s7), execute=True, feature="pq7",
                install_deps=False, baseline_diff=True)
    ok("PQ7 positive-green QUICK: корректная задача -> ready_for_pr=True, overall=delivered, гейты закрыты",
       rep7.get("ready_for_pr") is True and rep7.get("overall_status") == "delivered"
       and not (rep7.get("gates") or {}).get("unmet")
       and (rep7.get("commit") or {}).get("evidence_on_exact_sha") is True
       and (root7 / "features" / "pq7" / "run-report.json").is_file())

    # PQ8: полностью зелёный ENGINEERING с author + review + security evidence. specification (OpenSpec)
    # требует реального openspec CLI -> если его нет, честно проверяем, что спек-гейт БЛОКИРУЕТ (fail-closed).
    import shutil as _sh

    def _author(prompt):
        if "requirements-artifact" in prompt:
            return ("schema_version: 1\nkind: requirements-artifact\nrequirements:\n"
                    "  - id: R1\n    statement: clamp ограничивает значение диапазоном\n"
                    "    acceptance:\n      - when x>hi then возвращает hi\n")
        if "spec-change" in prompt:
            return ("schema_version: 1\nkind: spec-change\ncapability: mathx\nwhy: нужен clamp\n"
                    "what_changes:\n  - добавить clamp\ntasks:\n  - реализовать\n"
                    "requirements:\n  - name: Clamp\n    text: The system SHALL clamp values to a range.\n"
                    "    scenarios:\n      - {name: T, when: x>hi, then: возвращает hi}\n")
        return ("schema_version: 1\nkind: plan-artifact\nwork_packages:\n"
                "  - id: WP1\n    summary: clamp\n    depends_on: []\nwrite_scope:\n  - .\n")
    # v3.0.18 (finding аудита: PQ8 positive-green падал ТОЛЬКО с openspec — no-openspec parity маскировал).
    # С v3.0.11 блокирующий ai-review не закрывается pass-вердиктом без единого чтения (рубер-стамп).
    # Ревьюер СНАЧАЛА читает изменённый файл, затем pass — иначе code_review остаётся unmet и ready=False.
    def _reviewer(p):
        if "--- mathx.py ---" in p:
            return '{"kind":"reviewer-result","status":"pass","checks":[{"id":"ok","status":"pass"}]}'
        return '{"op":"read","path":"mathx.py"}'
    root8 = _mkrepo({"calc.py": "def add(a, b):\n    return a + b\n"})
    s8 = iter([{"op": "write", "path": "mathx.py", "content": "def clamp(x, lo, hi):\n    return max(lo, min(hi, x))\n"},
               {"op": "write", "path": "test_mathx.py",
                "content": "from mathx import clamp\n\ndef test_clamp():\n    assert clamp(5, 0, 3) == 3\n"},
               {"done": True}])
    rep8 = _run("добавить clamp (engineering)", {"task_type": "ENGINEERING", "size": "small", "risk": "low",
                                                 "affected_areas": ["core"]},
                root8, engine="pipeline", proposer=lambda c: next(s8), execute=True, feature="pq8",
                install_deps=False, baseline_diff=True, author=True, author_proposer=_author,
                review=True, reviewer_proposer=_reviewer)
    reviews8 = {rv.get("gate"): rv.get("status") for rv in (rep8.get("reviews") or [])}
    sec8 = (rep8.get("security_scan") or {}).get("overall")
    if _sh.which("openspec"):
        ok("PQ8 positive-green ENGINEERING: author+review+security -> ready_for_pr=True (openspec доступен)",
           rep8.get("ready_for_pr") is True and not (rep8.get("gates") or {}).get("unmet")
           and all(a.get("valid") for a in (rep8.get("authored") or []))
           and reviews8.get("code_review") == "pass" and sec8 == "clear")
    else:
        ok("PQ8 fail-closed ENGINEERING: без openspec CLI спек-гейт БЛОКИРУЕТ (честно, не зелёный)",
           rep8.get("ready_for_pr") is False and "specification" in ((rep8.get("gates") or {}).get("unmet") or []))

    # PQ9 (v3.1): WorkPackages РЕАЛЬНО исполняются последовательно (не одним блобом). Каждый пакет —
    # свой коммит/SHA, поверх предыдущего; зависимый пакет не стартует без подтверждённого.
    import atomic_planner as _ap
    import workpackage_executor as _wpe
    root9 = _mkrepo({"calc.py": "def add(a, b):\n    return a + b\n"})
    cur9 = subprocess.run(["git", "-C", str(root9), "rev-parse", "--abbrev-ref", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    sig9 = {"task_type": "ENGINEERING", "size": "large", "risk": "low",
            "affected_areas": ["catalog", "orders", "billing"]}
    pkgs9 = _ap.decompose(sig9, wid="pq9", child_root=root9)["work_packages"]
    def _prop_for(pkg):
        it = iter([{"op": "write", "path": f"src/{pkg['id']}.py", "content": "x=1\n"}, {"done": True}])
        return lambda c: next(it)
    # v2.121 (P1.1): heavy требует спеку ДО tool loop -> исполняем пакеты с author=True (движок
    # авторизует спеку пре-стадией). Иначе первый же пакет блокируется preflight (spec-first).
    seq9 = _wpe.execute_sequence("большой рефактор", sig9, root9, pkgs9, _prop_for, feature="pq9",
                                 base=cur9, baseline_diff=False, author=True, author_proposer=_author)
    shas9 = [p.get("sha") for p in seq9["packages"]]
    ok("PQ9 sequential executor: 3 пакета исполнены по одному (свой SHA, цепочкой), не одним блобом",
       seq9["executed_all"] is True and len(pkgs9) == 3 and all(shas9) and len(set(shas9)) == 3
       and seq9["sequential_chain"] is True
       and all((root9 / "features" / "pq9" / "work-packages" / p["id"] / "report.json").is_file()
               for p in seq9["packages"]))

    return r


def main(argv):
    results = run_scenarios()
    ok = True
    for name, passed in results:
        ok = ok and passed
        print(f"{'PASS' if passed else 'FAIL'} {name}")
    print("НАПОМИНАНИЕ: живые сценарии с моделью (качество правок) — на машине пользователя "
          "(qualification/scenarios.yaml + tools/qual_run.py, DeepSeek/стек). Здесь — ГАРАНТИИ продукта.")
    print("validate_product_qualification:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def selftest():
    return main([])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
