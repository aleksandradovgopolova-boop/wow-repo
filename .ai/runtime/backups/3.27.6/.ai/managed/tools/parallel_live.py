#!/usr/bin/env python3
"""parallel_live.py (v3.8.3-rc2) — LIVE MULTI-PACKAGE execution с governed fan-in. Два пути:
  - run_live (СЕРИЙНО, один репо): execution_concurrency=serial, parallel_safe=true, fan_in=live;
  - run_live_concurrent (НАСТОЯЩАЯ конкурентность): отдельный disposable-клон+ветка+прогон на пакет,
    integration-клон -> fetch package-ветки -> merge -> stack-aware aggregate на НОВОМ integration-SHA.

Инварианты (из parallel_planner/executor, не ослабляются):
  - package-SHA НЕ доказывают всю работу -> после fan-in новый integration-SHA + повтор проверок;
  - доставка открывается ТОЛЬКО при зелёном aggregate на integration-SHA;
  - пакеты parallel-SAFE по непересекающимся write_scope.

v3.8.3-rc2 Parallel Trust Wiring (concurrent path):
  #1 package-specific signals (write_scope/affected_areas/shared_contracts/capability/budget), не одинаковые;
  #2 post-commit changed_files ⊆ write_scope -> нарушитель НЕ проходит в fan-in;
  #3 contract_shas прокидывается в executor (shared-contract-first, не хардкод None);
  #5 parallel_live НЕ владеет доставкой -> возвращает DeliveryPlan для канонического Intent/Receipt controller;
  #5b DeliveryPlan несёт РЕАЛЬНЫЙ github_remote (клон интеграции имеет origin = локальный путь);
  #7 fail-closed на clone/checkout/fetch/merge (ошибка блокирует, не «тихо зелёный»);
  #8 run-scoped уникальный каталог клонов + гарантированная очистка.
Аггрегатный strict security (#4/#4b) и provider fallback (#6) — в ai_ops_run/workpackage_executor (rc2b).

Только stdlib + существующие модули кита. CLI: parallel_live.py --selftest
"""
from __future__ import annotations

import fnmatch
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parallel_executor as pe   # noqa: E402


def _git(root, *args, check=True):
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:200]}")
    return r


def _ensure_identity(root):
    """Гарантировать git-идентичность в СОЗДАННОМ НАМИ клоне. -> True, если поставили fallback.

    `git clone` не наследует локальный user.name/email источника. В окружении без ГЛОБАЛЬНОЙ
    идентичности (CI-раннер, чистый контейнер) любой коммит в таком клоне падает — и fan-in
    считал бы это КОНФЛИКТОМ пакетов, хотя ветки сливаются чисто. Это ложный негатив, а по
    инварианту кита статус обязан быть честным: конфликт — это конфликт, а не отсутствие
    настроек. Fallback ставится ТОЛЬКО когда идентичность не разрешается — реальные окружения
    (у пользователя/агента она есть) сохраняют свою и авторство коммитов не подменяется.
    """
    probe = subprocess.run(["git", "-C", str(root), "var", "GIT_COMMITTER_IDENT"],
                           capture_output=True, text=True)
    if probe.returncode == 0:
        return False
    _git(root, "config", "user.email", "ai-ops@local", check=False)
    _git(root, "config", "user.name", "AI Ops", check=False)
    return True


# --- v3.8.3-rc2 Parallel Trust Wiring helpers ---
_INFRA_PREFIXES = (".ai/", "openspec/", "features/")


def _glob_match(path, pat):
    """write_scope-глоб. 'api/**' -> префикс; 'aa.py' -> точное; иначе fnmatch."""
    if pat.endswith("/**"):
        base = pat[:-3]
        return path == base or path.startswith(base + "/")
    if pat.endswith("**"):
        return path.startswith(pat[:-2])
    return fnmatch.fnmatch(path, pat) or path == pat


def _changed_files(root, base_sha, head="HEAD"):
    """Файлы, изменённые между base_sha и head (для post-commit scope-проверки). head по умолчанию HEAD,
    но concurrent-путь ОБЯЗАН передавать РЕАЛЬНЫЙ package-SHA: ai_ops_run коммитит в ВЛОЖЕННЫЙ
    worktree-branch внутри клона, а HEAD самого клона остаётся на base -> diff base...HEAD был бы пуст
    (scope-проверка no-op). Диффим base...<package_sha>."""
    r = _git(root, "diff", "--name-only", f"{base_sha}...{head}", check=False)
    if r.returncode != 0:
        r = _git(root, "diff", "--name-only", base_sha, head, check=False)
    return [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]


def _is_build_artifact(f):
    """Инкрементальные build-артефакты (не код модели): setuptools egg-info, кэши, dist/build, lock-файлы,
    node_modules. Создаются install/сборкой, не писателем -> НЕ считаются нарушением write_scope."""
    return (".egg-info/" in f or "__pycache__/" in f or f.endswith((".pyc", ".pyo"))
            or f.startswith(("dist/", "build/", "node_modules/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/"))
            or "/dist/" in f or "/build/" in f or "/node_modules/" in f
            or f.endswith((".lock",)) or f == "package-lock.json" or f.endswith("/package-lock.json"))


