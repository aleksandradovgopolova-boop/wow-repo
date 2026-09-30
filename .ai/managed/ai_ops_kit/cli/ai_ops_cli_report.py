"""Проб-свободные read-only intent-хендлеры отчётности, вынесенные из `ai_ops_cli_intents`.

Здесь живут владельческие карточки `explain` (что с моей задачей прямо сейчас) и `inbox`
(что требует моего внимания). Обе команды ТОЛЬКО ЧИТАЮТ реестры, workitem, живой статус-док
и журналы — ничего не пишут и ничего не сверяют с записью, поэтому проб-свободны.

`_inbox_release_warnings` переиспользует `_product_health_report` из `ai_ops_cli_product`.
Регистрация обработчиков делается в `ai_ops_cli`. Модуль НЕ импортирует `ai_ops_cli` на
верхнем уровне — обращения к его хелперам идут ленивым импортом внутри функции.
"""
from __future__ import annotations

import json
from pathlib import Path
from ai_ops_kit.cli.ai_ops_cli_product import _product_health_report

from ai_ops_kit.cli.ai_ops_cli_explain import (  # noqa: F401 — фасад: имена explain для вызывающих/тестов
    _EXPLAIN_STATUS_LABEL, _explain_wid, _explain_reconcile, _explain_active, _explain_workitem, _explain_gates, _explain_conflicts, _explain_cost, _explain_blocker, _explain_next, _explain_next_command, _explain_living_note, _explain_cost_line, _explain_cost_tech, _explain_outcome, _explain_state, _explain_apply_outcome, _explain_message, _intent_explain,
)


# ── ai-ops inbox (#540): единая владельческая очередь «что ждёт моего решения» ────────────────────
# Один список всего, что застряло на человеке: решения (компактная карточка что/варианты/рекомендация),
# остановленные работы, работы, ждущие подтверждения, свежий ночной обзор и предупреждения о выпуске.
# Команда ТОЛЬКО ЧИТАЕТ те же источники, что explain/health и каталог решений, и ничего не пишет и не
# сверяет с записью — поэтому обработчик проб-свободен и живёт здесь. Пусто -> честное «ничего не
# ждёт», а не выдуманный список; битый реестр идущих работ -> «не знаю, что ждёт» (не ложное «ничего»).


def _inbox_decisions(child_root):
    """Ожидающие решения владельца (product-decision, status pending) — read-only. Карточка:
    что/варианты/рекомендация. Каталог решений читаем НАПРЯМУЮ, а не через его API-обёртку
    (list-decisions): та на входе делает mkdir каталога, а inbox обязан ничего не писать. Сбой чтения
    файла -> пропуск (решение молча НЕ показываем ложно)."""
    import yaml
    ddir = Path(child_root) / ".ai" / "project" / "decisions"
    if not ddir.is_dir():
        return []
    out = []
    for f in sorted(ddir.glob("*.yaml")):
        try:
            d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError):
            continue
        if not isinstance(d, dict) or d.get("status") != "pending":
            continue
        out.append({"id": d.get("id"), "what": d.get("proposal") or d.get("id") or "решение",
                    "options": [str(o) for o in (d.get("options") or [])],
                    "recommendation": d.get("recommendation") or ""})
    return out


def _inbox_works(child_root):
    """Идущие работы, застрявшие на человеке: остановленные (blocked/needs_more_evidence) и ждущие
    подтверждения (needs_human_decision / требуется одобрение). Переиспользует read-only сборку
    explain (реестр + сверка с базой БЕЗ persist). -> (blocked, reviews) либо (None, None) при
    недостоверном реестре (сигнал «не знаю, что идёт», а не «ничего»)."""
    root = Path(child_root)
    active = _explain_active(root)                 # read-only, без persist
    if active is None:
        return None, None
    blocked, reviews = [], []
    for entry in active:
        wid = _explain_wid(entry)
        wi = _explain_workitem(root, wid)
        st = wi.get("status") or "in_progress"
        task = wi.get("task") or entry.get("title") or wid
        if st in ("blocked", "needs_more_evidence"):
            why = _explain_blocker(st, wi.get("human_approval_required"), []) or "продолжить нельзя"
            blocked.append({"wid": wid, "task": task, "why": why})
        elif st == "needs_human_decision" or wi.get("human_approval_required"):
            reviews.append({"wid": wid, "task": task,
                            "why": "работа затрагивает то, что я не меняю без твоего подтверждения"})
    return blocked, reviews


