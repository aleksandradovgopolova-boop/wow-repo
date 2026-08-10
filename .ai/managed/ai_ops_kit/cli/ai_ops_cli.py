#!/usr/bin/env python3
"""Intent-based UX поверх движка (v2.102, эпик Context Engineering, этап 6).

Снаружи AI Ops должен быть проще внутренней архитектуры. Обычный сценарий управляется намерениями,
а не флагами: пользователю не нужно помнить --engine pipeline / --author / --review / --baseline-diff
/ --sandbox — система сама подбирает workflow, стадии и нужные флаги (presets) и ПОКАЗЫВАЕТ
execution preview до запуска. Низкоуровневые флаги остаются доступны, но не обязательны.

Команды намерений:
  new · onboard · discuss · specify · plan · run · resume · review · status · health

Использование:
  ai_ops_cli.py <intent> [задача] <child_root> [--signals '{...}'] [--feature name] [--json] [--execute]
  ai_ops_cli.py preview <intent> [задача] <child_root> ...
  ai_ops_cli.py --selftest
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402
# intent -> (описание, какое действие, нужен ли текст задачи)
INTENTS = {
    "new":     ("создать новую фичу/каркас", "scaffold", False),
    "onboard": ("определить стек и команды репозитория", "onboard", False),
    "discuss": ("обсудить идею до спецификации (discovery)", "discuss", True),
    "specify": ("построить спецификацию нужной глубины", "specify", True),
    "plan":    ("построить RunPlan + контекст + оценку пакета (без правок)", "plan", True),
    "run":     ("выполнить задачу движком (авто-подбор стадий)", "run", True),
    "do":      ("автономный прогон: run --execute + авторазрешение блокировщиков", "do", True),
    "advise":  ("инженерный совет: окружения, delivery plan, альтернативы (без исполнения)", "advise", True),
    "resume":  ("продолжить прерванную работу по фиче", "resume", False),
    "review":  ("независимый ревью произведённого", "review", True),
    "status":  ("статус активной работы", "status", False),
    "health":  ("здоровье продукта", "health", False),
    # v3.35 Product Operating Model: план продукта и его связность.
    "next":    ("что взять следующим: где мы, что идёт, что блокирует, что можно параллельно", "next", False),
    "model":   ("модель продуктового репозитория: классификация, контуры, пробелы, вопросы", "model", False),
}


def resolve_flags(signals):
    """Авто-подбор внутренних флагов по классу задачи (preset). Пользователь их не задаёт вручную."""
    tt = (signals.get("task_type") or "QUICK").upper()
    flags = {"engine": "pipeline", "sandbox": True, "baseline_diff": True,
             "review": False, "author": False}
    if tt in ("ENGINEERING", "PRODUCT", "CRITICAL", "AI_FEATURE", "RESEARCH"):
        flags["review"] = True
        flags["author"] = True
    if signals.get("fix") or tt == "QUICK" and signals.get("require_fix"):
        flags["require_fix"] = True
    return flags


def build_preview(intent, task, child_root, signals):
    """Execution preview: что понято, что будет сделано, какие данные, какие approvals, результат."""
    from ai_ops_kit.engine import run_plan
    from ai_ops_kit.context import context_compiler
    from ai_ops_kit.gates import spec_levels
    from ai_ops_kit.engine import atomic_planner
    signals = dict(signals or {})
    if task:
        signals.setdefault("task_text", task)
    plan = run_plan.build_plan(signals, workitem_id=signals.get("feature"))
    # v2.107 (finding аудита): единый результат классификации. Раньше router мог решить ENGINEERING,
    # а preset/Spec-First — QUICK (task_type по умолчанию) -> противоречивый режим (workflow
    # ENGINEERING, spec L0, review/author off -> закономерный блок). Теперь task_type берём из
    # РЕШЕНИЯ роутера (base_workflow), и его же используют resolve_flags и spec_levels.
    if not signals.get("task_type"):
        signals["task_type"] = plan["base_workflow"]
    flags = resolve_flags(signals)
    bundle, bundle_error = None, None
    try:
        bundle = context_compiler.compile_bundle(signals, child_root, plan=plan)
    except Exception as _e:  # noqa: BLE001 — сборка контекста не должна ронять превью...
        # ...но и молчать о деградации нельзя: с bundle=None превью печатало «агентов 0 · ~None
        # ток.» как обычный результат, и прогон с несобранным контекстом выглядел нормальным
        # (показательный случай из внешнего ревью про 137 проглоченных исключений).
        bundle, bundle_error = None, f"{type(_e).__name__}: {_e}"[:200]
    cov = spec_levels.assess(signals)
    wp = atomic_planner.assess(signals, child_root=child_root, bundle=bundle)

    # approvals: CRITICAL уровень, needs_human разделы, human-approval сигналы
    approvals = []
    if cov["level"] >= 3:
        approvals.append("человек: критическое/необратимое изменение (L3 CRITICAL)")
    if cov["needs_human"]:
        approvals.append("человек: разделы спецификации " + ", ".join(cov["needs_human"]))
    if signals.get("secret_boundary") or signals.get("destructive"):
        approvals.append("человек: затронута граница секретов/деструктивное действие")

    expected = ("проверяемый draft PR (если гейты закрыты)" if intent == "run"
                else {"plan": "RunPlan + оценка без изменений кода",
                      "specify": f"спецификация уровня {cov['level_name']}",
                      "review": "вердикты независимых ревьюеров",
                      "onboard": "RepositoryProfile (стек/команды)",
                      "status": "список активной работы", "health": "Product Health Score",
                      "next": "ответ на четыре вопроса + следующая работа с обоснованием",
                      "model": "понимание репозитория: класс, контуры, пробелы, вопросы владельцу",
                      "discuss": "черновик проблемы/гипотез (discovery)",
                      "new": "каркас фичи",
                      "resume": "продолжение с последнего подтверждённого шага"}.get(intent, "результат намерения"))

    return {
        "schema_version": 1, "kind": "ExecutionPreview",
        "intent": intent, "understood": {"task": task, "task_type": signals.get("task_type", "QUICK"),
                                          "workflow": plan["base_workflow"],
                                          "classification_confidence": plan.get("classification_confidence", "normal"),
                                          "spec_level": cov["level_name"]},
        "will_do": {"stages": plan["gates"], "tracks": [t["track"] for t in plan.get("required_tracks", [])],
                    "auto_flags": flags},
        "data_used": {"agents": (bundle or {}).get("included", {}).get("agents", []),
                      "rules": (bundle or {}).get("included", {}).get("rules", []),
                      "estimated_tokens": (bundle or {}).get("estimated_tokens"),
                      "context_budget": (bundle or {}).get("context_budget"),
                      # None здесь означает «контекст не собран», а не «контекст пуст» — разницу
                      # обязан видеть и человек, и машиночитаемый потребитель превью.
                      "context_error": bundle_error},
        "approvals_needed": approvals,
        "decomposition_advised": wp["should_decompose"],
        "expected_result": expected,
    }


def _print_preview(pv):
    u = pv["understood"]
    print(f"■ intent: {pv['intent']} · {INTENTS.get(pv['intent'], ('',))[0]}")
    print(f"  понял: {u['task_type']} -> workflow {u['workflow']} · спецификация {u['spec_level']}")
    af = pv["will_do"]["auto_flags"]
    print(f"  сделаю: гейтов {len(pv['will_do']['stages'])} · авто-режим "
          f"(engine={af['engine']}, review={af['review']}, author={af['author']}, sandbox={af['sandbox']})")
    du = pv["data_used"]
    if du.get("context_error"):
        print(f"  ⚠ данные: КОНТЕКСТ НЕ СОБРАН ({du['context_error']}) — прогон пойдёт вслепую, "
              f"оценки агентов и токенов недоступны")
    else:
        print(f"  данные: агентов {len(du['agents'])} · ~{du['estimated_tokens']}/{du['context_budget']} ток.")
    if pv["approvals_needed"]:
        for a in pv["approvals_needed"]:
            print(f"  approval: {a}")
    if pv["decomposition_advised"]:
        print("  ⚠ советую разбить задачу (превышает атомарный размер)")
    print(f"  ожидаю: {pv['expected_result']}")


def _wid_for(task, signals, feature):
    from ai_ops_kit.engine import run_plan
    return feature or run_plan.build_plan(dict(signals, task_text=task or ""),
                                          workitem_id=feature)["workitem_id"]


def _run_intent(intent, task, child_root, signals, a):
    """v2.112 Intent UX: РЕАЛЬНОЕ действие для намерения. -> код возврата или None (нет спец-действия)."""
    import yaml
    child_root = Path(child_root)
    js = a.json

    if intent == "onboard":
        from ai_ops_kit.shared import project_detector
        prof = project_detector.detect(child_root)
        out = child_root / ".ai" / "repository-profile.yaml"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(yaml.safe_dump(prof, allow_unicode=True, sort_keys=False), encoding="utf-8")
        if js:
            print(json.dumps({"written": str(out), "profile": prof}, ensure_ascii=False, indent=2))
        else:
            stacks = ", ".join(s.get("language", "?") for s in prof.get("stacks", [])) or "не определён"
            print(f"ONBOARD: стек {stacks} · профиль записан {out.relative_to(child_root)}")
            for s in prof.get("stacks", []):
                cmds = {k: v for k, v in (s.get("commands") or {}).items() if v}
                print(f"  · {s.get('language')}: {', '.join(f'{k}={v}' for k, v in cmds.items()) or 'команды не найдены'}")
            if prof.get("undetermined"):
                print(f"  ⚠ не определено: {', '.join(prof['undetermined'])}")
        return 0

    if intent == "status":
        from ai_ops_kit.lifecycle import active_work
        awp = child_root / ".ai" / "runtime" / "active-work.yaml"
        if not awp.is_file():
            print("STATUS: активной работы нет (нет .ai/runtime/active-work.yaml)")
            return 0
        return active_work.list_cmd(awp, as_json=js)

    if intent == "next":
        # Четыре вопроса: где мы, что идёт сейчас, что блокирует, что взять следующим.
        from ai_ops_kit.planning import next_work
        from ai_ops_kit.planning import delivery_plan as _plan
        from ai_ops_kit.planning import contours as _contours
        try:
            rep = next_work.compute(child_root, budget_left=getattr(a, "budget", None))
        except (_plan.PlanCorrupt, _contours.ModelCorrupt) as e:
            print(f"ОШИБКА: {e}")
            return 1
        if js:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        else:
            # v3.35 Human Communication Layer: по умолчанию говорим смыслом, а не внутренним
            # состоянием. Разбор по четырём вопросам доступен на technical/debug и по --json.
            from ai_ops_kit.ui import presenter
            aud = presenter.audience_from_config(child_root)
            print(presenter.render(presenter.from_next_work(rep), audience=aud))
            # Ошибки плана и направления печатаются ВСЕГДА: «показать по запросу» относится к
            # техническим деталям исправного прогона, а не к дефекту, который блокирует ответ.
            for _e in (rep.get("plan_errors") or []):
                print(f"  ✗ план: {_e}")
            for _e in (rep.get("roadmap") or {}).get("errors") or []:
                print(f"  ✗ направление: {_e}")
            if aud != "product":
                print()
                print(next_work.render(rep))
        # Код возврата — ГОТОВНОСТЬ ОТВЕТИТЬ, а не наличие работы: без плана и с битым roadmap
        # ответ «что взять следующим» недостоверен, и молчаливый ноль это скрывал бы.
        return 0 if (rep.get("plan_present") and not rep.get("plan_errors")
                     and not rep["roadmap"]["errors"]) else 1

    if intent == "model":
        # DISCOVER -> CLASSIFY -> RECONSTRUCT -> AUDIT -> ASK. Ничего не пишет: онбординг сначала
        # ПОНИМАЕТ репозиторий и только потом предлагает достройку.
        from ai_ops_kit.planning import repo_audit
        from ai_ops_kit.planning import contours as _contours
        try:
            rep = repo_audit.run(child_root)
        except _contours.ModelCorrupt as e:
            print(f"ОШИБКА: {e}")
            return 1
        if js:
            print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
        else:
            from ai_ops_kit.ui import presenter
            aud = presenter.audience_from_config(child_root)
            print(presenter.render(presenter.from_repository_understanding(rep), audience=aud))
            if aud != "product":
                print()
                print(repo_audit.render(rep))
            for q in rep["ask"]["questions"]:
                mark = "⚠" if q["blocks_work"] else "·"
                print(f"  {mark} {q['ask']}")
                if q["proposal"]:
                    print(f"      предполагаю: {q['proposal']['value']} — подтвердить?")
        return 0

    if intent == "health":
        from ai_ops_kit.intelligence import product_health
        cand = [child_root / "product" / "product-health.yaml",
                child_root / ".ai" / "product-health.yaml",
                child_root / "product-health.yaml"]
        src = next((p for p in cand if p.is_file()), None)
        if not src:
            print("HEALTH: нет входных метрик (ожидается product/product-health.yaml) — "
                  "честно: без данных score не считается")
            return 1
        report = product_health.compute(yaml.safe_load(src.read_text(encoding="utf-8")))
        if js:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            hs = report["health_score"]
            print(f"HEALTH: score {hs['value']} ({hs['band']}) · источник {src.relative_to(child_root)}")
        return 0

    if intent == "new":
        from ai_ops_kit.lifecycle import workitem
        from ai_ops_kit.gates import spec_levels
        from ai_ops_kit.engine import run_plan
        if not signals.get("task_type"):
            signals["task_type"] = run_plan.build_plan(dict(signals, task_text=task or ""))["base_workflow"]
        wid = _wid_for(task, signals, a.feature)
        workitem.start(str(child_root / "features"), wid, task or wid,
                       task_type=signals.get("task_type"), risk=signals.get("risk"))
        # v3.35: `affects` засевается ПОДСКАЗКОЙ по типу работы, а не выводится из diff. Разница
        # принципиальная: заявление автора и факт изменения — два независимых источника, и сверять
        # их имеет смысл только пока они независимы. Автозаполнение из diff сделало бы находку
        # `undeclared_change` невозможной, то есть выключило бы гейт, оставив его зелёным.
        # Прежде поле оставалось `{}` навсегда, а `suggest_affects` не вызывался нигде (ревью 3.35).
        _seed_workitem_affects(child_root, wid, signals.get("task_type"))
        sp, created = spec_levels.create_spec(child_root, wid, signals)
        if js:
            print(json.dumps({"workitem_id": wid, "workitem": f"features/{wid}/workitem.yaml",
                              "spec": str(sp), "spec_created": created}, ensure_ascii=False, indent=2))
        else:
            print(f"NEW: каркас фичи '{wid}' · features/{wid}/workitem.yaml + spec.yaml "
                  f"({'создан' if created else 'уже был'})")
            print(f"  далее: ai-ops specify \"{task or '<задача>'}\" {child_root} --feature {wid}")
        return 0

    if intent == "plan":
        from ai_ops_kit.engine import run_plan
        from ai_ops_kit.context import context_compiler
        from ai_ops_kit.gates import spec_levels
        from ai_ops_kit.engine import atomic_planner
        if not signals.get("task_type"):
            signals["task_type"] = run_plan.build_plan(dict(signals, task_text=task or ""))["base_workflow"]
        plan = run_plan.build_plan(dict(signals, task_text=task or ""), workitem_id=a.feature)
        wid = plan["workitem_id"]
        fdir = child_root / "features" / wid
        fdir.mkdir(parents=True, exist_ok=True)
        (fdir / "run-plan.yaml").write_text(yaml.safe_dump(plan, allow_unicode=True, sort_keys=False), encoding="utf-8")
        bundle = None
        try:
            bundle = context_compiler.compile_bundle(signals, child_root, plan=plan)
            (fdir / "context-bundle.yaml").write_text(
                yaml.safe_dump(bundle, allow_unicode=True, sort_keys=False), encoding="utf-8")
        except Exception as _ce:  # noqa: BLE001 — план не должен рушиться из-за контекста...
            # ...но деградация обязана быть видна: без бандла оценка пакета уходит на дефолты,
            # а context-bundle.yaml не пишется — молча это выглядит как обычный план.
            bundle = None
            print(f"  ⚠ контекст не собран ({type(_ce).__name__}: {_ce}) — оценка пакета по "
                  f"умолчаниям, context-bundle.yaml не записан")
        cov = spec_levels.assess_from_artifacts(signals, child_root, wid)
        (fdir / "spec-coverage.yaml").write_text(yaml.safe_dump(cov, allow_unicode=True, sort_keys=False), encoding="utf-8")
        wp = atomic_planner.decompose(signals, wid=wid, child_root=child_root, bundle=bundle)
        (fdir / "work-package.yaml").write_text(yaml.safe_dump(wp, allow_unicode=True, sort_keys=False), encoding="utf-8")
        if js:
            print(json.dumps({"workitem_id": wid, "plan": f"features/{wid}/run-plan.yaml",
                              "spec_level": cov["level_name"], "should_decompose": wp["should_decompose"],
                              "work_packages": len(wp["work_packages"])}, ensure_ascii=False, indent=2))
        else:
            print(f"PLAN: '{wid}' · workflow {plan['base_workflow']} · спека {cov['level_name']} · "
                  f"пакетов {len(wp['work_packages']) or 'атомарно'}")
            print(f"  артефакты в features/{wid}/ (run-plan, context-bundle, spec-coverage, work-package) — код НЕ менялся")
        return 0

    if intent == "review":
        from ai_ops_kit.delivery import review_branch
        from ai_ops_kit.engine import run_plan
        wid = a.feature or _wid_for(task, signals, a.feature)
        # реальный ревьюер — отдельный провайдер (writer ≠ judge); mock не выносит вердикт (needs-reviewer)
        rev_prop = None
        prov = getattr(a, "provider", "mock") or "mock"
        if prov != "mock":
            from ai_ops_kit.providers import orchestrator
            rev_prop = orchestrator.make_provider(prov, getattr(a, "model", None))
        rep = review_branch.review(child_root, wid, reviewer_proposer=rev_prop, base=a.base)
        if js:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        else:
            rmerge = (rep.get("readiness") or {}).get("ready_for_merge")
            print(f"REVIEW {wid}: verdict={rep['verdict']} · ready_for_merge={rmerge} · ревьюируемых гейтов "
                  f"{len(rep.get('reviewable') or [])} · изменено файлов {len(rep.get('changed_files') or [])}")
            for rv in rep.get("reviews") or []:
                print(f"  · {rv['gate']}: {rv.get('status') or 'invalid'}")
            if rep.get("evidence_path"):
                print(f"  evidence: {rep['evidence_path']}")
            if rep.get("note"):
                print(f"  {rep['note']}")
        # v2.121 (P1.3): exit code = готовность к merge (needs-reviewer/needs-changes -> non-zero)
        return 0 if (rep.get("readiness") or {}).get("ready_for_merge") else 1

    if intent == "discuss":
        from ai_ops_kit.engine import run_plan
        wid = _wid_for(task, signals, a.feature)
        fdir = child_root / "features" / wid
        fdir.mkdir(parents=True, exist_ok=True)
        draft = fdir / "discovery-draft.md"
        if not draft.is_file():
            draft.write_text(
                f"# Discovery: {task or wid}\n\n"
                "## Проблема\n_TODO: какую боль решаем, чьи слова_\n\n"
                "## Пользователи и JTBD\n_TODO_\n\n"
                "## Гипотезы\n_TODO: если … то … потому что …_\n\n"
                "## Как измерим\n_TODO: сигнал успеха_\n\n"
                "## Открытые вопросы / риски\n_TODO_\n\n"
                "## Что НЕ делаем (scope out)\n_TODO_\n", encoding="utf-8")
            created = True
        else:
            created = False
        if js:
            print(json.dumps({"workitem_id": wid, "draft": str(draft), "created": created},
                             ensure_ascii=False, indent=2))
        else:
            print(f"DISCUSS: {'создан' if created else 'уже есть'} черновик discovery {draft.relative_to(child_root)}")
            print("  заполни разделы, затем: ai-ops specify …")
        return 0

    if intent == "advise":
        from ai_ops_kit.engops import engineering_advisor
        result = engineering_advisor.advise(str(child_root), task_type=signals.get("task_type"))
        if js:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"ENGINEERING ADVISOR: {result['summary']}")
            print(f"Repository: {result['repository']}")
            if result.get("task_type"):
                print(f"Task type: {result['task_type']}")
            print()
            for r in result["recommendations"]:
                p = r.get("priority", 3)
                marker = "⚠" if p == 1 else "·" if p == 2 else " "
                print(f"  {marker} [{r.get('category')}] {r['advice']}")
                print(f"    (source: {r.get('source')})")
        return 0

    return None


def _seed_workitem_affects(child_root, wid, task_type):
    """Записать в WorkItem подсказку `affects` по типу работы (v3.35). Тихо ничего не делает, если
    модель недоступна: онбординг и создание фичи не обязаны падать из-за реестра контуров."""
    import yaml as _yaml
    try:
        from ai_ops_kit.planning import contours as _c
        model = _c.load_model()
    except Exception:                                  # noqa: BLE001 — реестр не обязан быть рядом
        return None
    wf = (task_type or "").strip()
    # Тип работы плана (`engineering`/`visual`/…) и класс задачи движка (`ENGINEERING`/`VISUAL`/…)
    # — разные словари; сопоставление объявлено здесь, а не угадывается по регистру.
    by_workflow = {"QUICK": "engineering", "ENGINEERING": "engineering", "PRODUCT": "product",
                   "RESEARCH": "research", "VISUAL": "visual", "AI_FEATURE": "engineering",
                   "CRITICAL": "security", "ANALYTICS": "analytics"}
    suggested = _c.suggest_affects(model, by_workflow.get(wf.upper(), ""))
    if not suggested:
        return None
    wp = Path(child_root) / "features" / str(wid) / "workitem.yaml"
    if not wp.is_file():
        return None
    try:
        data = _yaml.safe_load(wp.read_text(encoding="utf-8")) or {}
    except _yaml.YAMLError:
        return None
    if data.get("affects"):
        return None                                    # объявленное человеком не перезаписываем
    data["affects"] = suggested
    wp.write_text(_yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return suggested


def _session_guard_before_start(child_root, task, signals, feature=None):
    """v3.22 Culture Runtime Integration: session guard ДО старта задачи.
    1. snapshot — текущее состояние сессии
    2. relation по факту — session_boundary.classify (не жёсткое значение)
    3. recommend — если new_session/compact, предупредить (advise, не block)
    4. delegation — если большая разведка, рекомендовать сабагент
    Выводит рекомендации пользователю, не блокирует прогон."""
    try:
        from ai_ops_kit.engops import session_telemetry
        from ai_ops_kit.engops import session_guardrails
        from ai_ops_kit.engops import session_boundary
        from ai_ops_kit.engops import delegation_advisor
        # 1. snapshot
        snap = session_telemetry.snapshot(str(child_root), workitem_id=feature)
        ctx = snap.get("context_current")
        ctx_txt = f"{ctx/1000:.0f}k" if ctx else "н/д"
        # 2. relation по факту
        current_wid = snap.get("workitem_id")
        relation_cls, reason = session_boundary.classify(
            current_workitem=current_wid, new_task=task or "", new_workitem=feature)
        relation = session_boundary.to_relation(relation_cls)
        # 3. recommend
        rec = session_guardrails.recommend(snap, next_relation=relation, next_task=task, task_done=False)
        outcome = rec.get("outcome")
        if outcome in ("new_session", "compact"):
            print(f"⚠ SESSION GUARD: {outcome} — {rec.get('reason', '')}")
            print(f"  контекст: {ctx_txt} [{snap.get('context_status')}]")
            print(f"  команда: {rec.get('command', '')}")
        # 4. delegation
        del_signals = {"task_text": task or "", "files_count": 0}
        del_recs = delegation_advisor.advise(del_signals)
        if del_recs:
            print(f"⚠ DELEGATION: {len(del_recs)} рекомендация(ий)")
            for r in del_recs[:2]:
                print(f"  · {r.get('trigger')}: {r.get('reason', '')[:80]}")
    except Exception as e:  # noqa: BLE001
        # session guard — advise, не block; если что-то сломалось, продолжаем
        print(f"⚠ session guard: {e}")


def main(argv):
    ap = argparse.ArgumentParser(prog="ai_ops_cli.py")
    ap.add_argument("intent", choices=list(INTENTS) + ["preview"])
    ap.add_argument("rest", nargs="*")
    ap.add_argument("--signals", default="{}")
    ap.add_argument("--feature")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="resume: продолжить даже при нужной ревалидации (осознанно)")
    ap.add_argument("--base", default=None, help="resume/review: base-ветка (по умолчанию auto: upstream/remote-default/текущая)")
    # v3.28.x (P0-1): дефолта `mock` больше НЕТ. Для `run --execute`/`do` провайдера выбирает резолв
    # (.ai-ops.yaml + ключ в env -> claude в PATH -> mock с громким предупреждением); явный --provider
    # (в т.ч. mock) всегда побеждает. Для `review`/`resume` остаётся прежний офлайн-дефолт mock.
    ap.add_argument("--provider", default=None,
                    help="провайдер (mock|anthropic|openai|claude-cli|qwen|deepseek|kimi). "
                         "run --execute без флага — авторезолв (AI_OPS_PROVIDER_AUTORESOLVE=0 выключает); "
                         "review: провайдер ревьюера (не mock -> живой вердикт)")
    ap.add_argument("--model", help="review: модель ревьюера")
    ap.add_argument("--sequential", action="store_true",
                    help="run: неатомарную задачу исполнять по WorkPackages последовательно (v3.1)")
    ap.add_argument("--open-pr", action="store_true",
                    help="run: открыть draft PR по результату (нужен GITHUB_TOKEN)")
    ap.add_argument("--max-steps", type=int, default=40, help="run: потолок шагов tool-loop")
    ap.add_argument("--resume-from", help="run --sequential: продолжить с конкретного WorkPackage (id); "
                                          "пакеты до него берутся из снимков прошлого прогона")
    ap.add_argument("--retry-package", help="run --sequential: ДОВЕРЕННЫЙ retry заблокированного пакета (id) "
                                            "— архивирует проваленную попытку, восстанавливает ветку на "
                                            "checkpoint предшественника и продолжает (без ручного git reset)")
    ap.add_argument("--replan", action="store_true",
                    help="resume: осознанно сменить классификацию/policy (replan c ревалидацией)")
    ap.add_argument("--budget", type=int, default=None,
                    help="next: остаток бюджета в токенах (нет значения -> unknown, НЕ ноль)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    intent = a.intent
    rest = list(a.rest)
    preview_mode = intent == "preview"
    if preview_mode:
        intent = rest.pop(0) if rest else "run"
    # разбор [задача] child_root
    needs_task = INTENTS.get(intent, ("", "", False))[2]
    task, child_root = None, "."
    if needs_task:
        task = rest.pop(0) if rest else ""
    child_root = rest.pop(0) if rest else "."
    signals = json.loads(a.signals)
    if a.feature:
        signals["feature"] = a.feature

    if intent == "resume":
        from ai_ops_kit.engine import ai_ops_run
        # v2.109 Real Resume: --execute реально продолжает прогон (не рестарт); без флага — preflight.
        argv2 = ["resume", child_root, a.feature or (task or ""), "--base", a.base]
        # v3.0-rc2 (P0.1): intent CLI ПРОВОДИТ provider/model/signals в низкоуровневый resume — иначе
        # `ai-ops resume --provider X --model Y` молча уходил в mock (политика/провайдер терялись).
        argv2 += ["--provider", a.provider or "mock", "--signals", a.signals]
        if a.model:
            argv2 += ["--model", a.model]
        if getattr(a, "replan", False):
            argv2.append("--replan")   # v3.0-rc4 (P0.1): осознанная смена policy при продолжении
        if a.execute:
            argv2.append("--execute")
        if a.force:
            argv2.append("--force")
        if a.json:
            argv2.append("--json")
        return ai_ops_run.main(argv2)

    # v2.110 Real Spec-First: `specify` РЕАЛЬНО создаёт spec-артефакт нужной глубины (не только превью).
    if intent == "specify":
        from ai_ops_kit.gates import spec_levels
        from ai_ops_kit.engine import run_plan
        if not signals.get("task_type"):
            signals["task_type"] = run_plan.build_plan(dict(signals, task_text=task or ""))["base_workflow"]
        wid = a.feature or run_plan.build_plan(dict(signals, task_text=task or ""))["workitem_id"]
        sp, created = spec_levels.create_spec(Path(child_root), wid, signals, overwrite=a.force)
        cov = spec_levels.assess_from_artifacts(signals, Path(child_root), wid)
        if a.json:
            print(json.dumps({"path": str(sp), "created": created, "coverage": cov},
                             ensure_ascii=False, indent=2))
        else:
            print(f"SPECIFY: {'создан' if created else 'уже существует'} {sp}")
            print(f"  уровень {cov['level_name']} · обязательных разделов {len(cov['sections'])} · "
                  f"заполнить: {len(cov['blocking_missing'])}")
            print(f"  заполни разделы в {sp.relative_to(Path(child_root)) if str(sp).startswith(child_root) else sp}, "
                  f"затем: ai-ops run \"{task or '<задача>'}\" {child_root} --feature {wid} --execute")
        return 0

    # v2.112 Intent UX: настоящие действия (не только превью). preview_mode -> всегда показать превью.
    # v2.116: `review` тоже настоящий intent — read-only ревью действующей ветки.
    if not preview_mode and intent in ("onboard", "status", "health", "plan", "new", "discuss",
                                       "review", "advise", "next", "model"):
        rc = _run_intent(intent, task, Path(child_root), signals, a)
        if rc is not None:
            return rc

    pv = build_preview(intent, task, Path(child_root), signals)
    # v3.28.x (F-015): роутер классифицировал тип задачи ВНУТРИ build_preview — но там он работает
    # с копией signals, и наружу классификация не выходила. Движок получал сигналы без task_type,
    # терял evidence `classified_type` и валил блокирующий intake_completeness у пользователя,
    # который всё указал правильно. Материализуем решение роутера в сигналы прогона.
    _understood_type = (pv.get("understood") or {}).get("task_type")
    if _understood_type and not signals.get("task_type"):
        signals["task_type"] = _understood_type
    if a.json:
        print(json.dumps(pv, ensure_ascii=False, indent=2))
    else:
        _print_preview(pv)

    # только `run --execute` и `do` реально запускают движок; остальное — превью/делегация
    # v3.22: `do` — alias для `run --execute` с авторазрешением блокировщиков (review_fix_attempts, author, open_pr)
    if (intent == "run" and a.execute) or intent == "do":
        from ai_ops_kit.engine import ai_ops_run
        from ai_ops_kit.engine import pipeline_helpers
        # v3.28.x (F-015, находка живой квалификации): intake-сигналы проверяем ДО старта.
        # `size` требует блокирующий гейт intake_completeness, вывести его из репозитория нечем,
        # и раньше пользователь узнавал о пропаже только из вердикта ПОСЛЕ прогона — в раунде C
        # так сгорело 6 прогонов из 6, самый долгий 36 минут. Fail-closed сохраняется (exit 2,
        # тот же код, что у незакрытого гейта), но платится секундами, а не часом работы модели.
        _missing = pipeline_helpers.missing_intake_signals(signals)
        if _missing:
            _hint = pipeline_helpers.intake_signals_hint(_missing, task)
            if a.json:
                print(json.dumps({"kind": "intake-incomplete", "exit": 2,
                                  "missing": _missing, "hint": _hint}, ensure_ascii=False, indent=2))
            else:
                for _ln in _hint:
                    print(_ln)
            return 2
        flags = pv["will_do"]["auto_flags"]
        # v3.28.x (P0-1): провайдер выбирается ОДИН раз здесь и дальше идёт под своим именем во все
        # ветки (sequential/обычная) — иначе автовыбор терялся бы по дороге, как уже было в v2.120/v3.0-rc2.
        _pres = ai_ops_run.resolve_provider_for_run(a.provider, Path(child_root), execute=True,
                                                    quiet=a.json)
        provider = _pres["provider"]
        # v3.22: `do` форсирует флаги автономного прогона
        if intent == "do":
            flags["author"] = True
            flags["review"] = True
            a.open_pr = True
        # v3.1/v2.120: --sequential — неатомарную задачу исполнить по WorkPackages (пакет за пакетом).
        # v2.120: sequential НАСЛЕДУЕТ провайдера/модель/sandbox/install/baseline/open-pr/budget обычного
        # пути — иначе тихая потеря containment и live-провайдера (дефект аудита P0.2).
        if a.sequential:
            from ai_ops_kit.engine import atomic_planner
            from ai_ops_kit.engine import workpackage_executor
            from ai_ops_kit.engine import tool_loop
            from ai_ops_kit.providers import orchestrator
            wid = a.feature or _wid_for(task, signals, a.feature)
            wp = atomic_planner.decompose(signals, wid=wid, child_root=Path(child_root))
            # v3.0-rc13 (P1): доверенный retry — архив попытки + reset на checkpoint предшественника,
            # затем продолжаем как resume_from (без ручного git reset у пользователя).
            resume_from = a.resume_from
            if a.retry_package:
                rt = workpackage_executor.retry_package(Path(child_root), wid, a.retry_package)
                if not rt.get("ok"):
                    print(f"RETRY ОТКАЗ: {rt.get('error')}")
                    return 2
                print(f"RETRY {a.retry_package}: ветка восстановлена на checkpoint "
                      f"{(rt.get('checkpoint') or '')[:12]} (предшественник {rt.get('predecessor') or 'база'}); "
                      f"попытка заархивирована -> {rt.get('archived_attempt') or '—'}")
                resume_from = a.retry_package
            if wp["should_decompose"] and wp["work_packages"]:
                base_prop = tool_loop.make_model_proposer(orchestrator.make_provider(provider, a.model))
                auth = orchestrator.make_provider(provider, a.model) if flags["author"] and provider != "mock" else None
                rev = orchestrator.make_provider(provider, a.model) if flags["review"] and provider != "mock" else None
                print(f"— исполняю по WorkPackages: {len(wp['work_packages'])} пакет(ов) —")
                seq = workpackage_executor.execute_sequence(
                    task, signals, Path(child_root), wp["work_packages"], lambda pkg: base_prop,
                    feature=wid, base=a.base, provider_name=provider, model=a.model,
                    author=flags["author"], author_proposer=auth,
                    review=flags["review"], reviewer_proposer=rev, baseline_diff=flags["baseline_diff"],
                    sandbox=flags["sandbox"], install_deps=True, open_pr=a.open_pr, max_steps=a.max_steps,
                    # v2.123 (P0.3): package write_scope РЕАЛЬНО протянут — брокер ограничит пакет его каталогом
                    write_scope_for=lambda pkg: pkg.get("write_scope"),
                    resume_from=resume_from)   # v2.124: resume; v3.0-rc13: retry -> resume_from=retry-package
                _dlv = seq.get("delivery") or {}
                print(f"SEQUENCE {wid}: executed_all={seq['executed_all']} · ready_all={seq['ready_all']} · "
                      f"пакетов {seq['total']} · остановлен_на={seq['stopped_at'] or '—'}"
                      + (f" · доставка={_dlv.get('status')}" if _dlv.get('requested') else ""))
                for p in seq["packages"]:
                    print(f"  [{p['id']}] {p['status']} · sha={(p.get('sha') or '')[:12] or '—'} · ready={p.get('ready')}")
                # v2.120/2.124 exit-код: 0 — ready_all И (если запрошен PR) он реально открыт;
                # 1 — исполнено, но не готово / доставка не удалась; 2 — цепочка блокирована/ошибка.
                if seq["ready_all"]:
                    if _dlv.get("requested") and _dlv.get("status") not in ("opened", "updated"):
                        return 1   # готово, но draft PR не открыт -> не полный успех
                    return 0
                return 1 if seq["executed_all"] else 2
            print("— задача атомарна: последовательное исполнение не требуется, обычный прогон —")
        print("— запускаю —")
        # v3.22: session guard ДО старта — snapshot + relation по факту + delegation
        _session_guard_before_start(Path(child_root), task, signals, a.feature)
        # v2.120: канонический вход ПРОВОДИТ провайдера/модель/base/open-pr/max-steps/require-fix в движок
        # (дефект аудита P0.1: раньше уходило в mock и без пути до draft PR).
        # v3.22: `do` добавляет review_fix_attempts=2 (авторазрешение блокировщиков)
        review_fix = 2 if intent == "do" else getattr(a, "review_fix_attempts", 0)
        rep = ai_ops_run.run(task, signals, Path(child_root), engine=flags["engine"],
                             feature=a.feature, execute=True, sandbox=flags["sandbox"],
                             baseline_diff=flags["baseline_diff"], review=flags["review"],
                             author=flags["author"], provider_name=provider, model=a.model,
                             base=a.base, open_pr=a.open_pr, max_steps=a.max_steps,
                             require_fix=flags.get("require_fix", False),
                             review_fix_attempts=review_fix,
                             provider_resolution={k: _pres.get(k) for k in
                                                  ("provider", "source", "reason", "warning")})
        ai_ops_run.print_human(rep)
        return ai_ops_run.exit_code(rep)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
