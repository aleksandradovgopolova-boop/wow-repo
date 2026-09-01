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
    # v3.36.13 (session-command-reaches-the-child): команда session перенесена из установщика в CLI,
    # чтобы работала из установленной дочки. Показывает снимок телеметрии сессии и рекомендацию.
    "session": ("снимок телеметрии сессии + рекомендация (continue/compact/clear/new_session)",
                "session", False),
    # 19.08.2026 (аудит): диагностика установки СИЛАМИ ДОЧКИ. Полный `doctor` живёт в установщике,
    # а он в поставку не едет — значит в дочке без клона кита команда отвечала «исходник рядом не
    # найден». Этот интент покрывает то, что видно изнутри репозитория, и НАЗЫВАЕТ, чего не видно.
    "doctor":  ("проверить установку изнутри репозитория (полная проверка — у кита)", "doctor", False),
    # v3.35 Product Operating Model: план продукта и его связность.
    "next":    ("что взять следующим: где мы, что идёт, что блокирует, что можно параллельно", "next", False),
    "model":   ("модель продуктового репозитория: классификация, контуры, пробелы, вопросы", "model", False),
    # Product Contract (единый объект продукта): агрегирует идентичность, стандарт, артефакты слоя,
    # источники истины контуров и здоровье в ОДИН объект с одним вердиктом. Ничего не пишет.
    "contract": ("единый контракт продукта: идентичность/стандарт/артефакты/контуры/здоровье + вердикт",
                 "contract", False),
    # Product Registry (флот): сводный вердикт по ВСЕМ продуктам из реестра флота (products.yaml /
    # $AI_OPS_PRODUCTS). «Увидеть состояние всех продуктов разом». Только чтение.
    "products": ("флот продуктов: (без арг.) сводный вердикт по всем | register — добавить текущий репозиторий",
                 "products", False),
    # Подробная карточка ОДНОГО продукта флота по id (контракт+вердикт+здоровье+риски) — без cd в его
    # репозиторий. id — единственный аргумент. Только чтение.
    "inspect": ("карточка одного продукта флота по id: контракт, вердикт, здоровье, риски", "inspect", True),
    # Team status (Фаза 4): здоровье×3 + топ-риски + блокеры + следующие задачи + milestone одним
    # снимком. Только чтение.
    "team":    ("статус команды: здоровье, риски, блокеры, следующие задачи, milestone", "team", False),
    # Governance (Фаза 4): активная политика автономии + журнал решений AI + человеческие
    # переопределения. Только чтение (enforcement не трогаем — это отдельное решение).
    "governance": ("governance продукта: политика автономии, журнал решений AI, переопределения человека",
                   "governance", False),
    # Фаза 3 (лента 4): roadmap Now/Next/Later и delivery-план из backlog.
    "roadmap": ("roadmap Now/Next/Later из плана + отклонение от авторского ROADMAP.md", "roadmap", False),
    "delivery": ("delivery-план из backlog под milestone: порядок, прогноз-оценка, риски, блокеры",
                 "delivery", False),
    # v3.35.2 (тир 4): BOOTSTRAP существовал СТРОКОЙ в реестре — кит не создавал ни направления, ни
    # плана, и владелец после онбординга оставался с пониманием и без работы. Сухой прогон по
    # умолчанию: запись в чужой репозиторий он обязан увидеть до того, как она произошла.
    "bootstrap": ("создать первое направление и план из фактов репозитория (--apply — записать)",
                  "bootstrap", False),
    # 2026-08-17: наблюдения о САМОМ КИТЕ из продуктового репозитория. Раньше они доезжали только
    # пересказом человека — три работы плана кита ссылаются на «сообщение параллельной сессии».
    # Без текста — показать судьбу уже записанных (канал обязан быть двусторонним).
    "feedback": ("рассказать киту, что он сделал не так (без текста — судьба уже сказанного)",
                 "feedback", True),
    # Backlog Intelligence (Фаза 2): GitHub Issues как операционная единица. Подкоманда — первым
    # словом: classify | dedup | prioritize | graph. Без доступа к GitHub отвечает «не проверено»
    # с причиной, а не пустотой. Форма ещё меняется — интент experimental.
    "backlog": ("backlog из GitHub Issues: classify | dedup | prioritize | graph", "backlog", True),
    # Autonomous Replanning (Фаза 5, капстоун): цикл сам сводит приоритеты плана к реальности. Без
    # флага — отчёт (read-only превью: что переупорядочено и почему + структурные ПРЕДЛОЖЕНИЯ).
    # С `--apply` — записывает переприоритизацию (класс A, обратимо, состав работ не меняет) в
    # машинный артефакт дочки; авторский plan.yaml и main не трогает. Структурные правки — только
    # предложением, автоматически не применяются.
    "replan":  ("перепланирование: сам переприоритизирует план под реальность (--apply — записать), "
                "структурные изменения — предложением", "replan", False),
}


