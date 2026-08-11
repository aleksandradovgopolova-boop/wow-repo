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
    # Третье поле — нужен ли текст задачи. `new` и `resume` его ИСПОЛЬЗУЮТ (заголовок работы,
    # выбор фичи), поэтому объявлены честно: расхождение объявления с использованием и было тем,
    # из-за чего `new` принимал текст задачи за каталог репозитория.
    "new":     ("создать новую фичу/каркас", "scaffold", True),
    "onboard": ("определить стек и команды репозитория", "onboard", False),
    "discuss": ("обсудить идею до спецификации (discovery)", "discuss", True),
    "specify": ("построить спецификацию нужной глубины", "specify", True),
    "plan":    ("построить RunPlan + контекст + оценку пакета (без правок)", "plan", True),
    "run":     ("выполнить задачу движком (авто-подбор стадий)", "run", True),
    "do":      ("автономный прогон: run --execute + авторазрешение блокировщиков", "do", True),
    "advise":  ("инженерный совет: окружения, delivery plan, альтернативы (без исполнения)", "advise", True),
    "resume":  ("продолжить прерванную работу по фиче", "resume", True),
    "review":  ("независимый ревью произведённого", "review", True),
    "status":  ("статус активной работы", "status", False),
    "health":  ("здоровье продукта", "health", False),
    # v3.35 Product Operating Model: план продукта и его связность.
    "next":    ("что взять следующим: где мы, что идёт, что блокирует, что можно параллельно", "next", False),
    "model":   ("модель продуктового репозитория: классификация, контуры, пробелы, вопросы", "model", False),
    # v3.35.2 (тир 4): BOOTSTRAP существовал СТРОКОЙ в реестре — кит не создавал ни направления, ни
    # плана, и владелец после онбординга оставался с пониманием и без работы. Сухой прогон по
    # умолчанию: запись в чужой репозиторий он обязан увидеть до того, как она произошла.
    "bootstrap": ("создать первое направление и план из фактов репозитория (--apply — записать)",
                  "bootstrap", False),
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

    # ЭТУ СТРОКУ ЧИТАЕТ ЧЕЛОВЕК. Прежде здесь стояли внутренние имена артефактов — `RunPlan +
    # оценка без изменений кода`, `RepositoryProfile (стек/команды)`, `Product Health Score`, — и
    # они выходили наружу через превью, то есть через самое частое сообщение кита. Проверка на
    # реалистичном дереве показала это первой же строкой ответа.
    # Формулировка — от первого лица и глаголом: эту строку человек читает как ответ на «что ты
    # сейчас сделаешь». Существительные не годятся: «Собираюсь: чем проект написан» — не фраза.
    expected = ("проверю изменение и открою черновой pull request, если все проверки пройдут"
                if intent == "run"
                else {"plan": "построю план работы и оценю объём; код при этом не меняю",
                      "specify": "напишу заготовку описания задачи нужной глубины",
                      "review": "проведу независимую проверку сделанного",
                      "onboard": "разберусь, чем проект написан и чем он проверяется",
                      "status": "скажу, что идёт прямо сейчас",
                      "health": "оценю состояние продукта",
                      "next": "скажу, где мы, что идёт, что мешает и что взять следующим",
                      "model": "разберусь в проекте: что за продукт, что я знаю, чего не знаю",
                      "discuss": "заведу черновик обсуждения: какую боль решаем и как поймём, "
                                 "что помогло",
                      "new": "заведу место для новой работы",
                      "resume": "продолжу с последнего подтверждённого шага"}.get(
                          intent, "выполню намерение"))

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


def _say(child_root, translator, *args, **kwargs):
    """Внутренний отчёт -> человеческий текст. ЕДИНСТВЕННЫЙ путь наружу для команд намерений.

    Прежде каждая команда печатала своё: `ONBOARD: стек python`, `SPECIFY: создан`, `REVIEW wid:
    verdict=…`. Правило «наружу выходит смысл» держалось на памяти автора команды, и из двенадцати
    команд его соблюдали три. Здесь оно держится на том, что другого способа напечатать нет.

    Имя переводчика — строка: так `cli` не тянет `ui` на импорте (слои). Опечатка в имени падает
    громко (`AttributeError`), а не печатает пустоту, и её же ловит тест разводки.
    """
    from ai_ops_kit.ui import presenter
    fn = getattr(presenter, translator)
    print(presenter.render(fn(*args, **kwargs),
                           audience=presenter.audience_from_config(child_root)))


