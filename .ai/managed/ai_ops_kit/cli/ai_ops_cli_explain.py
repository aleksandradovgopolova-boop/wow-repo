"""ai_ops_cli_explain.py — read-only intent-хендлер `explain` (владельческая карточка).

Вынесен из `ai_ops_cli_report` (façade+сателлит, порог модуля 700 строк): `explain` отвечает на
вопрос «что с моей задачей прямо сейчас», `inbox` — «что ждёт моего решения». Обе ТОЛЬКО ЧИТАЮТ и
проб-свободны; `inbox` (в `ai_ops_cli_report`) переиспользует `_explain_*` отсюда. Модуль НЕ
зависит от `inbox` — импорт односторонний (report -> explain), цикла нет. Регистрация обработчика
делается в `ai_ops_cli`; `ai_ops_cli_report` ре-экспортирует эти имена для вызывающих/тестов.
"""
from __future__ import annotations

import json
from pathlib import Path


# ── ai-ops explain (#539): владельческая карточка «что с моей задачей прямо сейчас» ──────────────
# Одна карточка на один вопрос владельца: какая задача, на какой она стадии, что готово, что идёт,
# ЧТО МЕШАЕТ И ПОЧЕМУ (последствием, простыми словами — не гейтом и не трейсбеком), следующий шаг и
# оценка стоимости. Команда ТОЛЬКО ЧИТАЕТ: реестр идущих работ, workitem дочки, живой статус-док
# (living_status.describe — read-only, не бросает) и журнал расхода (usage-ledger). Ничего не пишет
# и ничего не сверяет с записью (в отличие от `status`, который снятое сверкой ПЕРСИСТИТ) — поэтому
# обработчик проб-свободен и живёт здесь, а не в ai_ops_cli.py.

_EXPLAIN_STATUS_LABEL = {
    "draft": "заведена, ещё не оценивалась",
    "in_progress": "в работе",
    "blocked": "остановлена",
    "needs_human_decision": "ждёт твоего решения",
    "needs_more_evidence": "не подтверждена",
    "done": "готова",
}


def _explain_wid(entry):
    """id работы из записи active-work: движок пишет `workitem` ПУТЁМ `features/<id>/workitem.yaml`.
    Та же нормализация, что у delivery_plan._workitem_key — своя, чтобы не тянуть приватный хелпер."""
    wi = str(entry.get("workitem") or "").replace("\\", "/").strip()
    if wi:
        parts = [x for x in wi.split("/") if x]
        if len(parts) >= 2 and parts[0] == "features":
            return parts[1]
        if not wi.endswith(".yaml"):
            return wi
    return str(entry.get("id") or "")


def _explain_reconcile(team, child_root):
    """Сверить заявки с базой (снять уже влитое) read-only. Сбой git -> исходный список без сверки:
    карточка не обязана падать из-за недоступного/непроверяемого репозитория."""
    from ai_ops_kit.lifecycle import active_work
    try:
        return active_work.reconcile_with_base(team, child_root)   # чистая: исходные не мутируются
    except Exception:  # noqa: BLE001 — сверка с базой не обязана удаваться, карточку не роняем
        return team


def _explain_active(child_root):
    """READ-ONLY список идущих работ: реестр + носитель копий + сверка с базой БЕЗ записи.

    Снятое (done/superseded) и мёртвые держатели исключены — как это делает `status`, но здесь без
    `persist_reconciliation`: карточка ничего не переписывает. Битый реестр -> None (сигнал «не знаю,
    что идёт», а не «ничего не идёт»)."""
    from ai_ops_kit.lifecycle import active_work
    awp = Path(child_root) / ".ai" / "runtime" / "active-work.yaml"
    local = []
    if awp.is_file():
        try:
            local = active_work.load(awp).get("active") or []
        except active_work.ActiveWorkCorrupt:
            return None
    pub = active_work.publication_enabled(child_root)
    team = active_work.team_view(child_root, local, pub)
    team = _explain_reconcile(team, child_root)   # чистая сверка с базой, БЕЗ persist
    out = []
    for a in team:
        if (a.get("status") or "") in ("done", "superseded"):
            continue
        if active_work.holder_is_gone(a):
            continue
        out.append(a)
    return out


def _explain_workitem(child_root, wid):
    """workitem.yaml идущей работы: task/workflow/status/human_approval. -> dict (read-only)."""
    import yaml
    p = Path(child_root) / "features" / str(wid) / "workitem.yaml"
    if not p.is_file():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def _explain_gates(child_root, wid):
    """Сколько гейтов в плане работы (features/<wid>/run-plan.yaml) — для стадии. -> int|None."""
    import yaml
    p = Path(child_root) / "features" / str(wid) / "run-plan.yaml"
    if not p.is_file():
        return None
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    g = data.get("gates")
    return len(g) if isinstance(g, list) else None


