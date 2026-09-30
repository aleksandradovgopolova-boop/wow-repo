"""Проб-свободные intent-хендлеры продукта и поставки, вынесенные из `ai_ops_cli_intents`.

Здесь живут обработчики намерений вокруг продукта, поставки и бэклога: `build_preview`
(execution preview), `_run_backlog` (Backlog Intelligence через CLI), `products` / `delivery`
/ `model` / `contract` / `inspect` / `plan` / `session`, плюс общие хелперы
`_product_health_report` / `_product_risks`. Ни одна строка не несёт мутационных проб
(охраняемые строки остаются в `ai_ops_cli.py`).

Регистрация обработчиков в общий реестр интентов делается в `ai_ops_cli` (декоратор `_intent`
и `_INTENT_HANDLERS` живут там). Модуль НЕ импортирует `ai_ops_cli` на верхнем уровне —
единичные обращения к его хелперам идут ленивым импортом внутри функции, поэтому цикла нет.
"""
from __future__ import annotations

import json
from pathlib import Path

def build_preview(intent, task, child_root, signals):
    """Execution preview: что понято, что будет сделано, какие данные, какие approvals, результат."""
    from ai_ops_kit.engine import run_plan
    from ai_ops_kit.context import context_compiler
    from ai_ops_kit.gates import spec_levels
    from ai_ops_kit.engine import atomic_planner
    from ai_ops_kit.cli.ai_ops_cli import resolve_flags   # проб-несущий, живёт в ai_ops_cli
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
    # Формулировка — от первого лица и глаголом (ответ на «что ты сейчас сделаешь»), не существительным.
    # `run` неоднороден: не каждый workflow кончается pull request'ом. Для RESEARCH итог — не код,
    # а доказательства и пакет для решения, иначе общий текст поставки был бы утечкой чужого сценария.
    if intent == "run":
        expected = ("запущу исследование: превращу вопрос в доказательства и пакет для решения "
                    "(материалы соберу в .research/)" if plan["base_workflow"] == "RESEARCH"
                    else "проверю изменение и открою черновой pull request, если все проверки пройдут")
    else:
        expected = {"plan": "построю план работы и оценю объём; код при этом не меняю",
                      "specify": "напишу заготовку описания задачи нужной глубины",
                      "review": "проведу независимую проверку сделанного",
                      "onboard": "разберусь, чем проект написан и чем он проверяется",
                      "status": "скажу, что идёт прямо сейчас",
                      "health": "оценю состояние продукта",
                      "next": "скажу, где мы, что идёт, что мешает и что взять следующим",
                      "explain": "покажу карточку задачи: стадия, что мешает и почему, следующий "
                                 "шаг, оценка стоимости",
                      "model": "разберусь в проекте: что за продукт, что я знаю, чего не знаю",
                      "discuss": "заведу черновик обсуждения: какую боль решаем и как поймём, "
                                 "что помогло",
                      "new": "заведу место для новой работы",
                      "resume": "продолжу с последнего подтверждённого шага",
                      "feedback": "запишу твоё замечание о моей работе так, чтобы его можно было "
                                  "проверить",
                      "reach": "покажу состояние добровольной отметки о подключении (охват — "
                               "без телеметрии, только по твоему решению)",
                      "backlog": "разберу GitHub Issues: тип, дубликаты, приоритет, зависимости"}.get(
                          intent, "выполню намерение")

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
    from ai_ops_kit.cli.ai_ops_cli import INTENTS   # реестр намерений живёт в ai_ops_cli
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


_BACKLOG_SUBS = ("classify", "dedup", "prioritize", "graph", "merge")


