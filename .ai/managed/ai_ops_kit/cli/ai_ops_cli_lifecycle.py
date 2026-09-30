"""Проб-свободные intent-хендлеры жизненного цикла и управления, вынесенные из
`ai_ops_cli_intents`.

Здесь живут обработчики намерений вокруг онбординга, планирования и governance:
`onboard` / `reach` / `team` / `replan` / `governance` / `bootstrap` / `health` / `roadmap`
/ `doctor` / `new` / `discuss` и хелпер `_copy_affects_from_plan`. Ни одна строка не несёт
мутационных проб (охраняемые строки остаются в `ai_ops_cli.py`).

Регистрация обработчиков делается в `ai_ops_cli`. Модуль НЕ импортирует `ai_ops_cli` на
верхнем уровне — обращения к его хелперам идут ленивым импортом внутри функции.
"""
from __future__ import annotations

import json
from pathlib import Path

# Декоратор `_intent` и реестр `_INTENT_HANDLERS` живут в `ai_ops_cli`; регистрация этих функций —
# там же, расширением общего for-цикла. `_say`/`_wid_for` (инфраструктура main/диспетча) остаются
# в `ai_ops_cli` — сюда они приходят ленивым импортом внутри функции, поэтому цикла импорта нет.


def _intent_onboard(task, child_root, signals, a):
    import yaml
    js = a.json
    from ai_ops_kit.shared import project_detector
    from ai_ops_kit.cli.ai_ops_cli import _say   # единый путь наружу, живёт в ai_ops_cli
    prof = project_detector.detect(child_root)
    out = child_root / ".ai" / "repository-profile.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(prof, allow_unicode=True, sort_keys=False), encoding="utf-8")
    # Соответствие Архитектурной конституции: первичный отчёт-РЕКОМЕНДАЦИИ владельцу (#846). Advisory,
    # не блок. Пишется в protected-зону дочки (.ai/project/**), кит его при update не затирает.
    conf_rel = None
    try:
        from ai_ops_kit.checks import constitution_conformance as _cc
        _findings = _cc.conform(child_root)
        _local = _cc.load_local_rules(child_root)         # локальные правила дочки из её уроков (#849)
        conf_out = child_root / ".ai" / "project" / "constitution-conformance.md"
        conf_out.parent.mkdir(parents=True, exist_ok=True)
        conf_out.write_text(_cc.render_report(_findings, local=_local), encoding="utf-8")
        conf_rel = str(conf_out.relative_to(child_root))
        _conf_summary = _cc.summary(_findings)
    except (ImportError, OSError, ValueError):           # отчёт-советчик не должен ронять онбординг
        _conf_summary = None
    # voluntary-child-registration: решение об отметке — РЕАЛЬНАЯ часть онбординга. По умолчанию
    # (флага нет) не создаётся ничего — инвариант opt-in; явный флаг записывает решение владельца.
    # Сети нет ни в одной ветке — кит ничего никуда не отправляет.
    from ai_ops_kit.engops import child_registry as _reg
    reach = getattr(a, "reach", None)
    reach_recorded = None                                 # для JSON: что записано ("registered"/"declined") или None
    if reach == "register":
        _reg.register(child_root)
        reach_recorded = "registered"
    elif reach == "decline":
        _reg.decline(child_root)
        reach_recorded = "declined"
    if js:
        print(json.dumps({"written": str(out), "profile": prof,
                          "conformance_report": conf_rel, "reach": reach_recorded},
                         ensure_ascii=False, indent=2))
    else:
        _say(child_root, "from_onboarding_profile", prof, str(out.relative_to(child_root)))
        if conf_rel:
            print(f"\n  Соответствие конституции: {_conf_summary}")
            print(f"  Отчёт с рекомендациями: {conf_rel}")
        if reach == "register":
            # Явное «да»: отметка записана анонимно и локально, без сети.
            print("\n  Отметились: анонимно и без телеметрии — кит ничего никуда не отправляет.")
            print("  Охват обновится, когда владелец кита периодически соберёт отметившиеся проекты "
                  "(reach collect).")
        elif reach == "decline":
            # Явное «нет»: ничего не создано, онбординг это не затрагивает.
            print("\n  Отметку отклонили: ничего не создано, онбординг это не затрагивает.")
        elif not _reg.has_decided(child_root):
            # Решения ещё нет -> ОДИН РАЗ задаём явный вопрос да/нет. По умолчанию НЕ отмечаемся и
            # НИЧЕГО не создаём в этой ветке (иначе отказ перестал бы быть безопасным и молчаливым).
            print("\n  Отмечаемся? Это добровольно и без телеметрии — кит ничего никуда не отправляет "
                  "(запишем имя проекта + версию кита + дату + анонимный id, без путей и почты).")
            print("  Да:  ./ai-ops onboard . --reach register   (или ./ai-ops reach register .)")
            print("  Нет: ./ai-ops onboard . --reach decline    (или ./ai-ops reach decline .)")
            print("  Охват потом обновляется, когда владелец кита периодически собирает отметившиеся "
                  "проекты (reach collect).")
    return 0


