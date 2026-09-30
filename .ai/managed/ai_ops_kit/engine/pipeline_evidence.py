#!/usr/bin/env python3
"""Evidence collection, authoring, and review functions for the execution pipeline.

Extracted from execution_pipeline.py — artifact authoring, independent reviews,
security review, evidence re-evaluation, dependency installation.
"""
from __future__ import annotations

import re
from pathlib import Path

from ai_ops_kit.engine import tool_loop
from ai_ops_kit.engine import tool_broker
from ai_ops_kit.gates import gate_executor
from ai_ops_kit.gates import gate_policy

from ai_ops_kit.engine.pipeline_helpers import (
    _openspec_validate, _authoring_specs,
    _reviewable_gates, _gate_checklist,
)
from ai_ops_kit.engine.pipeline_git import _change_context
from ai_ops_kit.engine.pipeline_failure import _security_verdict_errors, _evidence_ref_errors

# Ре-экспорт кластера «авторинг + contour-evidence» — вынесен в сателлит `pipeline_authoring`
# (разрез монолита при потолке размера). Публичная поверхность неизменна: `execution_pipeline`,
# `pipeline_stages` и тесты по-прежнему импортируют эти имена из `pipeline_evidence`. Сателлит фасад
# НЕ импортирует — обратного ребра нет.
from ai_ops_kit.engine.pipeline_authoring import (  # noqa: F401 — публичный ре-экспорт
    _install_dependencies, _raw_author_tail, _author_with_retry, _run_spec_authoring,
    _run_authoring, _authored_context, contour_consistency_evidence,
)


def _reevaluate_artifact_evidence(work_root, wid, gate_ids):
    """v3.8.3 reevaluate: пере-вывести evidence артефакт-гейтов из СУЩЕСТВУЮЩИХ на диске артефактов
    (БЕЗ модели) — SHA не менялся, форма уже подтверждена оригинальным прогоном."""
    import yaml as _yaml
    ev = {}
    out_dir = Path(work_root) / ".ai" / "runplan" / wid
    for gid, (fname, mod, _kind, _shape) in _authoring_specs().items():
        if gid not in gate_ids:
            continue
        p = out_dir / fname
        if not p.is_file():
            continue
        try:
            data = _yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and not mod.check(data):
                ev[gid] = {"status": "pass", "provided": mod.provided_evidence(data),
                           "evidence": [f".ai/runplan/{wid}/{fname} — форма подтверждена (reevaluate, SHA стабилен)"]}
        # Причина подавления ЗАПИСАНА (срез engine ратчета 2026-08-12): направление отказа
        # fail-closed. Нечитаемый или невалидный артефакт НЕ попадает в `ev`, а `ev` — единственный
        # способ этой функции сказать «гейт закрыт». Значит сбой разбора даёт НЕЗАКРЫТЫЙ гейт, а не
        # закрытый по ошибке: гейт пересчитается обычным путём. Обратное направление здесь
        # невозможно по построению — `pass` не умеет добавить `status: pass`.
        except Exception:  # noqa: BLE001,S110 — сбой разбора артефакта не закрывает гейт (fail-closed)
            pass
    if "specification" in gate_ids:
        try:
            _avail, _ok, _ = _openspec_validate(work_root, wid)
            if _avail and _ok:
                # СИММЕТРИЯ С АВТОРИНГОМ (живой прогон 587). Путь авторинга спеки закрывает
                # `specification` с ОБОИМИ required_evidence — `openspec_valid` И `requirements_covered`
                # — на одном основании: `openspec validate --strict OK` (см. _run_spec_authoring,
                # gate_ev["specification"].provided = ["openspec_valid", "requirements_covered"]).
                # Здесь, на reevaluate, при ТОМ ЖЕ подтверждении отдавалось только `openspec_valid`,
                # и гейт падал «бездоказательным pass: не подтверждён requirements_covered» — хотя SHA
                # стабилен и спека валидна ровно как при авторинге. Правка чисто правку кода (без смены
                # спеки) роняла specification на reevaluate. Отдаём оба — то же основание, что авторинг.
                ev["specification"] = {"status": "pass",
                                       "provided": ["openspec_valid", "requirements_covered"],
                                       "evidence": ["openspec validate --strict (reevaluate, SHA стабилен)"]}
        except Exception:  # noqa: BLE001,S110 — то же: не подтвердили спеку -> гейт остаётся незакрытым
            pass
    return ev