def _run_backlog(sub, child_root, signals, js, a=None):
    """Backlog Intelligence через CLI: подкоманда первым словом, репозиторий — `child_root`.

    Читает GitHub Issues САМОЙ дочки. Третье состояние честно: если доступа к GitHub нет, ответ —
    «не проверено» с причиной и код 2 (блокировано), а НЕ пустой backlog с кодом 0. `graph` —
    синоним `depgraph`/`deps`. Состояние выборки берётся из --signals '{"state":"all"}' (по
    умолчанию open). `merge` — approval-gated слияние дублей: `a` несёт --approved/--apply."""
    sub = (sub or "").strip().lower()
    if sub in ("depgraph", "deps"):
        sub = "graph"
    state = (signals or {}).get("state", "open")
    root = str(child_root)
    if sub not in _BACKLOG_SUBS:
        # Без подкоманды (или с неизвестной) — назвать, что умеет, а не молча вернуть успех.
        msg = ("backlog: операционный разбор GitHub Issues. Подкоманды:\n"
               "  classify    — тип/область/приоритет/атрибуты, каждый вывод с объяснением\n"
               "  dedup       — дубликаты (предлагает объединение) и устаревшие\n"
               "  prioritize  — приоритет с объяснением и учётом override человека\n"
               "  graph       — граф зависимостей: блокирующие, критический путь, циклы\n"
               "  merge       — СЛИТЬ одобренные дубли (--approved файл; без --apply — dry-run)\n"
               "Пример: ./ai-ops backlog classify .   (state: --signals '{\"state\":\"all\"}')")
        if js:
            print(json.dumps({"ok": False, "reason": f"нет подкоманды backlog: {sub or '—'}",
                              "subcommands": list(_BACKLOG_SUBS)}, ensure_ascii=False, indent=2))
        else:
            print(msg)
        return 0 if not sub else 2

    if sub == "classify":
        from ai_ops_kit.planning import backlog_classify as _bc
        rep = _bc.classify_backlog(root, state=state)
        if js:
            print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))
        elif not rep.ok:
            print(f"backlog не проверен: {rep.reason}")
        else:
            print(f"Backlog {rep.repo}: {rep.total} Issues — "
                  + ", ".join(f"{k} {v}" for k, v in sorted(rep.by_type.items())))
            for c in rep.items:
                dep = f", зависит от {c.dependencies}" if c.dependencies else ""
                print(f"  #{c.number} {c.type}/{c.priority} · {c.area} (увер. {c.confidence}){dep}")
        return 0 if rep.ok else 2

    if sub == "dedup":
        from ai_ops_kit.planning import backlog_dedup as _dd
        from datetime import datetime, timezone
        rep = _dd.dedup_backlog(root, state=state, now_iso=datetime.now(timezone.utc).isoformat())
        if js:
            print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))
        elif not rep.ok:
            print(f"backlog не проверен: {rep.reason}")
        else:
            print(f"Backlog {rep.repo}: {rep.total} Issues · "
                  f"кандидатов в дубликаты {len(rep.duplicate_pairs)} (ПРЕДЛОЖЕНИЕ, слияние — с "
                  f"одобрения) · устаревших {len(rep.stale)}")
            for p in rep.duplicate_pairs:
                print(f"  #{p.a} ↔ #{p.b}  похожесть {p.score} — {p.evidence}")
            for s in rep.stale:
                print(f"  устарел #{s.number} ({s.days_idle}д): {s.title[:60]}")
        return 0 if rep.ok else 2

    if sub == "prioritize":
        from ai_ops_kit.planning import backlog_prioritize as _bp
        rep = _bp.prioritize_backlog(root, state=state)
        if js:
            print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))
        elif not rep.ok:
            print(f"backlog не проверен: {rep.reason}")
        else:
            print(f"Приоритеты {rep.repo}: {len(rep.items)} задач")
            for p in rep.items:
                mark = " [решение человека]" if p.overridden else ""
                print(f"  #{p.number} {p.priority}{mark} (score {p.score}, увер. {p.confidence})")
                print(f"      {p.explanation}")
        return 0 if rep.ok else 2

    if sub == "merge":
        # Approval-gated слияние дублей (PR-19/20 «Execute → Require approval»). Пары одобряет
        # ЧЕЛОВЕК файлом --approved (из детектора они не берутся). Без --apply — dry-run (что закроется,
        # видно ДО того). Закрывается ТОЛЬКО дубль, канонический остаётся; операция обратима.
        import yaml as _yaml
        from ai_ops_kit.planning import backlog_dedup as _dd
        approved_path = getattr(a, "approved", None) if a is not None else None
        if not approved_path:
            print(json.dumps({"ok": False, "reason": "нужен --approved <файл> с одобренными парами "
                              "{approved: [{duplicate, canonical}]}"}, ensure_ascii=False, indent=2)
                  if js else "backlog merge: нужен --approved <файл> с одобренными парами "
                  "человека ({approved: [{duplicate: N, canonical: M}]}). Слияние без явного "
                  "одобрения кит не делает.")
            return 2
        p = Path(approved_path)
        if not p.is_file():
            print(f"backlog merge: файл одобрений не найден: {approved_path}")
            return 2
        try:
            doc = _yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except _yaml.YAMLError as e:
            print(f"backlog merge: файл одобрений не разобран: {e}")
            return 2
        approved = doc.get("approved") if isinstance(doc, dict) else None
        dry = not getattr(a, "apply", False) if a is not None else True
        res = _dd.execute_merge(root, approved, dry_run=dry,
                                by=(doc.get("by") if isinstance(doc, dict) else None) or "owner")
        if js:
            print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
        else:
            head = "DRY-RUN (ничего не закрыто)" if res.dry_run else ("СЛИТО" if res.ok else "СЛИТО ЧАСТИЧНО")
            print(f"backlog merge [{head}]: выполнено {len(res.executed)}, пропущено {len(res.skipped)}")
            for e in res.executed:
                if e.get("dry_run"):
                    print(f"  #{e['duplicate']} → закрыть как дубль #{e['canonical']} (dry-run)")
                else:
                    print(f"  #{e['duplicate']} закрыт как дубль #{e['canonical']} "
                          f"(комментарий+закрытие: {'ок' if e.get('close_ok') else e.get('close_reason')})")
            for s in res.skipped:
                print(f"  пропущено #{s.get('duplicate')}↔#{s.get('canonical')}: {s.get('reason')}")
            if res.reason:
                print(f"  {res.reason}")
        return 0 if res.ok or res.dry_run else 2

    # graph
    from ai_ops_kit.planning import backlog_depgraph as _dg
    g = _dg.graph_from_backlog(root, state=state)
    if js:
        print(json.dumps(g.to_dict(), ensure_ascii=False, indent=2))
    elif not g.ok:
        print(f"backlog не проверен: {g.reason}")
    else:
        print(f"Граф зависимостей: {len(g.nodes)} задач, {len(g.edges)} связей")
        if g.cycles:
            print(f"  ⚠ циклы (доставить нельзя): {g.cycles}")
        print("  блокирующие: " + (", ".join(f"#{b['number']}×{b['dependents']}"
                                              for b in g.blocking) or "нет"))
        print("  критический путь: " + (" → ".join(f"#{n}" for n in g.critical_path) or "нет"))
        for t in g.transitive:
            print(f"  скрытая зависимость: #{t['number']} → {t['hidden']}")
    return 0 if g.ok else 2