# Путь durable-инбокса ночных обзоров. Держим строкой, а НЕ импортом
# nightly_review.BRIEFS_DIR_REL: этот модуль диспетчируется процессом (CI-workflow), и dormant-
# инвентарь стережёт его «0 импортеров» как легит-вход — импорт ради одной константы пробил бы этот
# инвариант. Формат стабилен (deliver_brief пишет ровно сюда).
_INBOX_BRIEFS_LATEST_REL = ".ai/project/nightly-review/briefs/latest.md"


def _inbox_insight(child_root):
    """Свежий ночной обзор владельцу: доставлен ли бриф (`latest.md` в durable-инбоксе обзоров).
    read-only. -> dict|None (None — обзора нет, а не «нечего сказать»)."""
    latest = Path(child_root) / _INBOX_BRIEFS_LATEST_REL
    if not latest.is_file():
        return None
    return {"what": "ночной обзор изменений — что поменялось и на что взглянуть",
            "path": _INBOX_BRIEFS_LATEST_REL}


def _inbox_outcome_candidate(child_root):
    """Кандидат-работа из обратной петли Outcome→Insight (#567) — ждёт решения владельца. read-only.

    Автообнаружение контракта/отчёта и прогон петли делает ЕДИНЫЙ путь post_release_loop.discover_and_run
    (тот же, что у `readout`/`explain` — один механизм). Инсайт рождается только на РЕАЛЬНОМ замере
    (met/failed); на `unknown` кандидата нет — «нет данных», а не пункт очереди. Кандидат — DRAFT:
    показываем как предложение с рекомендацией, активной работой без решения человека он не станет.
    -> dict|None (None — контракта/инсайта/кандидата нет)."""
    from ai_ops_kit.cli import post_release_loop
    result = post_release_loop.discover_and_run(child_root) or {}
    cand = result.get("candidate_work")
    if not cand:
        return None
    rec, ins = result.get("recommendation") or {}, result.get("insight") or {}
    # #987 (P0 №4): последний шаг петли — конкретное действие + «потому что <что произошло с
    # продуктом>». Именно `action`/`because` показывает `next`; остальное — детали для inbox.
    na = result.get("next_action") or {}
    return {"wid": cand.get("id"), "what": cand.get("title"), "confidence": ins.get("confidence"),
            "action": na.get("action") or cand.get("title"), "because": na.get("because") or "",
            "owner_role": na.get("owner_role") or cand.get("owner_role"),
            "proposal": rec.get("proposal") or "", "facts": rec.get("facts") or [],
            "assumptions": rec.get("assumptions") or [],
            "critical_unknowns": rec.get("critical_unknowns") or [], "sources": rec.get("sources")}


def _inbox_findings(child_root):
    """Обратное наследование §28 (#585): наблюдения дочек из findings/from-children → кандидаты + уроки.

    READ-ONLY проекция через ЕДИНЫЙ путь intelligence.child_findings (кандидаты из ОТКРЫТЫХ наблюдений,
    уроки-прецеденты из ВСЕХ — факт+число случаев+контексты, без причинности, #586). Кандидаты — DRAFT:
    активной работой без решения человека не станут (writer ≠ judge). Нет наблюдений → None (не пункт
    очереди, а «нечего показать»). Сбой сбора → None (выдуманного не показываем).
    -> dict|None: {candidates:[...], precedents:[...], open_count, observation_count}."""
    try:
        from ai_ops_kit.intelligence import child_findings
        proj = child_findings.project_findings(Path(child_root))
    except Exception:  # noqa: BLE001 — обратный канал обогащает очередь, не является её предусловием
        return None
    if not proj.get("observation_count"):
        return None
    return {"candidates": proj.get("candidates") or [], "precedents": proj.get("precedents") or [],
            "open_count": proj.get("open_count", 0), "observation_count": proj.get("observation_count", 0)}