def _delivered_files(work_root, revision) -> set:
    """Файлы, затронутые ДОСТАВЛЕННОЙ правкой на revision. Авторитет — git, не судья.

    Тот же источник, что уже использует `_review_security` (git show --name-only). Нужен для
    заземления вердикта ревьюера по Fix C: кит сам знает состав правки, а не полагается на read-op."""
    if not revision:
        return set()
    from ai_ops_kit.engine.pipeline_git import _git
    rc, names, _ = _git(work_root, "show", "--name-only", "--format=", revision)
    if rc != 0:
        return set()
    return {ln.strip().lstrip("./") for ln in names.splitlines() if ln.strip()}


def _cited_lines_confirmed(work_root, source, lines) -> bool:
    """Прочитать ДОСТАВЛЕННЫЙ файл и подтвердить, что процитированный диапазон строк РЕАЛЕН.

    Заземление ревью поднято до уровня приёмки (Fix C, P0 04.09.2026). Прежде хватало совпадения
    ИМЕНИ файла — содержимое не читалось, и `pass` проходил с ВЫДУМАННЫМ диапазоном строк при 0 reads
    (остаточный рубер-штамп: у приёмки он закрыт сверкой цитаты по файлу, у ревьюеров — нет). Здесь
    кит САМ читает файл тем же механизмом, что и приёмка (`_read_source` — тот же пакет engine, без
    восходящего ребра слоёв), и сверяет цитату ревьюера: у ревьюер-evidence цитата — это `lines`
    (диапазон в доставленном файле), поэтому «фрагмент присутствует в файле» = процитированные строки
    существуют в нём. Диапазон за пределами файла (или пустой/неразбираемый) — цитата чтением НЕ
    подтверждена: fail-closed, как рубер-штамп в приёмке."""
    from ai_ops_kit.engine.acceptance_verify import _read_source  # переиспользование ВНУТРИ engine
    body, _problem = _read_source(work_root, source)
    if body is None:
        return False
    total = len(body.splitlines())
    if total == 0:
        return False
    nums = [int(n) for n in re.findall(r"\d+", str(lines or ""))]
    if not nums:
        return False
    return 1 <= min(nums) and max(nums) <= total


def _review_cites_delivered_file(res, delivered, work_root) -> bool:
    """Заземлена ли хоть одна evidence-цитата ревьюера ЧТЕНИЕМ доставленного файла? (Fix C для ревью).

    Reviewer-result несёт `checks[].evidence[] = {file, lines}`. Заземление требует ДВУХ условий,
    как в приёмке: (1) evidence ссылается на файл, реально входящий в правку (не пустая/чужая
    ссылка); (2) кит ПРОЧИТАЛ этот файл и процитированный диапазон строк в нём ЕСТЬ (не выдуман).
    Совпадения имени БЕЗ чтения содержимого — недостаточно: `pass` с выдуманными строками при 0
    reads остаётся рубер-штампом и блокирующий гейт не закрывает."""
    if not delivered or not isinstance(res, dict):
        return False
    for c in res.get("checks") or []:
        if not isinstance(c, dict):
            continue
        for ev in c.get("evidence") or []:
            f = ev.get("file") if isinstance(ev, dict) else None
            if not f or f.strip().lstrip("./") not in delivered:
                continue
            if _cited_lines_confirmed(work_root, f, ev.get("lines")):
                return True
    return False