# Интенты, которые ИСПОЛНЯЮТСЯ, а не показывают превью. Список обязан совпадать с тем, что умеет
# `_run_intent`: расхождение означает «обработчик есть, до него не доходит» — молчаливый no-op с
# кодом 0, самый дорогой вид отказа, потому что выглядит успехом. Сверяется тестом.
DIRECT_INTENTS = ("onboard", "status", "health", "plan", "new", "discuss", "review", "advise",
                  "next", "model", "bootstrap", "feedback", "session", "doctor",
                  "roadmap", "delivery", "backlog", "contract", "products", "team", "governance",
                  "inspect", "replan")


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
                      "resume": "продолжу с последнего подтверждённого шага",
                      "feedback": "запишу твоё замечание о моей работе так, чтобы его можно было "
                                  "проверить",
                      "backlog": "разберу GitHub Issues: тип, дубликаты, приоритет, зависимости"}.get(
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


# --- Реестр обработчиков команд (v3.38). Прежде диспетч был цепочкой `if intent == …` в
# `_run_intent` (файл ~1377 строк): добавить команду значило править монолит, и цена этой
# проводки — корень того, что модули продуктовых операций написаны, но не заведены. Теперь
# команда подключается регистрацией обработчика; список ключей реестра сверяется с DIRECT_INTENTS
# тестом (tests/contracts/test_direct_intents_match_handler.py).
_INTENT_HANDLERS = {}


def _intent(name):
    """Зарегистрировать обработчик команды. Повторное имя — ошибка, а не тихое затирание."""
    def _register(fn):
        if name in _INTENT_HANDLERS:
            raise ValueError(f"intent {name!r} зарегистрирован дважды")
        _INTENT_HANDLERS[name] = fn
        return fn
    return _register


@_intent("onboard")
def _intent_onboard(task, child_root, signals, a):
    import yaml
    js = a.json
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


@_intent("backlog")
def _intent_backlog(task, child_root, signals, a):
    js = a.json
    return _run_backlog(task, child_root, signals, js, a)


@_intent("feedback")
def _intent_feedback(task, child_root, signals, a):
    js = a.json
    # Наблюдение о ките — данные, а не пересказ. Без текста команда показывает судьбу уже
    # сказанного: канал в одну сторону перестают наполнять, поэтому ответ обязан быть виден.
    from ai_ops_kit.engops import kit_feedback
    # ПУТЬ РЕПОЗИТОРИЯ — НЕ НАБЛЮДЕНИЕ (проба канала на живой дочке, 18.08.2026). Обёртка
    # `./ai-ops` подставляет абсолютный путь сразу после интента, а человек по привычке от всех
    # остальных команд дописывает `.` — и второй позиционный уезжал в ТЕКСТ. `./ai-ops feedback .`
    # (ровно та команда, которую кит сам печатает как «посмотреть судьбу сказанного», плюс точка)
    # записывала наблюдение с содержанием «.», возвращала «записал» и судьбу не показывала.
    # Здесь путь читается как путь: человек просил показать судьбу, а не сообщать про каталог.
    _txt = (task or "").strip()
    if _txt and Path(_txt).is_dir():
        _txt = ""
    if not _txt:
        rep = kit_feedback.status(child_root)
        if js:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        else:
            _say(child_root, "from_kit_feedback_status", rep)
            if _audience(child_root) != "product":
                print()
                print(kit_feedback.render_status(rep))
        return 1 if rep["errors"] else 0
    ev = kit_feedback.evidence_from_args(getattr(a, "evidence_file", None),
                                         getattr(a, "evidence_command", None),
                                         getattr(a, "evidence_note", None))
    p, created, errors = kit_feedback.record(
        child_root, _txt, evidence=ev, severity=getattr(a, "severity", None),
        observation_class=getattr(a, "observation_class", None))
    if js:
        print(json.dumps({"path": str(p), "created": created, "errors": errors},
                         ensure_ascii=False, indent=2))
    else:
        try:
            shown = p.relative_to(child_root)
        except ValueError:
            shown = p
        _say(child_root, "from_kit_feedback_recorded", str(shown), created, errors,
             bool(ev), getattr(a, "observation_class", None))
    return 1 if errors else 0


@_intent("status")
def _intent_status(task, child_root, signals, a):
    js = a.json
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
    pub = active_work.publication_enabled(child_root)
    if js:
        # Досягаемость видна и в JSON — потребитель ответа не должен угадывать её сам.
        return (active_work.list_cmd(awp, as_json=True, published=pub, child_root=child_root)
                if awp.is_file() else 0)
    aud = presenter.audience_from_config(child_root)
    # Общая карта: локальные заявки + опубликованные заявки ДРУГИХ машин (если публикация
    # включена). Так «работа идёт» становится фактом о команде, а не об одной машине.
    team = active_work.team_view(child_root, data.get("active") or [], pub)
    # #137: СВЕРКА С БАЗОЙ на чтении. Поле 17.08.2026: три записи из четырёх относились к работе,
    # давно влитой в main, а `status` отвечал «Работа идёт» и советовал не трогать те же файлы.
    team = active_work.reconcile_with_base(team, child_root)
    reconciled = active_work.persist_reconciliation(awp, team) if awp.is_file() else 0
    # ВТОРОЙ ИСТОЧНИК ПРАВДЫ СПРАШИВАЕТСЯ ЗДЕСЬ, а не заводится третьим (замер 18.08.2026):
    # реестр говорит, что исполняется на этой машине, план — что объявлено идущим. Сверка живёт в
    # `planning` осознанно: `lifecycle` не вправе его импортировать (слои), а отвечает человеку
    # entrypoint — он и складывает два ответа в один.
    from ai_ops_kit.planning import delivery_plan as _dp
    try:
        cross = _dp.crosscheck_running(child_root, team, registry_exists=awp.is_file())
    except _dp.PlanCorrupt as e:
        # Битый план — не «в плане ничего не объявлено»: это ровно тот случай, где «не знаю»
        # нельзя выдать за «нет». Ответ про реестр остаётся, а про план говорим прямо.
        print(presenter.render(presenter.message(
            status="degraded",
            summary="Про заявки на работу отвечу, а про план — нет: файл плана не разбирается.",
            why_it_matters="Пока план не читается, я не могу сказать, не объявлена ли идущей "
                           "работа, которой никто не занимается.",
            next_steps=["починить файл плана и повторить"],
            technical={"ошибка": str(e)}), audience=aud))
        cross = None
    print(presenter.render(presenter.from_active_work({"active": team}, published=pub,
                                                      reconciled=reconciled, crosscheck=cross),
                           audience=aud))
    return 0


@_intent("next")
def _intent_next(task, child_root, signals, a):
    js = a.json
    # Четыре вопроса: где мы, что идёт сейчас, что блокирует, что взять следующим.
    from ai_ops_kit.planning import next_work
    from ai_ops_kit.planning import delivery_plan as _plan
    from ai_ops_kit.planning import contours as _contours
    try:
        # Личность спрашивающего — чтобы «что взять» отвечалось участнику, а не
        # репозиторию: своя работа отделяется от чужой той же меркой, что и в реестре.
        rep = next_work.compute(child_root, budget_left=getattr(a, "budget", None),
                                me=_session_identity(child_root))
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


@_intent("model")
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


@_intent("contract")
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


@_intent("products")
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


@_intent("inspect")
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


@_intent("team")
def _intent_team(task, child_root, signals, a):
    js = a.json
    # Снимок статуса команды (Фаза 4): здоровье×3 + топ-риски + блокеры + следующие задачи +
    # milestone. Агрегатор из intelligence; CLI зовёт его вниз. Только чтение.
    from ai_ops_kit.intelligence import team_sync
    try:
        status = team_sync.team_status(Path(child_root))
    except Exception as e:  # noqa: BLE001 — сбор статуса не обязан ронять команду CLI
        print(f"ОШИБКА: статус команды не собран: {e}")
        return 1
    if js:
        print(json.dumps(status, ensure_ascii=False, indent=2, default=str))
        return 0
    print(team_sync._render(status))
    return 0


@_intent("replan")
def _intent_replan(task, child_root, signals, a):
    js = a.json
    # Autonomous Replanning (Фаза 5, капстоун): оркестратор из intelligence, CLI зовёт его вниз.
    # Без --apply — read-only отчёт (превью). С --apply — записывает переприоритизацию (класс A):
    # обратимо, состав работ не меняет, авторский plan.yaml/main не трогает, kill-switch/policy/
    # budget=0 внутри модуля.
    from ai_ops_kit.intelligence import replan_loop
    root = Path(child_root)
    if getattr(a, "apply", False):
        try:
            res = replan_loop.apply_reprioritization(root)
        except Exception as e:  # noqa: BLE001 — запись не обязана ронять команду CLI
            print(f"ОШИБКА: перепланирование не применено: {e}")
            return 1
        if js:
            print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"перепланирование [{res['status']}]: {res.get('reason')}")
            if res.get("written"):
                print(f"артефакт: {res['written']} (авторский план и main не тронуты)")
        return 0
    try:
        report = replan_loop.replan_report(root)
    except Exception as e:  # noqa: BLE001 — отчёт не обязан ронять команду CLI
        print(f"ОШИБКА: отчёт-перепланирование не построен: {e}")
        return 1
    if js:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(replan_loop.format_report(report))
    return 0