def _inbox_decision_candidates(child_root):
    """T3: недобравшие фичи → продуктовые РЕШЕНИЯ-кандидаты (DRAFT) во входящих владельца.

    Через ЕДИНЫЙ путь candidates_cli.outcome_decision_candidates (загрузка+вердикт на CLI, проектор
    intelligence.decision_candidates чист). Решение назрело, когда исход НЕ дотянул по числам ИЛИ
    гипотеза не подтверждена — иначе кандидата нет. DRAFT: активной работой без решения владельца не
    станет (writer ≠ judge). Сбой сбора → [] (выдуманного не показываем). -> list."""
    try:
        from ai_ops_kit.cli.candidates_cli import outcome_decision_candidates
        return outcome_decision_candidates(Path(child_root))
    except Exception:  # noqa: BLE001 — обратный канал обогащает очередь, не является её предусловием
        return []


def _inbox_release_warnings(child_root):
    """Предупреждения о выпуске из живого здоровья продукта: band red/yellow -> выпускать рискованно.
    Нет метрик/сбой сбора -> пусто (выдуманного предупреждения не даём). read-only (health считает
    intelligence, как в contract/team)."""
    rep = _product_health_report(child_root)
    if not rep:
        return []
    band = (rep.get("band") or "").lower()
    if band not in ("red", "yellow"):
        return []
    sev = "красное" if band == "red" else "жёлтое"
    drivers = [str(x) for x in (rep.get("reasons") or [])][:3]
    return [{"what": f"здоровье продукта {sev} — выпускать рискованно",
             "why": "; ".join(drivers) if drivers else "см. здоровье продукта", "band": band}]


def _inbox_attention(child_root):
    """#633: durable сток внимания — эфемерные вызовы человека (blocked-preflight, эскалация модели,
    граница решений), у которых нет иного durable-дома. Гарантия охвата: что записано в шину, видно
    здесь. Read-only. -> [{key, source, reason, kind, work_id}]."""
    from ai_ops_kit.lifecycle import attention_bus as _ab
    try:
        return _ab.collect(child_root)
    except Exception:  # noqa: BLE001 — сток внимания не роняет inbox; пропавший повод вернётся
        return []


def _inbox_collect(child_root):
    """READ-ONLY снимок очереди владельца из всех источников. Ничего не пишет. -> dict."""
    root = Path(child_root)
    decisions = _inbox_decisions(root)
    blocked, reviews = _inbox_works(root)
    registry_ok = blocked is not None
    insight = _inbox_insight(root)
    candidate = _inbox_outcome_candidate(root)   # #567: кандидат-работа из обратной петли
    findings = _inbox_findings(root)             # #585: наблюдения дочек §28 -> кандидаты + уроки
    from ai_ops_kit.cli.candidates_cli import inbox_direction_candidates
    dir_candidates = inbox_direction_candidates(root)   # auto-slice: непокрытые направления роадмапа
    decision_candidates = _inbox_decision_candidates(root)  # T3: недобравшие фичи → продуктовые решения
    warnings = _inbox_release_warnings(root)
    attention = _inbox_attention(root)           # #633: шина внимания — вызовы человека из прогона
    findings_items = len((findings or {}).get("candidates") or []) + (
        1 if (findings or {}).get("precedents") else 0)
    total = (len(decisions) + len(blocked or []) + len(reviews or [])
             + (1 if insight else 0) + (1 if candidate else 0) + findings_items + len(warnings)
             + len(attention) + len(dir_candidates or []) + len(decision_candidates or []))
    return {"registry_ok": registry_ok, "total": total, "decisions": decisions,
            "blocked": blocked or [], "reviews": reviews or [], "insight": insight,
            "candidate": candidate, "findings": findings, "warnings": warnings,
            "attention": attention, "direction_candidates": dir_candidates or [],
            "decision_candidates": decision_candidates or []}


def _inbox_status(queue):
    """Статус контракта для очереди: решения/подтверждения -> нужно решение; остановки/предупреждения
    -> заблокировано; иначе ок; недостоверный реестр -> degraded (не знаем, что застряло)."""
    if not queue.get("registry_ok", True):
        return "degraded"
    findings = queue.get("findings") or {}
    attention = queue.get("attention") or []
    # #633: повод-решение из шины -> нужно решение; повод-остановка -> заблокировано.
    att_decision = any(a.get("kind") == "decision" for a in attention)
    att_blocked = any(a.get("kind") != "decision" for a in attention)
    if (queue["decisions"] or queue["reviews"] or queue.get("candidate")
            or findings.get("candidates") or queue.get("direction_candidates")
            or queue.get("decision_candidates") or att_decision):
        return "needs_input"
    if queue["blocked"] or queue["warnings"] or att_blocked:
        return "blocked"
    return "ok"