def _intent_model(task, child_root, signals, a):
    js = a.json
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
    # #612 + one-screen: `model --answer <qid> "<value>"` записывает ответ(ы) онбординга без ручной
    # правки YAML. Флаг ПОВТОРЯЕМ (`--answer a "x" --answer b "y"`) — все ответы пишутся по очереди.
    # `--why` тоже повторяем, по одному на ответ в порядке; лишние ответы просто без основания.
    if getattr(a, "answer", None):
        whys = list(getattr(a, "why", None) or [])
        for i, pair in enumerate(a.answer):
            why = whys[i] if i < len(whys) else None
            ok, msg = repo_audit.record_answer(child_root, pair[0], pair[1], rep["ask"], why=why)
            print(msg)
            # Любой невалидный ответ — стоп и ненулевой код: пакет ответов не должен применяться
            # наполовину, а `--flow` за ним — тем более (мы бы собрали направление по неполным фактам).
            if not ok:
                return 2
        # Без `--flow` — записал и вышел (прежнее поведение, только пакетом ответов).
        if not getattr(a, "flow", False):
            return 0
        # С `--flow` — НЕ ранний return: продолжаем в первый час тем же вызовом. Понимание
        # ПЕРЕСЧИТЫВАЕМ — записанные ответы обязаны учитываться, иначе first_hour собрал бы
        # направление по устаревшим фактам (rep выше посчитан ДО записи).
        rep = repo_audit.run(child_root)
    # ПОБОЧНЫЙ ЭФФЕКТ НЕ ЗАВИСИТ ОТ ФОРМАТА ВЫВОДА и от `--flow`. Форма ответов создаётся в ЛЮБОМ
    # пути просмотра (обычном И первом часе): первый час не полон без места, куда человек впишет
    # ответы. Прежде `--flow` короткозамыкал ДО записи формы, и `setup` (шаг первого часа) не
    # оставлял «ответь на вопросы» — остаток прятался (issue #612).
    answers_file = None
    if rep["ask"]["questions"]:
        answers_file = repo_audit.write_question_file(child_root, rep["ask"])
    # #647: `model --flow` — оркестратор первого часа ОДНИМ нарративом: понял → знаю/не знаю →
    # (если ответы есть) направление+план → следующая работа. `--apply` записывает bootstrap.
    # Склейка готовых функций (repo_audit уже посчитан выше — переиспользуем rep), не новый движок.
    if getattr(a, "flow", False):
        from ai_ops_kit.planning import first_hour
        res = first_hour.run(child_root, apply=bool(getattr(a, "apply", False)),
                             budget_left=getattr(a, "budget", None), understanding=rep)
        # #1200: первый результат говорит, в каком состоянии проект СЕЙЧАС — из уже существующих
        # проверок (конституция, сканер, команды проверки). Только чтение; на непрочитанном дереве не
        # считаем: говорить «в порядке» о том, чего кит не понял, — выдумка.
        if res.get("stage") != first_hour.BLOCKED_UNDERSTANDING:
            from ai_ops_kit.cli import ai_ops_cli_first_health as _health
            res["health"] = _health.assess(child_root)
        # Конец первого часа — не строка на экране, которая улетит вверх, а ФАЙЛ с результатом,
        # который человек может открыть. Пишем только при apply (та же дисциплина, что у bootstrap:
        # сухой прогон в чужой репозиторий ничего не пишет). None — стадия без результата (дерево
        # не прочиталось): файла-обманки не создаём.
        result_file = first_hour.write_result(child_root, res) \
            if bool(getattr(a, "apply", False)) else None
        if js:
            out = dict(res)
            if answers_file:
                out["answers_file"] = str(answers_file)
            if result_file:
                out["first_result_file"] = str(result_file)
            print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        else:
            from ai_ops_kit.ui import presenter
            aud = presenter.audience_from_config(child_root)
            print(presenter.render(presenter.from_first_hour(res), audience=aud))
            if (res.get("health") or {}).get("verdict"):
                print(f"\nСостояние проекта: {res['health']['verdict']}")
            if result_file:
                print(f"\nПервый результат сохранён: {result_file}")
        return 0
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
        qs = rep["ask"]["questions"]
        if qs:
            print("\n  ⚠ — ответить сейчас; · — можно позже. В [скобках] — id для ответа командой.")
        for q in qs:
            mark = "⚠" if q["blocks_work"] else "·"
            print(f"  {mark} [{q['id']}] {q['ask']}")
            if q["proposal"]:
                print(f"      предполагаю: {q['proposal']['value']} — подтвердить?")
        # ВОПРОСАМ НУЖНО МЕСТО. Прежде кит печатал их и завершался: куда отвечать — не сказано,
        # интерактива нет, человек в тупике на главном шаге первого сценария. И проще всего —
        # по одному, командой, а не правкой YAML-файла руками (её оставляем как альтернативу).
        if answers_file:
            try:
                shown = answers_file.relative_to(Path(child_root))
            except ValueError:
                shown = answers_file
            print("\n  Ответить проще всего по одному, не открывая файл:")
            print('    ./ai-ops model --answer <id> "твой ответ"   (id — из [скобок] выше)')
            print(f"  Или впиши все ответы сразу в {shown} и запусти снова: ./ai-ops model —")
            print("  ответы станут подтверждёнными фактами и больше не будут переспрашиваться.")
    return 0