@_intent("governance")
def _intent_governance(task, child_root, signals, a):
    js = a.json
    # Governance-обзор (Фаза 4): активная политика автономии + журнал решений AI + переопределения
    # человека. ТОЛЬКО ЧТЕНИЕ: enforcement (policy_engine.enforce) сознательно не трогаем — где он
    # включается в путь исполнения, решается отдельно; здесь показываем состояние governance.
    from ai_ops_kit.governance import decision_log, human_override, policy_engine
    root = Path(child_root)
    try:
        policy = policy_engine.load_policy(root)
    except policy_engine.PolicyInvalid as e:
        print(f"ОШИБКА политики: {e}")
        return 1
    decisions = decision_log.ai_decisions(root)
    ovr = human_override.overrides(root)
    if js:
        print(json.dumps({"policy": policy, "ai_decisions_count": len(decisions),
                          "overrides_count": len(ovr), "recent_decisions": decisions[-5:],
                          "overrides": ovr}, ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"GOVERNANCE ПРОДУКТА ({root})")
    print(f"  политика автономии: default={policy['default']} (источник: {policy['source']})")
    for act, lvl in (policy.get("actions") or {}).items():
        print(f"    {act}: {lvl}")
    print(f"  решений AI в журнале: {len(decisions)}; переопределений человека: {len(ovr)}")
    for e in decisions[-5:]:
        print(f"    · {e.get('date', '?')} {e.get('id', '?')}: {str(e.get('decision', ''))[:70]}")
    return 0


@_intent("bootstrap")
def _intent_bootstrap(task, child_root, signals, a):
    js = a.json
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


@_intent("health")
def _intent_health(task, child_root, signals, a):
    import yaml
    js = a.json
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


@_intent("roadmap")
def _intent_roadmap(task, child_root, signals, a):
    js = a.json
    # PR-7 (лента 4): roadmap Now/Next/Later ВЫВОДИТСЯ из плана (цели + исходы), а не пишется
    # руками. Команда read-only: строит три горизонта и сверяет их с авторским ROADMAP.md.
    # Авторскую сторону разбирает существующий roadmap.py — второй правды об одном горизонте нет.
    from ai_ops_kit.planning import roadmap_manager
    from ai_ops_kit.planning import delivery_plan as _plan
    try:
        rep = roadmap_manager.check(child_root)
    except _plan.PlanCorrupt as e:
        print(f"ОШИБКА: {e}")
        return 1
    if rep.get("errors"):
        for e in rep["errors"]:
            print(f"  ✗ {e}")
        return 1
    if js:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    labels = {"now": "СЕЙЧАС", "next": "СЛЕДУЮЩИЙ", "later": "ДАЛЬШЕ"}
    for h in ("now", "next", "later"):
        block = rep["roadmap"].get(h) or []
        print(f"{labels[h]}:")
        if not block:
            print("  (пусто)")
        for d in block:
            print(f"  • {d['goal']}: исходы {d['reached']}/{d['total']}")
    if not rep["authored_present"]:
        print("  · авторского ROADMAP.md нет — сверять с ним нечего (третье состояние)")
    for dv in rep["deviations"]:
        print(f"  ⚠ отклонение: {dv}")
    return 0


@_intent("delivery")
def _intent_delivery(task, child_root, signals, a):
    import yaml
    js = a.json
    # PR-10/PR-15 (лента 4): backlog под milestone -> исполнимый delivery-план (порядок, прогноз-
    # ОЦЕНКА, риски) + ранние блокеры. Backlog берётся ПО КОНТРАКТУ ленты 3 из файла
    # (--backlog или .ai-ops/backlog.yaml); источника нет -> третье состояние, а не пустой план.
    from ai_ops_kit.planning import roadmap_manager as _rm
    from ai_ops_kit.planning import roadmap_milestones as _ms
    from ai_ops_kit.planning import delivery_planning as _dpn
    from ai_ops_kit.planning import delivery_planning_blockers as _blk
    from ai_ops_kit.planning import delivery_plan as _plan
    bl_arg = getattr(a, "backlog", None)
    bpath = Path(bl_arg) if bl_arg else (child_root / ".ai-ops" / "backlog.yaml")
    if not bpath.is_file():
        msg = (f"источник backlog не подключён ({bpath}) — delivery-план строить не из чего. "
               f"Его кладёт интеграция ленты 3; форма файла: {{tasks: [...], milestones: [...]}}")
        if js:
            print(json.dumps({"connected": False, "note": msg}, ensure_ascii=False, indent=2))
        else:
            print(f"  · {msg}")
        return 0
    try:
        plan = _plan.load(child_root)
        doc = yaml.safe_load(bpath.read_text(encoding="utf-8")) or {}
    except _plan.PlanCorrupt as e:
        print(f"ОШИБКА: {e}")
        return 1
    if plan is None:
        print("ОШИБКА: нет planning/plan.yaml — roadmap выводить не из чего")
        return 1
    tasks = [t for t in (doc.get("tasks") or []) if isinstance(t, dict)]
    milestones = [m for m in (doc.get("milestones") or []) if isinstance(m, dict)]
    capacity, today = doc.get("capacity"), doc.get("today")
    milestone = getattr(a, "milestone", None)
    roadmap = _rm.build(plan, _plan.load_history(child_root))
    result = {"link": _ms.link(roadmap, milestones, tasks),
              "blockers": _blk.report(tasks, milestone, today)}
    if milestone:
        due = next((m.get("due") for m in milestones if m.get("id") == milestone), None)
        result["plan"] = _dpn.plan(tasks, milestone, capacity=capacity,
                                   start=today, due=due).as_dict()
    if js:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    for dl in result["link"]["directions"]:
        if dl["horizon"] in ("now", "next"):
            print(f"  • {dl['goal']} [{dl['horizon']}]: "
                  f"{len(dl['milestones'])} milestone / {len(dl['tasks'])} задач")
    for s in result["link"]["dangling_links"]:
        print(f"  ✗ {s}")
    if "plan" in result:
        fc = result["plan"]["forecast"]
        if fc and fc.get("available"):
            end = f" → {fc.get('estimated_end')}" if fc.get("estimated_end") else ""
            print(f"  прогноз (ОЦЕНКА): {fc['days']} дн.{end}")
        elif fc:
            print(f"  прогноз: НЕДОСТУПЕН — {fc.get('reason')}")
        for r in result["plan"]["risks"]:
            print(f"  ⚠ {r}")
    for b in result["blockers"]["early_blockers"]:
        print(f"  ⚠ блокер '{b['id']}' держит {b['downstream']} задач")
    return 0


# v3.36.13 (session-command-reaches-the-child): команда session перенесена из установщика в CLI,
# чтобы работала из установленной дочки. Показывает снимок телеметрии сессии и рекомендацию.
@_intent("doctor")
def _intent_doctor(task, child_root, signals, a):
    js = a.json
    from ai_ops_kit.lifecycle import child_doctor
    rep = child_doctor.assess(child_root)
    if js:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(child_doctor.render(rep))
    # Ненулевой код — ТОЛЬКО на блокерах: замечание («допишите имя проекта») не отказ.
    return 1 if rep.get("blocking") else 0


@_intent("session")
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


@_intent("new")
def _intent_new(task, child_root, signals, a):
    js = a.json
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
    sp, created, spec_rep = spec_levels.create_spec(child_root, wid, signals)
    if js:
        print(json.dumps({"workitem_id": wid, "workitem": f"features/{wid}/workitem.yaml",
                          "spec": str(sp), "spec_created": created,
                          "spec_added": spec_rep["added"]}, ensure_ascii=False, indent=2))
    else:
        _say(child_root, "from_new_feature", wid, task or wid, created,
             f"./ai-ops specify \"{task or '<задача>'}\" --feature {wid}")
    return 0


@_intent("plan")
def _intent_plan(task, child_root, signals, a):
    import yaml
    js = a.json
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


@_intent("review")
def _intent_review(task, child_root, signals, a):
    js = a.json
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


@_intent("discuss")
def _intent_discuss(task, child_root, signals, a):
    js = a.json
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


@_intent("advise")
def _intent_advise(task, child_root, signals, a):
    js = a.json
    from ai_ops_kit.engops import engineering_advisor
    result = engineering_advisor.advise(str(child_root), task_type=signals.get("task_type"))
    if js:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _say(child_root, "from_advice", result)
    return 0


def _run_intent(intent, task, child_root, signals, a):
    """v2.112 Intent UX: РЕАЛЬНОЕ действие для намерения. -> код возврата или None (нет спец-действия)."""
    child_root = Path(child_root)
    handler = _INTENT_HANDLERS.get(intent)
    return handler(task, child_root, signals, a) if handler else None


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


def _session_identity(child_root) -> str:
    """КТО держит работу — измеренная личность, а не константа.

    ЗАМЕР 18.08.2026: `session` по всему пути прогона имел значение по умолчанию `cli`, то есть ВСЕ
    параллельные сессии на машине выглядели одним держателем. Из этого следовало сразу два следствия
    заявки потребителя #150: отказ второй сессии не мог сработать в принципе (держатель «тот же»), и
    атрибуция инцидента была невозможна — в записях стояло `cli` у всех.

    Личность берётся из ТОГО ЖЕ измерения, которым кит уже считает расход сессии
    (`engops.session_telemetry`): идентификатор рантайма живёт дольше процесса, поэтому повторный
    прогон в той же сессии — тот же держатель, а не новый. Не измерилось — честный `pid:<pid>`: это
    «вот этот процесс», а не «все мы вместе»; мёртвый pid заявку не держит (`active_work`).
    """
    try:
        from ai_ops_kit.engops import session_telemetry
        sid = (session_telemetry.snapshot(str(child_root)) or {}).get("session_id")
    except Exception:      # noqa: BLE001 — телеметрия недоступна: личность НЕ теряется, см. ниже
        sid = None
    if sid:
        return f"session:{str(sid)[:8]}"
    import os as _os
    return f"pid:{_os.getpid()}"


def _session_guard_before_start(child_root, task, signals, feature=None):
    """v3.22 Culture Runtime Integration: session guard ДО старта задачи.
    1. snapshot — текущее состояние сессии (контекст и расход — measured по транскрипту сессии)
    2. relation по факту — session_boundary.classify (не жёсткое значение)
    3. recommend — расход и исход НАЗЫВАЮТСЯ ВСЕГДА (advise, не block)
    4. delegation — если большая разведка, рекомендовать сабагент
    Выводит рекомендации пользователю, не блокирует прогон.

    Почему пункт 3 говорит всегда (2026-08-13): раньше он печатал что-либо только на исходах
    `new_session`/`compact`. Контекст при этом всегда был `unknown` — транскрипт сессии не читался
    ни разу, — этих исходов не наступало, и страж молчал в 100% прогонов. Молчание неотличимо от
    «всё в порядке», а решение «здесь новую сессию не начинаем» нужно ДО траты, не после.
    """
    try:
        from ai_ops_kit.engops import session_telemetry
        from ai_ops_kit.engops import session_guardrails
        from ai_ops_kit.engops import session_boundary
        from ai_ops_kit.engops import delegation_advisor
        # 1. snapshot
        snap = session_telemetry.snapshot(str(child_root), workitem_id=feature)
        # 2. relation по факту
        current_wid = snap.get("workitem_id")
        relation_cls, reason = session_boundary.classify(
            current_workitem=current_wid, new_task=task or "", new_workitem=feature)
        relation = session_boundary.to_relation(relation_cls)
        # 3. recommend — наружу через presenter, на любом исходе
        pol = session_guardrails.load_policy(child_root)
        rec = session_guardrails.recommend(snap, pol, next_relation=relation, next_task=task,
                                           task_done=False, repo_path=str(child_root))
        _say(child_root, "from_session_economy", snap, rec)
        # 3а. автономия: может ли кит взять эту работу в ОТДЕЛЬНУЮ сессию сам, и разрешено ли ему
        # тратить. Здесь только РЕШЕНИЕ — трата отсюда невозможна: исполнителя и учёт расхода
        # подключает вызывающий (см. session_launcher.spawn, шов usage_hooks). Печатаем, только когда
        # рекомендация уже говорит о смене сессии: иначе строка была бы шумом на каждом прогоне.
        if rec.get("outcome") in ("new_session", "clear"):
            # 3б. ПОДГОТОВКА ПЕРЕХОДА, а не только совет о нём. Замер 17.08.2026: совет «уйди в
            # новую сессию» существовал и был верен, но уходить было НЕ С ЧЕМ — сессионного handoff
            # кит не писал нигде, при этом текст рекомендации утверждал, что handoff сохранён.
            # Пишем ровно на тех исходах, где переход советуется: писать на каждом прогоне значило бы
            # заводить файл там, где никто никуда не уходит.
            from ai_ops_kit.engops import session_handoff
            try:
                _h = session_handoff.write(
                    child_root, session_handoff.build(child_root, snap, rec, goal=task))
                print(f"  handoff сессии записан: {_h}")
            except Exception as _he:  # noqa: BLE001 — не смогли записать: говорим, а не молчим
                print(f"⚠ handoff сессии НЕ записан: {_he}")
            from ai_ops_kit.engops import session_launcher
            dec = session_launcher.decide(str(child_root), snap, next_relation=relation,
                                          next_task=task, task_done=False)
            _say(child_root, "from_subsession_decision", dec)
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


def _process_gate(intent, task, child_root, signals, a, preview_mode):
    """Два механизма ПЕРЕД процессным шагом. -> код возврата (шаг делать не надо) или None.

    Решение владельца 2026-08-17 по работе `kit-as-first-step-or-as-trace`. Порядок здесь —
    содержательный, не случайный:

    1. КОРОТКИЙ ПУТЬ (`planning/short_path`). Если работа уже описана, описывать её заново незачем,
       и потолок траты на описание к ней применять тоже незачем — повода тратить просто нет.
    2. ПОТОЛОК ПРОЦЕССНОЙ ФАЗЫ (`engops/process_spend`). Ловит противоположный случай: описания нет,
       разбор идёт, кода всё ещё нет. Замер поля — две сессии по 200+ тысяч токенов.

    Превью не задето: оно ничего не делает и ничего не тратит, а короткий путь — это ДЕЙСТВИЕ.
    """
    from ai_ops_kit.engops import process_spend
    if preview_mode or intent not in process_spend.PROCESS_INTENTS:
        return None
    from ai_ops_kit.planning import short_path
    child_root = Path(child_root)
    wid = a.feature or _wid_for(task, signals, a.feature)

    if not getattr(a, "full_process", False):
        d = short_path.assess(task, signals, child_root, wid)
        if d["short_path"]:
            tr = short_path.trace(child_root, wid, signals, d)
            run_cmd = f'./ai-ops run "{task or "<задача>"}" --feature {wid} --execute'
            if a.json:
                print(json.dumps({"kind": "short-path", "decision": d,
                                  "spec": str(tr.get("spec")), "record": str(tr.get("record")),
                                  "sections_filled": tr.get("filled"),
                                  "sections_declined": tr.get("declined"),
                                  "trace_error": tr.get("error"),
                                  "next_command": run_cmd}, ensure_ascii=False, indent=2))
            else:
                _say(child_root, "from_short_path", d, tr, run_cmd)
                if _audience(child_root) != "product":
                    print()
                    print(short_path.render(d))
            # След не записался — это не короткий путь, а пропуск проверок без записи о нём.
            # Отдаём тот же код, что у незакрытого гейта: молча продолжать нельзя.
            return 1 if tr.get("error") else 0
        if d["declared"] and not a.json:
            # Заявлено, но не подтверждено: называем, чего не хватает, и идём ОБЫЧНЫМ путём.
            _say(child_root, "from_short_path", d, None, None)

    # ПОВТОРНЫЙ `specify` ПОД ПОДНЯВШИЙСЯ УРОВЕНЬ — НЕ РАЗБОР (B2-26, поле 19.08.2026).
    # Потолок ловит «описание уточняется, кода нет, деньги текут». Дописывание недостающих разделов
    # ни того, ни другого не делает: шаг детерминированный, конечный и модель не зовёт вовсе. Отказ
    # экономической причиной здесь не просто лишний — он ВРЁТ: человек читает про деньги, а дело в
    # разделах, и уровень в файле так и остаётся прежним.
    top_up = {"missing": []}
    if intent == "specify":
        from ai_ops_kit.gates import spec_levels
        top_up = spec_levels.pending_sections(child_root, wid, signals)

    check = process_spend.assess(child_root, wid, intent)
    if check["blocks"] and top_up["missing"]:
        _lvl = f"{top_up.get('level_in_file')} -> {top_up['level_now']} ({top_up['level_name']})"
        if a.json:
            print(json.dumps({"kind": "spec-top-up-exempt", "check": check,
                              "level": _lvl, "sections_to_add": top_up["missing"]},
                             ensure_ascii=False, indent=2))
        else:
            # печатается ОБЕИМ аудиториям намеренно: владелец видел на этом месте отказ про деньги,
            # и заменить его молчанием значило бы починить только техническую половину
            print(f"· уровень описания поднялся ({_lvl}): дописываю разделы "
                  f"{', '.join(top_up['missing'])}. Это конечный шаг без обращения к модели — "
                  f"потолок траты на разбор к нему не применяю.")
        return None

    if check["blocks"] and not getattr(a, "spend_ok", False):
        run_cmd = f'./ai-ops run "{task or "<задача>"}" --feature {wid} --execute'
        cont_cmd = f'./ai-ops {intent} "{task or "<задача>"}" --feature {wid} --spend-ok'
        if a.json:
            print(json.dumps({"kind": "process-spend-ceiling", "exit": 2, "check": check,
                              "run_command": run_cmd, "continue_command": cont_cmd},
                             ensure_ascii=False, indent=2))
        else:
            _say(child_root, "from_process_spend", check, cont_cmd, run_cmd)
        return 2
    if check["state"] == "unknown" and not a.json and _audience(child_root) != "product":
        # «Не знаю» не выдаём за норму — но и не тревожим владельца тем, что мерить нечем.
        _say(child_root, "from_process_spend", check, None, None)
    return None


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
    ap.add_argument("--approved", default=None,
                    help="backlog merge: файл с одобренными парами дублей "
                         "({approved: [{duplicate, canonical}]}); слияние без него кит не делает")
    ap.add_argument("--backlog", default=None,
                    help="delivery: файл backlog {tasks, milestones, capacity, today} (лента 3); "
                         "по умолчанию .ai-ops/backlog.yaml")
    ap.add_argument("--milestone", default=None,
                    help="delivery: id milestone, под который строить delivery-план и прогноз")
    ap.add_argument("--apply", action="store_true",
                    help="bootstrap: РЕАЛЬНО создать отсутствующие направление и план "
                         "(без флага — сухой прогон: показать, что будет создано)")
    # Решение владельца 2026-08-17: короткий путь для уже описанной работы + потолок траты на
    # описание. Оба флага — осознанный выход из автоматики, а не режим по умолчанию.
    ap.add_argument("--full-process", action="store_true",
                    help="discuss/specify/plan: пройти полный путь даже на уже описанной работе "
                         "(короткий путь не применять)")
    ap.add_argument("--spend-ok", action="store_true",
                    help="discuss/specify/plan: продолжить разбор, зная что потолок траты на "
                         "описание до первой правки кода уже пробит")
    # Улики для `feedback`: наблюдение класса «дефект» без улики не записывается — иначе канал
    # производил бы дефекты из впечатлений.
    ap.add_argument("--evidence-file", action="append", metavar="ПУТЬ=ЦИТАТА",
                    help="feedback: файл и строка из него как основание наблюдения")
    ap.add_argument("--evidence-command", action="append", metavar="КОМАНДА=ВЫВОД",
                    help="feedback: команда и её вывод как основание наблюдения")
    ap.add_argument("--evidence-note", action="append", metavar="ТЕКСТ",
                    help="feedback: пояснение к наблюдению (уликой не считается)")
    ap.add_argument("--severity", choices=["p0", "p1", "p2"],
                    help="feedback: насколько это мешает")
    ap.add_argument("--class", dest="observation_class",
                    choices=["defect", "friction", "question", "idea"],
                    help="feedback: дефект / трение / вопрос / идея (по умолчанию выводится из улик)")
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
    # КАТАЛОГ РЕПОЗИТОРИЯ РАСПОЗНАЁТСЯ И В НАЧАЛЕ, И В КОНЦЕ — иначе один из двух путей вызова
    # ломается (F-032, третье подтверждение 17.08.2026, чистая установка).
    #
    # Обёртка `./ai-ops` подставляет путь СРАЗУ ПОСЛЕ интента (`<intent> <путь> "текст" --флаг`) — и
    # так она делает не по прихоти: хвостовой аргумент после флагов argparse в позиционную группу не
    # берёт вовсе (B2-15, 14.08.2026). Разбор же искал каталог ТОЛЬКО в хвосте. В итоге
    # `./ai-ops specify "текст" --feature X` терял текст задачи, а задачей становился путь
    # репозитория: кит печатал владельцу следующую команду с абсолютным путём вместо задачи.
    # Проверялось руками на установленной копии; тесты кита этого не видели, потому что зовут
    # `main()` в человеческом порядке.
    #
    # Порядок правил важен: сначала абсолютный путь в НАЧАЛЕ (так делает только обёртка), потом
    # хвост (так пишет человек), потом относительный путь в начале (`./ai-ops specify . "текст"`).
    # Абсолютность в первом правиле — не украшение: она отличает подстановку обёртки от текста
    # задачи, который случайно совпал с именем каталога.
    if len(rest) >= 2 and Path(rest[0]).is_dir() and Path(rest[0]).is_absolute():
        child_root = rest.pop(0)
    elif rest and Path(rest[-1]).is_dir():
        child_root = rest.pop()
    elif len(rest) >= 2 and Path(rest[0]).is_dir():
        child_root = rest.pop(0)
    if rest:
        task = rest.pop(0)
    elif needs_task:
        task = ""
    signals = json.loads(a.signals)
    if a.feature:
        signals["feature"] = a.feature

    # ПЕРЕД процессным шагом: уже описанная работа идёт коротким путём, залипший разбор
    # останавливается вопросом владельцу. Место выбрано так, что ни один процессный шаг мимо не
    # проходит: ниже команды расходятся по ветвям, и проверку пришлось бы повторять в каждой.
    _gate_rc = _process_gate(intent, task, child_root, signals, a, preview_mode)
    if _gate_rc is not None:
        return _gate_rc

    if intent == "resume":
        from ai_ops_kit.engine import ai_ops_run
        # `ai-ops resume . <feature>` — путь репозитория ПЕРВЫМ позиционным (живой прогон на child,
        # 2026-08-14). Разбор каталога в хвосте эту форму не ловит: "." уезжал в task, оттуда в
        # workitem_id -> ValueError со стеком. Здесь она разбирается явно, и только когда за
        # каталогом реально стоит второй позиционный — обычный `resume "текст задачи"` не задет.
        if task and rest and Path(task).is_dir():
            child_root, task = task, rest.pop(0)
        # v2.109 Real Resume: --execute реально продолжает прогон (не рестарт); без флага — preflight.
        argv2 = ["resume", child_root, a.feature or (task or ""), "--base", a.base]
        # v3.0-rc2 (P0.1): intent CLI ПРОВОДИТ provider/model/signals в низкоуровневый resume — иначе
        # `ai-ops resume --provider X --model Y` молча уходил в mock (политика/провайдер терялись).
        # F-026: провайдера НЕ подставляем. Здесь стояло `a.provider or "mock"` — то есть путь
        # человека всегда объявлял заглушку ЯВНО, и автовыбор живого провайдера в движке не мог
        # сработать в принципе. Не задан — пусть решает та же логика, что у `run --execute`.
        argv2 += ["--signals", a.signals]
        if a.provider:
            argv2 += ["--provider", a.provider]
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
        # F-029: create_spec ДОПИСЫВАЕТ разделы, если уровень поднялся с прошлого раза. Раньше здесь
        # приходило «уже существует», а сообщение звало заполнить разделы, которых в файле не было.
        sp, created, spec_rep = spec_levels.create_spec(Path(child_root), wid, signals,
                                                        overwrite=a.force)
        cov = spec_levels.assess_from_artifacts(signals, Path(child_root), wid)
        if a.json:
            print(json.dumps({"path": str(sp), "created": created, "added": spec_rep["added"],
                              "add_error": spec_rep["error"], "coverage": cov},
                             ensure_ascii=False, indent=2))
        else:
            try:
                shown = sp.relative_to(Path(child_root))
            except ValueError:
                shown = sp
            # obs e09fe515 (поле 20.08.2026): подсказка после specify вела СРАЗУ на `run --execute`,
            # пропуская `plan`. Заявленный путь кита — specify -> plan -> run; человек, идущий по
            # подсказкам, планирования не видел вовсе. Следующий шаг — `plan`.
            _say(Path(child_root), "from_specification", shown, created, cov["level_name"],
                 cov["sections"], cov["blocking_missing"],
                 f"./ai-ops plan \"{task or '<задача>'}\" --feature {wid}",
                 spec_rep["added"], spec_rep["error"])
        return 0

    # v2.112 Intent UX: настоящие действия (не только превью). preview_mode -> всегда показать превью.
    # v2.116: `review` тоже настоящий intent — read-only ревью действующей ветки.
    # 2026-08-19: +session. Обработчик в `_run_intent` был написан, интент объявлен в INTENTS, а
    # сюда имя не внесли — и команда МОЛЧА печатала общую заглушку с кодом 0. То есть работа
    # `session-command-reaches-the-child` довела команду до дочки и не довела до исполнения.
    # Расхождение этого списка с тем, что реально умеет `_run_intent`, теперь краснеет тестом
    # `test_direct_intents_match_the_handler` — рукой список больше не забудут.
    if not preview_mode and intent in DIRECT_INTENTS:
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
        # v3.38 (W3): регистрация подписчиков спутников — ЗДЕСЬ, на входе, а не в ядре.
        # Ядро испускает события (ai_ops_run -> events.emit) и НЕ импортирует спутники
        # (kernel-boundary); без этого импорта подписка engops зависела бы от того, тронул
        # ли ДРУГОЙ интент пакет engops раньше. Явный импорт делает регистрацию видимой
        # и статическому обходчику (test_capability_reachability).
        from ai_ops_kit.engops import session_events as _session_events  # noqa: F401
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
        # F-026: прогон, в котором модель не вызывается, не доводится до вердикта — отказ с причиной
        # (офлайн доступен, но как явный выбор: `--provider mock`).
        _refusal = ai_ops_run.live_provider_refusal(_pres, a.provider)
        if _refusal:
            if a.json:
                print(json.dumps({"kind": "run", "status": "error", "exit": 2,
                                  "error": _refusal, "provider_resolution": _pres},
                                 ensure_ascii=False, indent=2))
            else:
                print(f"ОТКАЗ: {_refusal}")
            return 2
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
                             session=_session_identity(child_root),
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


def _main_guarded(argv):
    """Граница CLI: отказ провайдера по лимиту — человеку ФРАЗОЙ и кодом, а не трейсбеком.

    ЗАМЕР ПОЛЯ 20.08.2026 (obs 99aa67ef): при исчерпании лимита сессии claude-cli наружу выходил
    RuntimeError с полным питоновским трейсбеком. Провайдер теперь поднимает типизированный
    `ProviderLimitError`; здесь он превращается в сообщение и код возврата 3 («модель недоступна»).
    Ловим ТОЛЬКО этот тип — прочие ошибки по-прежнему всплывают, чтобы дефекты не тонули в тихом
    отказе.
    """
    from ai_ops_kit.providers.orchestrator_providers import ProviderLimitError
    try:
        return main(argv)
    except ProviderLimitError as e:
        print(e.human_message(), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(_main_guarded(sys.argv[1:]))