# СТРУКТУРНО НЕМОЙ ОТВЕТ ПРОВАЙДЕРА-РЕВЬЮЕРА — среда недоступна (вложенный `claude -p` оборвался ДО
# модели, #160) ИЛИ провайдер вернул ПУСТО (`empty_answer`): вердикта в ответе нет ВОВСЕ, повтор его
# не родит. Только такой ответ ведёт в handoff (awaiting_reviewer). Это НЕ «дал разбор без вердикта»
# (проза / `shape_violated` / `refused_by_model`) — тот остаётся НАЗВАННЫМ no-verdict, а не handoff:
# иначе ЛЮБОЙ no-verdict молча превращался бы в ожидание оркестратора (страж бы ослаб).
_PROVIDER_MUTE_REFUSALS = frozenset({"env_unavailable", "empty_answer"})


def _provider_structurally_mute(rv) -> bool:
    """Провайдер СТРУКТУРНО не дал ответа (среда недоступна / пустой ответ), а не «дал разбор без
    вердикта». `env-unavailable` несёт свой `stopped`; пустой ответ приходит `ProviderRefusal` с
    `reason` в _PROVIDER_MUTE_REFUSALS (run_review кладёт его в `stopped='refusal: <reason>'`)."""
    if rv.get("stopped") == "env-unavailable":
        return True
    return (rv.get("refusal") or {}).get("reason") in _PROVIDER_MUTE_REFUSALS


