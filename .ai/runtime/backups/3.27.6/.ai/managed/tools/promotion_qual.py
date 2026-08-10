#!/usr/bin/env python3
"""promotion_qual.py (v3.6.8 harness) — драйвер живой квалификации PromotionQualificationPlan.

Не фейкает живые прогоны. Делает то, что МОЖНО честно сделать до провайдер-ключа:
  1. грузит и валидирует план (validate_promotion_qualification.check);
  2. **проверяет весь обязательный набор негативов ОФФЛАЙН** — они детерминированно решаемы уже сейчас
     (fail-closed поведение parallel_planner / gate_runtime / context_engine), поэтому матрицу негативов
     можно ДОКАЗАТЬ, а не «запланировать»;
  3. preflight готовности среды для трёх живых прогонов (ключ / git / node / scratch-репо);
  4. runbook — точная последовательность команд каждого прогона (чтобы запуск был одной командой).

Живые прогоны (python-child / ts-react-storybook / parallel-2) требуют провайдер-ключа + Node/React +
scratch child-репо и здесь НЕ исполняются: без готовности execute честно возвращает status=blocked с
перечнем недостающего (никогда не выдаёт фейковый pass).

CLI:
  promotion_qual.py --verify-negatives [plan]   # оффлайн-доказательство матрицы негативов
  promotion_qual.py --preflight [plan]          # готовность среды к живым прогонам
  promotion_qual.py --runbook [plan]            # команды прогонов (dry, без исполнения)
  promotion_qual.py --selftest
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / "tools"))
sys.path.insert(0, str(PKG / "validation"))
import parallel_planner as pp          # noqa: E402
import gate_runtime as gr              # noqa: E402
import context_engine as ce            # noqa: E402
import context_retrieval as cr         # noqa: E402
import validate_promotion_qualification as vpq   # noqa: E402

DEFAULT_PLAN = PKG / "qualification" / "promotion" / "v3.6.8-plan.yaml"
# v3.6.8: провайдер по умолчанию — kimi (moonshot) через openai-compatible ветку make_provider.
# Anthropic не обязателен; ядро provider-agnostic и fail-closed (слабая модель -> honest fail, не false-green).
DEFAULT_PROVIDER = "openai-compatible"
DEFAULT_MODEL_BY_PROVIDER = {"openai-compatible": "kimi-k2", "anthropic": "claude-sonnet-5"}


def _resolve_provider(provider=None, model=None):
    provider = provider or os.environ.get("AI_OPS_PROVIDER") or DEFAULT_PROVIDER
    model = (model or os.environ.get("AI_OPS_MODEL")
             or os.environ.get("OPENAI_COMPATIBLE_MODEL")
             or DEFAULT_MODEL_BY_PROVIDER.get(provider, "kimi-k2"))
    return provider, model


# --- Оффлайн-доказательства негативов: каждый covers-тег -> детерминированная проверка fail-closed ---

def _neg_write_scope_overlap():
    okp, _ = pp.can_parallel({"id": "a", "write_scope": ["**"]},
                             {"id": "b", "write_scope": ["src/b/**"]})
    return okp is False, "глобальный ** не уходит в parallel с src/b/** -> serialize"


def _neg_package_fail():
    r = pp.integration_gate(["a", "b"],
                            {"a": {"status": "pass", "sha": "aaaaaaa", "gate_report": {"all_pass": True, "tested_revision": "aaaaaaa"}},
                             "b": {"status": "fail", "sha": "bbbbbbb", "gate_report": {"all_pass": False, "tested_revision": "bbbbbbb"}}})
    return r["proceed"] is False, "package fail -> fan-in НЕ начинается"


def _neg_contract_not_fixed():
    r = pp.integration_gate(["a"],
                            {"a": {"status": "pass", "sha": "aaaaaaa", "gate_report": {"all_pass": True, "tested_revision": "aaaaaaa"}}},
                            shared_contracts=["C"], contract_shas={})
    return r["proceed"] is False, "общий контракт без зафиксированного SHA -> block"


def _neg_duplicate_package_id():
    p = pp.plan({"packages": [{"id": "a", "write_scope": ["a/**"]}, {"id": "a", "write_scope": ["b/**"]}]})
    return p["valid"] is False, "duplicate package id -> plan.valid=false"


def _neg_merge_conflict():
    r = pp.integration_gate(["a"],
                            {"a": {"status": "pass", "sha": "aaaaaaa", "gate_report": {"all_pass": True, "tested_revision": "aaaaaaa"}}},
                            conflicts=1)
    return r["proceed"] is False, "merge conflict -> block"


def _neg_base_moved():
    r = pp.integration_gate(["a"],
                            {"a": {"status": "pass", "sha": "aaaaaaa", "gate_report": {"all_pass": True, "tested_revision": "aaaaaaa"}}},
                            base_moved=True)
    return r.get("revalidation_required") is True and r["open_pr"] is False, "base moved -> revalidation, PR не открыт"


def _neg_aggregate_wrong_sha():
    r = pp.integration_gate(["a"],
                            {"a": {"status": "pass", "sha": "aaaaaaa", "gate_report": {"all_pass": True, "tested_revision": "aaaaaaa"}}},
                            aggregate={"all_pass": True, "tested_revision": "OTHER99"}, integration_sha="1234567")
    return r["open_pr"] is False, "aggregate evidence на другом SHA -> PR не открыт"


def _neg_reviewer_abstain_twice():
    res, meta = gr.decide("accessibility_review", {"ui_changed": True, "ui_impact": "user_facing"},
                          ["abstain", "abstain"], max_retries=1)
    return (res["delivery_allowed"] is False and res["resolution"] == "pending_human"
            and meta["human_handoff"] is True), "reviewer abstain x2 -> pending_human, доставка запрещена"


def _neg_mandatory_context_missing():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "a.py").write_text("# x\n", encoding="utf-8")
        afp = {"id": "T", "kind": "AccessFilterPolicy",
               "rules": [{"role": "executor", "allowed_classes": ["public", "internal"]}]}
        v = ce.build_context(root, "x", "executor", sha="deadbee", afp=afp,
                             v1_mandatory=["does/not/exist.md"], require_snapshot=False)
    return v["valid"] is False, "обязательный контекст отсутствует -> view valid=false"


def _neg_child_policy_changed():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "tools").mkdir()
        (root / "tools" / "b.py").write_text("# data-class: confidential\n# foo\n", encoding="utf-8")
        wide = {"id": "P", "kind": "AccessFilterPolicy",
                "rules": [{"role": "executor", "allowed_classes": ["public", "internal", "confidential"]}]}
        narrow = {"id": "P", "kind": "AccessFilterPolicy",
                  "rules": [{"role": "executor", "allowed_classes": ["public", "internal"]}]}
        cache = cr.RetrievalCache()
        _, h1 = cache.get_or_build(root, "foo", "executor", wide, 10000, sha="s1")
        vn, h2 = cache.get_or_build(root, "foo", "executor", narrow, 10000, sha="s1")
        leaked = "tools/b.py" in {i["file"] for i in vn["included"]}
    return (h1 is False and h2 is False and not leaked), "ужесточение policy -> cache miss, нет утечки старого view"


NEG_CHECKS = {
    "write_scope_overlap": _neg_write_scope_overlap,
    "package_fail": _neg_package_fail,
    "contract_not_fixed": _neg_contract_not_fixed,
    "duplicate_package_id": _neg_duplicate_package_id,
    "merge_conflict": _neg_merge_conflict,
    "base_moved": _neg_base_moved,
    "aggregate_wrong_sha": _neg_aggregate_wrong_sha,
    "reviewer_abstain_twice": _neg_reviewer_abstain_twice,
    "mandatory_context_missing": _neg_mandatory_context_missing,
    "child_policy_changed": _neg_child_policy_changed,
}


def load_plan(path=DEFAULT_PLAN):
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    errs = vpq.check(data)
    if errs:
        raise ValueError(f"план невалиден: {errs}")
    return data


def verify_negatives(plan):
    """Оффлайн-доказательство: каждый заявленный негатив реально держит fail-closed СЕЙЧАС."""
    out = []
    for neg in plan.get("negative_scenarios", []):
        cov = neg.get("covers")
        fn = NEG_CHECKS.get(cov)
        if not fn:
            out.append({"id": neg.get("id"), "covers": cov, "proven": False, "detail": "нет оффлайн-проверки"})
            continue
        try:
            ok, detail = fn()
        except Exception as ex:  # noqa: BLE001
            ok, detail = False, f"исключение: {type(ex).__name__}: {ex}"
        out.append({"id": neg.get("id"), "covers": cov, "proven": bool(ok), "detail": detail})
    return {"total": len(out), "proven": sum(1 for r in out if r["proven"]), "results": out}


def preflight(plan, provider=None, model=None):
    """Готовность среды к ЖИВЫМ прогонам (ничего не исполняет)."""
    provider, model = _resolve_provider(provider, model)
    # честно: для openai-compatible (kimi/moonshot) ключа мало — нужен ещё base_url,
    # иначе make_provider не сможет вызвать endpoint.
    if provider == "anthropic":
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    elif provider == "openai":
        has_key = bool(os.environ.get("OPENAI_API_KEY"))
    else:  # openai-compatible (kimi/moonshot/deepseek): ключ И base_url
        has_key = bool(os.environ.get("OPENAI_COMPATIBLE_API_KEY")
                       and os.environ.get("OPENAI_COMPATIBLE_BASE_URL"))
    has_git = bool(shutil.which("git"))
    has_node = bool(shutil.which("node") and shutil.which("npx"))
    has_scratch = bool(os.environ.get("AI_OPS_SCRATCH_REPO"))
    checks = {"provider_key": has_key, "git": has_git, "node_react": has_node, "scratch_repo": has_scratch}
    need = {"python-child": ["provider_key", "git", "scratch_repo"],
            "ts-react-storybook": ["provider_key", "git", "node_react", "scratch_repo"],
            "parallel-2": ["provider_key", "git", "scratch_repo"]}
    per_run = {}
    for r in plan.get("runs", []):
        req = need.get(r.get("kind"), [])
        miss = [c for c in req if not checks.get(c)]
        per_run[r["id"]] = {"ready": not miss, "missing": miss}
    missing = sorted({c for c, ok in checks.items() if not ok})
    hint = ("для openai-compatible нужны OPENAI_COMPATIBLE_API_KEY + OPENAI_COMPATIBLE_BASE_URL"
            if provider not in ("anthropic", "openai") and not checks["provider_key"] else None)
    return {"provider": provider, "model": model, "checks": checks, "ready": not missing,
            "missing": missing, "provider_key_hint": hint, "per_run": per_run}


def runbook(plan, provider=None, model=None):
    """Точные команды каждого прогона (dry). Плейсхолдеры <task>/<scratch> заполняет оператор.
    По умолчанию провайдер — kimi (moonshot) через openai-compatible; переопределяется provider/model
    или env AI_OPS_PROVIDER / AI_OPS_MODEL."""
    provider, model = _resolve_provider(provider, model)
    common = 'GITHUB_TOKEN=$(gh auth token) python3 tools/ai_ops_run.py run "<task>" <scratch>'
    prov = (f"--engine pipeline --provider {provider} --model {model} "
            "--execute --author --review --open-pr --context-shadow")
    # для openai-compatible (kimi/moonshot) ключ+endpoint читаются из env make_provider'ом
    env_note = ([] if provider in ("anthropic", "openai")
                else ["# env: OPENAI_COMPATIBLE_BASE_URL=<moonshot .../v1/chat/completions> "
                      "OPENAI_COMPATIBLE_API_KEY=<ключ> (секрет ТОЛЬКО в env, не в файлы/логи)"])
    steps = {
        "python-child": env_note + [f"{common} {prov}",
                         "проверить rep.context_shadow.valid + snapshot_verified + sources_used + mandatory сохранён"],
        "ts-react-storybook": env_note + ["cd <scratch> && npm ci && npm run build-storybook   # storybook-static/index.json",
                               f"{common} {prov}",
                               "проверить UIEvidenceBundle на integration-SHA (stories/interaction/a11y/visual)"],
        "parallel-2": env_note + ["python3 tools/parallel_planner.py <scratch>/work-graph.yaml --json   # plan.valid=true",
                       f"{common} {prov}   # bounded parallel-2 -> integration SHA -> один PR",
                       "integration_gate: полный доказательный набор package-результатов -> один draft PR"],
    }
    return [{"run": r["id"], "kind": r["kind"], "provider": provider, "model": model,
             "commands": steps.get(r["kind"], [])} for r in plan.get("runs", [])]


def execute(plan, dry_run=True, provider=None, model=None):
    """Честный гейт: без готовности среды НЕ исполняет живые прогоны и НЕ выдаёт pass."""
    pf = preflight(plan, provider, model)
    negatives = verify_negatives(plan)
    if not pf["ready"] or dry_run:
        return {"status": ("dry-run" if dry_run else "blocked"),
                "reason": ("dry-run (живые прогоны не запускались)" if dry_run
                           else f"среда не готова: не хватает {pf['missing']}"),
                "negatives_offline": negatives, "preflight": pf, "runbook": runbook(plan, provider, model),
                "note": "живые прогоны не выполнены и НЕ засчитаны (никакого фейкового pass)"}
    # ready и не dry-run: здесь оператор/следующий live-инкремент исполняет runbook.
    return {"status": "ready", "negatives_offline": negatives, "preflight": pf, "runbook": runbook(plan, provider, model),
            "note": "среда готова — исполнить runbook (живой прогон = отдельный live-инкремент v3.6.8)"}


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    plan = load_plan()
    expect("реальный план грузится и валиден", plan["plan_id"] == "PQP-001")

    neg = verify_negatives(plan)
    expect(f"ВСЕ негативы доказаны оффлайн ({neg['proven']}/{neg['total']})",
           neg["proven"] == neg["total"] and neg["total"] == 10)
    for r in neg["results"]:
        if not r["proven"]:
            print("   НЕ доказан:", r["covers"], "-", r["detail"])

    pf = preflight(plan)
    expect("preflight возвращает checks по всем 4 требованиям среды",
           set(pf["checks"]) == {"provider_key", "git", "node_react", "scratch_repo"})
    expect("preflight per_run по каждому прогону", set(pf["per_run"]) ==
           {r["id"] for r in plan["runs"]})

    rb = runbook(plan)
    expect("runbook покрывает все 3 прогона с командами",
           len(rb) == 3 and all(r["commands"] for r in rb))

    # execute без ключа/среды -> blocked ИЛИ dry-run, НО никогда не 'passed'
    ex = execute(plan, dry_run=False)
    expect("execute без готовности -> status blocked (не фейковый pass)",
           ex["status"] in ("blocked", "ready") and "passed" not in ex["status"])
    exd = execute(plan, dry_run=True)
    expect("execute dry-run -> живые прогоны не засчитаны", exd["status"] == "dry-run")

    print("promotion_qual selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    plan = load_plan(args[0] if args else DEFAULT_PLAN)
    if "--verify-negatives" in argv:
        out = verify_negatives(plan)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out["proven"] == out["total"] else 1
    if "--preflight" in argv:
        print(json.dumps(preflight(plan), ensure_ascii=False, indent=2))
        return 0
    if "--runbook" in argv:
        print(json.dumps(runbook(plan), ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(execute(plan, dry_run=True), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