def _inbox_counts(queue):
    """Однострочная сводка очереди человеческими словами (без «0/N» и идентификаторов)."""
    parts = []
    if queue["decisions"]:
        parts.append(f"решений — {len(queue['decisions'])}")
    if queue["blocked"]:
        parts.append(f"остановлено работ — {len(queue['blocked'])}")
    if queue["reviews"]:
        parts.append(f"ждут подтверждения — {len(queue['reviews'])}")
    if queue.get("candidate"):
        parts.append("предложенная работа по итогу релиза")
    findings = queue.get("findings") or {}
    if findings.get("candidates"):
        parts.append(f"наблюдений дочек к разбору — {len(findings['candidates'])}")
    if findings.get("precedents"):
        parts.append("уроки из прогонов")
    if queue.get("direction_candidates"):
        parts.append(f"направлений роадмапа без работ — {len(queue['direction_candidates'])}")
    if queue.get("decision_candidates"):
        parts.append(f"недобравших фич к решению — {len(queue['decision_candidates'])}")
    if queue["insight"]:
        parts.append("свежий обзор")
    if queue["warnings"]:
        parts.append(f"предупреждений о выпуске — {len(queue['warnings'])}")
    if queue.get("attention"):
        parts.append(f"вызовов из прогона — {len(queue['attention'])}")
    return ", ".join(parts)