def _product_health_report(root):
    """Живое ПОЛНОЕ здоровье продукта для впрыска в контракт: продукт + технологии + delivery,
    сведённые одним rollup'ом health_common (band green/yellow/red/unknown + причины-драйверы).

    Три измерения здоровья считает intelligence (слой выше planning), поэтому их собирает CLI и
    передаёт вниз параметром. Сведение через тот же `build_report`, что у каждого измерения по
    отдельности: worst-known-band побеждает, unknown не зеленит, причины — драйверы итогового band
    по всем трём измерениям. Любой сбор -> None (контракт покажет not_computed, а не упадёт):
    здоровье обогащает вердикт, а не является его предусловием."""
    try:
        from ai_ops_kit.intelligence import health_common as hc
        from ai_ops_kit.intelligence import health_delivery, health_product, health_tech
        r = Path(root)
        signals = (health_product.collect_signals(r)
                   + health_tech.collect_signals(r)
                   + health_delivery.collect_signals(r))
        return hc.build_report("product-contract-health", signals, scope="product")
    except Exception:  # noqa: BLE001 — сбор здоровья не обязан ронять просмотр контракта
        return None


def _product_risks(root):
    """Живой реестр рисков для впрыска в контракт (risk_register: риски из здоровья+дрейфа + слепые
    зоны). intelligence выше planning -> считает CLI, передаёт вниз. Сбой -> None (риски покажутся
    not_computed, а не уронят просмотр)."""
    try:
        from ai_ops_kit.intelligence import risk_register
        return risk_register.risk_register(Path(root))
    except Exception:  # noqa: BLE001 — сбор рисков не обязан ронять просмотр контракта
        return None