def _intent_reach(task, child_root, signals, a):
    """Добровольная отметка о подключении и охват — БЕЗ телеметрии (voluntary-child-registration).

    Подкоманда — первым словом (как у backlog/products). Всё opt-in, рукой владельца, без сети:
      register|decline|forget|status|summary  — в дочке (child_root);
      coverage                                  — в ките (child_root = корень кита);
      collect                                   — списком дочек, зовётся модулем (не одним токеном).
    Без подкоманды — показать состояние отметки. Обработчик проб-свободен (пишет обычные локальные
    файлы `.ai/reach/*`, мутационных git-проб не несёт), поэтому живёт здесь, а не в ai_ops_cli.py.
    """
    from ai_ops_kit.engops import child_registry as cr
    js = a.json
    sub = (task or "").strip().lower()
    root = child_root

    if sub in ("", "status"):
        rep = cr.registration_state(root)
        print(json.dumps(rep, ensure_ascii=False, indent=2) if js else cr.render_state(rep))
        return 1 if rep["errors"] else 0

    if sub == "register":
        p, created, rec = cr.register(root)
        if js:
            print(json.dumps({"path": str(p), "created": created, "registration": rec},
                             ensure_ascii=False, indent=2))
        else:
            print(f"Отмечено: {rec['project']} (анонимный id {rec['id']}, версия кита "
                  f"{rec.get('kit_version') or 'н/д'}). Запись локальна — кит её никуда не отправляет; "
                  "поделиться можешь ты сам. Отозвать: ./ai-ops reach forget ." if created
                  else f"Уже отмечено: {rec['project']} (id {rec['id']}).")
        return 0

    if sub == "decline":
        cr.decline(root)
        print(json.dumps({"decision": "declined"}, ensure_ascii=False, indent=2) if js
              else "Отметка отклонена. Ничего не создано, онбординг это не затрагивает. "
                   "Передумать: ./ai-ops reach register .")
        return 0

    if sub == "forget":
        removed = cr.forget(root)
        print(json.dumps({"removed": removed}, ensure_ascii=False, indent=2) if js
              else f"Согласие отозвано: удалено записей — {len(removed)}.")
        return 0

    if sub == "summary":
        # Продуктовый статус берём из УЖЕ существующего Product Passport и впрыскиваем ВНИЗ (engops
        # не тянет planning — тот же приём, что health->contract). Паспорт с пробелом -> в сводке
        # пробел: снимок переносит verified/inferred/unknown как есть, ничего не выдумывая.
        ps = None
        try:
            from ai_ops_kit.planning import passport_generator
            ps = cr.product_status_snapshot(passport_generator.sections(Path(root)))
        except Exception:  # noqa: BLE001 — паспорт не обязан собираться; тогда статус честно unknown
            ps = None
        p, rep = cr.write_summary(root, product_status=ps)
        if js:
            print(json.dumps(dict(rep, saved_to=str(p)), ensure_ascii=False, indent=2))
        else:
            print(cr.render_summary(rep))
            print(f"  Сохранено: {p} — поделиться можно, передав этот файл владельцу кита.")
        return 0

    if sub == "coverage":
        rep = cr.coverage(root)
        print(json.dumps(rep, ensure_ascii=False, indent=2) if js else cr.render_coverage(rep))
        return 1 if rep["errors"] else 0

    if sub == "collect":
        # Сбор дочек в кит зовётся модулем (интент CLI отдаёт один токен, а collect берёт список дочек).
        print(json.dumps({"ok": False, "reason": "collect зовётся модулем: "
                          "python3 -m ai_ops_kit.engops.child_registry collect <kit> <child>..."},
                         ensure_ascii=False, indent=2) if js
              else "reach collect собирает несколько дочек и зовётся модулем:\n"
                   "  python3 -m ai_ops_kit.engops.child_registry collect <корень-кита> <дочка> [<дочка>...]")
        return 2

    print(f"неизвестная подкоманда '{sub}'. Есть: register | decline | forget | status | "
          "summary (в дочке) · coverage (в ките)")
    return 1


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