def _gate_ev_from_verdict(gid, g, rv, *, revision, delivered, work_root, valid_ids, signals,
                          calibrated_enforcement, ui_evidence):
    """Из ПРИНЯТОГО reviewer-result (`rv`) -> (gate_ev-запись, review-запись).

    Одна логика статуса и заземления для ОБОИХ путей — живого вердикта провайдера (`_run_reviews`) и
    потреблённого handoff-артефакта (`_consume_handoff_verdicts`), — чтобы artifact-first и живой
    вердикт не расходились. Заземление pass идёт тем же путём, что рубер-штамп (Fix C: 0 reads И ни
    одной цитаты, подтверждённой чтением ДОСТАВЛЕННОГО файла, -> блок). Извлечено из `_run_reviews`
    (07.09) РОВНО тем же телом: потребление вердикта нельзя было держать в двух разных местах."""
    from ai_ops_kit.checks import reviewer_result as vrr  # чистая проверка вниз (лента №5)
    req = g.get("required_evidence", []) or []
    res = rv.get("result")
    # issue #614: прозаический вердикт (`Recommendation: pass|needs_work`) приходит как reviewer-result
    # со `status`, но БЕЗ структурных checks. Пустой checks для него — норма, а не порок формы: структура
    # перестала быть условием вердикта. Заземление pass это НЕ ослабляет — рубер-штамп ниже держит.
    _prose = isinstance(res, dict) and bool(res.get("prose_verdict"))
    errs = (vrr.check(res, gate_ids=valid_ids, allow_empty_checks=_prose)
            if isinstance(res, dict) else ["ревьюер не вынес вердикт"])
    entry = {"gate": gid, "stopped": rv.get("stopped"), "reads": rv.get("reads"),
             "denied": rv.get("denied"), "valid": not errs, "source": rv.get("source"),
             "status": (res or {}).get("status") if not errs else None,
             "blockers": (res or {}).get("blockers") if isinstance(res, dict) else None,
             "errors": errs or None}
    if errs:
        # НЕ тихий пропуск. no-verdict -> НАЗВАННЫЙ отказ в gate_ev (взводит reviewer-blocked),
        # с причиной, которую человек может разобрать (находка поля P0, obs-2026-08-20).
        ref = gate_executor.evidence_from_no_verdict(
            g, gate_id=gid, stopped=rv.get("stopped"), reads=rv.get("reads"),
            errors=errs, refusal=rv.get("refusal"))
        entry["closed_as"] = "refused"
        entry["status"] = ref["status"]                       # fail/warn, не None
        entry["reason"] = (ref.get("blockers") or ref.get("warnings") or [None])[0]
        return ref, entry
    status = res.get("status")
    blocking = bool(g.get("blocking"))
    ev_ref = f"independent reviewer verdict @ {revision or 'HEAD'}"
    ev_status = "not_run"
    calib_ui = calibrated_enforcement and gid in gate_policy.UI_GATES
    if calib_ui and isinstance(ui_evidence, dict):
        ev_status = (ui_evidence.get(gid) or {}).get("deterministic_status", "not_run")
    # РУБЕР-ШТАМП ПЕРЕОПРЕДЕЛЁН ЧЕРЕЗ ЭТАЛОН, А НЕ ЧЕРЕЗ read-op СУДЬИ (Fix C для ревьюеров,
    # 01.09.2026). claude-cli судит из диффа и read-op детерминированно НЕ эмитит — поэтому
    # вовлечённость определяется тем, СВЕРЕН ли вердикт с ДОСТАВЛЕННЫМ файлом ЧТЕНИЕМ (P0
    # 04.09.2026): кит сам знает состав правки (`delivered`), САМ читает файл и подтверждает, что
    # процитированный диапазон строк в нём РЕАЛЕН. Рубер-штампом остаётся pass с И 0 reads, И ни
    # одной цитатой, подтверждённой чтением доставленного файла. Фабрикация «pass без ничего» — fail.
    if (status == "pass" and blocking and not rv.get("reads")
            and not _review_cites_delivered_file(res, delivered, work_root)):
        entry["closed_as"] = "blocked"
        entry["status"] = "fail"
        return ({"status": "fail",
                 "blockers": [f"reviewer вынес pass без единого чтения (0 reads) и ни одна "
                              f"цитата не подтверждена чтением доставленного файла "
                              f"(строки выдуманы/файл чужой) — рубер-штамп не закрывает "
                              f"блокирующий гейт @ {gid}; сверка с эталоном не доказана"],
                 "checks": res.get("checks", []), "evidence": [ev_ref]}, entry)
    if calib_ui and ev_status == "fail":
        entry["closed_as"] = "blocked"
        entry["status"] = "fail"
        entry["calibrated"] = "evidence_block"
        return ({"status": "fail",
                 "blockers": [f"детерминированное UI-evidence: реальная регрессия/дефект @ {gid} "
                              f"(evidence=fail) — блокирует независимо от вердикта ревьюера"],
                 "checks": res.get("checks", []), "evidence": [ev_ref]}, entry)
    if status == "fail" or (status == "warn" and blocking):
        if calib_ui:
            action, reason = gate_policy.effective_review_outcome(gid, signals, status, ev_status)
            if action == "advisory":
                entry["closed_as"] = "advisory"
                entry["status"] = "warn"
                entry["calibrated"] = reason
                return ({"status": "warn",
                         "warnings": [f"калибровка v3.1.8: {reason} (reviewer {status})"],
                         "checks": res.get("checks", []), "evidence": [ev_ref]}, entry)
        blockers = res.get("blockers") or (
            [f"reviewer WARN на блокирующем гейте — не чистый pass @ {gid}"] if status == "warn"
            else [f"reviewer FAIL @ {gid}"])
        entry["closed_as"] = "blocked"
        return ({"status": "fail", "blockers": blockers,
                 "checks": res.get("checks", []), "evidence": [ev_ref]}, entry)
    entry["closed_as"] = status
    return ({"status": status, "provided": list(req),
             "checks": res.get("checks", []), "evidence": [ev_ref]}, entry)