def _explain_conflicts(focus, others):
    """Пересечение области записи фокусной работы с другими идущими. -> список веток/id.

    То же правило, что у delivery_plan._scope_conflict: сравнение по префиксу до первого `*`.
    Пересечение — реальная причина остановиться (две работы заденут одни и те же места). Сырьё
    (id/ветки) идёт в технические детали, а человеку блокер называется следствием."""
    from ai_ops_kit.planning.delivery_plan import scope_prefix, scopes_overlap
    mine = [scope_prefix(x) for x in (focus.get("affected_areas") or focus.get("areas") or [])]
    if not mine:
        return []
    hits = []
    for o in others:
        theirs = [scope_prefix(x) for x in (o.get("affected_areas") or o.get("areas") or [])]
        if any(scopes_overlap(m, t) for m in mine for t in theirs):
            hits.append(_explain_wid(o) or o.get("branch") or "другая работа")
    return sorted(set(hits))


def _explain_cost(child_root, wid):
    """Оценка стоимости работы из usage-ledger (persist). Честно про неизмеренное. -> dict."""
    from ai_ops_kit.shared import usage_ledger
    try:
        recs = usage_ledger.load_task(child_root, wid)
    except Exception:  # noqa: BLE001 — журнал расхода не обязан существовать/читаться
        recs = None
    if not recs:
        return {"measured": False}
    agg = usage_ledger.aggregate(recs)
    return {"measured": True, "cost_usd": agg.get("cost"),
            "cost_complete": agg.get("cost_complete"), "calls": agg.get("calls"),
            "tokens": (agg.get("input_tokens") or 0) + (agg.get("output_tokens") or 0)}


def _explain_blocker(status, human_approval, conflicts):
    """Что мешает и ПОЧЕМУ — последствием, не местом в коде и не именем гейта. -> строка|None.

    Пересечение областей — самая конкретная причина, поэтому первым. Дальше — по статусу WorkItem;
    формулировки объясняют СЛЕДСТВИЕ («к выпуску не готова», «жду решения»), а не гейт.

    Пересечение названо СЛЕДСТВИЕМ, без жаргона: имена веток и внутренние id работ, с которыми
    совпадение, — это техническая кухня (как SHA/gate-id), их место в технических деталях карточки
    (`пересечение областей`), а не в строке, которую читает человек."""
    if conflicts:
        return ("эту задачу лучше не вести сейчас одновременно с другой начатой — они заденут "
                "одни и те же места")
    return {
        "blocked": ("к выпуску пока не готова: обязательная проверка не пройдена, и я не выдаю за "
                    "готовое то, что не проверено"),
        "needs_human_decision": ("жду твоего решения: работа затрагивает то, что я не меняю без "
                                 "твоего подтверждения"),
        "needs_more_evidence": ("подтвердить готовность не могу: не хватает доказательств, что "
                                "изменение действительно проверено"),
    }.get(status)


def _explain_next(status, wid, task, no_active):
    """Следующий шаг простыми словами — БЕЗ команды и внутреннего id работы (#958).

    Продуктовая строка говорит, ЧТО я сделаю, и как это запустить по-человечески («скажи
    „продолжай“»): внутренний id работы (`wi-…`) и синтаксис `./ai-ops …` — техническая кухня
    (как SHA/gate-id), их место в технических деталях карточки (`_explain_next_command`), а не в
    строке, которую читает человек. Кит и сам доводит по «продолжай» — функциональность цела."""
    if no_active:
        return "скажи, что взять, или спроси «что дальше» — предложу с обоснованием"
    return {
        "blocked": "покажу, что именно не прошло, и доведу — скажи «продолжай»",
        "needs_human_decision": "подтверди изменение — и я продолжу",
        "needs_more_evidence": "соберу недостающие доказательства — скажи «продолжай»",
        "in_progress": "продолжу то, что уже в работе",
        "draft": "опишу и запущу — скажи «продолжай»",
        "done": "работа готова — можно доставлять или брать следующую",
    }.get(status, "продолжу то, что уже в работе")


def _explain_next_command(status, wid, task):
    """Точная CLI-команда продолжения — для технических деталей (по запросу), не в лицо человеку.

    Ничего не теряем: команда с внутренним id работы остаётся доступна на уровне technical/по
    запросу «покажи технические детали»; product-строка её не показывает (#958). -> строка|None."""
    if status in ("blocked", "needs_more_evidence"):
        return f"./ai-ops resume . {wid} --execute"
    if status == "draft":
        q = task or wid
        return f'./ai-ops specify "{q}" --feature {wid}'
    return None