def _scope_ok(changed, write_scope):
    """#2 post-commit: changed_files ⊆ write_scope. Инфра-пути (.ai/openspec/features) и инкрементальные
    build-артефакты (egg-info/кэши/lock/node_modules) разрешены. write_scope пуст -> не форсим здесь
    (границу объявляет план). -> (ok, outside_files)."""
    if not write_scope:
        return True, []
    outside = [f for f in changed
               if not f.startswith(_INFRA_PREFIXES) and not _is_build_artifact(f)
               and not any(_glob_match(f, p) for p in write_scope)]
    return (not outside), outside


def _github_remote(child_root):
    """#5b: реальный GitHub-remote (origin) исходного child_root — чтобы доставка шла на GitHub,
    а НЕ в локальный клон (клон интеграции имеет origin = локальный путь). None если origin не GitHub."""
    r = _git(child_root, "remote", "get-url", "origin", check=False)
    url = (r.stdout or "").strip()
    return url if (r.returncode == 0 and ("github.com" in url or url.endswith(".git"))) else None


def _pkg_signals(base_signals, pkg):
    """#1: package-specific view сигналов — write_scope/affected_areas/shared_contracts/capability/budget
    из объявления пакета WorkGraph. НЕ шлём одинаковые signals всем пакетам."""
    sig = dict(base_signals or {})
    ws = pkg.get("write_scope")
    if ws:
        sig["write_scope"] = ws
        sig["affected_areas"] = pkg.get("affected_areas") or sorted({p.split("/")[0] for p in ws if "/" in p})
    elif pkg.get("affected_areas"):
        sig["affected_areas"] = pkg["affected_areas"]
    for k in ("shared_contracts", "capability", "budget"):
        if pkg.get(k):
            sig[k if k != "budget" else "budget_contract"] = pkg[k]
    return sig


def package_result_from_rep(rep):
    """rep (ai_ops_run) -> package-result для integration_gate. ЧИСТАЯ функция (тестируема)."""
    if not isinstance(rep, dict):
        return {"status": "error", "sha": None, "gate_report": {"all_pass": False}, "error": "rep не dict"}
    sha = (rep.get("commit") or {}).get("sha")
    ok = bool(rep.get("ready_for_pr"))
    return {"status": "pass" if ok else "fail", "sha": sha,
            "gate_report": {"all_pass": ok, "tested_revision": sha},
            "model_resolution": rep.get("model_resolution")}


def make_package_runner(child_root, base_sha, task_map, signals, run_fn, features_dir=None):
    """package_runner(pkg): прогнать ai_ops_run для задачи пакета в СВОЕЙ ветке (feature=pkg.id).
    run_fn — ai_ops_run.run (инъекция для тестов). Возвращает package-result."""
    def runner(pkg):
        pid = pkg["id"]
        task = task_map[pid]
        # каждый пакет от общей базы, своя фича/ветка
        _git(child_root, "checkout", "-q", "main", check=False)
        _git(child_root, "reset", "--hard", "-q", base_sha)
        _git(child_root, "clean", "-fdq", "-e", ".ai", check=False)
        rep = run_fn(task, dict(signals), Path(child_root), engine="pipeline",
                     provider_name="openai-compatible", feature=pid, execute=True,
                     author=True, review=True, features_dir=features_dir)
        return package_result_from_rep(rep)
    return runner


def make_integration_runner(child_root, base_sha, integration_branch="ai-ops/integration"):
    """integration_runner(results): создать integration-ветку от base, слить ветки пакетов (git fan-in),
    ПОВТОРИТЬ aggregate (pytest) на НОВОМ integration-SHA. -> (integration_sha, aggregate, conflicts, base_moved)."""
    def runner(results):
        _git(child_root, "checkout", "-q", "main", check=False)
        _git(child_root, "branch", "-D", integration_branch, check=False)
        _git(child_root, "checkout", "-q", "-B", integration_branch, base_sha)
        conflicts = 0
        for pid in results:
            br = f"ai-ops/{pid}"
            m = _git(child_root, "merge", "--no-edit", "--no-ff", br, check=False)
            if m.returncode != 0:
                conflicts += 1
                _git(child_root, "merge", "--abort", check=False)
        integration_sha = _git(child_root, "rev-parse", "HEAD").stdout.strip()
        # v3.8.2 STACK-AWARE aggregate на integration-SHA: детект стека (project_detector) + повтор
        # evidence_collector (backend pytest/lint/typecheck; frontend build/lint/test; ...) — НЕ хардкод
        # pytest. Как у sequential-executor'а (_aggregate_verify). Fail-closed при сбое коллектора.
        try:
            import project_detector as _pd, evidence_collector as _ec, tool_broker as _tb
            _pol = _tb.sandbox_policy(child_root=str(child_root))
            _coll = _ec.collect(_pd.detect(child_root), child_root, _pol)
            _iv = (_coll.get("gate_evidence") or {}).get("implementation_verification") or {}
            stack_ok = _iv.get("status") == "pass"
            stack_checks = {k: (v.get("status") if isinstance(v, dict) else v) for k, v in (_coll.get("checks") or {}).items()}
        except Exception as _e:  # noqa: BLE001 — сбой коллектора -> fail-closed
            stack_ok = False
            stack_checks = {"error": f"{type(_e).__name__}: {_e}"[:160]}
        all_pass = conflicts == 0 and stack_ok
        aggregate = {"all_pass": all_pass, "tested_revision": integration_sha, "conflicts": conflicts,
                     "stack_aware": True, "stack_checks": stack_checks}
        return integration_sha, aggregate, conflicts, False
    return runner