def _consume_handoff_verdicts(work_root, gate_ids, gate_ev, signals, revision, *, child_root=None,
                              calibrated_enforcement=False, ui_evidence=None):
    """Потребить ЗАПИСАННЫЕ оркестратором вердикты для ai-review гейтов БЕЗ вызова провайдера.

    #570 follow-up (живой прогон 07.09): artifact-first жил ТОЛЬКО внутри `_run_reviews`, а тот
    зовётся лишь при `--review` с живым провайдером. На штатном resume/reevaluate (0 model-вызовов,
    без `--review`) записанный вердикт не потреблялся, и code_review оставался «нет заключения
    reviewer», хотя `reviewer_handoff.load_verdict` тот же артефакт ПРИНИМАЕТ. Здесь вердикт-артефакт
    потребляется НЕЗАВИСИМО от `--review`: он читается, провайдера НЕ вызывает и ПЕРЕОПРЕДЕЛЯЕТ
    отсутствие/пустой no-verdict (а не проигрывает ему из-за порядка/скипа). Заземление pass идёт
    ТЕМ ЖЕ путём, что живой вердикт (`_gate_ev_from_verdict`) — 0 false-green и writer≠judge не
    ослаблены: устаревший-SHA / писательский / незаземлённый вердикт гейт не закроет."""
    from ai_ops_kit.engine import reviewer_handoff  # #160: handoff оркестратору (тот же слой engine)
    handoff_root = child_root or work_root
    gates = gate_executor.load_gates()
    valid_ids = set(gates)
    delivered = _delivered_files(work_root, revision)
    gate_ev = dict(gate_ev)
    reviews = []
    for gid in _reviewable_gates(gate_ids, signals):
        # Настоящий pass независимый вердикт не переспоривает; отсутствие/пустой no-verdict — да
        # (принцип: вердикт-артефакт оркестратора ПЕРЕОПРЕДЕЛЯЕТ пустой no-verdict).
        if (gate_ev.get(gid) or {}).get("status") == "pass":
            continue
        handoff_rr, _ = reviewer_handoff.load_verdict(handoff_root, gid, revision=revision)
        if handoff_rr is None:
            continue
        g = gates.get(gid) or {}
        rv = {"result": handoff_rr, "stopped": "handoff-artifact", "reads": [], "denied": [],
              "source": "handoff-artifact"}
        ev, entry = _gate_ev_from_verdict(
            gid, g, rv, revision=revision, delivered=delivered, work_root=work_root,
            valid_ids=valid_ids, signals=signals, calibrated_enforcement=calibrated_enforcement,
            ui_evidence=ui_evidence)
        gate_ev[gid] = ev
        reviews.append(entry)
    return gate_ev, reviews


# issue #614: чем просим ревьюера кода завершить на форс-ходе — ПРОСТОЙ строкой-итогом, а не тяжёлым
# структурным reviewer-result. Живой `claude -p` на реальном диффе тонул в 56с прозы под структурным
# JSON, а на этой строке заключал за 5с; `_last_prose_verdict` её ловит (make_reviewer_proposer
# синтезирует reviewer-result со status). Security-ревью остаётся на структурной форме: ему нужен
# domain_results, строкой его не заменить.
_REVIEWER_VERDICT_HINT = ("для needs_work/fail — РОВНО одну последнюю строку-итог "
                          "`Recommendation: needs_work`; для pass на блокирующем гейте — reviewer-result "
                          "с ХОТЯ БЫ одной цитатой evidence{file,lines} на изменённый файл "
                          "(прозаический pass без цитаты не заземлить, гейт не закроется)")