def _explain_living_note(doc):
    """Строка о судьбе статус-дока — в технические детали (не блокер сам по себе)."""
    doc = doc or {}
    if doc.get("managed"):
        return ("статус-док свеж на сегодня" if doc.get("fresh_today")
                else f"статус-док обновлялся {doc.get('reviewed_at') or '—'}")
    return f"статус-док: {doc.get('reason') or 'не найден'}"


def _explain_cost_line(cost):
    """Человеческая строка о стоимости для summary."""
    if not cost.get("measured"):
        return "Стоимость пока не измерена — обращений к модели по этой задаче ещё не было."
    usd, calls = cost.get("cost_usd"), cost.get("calls") or 0
    if usd is None or (not usd and not cost.get("cost_complete")):
        return f"Обращений к модели: {calls}; их стоимость пока не измерена."
    approx = "" if cost.get("cost_complete") else " (часть обращений без стоимости)"
    return f"Пока потрачено примерно ${usd:.2f} (обращений к модели: {calls}){approx}."


def _explain_cost_tech(cost):
    """Стоимость для технических деталей."""
    if not cost.get("measured"):
        return "не измерена (нет журнала расхода задачи)"
    usd = cost.get("cost_usd")
    money = f"${usd:.4f}" if usd is not None else "—"
    return (f"{money}, обращений {cost.get('calls')}, токенов {cost.get('tokens')}, "
            + ("стоимость полная" if cost.get("cost_complete") else "стоимость неполная"))


def _explain_outcome(child_root):
    """Продуктовый ИТОГ по релизу для карточки explain (#566). Read-only, опционально.

    Автообнаружение OutcomeContract(+Readout) и прогон делает ЕДИНЫЙ путь
    post_release_loop.discover_and_run — тот же механизм, что у `readout`/`inbox`/`next`, не второй.
    Нет артефактов -> None (карточка не меняется)."""
    from ai_ops_kit.cli import post_release_loop
    result = post_release_loop.discover_and_run(child_root)
    if result is None:
        return None
    ev = (result.get("outcome") or {}).get("measured_evaluation") or {}
    return {"product_status": result.get("product_status"),
            "outcome_verdict": result.get("outcome_verdict"),
            "reason": ev.get("reason"), "flip_ready": result.get("outcome_flip_ready")}


def _explain_state(child_root):
    """READ-ONLY снимок «что с моей задачей прямо сейчас». Ничего не пишет. -> dict."""
    from ai_ops_kit.engine import living_status
    root = Path(child_root)
    doc = living_status.describe(root)                       # read-only, не бросает
    active = _explain_active(root)
    if active is None:
        return {"registry_ok": False, "focus": None, "living_status": doc}
    if not active:
        return {"registry_ok": True, "focus": None, "active_count": 0, "living_status": doc,
                "product_outcome": _explain_outcome(root)}
    focus = active[0]
    wid = _explain_wid(focus)
    # #565: per-work факты берём из ЕДИНОЙ Work-проекции (тот же источник, что у `work show` и
    # `status`), а не своим набором чтений workitem/active-work. Так explain и work show не расходятся.
    from ai_ops_kit.lifecycle import work_view
    view = work_view.project_work(wid, root)
    status = view.get("status") or "in_progress"
    return {
        "registry_ok": True, "active_count": len(active),
        "focus": {"wid": wid, "task": view.get("title") or focus.get("title") or wid,
                  "workflow": view.get("workflow"), "status": status,
                  "branch": view.get("branch") or focus.get("branch"),
                  "human_approval": bool(view.get("human_approval_required"))},
        "conflicts": _explain_conflicts(focus, active[1:]),
        "cost": _explain_cost(root, wid), "gates": _explain_gates(root, wid),
        "work_view": view,
        "living_status": doc,
        "product_outcome": _explain_outcome(root),
    }