def preflight_disposable(child_root):
    """v3.7.1 (#2): runner делает reset --hard/clean -fd -> ЗАПРЕЩЕН в обычном рабочем checkout с
    незакоммиченными файлами (уничтожил бы их). Требует чистый/disposable checkout. -> (ok, reason)."""
    st = _git(child_root, "status", "--porcelain", check=False)
    dirty = [ln for ln in (st.stdout or "").splitlines() if ln.strip() and not ln.strip().endswith(".ai")]
    if dirty:
        return False, ("грязный checkout: runner делает reset --hard/clean -fd и уничтожит "
                       f"незакоммиченные файлы ({len(dirty)}). Нужен disposable-clone/чистый checkout.")
    return True, "clean"


def run_live(wg, child_root, base_sha, task_map, signals, run_fn, open_pr=False,
             repo_slug=None, features_dir=None, integration_branch="ai-ops/integration"):
    """Live MULTI-PACKAGE execution с governed fan-in (НЕ настоящий concurrency — см. поля ниже): пакеты
    -> fan-in -> (опц.) ОДИН draft PR. Пакеты СЕРИЙНО (max_parallel=1, против гонок git-worktree в одном
    репо); parallel-SAFE по write_scope; настоящая конкурентность = отдельные клоны на пакет.
    ТРЕБУЕТ disposable/чистый checkout (runner делает reset --hard/clean)."""
    ok, reason = preflight_disposable(child_root)
    if not ok:
        return {"proceed": False, "stage": "preflight", "reason": reason,
                "execution_concurrency": "serial", "parallel_safe": True, "fan_in": "live",
                "delivery": {"intents": 0, "open_pr": False}, "pr": None}
    pr = make_package_runner(child_root, base_sha, task_map, signals, run_fn, features_dir)
    ir = make_integration_runner(child_root, base_sha, integration_branch)
    rec = pe.execute_parallel(wg, pr, ir, contract_shas=None, max_parallel=1)
    # честные поля (не выдаём serial за parallel): governance/fan-in — живые, concurrency — серийный
    rec["execution_concurrency"] = "serial"
    rec["parallel_safe"] = True
    rec["fan_in"] = "live"
    rec["concurrency_note"] = "серийно (max_parallel=1); настоящая конкурентность требует отдельных клонов на пакет"
    rec["pr"] = None
    if open_pr and rec.get("delivery", {}).get("open_pr") and repo_slug:
        _git(child_root, "push", "-f", "-q", "origin", integration_branch, check=False)
        title = f"[parallel-2] {wg.get('id')} fan-in @ {rec['integration_sha'][:12]}"
        body = (f"Автоматический parallel-2 fan-in (integration-SHA {rec['integration_sha'][:12]}).\n"
                f"Пакеты: {', '.join(task_map)}. aggregate повторён на integration-SHA.\n\n"
                "🤖 Generated with [Claude Code](https://claude.com/claude-code)")
        p = subprocess.run(["gh", "pr", "create", "--repo", repo_slug, "--draft",
                            "--head", integration_branch, "--base", "main",
                            "--title", title, "--body", body], cwd=str(child_root),
                           capture_output=True, text=True)
        rec["pr"] = (p.stdout.strip() or p.stderr.strip())[:300]
    return rec