def _inbox_render(queue, aud):
    """Очередь -> текст. Для аудитории `product` внутренняя лексика скрыта (глоссарий политики), а
    идентификаторы работ/решений держим в технических деталях — как SHA/gate-id в explain. Пусто ->
    честное «ничего не ждёт», битый реестр -> «не знаю, что ждёт» (не ложное «ничего»)."""
    from ai_ops_kit.ui import presenter
    h = (lambda s: presenter._humanize(s)) if aud == "product" else (lambda s: s)
    L = presenter.statuses()
    if not queue.get("registry_ok", True):
        return (f"{L['degraded']}. Не знаю, что ждёт: запись об идущих работах повреждена.\n"
                "Восстанови её и повтори — иначе я не поручусь, что ничего не застряло на тебе.")
    if queue["total"] == 0:
        return (f"{L['ok']}. Ничего не ждёт твоего решения.\n"
                "Открытых решений, остановленных работ, непрочитанных обзоров и предупреждений о "
                "выпуске нет.")
    lines = [f"{L[_inbox_status(queue)]}. Тебя ждёт: {_inbox_counts(queue)}."]
    for d in queue["decisions"]:
        lines.append("")
        lines.append(h(f"• Реши: {d['what']}"))
        if d["options"]:
            lines.append("    варианты: " + h("; ".join(d["options"])))
        if d["recommendation"]:
            lines.append("    " + h(f"рекомендую: {d['recommendation']}"))
    for b in queue["blocked"]:
        lines.append("")
        lines.append(h(f"• Остановлена «{b['task']}»: {b['why']}"))
    for r in queue["reviews"]:
        lines.append("")
        lines.append(h(f"• Ждёт подтверждения «{r['task']}»: {r['why']}"))
    c = queue.get("candidate")
    if c:
        # #567: рекомендация несёт evidence ДО предложения — сколько наблюдений, что факт, что
        # допущение, что критично неизвестно; и только потом «предлагаю». Кандидат — черновик:
        # не станет активной работой без решения владельца (writer ≠ judge).
        lines.append("")
        lines.append(h(f"• Предлагаю по итогу релиза: {c['what']}"))
        lines.append("    " + h(f"на чём основано: наблюдений — {c.get('sources')}, "
                                f"фактов — {len(c['facts'])}, допущений — {len(c['assumptions'])}, "
                                f"критично неизвестно — {len(c['critical_unknowns'])} "
                                f"(уверенность {c.get('confidence')})"))
        for f_ in c["facts"][:3]:
            lines.append("      " + h(f"факт: {f_}"))
        for u in c["critical_unknowns"][:3]:
            lines.append("      " + h(f"не знаю: {u}"))
        lines.append("    " + h("это черновик — активной работой станет только по твоему решению"))
    # #585: обратное наследование §28 — наблюдения дочек как кандидаты к разбору (DRAFT) и уроки.
    findings = queue.get("findings") or {}
    for cand in findings.get("candidates") or []:
        lines.append("")
        lines.append(h(f"• Наблюдение из прогона к разбору: {cand.get('title')}"))
        if cand.get("source_context"):
            lines.append("    " + h(f"контекст: {cand['source_context']}"))
        lines.append("    " + h("это черновик — активной работой станет только по твоему решению"))
    # auto-slice: непокрытые направления роадмапа как кандидаты к декомпозиции (DRAFT).
    for dcand in queue.get("direction_candidates") or []:
        lines.append("")
        lines.append(h(f"• Направление роадмапа без работ: {dcand.get('title')}"))
        lines.append("    " + h("черновик — станет работой по твоему решению (./ai-ops candidates accept)"))
    # T3: недобравшие фичи как продуктовые РЕШЕНИЯ (DRAFT) — что произошло с продуктом, и что решать.
    for xcand in queue.get("decision_candidates") or []:
        lines.append("")
        lines.append(h(f"• {xcand.get('title')}"))
        if xcand.get("rationale"):
            lines.append("    " + h(xcand["rationale"]))
        lines.append("    " + h("черновик — станет работой по твоему решению (./ai-ops candidates accept)"))
    precedents = findings.get("precedents") or []
    if precedents:
        # Урок = ПРЕЦЕДЕНТ: факт + В СКОЛЬКИХ случаях + контексты, БЕЗ утверждения причинности (#586).
        lines.append("")
        lines.append(h("• Уроки из прогонов (прецеденты, не правила):"))
        for p in precedents[:3]:
            ctx = ", ".join(p.get("contexts") or [])[:80]
            lines.append("    " + h(f"«{p.get('pattern')}» — {p.get('frequency_label')} "
                                    f"(случаев: {p.get('case_count')}"
                                    + (f"; контексты: {ctx}" if ctx else "") + ")"))
        lines.append("    " + h("кит показывает факт и число случаев — вывод и перенос за тобой"))
    # #633: вызовы человека из прогона (шина внимания) — durable-повод, у которого нет иного дома.
    for att in queue.get("attention") or []:
        lines.append("")
        verb = "Реши" if att.get("kind") == "decision" else "Остановлено"
        lines.append(h(f"• {verb} — {att.get('source')}: {att.get('reason')}"))
    if queue["insight"]:
        lines.append("")
        lines.append(h(f"• Есть {queue['insight']['what']}"))
    for w in queue["warnings"]:
        lines.append("")
        lines.append(h(f"• Предупреждение о выпуске: {w['what']}"))
        if w.get("why"):
            lines.append("    " + h(w["why"]))
    lines.append("")
    lines.append("Дальше: реши, что из очереди берём первым — по любому пункту скажу подробнее по запросу.")
    if aud in ("technical", "debug"):
        ids = [str(d["id"]) for d in queue["decisions"] if d.get("id")]
        ids += [b["wid"] for b in queue["blocked"]] + [r["wid"] for r in queue["reviews"]]
        if ids:
            lines.append("")
            lines.append("Технические детали:")
            lines.append("  идентификаторы: " + ", ".join(ids))
    return "\n".join(lines)


def _intent_inbox(task, child_root, signals, a):
    js = a.json
    from ai_ops_kit.ui import presenter
    root = Path(child_root)
    queue = _inbox_collect(root)
    if js:
        print(json.dumps(queue, ensure_ascii=False, indent=2, default=str))
    else:
        print(_inbox_render(queue, presenter.audience_from_config(root)))
    # Код возврата — ГОТОВНОСТЬ ОТВЕТИТЬ, а не наличие пунктов: очередь собрана -> 0 (в т.ч. пустая);
    # недостоверный реестр идущих работ (честно собрать не смогли) -> 1.
    return 0 if queue.get("registry_ok", True) else 1