def _audience(child_root):
    """Уровень детализации для этого репозитория. Отдельно — там, где нужен и внутренний вывод."""
    from ai_ops_kit.ui import presenter
    return presenter.audience_from_config(child_root)


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
            _say(child_root, "from_onboarding_profile", prof, str(out.relative_to(child_root)))
        return 0

    if intent == "status":
        from ai_ops_kit.lifecycle import active_work
        from ai_ops_kit.ui import presenter
        awp = child_root / ".ai" / "runtime" / "active-work.yaml"
        data = {"active": []}
        if awp.is_file():
            try:
                data = active_work.load(awp)
            except active_work.ActiveWorkCorrupt as e:
                # Битый реестр — не «работы нет»: координация сессий недостоверна (инвариант 3.0.12).
                print(presenter.render(presenter.message(
                    status="blocked",
                    summary="Не могу сказать, что идёт прямо сейчас: запись об идущих работах "
                            "повреждена.",
                    why_it_matters="Пока это так, я не знаю, не перепишет ли новая работа то, что "
                                   "уже правит другая сессия.",
                    next_steps=["восстановить запись и повторить"],
                    technical={"ошибка": str(e)}),
                    audience=presenter.audience_from_config(child_root)))
                return 1
        if js:
            return active_work.list_cmd(awp, as_json=True) if awp.is_file() else 0
        aud = presenter.audience_from_config(child_root)
        print(presenter.render(presenter.from_active_work(data), audience=aud))
        return 0

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
        # DISCOVER -> CLASSIFY -> RECONSTRUCT -> AUDIT -> ASK. Понимание репозитория: артефактов
        # проекта команда не создаёт и ничего не перестраивает.
        #
        # ОДИН ФАЙЛ ОНА ВСЁ-ТАКИ ПИШЕТ, и объявить это обязательно: `.ai/project/
        # onboarding-answers.yaml` — форма, в которую человек впишет ответы. Раньше здесь стояло
        # «ничего не пишет», а команда писала (это внёс фикс тупика с вопросами), и человек, позвав
        # `model` просто посмотреть состояние, находил в своём `git status` незнакомый файл.
        # Заявление приведено к фактам, повторный вызов файл НЕ трогает, если текст тот же.
        from ai_ops_kit.planning import repo_audit
        from ai_ops_kit.planning import contours as _contours
        try:
            rep = repo_audit.run(child_root)
        except _contours.ModelCorrupt as e:
            print(f"ОШИБКА: {e}")
            return 1
        # ПОБОЧНЫЙ ЭФФЕКТ НЕ ЗАВИСИТ ОТ ФОРМАТА ВЫВОДА. Прежде форма ответов создавалась только в
        # человеческой ветке: `--json` того же намерения оставлял человека без места для ответа, то
        # есть одна команда вела себя двумя разными способами.
        answers_file = None
        if rep["ask"]["questions"]:
            answers_file = repo_audit.write_question_file(child_root, rep["ask"])
        if js:
            out = dict(rep)
            if answers_file:
                out["answers_file"] = str(answers_file)
            print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
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
            # ВОПРОСАМ НУЖНО МЕСТО. Прежде кит печатал их и завершался: куда отвечать — не сказано,
            # интерактива нет, человек в тупике на главном шаге первого сценария.
            if answers_file:
                try:
                    shown = answers_file.relative_to(Path(child_root))
                except ValueError:
                    shown = answers_file
                print(f"\n  Ответы впишите здесь: {shown}")
                print("  Потом запустите снова: ./ai-ops model — ответы станут подтверждёнными "
                      "фактами и больше не будут переспрашиваться.")
        return 0

    if intent == "bootstrap":
        # BOOTSTRAP: онбординг заканчивается работой, а не документацией. Пишет ТОЛЬКО с --apply и
        # ТОЛЬКО отсутствующее; заготовку кита заменяет (в ней нет фактов о продукте), настоящий
        # план — никогда.
        from ai_ops_kit.planning import product_bootstrap as _boot
        from ai_ops_kit.planning import contours as _contours
        from ai_ops_kit.planning import delivery_plan as _dp
        from ai_ops_kit.planning import repo_audit as _ra
        try:
            # Аудит — один раз на команду: сухой прогон и запись смотрят на ОДНИ факты, иначе между
            # «вот что создам» и «создал» могла бы оказаться разница, которую человек не просил.
            _und = _ra.run(child_root)
            boot = _boot.plan(child_root, _und)
        except (_contours.ModelCorrupt, _dp.PlanCorrupt) as e:
            print(f"ОШИБКА: {e}")
            return 1
        applied = bool(getattr(a, "apply", False))
        rep = _boot.apply(child_root, boot, _und) if applied else boot
        if js:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        else:
            _say(child_root, "from_bootstrap", rep, applied=applied)
            if not applied and rep["will_write"]:
                print(f"\n  Записать: ./ai-ops bootstrap --apply")
        return 1 if rep.get("error") else 0

    if intent == "health":
        from ai_ops_kit.intelligence import product_health
        cand = [child_root / "product" / "product-health.yaml",
                child_root / ".ai" / "product-health.yaml",
                child_root / "product-health.yaml"]
        src = next((p for p in cand if p.is_file()), None)
        if not src:
            from ai_ops_kit.ui import presenter
            aud = presenter.audience_from_config(child_root)
            print(presenter.render(presenter.from_product_health(None), audience=aud))
            return 1
        report = product_health.compute(yaml.safe_load(src.read_text(encoding="utf-8")))
        if js:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            from ai_ops_kit.ui import presenter
            aud = presenter.audience_from_config(child_root)
            print(presenter.render(presenter.from_product_health(report), audience=aud))
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
        # v3.35.1 (ревью перед квалификацией): засев `affects` ПО ТИПУ ЗАДАЧИ УБРАН. Кит записывал
        # `{engineering_quality_security: true}` всем шести инженерным типам, а `reconcile` читал это
        # как заявление АВТОРА — и на каждой обычной задаче выдавал major-находку «источник истины не
        # обновлён», потому что задача не трогает DevelopmentProcess.md. Кит ловил себя же.
        # Теперь `affects` берётся ТОЛЬКО из плана: если элемент с этим id объявлен в
        # `planning/plan.yaml`, его заявление переносится в WorkItem — это настоящее заявление
        # человека и настоящая связь уровней. Нет элемента — поле остаётся пустым, и гейт называет
        # затронутые контуры информацией, а не расхождением.
        _copy_affects_from_plan(child_root, wid)
        sp, created = spec_levels.create_spec(child_root, wid, signals)
        if js:
            print(json.dumps({"workitem_id": wid, "workitem": f"features/{wid}/workitem.yaml",
                              "spec": str(sp), "spec_created": created}, ensure_ascii=False, indent=2))
        else:
            _say(child_root, "from_new_feature", wid, task or wid, created,
                 f"./ai-ops specify \"{task or '<задача>'}\" --feature {wid}")
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
        bundle, ctx_error = None, None
        try:
            bundle = context_compiler.compile_bundle(signals, child_root, plan=plan)
            (fdir / "context-bundle.yaml").write_text(
                yaml.safe_dump(bundle, allow_unicode=True, sort_keys=False), encoding="utf-8")
        except Exception as _ce:  # noqa: BLE001 — план не должен рушиться из-за контекста...
            # ...но деградация обязана быть видна: без бандла оценка пакета уходит на дефолты,
            # а context-bundle.yaml не пишется — молча это выглядит как обычный план.
            bundle = None
            ctx_error = f"{type(_ce).__name__}: {_ce}"[:200]
        cov = spec_levels.assess_from_artifacts(signals, child_root, wid)
        (fdir / "spec-coverage.yaml").write_text(yaml.safe_dump(cov, allow_unicode=True, sort_keys=False), encoding="utf-8")
        wp = atomic_planner.decompose(signals, wid=wid, child_root=child_root, bundle=bundle)
        (fdir / "work-package.yaml").write_text(yaml.safe_dump(wp, allow_unicode=True, sort_keys=False), encoding="utf-8")
        if js:
            print(json.dumps({"workitem_id": wid, "plan": f"features/{wid}/run-plan.yaml",
                              "spec_level": cov["level_name"], "should_decompose": wp["should_decompose"],
                              "work_packages": len(wp["work_packages"]),
                              "context_error": ctx_error}, ensure_ascii=False, indent=2))
        else:
            _say(child_root, "from_plan_built", wid, plan["base_workflow"], cov["level_name"],
                 len(wp["work_packages"]), context_error=ctx_error)
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
            _say(child_root, "from_review", rep)
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
            _say(child_root, "from_discovery_draft", draft.relative_to(child_root), created)
        return 0

    if intent == "advise":
        from ai_ops_kit.engops import engineering_advisor
        result = engineering_advisor.advise(str(child_root), task_type=signals.get("task_type"))
        if js:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            _say(child_root, "from_advice", result)
        return 0

    return None


