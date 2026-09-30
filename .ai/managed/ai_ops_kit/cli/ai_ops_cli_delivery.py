#!/usr/bin/env python3
"""Интент `ai-ops delivery` — вынесен спутником из ai_ops_cli_product.py (ратчет размера модуля).

Причина выноса: product-хаб стоял впритык к порогу 700 строк, и подкоманда `delivery record`
(подтверждённая расписка для вручную влитого PR) его переваливала. Delivery — самостоятельный интент
(delivery-план из backlog + record), поэтому живёт своим файлом, как candidates_cli/portfolio_cli;
регистрируется в ai_ops_cli.py тем же ре-экспортом. Поведение не изменено — только адрес кода.

Тонкая проводка: вся честная логика записи расписки (сверка с GitHub по номеру PR, инвариант
sha_verified) — в delivery/record_delivery; здесь только разбор ввода и перевод исхода на язык человека.
"""
from __future__ import annotations

import json
from pathlib import Path


def _delivery_record(child_root, a):
    """`ai-ops delivery record <feature-id> --pr <номер|url>` — записать ПОДТВЕРЖДЁННУЮ расписку о
    поставке для вручную влитого PR. Логика (сверка с GitHub по НОМЕРУ + запись) — в
    delivery/record_delivery; здесь тонкая проводка: разобрать feature-id и номер PR, позвать,
    перевести исход на продуктовый язык. sha_verified=true никогда не пишется отсюда — только из
    delivery-слоя и только при GitHub-подтверждённом слиянии."""
    from ai_ops_kit.delivery import record_delivery as _rd
    js = a.json
    # feature-id: второй позиционный (a.rest[1] — сырой список argparse, разбор child_root его не
    # мутирует) либо --feature. `--pr` принимает номер ИЛИ полный URL PR (парсер извлечёт число).
    rest = list(getattr(a, "rest", []) or [])
    feature_id = (rest[1] if len(rest) >= 2 else None) or getattr(a, "feature", None)
    pr_number = _rd.parse_pr_number(getattr(a, "pr", None))
    if not feature_id or pr_number is None:
        print("нужно: ai-ops delivery record <feature-id> --pr <номер|url PR> — по какому PR и для "
              "какой фичи записать подтверждённую поставку")
        return 1
    res = _rd.record_delivery_for_pr(child_root, feature_id, pr_number)
    if js:
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    st = res.get("status")
    if st == "recorded":
        if not js:
            print(f"  · записал подтверждённую поставку фичи {feature_id} из PR #{pr_number} "
                  f"(влит в {(res.get('commit_sha') or '')[:12]})")
        return 0
    if st in ("not-delivered", "mismatch"):
        if not js:
            print(f"  · PR #{pr_number} НЕ влит по данным GitHub — расписку не записываю "
                  "(иначе это ложное доказательство поставки)")
        return 1
    if st == "receipt-write-failed":
        if not js:
            print(f"  · сверка прошла, но сохранить расписку не удалось ({res.get('note')}) — повтори")
        return 1
    # unavailable (нет токена/доступа/PR не найден)
    if not js:
        print("  · не смог спросить GitHub (нет токена/доступа) — попробуй позже; "
              "расписку без подтверждения не пишу")
    return 1


def _intent_delivery(task, child_root, signals, a):
    import yaml
    js = a.json
    # Подкоманда — первым словом (как у `products`/`backlog`):
    #   delivery record <feature-id> --pr <n> — подтверждённая расписка для вручную влитого PR;
    #   delivery (без подкоманды)              — delivery-план из backlog под milestone (ниже).
    if (task or "").strip().lower() == "record":
        return _delivery_record(child_root, a)
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