def _intent_contract(task, child_root, signals, a):
    js = a.json
    # Единый объект продукта: агрегирует существующие вычислители (product_templates, contours,
    # passport_generator) в один контракт. Ничего не пишет и ничего не перестраивает.
    from ai_ops_kit.planning import artifact_registry as _AR
    from ai_ops_kit.planning import product_contract
    # Здоровье и риски считает intelligence (слой ВЫШЕ planning) — поэтому их считает CLI (может звать
    # вниз) и ВПРЫСКИВАЕТ в контракт. band уже в вокабуляре green/yellow/red/unknown; нет данных ->
    # unknown/not_computed (честно), а не выдуманное зелёное.
    health = _product_health_report(child_root)
    risks = _product_risks(child_root)
    try:
        contract = product_contract.resolve(child_root, health=health, risks=risks)
        verdict = product_contract.validate(child_root, health=health)
    except _AR.RegistryCorrupt as e:
        print(f"ОШИБКА: реестр артефактов недостоверен: {e}")
        return 1
    if js:
        print(json.dumps({"contract": contract, "verdict": verdict},
                         ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"КОНТРАКТ ПРОДУКТА — вердикт: {verdict['verdict'].upper()}")
    print(f"  стандарт: v{contract['standard']['contract_version']}")
    print(f"  артефакты слоя: {contract['artifacts']['counts']}")
    incomplete = [cid for cid, cv in contract["contours"].items() if not cv["ok"]]
    print("  источники истины контуров: "
          + ("все на месте" if not incomplete else "неполны — " + ", ".join(incomplete)))
    print(f"  здоровье: {contract['health'].get('band') or contract['health'].get('state')}")
    _rk = contract["risks"]
    if "count_by_severity" in _rk:
        _sev = _rk.get("count_by_severity") or {}
        _bs = len(_rk.get("blind_spots") or [])
        print(f"  риски: high={_sev.get('high', 0)}, medium={_sev.get('medium', 0)}"
              + (f"; слепых зон: {_bs}" if _bs else ""))
    else:
        print(f"  риски: {_rk.get('state')}")
    if verdict["blocking"]:
        print("  что мешает вердикту 'valid':")
        for b in verdict["blocking"]:
            print(f"    - {b}")
    return 0


def _intent_products(task, child_root, signals, a):
    js = a.json
    # Флит-операции над реестром продуктов. Подкоманда — первым словом (как у `backlog`):
    #   products           — сводный вердикт по всему флоту (только чтение);
    #   products register  — добавить/обновить ТЕКУЩИЙ репозиторий в реестре флота (запись).
    # Подробная карточка ОДНОГО продукта — это `ai-ops contract`, запущенный в его репозитории:
    # модель CLI передаёт один токен задачи + путь, поэтому inspect-по-id отдельной командой пока нет
    # (функция product_registry.inspect() есть для программного вызова и будущего флага).
    from ai_ops_kit.planning import product_registry
    sub = (task or "").strip()

    if sub == "register":
        # Регистрируем ТЕКУЩИЙ репозиторий (child_root). Реестр флота — центральный файл оператора
        # ($AI_OPS_PRODUCTS), иначе products.yaml рядом. Так `cd продукт && ai-ops products register`
        # накапливает флот в одном файле.
        reg_path = product_registry._default_registry(Path(child_root)) or (Path(child_root) / "products.yaml")
        res = product_registry.register(reg_path, Path(child_root).resolve())
        if js:
            print(json.dumps(res, ensure_ascii=False, indent=2, default=str)); return 0
        if res["status"] == "invalid":
            print("РЕЕСТР ПРОДУКТОВ: запись не добавлена — ошибки формы:")
            for e in res["errors"]:
                print(f"  - {e}")
            return 1
        p = res["product"]
        print(f"{res['status'].upper()}: продукт '{p['id']}' ({p['name']}) -> {res['registry']}")
        print(f"  путь: {p['path']}   вердикт сейчас: {res['verdict'] or 'не посчитан'}")
        print("  (реестр флота — центральный файл оператора; задайте $AI_OPS_PRODUCTS, чтобы "
              "накапливать все продукты в одном месте)")
        return 0

    if sub:
        print(f"неизвестная подкоманда '{sub}'. Есть: (без аргумента) — весь флот; "
              "register — добавить текущий репозиторий")
        return 1

    reg_path = product_registry._default_registry(Path(child_root))
    if reg_path is None or not Path(reg_path).is_file():
        print("НЕТ РЕЕСТРА ПРОДУКТОВ. Заведите: зайдите в репозиторий продукта и `ai-ops products register`")
        print("(создаст products.yaml; задайте $AI_OPS_PRODUCTS для общего файла флота),")
        print("или создайте вручную: kind: product-registry, products: [{id, name, path}].")
        return 1

    # Живое здоровье по каждому продукту считаем ЗДЕСЬ (CLI видит intelligence) и передаём во флот
    # картой id->отчёт: planning не тянет intelligence вверх. Нет метрик у продукта -> band=unknown.
    health_map = {}
    _data = product_registry.load(reg_path)
    for _p in (_data.get("products", []) if isinstance(_data, dict) else []):
        _pid, _path = _p.get("id"), _p.get("path")
        if _pid and _path and Path(_path).expanduser().is_dir():
            _hr = _product_health_report(Path(_path).expanduser())
            if _hr is not None:
                health_map[_pid] = _hr
    rep = product_registry.fleet(reg_path, health_map=health_map)
    if js:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
        return 0
    if rep["registry_errors"]:
        print(f"РЕЕСТР ПРОДУКТОВ {rep['registry']}: ошибки формы:")
        for x in rep["registry_errors"]:
            print(f"  - {x}")
        return 1
    print(f"ФЛОТ ({len(rep['products'])} продукт(ов)) — {rep['counts']}:")
    for r in rep["products"]:
        if r["status"] == "error":
            print(f"  ✗ {r['id']}: ОШИБКА — {r.get('reason')}")
        else:
            mark = "✓" if r["verdict"] == "valid" else "•"
            print(f"  {mark} {r['id']} ({r['name']}): {r['verdict']} "
                  f"[артефакты={r['worst_artifact_state']}, "
                  f"контуры={'ok' if r['contours_ok'] else 'неполны'}, health={r['health_band']}]")
    return 0


def _intent_inspect(task, child_root, signals, a):
    js = a.json
    # Карточка одного продукта флота по id. id — единственный токен задачи (модель CLI отдаёт один).
    # health/risks считает CLI и впрыскивает вниз — как в `contract`.
    from ai_ops_kit.planning import product_registry
    pid = (task or "").strip()
    if not pid:
        print("нужен id продукта: ai-ops inspect <id> (список — `ai-ops products`)")
        return 1
    reg_path = product_registry._default_registry(Path(child_root))
    if reg_path is None or not Path(reg_path).is_file():
        print("НЕТ РЕЕСТРА ПРОДУКТОВ. Заведите: зайдите в репозиторий продукта и `ai-ops products register`.")
        return 1
    path = product_registry.product_path(reg_path, pid)
    health = _product_health_report(path) if path and path.is_dir() else None
    risks = _product_risks(path) if path and path.is_dir() else None
    res = product_registry.inspect(reg_path, pid, health=health, risks=risks)
    if js:
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        return 0
    if res["status"] == "not_found":
        print(f"НЕТ ПРОДУКТА '{pid}' в реестре. Известные: {', '.join(res['known']) or '—'}")
        return 1
    if res["status"] == "error":
        print(f"ПРОДУКТ '{res['id']}': ОШИБКА — {res['reason']}")
        return 1
    c, v = res["contract"], res["verdict"]
    print(f"ПРОДУКТ '{res['id']}' ({res['name']}) — вердикт: {v['verdict'].upper()}")
    print(f"  стандарт: v{c['standard']['contract_version']}   артефакты: {c['artifacts']['counts']}")
    for cid, cv in c["contours"].items():
        print(f"  контур {cid}: {'ok' if cv['ok'] else 'НЕПОЛН (' + ', '.join(cv['required_missing']) + ')'}")
    print(f"  здоровье: {c['health'].get('band') or c['health'].get('state')}")
    _rk = c["risks"]
    if "count_by_severity" in _rk:
        _s = _rk.get("count_by_severity") or {}
        print(f"  риски: high={_s.get('high', 0)}, medium={_s.get('medium', 0)}")
    for b in v["blocking"]:
        print(f"  - {b}")
    return 0


def _intent_plan(task, child_root, signals, a):
    import yaml
    js = a.json
    from ai_ops_kit.engine import run_plan
    from ai_ops_kit.context import context_compiler
    from ai_ops_kit.gates import spec_levels
    from ai_ops_kit.engine import atomic_planner
    from ai_ops_kit.cli.ai_ops_cli import _say   # единый путь наружу, живёт в ai_ops_cli
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


def _intent_session(task, child_root, signals, a):
    js = a.json
    from ai_ops_kit.engops import session_guardrails, session_telemetry
    snap = session_telemetry.snapshot(str(child_root))
    pol = session_guardrails.load_policy(child_root)
    rec = session_guardrails.recommend(snap, pol)
    # session-ritual-validators-are-dead: check() вызывается на каждом produced-артефакте,
    # а не только в собственных тестах. Ошибка валидации — warning, не блок: команда session
    # read-only, и владелец должен увидеть проблему, а не получить отказ.
    #
    # ЗДЕСЬ БЫЛ ВЫЗВАН ВАЛИДАТОР ЧУЖОГО АРТЕФАКТА (снято 19.08.2026). Стояло
    # `session_guardrails.check(rec)`, но эта функция проверяет `CompletionRitual` — результат
    # ДРУГОЙ функции (`completion_ritual`), а `recommend()` возвращает рекомендацию без `kind`.
    # Итог: КАЖДЫЙ запуск `./ai-ops session` печатал в stderr «kind должен быть
    # CompletionRitual» — замерено на чистой установке. Проверка не проверяла ничего и при этом
    # обучала владельца игнорировать строки `session-check:`.
    # Своего валидатора у `SessionRecommendation` нет вовсе; заводить его здесь нельзя — это
    # `ai_ops_kit/engops/`, территория второй ленты. Передано ей работой
    # `session-recommendation-has-a-validator`.
    snap_errors = session_telemetry.check(snap)
    if snap_errors:
        import sys as _sys
        for e in snap_errors:
            print(f"session-check: {e}", file=_sys.stderr)
    if js:
        print(json.dumps({"snapshot": snap, "recommendation": rec}, ensure_ascii=False, indent=2))
    else:
        # Простой текстовый вывод без presenter (функция from_session_snapshot не реализована)
        print("Session Snapshot:")
        for k, v in snap.items():
            print(f"  {k}: {v}")
        print("\nRecommendation:")
        for k, v in rec.items():
            print(f"  {k}: {v}")
    return 0