def _run_reviews(reviewer_proposer, work_root, gate_ids, gate_ev, signals, revision, budget,
                 max_reads=10, change_context=None,
                 calibrated_enforcement=False, ui_evidence=None, child_root=None):
    """Прогнать независимые ревью для ai-review гейтов плана, у которых ещё нет evidence.

    #160 (сессия Клода): перед вызовом провайдера смотрим, не оставил ли ОРКЕСТРАТОР валидный вердикт
    в handoff-артефакте на ТЕКУЩЕЙ ревизии (artifact-first) — тогда берём его БЕЗ вызова провайдера.
    А если провайдер ревьюера СТРУКТУРНО не дал ответа (среда недоступна ИЛИ вернул пусто) на
    БЛОКИРУЮЩЕМ гейте, тот не уходит в глухой no-verdict, а встаёт в `awaiting_reviewer` с записанным
    запросом на ревью. Handoff-артефакты живут под `child_root/.ai` (переживают пересборку worktree);
    при отсутствии child_root — под work_root. Решение по вердикту (форма/заземление/статус) вынесено
    в `_gate_ev_from_verdict` — общее с artifact-only-потреблением (`_consume_handoff_verdicts`)."""
    from ai_ops_kit.engine import reviewer_handoff  # #160: handoff оркестратору (тот же слой engine)
    handoff_root = child_root or work_root
    gates = gate_executor.load_gates()
    ro_policy = tool_broker.Policy(level="read-only", child_root=str(work_root))
    reviews = []
    gate_ev = dict(gate_ev)
    # СРЕЗ engine РАТЧЕТА 2026-08-12: здесь стоял `try: valid_ids = set(gates) except: valid_ids = None`.
    # Это была ФИКТИВНАЯ защита того же класса, что R-31/R-32: `set()` по словарю бросить не может,
    # а единственный путь к исключению (`gates: null` в реестре) молча превращал `valid_ids` в None —
    # и `vrr.check(gate_ids=None)` ПЕРЕСТАВАЛ проверять, существует ли гейт в quality/gates.yaml
    # (validate_reviewer_result.py:63 — проверка под `gate_ids is not None`). То есть подавление не
    # спасало от сбоя, а выключало проверку. Сам `load_gates()` выше не обёрнут и падает честно.
    valid_ids = set(gates)
    change_ctx = change_context if change_context is not None else _change_context(work_root, revision)
    # Fix C для ревьюеров: авторитетный состав доставленной правки (кит, не судья) — для заземления
    # вердикта, когда read-op не эмитится (claude-cli детерминированно его не даёт).
    delivered = _delivered_files(work_root, revision)
    for gid in _reviewable_gates(gate_ids, signals):
        if gid in gate_ev:
            continue
        g = gates.get(gid) or {}
        req = g.get("required_evidence", []) or []
        # Критерии ревьюера (#1183): для code_review — статьи конституции + находки конформанса по
        # ДОСТАВЛЕННЫМ файлам проверяемого дерева. Один текст на живой промпт и на handoff-запрос.
        # Состав правки неизвестен (нет ревизии) -> находок не просим: «нет находок» было бы ложью.
        checklist = _gate_checklist(g, root=work_root, changed_files=delivered or None)
        # ARTIFACT-FIRST (#160 handoff): валидный вердикт оркестратора на ТЕКУЩЕМ SHA -> берём его БЕЗ
        # вызова провайдера. Валидность (форма + gate + reviewed_revision==revision + writer≠judge)
        # проверяет load_verdict; ЗАЗЕМЛЕНИЕ pass идёт в _gate_ev_from_verdict тем же путём, что
        # живой вердикт, поэтому здесь не дублируется и не расходится.
        handoff_rr, _ = reviewer_handoff.load_verdict(handoff_root, gid, revision=revision)
        if handoff_rr is not None:
            rv = {"result": handoff_rr, "stopped": "handoff-artifact", "reads": [], "denied": [],
                  "source": "handoff-artifact"}
        else:
            reviewer = tool_loop.make_reviewer_proposer(
                reviewer_proposer, gid, checklist=checklist,
                required_evidence=req, reviewed_revision=revision)
            rv = tool_loop.run_review(reviewer, work_root, ro_policy, gid, budget=budget,
                                      max_reads=max_reads, base_context=change_ctx,
                                      required_evidence=req, reviewed_revision=revision,
                                      verdict_hint=_REVIEWER_VERDICT_HINT)
            # HANDOFF-OPEN (#160 + follow-up 07.09): провайдер ревьюера СТРУКТУРНО не дал ответа
            # (среда недоступна ИЛИ пустой ответ) на БЛОКИРУЮЩЕМ гейте -> НЕ глухой no-verdict, а
            # awaiting_reviewer: пишем запрос на ревью, гейт остаётся блокирующим (fail), но ОТЛИЧИМ —
            # прогон не падает и оркестратор знает, что заполнить. resume перечитает вердикт.
            # Только БЛОКИРУЮЩИЙ гейт: advisory-гейт всё равно не блокирует, ему handoff не нужен (и
            # это сохраняет named refusal у ux_review — «дал разбор без вердикта» не станет awaiting).
            if _provider_structurally_mute(rv) and bool(g.get("blocking")):
                # причина едет в handoff человеку/оркестратору: пустой ответ провайдера ≠ недоступность
                # среды, и подменять одну формулировкой другой значило бы врать о причине.
                cause = ("исполнитель ревьюера недоступен в этой среде (сессия Клода, #160)"
                         if rv.get("stopped") == "env-unavailable"
                         else (rv.get("refusal") or {}).get("reason_text")
                         or "провайдер ревьюера вернул пустой ответ (вердикта нет)")
                aw = reviewer_handoff.open_request(
                    handoff_root, gid, checklist=checklist, reviewed_revision=revision,
                    changed_files=delivered, blocking=True, required_evidence=req, cause=cause)
                gate_ev[gid] = aw
                # entry.status="awaiting_reviewer" (НЕ "fail"): гейт-evidence блокирует (fail), но
                # это ОЖИДАНИЕ ревью, а не отрицательный вердикт судьи — иначе _hard_stop счёл бы это
                # reviewer-blocked и остановил бы цепочку.
                reviews.append({"gate": gid, "stopped": rv.get("stopped"), "reads": rv.get("reads") or [],
                                "denied": rv.get("denied"), "valid": False, "source": "handoff-request",
                                "status": "awaiting_reviewer", "closed_as": "awaiting_reviewer",
                                "reason": (aw.get("blockers") or aw.get("warnings") or [None])[0]})
                continue
        ev, entry = _gate_ev_from_verdict(
            gid, g, rv, revision=revision, delivered=delivered, work_root=work_root,
            valid_ids=valid_ids, signals=signals, calibrated_enforcement=calibrated_enforcement,
            ui_evidence=ui_evidence)
        gate_ev[gid] = ev
        reviews.append(entry)
    return gate_ev, reviews