def _copy_affects_from_plan(child_root, wid):
    """Перенести `affects` из элемента плана с этим id в WorkItem. -> перенесённое или None.

    Это ЕДИНСТВЕННЫЙ законный источник `affects`: заявление человека в `planning/plan.yaml`. Кит не
    заявляет за автора — прежний засев по типу задачи выдумывал заявление и ловил на нём сам себя.
    Нет элемента плана с этим id — поле остаётся пустым, и это честно: заявления действительно не
    было. Тихо ничего не делает при недоступности плана: создание фичи не обязано падать из-за него.
    """
    import yaml as _yaml
    try:
        from ai_ops_kit.planning import delivery_plan as _dp
        plan = _dp.load(child_root)
    except Exception:                                  # noqa: BLE001 — план не обязан существовать
        return None
    if not plan:
        return None
    item = next((w for w in _dp.items(plan) if str(w.get("id")) == str(wid)), None)
    declared = (item or {}).get("affects") or {}
    if not declared:
        return None
    wp = Path(child_root) / "features" / str(wid) / "workitem.yaml"
    if not wp.is_file():
        return None
    try:
        data = _yaml.safe_load(wp.read_text(encoding="utf-8")) or {}
    except _yaml.YAMLError:
        return None
    if data.get("affects"):
        return None                                    # уже объявлено — не перезаписываем
    data["affects"] = dict(declared)
    data["affects_source"] = f"planning/plan.yaml -> {wid}"
    wp.write_text(_yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return declared


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
    ap.add_argument("--apply", action="store_true",
                    help="bootstrap: РЕАЛЬНО создать отсутствующие направление и план "
                         "(без флага — сухой прогон: показать, что будет создано)")
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
    # КАТАЛОГ РЕПОЗИТОРИЯ — ПОСЛЕДНИЙ АРГУМЕНТ (его подставляет `./ai-ops`), текст задачи — перед
    # ним. Прежде разбор шёл слева и опирался на флаг `needs_task`: у `new` он стоял `False`, и
    # `./ai-ops new "добавить экспорт в CSV"` объявлял каталогом репозитория… текст задачи. Каркас
    # создавался в `./добавить экспорт в CSV/features/wi-unknown/`, код возврата 0 — кит молча
    # работал не в том репозитории и сообщал об успехе. Нашлось тестом разводки команд.
    if rest and Path(rest[-1]).is_dir():
        child_root = rest.pop()
    if rest:
        task = rest.pop(0)
    elif needs_task:
        task = ""
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
            try:
                shown = sp.relative_to(Path(child_root))
            except ValueError:
                shown = sp
            _say(Path(child_root), "from_specification", shown, created, cov["level_name"],
                 cov["sections"], cov["blocking_missing"],
                 f"./ai-ops run \"{task or '<задача>'}\" --feature {wid} --execute")
        return 0

    # v2.112 Intent UX: настоящие действия (не только превью). preview_mode -> всегда показать превью.
    # v2.116: `review` тоже настоящий intent — read-only ревью действующей ветки.
    if not preview_mode and intent in ("onboard", "status", "health", "plan", "new", "discuss",
                                       "review", "advise", "next", "model", "bootstrap"):
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
        # Смысл — всегда; внутренний разбор превью (стадии, флаги, бюджет) — на technical/debug,
        # как у `next` и `model`. Разбор не выброшен: без него не отладить неверный подбор режима.
        _say(Path(child_root), "from_execution_preview", pv)
        if _audience(Path(child_root)) != "product":
            print()
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
                # Готовая команда с ответом обязана дойти до человека на любом уровне детализации,
                # иначе сообщение назовёт препятствие и не даст его убрать.
                _say(Path(child_root), "from_intake_gap", _missing,
                     pipeline_helpers.intake_signals_command(_missing))
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