def _intent_governance(task, child_root, signals, a):
    js = a.json
    # Governance-обзор (Фаза 4): активная политика автономии + журнал решений AI + переопределения
    # человека. ТОЛЬКО ЧТЕНИЕ: enforcement (policy_engine.enforce) сознательно не трогаем — где он
    # включается в путь исполнения, решается отдельно; здесь показываем состояние governance.
    from ai_ops_kit.governance import decision_boundary, decision_log, human_override, policy_engine
    root = Path(child_root)
    try:
        policy = policy_engine.load_policy(root)
    except policy_engine.PolicyInvalid as e:
        print(f"ОШИБКА политики: {e}")
        return 1
    decisions = decision_log.ai_decisions(root)
    ovr = human_override.overrides(root)
    # Граница решений (#631): три оси действия РАЗОМ -> имя класса + причина. Классифицируем текущее
    # действие по сигналам вызова; без сигналов это профиль по умолчанию (fail-closed COLLABORATIVE).
    boundary = decision_boundary.evaluate(signals)
    if js:
        print(json.dumps({"policy": policy, "ai_decisions_count": len(decisions),
                          "overrides_count": len(ovr), "recent_decisions": decisions[-5:],
                          "overrides": ovr, "decision_boundary": boundary},
                         ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"GOVERNANCE ПРОДУКТА ({root})")
    print(f"  политика автономии: default={policy['default']} (источник: {policy['source']})")
    for act, lvl in (policy.get("actions") or {}).items():
        print(f"    {act}: {lvl}")
    for _ln in decision_boundary.describe(boundary):   # граница решений (#631): 3 оси -> класс
        print(_ln)
    print(f"  решений AI в журнале: {len(decisions)}; переопределений человека: {len(ovr)}")
    for e in decisions[-5:]:
        print(f"    · {e.get('date', '?')} {e.get('id', '?')}: {str(e.get('decision', ''))[:70]}")
    return 0


def _intent_bootstrap(task, child_root, signals, a):
    js = a.json
    # BOOTSTRAP: онбординг заканчивается работой, а не документацией. Пишет ТОЛЬКО с --apply и
    # ТОЛЬКО отсутствующее; заготовку кита заменяет (в ней нет фактов о продукте), настоящий
    # план — никогда.
    from ai_ops_kit.planning import product_bootstrap as _boot
    from ai_ops_kit.planning import contours as _contours
    from ai_ops_kit.planning import delivery_plan as _dp
    from ai_ops_kit.planning import repo_audit as _ra
    from ai_ops_kit.cli.ai_ops_cli import _say   # единый путь наружу, живёт в ai_ops_cli
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


def _intent_roadmap(task, child_root, signals, a):
    js = a.json
    if (task or "").strip().lower() == "sync-issues":   # реализация в roadmap_sync_cli (ратчет размера)
        from ai_ops_kit.cli.roadmap_sync_cli import run_roadmap_sync
        return run_roadmap_sync(child_root, a)
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
    labels = {"now": "СЕЙЧАС В РАБОТЕ", "next": "СЛЕДУЮЩИЙ РЕЗУЛЬТАТ", "later": "ПОЗЖЕ"}
    for h in ("now", "next", "later"):
        block = rep["roadmap"].get(h) or []
        print(f"{labels[h]}:")
        if not block:
            print("  (пусто)")
        for d in block:
            # Слаг — технический якорь; счётчик исходов подаём человеку словами, а не «0/2».
            name = d.get("title") or d["goal"]
            anchor = f" ({d['goal']})" if name != d["goal"] else ""
            print(f"  • {name}{anchor}: "
                  f"{roadmap_manager.humanize_outcomes(d['reached'], d['total'])}")
    if not rep["authored_present"]:
        print("  · авторского обзора-файла ROADMAP.md нет — сверять с ним нечего (третье состояние)")
    for dv in rep["deviations"]:
        print(f"  ⚠ расхождение с обзором: {dv}")
    return 0


# v3.36.13 (session-command-reaches-the-child): команда doctor показывает готовность дочки.
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


def _intent_new(task, child_root, signals, a):
    js = a.json
    from ai_ops_kit.lifecycle import workitem
    from ai_ops_kit.gates import spec_levels
    from ai_ops_kit.engine import run_plan
    from ai_ops_kit.cli.ai_ops_cli import _say, _wid_for   # инфраструктура, живёт в ai_ops_cli
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


def _intent_specify(task, child_root, signals, a):
    """v2.110 Real Spec-First: `specify` РЕАЛЬНО создаёт spec-артефакт нужной глубины (не превью).

    Исход 3 (#768, obs 8a891ce7): раскрытие ЭСКАЛАЦИИ формы считаем по СЫРЫМ сигналам — ДО того как
    ниже проставим task_type (иначе провизорность потерялась бы: доопределённый task_type выглядит
    как заявленный тип). Симметрично #838 на шаге plan, но об УРОВНЕ и РАЗДЕЛАХ формы.

    #863 (живой zero-touch прогон): `--answers "слово=ответ; …"` записывает ответы ОБЫЧНЫМИ
    СЛОВАМИ в уже созданный spec.yaml — владелец не открывает файл вовсе. Разбор и запись живут в
    `ai_ops_kit.shared.spec_answers` (общий модуль: те же слова использует и presenter в подсказке
    `answer_command`, и это разбор). Применяем ответ ДО пересчёта coverage — иначе только что
    заполненные разделы попали бы в `blocking_missing`, будто их и не назвали."""
    from ai_ops_kit.gates import spec_levels
    from ai_ops_kit.engine import run_plan
    from ai_ops_kit.shared import spec_answers
    from ai_ops_kit.cli.ai_ops_cli import _say   # единый путь наружу, живёт в ai_ops_cli
    child_root = Path(child_root)
    spec_provisional, spec_disclosure = spec_levels.escalation_disclosure(signals)
    if not signals.get("task_type"):
        signals["task_type"] = run_plan.build_plan(dict(signals, task_text=task or ""))["base_workflow"]
    wid = a.feature or run_plan.build_plan(dict(signals, task_text=task or ""))["workitem_id"]
    # F-029: create_spec ДОПИСЫВАЕТ разделы, если уровень поднялся с прошлого раза. Раньше здесь
    # приходило «уже существует», а сообщение звало заполнить разделы, которых в файле не было.
    sp, created, spec_rep = spec_levels.create_spec(child_root, wid, signals, overwrite=a.force)
    ans_rep = {"applied": [], "unmatched": [], "error": None}
    if getattr(a, "answers", None):
        ans_rep = spec_answers.apply_answers(child_root, wid, a.answers)
    cov = spec_levels.assess_from_artifacts(signals, child_root, wid)
    _disc = spec_disclosure or {}
    if a.json:
        print(json.dumps({"path": str(sp), "created": created, "added": spec_rep["added"],
                          "add_error": spec_rep["error"],
                          "answers_applied": ans_rep["applied"],
                          "answers_unmatched": ans_rep["unmatched"],
                          "answers_error": ans_rep["error"],
                          # Исход 3: форма может быть предварительной — называем это ДО заполнения.
                          "spec_provisional": spec_provisional,
                          "level_if_escalated": _disc.get("level_if_escalated"),
                          "sections_if_escalated": _disc.get("sections_if_escalated"),
                          "coverage": cov}, ensure_ascii=False, indent=2))
        return 0
    try:
        shown = sp.relative_to(child_root)
    except ValueError:
        shown = sp
    # Готовая команда ответить СЛОВАМИ на то, что ещё осталось (не файл, не JSON): пример из первых
    # нескольких вопросов, чтобы строка не разрасталась на весь L2/L3 список.
    _answer_words = spec_answers.answer_words_for(cov["blocking_missing"][:3])
    _answer_command = None
    if _answer_words:
        _pairs = "; ".join(f"{w}=..." for w in _answer_words)
        # #958: feature id (wi-…) детерминирован от текста задачи — кит резолвит ту же фичу по
        # тексту без `--feature`, поэтому в человеко-обращённую команду id не подставляем.
        _answer_command = f'./ai-ops specify "{task or "<задача>"}" --answers "{_pairs}"'
    # obs e09fe515 (поле 20.08.2026): подсказка после specify вела СРАЗУ на `run --execute`, минуя
    # plan. Заявленный путь кита — specify -> plan -> run; следующий шаг — `plan`.
    _say(child_root, "from_specification", shown, created, cov["level_name"],
         cov["sections"], cov["blocking_missing"],
         f"./ai-ops plan \"{task or '<задача>'}\"",
         spec_rep["added"], spec_rep["error"],
         spec_provisional, _disc.get("sections_if_escalated"), _disc.get("level_if_escalated"),
         _answer_command, ans_rep["applied"], ans_rep["unmatched"], ans_rep["error"],
         task=task, level_reason=cov.get("level_reason"))
    return 0


def _intent_discuss(task, child_root, signals, a):
    js = a.json
    from ai_ops_kit.cli.ai_ops_cli import _say, _wid_for   # инфраструктура, живёт в ai_ops_cli
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