def _explain_apply_outcome(msg, po):
    """Вплести продуктовый ИТОГ в карточку explain (#566): «технически done, продуктово нет» — явно.

    Итог ещё не измерен (unknown) — карточку НЕ трогаем: unknown ≠ провал. Провал измеренного итога
    при зелёной доставке — отдельный явный заголовок, чтобы «хорошо сделали» не читалось как «сделали
    правильное»."""
    if not po:
        return msg
    ps = po.get("product_status") or {}
    verdict = po.get("outcome_verdict")
    if verdict not in ("met", "failed"):
        return msg
    reason = po.get("reason") or ""
    base_summary = (msg.get("summary") or "").rstrip(". ")
    if ps.get("technically_done_not_product"):
        msg["headline"] = "Технически done, продуктово нет"
        msg["summary"] = base_summary + ". Доставка зелёная, но измеренный продуктовый результат " \
                                        "цель не берёт."
        msg["why_it_matters"] = ("«Сделали хорошо» и «сделали правильное» — это разные вещи. "
                                 + reason).strip()
        msg["status"] = "degraded"
    elif verdict == "failed":
        msg["summary"] = base_summary + ". Продуктовый результат по релизу не достигнут."
        msg["why_it_matters"] = ((msg.get("why_it_matters") or "") + " " + reason).strip()
        msg["status"] = "degraded"
    else:  # met
        msg["summary"] = base_summary + ". Продуктовый результат по релизу достигнут."
    td = msg.setdefault("technical_details", {"available": True, "payload": {}})
    td["available"] = True
    td.setdefault("payload", {})["продуктовый итог"] = ps.get("label")
    return msg


def _explain_message(state):
    """Снимок -> UserMessage (одна карточка). presenter скрывает технические детали и лексику для
    аудитории `product`; здесь мы лишь собираем факты и говорим блокер СЛЕДСТВИЕМ."""
    from ai_ops_kit.ui import presenter
    ls = _explain_living_note(state.get("living_status"))
    po = state.get("product_outcome")
    if not state.get("registry_ok"):
        return presenter.message(
            status="degraded", headline="Не знаю, что идёт прямо сейчас",
            summary="Запись об идущих работах повреждена, поэтому карточку задачи собрать не могу.",
            why_it_matters="Пока это так, я не поручусь, что новая работа не перепишет то, что уже "
                           "правит другая сессия.",
            next_steps=["восстановить запись об идущих работах и повторить"],
            technical={"статус-док": ls})
    if state.get("focus") is None:
        return _explain_apply_outcome(presenter.message(
            status="ok", headline="Прямо сейчас ничего не идёт",
            summary="Активной работы нет — ни одной начатой задачи.",
            why_it_matters="Сужу по тому, что сейчас идёт: начатых работ нет.",
            next_steps=["скажи, что взять, или спроси «что дальше» — предложу с обоснованием"],
            technical={"идёт работ": 0, "статус-док": ls}), po)
    f = state["focus"]
    st = f["status"]
    label = _EXPLAIN_STATUS_LABEL.get(st, "в работе")
    cost = state.get("cost") or {}
    # Имя ветки — жаргон для product-аудитории (как SHA/gate-id): держим его в technical, а в
    # продуктовой строке говорим состоянием, не идентификатором. #539 judge-fix.
    where = ("Работа идёт." if f.get("branch") else "Работа начата.")
    blocker = _explain_blocker(st, f.get("human_approval"), state.get("conflicts") or [])
    if blocker:
        why = "Что мешает: " + blocker + "."
        status = "needs_input" if st == "needs_human_decision" else "blocked"
    else:
        why = "Сейчас ничего не мешает — работа продолжается."
        status = "ok"
    # Точную команду продолжения (с внутренним wi-id) держим в технических деталях — в
    # продуктовый next-step она не летит (#958). Кита можно продолжить и словом «продолжай».
    cmd = _explain_next_command(st, f["wid"], f["task"])
    technical = {"работа": f["wid"], "workflow": f.get("workflow") or "—", "статус": st,
                 "гейтов в плане": state.get("gates") if state.get("gates") is not None else "—",
                 "оценка стоимости": _explain_cost_tech(cost), "ветка": f.get("branch") or "—",
                 "идёт работ всего": state.get("active_count"),
                 "пересечение областей": ", ".join(state.get("conflicts") or []) or "—",
                 "статус-док": ls}
    if cmd:
        technical["продолжить командой"] = cmd
    return _explain_apply_outcome(presenter.message(
        status=status, headline=f'«{f["task"]}» — {label}',
        summary=f"{where} {_explain_cost_line(cost)}",
        why_it_matters=why,
        next_steps=[_explain_next(st, f["wid"], f["task"], no_active=False)],
        technical=technical), po)


def _intent_explain(task, child_root, signals, a):
    js = a.json
    from ai_ops_kit.ui import presenter
    state = _explain_state(Path(child_root))
    if js:
        print(json.dumps(state, ensure_ascii=False, indent=2, default=str))
    else:
        print(presenter.render(_explain_message(state),
                               audience=presenter.audience_from_config(Path(child_root))))
    # Код возврата — ГОТОВНОСТЬ ОТВЕТИТЬ, а не наличие работы: карточка собрана -> 0; недостоверный
    # реестр (собрать честно не смогли) -> 1. «Ничего не идёт» — это ответ, а не отказ, поэтому 0.
    return 0 if state.get("registry_ok") else 1