def _review_security(reviewer_proposer, work_root, pack_result, revision, budget, change_context=None):
    """v2.106: независимый security-reviewer выносит вердикт по needs_review доменам."""
    from ai_ops_kit.security import security_pack
    from ai_ops_kit.checks import reviewer_result as vrr  # чистая проверка вниз (лента №5)
    ro_policy = tool_broker.Policy(level="read-only", child_root=str(work_root))
    domains = {d["id"]: d for d in security_pack.load_domains()[0]}
    applicable = list(pack_result.get("needs_review", []) or [])
    checklist_items = []
    for did in applicable:
        checklist_items += (domains.get(did, {}).get("reviewer_checklist") or [])
    checklist_items.append(
        "ОБЯЗАТЕЛЬНО верни в reviewer-result поле domain_results — список "
        "{domain:<id>, status:pass|warn|fail, checks:[{id,status}], evidence:[{type,path,lines|command}]} "
        "РОВНО по этим применимым доменам: " + ", ".join(applicable) + " (по одному на каждый, без "
        "пропусков/дублей/лишних). У КАЖДОГО домена СВОИ непустые checks; для pass — хотя бы одна КОНКРЕТНАЯ "
        "evidence-ссылка. ФОРМАТ evidence.type — СТРОГО одно из: 'code-read' (прочитанный файл: path + lines), "
        "'test' (command), 'finding' (id/detail сканера). Для ссылки на код используй type:'code-read' "
        "(НЕ 'file'/'source'). Для warn/fail — непустые blockers")
    checklist = "; ".join(checklist_items)
    reviewer = tool_loop.make_reviewer_proposer(
        reviewer_proposer, "security", checklist=checklist, required_evidence=["security_reviewer"])
    rv = tool_loop.run_review(
        reviewer, work_root, ro_policy, "security", budget=budget,
        base_context=(change_context if change_context is not None else _change_context(work_root, revision)),
        required_evidence=["security_reviewer"], reviewed_revision=revision)
    res = rv.get("result")
    observed = list(rv.get("reads") or [])
    if revision:
        from ai_ops_kit.engine.pipeline_git import _git
        _rc, _names, _ = _git(work_root, "show", "--name-only", "--format=", revision)
        if _rc == 0:
            observed += [ln.strip() for ln in _names.splitlines() if ln.strip()]
    errs = _security_verdict_errors(res, revision, applicable, vrr, reviewer_reads=observed)
    if errs:
        return None, {"status": (res or {}).get("status"), "invalid": errs, "raw": res}
    return (res or {}).get("status"), res