def make_isolated_package_runner(child_root, base_sha, task_map, signals, run_fn, clones_dir, clones):
    """v3.8.3 TRUE CONCURRENT: каждый пакет — в СВОЁМ disposable-клоне (git clone) на base_sha -> безопасно
    конкурентно (нет гонок git-worktree одного репо). Заполняет clones[pid]={path,branch}.
    v3.8.3-rc2: (#1) package-specific signals; (#7) fail-closed clone/checkout; (#2) post-commit
    changed_files ⊆ write_scope (нарушение -> пакет fail, не проходит в fan-in)."""
    def runner(pkg):
        pid = pkg["id"]
        cpath = Path(clones_dir) / pid
        cl = subprocess.run(["git", "clone", "-q", str(child_root), str(cpath)], capture_output=True, text=True)
        if cl.returncode != 0:  # #7 fail-closed
            clones[pid] = {"path": str(cpath), "branch": f"ai-ops/{pid}", "error": "clone-failed"}
            return {"status": "error", "sha": None, "gate_report": {"all_pass": False},
                    "error": f"clone: {cl.stderr.strip()[:160]}", "clone": str(cpath)}
        _ensure_identity(cpath)  # прогон пакета коммитит в этом клоне — идентичность не унаследована
        co = _git(cpath, "checkout", "-q", base_sha, check=False)
        if co.returncode != 0:  # #7 fail-closed
            clones[pid] = {"path": str(cpath), "branch": f"ai-ops/{pid}", "error": "checkout-failed"}
            return {"status": "error", "sha": None, "gate_report": {"all_pass": False},
                    "error": f"checkout {base_sha[:12]}: {co.stderr.strip()[:160]}", "clone": str(cpath)}
        rep = run_fn(task_map[pid], _pkg_signals(signals, pkg), Path(cpath), engine="pipeline",
                     provider_name="openai-compatible", feature=pid, execute=True, author=True, review=True)
        clones[pid] = {"path": str(cpath), "branch": f"ai-ops/{pid}"}
        res = package_result_from_rep(rep)
        res["clone"] = str(cpath)
        # #2 post-commit: заявленный write_scope принуждается на РЕАЛЬНОМ диффе (клон изолирует физически,
        # но не запрещает модели тронуть лишний файл — принуждаем декларацию плана исполнением).
        if res.get("sha") and pkg.get("write_scope"):
            changed = _changed_files(cpath, base_sha, res["sha"])   # #2-fix: диффим РЕАЛЬНЫЙ package-SHA,
            sok, outside = _scope_ok(changed, pkg["write_scope"])   # не HEAD клона (ai_ops_run — вложенный worktree)
            res["scope_check"] = {"write_scope": pkg["write_scope"], "changed": len(changed),
                                  "outside": outside[:20], "ok": sok}
            if not sok:
                res["status"] = "fail"
                res.setdefault("gate_report", {})["all_pass"] = False
                res["scope_violation"] = outside[:20]
        return res
    return runner


def make_isolated_integration_runner(child_root, base_sha, clones, integration_branch="ai-ops/integration"):
    """v3.8.3: integration-КЛОН от base; fetch веток пакетов ИЗ их клонов (по пути) + merge; stack-aware
    aggregate (project_detector+evidence_collector) на НОВОМ integration-SHA. fail-closed при сбое."""
    def runner(results):
        iroot = (clones.get("_integration") or {}).get("path")
        if not iroot:  # #7 fail-closed: нет integration-клона -> блок (не «тихо зелёный»)
            return None, {"all_pass": False, "conflicts": len(results), "stack_aware": True,
                          "stack_checks": {"error": "integration-клон отсутствует"},
                          "isolation": "per-package-clone"}, len(results) or 1, False
        co = _git(iroot, "checkout", "-q", "-B", integration_branch, base_sha, check=False)
        if co.returncode != 0:  # #7 fail-closed
            return None, {"all_pass": False, "conflicts": len(results), "stack_aware": True,
                          "stack_checks": {"error": f"integration checkout: {co.stderr.strip()[:120]}"},
                          "isolation": "per-package-clone"}, len(results) or 1, False
        conflicts = 0
        for pid in results:
            c = clones.get(pid)
            if not c or c.get("error"):  # #7: битый/отсутствующий пакетный клон -> конфликт (блокирует)
                conflicts += 1; continue
            f = _git(iroot, "fetch", "-q", c["path"], f"{c['branch']}:{c['branch']}", check=False)
            if f.returncode != 0:  # #7 fail-closed: не смогли забрать ветку пакета
                conflicts += 1; continue
            m = _git(iroot, "merge", "--no-edit", "--no-ff", c["branch"], check=False)
            if m.returncode != 0:
                conflicts += 1; _git(iroot, "merge", "--abort", check=False)
        integration_sha = _git(iroot, "rev-parse", "HEAD").stdout.strip()
        try:
            import project_detector as _pd, evidence_collector as _ec, tool_broker as _tb
            _coll = _ec.collect(_pd.detect(iroot), iroot, _tb.sandbox_policy(child_root=str(iroot)))
            _iv = (_coll.get("gate_evidence") or {}).get("implementation_verification") or {}
            stack_ok = _iv.get("status") == "pass"
            stack_checks = {k: (v.get("status") if isinstance(v, dict) else v) for k, v in (_coll.get("checks") or {}).items()}
        except Exception as _e:  # noqa: BLE001
            stack_ok = False; stack_checks = {"error": f"{type(_e).__name__}: {_e}"[:160]}
        all_pass = conflicts == 0 and stack_ok
        return integration_sha, {"all_pass": all_pass, "tested_revision": integration_sha, "conflicts": conflicts,
                                 "stack_aware": True, "stack_checks": stack_checks,
                                 "isolation": "per-package-clone"}, conflicts, False
    return runner