def _human_approval_domains_uncovered(approval_root, wid, changed_files, diff_root=None):
    """v3.0-rc20/rc3.0.2 (finding аудита P0/P1): домены с непустыми human_approval_conditions, чьи
    file_patterns СОВПАЛИ с РЕАЛЬНО изменёнными путями, ОБЯЗАНЫ иметь валидный человеческий ApprovalRecord."""
    import re as _re
    from ai_ops_kit.security import security_pack
    from ai_ops_kit.gates import approvals as _appr
    _CATCH_ALL = {".*", ".+", "", "^.*$", "(?s).*"}
    triggered = {}
    try:
        for d in security_pack.load_domains()[0]:
            if not d.get("human_approval_conditions"):
                continue
            pats = [p for p in ((d.get("applicability", {}) or {}).get("file_patterns") or [])
                    if p.strip() not in _CATCH_ALL]
            if not pats:
                continue
            matched = [f for f in (changed_files or []) if any(_re.search(p, f) for p in pats)]
            if matched:
                triggered[d["id"]] = matched
    except Exception:  # noqa: BLE001
        return sorted(set((changed_files and ["<security-domains-load-failed>"]) or []))
    if not triggered:
        return []
    # writer≠judge как ПРОВЕРКА, а не следствие изоляции (аудит F2). ApprovalRecord'ы лежат вне git
    # (`features/` в .gitignore), поэтому доверие к ним держалось только на том, ГДЕ их читают: на
    # `--execute` writer заперт в отдельном worktree (child_root/.ai/worktrees/<wid>), а approval-store
    # (child_root/features/<wid>/approvals) — ВНЕ его дерева, писать туда он не может. Но при work_root
    # == child_root (isolate=False / библиотечный путь) store оказывается ВНУТРИ writable-дерева
    # писателя: он мог создать себе одобрение сам за этот прогон. Такой store не доверяется — fail-closed:
    # все триггернутые домены остаются uncovered (человек обязателен), сколько бы «валидных» записей там
    # ни лежало. Разделение store и дерева писателя проверяется явно, а не подразумевается настройкой.
    if diff_root is not None:
        store = Path(approval_root) / "features" / str(wid) / "approvals"
        try:
            if store.resolve().is_relative_to(Path(diff_root).resolve()):
                return sorted(triggered)
        except (OSError, ValueError):
            return sorted(triggered)   # не смогли доказать разделение store и дерева писателя -> fail-closed
    try:
        recs = _appr.load_approvals(approval_root, wid)
        now = _appr._now_iso()
        plan_hash = _appr.plan_binding_hash(approval_root, wid)
    except Exception:  # noqa: BLE001
        return sorted(triggered)
    uncovered = []
    for dom, files in sorted(triggered.items()):
        rec = next((r for r in recs if r.get("approval") == dom), None)
        try:
            ok = (rec is not None
                  and _appr._record_valid(rec, now=now, plan_hash=plan_hash, strict=True)
                  and _appr.covers_paths(rec, files))
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            uncovered.append(dom)
    return uncovered

# Запуск скриптом ОБЪЯСНЯЕТ модуль, а не молчит (ревизия 2026-08-11).
#
# Здесь стояло `sys.exit(selftest())`, а сама функция удалена в v3.30 вместе с переносом
# селфтестов в pytest: любой запуск падал с `NameError`. Просто убрать блок — тоже неверно:
# модуль остаётся запускаемой точкой входа (`python3 -m ai_ops_kit.engine.pipeline_evidence`), и молчаливый
# выход с кодом 0 — тот самый дефект «ноль и есть симптом».
# Поэтому вход делает осмысленную работу — печатает назначение модуля, как `invariants.py`.
# Проверки модуля — в `tests/unit/`.
if __name__ == "__main__":
    print(__doc__)
    print("Проверки этого модуля — в tests/unit/ (pytest), отдельного --selftest нет с v3.30.")