def run_live_concurrent(wg, child_root, base_sha, task_map, signals, run_fn, clones_dir=None,
                        repo_slug=None, integration_branch="ai-ops/integration", max_parallel=None,
                        contract_shas=None):
    """v3.8.3 НАСТОЯЩИЙ concurrent parallel-2: отдельный disposable-клон+ветка+прогон на пакет, КОНКУРЕНТНО;
    integration-клон -> fetch package-ветки -> merge -> stack-aware aggregate. Честные поля:
    execution_concurrency=concurrent, isolation=per-package-clone. Основной checkout НЕ трогается.

    v3.8.3-rc2 Parallel Trust Wiring:
      (#3) contract_shas прокидывается в executor (shared-contract-first, не хардкод None);
      (#5) parallel_live НЕ владеет доставкой — возвращает rec["delivery_plan"] для канонического
           controller'а (DeliveryIntent -> push -> DeliveryReceipt); прямого push/gh pr create тут НЕТ;
      (#5b) delivery_plan несёт РЕАЛЬНЫЙ github_remote исходного репо (клон интеграции имеет origin =
            локальный путь — доставка в него не попала бы на GitHub);
      (#7) fail-closed при сбое клона интеграции;
      (#8) run-scoped уникальный каталог клонов + гарантированная очистка (self-created)."""
    _self_clones = clones_dir is None
    if _self_clones:
        clones_dir = tempfile.mkdtemp(prefix="ai-ops-parallel-")
    try:
        clones = {}
        iroot = Path(clones_dir) / "_integration"
        cl = subprocess.run(["git", "clone", "-q", str(child_root), str(iroot)], capture_output=True, text=True)
        if cl.returncode != 0:  # #7 fail-closed
            return {"proceed": False, "stage": "isolation", "execution_concurrency": "concurrent",
                    "isolation": "per-package-clone", "reason": f"integration clone: {cl.stderr.strip()[:160]}",
                    "delivery": {"open_pr": False}, "delivery_plan": None, "pr": None}
        _ensure_identity(iroot)  # merge fan-in — это КОММИТ; клон не унаследовал идентичность
        clones["_integration"] = {"path": str(iroot)}
        pr = make_isolated_package_runner(child_root, base_sha, task_map, signals, run_fn, clones_dir, clones)
        ir = make_isolated_integration_runner(child_root, base_sha, clones, integration_branch)
        n = max_parallel or min(len(wg.get("packages", []) or [1]), getattr(pe.pp, "MAX_PARALLEL", 2))
        rec = pe.execute_parallel(wg, pr, ir, contract_shas=contract_shas, max_parallel=max(2, n))
        rec["execution_concurrency"] = "concurrent"
        rec["isolation"] = "per-package-clone"
        rec["parallel_safe"] = True
        rec["fan_in"] = "live"
        rec["pr"] = None
        # #5/#5b DeliveryPlan (не доставляем сами): канонический controller зафиксирует Intent, запушит на
        # РЕАЛЬНЫЙ github_remote, откроет PR, зафиксирует Receipt. Готово к доставке только при зелёном aggregate.
        _ready = bool(rec.get("delivery", {}).get("open_pr") and rec.get("integration_sha"))
        rec["delivery_plan"] = {
            "kind": "DeliveryPlan", "ready": _ready,
            "integration_sha": rec.get("integration_sha"),
            "integration_branch": integration_branch,
            "base_sha": base_sha,
            "integration_clone": str(iroot),
            "github_remote": _github_remote(child_root),   # #5b: реальный GitHub, не локальный клон
            "repo_slug": repo_slug,
            "packages": list(task_map),
            "note": "доставка через канонический DeliveryIntent/Receipt controller; parallel_live не пушит сам",
        }
        return rec
    finally:
        if _self_clones:  # #8 гарантированная очистка self-created каталога
            shutil.rmtree(clones_dir, ignore_errors=True)


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    # package_result_from_rep — чистое отображение
    expect("rep ready -> pass + sha", package_result_from_rep(
        {"ready_for_pr": True, "commit": {"sha": "a" * 40}})["status"] == "pass")
    expect("rep not-ready -> fail", package_result_from_rep(
        {"ready_for_pr": False, "commit": {"sha": "b" * 40}})["status"] == "fail")
    expect("rep не dict -> error", package_result_from_rep(None)["status"] == "error")

    # оркестрация с МОК-раннерами (без git/LLM): 2 parallel-safe пакета -> fan-in -> open_pr
    wg = {"schema_version": 1, "kind": "WorkGraph", "id": "WG-T", "feature": "f", "execution_mode": "hybrid",
          "packages": [{"id": "api", "depends_on": [], "write_scope": ["a/**"]},
                       {"id": "ui", "depends_on": [], "write_scope": ["b/**"]}]}
    good = {"api": None, "ui": None}

    def fake_run(task, sig, root, **kw):
        return {"ready_for_pr": True, "commit": {"sha": kw["feature"].ljust(40, "0")}}

    # инъекция мок-integration_runner через прямой execute_parallel
    def pr_runner(pkg):
        return package_result_from_rep(fake_run(None, None, None, feature=pkg["id"]))

    def ir_runner(results):
        return ("c" * 40, {"all_pass": True, "tested_revision": "c" * 40}, 0, False)

    rec = pe.execute_parallel(wg, pr_runner, ir_runner, contract_shas=None, max_parallel=1)
    expect("2 parallel-safe пакета зелены -> fan-in proceed + open_pr",
           rec["proceed"] and rec["delivery"]["open_pr"] and rec["integration_sha"] == "c" * 40)

    def ir_conflict(results):
        return ("d" * 40, {"all_pass": False, "tested_revision": "d" * 40}, 1, False)
    rec2 = pe.execute_parallel(wg, pr_runner, ir_conflict, contract_shas=None, max_parallel=1)
    expect("конфликт при fan-in -> НЕ open_pr (integration не зелёный)", rec2["delivery"]["open_pr"] is False)

    def pr_fail(pkg):
        return {"status": "fail", "sha": None, "gate_report": {"all_pass": False}}
    rec3 = pe.execute_parallel(wg, pr_fail, ir_runner, contract_shas=None, max_parallel=1)
    expect("пакет не pass -> fan-in НЕ начинается (pre-fan-in блок)",
           rec3["delivery"]["open_pr"] is False and rec3["stage"] == "pre-fan-in")

    # v3.7.1 (#2) preflight disposable: грязный checkout -> отказ (runner уничтожил бы правки)
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        _git(td, "init", "-q", check=False)
        _git(td, "config", "user.email", "t@t"); _git(td, "config", "user.name", "t")
        (Path(td) / "a.py").write_text("x=1\n", encoding="utf-8")
        _git(td, "add", "-A"); _git(td, "commit", "-qm", "init", check=False)
        okp, _ = preflight_disposable(td)
        expect("preflight: чистый checkout -> ok", okp is True)
        (Path(td) / "dirty.py").write_text("y=2\n", encoding="utf-8")
        okp2, rsn = preflight_disposable(td)
        expect("preflight: грязный checkout -> ОТКАЗ (защита незакоммиченных)", okp2 is False and "грязный" in rsn)

    # v3.8.2 STACK-AWARE integration runner: aggregate детектит стек и гоняет evidence_collector,
    # а НЕ хардкодит pytest. На python-репо -> stack_checks несёт 'test'; зелёный тест -> all_pass.
    # ГАРД (урок: CI-quality имеет ТОЛЬКО pyyaml, без pytest): all_pass-часть только при наличии pytest;
    # структурная часть (stack_aware, stack_checks) проверяется всегда.
    import importlib.util as _ilu
    _has_pytest = _ilu.find_spec("pytest") is not None
    with tempfile.TemporaryDirectory() as td2:
        _git(td2, "init", "-q", check=False)
        _git(td2, "config", "user.email", "t@t"); _git(td2, "config", "user.name", "t")
        (Path(td2) / "pyproject.toml").write_text(
            "[project]\nname='t'\nversion='0.1.0'\n[tool.pytest.ini_options]\npythonpath=['.']\n", encoding="utf-8")
        Path(td2, "tests").mkdir()
        (Path(td2) / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        _git(td2, "add", "-A"); _git(td2, "commit", "-qm", "seed", check=False)
        base2 = _git(td2, "rev-parse", "HEAD").stdout.strip()
        # пакет p1: добавляет файл на своей ветке
        _git(td2, "checkout", "-q", "-b", "ai-ops/p1", check=False)
        (Path(td2) / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        _git(td2, "add", "-A"); _git(td2, "commit", "-qm", "p1", check=False)
        _git(td2, "checkout", "-q", base2, check=False)
        ir = make_integration_runner(td2, base2, integration_branch="ai-ops/it-test")
        _isha, _agg, _conf, _bm = ir({"p1": {"status": "pass"}})
        expect("v3.8.2: aggregate STACK-AWARE (не хардкод pytest)", _agg.get("stack_aware") is True)
        expect("v3.8.2: stack_checks несёт проверку 'test' детектированного стека", "test" in (_agg.get("stack_checks") or {}))
        if _has_pytest:
            expect("v3.8.2: зелёный python-стек на integration-SHA -> all_pass", _agg.get("all_pass") is True and _conf == 0)
        else:
            expect("v3.8.2: без pytest в env — структура stack-aware цела (all_pass не проверяем)", _conf == 0)

    # v3.8.3 TRUE CONCURRENT: отдельный клон+ветка на пакет (конкурентно) -> integration-клон fetch+merge.
    with tempfile.TemporaryDirectory() as td3, tempfile.TemporaryDirectory() as cdir:
        _git(td3, "init", "-q", check=False)
        _git(td3, "config", "user.email", "t@t"); _git(td3, "config", "user.name", "t")
        (Path(td3) / "pyproject.toml").write_text(
            "[project]\nname='t'\nversion='0.1.0'\n[tool.pytest.ini_options]\npythonpath=['.']\n", encoding="utf-8")
        Path(td3, "tests").mkdir()
        (Path(td3) / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        _git(td3, "add", "-A"); _git(td3, "commit", "-qm", "seed", check=False)
        base3 = _git(td3, "rev-parse", "HEAD").stdout.strip()

        def commit_run(task, sig, root, **kw):  # мок вместо LLM: пишет файл + коммитит в СВОЙ клон
            pid = kw["feature"]
            # git clone НЕ копирует локальный user.name/email -> в CI (без глобальной идентичности)
            # коммит бы молча падал. Реальный путь (ai_ops_run) настраивает идентичность сам; мок — тоже.
            _git(root, "config", "user.email", "t@t"); _git(root, "config", "user.name", "t")
            _git(root, "checkout", "-q", "-B", f"ai-ops/{pid}", check=False)
            (Path(root) / f"{pid}.py").write_text(f"def {pid}():\n    return 1\n", encoding="utf-8")
            _git(root, "add", "-A"); _git(root, "commit", "-qm", f"pkg {pid}", check=False)
            return {"ready_for_pr": True, "commit": {"sha": _git(root, "rev-parse", "HEAD").stdout.strip()}}

        wg3 = {"schema_version": 1, "kind": "WorkGraph", "id": "WG-C", "feature": "f", "execution_mode": "hybrid",
               "packages": [{"id": "aa", "depends_on": [], "write_scope": ["aa.py"]},
                            {"id": "bb", "depends_on": [], "write_scope": ["bb.py"]}]}
        rec_c = run_live_concurrent(wg3, td3, base3, {"aa": "t", "bb": "t"}, {"task_type": "QUICK"},
                                    commit_run, cdir)
        expect("v3.8.3: execution_concurrency=concurrent + isolation=per-package-clone",
               rec_c.get("execution_concurrency") == "concurrent" and rec_c.get("isolation") == "per-package-clone")
        expect("v3.8.3: отдельные клоны на пакет созданы (aa, bb, _integration)",
               all((Path(cdir) / p).is_dir() for p in ("aa", "bb", "_integration")))
        agg_c = rec_c.get("aggregate") or {}
        expect("v3.8.3: fan-in слил обе ветки без конфликта (disjoint scope)", agg_c.get("conflicts") == 0)
        # v3.8.3-rc2 #5/#5b: parallel_live НЕ пушит сам -> DeliveryPlan для controller'а; несёт github_remote
        dp = rec_c.get("delivery_plan") or {}
        expect("rc2 #5: возвращён DeliveryPlan (parallel_live не владеет доставкой, нет прямого push)",
               dp.get("kind") == "DeliveryPlan" and "github_remote" in dp and rec_c.get("pr") is None)
        if _has_pytest:
            expect("v3.8.3: concurrent fan-in зелёный -> proceed + open_pr",
                   rec_c.get("proceed") is True and rec_c.get("delivery", {}).get("open_pr") is True)
            expect("rc2 #5: зелёный aggregate -> DeliveryPlan.ready=True с integration_sha",
                   dp.get("ready") is True and bool(dp.get("integration_sha")))
        else:
            expect("v3.8.3: без pytest — структура concurrent fan-in цела", agg_c.get("stack_aware") is True)

        # v3.8.3-rc2 #2 POST-COMMIT SCOPE: пакет пишет ФАЙЛ ВНЕ своего write_scope -> пакет FAIL (не в fan-in).
        def rogue_run(task, sig, root, **kw):  # мок ai_ops_run: коммит в ВЛОЖЕННУЮ ветку, HEAD клона -> base
            pid = kw["feature"]
            _git(root, "config", "user.email", "t@t"); _git(root, "config", "user.name", "t")
            _git(root, "checkout", "-q", "-B", f"ai-ops/{pid}", check=False)
            (Path(root) / "OUTSIDE_scope.py").write_text("x=1\n", encoding="utf-8")  # не совпадает с cc.py
            _git(root, "add", "-A"); _git(root, "commit", "-qm", f"rogue {pid}", check=False)
            _sha = _git(root, "rev-parse", "HEAD").stdout.strip()
            _git(root, "checkout", "-q", base3, check=False)   # клон HEAD обратно на base (как оставляет ai_ops_run)
            return {"ready_for_pr": True, "commit": {"sha": _sha}}   # #2-fix guard: чек ОБЯЗАН читать этот sha, не HEAD
        with tempfile.TemporaryDirectory() as cdir2:
            _rrunner = make_isolated_package_runner(td3, base3, {"cc": "t"}, {"task_type": "QUICK"},
                                                    rogue_run, cdir2, {})
            _rres = _rrunner({"id": "cc", "write_scope": ["cc.py"]})
            expect("rc2 #2: запись вне write_scope -> пакет FAIL (scope-violation)",
                   _rres.get("status") == "fail" and "OUTSIDE_scope.py" in (_rres.get("scope_violation") or []))
        with tempfile.TemporaryDirectory() as cdir3:
            _grunner = make_isolated_package_runner(td3, base3, {"cc": "t"}, {"task_type": "QUICK"},
                                                    commit_run, cdir3, {})
            _gres = _grunner({"id": "cc", "write_scope": ["cc.py"]})
            expect("rc2 #2: запись В пределах write_scope -> пакет проходит scope-check",
                   (_gres.get("scope_check") or {}).get("ok") is True)

    # v3.8.3-rc2c: КЛОН НЕ НАСЛЕДУЕТ ИДЕНТИЧНОСТЬ. Окружение без ГЛОБАЛЬНОЙ идентичности (CI-раннер,
    # чистый контейнер) роняло merge-коммит fan-in, и он считался КОНФЛИКТОМ пакетов — ложный негатив.
    # Проверяем оба края: нет идентичности -> fallback + коммит проходит; своя есть -> НЕ подменяем.
    import os as _os
    with tempfile.TemporaryDirectory() as tdi:
        src = Path(tdi) / "src"; src.mkdir()
        _git(src, "init", "-q", check=False)
        _git(src, "config", "user.email", "own@t"); _git(src, "config", "user.name", "own")
        (src / "f.py").write_text("x=1\n", encoding="utf-8")
        _git(src, "add", "-A"); _git(src, "commit", "-qm", "seed", check=False)
        cl = Path(tdi) / "clone"
        subprocess.run(["git", "clone", "-q", str(src), str(cl)], capture_output=True, text=True)
        _saved = {k: _os.environ.get(k) for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")}
        try:  # имитируем окружение БЕЗ глобальной идентичности (как CI-раннер)
            _os.environ["GIT_CONFIG_GLOBAL"] = _os.devnull
            _os.environ["GIT_CONFIG_SYSTEM"] = _os.devnull
            # Кросс-платформенно: на Linux-раннере без идентичности `git var GIT_COMMITTER_IDENT` падает ->
            # ставится fallback; на macOS git СИНТЕЗИРУЕТ identity из OS (user@host) -> fallback не нужен.
            # Инвариант _ensure_identity на ОБОИХ: fallback ровно тогда, когда identity неразрешима, и после
            # вызова коммит в клоне проходит (это и снимает ложный конфликт fan-in).
            _resolvable = subprocess.run(["git", "-C", str(cl), "var", "GIT_COMMITTER_IDENT"],
                                         capture_output=True, text=True).returncode == 0
            expect("rc2c: fallback ставится РОВНО когда идентичность неразрешима (кросс-платформенно)",
                   _ensure_identity(cl) is (not _resolvable))
            (cl / "g.py").write_text("y=2\n", encoding="utf-8")
            _git(cl, "add", "-A")
            expect("rc2c: после _ensure_identity коммит в клоне проходит (не ложный конфликт fan-in)",
                   _git(cl, "commit", "-qm", "c", check=False).returncode == 0)
            expect("rc2c: своя идентичность НЕ подменяется", _ensure_identity(src) is False
                   and _git(src, "config", "user.email").stdout.strip() == "own@t")
        finally:
            for _k, _v in _saved.items():
                _os.environ.pop(_k, None) if _v is None else _os.environ.__setitem__(_k, _v)

    # v3.8.3-rc2 unit: helpers (#1 pkg signals, glob/scope, #8 self-clean каталог)
    expect("rc2 #1: _pkg_signals прокидывает write_scope+выводит affected_areas",
           _pkg_signals({"task_type": "X"}, {"id": "p", "write_scope": ["api/**", "db/x.py"]}).get("affected_areas") == ["api", "db"])
    expect("rc2: _glob_match 'api/**' ловит вложенный путь", _glob_match("api/routes/x.py", "api/**") and not _glob_match("ui/x.py", "api/**"))
    expect("rc2 #2: _scope_ok пропускает инфра-пути (.ai/…), ловит чужие",
           _scope_ok([".ai/plan.yaml", "api/x.py", "ui/y.py"], ["api/**"]) == (False, ["ui/y.py"]))
    expect("rc3: _scope_ok освобождает build-артефакты (egg-info/кэш/lock), не считает нарушением",
           _scope_ok(["calc.py", "tasks.egg-info/PKG-INFO", "__pycache__/x.pyc", "package-lock.json"],
                     ["calc.py"]) == (True, [])
           and _scope_ok(["calc.py", "tasks.egg-info/PKG-INFO", "rogue.py"], ["calc.py"]) == (False, ["rogue.py"]))
    with tempfile.TemporaryDirectory() as td4:  # #8: self-created clones_dir очищается (нет утечки)
        _git(td4, "init", "-q", check=False); _git(td4, "config", "user.email", "t@t"); _git(td4, "config", "user.name", "t")
        (Path(td4) / "s.py").write_text("x=1\n", encoding="utf-8"); _git(td4, "add", "-A"); _git(td4, "commit", "-qm", "s", check=False)
        _b4 = _git(td4, "rev-parse", "HEAD").stdout.strip()
        _pre = set(Path(tempfile.gettempdir()).glob("ai-ops-parallel-*"))
        run_live_concurrent({"schema_version": 1, "kind": "WorkGraph", "id": "WG-X", "feature": "f",
                             "execution_mode": "hybrid", "packages": [{"id": "z", "depends_on": [], "write_scope": ["z.py"]}]},
                            td4, _b4, {"z": "t"}, {"task_type": "QUICK"}, commit_run)  # clones_dir=None -> self-created
        _post = set(Path(tempfile.gettempdir()).glob("ai-ops-parallel-*"))
        expect("rc2 #8: self-created каталог клонов очищен (нет утечки временных клонов)", _post == _pre)

    print("parallel_live selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
