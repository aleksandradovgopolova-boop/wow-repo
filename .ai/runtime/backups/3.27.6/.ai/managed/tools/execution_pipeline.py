#!/usr/bin/env python3
"""Единый execution-pipeline (v2.58, P0-эпик) — СБОРКА исполнения в один движок.

Аудит: компоненты есть, но не собраны; generic-путь гонял doc-оркестратор, а не tool-loop.
Этот модуль соединяет уже построенные части в ОДНУ цепочку:

  detect (RepositoryProfile) -> tool-loop (модель предлагает, Policy решает, Broker исполняет,
  результат в контекст) -> evidence collector (реальный прогон build/lint/typecheck/test через
  Broker) -> RunPlan-гейты (base_workflow + треки) -> единый отчёт.

Честная граница (НЕ имитируется): commit + reverify на точном SHA и открытие draft PR — ещё НЕ
здесь (нужен git-commit шаг и живой прогон); pipeline доводит до «изменения применены + evidence
собран + гейты оценены». Механика детерминирована и тестируется offline mock-предложителем;
живой предложитель — swap провайдера (как tool_loop.make_model_proposer).

Использование (программно):
  run_pipeline(task, signals, child_root, proposer, policy, budget, max_steps) -> отчёт.
  execution_pipeline.py --selftest
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
for _p in (PKG / "tools", PKG / "validation"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import project_detector      # noqa: E402
import tool_loop             # noqa: E402
import tool_broker           # noqa: E402
import evidence_collector    # noqa: E402
import run_plan              # noqa: E402
import gate_executor         # noqa: E402
import gate_policy           # noqa: E402  (v3.1.8 калиброванное UI-enforcement)
import storybook_adapter     # noqa: E402  (v3.1.9 exact-SHA UI evidence)


def _profile_summary(profile):
    stacks = profile.get("stacks") or []
    langs = ", ".join(s.get("language", "?") for s in stacks) or "не определён"
    cmds = {}
    for s in stacks:
        for k, v in (s.get("commands") or {}).items():
            if v and k not in cmds:
                cmds[k] = v
    return f"Стек: {langs}. Команды проверки: {cmds or 'нет'}."


def _intake_evidence(signals):
    """intake_completeness evidence из сигналов: классификация уже сделана (реальный evidence,
    не фабрикация). Маппинг сигнал->required_evidence-флаг; provided только для присутствующих."""
    sig = signals or {}
    mapping = {"classified_type": "task_type", "size": "size", "risk": "risk"}
    provided = [flag for flag, key in mapping.items() if sig.get(key)]
    if not provided:
        return None
    return {"status": "pass", "provided": provided,
            "evidence": [f"intake из сигналов: {', '.join(provided)}"]}


# v2.85 (finding аудита): гейты, которые НЕЛЬЗЯ закрывать автоматическим ревьюером той же модели —
# слишком консеквентны для self-attestation. Даже когда classify=ai-review (нет спец-сигналов),
# security/red-team требуют ЛИБО настоящей независимости (другая модель/человек), ЛИБО остаются
# блокирующими. Иначе security-ревью деградирует до «сам себя проверил».
NO_SELF_REVIEW = {"security", "ai_red_team"}


def _reviewable_gates(gate_ids, signals):
    """v2.83/2.85: гейты плана, которые НЕЗАВИСИМЫЙ ревьюер той же модели может закрыть легитимно —
    только ai-review (writer ≠ judge), И НЕ из NO_SELF_REVIEW. Детерминированные гейты с валидатором
    (requirements/specification/plan_readiness) НЕ закрываются словом ревьюера — им нужны артефакты и
    запускаемые валидаторы. security/ai_red_team не отдаём self-review — нужна настоящая
    независимость/человек; они честно остаются блокирующими."""
    gates = gate_executor.load_gates()
    out = []
    for gid in gate_ids:
        if gid in NO_SELF_REVIEW:
            continue
        g = gates.get(gid) or {}
        if gate_executor.classify(g, signals) == "ai-review":
            out.append(gid)
    return out


def _gate_checklist(gate):
    """Короткий чек-лист для ревьюера: required_evidence + ответственная роль. (Тела правил в
    rules/ доступны ревьюеру через read; здесь — компактный ориентир, не весь файл.)"""
    req = gate.get("required_evidence", []) or []
    role = gate.get("responsible_role", "reviewer")
    parts = [f"роль: {role}"]
    if req:
        parts.append("подтверди по факту: " + ", ".join(req))
    return "; ".join(parts)


def _resolve_base(root, base_ref):
    """v3.0.2/v3.0.7 (finding аудита P0): разрешение base-ветки. ТОЛЬКО ветка (локальная/origin), не tag/SHA.

    v3.0.7 BaseResolver v3 — два режима:
    * base_ref=None -> AUTO: upstream текущей ветки (@{u}) -> remote default (origin/HEAD) -> текущая
      ветка. auto ВСЕГДА разрешается (в пределе — текущая ветка), никакого хардкода 'main'.
    * base_ref задан -> EXPLICIT: обязана существовать (refs/heads/<ref> или origin/<ref>); иначе
      resolved=False (вызывающий обязан заблокировать прогон ДО модели — не выполнять от HEAD).
    -> {base_ref, base_sha, source, mode, resolved, reason}."""
    if _git(root, "rev-parse", "--is-inside-work-tree")[0] != 0:
        return {"base_ref": base_ref, "resolved": False, "mode": "explicit" if base_ref else "auto",
                "reason": "не git-репозиторий"}
    if base_ref:   # EXPLICIT — строго ветка
        rc_l, sha_l, _ = _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{base_ref}")
        if rc_l == 0 and (sha_l or "").strip():
            return {"base_ref": base_ref, "base_sha": sha_l.strip(), "source": "explicit-local",
                    "mode": "explicit", "resolved": True}
        rc_r, sha_r, _ = _git(root, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base_ref}")
        if rc_r == 0 and (sha_r or "").strip():
            return {"base_ref": base_ref, "base_sha": sha_r.strip(), "source": "explicit-remote",
                    "mode": "explicit", "resolved": True}
        return {"base_ref": base_ref, "resolved": False, "mode": "explicit",
                "reason": f"явная base '{base_ref}' не найдена ни локально (refs/heads), ни в origin"}
    # AUTO: upstream -> remote default -> текущая ветка
    rc_u, up, _ = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if rc_u == 0 and (up or "").strip():
        ref = up.strip()
        rc_s, sha, _ = _git(root, "rev-parse", "--verify", "--quiet", ref)
        if rc_s == 0 and (sha or "").strip():
            br = ref.split("origin/", 1)[1] if ref.startswith("origin/") else ref
            return {"base_ref": br, "base_sha": sha.strip(), "source": "upstream",
                    "mode": "auto", "resolved": True}
    rc_d, dref, _ = _git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if rc_d == 0 and (dref or "").strip():
        rc_s, sha, _ = _git(root, "rev-parse", "--verify", "--quiet", dref.strip())
        if rc_s == 0 and (sha or "").strip():
            br = dref.strip().split("refs/remotes/origin/", 1)[-1]
            return {"base_ref": br, "base_sha": sha.strip(), "source": "remote-default",
                    "mode": "auto", "resolved": True}
    rc_c, cur, _ = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    rc_h, head, _ = _git(root, "rev-parse", "--verify", "--quiet", "HEAD")
    if rc_c == 0 and (cur or "").strip() and rc_h == 0 and (head or "").strip():
        return {"base_ref": cur.strip(), "base_sha": head.strip(), "source": "current-branch",
                "mode": "auto", "resolved": True}
    return {"base_ref": None, "resolved": False, "mode": "auto",
            "reason": "не удалось определить base автоматически (нет upstream/remote-default/HEAD)"}


def _verify_remote_base(root, base_ref, base_sha):
    """v3.0.9 (finding аудита P0): ЕДИНЫЙ fail-closed верификатор remote base для доставки (single-run И
    sequential — один контракт доверия). -> {verdict, remote_sha, reason}, где verdict:
      'verified-equal'  — remote refs/heads/<base_ref> == base_sha -> можно открывать PR;
      'verified-moved'  — существует, но SHA разошёлся -> нужна ревалидация (PR не открывать);
      'unverifiable'    — нет origin/сети/ветки/ошибка ls-remote -> доставка НЕДОСТУПНА (НЕ «успех
                          по умолчанию»; отсутствие проверки != пройденная проверка)."""
    if not (base_ref and base_sha):
        return {"verdict": "unverifiable", "reason": "нет base_ref/base_sha для сверки"}
    try:
        rc, out, err = _git(root, "ls-remote", "origin", f"refs/heads/{base_ref}")
    except Exception as e:  # noqa: BLE001
        return {"verdict": "unverifiable", "reason": f"ls-remote исключение: {e}"}
    if rc != 0:
        return {"verdict": "unverifiable", "reason": f"ls-remote rc={rc}: {(err or '').strip()[:120]}"}
    line = (out or "").strip()
    if not line:
        return {"verdict": "unverifiable", "reason": f"remote-ветка refs/heads/{base_ref} не найдена в origin"}
    remote_sha = line.split()[0].strip()
    if remote_sha == base_sha:
        return {"verdict": "verified-equal", "remote_sha": remote_sha}
    return {"verdict": "verified-moved", "remote_sha": remote_sha}


def _deliver_pr(work_root, work_branch, base_ref, base_sha, base_binding, committed_sha, wid, task,
                delivery_id=None):
    """v3.0.15/v3.0.16 (finding аудита P0/#1): доверенная доставка draft PR — единственная точка открытия
    PR. Fail-closed по remote base (verified-equal -> PR; unverifiable/moved -> НЕ открываем). Вызывается
    ИСКЛЮЧИТЕЛЬНО транзакционным контроллером (ai_ops_run) ПОСЛЕ durable-фиксации RunHandoff+final report+
    journal+DeliveryIntent. run_pipeline НИКОГДА не вызывает эту функцию (только возвращает DeliveryPlan) —
    так прямой вызов pipeline не может обойти lifecycle-барьер. Идемпотентно (pr_open находит существующий
    PR ветки и возвращает 'updated', не создавая дубль). -> delivery dict."""
    delivery = {"requested": True, "base_binding": base_binding}
    if not base_binding.get("resolved") or not base_sha:
        delivery.update(status="unavailable",
                        reason=f"base '{base_ref}' не разрешилась в ветку: {base_binding.get('reason')} "
                               "— PR к произвольному HEAD не открываем")
        return delivery
    _rv = _verify_remote_base(work_root, base_ref, base_sha)
    if _rv.get("verdict") == "unverifiable":
        delivery.update(status="unavailable",
                        reason=f"remote-base-unverified: {_rv.get('reason')} — доставка невозможна fail-closed")
        return delivery
    if _rv.get("verdict") == "verified-moved":
        delivery.update(status="not-attempted",
                        reason=f"remote base сдвинулась (validated {base_sha[:12]} != remote "
                               f"{(_rv.get('remote_sha') or '?')[:12]}) — нужна ревалидация; PR не открыт")
        return delivery
    import pr_open
    pr = pr_open.open_draft_pr(work_root, work_branch, title=f"ai-ops: {task[:60]}", base=base_ref,
                               body=f"Автопрогон AI Ops. WorkItem: {wid}. База {base_ref} "
                                    f"({base_sha[:12]}) → evidence на {committed_sha}.",
                               delivery_id=delivery_id)
    delivery.update(status=(pr or {}).get("status"), pr=pr)
    return delivery


def _change_context(work_root, revision, max_chars=12000):
    """v3.0-rc9 (finding живого прогона kimi): детерминированно собрать КОНТЕКСТ ИЗМЕНЕНИЯ для
    независимого ревьюера — полный список изменённых файлов (`git show --stat`, всегда целиком) +
    ограниченный по размеру unified-дифф ревизии.

    Корень finding: `run_review` вызывался с пустым base_context — ревьюеру НЕ давали ни диффа, ни
    списка изменённых файлов. Прилежная модель (kimi) честно возвращала fail: «контекст изменения
    пуст — не могу подтвердить факт ревью именно этой ревизии», сделав 0 чтений. Это делало
    code_review структурно НЕпроходимым независимо от качества модели (ложный блокер positive-green).

    Даём ревьюеру ровно то, что видит человек-ревьюер в PR (дифф + список файлов) — НЕ вердикт.
    Ревьюер всё равно сам читает файлы (read-only) для верификации; его reads и есть evidence.
    writer≠judge сохранён: отдельный вызов, read-only Policy, собственный вердикт по фактам."""
    if not revision:
        return ""
    rc, stat, _ = _git(work_root, "show", "--stat", "--format=", revision)
    if rc != 0:
        return ""                                   # не git / ревизия недоступна -> прежнее поведение
    parts = [f"Изменённые файлы (git show --stat @ {revision[:12]}):",
             (stat.strip() or "(список пуст)")]
    rc2, diff, _ = _git(work_root, "show", "--format=", "--unified=3", revision)
    if rc2 == 0 and (diff or "").strip():
        body = diff.strip()
        if len(body) > max_chars:
            body = (body[:max_chars] + f"\n... [дифф усечён на {max_chars} симв.; полный список файлов "
                    "выше — читай их целиком через {\"op\":\"read\"} для верификации]")
        parts.append("\nUnified-дифф ревизии:\n" + body)
    return "\n".join(parts) + "\n"


def _change_context_range(work_root, base_revision, head_revision, max_chars=14000):
    """v3.0-rc16 (finding аудита P0): контекст ВСЕЙ последовательности base..head для AGGREGATE-ревью.
    `_change_context` показывает `git show <head>` — только ПОСЛЕДНИЙ коммит; для 3 пакетов aggregate-
    судья видел лишь дифф пакета 3 (риск взаимодействия пакет1↔пакет3 пропускался). Здесь — интегрированный
    дифф base..head: `git diff --stat`, полный список файлов, ограниченный combined diff, хэши коммитов
    диапазона. Ревьюер получает всю транзакцию, вердикт связывается и с base, и с head."""
    if not (base_revision and head_revision):
        return _change_context(work_root, head_revision, max_chars=max_chars)   # деградация -> одиночная ревизия
    rng = f"{base_revision}..{head_revision}"
    rc, stat, _ = _git(work_root, "diff", "--stat", rng)
    if rc != 0:
        return _change_context(work_root, head_revision, max_chars=max_chars)
    parts = [f"ИНТЕГРИРОВАННЫЙ дифф последовательности {base_revision[:12]}..{head_revision[:12]}:",
             "git diff --stat:", (stat.strip() or "(пусто)")]
    rc_l, commits, _ = _git(work_root, "log", "--oneline", "--no-decorate", rng)
    if rc_l == 0 and commits.strip():
        parts.append("\nКоммиты диапазона (по пакетам):\n" + commits.strip())
    rc2, diff, _ = _git(work_root, "diff", "--unified=3", rng)
    if rc2 == 0 and (diff or "").strip():
        body = diff.strip()
        if len(body) > max_chars:
            body = (body[:max_chars] + f"\n... [combined-дифф усечён на {max_chars} симв.; полный список "
                    "файлов выше — читай целиком через {\"op\":\"read\"} для верификации]")
        parts.append("\nCombined unified-дифф base..head:\n" + body)
    return "\n".join(parts) + "\n"


def _run_reviews(reviewer_proposer, work_root, gate_ids, gate_ev, signals, revision, budget,
                 max_reads=10, change_context=None,   # rc10: многофайловый дифф; rc16: override контекста
                 calibrated_enforcement=False, ui_evidence=None):   # v3.1.8 калиброванное UI-enforcement
    """Прогнать независимые ревью для ai-review гейтов плана, у которых ещё нет evidence.
    Возвращает (обновлённый gate_ev, список трейсов ревью). Ревьюер гоняется под READ-ONLY
    политикой (capability-независимость от писателя). Вердикт валидируется по reviewer-result
    и кладётся в gate_ev; невынесенный вердикт -> гейт остаётся неподтверждённым (честный fail)."""
    import validate_reviewer_result as vrr
    gates = gate_executor.load_gates()
    ro_policy = tool_broker.Policy(level="read-only", child_root=str(work_root))
    reviews = []
    gate_ev = dict(gate_ev)
    valid_ids = None
    try:
        valid_ids = set(gates)
    except Exception:
        valid_ids = None
    # rc9: seed reviewer диффом; rc16: aggregate передаёт range-контекст base..final (вся цепочка)
    change_ctx = change_context if change_context is not None else _change_context(work_root, revision)
    for gid in _reviewable_gates(gate_ids, signals):
        if gid in gate_ev:                     # evidence уже есть (напр. из reviewer-артефактов)
            continue
        g = gates.get(gid) or {}
        req = g.get("required_evidence", []) or []
        reviewer = tool_loop.make_reviewer_proposer(
            reviewer_proposer, gid, checklist=_gate_checklist(g),
            required_evidence=req, reviewed_revision=revision)
        rv = tool_loop.run_review(reviewer, work_root, ro_policy, gid, budget=budget,
                                  max_reads=max_reads, base_context=change_ctx,
                                  required_evidence=req, reviewed_revision=revision)
        res = rv.get("result")
        errs = vrr.check(res, gate_ids=valid_ids) if isinstance(res, dict) else ["ревьюер не вынес вердикт"]
        entry = {"gate": gid, "stopped": rv.get("stopped"), "reads": rv.get("reads"),
                 "denied": rv.get("denied"), "valid": not errs,
                 "status": (res or {}).get("status") if not errs else None,
                 # v3.1.1 (fix-loop): выносим blockers/checks ревьюера в трейс -> контроллер feed'ит
                 # КОНКРЕТНЫЕ замечания писателю на итерацию (не общий «устрани замечания»).
                 "blockers": (res or {}).get("blockers") if isinstance(res, dict) else None,
                 "errors": errs or None}
        reviews.append(entry)
        if errs:
            continue                            # невалидный/пустой вердикт -> гейт не закрываем
        status = res.get("status")
        blocking = bool(g.get("blocking"))
        ev_ref = f"independent reviewer verdict @ {revision or 'HEAD'}"
        # v3.1.8: детерминированный статус UI-evidence для этого гейта (не_run, если evidence нет/выкл).
        ev_status = "not_run"
        calib_ui = calibrated_enforcement and gid in gate_policy.UI_GATES
        if calib_ui and isinstance(ui_evidence, dict):
            ev_status = (ui_evidence.get(gid) or {}).get("deterministic_status", "not_run")
        # v3.0.11 (finding аудита P2): симметрия с security-путём — БЛОКИРУЮЩИЙ ai-review гейт нельзя
        # закрыть pass-вердиктом БЕЗ единого чтения (рубер-стамп). Ревьюеру показан дифф в base_context,
        # но «увидел в контексте» != «проверил»: для чистого pass на блокирующем гейте требуем ≥1 read.
        # warn/fail пропускаем (они и так блокируют/не закрывают); неблокирующие — как раньше.
        if status == "pass" and blocking and not (rv.get("reads")):
            gate_ev[gid] = {"status": "fail",
                            "blockers": [f"reviewer вынес pass без единого чтения (0 reads) — рубер-стамп "
                                         f"не закрывает блокирующий гейт @ {gid}; требуется верификация чтением"],
                            "checks": res.get("checks", []), "evidence": [ev_ref]}
            entry["closed_as"] = "blocked"
            entry["status"] = "fail"
            continue
        # v3.1.8 SAFETY: детерминированное evidence показывает РЕАЛЬНУЮ регрессию/дефект -> блок ВСЕГДА,
        # даже если ревьюер вынес pass. Это УСИЛЕНИЕ (может только добавить блок), не ослабление.
        if calib_ui and ev_status == "fail":
            gate_ev[gid] = {"status": "fail",
                            "blockers": [f"детерминированное UI-evidence: реальная регрессия/дефект @ {gid} "
                                         f"(evidence=fail) — блокирует независимо от вердикта ревьюера"],
                            "checks": res.get("checks", []), "evidence": [ev_ref]}
            entry["closed_as"] = "blocked"
            entry["status"] = "fail"
            entry["calibrated"] = "evidence_block"
            continue
        if status == "fail" or (status == "warn" and blocking):
            # v3.1.8: КАЛИБРОВАННОЕ enforcement для UI-гейтов. Ревьюерский warn (субъективное сомнение)
            # НЕ блокирует, когда гейт advisory (internal low-risk не-safety) ИЛИ механика подтверждена
            # детерминированным evidence (evidence=pass). Жёсткий reviewer FAIL и evidence=fail -> block
            # (effective_review_outcome вернёт 'block'). accessibility в internal остаётся blocking.
            if calib_ui:
                action, reason = gate_policy.effective_review_outcome(gid, signals, status, ev_status)
                if action == "advisory":
                    gate_ev[gid] = {"status": "warn",
                                    "warnings": [f"калибровка v3.1.8: {reason} (reviewer {status})"],
                                    "checks": res.get("checks", []), "evidence": [ev_ref]}
                    entry["closed_as"] = "advisory"
                    entry["status"] = "warn"
                    entry["calibrated"] = reason
                    continue
            # v2.85 (finding аудита): reviewer `warn` на БЛОКИРУЮЩЕМ гейте раньше тихо закрывал его
            # (evaluate требует required_evidence только для pass). warn — это «есть сомнения», НЕ
            # чистый pass -> для блокирующего гейта это блок, а не молчаливое прохождение.
            blockers = res.get("blockers") or (
                [f"reviewer WARN на блокирующем гейте — не чистый pass @ {gid}"] if status == "warn"
                else [f"reviewer FAIL @ {gid}"])
            gate_ev[gid] = {"status": "fail", "blockers": blockers,
                            "checks": res.get("checks", []), "evidence": [ev_ref]}
            entry["closed_as"] = "blocked"
        else:
            # ai-review pass (или warn на НЕблокирующем): судья И ЕСТЬ evidence -> required_evidence
            # предоставлен (та же дисциплина, что gate_executor.collect_evidence для ai-review).
            gate_ev[gid] = {"status": status, "provided": list(req),
                            "checks": res.get("checks", []), "evidence": [ev_ref]}
            entry["closed_as"] = status
    return gate_ev, reviews


def _review_security(reviewer_proposer, work_root, pack_result, revision, budget, change_context=None):
    """v2.106: независимый security-reviewer выносит вердикт по needs_review доменам (writer≠judge,
    read-only, отдельный провайдер). -> (status|None, result). Закрывает то, что детерминированный
    сканер не может (no_injection_surface и т.п.), НО только по чек-листам применимых доменов.

    v3.0-rc16 (finding аудита P0): вердикт ВАЛИДИРУЕТСЯ, как и обычные ревью-гейты — раньше брали
    result.status «как есть», и модель могла вернуть {"status":"pass"} без checks/revision/обоснования
    -> false green (особенно на aggregate: needs_review -> clear). Теперь: schema-валидатор
    (validate_reviewer_result) + security-specific: gate==security, reviewed_revision==revision,
    непустые checks, КАЖДЫЙ применимый домен отражён в checks. Невалидный «pass» -> status=None
    (гейт остаётся needs_review/fail). change_context (rc16): aggregate передаёт range base..final."""
    import security_pack
    import validate_reviewer_result as vrr
    ro_policy = tool_broker.Policy(level="read-only", child_root=str(work_root))
    domains = {d["id"]: d for d in security_pack.load_domains()[0]}
    applicable = list(pack_result.get("needs_review", []) or [])
    checklist_items = []
    for did in applicable:
        checklist_items += (domains.get(did, {}).get("reviewer_checklist") or [])
    # v3.0.1 (finding аудита P0): SecurityVerdict v2 — требуем ОТДЕЛЬНЫЙ вердикт по КАЖДОМУ применимому
    # домену. Один общий check не доказывает 4 разных домена. Ревьюер обязан вернуть в reviewer-result
    # поле domain_results:[{domain,status}], покрывающее РОВНО применимые домены.
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
    # rc16: валидируем вердикт — иначе принимаем false-green. Невалидный -> НЕ pass.
    # v3.0.10 (finding аудита P1): observable surface ревьюера = файлы, которые он РЕАЛЬНО читал (rv.reads)
    # ∪ файлы, показанные ему в диффе ревизии. code-read evidence обязана ссылаться на файл из этой
    # поверхности — ссылка на непрочитанный/непоказанный файл = фабрикация (не пройдёт).
    observed = list(rv.get("reads") or [])
    if revision:
        _rc, _names, _ = _git(work_root, "show", "--name-only", "--format=", revision)
        if _rc == 0:
            observed += [ln.strip() for ln in _names.splitlines() if ln.strip()]
    errs = _security_verdict_errors(res, revision, applicable, vrr, reviewer_reads=observed)
    if errs:
        return None, {"status": (res or {}).get("status"), "invalid": errs, "raw": res}
    return (res or {}).get("status"), res


def _evidence_ref_errors(dom, ev_items, reviewer_reads=None):
    """v3.0.10 (finding аудита P1): evidence домена — СТРУКТУРНЫЕ ссылки (EvidenceRef), а не строка вроде
    'checked'. Распознаём type: code-read (path[+lines]) | test (command) | finding/scanner (id|detail|path).
    Если доступен РЕАЛЬНЫЙ trace ревьюера (reviewer_reads — список прочитанных путей), code-read ОБЯЗАН
    ссылаться на файл, который ревьюер ДЕЙСТВИТЕЛЬНО читал (иначе ссылка сфабрикована). -> список ошибок."""
    errs = []
    if not (isinstance(ev_items, list) and ev_items):
        return [f"домен '{dom}': пустой/неструктурный список evidence"]
    reads = reviewer_reads if isinstance(reviewer_reads, list) else None

    def _read_match(path):
        # v3.0.11 (finding аудита P2): суффикс/точное совпадение пути, БЕЗ bare-basename — иначе прочитанный
        # tests/config.py «закрывал» ссылку на src/prod/config.py (разные файлы, одно имя = не доказательство).
        p = str(path).strip().replace("\\", "/")
        for r in reads:
            rr = str(r or "").strip().replace("\\", "/")
            if not rr:
                continue
            if rr == p or rr.endswith("/" + p) or p.endswith("/" + rr):
                return True
        return False

    for ev in ev_items:
        if not isinstance(ev, dict):
            errs.append(f"домен '{dom}': evidence '{ev}' не структурная ссылка "
                        "(нужен {type, path/command/...}, не строка)")
            continue
        et = ev.get("type")
        # v3.6.8 (finding живой квалификации): evidence с путём — это code-read, как бы модель его ни
        # назвала (file/source/code/read). Раньше принимали ТОЛЬКО 'code-read'/'read' -> валидный вердикт
        # k3 (evidence type='file' + path+lines) отвергался как «нераспознанный type» -> security ложно
        # блокировал корректный код. Анти-false-green СОХРАНЁН: path обязателен И (если есть trace reads)
        # сверяется с реально прочитанными файлами — сфабрикованный путь по-прежнему не пройдёт.
        if et in ("code-read", "read", "file", "source", "code"):
            path = ev.get("path")
            if not path:
                errs.append(f"домен '{dom}': code-read evidence без path")
            elif reads is not None and not _read_match(path):
                errs.append(f"домен '{dom}': code-read evidence ссылается на '{path}', которого нет среди "
                            "реально прочитанных ревьюером файлов — сфабрикованная ссылка")
        elif et == "test":
            if not ev.get("command"):
                errs.append(f"домен '{dom}': test evidence без command")
        elif et in ("finding", "scanner"):
            if not (ev.get("id") or ev.get("detail") or ev.get("path")):
                errs.append(f"домен '{dom}': {et} evidence без id/detail/path")
        elif ev.get("path"):
            # неизвестный type, но есть path -> трактуем как code-read (та же анти-фабрикация)
            if reads is not None and not _read_match(ev["path"]):
                errs.append(f"домен '{dom}': evidence ссылается на '{ev['path']}', которого нет среди "
                            "реально прочитанных ревьюером файлов — сфабрикованная ссылка")
        else:
            errs.append(f"домен '{dom}': evidence без распознаваемого type и без path "
                        f"(нужен code-read|test|finding + path/command, получено {et!r})")
    return errs


def _security_verdict_errors(res, revision, applicable_domains, vrr, reviewer_reads=None):
    """v3.0-rc16: строгая проверка security reviewer-result — та же дисциплина, что для обычных гейтов,
    плюс security-специфика. reviewer_reads (v3.0.10) — реальный trace чтений ревьюера для сверки
    code-read evidence. -> список ошибок (пусто = валиден)."""
    if not isinstance(res, dict):
        return ["security-reviewer не вернул структурный вердикт"]
    errs = list(vrr.check(res, gate_ids=None) or [])
    if res.get("gate") not in (None, "security"):
        errs.append(f"gate вердикта '{res.get('gate')}' != security")
    if revision and res.get("reviewed_revision") not in (None, revision):
        errs.append("reviewed_revision вердикта != проверяемой ревизии")
    checks = res.get("checks") if isinstance(res.get("checks"), list) else []
    if not checks:
        errs.append("security-вердикт без checks — нечем подтвердить проверенные домены")
    # v3.0.1 (finding аудита P0): SecurityVerdict v2 — domain_results ОБЯЗАН покрыть РОВНО применимые
    # домены (не brittle id-substring из rc16, а структурный контракт: отдельный статус на каждый домен).
    if applicable_domains:
        dr = res.get("domain_results")
        if not isinstance(dr, list) or not dr:
            errs.append("нет domain_results — один общий вердикт не доказывает каждый применимый домен")
        else:
            seen = [str((x or {}).get("domain")) for x in dr if isinstance(x, dict)]
            got = set(seen)
            need = set(applicable_domains)
            if len(seen) != len(got):
                errs.append("domain_results содержит дубли доменов")
            if got != need:
                missing = need - got
                extra = got - need
                if missing:
                    errs.append(f"domain_results не покрывает домены: {', '.join(sorted(missing))}")
                if extra:
                    errs.append(f"domain_results содержит неизвестные/лишние домены: {', '.join(sorted(extra))}")
            for x in dr:
                st = (x or {}).get("status")
                dom = (x or {}).get("domain")
                if st not in ("pass", "warn", "fail"):
                    errs.append(f"domain_result '{dom}' без валидного status")
                elif st != "pass" and (res.get("status") == "pass"):
                    errs.append(f"домен '{dom}' = {st}, но общий status=pass — несогласованно")
                # v3.0.7/v3.0.8 (finding аудита P1): SecurityVerdict v2.1/v2.2 — КАЖДЫЙ домен несёт СВОИ
                # доказательства. v2.2: nested-check валиден (id+status), а не `checks:[{}]`; для pass-домена
                # хотя бы один check со status=pass; для warn/fail — непустой blockers. Иначе абстрактный
                # пустой check «прикрывает» домен.
                dchecks = (x or {}).get("checks")
                if not (isinstance(dchecks, list) and dchecks):
                    errs.append(f"домен '{dom}' без domain-specific checks — доказательства по домену отсутствуют")
                else:
                    for c in dchecks:
                        if not isinstance(c, dict) or not c.get("id") or c.get("status") not in ("pass", "warn", "fail"):
                            errs.append(f"домен '{dom}': nested-check без id/валидного status ({c})")
                    if st == "pass" and not any(isinstance(c, dict) and c.get("status") == "pass" for c in dchecks):
                        errs.append(f"домен '{dom}' pass, но ни один его check не подтверждён (status=pass)")
                    if st in ("warn", "fail") and not (x or {}).get("blockers"):
                        errs.append(f"домен '{dom}' = {st} без blockers — блокирующий вердикт без причины")
                    # v3.0.9 (finding аудита P1): SecurityVerdict v2.3 — pass-домен требует хотя бы одну
                    # КОНКРЕТНУЮ evidence-ссылку (code-read path/lines, test command, scanner finding, read
                    # path) — на уровне домена (evidence:[...]) ИЛИ в его check'ах. id+status без evidence
                    # ещё не доказательство. warn/fail довольствуются blockers (причина названа выше).
                    if st == "pass":
                        # v3.0.10 (finding аудита P1): собираем ВСЕ evidence-ссылки домена (уровень домена +
                        # уровень его check'ов) и валидируем их как СТРУКТУРНЫЕ EvidenceRef + сверяем code-read
                        # с реальным trace ревьюера. Непустой список строк «checked» больше не проходит.
                        _all_ev = []
                        _dom_ev = (x or {}).get("evidence")
                        if isinstance(_dom_ev, list):
                            _all_ev += _dom_ev
                        for c in dchecks:
                            _ce = c.get("evidence") if isinstance(c, dict) else None
                            if isinstance(_ce, list):
                                _all_ev += _ce
                            elif _ce:
                                _all_ev.append(_ce)
                        if not _all_ev:
                            errs.append(f"домен '{dom}' pass без evidence-ссылки — id+status не доказательство")
                        else:
                            errs += _evidence_ref_errors(dom, _all_ev, reviewer_reads)
    return errs


def _human_approval_domains_uncovered(approval_root, wid, changed_files, diff_root=None):
    """v3.0-rc20/rc3.0.2 (finding аудита P0/P1): домены с непустыми human_approval_conditions, чьи
    file_patterns СОВПАЛИ с РЕАЛЬНО изменёнными путями (Dockerfile/CI/auth, deploy, tenant, tool-access),
    ОБЯЗАНЫ иметь валидный человеческий ApprovalRecord. Security-REVIEWER их НЕ закрывает.
    v3.0.2: РАЗДЕЛЕНИЕ КОРНЕЙ — ApprovalRecord'ы и plan-binding читаются из LIFECYCLE-корня (approval_root
    = child_root/features), а изменённые файлы приходят из EXECUTION-корня (diff_root = worktree). Раньше
    оба читались из одного root -> человеческое одобрение из lifecycle отсутствовало в worktree ->
    ложный uncovered. Триггер — совпадение путей (не «always-applicable» secrets). -> список НЕпокрытых."""
    import re as _re
    import security_pack
    import approvals as _appr
    _CATCH_ALL = {".*", ".+", "", "^.*$", "(?s).*"}
    # (1) какие high-risk домены сработали ПО ПУТЯМ + какие именно файлы их триггернули (для scope-проверки)
    triggered = {}   # domain_id -> [matched changed files]
    try:
        for d in security_pack.load_domains()[0]:
            if not d.get("human_approval_conditions"):
                continue
            # ТОЛЬКО СПЕЦИФИЧНЫЕ паттерны = деятельная high-risk поверхность (dockerfile/auth/deploy...).
            # Catch-all (secrets: '.*') — always-on сканер, закрыт детерминированно; human форсируется НАХОДКОЙ.
            pats = [p for p in ((d.get("applicability", {}) or {}).get("file_patterns") or [])
                    if p.strip() not in _CATCH_ALL]
            if not pats:
                continue
            matched = [f for f in (changed_files or []) if any(_re.search(p, f) for p in pats)]
            if matched:
                triggered[d["id"]] = matched
    except Exception:  # noqa: BLE001 — не смогли определить применимость -> fail-closed на весь набор
        return sorted(set((changed_files and ["<security-domains-load-failed>"]) or []))
    if not triggered:
        return []
    # (2) СТРОГАЯ проверка покрытия: high-risk запись обязана быть strict-валидной (binds_to/expires_at/
    #     risk/source), привязанной к текущему plan_hash, не просроченной, и её scope обязан покрыть
    #     реально изменённые high-risk файлы. Любой сбой -> fail-closed (домен считается НЕпокрытым).
    try:
        recs = _appr.load_approvals(approval_root, wid)   # LIFECYCLE-корень (human input), не worktree
        now = _appr._now_iso()
        plan_hash = _appr.plan_binding_hash(approval_root, wid)
    except Exception:  # noqa: BLE001
        return sorted(triggered)   # не смогли прочитать одобрения -> ничего не покрыто
    uncovered = []
    for dom, files in sorted(triggered.items()):
        rec = next((r for r in recs if r.get("approval") == dom), None)
        try:
            ok = (rec is not None
                  and _appr._record_valid(rec, now=now, plan_hash=plan_hash, strict=True)
                  and _appr.covers_paths(rec, files))
        except Exception:  # noqa: BLE001 — сомнение = не покрыто
            ok = False
        if not ok:
            uncovered.append(dom)
    return uncovered


def _parse_yaml_block(text):
    """Достать YAML-артефакт из ответа author-модели. v3.0-rc5 (finding живого прогона kimi): терпимо к
    РАЗНЫМ стилям вывода моделей — несколько ```-блоков, проза вокруг, YAML без ограды после текста.
    Перебираем кандидатов (все fenced-блоки, срез от schema_version:/kind:, сырой текст) и берём ПЕРВЫЙ,
    который парсится в dict. Раньше брали только первый fenced-блок -> прозо-обёрнутый YAML kimi падал."""
    import yaml
    import re
    if isinstance(text, dict):
        return text
    s = text or ""
    candidates = []
    for m in re.finditer(r"```[ \t]*[A-Za-z0-9]*\n(.*?)```", s, re.S):   # все fenced-блоки
        candidates.append(m.group(1))
    for marker in ("schema_version:", "kind:"):                          # YAML без ограды / после прозы
        i = s.find(marker)
        if i >= 0:
            candidates.append(s[i:])
    candidates.append(s)                                                 # как есть
    for c in candidates:
        try:
            data = yaml.safe_load(c)
        except yaml.YAMLError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _openspec_validate(work_root, change_id):
    """v2.89: прогнать НАСТОЯЩИЙ openspec CLI на произведённом change. -> (available, ok, output).
    available=False -> CLI не установлен в child (гейт честно остаётся блокирующим, не фабрикуем)."""
    try:
        r = subprocess.run(["openspec", "validate", change_id, "--strict"],
                           cwd=str(work_root), capture_output=True, text=True, timeout=120,
                           env={**os.environ, "OPENSPEC_TELEMETRY": "0"})
        return True, r.returncode == 0, (r.stdout + r.stderr)[-600:]
    except FileNotFoundError:
        return False, False, "openspec CLI не найден в PATH (npm i -g @fission-ai/openspec)"
    except subprocess.TimeoutExpired:
        return True, False, "openspec validate: timeout"


# v2.86: артефакт-гейты, которые движок умеет ЗАКРЫВАТЬ производством артефакта + детерминированной
# проверкой ФОРМЫ (не «качества» — его судит независимый ревьюер/человек). specification (v2.89)
# обрабатывается ОТДЕЛЬНО — рендерит OpenSpec-change и валидирует реальным openspec CLI.
def _authoring_specs():
    import validate_requirements_artifact as vra
    import validate_plan_artifact as vpa
    return {
        "requirements": ("requirements.yaml", vra, "requirements-artifact",
                         "requirements: список объектов {id, statement (тестируемое требование), "
                         "acceptance: [сценарии приёмки]}"),
        "plan_readiness": ("plan.yaml", vpa, "plan-artifact",
                           "work_packages: [{id, summary, depends_on: [id,...]}], "
                           "write_scope: [пути]"),
    }


def _reevaluate_artifact_evidence(work_root, wid, gate_ids):
    """v3.8.3 reevaluate: пере-вывести evidence артефакт-гейтов из СУЩЕСТВУЮЩИХ на диске артефактов
    (БЕЗ модели) — SHA не менялся, форма уже подтверждена оригинальным прогоном. requirements/
    plan_readiness читаются из .ai/runplan/<wid>/ и валидируются детерминированно; specification —
    реальный openspec validate --strict. Так переоценка после человеко-approval не теряет уже
    доказанные гейты (клоббер run-report не влияет). -> {gate_id: evidence}."""
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
        except Exception:  # noqa: BLE001
            pass
    if "specification" in gate_ids:
        try:
            _avail, _ok, _ = _openspec_validate(work_root, wid)
            if _avail and _ok:
                ev["specification"] = {"status": "pass", "provided": ["openspec_valid"],
                                       "evidence": ["openspec validate --strict (reevaluate, SHA стабилен)"]}
        except Exception:  # noqa: BLE001
            pass
    return ev


def _author_with_retry(author_proposer, base_prompt, check_fn, bud, attempts=3):
    """v3.0-rc14 (finding живой квалификации kimi): author-вызов ретраится при невалидном/пустом
    артефакте. Флаки reasoning-провайдер (kimi) на части вызовов отдаёт пустой/битый YAML -> артефакт-
    гейт ложно не закрывается (не движковый дефект, а нестабильность модели) -> на multi-package
    прогоне почти всегда какой-то пакет не доходит до ready. Ретраим с корректирующим нуджем, каждая
    попытка — под потолком бюджета. check_fn(data)->errs (МОЖЕТ мутировать data: нормализация). Берём
    первый валидный; иначе последний (честный не-ready сохраняется, если модель так и не смогла).
    -> (data, errs). НЕ маскирует качество: форму по-прежнему судит валидатор, содержание — ревьюер."""
    import budget as _budget_mod
    prompt = base_prompt
    data, errs = None, ["author не вызван"]
    for attempt in range(attempts):
        try:
            bud.charge_call()
        except _budget_mod.BudgetExceeded as e:
            return data, [f"budget: {e}"]
        data = _parse_yaml_block(author_proposer(prompt))
        errs = check_fn(data)
        if not errs:
            return data, errs
        # корректирующий нудж: показать модели, ЧТО именно невалидно, и потребовать чистый YAML
        prompt = base_prompt + (
            f"\n\n[повтор {attempt + 1}/{attempts}] Твой предыдущий ответ НЕ прошёл валидацию: "
            f"{'; '.join(str(e) for e in (errs or [])[:3])}. Верни ТОЛЬКО валидный YAML строго по схеме "
            "выше — без прозы, без markdown-ограды, все обязательные поля заполнены.")
    return data, errs


def _run_spec_authoring(author_proposer, work_root, gate_ev, wid, task, bud, openspec_validate):
    """v2.89: произвести OpenSpec change для гейта specification. author даёт СТРУКТУРУ, движок
    рендерит точный OpenSpec-markdown и валидирует РЕАЛЬНЫМ openspec CLI. Закрывает гейт ТОЛЬКО
    если CLI доступен И strict-валидация прошла (иначе честный блок). -> (gate_ev, entry)."""
    import validate_spec_artifact as vsa
    prompt = (
        "Ты автор OpenSpec-изменения (spec-change) для задачи. Верни ТОЛЬКО YAML со схемой:\n"
        "  schema_version: 1\n  kind: spec-change\n  capability: <slug>\n  why: <зачем>\n"
        "  what_changes: [<что меняется>]\n  tasks: [<шаг>, ...]\n"
        "  requirements:\n    - name: <имя>\n      text: <нормативное требование со словом SHALL>\n"
        "      scenarios:\n        - {name: <имя>, when: <условие>, then: <результат>}\n"
        "Требования конкретные и проверяемые. Только JSON/YAML.\n\n=== ЗАДАЧА ===\n" + task)

    def _spec_check(data):
        # v3.0-rc8 (finding живого прогона kimi): строки в tasks/what_changes часто содержат двоеточие
        # («Написать unit-тесты: все ветвления...») -> YAML разбирает элемент списка как MAPPING {key: val},
        # а не строку -> vsa.check «непустой список строк» падает. Нормализуем: одноключевой dict от
        # случайного «k: v» -> строка «k: v». Модель имела в виду строку — восстанавливаем её.
        if not isinstance(data, dict):
            return ["author не вернул валидный YAML spec-change"]
        for _k in ("tasks", "what_changes"):
            _v = data.get(_k)
            if isinstance(_v, list):
                data[_k] = [(x if isinstance(x, str)
                             else "; ".join(f"{k}: {vv}" for k, vv in x.items()) if isinstance(x, dict)
                             else str(x)) for x in _v]
        return vsa.check(data)

    # v3.0-rc14: ретраим невалидный/пустой author-вывод (флаки reasoning-провайдер) с нуджем.
    data, errs = _author_with_retry(author_proposer, prompt, _spec_check, bud)
    entry = {"gate": "specification", "artifact": f"openspec/changes/{wid}", "valid": not errs,
             "errors": errs or None}
    if errs:
        return gate_ev, entry
    vsa.render(data, Path(work_root) / "openspec", wid)
    available, ok, out = openspec_validate(work_root, wid)
    entry["openspec_cli"] = "available" if available else "absent"
    entry["openspec_valid"] = ok if available else None
    if available and ok:
        gate_ev = dict(gate_ev)
        gate_ev["specification"] = {"status": "pass", "provided": ["openspec_valid", "requirements_covered"],
                                    "evidence": [f"openspec validate --strict OK @ openspec/changes/{wid}"]}
        entry["closed"] = True
    else:
        entry["closed"] = False
        entry["note"] = ("openspec CLI не установлен -> гейт остаётся блокирующим (честно)"
                         if not available else f"openspec validate провалился: {out}")
    return gate_ev, entry


def _run_authoring(author_proposer, work_root, gate_ids, gate_ev, wid, task, budget,
                   openspec_validate=None):
    """v2.86 Product Authoring: движок производит артефакты requirements/plan. author-модель даёт
    СОДЕРЖИМОЕ (YAML), движок пишет его в .ai/runplan/<wid>/ (доверенный путь, не произвольная
    запись модели) и подтверждает ФОРМУ детерминированным валидатором -> legitimate evidence для
    гейта. КАЧЕСТВО артефакта судит независимый ревьюер (--review) / человек, не эта проверка.
    -> (gate_ev, authored_trace, wrote_files)."""
    import budget as _budget_mod
    bud = budget if isinstance(budget, _budget_mod.Budget) else _budget_mod.Budget.from_dict(budget)
    out_dir = Path(work_root) / ".ai" / "runplan" / wid
    gate_ev = dict(gate_ev)
    authored, wrote = [], False
    for gid, (fname, mod, kind, shape) in _authoring_specs().items():
        if gid not in gate_ids or gid in gate_ev:
            continue                        # гейта нет в плане, либо evidence уже есть
        prompt = (
            f"Ты автор артефакта '{kind}' для задачи. Верни ТОЛЬКО YAML (без пояснений) со схемой:\n"
            f"  schema_version: 1\n  kind: {kind}\n  workitem_id: {wid}\n  {shape}\n"
            f"Артефакт должен точно отражать задачу ниже. Требования/пакеты — конкретные и "
            f"тестируемые, не общие слова.\n\n=== ЗАДАЧА ===\n{task}")

        def _check(data):
            return mod.check(data) if isinstance(data, dict) else ["author не вернул валидный YAML артефакта"]

        # v3.0-rc14: ретраим невалидный/пустой author-вывод (флаки reasoning-провайдер) с нуджем.
        data, errs = _author_with_retry(author_proposer, prompt, _check, bud)
        if errs and any("budget:" in str(e) for e in errs):
            authored.append({"gate": gid, "valid": False, "errors": errs})
            break
        entry = {"gate": gid, "artifact": fname, "valid": not errs, "errors": errs or None}
        if not errs:
            out_dir.mkdir(parents=True, exist_ok=True)
            import yaml as _yaml
            (out_dir / fname).write_text(
                _yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
            wrote = True
            gate_ev[gid] = {"status": "pass", "provided": mod.provided_evidence(data),
                            "evidence": [f".ai/runplan/{wid}/{fname} — форма подтверждена детерминированно"]}
            entry["provided"] = mod.provided_evidence(data)
        authored.append(entry)
    # v2.89: specification — отдельно (рендер OpenSpec-change + реальный openspec validate --strict).
    if "specification" in gate_ids and "specification" not in gate_ev:
        gate_ev, spec_entry = _run_spec_authoring(
            author_proposer, work_root, gate_ev, wid, task, bud,
            openspec_validate or _openspec_validate)
        if spec_entry.get("closed"):
            wrote = True
        authored.append(spec_entry)
    return gate_ev, authored, wrote


def _authored_context(authored, work_root, wid):
    """v2.123 (P0.1): текст ВАЛИДНЫХ author-артефактов (requirements/plan) для подачи в prompt
    реализации — реализация идёт ПО спеке, созданной до кода (Spec-First), а не до неё."""
    out = Path(work_root) / ".ai" / "runplan" / wid
    parts = []
    for e in (authored or []):
        fn = e.get("artifact")
        if e.get("valid") is False or not fn or str(fn).startswith("openspec"):
            continue                       # спека-change — каталог, не файл; берём requirements/plan
        p = out / fn
        try:
            if p.is_file():
                parts.append(f"# {e.get('gate')} ({fn})\n" + p.read_text(encoding="utf-8")[:2000])
        except OSError:
            pass
    return ("=== СПЕЦИФИКАЦИЯ ЗАДАЧИ (создана ДО реализации; следуй ей) ===\n" + "\n\n".join(parts)
            if parts else "")


def _git(root, *args):
    import gitio
    return gitio.git(root, *args)   # v3.0.13 (блок C): единый git-хелпер с таймаутом


def _committed_changed_files(root, sha):
    """Файлы, изменённые коммитом sha относительно его первого родителя. -> [path] (пусто при ошибке)."""
    if not sha:
        return []
    rc, out, _ = _git(root, "diff", "--name-only", f"{sha}~1", sha)
    if rc != 0:
        rc, out, _ = _git(root, "show", "--name-only", "--pretty=format:", sha)
    return [ln for ln in out.splitlines() if ln.strip()] if rc == 0 else []


def _commit_on_branch(root, branch, message):
    """Зафиксировать применённые изменения на рабочей ветке (не в main). -> полный commit SHA или None.

    finding аудита (P0.5): возвращаем ПОЛНЫЙ SHA (не --short) — evidence бьётся о точную ревизию,
    а короткий SHA теоретически коллизирует и не годится как надёжный идентификатор ревизии.
    """
    _git(root, "checkout", "-q", "-B", branch)   # рабочая ветка (не трогаем main)
    _git(root, "add", "-A")
    rc, _, _ = _git(root, "diff", "--cached", "--quiet")
    if rc == 0:                                   # нечего коммитить
        return None
    _git(root, "commit", "-q", "-m", message)
    rc, sha, _ = _git(root, "rev-parse", "HEAD")
    return sha if rc == 0 else None


def _tree_clean(root):
    """git status --porcelain пуст? -> рабочее дерево совпадает с HEAD (нет незакоммиченных правок).

    finding аудита (P0.5): evidence должен отражать ЗАКОММИЧЕННУЮ ревизию. Если дерево грязное
    (правки вне коммита или checks намутили артефакты), evidence не бьётся о SHA — это нужно видеть,
    а не молча объявлять ready_for_pr.
    """
    rc, out, _ = _git(root, "status", "--porcelain")
    return rc == 0 and out.strip() == ""


# v2.119 (finding живого прогона): известные тул-кэши/артефакты, которые тесты/сборка РУТИННО создают
# (pytest/npm/mypy/rust/...). В репо БЕЗ .gitignore этих путей они показываются в `git status` как
# untracked и делали дерево «грязным после проверок» (tree_after=False) -> ложный not-ready, хотя
# checks реально прошли. Их наличие как UNTRACKED-артефактов не нарушает evidence-целостность.
_TOOL_CACHE_RE = re.compile(
    r"(^|/)("
    r"__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.tox|\.nox|\.hypothesis|"
    r"htmlcov|\.eggs|[^/]+\.egg-info|\.coverage[^/]*|"
    r"node_modules|\.next|\.nuxt|\.turbo|\.parcel-cache|\.svelte-kit|"
    r"target|\.gradle|\.mvn|"
    r"dist|build|coverage|\.cache|__snapshots__"
    r")(/|$)"
    r"|\.(pyc|pyo|class)$"
)


def _tree_clean_after_checks(root):
    """v2.119: чистота дерева ПОСЛЕ проверок, игнорируя известные тул-кэши (UNTRACKED-артефакты тестов/
    сборки: __pycache__, .pytest_cache, node_modules, target, dist, ...). Модификации TRACKED-файлов и
    любой прочий untracked по-прежнему считаются грязью — evidence-целостность (P0.5) сохранена.
    Устраняет false not-ready в репозиториях без .gitignore этих кэшей. -> bool."""
    rc, out, _ = _git(root, "status", "--porcelain")
    if rc != 0:
        return False
    for ln in out.splitlines():
        if not ln.strip():
            continue
        code, path = ln[:2], ln[3:]
        # игнорируем ТОЛЬКО untracked (??) тул-кэши; tracked-правки и прочий untracked = грязь
        if code == "??" and _TOOL_CACHE_RE.search(path):
            continue
        return False
    return True


def _untracked(root):
    """Множество untracked-файлов (git status --porcelain, префикс '??'). Игнорируемые (.gitignore,
    напр. node_modules) сюда НЕ попадают — porcelain их не показывает без --ignored."""
    rc, out, _ = _git(root, "status", "--porcelain")
    if rc != 0:
        return set()
    return {ln[3:] for ln in out.splitlines() if ln.startswith("?? ")}


def _has_changes(root):
    """Есть ли ЛЮБЫЕ правки в рабочем дереве (tracked-diff ИЛИ новые untracked)? -> bool.

    v2.93 (finding аудита): раньше наличие правок считали ТОЛЬКО по успешным write-операциям петли.
    Если модель изменила код через разрешённый shell (sed/форматтер), правки реальны, но applied
    пусто -> коммит не создавался и работа не доставлялась. Считаем факт по git, а не по счётчику op."""
    return not _tree_clean(root)


def _install_dependencies(profile, root, policy):
    """Поставить зависимости стеков (install_command) через Broker перед сбором evidence.

    finding живого прогона (ii-sreda/DeepSeek): в СВЕЖЕМ git-worktree нет node_modules/venv,
    поэтому build/lint/test падают exit 127 (command not found) — это не «код сломан», а
    «окружение не подготовлено». Ставим детерминированную install-команду стека (npm ci /
    poetry install / pip install ...). Только в изолированном worktree (не трогаем основное
    дерево пользователя, где npm ci снёс бы node_modules). -> список результатов.
    """
    results = []
    seen = set()
    for stack in profile.get("stacks", []) or []:
        cmd = stack.get("install_command")
        if not cmd or cmd in seen:
            continue
        seen.add(cmd)
        ev = tool_broker.execute({"op": "shell", "command": cmd, "timeout": 600}, root, policy)
        results.append({"language": stack.get("language"), "command": cmd,
                        "allowed": ev.get("allowed"), "ok": ev.get("ok", False),
                        "exit_code": ev.get("exit_code"),
                        "output_tail": (ev.get("output_tail") or "")[-200:]})
    return results


_ENV_SYMPTOMS = ("command not found", "not found", "no such file", "no module named",
                 "modulenotfounderror", "cannot find module", "is not recognized",
                 "executable not found", "no such command")


def _check_has_env_symptom(c):
    """У проверки есть симптом неподготовленного окружения (нет тулчейна/зависимости)?"""
    for run in ((c or {}).get("runs") or []):
        if run.get("ok"):
            continue
        if run.get("exit_code") == 127:
            return True
        if any(s in (run.get("output_tail") or "").lower() for s in _ENV_SYMPTOMS):
            return True
    return False


def _env_proven_ok(checks):
    """v2.121 (P1.4, строгий install-фикс): окружение считается ДОКАЗАННО рабочим ТОЛЬКО если хотя бы
    одна применимая проверка РЕАЛЬНО отработала — прошла (pass) ЛИБО упала по настоящей причине кода
    (fail БЕЗ env-симптома: тулчейн есть, тест честно красный). Если проверок не запускалось вовсе
    (все not_run/not_applicable) ИЛИ все падения — env-симптомы (exit 127/нет модуля) -> НЕ доказано.
    Это устраняет дыру v2.118: раньше install-провал игнорировался и при полном отсутствии проверок."""
    for c in (checks or {}).values():
        st = (c or {}).get("status")
        if st == "pass":
            return True
        if st in ("fail", "error") and not _check_has_env_symptom(c):
            return True
    return False


def _env_unqualified(checks):
    """Обратная форма для совместимости/наглядности: окружение НЕ квалифицировано доказательно."""
    return not _env_proven_ok(checks)


def _baseline_failure_summary(checks, tail=500):
    """Свод падающих проверок базы с ФАКТИЧЕСКИМ выводом — чтобы модель знала, что чинить.

    finding живого прогона: на fix-задаче модель без вывода теста крутилась до max_steps с 0
    правок. Даём реальный stderr/stdout (не фабрикация — вывод настоящего прогона).
    """
    lines = []
    for name, c in (checks or {}).items():
        if (c or {}).get("status") != "fail":
            continue
        for run in (c.get("runs") or []):
            if run.get("ok"):
                continue
            out = (run.get("output_tail") or "")[-tail:]
            lines.append(f"[{name}] {run.get('command')} (exit {run.get('exit_code')}):\n{out}")
    return "\n".join(lines)


def _failure_signal(check):
    """Грубая метрика 'насколько плохо' для проверки: макс. число failed/errors в выводе.

    finding живого прогона: baseline-diff на уровне check (pass/fail) пропускал УХУДШЕНИЕ внутри
    уже-красной проверки — модель превратила 1 падающий тест в 8, а check как был 'fail', так и
    остался -> ложный 'no regression'. Считаем число падений из output_tail (vitest/jest/pytest:
    'N failed'; tsc: 'N errors'); рост числа при fail->fail = регрессия. Best-effort: output_tail
    усечён, поэтому счётчик может быть 0, если строка-итог не попала в хвост (тогда не хуже).
    """
    import re
    n = 0
    for run in (check or {}).get("runs", []) or []:
        for m in re.finditer(r"(\d+)\s+(?:failed|errors?)\b", run.get("output_tail") or "", re.I):
            n = max(n, int(m.group(1)))
    return n


# v2.84: СТРУКТУРНЫЕ идентификаторы падений — чтобы ловить «починил один тест, сломал другой»
# (число падений то же 1->1, но это ДРУГОЙ провал = регрессия, которую счётчик пропускал).
# Best-effort по типовым раннерам; неизвестный формат -> пустое множество (падаем обратно на счётчик).
_FAILURE_ID_PATTERNS = [
    r"(?:FAILED|ERROR)\s+(\S+::\S+)",                 # pytest: FAILED tests/x.py::test_y
    r"(\S+::\S+)\s+(?:FAILED|ERROR)\b",               # pytest альт.: x.py::test_y FAILED
    # go test: "--- FAIL: TestSub (0.00s)" / "--- FAIL: TestX/case (0.0s)". Раньше go-падения не
    # извлекались вовсе -> id схлопывался в мусорный {'FAIL'} из summary -> "починил один тест,
    # сломал другой" в ОДНОМ пакете не различалось (go не печатает 'N failed' -> счётчик тоже
    # молчит) -> ложный green (finding стек-квалификации go). \S+ обрывает волатильное "(0.00s)".
    r"---\s+FAIL:\s+(\S+)",
    r"(\S+\.\w+\(\d+,\d+\)):\s*error\s+(TS\d+)",      # tsc: file.ts(12,5): error TS2322
    # go build/vet: "./pkg/a.go:3:6: undefined: foo" / "a.go:13: msg" (file.go:line[:col]: message).
    # Стабильный id по файлу+позиции; для сборки/вета go (нет '--- FAIL:').
    r"([\w./\-]+\.go):(\d+):(?:(\d+):)?\s*(.+)",
    # vite/rollup/esbuild: "src/a.tsx (19:9): "X" is not exported by ..." — РЕАЛЬНАЯ строка ошибки
    # сборки (файл + позиция + сообщение). Даёт СТАБИЛЬНЫЙ id: новая поломка -> другой файл/позиция.
    r"([\w./\-]+\.\w+)\s*\((\d+)[,:](\d+)\):\s*(.+)",
    r"error\[(E\d+)\]",                               # rust: error[E0308] (компиляция)
    # rust `cargo test`: "thread 'tests::test_sub' (13663) panicked at src/lib.rs:10:21". Раньше
    # НИ один паттерн не ловил имя упавшего теста -> id схлопывался в константу из строки
    # "error: test failed, to rerun pass `--lib`" (одинакова для ЛЮБОГО падения) -> "починил один
    # тест, сломал другой" (rust печатает 'N failed', но счётчик 1->1 не растёт) не различалось =
    # ложный green (finding стек-квалификации rust). Берём имя теста + файл; pid в (...) отбрасываем.
    r"thread '([^']+)' .*?panicked at ([\w./\-]+\.rs):(\d+)",
    # java (maven-surefire / gradle + JUnit). Раньше НИ один паттерн не ловил падение java: id
    # оставался пустым, а maven печатает "Failures: 1" (слово ПЕРЕД числом) -> _failure_signal тоже
    # 0 -> swap (починил testSub, сломал testAdd) не ловился = ложный green для java-репо. Берём
    # Class.method упавшего теста. Проверено на РЕАЛЬНОМ surefire-выводе junit5 (v2.92).
    r"([\w.$]+\.[\w$]+)\s+--\s+Time elapsed[^\n]*<<<\s+(?:FAILURE|ERROR)",   # surefire header
    r"\[ERROR\]\s+([\w.$]+\.[\w$]+):(\d+)\b",                                # surefire summary: Class.method:line
    r"([\w.$]+)\s+>\s+([\w$]+)\(\)\s+FAILED",                                # gradle: Class > method() FAILED
    r"(?:✕|×|✗)\s+(.+?)(?:\s+\(\d+\s*ms\))?\s*$",     # jest/vitest: ✕ suite > test name
    r"(?:^|\n)\s*FAIL\s+(\S+)",                       # jest/vitest файловый: FAIL src/a.test.ts
    r"(?:^|\n)\s*(?:AssertionError|Error):\s*(.+)$",  # generic ассерт/ошибка
]

# v2.88 (finding живого прогона на ii-sreda): волатильные токены в выводе -> РАЗНЫЙ id при ОДНОЙ и
# той же поломке -> ложная регрессия. Классика: vite печатает "✗ Build failed in 1.41s" — время
# меняется от прогона к прогону. Нормализуем: выкидываем длительности (1.41s / 12 ms), hex-адреса и
# голые числа-времена, схлопываем пробелы. Реальные test-node-id (x.py::test) не содержат таких
# токенов -> не страдают.
_VOLATILE_RE = re.compile(r"\b\d+(?:\.\d+)?\s*m?s\b|0x[0-9a-fA-F]+|\b\d+(?:\.\d+)?\s*ms\b")


def _normalize_failure_id(token):
    import re as _re
    return _re.sub(r"\s+", " ", _VOLATILE_RE.sub("", token)).strip()


def _failure_ids(check):
    """Множество нормализованных id падений из output_tail проверки (best-effort по раннерам).
    v2.88: id нормализуется (убраны волатильные длительности/адреса), иначе "Build failed in 1.41s"
    даёт новый id каждый прогон -> ложная fail->fail регрессия."""
    import re
    ids = set()
    for run in (check or {}).get("runs", []) or []:
        tail = run.get("output_tail") or ""
        # v3.0-rc2: снимаем ANSI-цвета раннера (pytest/jest/... с forced color) — иначе "FAILED\x1b[0m
        # test::id" не парсится и node-id теряется (ложный «нет падений»). CI без TTY обычно без цвета,
        # но color может быть форсирован конфигом/окружением — делаем извлечение устойчивым.
        tail = re.sub(r"\x1b\[[0-9;]*m", "", tail)
        for pat in _FAILURE_ID_PATTERNS:
            for m in re.finditer(pat, tail, re.I | re.M):
                token = _normalize_failure_id(" ".join(t for t in m.groups() if t).strip())
                if token:
                    ids.add(token[:200])
    return ids


def _diff_checks(baseline, after):
    """Сравнить проверки ДО и ПОСЛЕ правки. -> (regressions, fixed).

    regression = было pass -> стало fail (сломал), ИЛИ было fail и стало ХУЖЕ: (v2.84) появился
    НОВЫЙ структурный id падения, которого не было в базе (починил один — сломал другой), ЛИБО
    (v2.77 fallback) выросло число падений. fixed = было fail -> стало pass.
    """
    baseline, after = baseline or {}, after or {}
    regressions, fixed = [], []
    real = ("pass", "fail")            # «настоящий» вердикт проверки (реально исполнена)
    for name, a in after.items():
        b = baseline.get(name) or {}
        b_status, a_status = b.get("status"), a.get("status")
        if a_status == "fail" and b_status != "fail":
            # v2.87 (finding аудита): стало КРАСНЫМ — из pass ИЛИ из warn/not_run (напр. на базе
            # тестов не было -> warn, правка добавила ПАДАЮЩИЙ тест). Раньше warn/not_run -> fail
            # проскакивало (implementation_verification baseline-освобождён) -> ложный green. Считаем.
            regressions.append(name)
        elif b_status == "fail" and a_status == "pass":
            fixed.append(name)
        elif b_status == "fail" and a_status == "fail":
            # структурно: НОВЫЙ id падения = регрессия (даже если общее число не выросло)
            b_ids, a_ids = _failure_ids(b), _failure_ids(a)
            new_ids = a_ids - b_ids
            if new_ids or _failure_signal(a) > _failure_signal(b):
                regressions.append(name)     # уже красная, но правка внесла НОВЫЙ провал / стало хуже
            # v2.122 (finding обкатки S10): чек ОСТАЁТСЯ red из-за НЕ связанного пред-существующего
            # падения, но профильный узел реально починен -> засчитываем fixed СИММЕТРИЧНО регрессиям
            # (по node-id), а не только когда чек целиком fail->pass. Эта elif-ветка = чистое улучшение
            # (нет новых падений и счётчик не вырос); swap «починил один — сломал другой» уже ушёл в
            # регрессию выше. Требует извлечённых id ПОСЛЕ правки (a_ids) — иначе не фабрикуем fixed на
            # непарсибельном выводе (build-fail без node-id и т.п.); пустой diff id -> нечего засчитывать.
            elif a_ids and (b_ids - a_ids):
                fixed.append(name)
        elif b_status in real and a_status not in real:
            # v2.85 (finding аудита): проверка ПЕРЕСТАЛА давать вердикт (pass/fail -> warn/not_run/None)
            # = потеря покрытия/верификации. Модель «чинит» красный тест, УДАЛЯЯ его -> tests_absent
            # -> status warn -> раньше это не считалось регрессией. Считаем.
            regressions.append(name)
    return regressions, fixed


def run_pipeline(task, signals, child_root, proposer, policy=None, budget=None,
                 max_steps=40, feature=None, commit=False, allow_missing_tests=True,
                 isolate=False, open_pr=False, install_deps=True, baseline_diff=False,
                 require_fix=False, discard_previous=False, sandbox=False,
                 review=False, reviewer_proposer=None,
                 author=False, author_proposer=None, plan=None, context_prelude=None,
                 resume=False, resume_context=None, write_scope=None, base=None, defer_delivery=False,
                 calibrated_enforcement=False, ui_evidence=None,   # v3.1.8 калиброванное UI-enforcement
                 strict_judge_qualified=True,   # v3.7.1: есть ли QUALIFIED security/integration судья
                 security_reviewer_proposer=None,   # v3.7.3 (#5): ОТДЕЛЬНЫЙ security-судья (не общий reviewer)
                 reevaluate_only=False):   # v3.8.4: переоценить гейты существующего HEAD БЕЗ переавторинга
    """Один прогон движка: [worktree-изоляция] -> детект -> правки через tool-loop ->
    [commit на ветке] -> evidence (на зафиксированном SHA) -> гейты RunPlan.

    v2.108 (Operational Context): context_prelude — compiled payload из ContextBundle (реальное
    содержимое релевантных правил/решений/спек), который РЕАЛЬНО попадает в prompt модели (prepend к
    base_context tool loop) — не только статистика в отчёте.

    v2.109 (Real Resume): resume=True — ПРОДОЛЖИТЬ WorkItem поверх уже подтверждённой работы, а не
    начинать заново. Ветка ai-ops/<wid> и её коммиты НЕ удаляются (иначе потеряли бы результат);
    worktree переиспользуется (или пере-подключается к сохранившейся ветке). resume_context —
    состояние из RunHandoff (что сделано/решения/следующий шаг), реально подаётся модели в начало
    prompt, чтобы она продолжила, а не переделала подтверждённое.

    v2.94 (One Run Transaction): если plan передан контроллером — используем ЕГО (не строим второй),
    чтобы pipeline и lifecycle жили в одной транзакции с общим WorkItem/RunPlan."""
    child_root = Path(child_root)
    signals = dict(signals or {})
    signals.setdefault("task_text", task)

    # 2. план (нужен workitem_id для имени ветки/worktree). v2.94: принимаем готовый план от
    #    контроллера; иначе строим сами (обратная совместимость: прямой вызов run_pipeline).
    if plan is None:
        plan = run_plan.build_plan(signals, workitem_id=feature)
    wid = plan["workitem_id"]

    # 1b. изоляция (finding аудита): весь прогон в отдельном git worktree на ветке ai-ops/<id>,
    #     основное рабочее дерево child не трогается. work_root = каталог worktree.
    work_root, worktree_rel = child_root, None
    resume_info = ({"requested": bool(resume), "resumed": False,
                    "reused_worktree": False, "reused_branch": False}
                   if (resume or reevaluate_only) else None)
    # v3.0.1 (finding аудита P0): BASE BINDING — рабочая ветка форкается от РАЗРЕШЁННОГО base (--base),
    # а НЕ от текущего HEAD. Фиксируем base_ref+base_sha; worktree создаётся от base_sha; после — проверка;
    # delivery ревалидирует remote base. Раньше _wt.add шёл от HEAD -> `--base develop` игнорировался.
    # v3.0.7 (finding аудита P0): base=None -> AUTO-резолв (upstream/remote-default/текущая ветка), НЕ
    # хардкод 'main'. Явная base обязана существовать. base_sha берётся из резолвера; форк — от него.
    _br = _resolve_base(child_root, base)   # base может быть None (auto) или явной веткой
    base_sha = _br.get("base_sha")
    base_ref = _br.get("base_ref") or base or "HEAD"
    base_binding = {"base_ref": base_ref, "base_sha": base_sha, "mode": _br.get("mode"),
                    "resolved": bool(_br.get("resolved")), "source": _br.get("source"),
                    "reason": _br.get("reason")}
    # P0.2: ЯВНО переданная, но неразрешённая base -> preflight-блок ДО модели/worktree (не выполнять
    # от HEAD). auto всегда разрешается, поэтому блокирует только явную несуществующую ветку.
    if isolate and _br.get("mode") == "explicit" and not _br.get("resolved"):
        return {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": wid,
                "status": "error", "ready_for_pr": False, "base_binding": base_binding,
                "error": (f"base-preflight: явная база '{base}' не разрешается в ветку "
                          f"({_br.get('reason')}) — прогон остановлен ДО вызова модели (не выполняем "
                          f"от произвольного HEAD)"),
                "loop": None, "isolation": {"worktree": None}, "gates": None, "overall_status": "error"}
    if isolate:
        import worktree as _wt
        branch = f"ai-ops/{wid}"
        wp = child_root / ".ai" / "worktrees" / wid
        branch_exists = _wt._branch_exists(child_root, branch)
        # v2.109 Real Resume: продолжаем ПОВЕРХ подтверждённой работы — ветку/коммиты НЕ удаляем.
        reused = False
        if (resume or reevaluate_only) and (branch_exists or wp.is_dir()):
            if not wp.is_dir() and branch_exists:
                # worktree утерян, но ветка (коммиты) на месте -> пере-подключаем worktree к ветке
                rc = _wt.add(child_root, wid, branch)
                if rc != 0:
                    return {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": wid,
                            "status": "error",
                            "error": f"resume: не удалось пере-подключить worktree к ветке {branch} "
                                     f"(занята? не в .gitignore?) — прогон остановлен, работа не тронута",
                            "loop": None, "isolation": {"worktree": None}, "gates": None,
                            "ready_for_pr": False, "resume": {**resume_info, "resumed": False}}
                resume_info["reused_branch"] = True
            else:
                resume_info["reused_worktree"] = True
                resume_info["reused_branch"] = branch_exists
            work_root = wp
            worktree_rel = wp.relative_to(child_root).as_posix()
            resume_info["resumed"] = True
            reused = True
        if not reused:
            if resume:
                # resume запрошен, но продолжать нечего (ни ветки, ни worktree) — честный свежий старт
                resume_info["reason"] = (f"ни ветки {branch}, ни worktree нет — продолжать нечего; "
                                         f"выполняется свежий старт")
            # finding живого прогона: worktree от ПРЕДЫДУЩЕГО прогона того же wid молча
            # переиспользовался -> прогон шёл поверх грязного состояния (нечистый baseline).
            # P0.3 (аудит v2.79): но слепо удалять прошлую ветку ОПАСНО — там могут быть НЕсохранённые
            # коммиты (PR не открылся и т.п.). Удаляем только если на ветке нет работы ЛИБО явный discard.
            if wp.is_dir() or branch_exists:
                ahead = 0
                if branch_exists:
                    # коммиты на ветке ai-ops/<wid>, которых нет в текущем HEAD -> несохранённая работа
                    rc_a, out_a, _ = _git(child_root, "rev-list", "--count", branch, "^HEAD")
                    ahead = int(out_a) if rc_a == 0 and out_a.isdigit() else 0
                if ahead > 0 and not discard_previous:
                    return {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": wid,
                            "status": "error",
                            "error": f"предыдущий прогон feature='{wid}' имеет {ahead} несохранённых "
                                     f"коммит(ов) на ветке {branch}. Чтобы не потерять работу, прогон "
                                     f"остановлен. Передай resume=True (--resume) чтобы ПРОДОЛЖИТЬ "
                                     f"поверх них, discard_previous=True (--discard) для перезаписи ИЛИ "
                                     f"запусти с другим --feature.",
                            "loop": None, "isolation": {"worktree": None}, "gates": None,
                            "ready_for_pr": False, "overall_status": "error"}
                _wt.remove(child_root, wid, force=True)
                _git(child_root, "worktree", "prune")
                _git(child_root, "branch", "-D", branch)
            rc = _wt.add(child_root, wid, branch, base=(base_sha or "HEAD"))   # v3.0.1: форк от base_sha
            if rc == 0:
                work_root = wp
                worktree_rel = wp.relative_to(child_root).as_posix()
                # v3.0.1 (P0): свежая ветка обязана форкнуться РОВНО от base_sha (иначе `--base` — фикция)
                if base_sha:
                    _rc_h, _wh, _ = _git(wp, "rev-parse", "HEAD")
                    if _rc_h != 0 or (_wh or "").strip() != base_sha:
                        return {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": wid,
                                "status": "error", "base_binding": base_binding,
                                "error": (f"base binding нарушен: ветка {branch} форкнулась от "
                                          f"{(_wh or '?').strip()[:12]}, а заявлен base={base_ref}"
                                          f" ({base_sha[:12]}) — прогон остановлен"),
                                "loop": None, "isolation": {"worktree": None}, "gates": None,
                                "ready_for_pr": False, "overall_status": "error"}
        if work_root is child_root:
            # finding adversarial-review: НЕ деградируем молча в основное дерево — это исполнило бы
            # правки и коммит в main вопреки isolate=True. Останавливаемся честной ошибкой.
            return {"schema_version": 1, "kind": "execution-pipeline", "workitem_id": wid,
                    "status": "error",
                    "error": f"isolate=True, но worktree .ai/worktrees/{wid} не создан "
                             f"(ветка занята? не в .gitignore?) — прогон остановлен, основное дерево не тронуто",
                    "loop": None, "isolation": {"worktree": None}, "gates": None,
                    "ready_for_pr": False}

    # 1. детект стека (в рабочем дереве)
    profile = project_detector.detect(work_root)

    # 3. политика по умолчанию: execution, границы — по work_root.
    #    v2.81 Containment: даже базовая политика запрещает модели push-ить (block_push=True) —
    #    доставка (PR) идёт ТОЛЬКО через доверенный delivery-слой, не через tool-loop.
    #    sandbox=True дополнительно включает allowlist на shell (произвольный shell выключен)
    #    и denylist на сетевые бинарники — см. tool_broker.sandbox_policy().
    if policy is not None:
        pol = policy
    elif sandbox:
        pol = tool_broker.sandbox_policy(child_root=str(work_root), write_scope=write_scope)
    else:
        pol = tool_broker.Policy(level="execution", child_root=str(work_root), block_push=True,
                                 write_scope=write_scope)
    is_git = _git(work_root, "rev-parse", "--is-inside-work-tree")[0] == 0

    # P0.6/v2.93: снимок untracked-файлов ДО install/baseline — чтобы потом удалить только НОВЫЕ
    # (созданные подготовкой, напр. package-lock.json от `npm install`), не тронув untracked-файлы,
    # которые уже были у пользователя. Игнорируемые (node_modules) сюда не попадают.
    untracked_before_prep = _untracked(work_root) if is_git else set()

    # 3b. подготовка окружения ДО петли и baseline: поставить зависимости стека В ИЗОЛИРОВАННОМ
    #     worktree, иначе build/lint/test упадут exit 127 (нет node_modules/venv). node_modules
    #     обычно в .gitignore -> дерево остаётся чистым. В основном дереве НЕ ставим.
    prepare = None
    if install_deps and isolate:
        prepare = _install_dependencies(profile, work_root, pol)
    # P0.6 (аудит v2.79): установка зависимостей должна ПРОЙТИ — иначе baseline/проверки
    # недостоверны. Провал install -> окружение не квалифицировано, прогон не может быть ready.
    prepare_ok = (prepare is None) or all(p.get("ok") for p in prepare)

    # 3c. baseline-evidence (finding живого прогона: ii-sreda был красным САМ ПО СЕБЕ — build/
    #     typecheck/test падали до любой правки). Прогон проверок на БАЗЕ до правок модели, чтобы
    #     отличить пред-существующие провалы репо от РЕГРЕССИЙ, внесённых этой правкой.
    baseline_checks = None
    if baseline_diff:
        baseline_checks = evidence_collector.collect(profile, work_root, pol)["checks"]

    # P0.6 (аудит v2.79) + v2.93 (finding аудита): install/baseline могли намутить TRACKED-файлы
    # (lock, снапшоты, конфиги) И создать НОВЫЕ untracked (классика: `npm install` создаёт
    # package-lock.json, которого не было). Откатываем ОБА вида ДО работы модели, иначе `git add -A`
    # в коммите втянул бы файлы подготовки в AI-коммит. Откат tracked — `checkout -- .`; новые
    # untracked (delta к снимку до install) — удаляем адресно (untracked ПОЛЬЗОВАТЕЛЯ не трогаем).
    # node_modules/venv в .gitignore -> в porcelain не видны, остаются для проверок.
    prepare_mutated_tree = False
    if is_git and not _tree_clean(work_root):
        prepare_mutated_tree = True
        _git(work_root, "checkout", "--", ".")
        new_untracked = _untracked(work_root) - untracked_before_prep
        for rel in new_untracked:
            try:
                fp = (work_root / rel)
                if fp.is_file() or fp.is_symlink():
                    fp.unlink()
            except OSError:
                pass

    # 4. tool-loop: модель применяет изменения (context = задача + профиль стека +
    #    ФАКТИЧЕСКИЙ вывод падающих проверок базы — finding живого прогона: без него модель
    #    не знала, ЧТО чинить, и крутилась до max_steps с 0 правок на fix-задачах).
    ctx = f"{task}\n\n{_profile_summary(profile)}"
    # v2.108 Operational Context: compiled payload из ContextBundle РЕАЛЬНО в prompt (не только отчёт).
    if context_prelude:
        ctx = context_prelude + "\n\n" + ctx
    # v2.109 Real Resume: состояние из RunHandoff в самое начало prompt — модель ПРОДОЛЖАЕТ, а не
    # переделывает подтверждённое (что сделано / решения / следующий шаг).
    if resume_context:
        ctx = resume_context + "\n\n" + ctx
    if baseline_diff:
        fails = _baseline_failure_summary(baseline_checks)
        if fails:
            ctx += ("\n\n=== ТЕКУЩИЕ ПРОВАЛЫ ПРОВЕРОК НА БАЗЕ (почини относящиеся к задаче; "
                    "не ломай остальное) ===\n" + fails)
    # 4a. v2.123 (P0.1) НАСТОЯЩИЙ Spec-First: СНАЧАЛА автор создаёт requirements/plan/specification,
    #     движок валидирует ФОРМУ (v2.86). Невалидный author-артефакт -> tool loop НЕ запускается
    #     (ноль implementation-вызовов). Валидные артефакты подаются в prompt реализации (код по спеке).
    #     Качество артефактов судит независимый ревьюер (--review)/человек, не эта проверка формы.
    #     Раньше authoring шёл ПОСЛЕ tool loop -> с --author heavy начинал писать код до спеки (обход
    #     Spec-First). Теперь порядок: authoring -> валидация -> [tool loop только при валидной спеке].
    authored, authored_ev = None, {}
    spec_prestage_bad = []
    if author and author_proposer is not None and not reevaluate_only:
        authored_ev, authored, _wrote_art = _run_authoring(
            author_proposer, work_root, plan["gates"], {}, wid, task, budget)
        spec_prestage_bad = [e for e in (authored or []) if e.get("valid") is False]
        if not spec_prestage_bad:
            _spec_ctx = _authored_context(authored, work_root, wid)
            if _spec_ctx:
                ctx = _spec_ctx + "\n\n" + ctx

    # 4b. tool-loop: реализация. Пропускается, если pre-authoring дал невалидную спецификацию
    #     (Spec-First: нет валидной спеки -> нет кода — ноль tool-loop вызовов).
    if reevaluate_only:
        # v3.8.4: НЕ авторим и НЕ гоняем tool-loop — переоцениваем существующий HEAD ветки как есть
        # (после добавления человеко-одобрения). Ноль model-вызовов, ноль правок, план не меняется.
        loop = {"schema_version": 1, "kind": "tool-loop-report", "stopped": "reevaluate-only",
                "steps": 0, "model_calls": 0, "executed": [], "denied": [], "evidence": [], "transcript": []}
    elif spec_prestage_bad:
        loop = {"schema_version": 1, "kind": "tool-loop-report", "stopped": "spec-prestage-failed",
                "steps": 0, "model_calls": 0, "executed": [], "denied": [], "evidence": [], "transcript": []}
    else:
        loop = tool_loop.run_loop(proposer, work_root, pol, budget=budget,
                                  max_steps=max_steps, base_context=ctx)
    applied = [e for e in loop["executed"] if e.get("op") == "write" and e.get("ok")]
    # v2.93 (finding аудита): факт правок берём из git (tracked-diff ИЛИ новые untracked), а не
    # только из счётчика write-операций. Иначе правки через разрешённый shell (sed/форматтер)
    # не распознавались как «применено» -> коммит не создавался и работа терялась.
    shell_changed = bool(applied) or (is_git and _has_changes(work_root))

    # 5. commit на рабочей ветке (finding аудита: evidence должен биться о ТОЧНЫЙ SHA, не
    #    о грязное дерево поверх старого HEAD). Коммитим ДО сбора evidence.
    committed_sha, work_branch = None, None
    tree_clean_before_checks = None
    # v2.93: коммитим, если В ДЕРЕВЕ есть правки (git-diff/untracked) — включая правки через shell и
    # произведённые артефакты — а не только при непустом applied. Для не-git репо fallback на applied.
    have_work = (is_git and _has_changes(work_root)) or bool(applied) or bool(authored)
    if reevaluate_only:
        # v3.8.4: существующий HEAD ветки — уже зафиксированная работа; НЕ создаём новый коммит,
        # план/SHA не меняются -> plan-bound ApprovalRecords остаются валидны.
        work_branch = f"ai-ops/{wid}"
        _rc_h, _out_h, _ = _git(work_root, "rev-parse", "HEAD")
        committed_sha = _out_h.strip() if _rc_h == 0 else None
        tree_clean_before_checks = _tree_clean(work_root)
    elif commit and have_work:
        work_branch = f"ai-ops/{wid}"
        committed_sha = _commit_on_branch(work_root, work_branch,
                                          f"ai-ops: {task[:60]}")
        # finding аудита (P0.5): после коммита дерево обязано быть чистым — иначе часть правок
        # не в SHA, и evidence соберётся о смешанном состоянии.
        tree_clean_before_checks = _tree_clean(work_root)

    # 6. evidence: реальный прогон команд профиля через Broker (теперь дерево чистое на SHA)
    # v3.26.1 Progressive Verification: передаём changed_files для targeted test execution
    _changed_for_verification = _committed_changed_files(work_root, committed_sha) if (commit and is_git and committed_sha) else None
    coll = evidence_collector.collect(profile, work_root, pol, changed_files=_changed_for_verification)

    # 6a. finding аудита (P0.5): проверки могли намутить дерево (build-артефакты, lock-файлы) —
    #     тогда собранный evidence уже не отражает закоммиченный SHA. Фиксируем факт, не скрываем.
    # v2.119: чистота ПОСЛЕ проверок терпима к тул-кэшам (pytest/npm/... создают их рутинно);
    # tracked-правки от проверок по-прежнему делают дерево грязным (evidence-целостность сохранена).
    tree_clean_after_checks = _tree_clean_after_checks(work_root) if (commit and is_git) else None

    # 6b. intake-evidence из сигналов: классификация УЖЕ произошла (task_type/size/risk в signals) —
    #     это реальный evidence для intake_completeness, а не фабрикация (finding живого прогона).
    gate_ev = dict(coll["gate_evidence"])
    intake = _intake_evidence(signals)
    if intake:
        gate_ev.setdefault("intake_completeness", intake)
    # v2.86: evidence артефакт-гейтов (requirements/plan_readiness) из author-стадии — форма
    # подтверждена детерминированно; НЕ перетираем уже имеющееся evidence (setdefault).
    for _gid, _ev in (authored_ev or {}).items():
        gate_ev.setdefault(_gid, _ev)

    # v3.8.3 reevaluate-only: SHA НЕ менялся -> артефакт-гейты (requirements/specification/plan_readiness)
    # пере-выводим ДЕТЕРМИНИРОВАННО из существующих на диске артефактов (без модели, без чтения
    # клоббер-подверженного run-report). code_review переподтверждается ревью на том же SHA (--review).
    # security НЕ сеем — переоценим ниже с человеко-approval. setdefault -> не перетираем свежий
    # impl_verification из evidence_collector.
    if reevaluate_only:
        # (1) primary: персистированное build-evidence по committed_sha (включая model-вердикт code_review) —
        # НЕ ре-ревьюим (недетерминизм) и не зависим от клоббер-подверженного run-report;
        # (2) fallback: детерминированный re-derive артефакт-гейтов из существующих на диске артефактов.
        try:
            import json as _json
            _rep = Path(child_root) / ".ai" / f"reevaluate-evidence-{wid}.json"
            if _rep.is_file():
                _rj = _json.loads(_rep.read_text(encoding="utf-8"))
                if _rj.get("sha") == committed_sha:
                    for _gid, _ev in (_rj.get("gate_ev") or {}).items():
                        if _gid != "security":
                            gate_ev.setdefault(_gid, _ev)
        except Exception:  # noqa: BLE001
            pass
        for _gid, _ev in _reevaluate_artifact_evidence(work_root, wid, plan["gates"]).items():
            gate_ev.setdefault(_gid, _ev)

    # 6c. «умное ослабление» (v2.61): инструмента нет в подтверждённом стеке -> флаг освобождается
    #     (build/lint/typecheck). tests — особый случай: по умолчанию тоже освобождаем + громкий
    #     warn; policy allow_missing_tests=False эскалирует до блока (untested -> not ready).
    exempt = set(coll.get("not_applicable") or [])
    tests_warn = None
    if coll.get("tests_absent"):
        if allow_missing_tests:
            exempt.add("tests_passed")
            tests_warn = "нет тестов в стеке — implementation_verification освобождён по tests (allow_missing_tests=True); это осознанное послабление"
        else:
            exempt.discard("tests_passed")   # тесты обязательны -> гейт заблокирует
            tests_warn = "нет тестов, а require_tests -> implementation_verification блокирует"
    not_applicable = {"implementation_verification": exempt}

    # 6d. v2.83 Full RunPlan: постадийный НЕЗАВИСИМЫЙ ревью для ai-review гейтов плана
    #     (code_review, ux_review, security-non-human, ...). writer ≠ judge: ревьюер — отдельный
    #     вызов под READ-ONLY политикой (писать/шеллить не может), выносит СТРУКТУРНЫЙ вердикт.
    #     Честно: детерминированные артефакт-гейты (requirements/specification/plan_readiness) и
    #     human-approval (security при privileged/destructive) ревьюер НЕ закрывает — остаются
    #     блокирующими. review только на зафиксированной ревизии (иначе судить нечего).
    # v3.1.9 EXACT-SHA UI EVIDENCE (trust-фикс): собираем UI-evidence ПОСЛЕ реализации, из РАБОЧЕГО
    # worktree, на ТОЧНОМ committed_sha, по файлам, изменённым этим коммитом; связываем с committed_sha.
    # Устаревшее/непривязанное/чужое evidence (meta.commit_sha != committed_sha) -> not_run (fail-closed),
    # НЕ освобождает гейт. Инжектированный ui_evidence (bench/синтетика) используется как есть (не строим).
    ui_evidence_bundle = None
    if calibrated_enforcement and ui_evidence is None and committed_sha:
        try:
            _changed = _committed_changed_files(work_root, committed_sha)
            # v3.11.0 UI Evidence Readiness: UI-CI ТОЛЬКО при изменении UI-файлов ИЛИ VISUAL-задаче.
            # Иначе — skip (не применимо; НЕ маскируем — просто не гоняем UI-CI зря на не-UI изменении).
            import ui_readiness as _uir
            _ui_run, _ui_reason = _uir.should_run_ui_evidence(_changed, signals)
            if not _ui_run:
                ui_evidence, ui_evidence_bundle = None, None
            else:
                # v3.7 UI-CI: собрать РЕАЛЬНЫЙ UI-evidence на committed_sha (vitest interaction + axe a11y +
                # storybook visual). Не-UI child / нет артефактов -> build_bundle честно вернёт not_run/absent.
                try:
                    import ui_evidence_collect
                    ui_evidence_collect.collect(work_root, committed_sha)
                except Exception:   # noqa: BLE001 — сбор evidence не должен ронять прогон
                    pass
                ui_evidence_bundle = storybook_adapter.build_bundle(work_root, changed_files=_changed)
                ui_evidence = storybook_adapter.evidence_for_gate(ui_evidence_bundle,
                                                                  expected_sha=committed_sha)
        except Exception:   # noqa: BLE001 — сбой сбора evidence не освобождает гейт (ui_evidence=None)
            ui_evidence, ui_evidence_bundle = None, None

    # v3.7.4 SEAM-SCAN (ADVISORY, non-blocking до обкатки): детектор «дефекта шва» по дифу base..committed.
    # Surfaces тихие швы (запись без round-trip / catch без happy-path / stub без real-run / optional-поле
    # в контракте / смена предусловия без аудита вызывающих). НЕ блокирует (advisory); станет gate после
    # обкатки на child. Экономия/скорость НЕ ослабляют проверку (ADR-004).
    seam_advisory = None
    if committed_sha:
        try:
            import seam_scan
            _diff = _change_context_range(work_root, base_sha, committed_sha, max_chars=20000)
            _sc = seam_scan.scan_diff(_diff or "")
            _dec = seam_scan.gate_decision(_sc)
            seam_advisory = {"mode": "advisory", "would_block": _dec["block"],
                             "blockers": _dec["blockers"], "advisories": _dec["advisories"],
                             "findings": _sc["findings"]}
        except Exception as _e:  # noqa: BLE001 — advisory-детектор не должен ронять прогон
            seam_advisory = {"error": f"seam_scan failed: {type(_e).__name__}: {_e}"[:200]}

    reviews = None
    if review and reviewer_proposer is not None and committed_sha:
        gate_ev, reviews = _run_reviews(reviewer_proposer, work_root, plan["gates"], gate_ev,
                                        signals, committed_sha, budget,
                                        calibrated_enforcement=calibrated_enforcement,
                                        ui_evidence=ui_evidence)

    # 6e. v2.95 -> v2.101 Security Pack: доменный security-вердикт (security/security-domains.yaml).
    #     Проверяются только ПРИМЕНИМЫЕ к изменению домены; детерминированные (secrets/deps/injection)
    #     ловятся с деталями и блокируют по severity. Домены, чьё required_evidence целиком
    #     детерминированно (secrets/dependencies), авто-закрываются при чистоте; домены с
    #     security_reviewer/human — needs_review (судья/человек). security проходит ТОЛЬКО если
    #     pack "clear" (все применимые домены закрыты детерминированно) — иначе честный блок.
    security_pack_result = None
    _security_scan_error = None
    # v2.125 (finding живого прогона): security pack запускается на ЛЮБОМ коммите (не только когда
    # "security" в плане workflow). Security-релевантная находка в диффе (новая зависимость/секрет)
    # обязана быть замечена и в QUICK — иначе новая зависимость в QUICK-задаче проскакивала без
    # ApprovalRecord. Если находка -> gate_ev.security=fail -> ниже security форсируется в оценку гейтов.
    if committed_sha and is_git and "security" not in gate_ev:
        import security_pack
        try:
            security_pack_result = security_pack.run_pack(work_root, base=f"{committed_sha}~1", signals=signals)
        except Exception as _e:  # noqa: BLE001
            _security_scan_error = str(_e)
            security_pack_result = None
    # v3.0-rc2 (P0.6): universal security scan — техническая ОШИБКА скана = FAIL-CLOSED, а не тихий обход.
    # Раньше exception -> result=None -> security-гейт не добавлялся -> QUICK оставался зелёным.
    effective_approval_signals = dict(signals)   # v3.0-rc2 (P0.5): signals намерения + findings-derived
    if _security_scan_error:
        gate_ev = dict(gate_ev)
        gate_ev["security"] = {"status": "fail",
                               "blockers": [f"security scan упал (fail-closed): {_security_scan_error}"]}
    elif security_pack_result:
        overall = security_pack_result["overall"]
        gate_ev = dict(gate_ev)
        # v2.123 (P0.2): ЕДИНЫЙ ApprovalDecision. Требования человеко-одобрения выводим из ВХОДНЫХ signals
        # И из РЕАЛЬНЫХ находок security pack (новая зависимость/секрет, внесённые самой правкой), даже
        # если сигнала заранее не было. boolean signals.human_approved БОЛЬШЕ НЕ используется — засчитывается
        # ТОЛЬКО валидный ApprovalRecord (человек). Reviewer (writer≠judge) НЕ заменяет человеко-одобрение.
        import approvals as _appr
        _merged_sig = {**signals, **_appr.signals_from_findings(security_pack_result)}
        effective_approval_signals = _merged_sig   # v3.0-rc2 (P0.5): используется и в recheck ниже
        _appr_missing = list(_appr.check(_merged_sig, child_root, wid).get("missing") or [])
        if _merged_sig.get("destructive"):
            _recs = _appr.load_approvals(child_root, wid)
            # v3.0.11 (finding аудита P1): destructive — high-risk, поэтому STRICT-валидация (expiry +
            # plan-binding + trusted source), как для остальных high-risk доменов. Прежде вызывался
            # _record_valid(r) с дефолтами -> просроченное/привязанное к другому плану/недоверенное
            # одобрение проходило (слабее, чем approvals.check() для high-risk).
            _dnow = _appr._now_iso()
            _dph = _appr.plan_binding_hash(child_root, wid)
            if not any(r.get("approval") == "destructive"
                       and _appr._record_valid(r, now=_dnow, plan_hash=_dph, strict=True) for r in _recs):
                _appr_missing.append({"domain": "destructive",
                                      "reason": "нет строго-валидного ApprovalRecord для деструктивного "
                                                "действия (expiry/plan-binding/trusted source)"})
        human_ok = not _appr_missing
        if not human_ok:
            # человеко-одобрение требуется (по сигналам ИЛИ по находкам диффа) и его нет -> fail, независимо
            # от чистого scan / pass ревьюера.
            gate_ev["security"] = {"status": "fail",
                                   "blockers": [f"{m['domain']}: {m.get('reason', 'нужно человеко-одобрение (ApprovalRecord)')}"
                                                for m in _appr_missing],
                                   "approvals_missing": _appr_missing,
                                   "pack": {"applicable": security_pack_result["applicable_domains"]}}
        elif overall == "clear":
            gate_ev["security"] = {"status": "pass",
                                   "provided": ["no_secrets", "no_injection_surface", "deps_approved"],
                                   "pack": {"applicable": security_pack_result["applicable_domains"],
                                            "note": "все применимые security-домены закрыты детерминированным evidence"}}
        elif (overall == "needs_review" and not security_pack_result["blocking"]
              and review and committed_sha and not strict_judge_qualified
              and not (signals or {}).get("_sequence_internal")):
            # v3.7.3 (#5) STRICT SECURITY JUDGE: security needs_review закрывает ТОЛЬКО КВАЛИФИЦИРОВАННЫЙ
            # security-судья (strict_judge_qualified) ЛИБО ЧЕЛОВЕК (ApprovalRecord). Общий code reviewer НЕ
            # закрывает security. Нет qualified судьи -> pending_human ДО валидного человеческого одобрения.
            # ПОД-ПАКЕТ executor'а (_sequence_internal) НЕ хардстопим здесь: security судится на АГРЕГАТЕ
            # (integration-SHA, _aggregate_close_security). Enforcement #5 на агрегате executor'а — следующий шаг.
            import approvals as _appr_sec
            _sec_recs = _appr_sec.load_approvals(child_root, wid)
            _sec_now, _sec_ph = _appr_sec._now_iso(), _appr_sec.plan_binding_hash(child_root, wid)
            _sec_domains = {"security", "security_review", *security_pack_result["needs_review"]}
            # v3.8.3: одобрение валидно, если привязано к ревизии плана (_sec_ph) ЛИБО к ТОЧНОМУ committed_sha.
            # SHA-binding стабилен при reevaluate (SHA не меняется, даже если run() перезаписал run-plan.yaml)
            # и семантически сильнее: человек одобряет КОНКРЕТНЫЙ код, а не ревизию плана (как aggregate #4b).
            def _appr_valid_here(r):
                return (_appr_sec._record_valid(r, now=_sec_now, plan_hash=_sec_ph, strict=True)
                        or (committed_sha and _appr_sec._record_valid(r, now=_sec_now, plan_hash=committed_sha, strict=True)))
            _human_closed = any(r.get("approval") in _sec_domains and _appr_valid_here(r) for r in _sec_recs)
            if _human_closed:
                gate_ev["security"] = {"status": "pass",
                                       "provided": ["no_secrets", "no_injection_surface", "deps_approved"],
                                       "human_approved": True,
                                       "pack": {"applicable": security_pack_result["applicable_domains"],
                                                "note": "нет qualified security-судьи -> needs_review закрыт "
                                                        "человеком (валидный ApprovalRecord)"}}
            else:
                gate_ev["security"] = {"status": "fail", "human_handoff": True, "pending_human": True,
                                       "blockers": ["нет QUALIFIED security-судьи: needs_review домены "
                                                    "закрывает ТОЛЬКО квалифицированный судья или человек "
                                                    "(ApprovalRecord); общий code reviewer НЕ закрывает "
                                                    "security: " + ", ".join(security_pack_result["needs_review"])],
                                       "pack": {"applicable": security_pack_result["applicable_domains"],
                                                "needs_review": security_pack_result["needs_review"]}}
        elif (overall == "needs_review" and not security_pack_result["blocking"]
              and review and committed_sha and (security_reviewer_proposer or reviewer_proposer) is not None):
            # v2.106/v3.7.3: КВАЛИФИЦИРОВАННЫЙ security-судья (strict_judge_qualified) закрывает needs_review.
            # Судья — ОТДЕЛЬНЫЙ security_reviewer_proposer (не общий code reviewer); fallback только если он
            # не передан (совместимость). Блокирующие детерминированные находки судья НЕ переопределяет.
            sec_status, sec_res = _review_security(security_reviewer_proposer or reviewer_proposer, work_root,
                                                   security_pack_result, committed_sha, budget)
            if sec_status == "pass":
                gate_ev["security"] = {"status": "pass",
                                       "provided": ["no_secrets", "no_injection_surface", "deps_approved"],
                                       "reviewer": {"status": sec_status},
                                       "pack": {"applicable": security_pack_result["applicable_domains"],
                                                "note": "детерминированные домены чисты + независимый "
                                                        "security-reviewer вынес pass по needs_review"}}
            else:
                # v3.6.8 (finding живой квалификации): раньше причина отказа вердикта была НЕМА
                # («не вынес pass»). Теперь фиксируем ТОЧНЫЕ ошибки вердикта (_review_security кладёт их
                # в res["invalid"]) + структурную диагностику — чтобы видеть, промпт/формат это или модель.
                _inv = sec_res.get("invalid") if isinstance(sec_res, dict) else None
                _raw = (sec_res.get("raw") if isinstance(sec_res, dict) and "raw" in sec_res else sec_res)
                _diag = {}
                if isinstance(_raw, dict):
                    _dr = _raw.get("domain_results")
                    _diag = {"raw_keys": sorted(_raw.keys()),
                             "has_domain_results": isinstance(_dr, list) and bool(_dr),
                             "domain_results_count": len(_dr) if isinstance(_dr, list) else 0}
                gate_ev["security"] = {"status": "fail", "blockers": ["security-reviewer не вынес pass"],
                                       "reviewer": {"status": sec_status},
                                       "verdict_errors": _inv, "verdict_diag": _diag,
                                       "pack": {"applicable": security_pack_result["applicable_domains"],
                                                "needs_review": security_pack_result["needs_review"]}}
        else:
            blockers = []
            if security_pack_result["blocking"]:
                blockers.append("блокирующие домены (critical/high находки): " + ", ".join(security_pack_result["blocking"]))
            if security_pack_result["needs_review"]:
                blockers.append("нужен независимый security-reviewer/человек по доменам: "
                                + ", ".join(security_pack_result["needs_review"]))
            gate_ev["security"] = {"status": "fail", "blockers": blockers,
                                   "pack": {"applicable": security_pack_result["applicable_domains"],
                                            "blocking": security_pack_result["blocking"],
                                            "needs_review": security_pack_result["needs_review"]}}

        # v3.0-rc20 (finding аудита P0): БРАНЧ-НЕЗАВИСИМАЯ проверка — high-risk домены, применимые ПО
        # РЕАЛЬНО ИЗМЕНЁННЫМ ПУТЯМ (Dockerfile/CI/auth), требуют человеческого ApprovalRecord, даже если
        # security-reviewer/детерминированные проверки дали pass. Неожиданное изменение прод-конфига без
        # одобрения -> security=fail. Форсируется поверх любой ветки выше.
        # v3.0.2 (finding аудита P1): изменённые файлы берём из EXECUTION-root (worktree), а
        # ApprovalRecord'ы/plan-binding — из LIFECYCLE-root (child_root/features), где их создаёт человек
        # после preflight-блока. Раньше и то и другое читалось из work_root -> человеческое одобрение в
        # lifecycle-каталоге отсутствовало в worktree -> ложный uncovered.
        try:
            import security_scan as _ss
            _sec_changed = _ss._git_changed_files(work_root, committed_sha + "^") if committed_sha else []
        except Exception:  # noqa: BLE001
            _sec_changed = []
        _hu = _human_approval_domains_uncovered(child_root, wid, _sec_changed, diff_root=work_root)
        if _hu and (gate_ev.get("security") or {}).get("status") != "fail":
            gate_ev["security"] = {"status": "fail",
                                   "blockers": ["high-risk изменение по путям без человеческого ApprovalRecord "
                                                "(reviewer не закрывает): " + ", ".join(_hu)],
                                   "human_approval_uncovered": _hu,
                                   "pack": {"applicable": security_pack_result["applicable_domains"]}}

    # 7. гейты RunPlan (base + треки), c evidence из коллектора + сигналы (условный approval) +
    #    освобождения по неприменимым проверкам. tested_revision -> в evidence/аудит гейтов.
    # v2.125 (finding живого прогона): security-релевантная НАХОДКА в диффе (новая зависимость/секрет →
    # gate_ev.security=fail) обязана блокировать НЕЗАВИСИМО от workflow. QUICK не содержит security-гейта,
    # поэтому новая зависимость в QUICK-задаче проскакивала. Форсируем security в оценку, если он упал.
    _gate_ids = list(plan["gates"])
    if (gate_ev.get("security") or {}).get("status") == "fail" and "security" not in _gate_ids:
        _gate_ids.append("security")
    gates = gate_executor.evaluate(plan["base_workflow"], gate_ev,
                                   gate_ids=_gate_ids, tested_revision=committed_sha,
                                   signals=signals, not_applicable=not_applicable)

    # v3.8.3: персистим ПРОЙДЕННОЕ gate-evidence билда (кроме security) по committed_sha в worktree/.ai —
    # чтобы последующий reevaluate (после человеко-approval) переиспользовал model-вердикт code_review и
    # артефакт-гейты БЕЗ ре-ревью (недетерминизм) и без зависимости от клоббер-подверженного run-report.
    # Только non-reevaluate билд с коммитом (reevaluate не перетирает источник).
    if committed_sha and not reevaluate_only and worktree_rel is not None:
        try:
            import json as _json
            _passed = {gid: ev for gid, ev in gate_ev.items()
                       if gid != "security" and isinstance(ev, dict) and ev.get("status") == "pass"}
            # пишем в child_root/.ai (репо-корень), ВНЕ worktree-дерева -> не грязним committed_sha
            (Path(child_root) / ".ai").mkdir(parents=True, exist_ok=True)
            (Path(child_root) / ".ai" / f"reevaluate-evidence-{wid}.json").write_text(
                _json.dumps({"sha": committed_sha, "gate_ev": _passed}, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    # честность evidence: ревизия сбора совпадает с зафиксированным SHA (если коммитили)
    evidence_revision = coll.get("revision")
    revision_matches = (committed_sha is not None and evidence_revision == committed_sha)

    # v2.106 #2 Spec-depth enforcement: разделы спецификации уровня задачи, ЗАКРЫВАЕМЫЕ evidence
    # гейтов, но незакрытые -> блокируют ready. Маппим только доказуемые разделы (недоказуемые не
    # над-блокируем). Это подмножество unmet-гейтов -> не блокирует сверх гейтов, но делает
    # spec-depth явным ready-критерием ("реализация не начинается без блокирующих разделов").
    import spec_levels as _sl
    _SECTION_GATE = {
        "goal": "intake_completeness", "scope": "intake_completeness",
        "acceptance_criteria": "intake_completeness",
        "requirements": "requirements", "acceptance_scenarios": "requirements",
        "implementation_plan": "plan_readiness", "verification_strategy": "implementation_verification",
        "problem": "discovery_completeness", "users_jtbd": "discovery_completeness",
        "value": "discovery_completeness", "success_metrics": "analytics_readiness",
    }
    _unmet = set(gates["unmet_gates"])
    # v3.8.4 (finding живой full-stack квалификации): spec_depth ДОЛЖЕН быть baseline-осведомлён.
    # verification_strategy маппится на implementation_verification; в baseline-diff режиме этот гейт
    # baseline-освобождён (красная база не блокирует — см. other_blocking_unmet ниже). Раньше spec_depth
    # брал СЫРОЙ _unmet -> предсуществующий провал базы (напр. flaky date-тест) блокировал ready через
    # verification_strategy, ОБХОДЯ baseline-diff. Теперь: если правка НЕ внесла новых регрессий, гейт
    # implementation_verification не считается незакрытым и для spec_depth (реальная регрессия ПРАВКИ —
    # по-прежнему блокирует, т.к. тогда _diff_checks вернёт непустые regressions).
    _iv_baseline_exempt = bool(baseline_diff) and not _diff_checks(baseline_checks, coll["checks"])[0]
    _unmet_for_spec = (_unmet - {"implementation_verification"}) if _iv_baseline_exempt else _unmet
    _level = _sl.classify(signals)["level"]
    _req_sections = set(_sl.required_sections(_level))
    spec_depth_missing = sorted({s for s, g in _SECTION_GATE.items()
                                 if s in _req_sections and g in plan["gates"] and g in _unmet_for_spec})
    spec_depth_ok = not spec_depth_missing

    # v2.110 Real Spec-First enforcement: если для этого WorkItem СУЩЕСТВУЕТ явный spec-артефакт
    # (features/<wid>/spec.yaml), но он НЕ полон (есть blocking_missing) -> «неполная спека не
    # пускает в implementation» (аудит). Спеки нет -> поведение прежнее (spec-first опционален для
    # мелких задач, spec_depth через гейты). Читаем реальный артефакт, а не сигналы.
    spec_incomplete = []
    try:
        _cov = _sl.assess_from_artifacts(signals, child_root, wid, work_root=work_root)
        if _cov.get("spec_artifact") and _cov.get("blocking_missing"):
            spec_incomplete = list(_cov["blocking_missing"])
    except Exception as _e:  # noqa: BLE001 — v3.0.11 (finding аудита P2): FAIL-CLOSED. Прежде исключение
        # -> spec_incomplete=[] -> spec_complete_ok=True: реальный, но неоцениваемый spec.yaml проходил в
        # реализацию. Теперь ошибка оценки спеки = блокирующий незакрытый пункт (не тихий пропуск).
        spec_incomplete = [f"<spec-assess-failed: {type(_e).__name__}>"]
    spec_complete_ok = not spec_incomplete

    # v2.106 #3 Context-budget enforcement: если контекст задачи превышает бюджет (ContextBundle
    # overflow) -> пакет не атомарен, доставлять как один нельзя -> блок ready (аудит: "при
    # превышении context budget выполнение блокируется или задача дробится"). Мягкие оси
    # (подсистемы/размер) остаются advisory (в report['work_package']), блокирует только жёсткий лимит.
    context_overflow = False
    try:
        import context_compiler as _cc
        _bundle = _cc.compile_bundle(signals, work_root, plan=plan)
        context_overflow = bool(_bundle.get("overflow"))
    except Exception:  # noqa: BLE001 — v3.0.11 (finding аудита P2): FAIL-CLOSED. Прежде исключение при
        # сборке bundle -> context_overflow=False -> блокер «задача превышает context budget» тихо исчезал,
        # и over-budget задача проходила в ready. Теперь ошибка = считаем overflow (блокируем, не молчим).
        context_overflow = True

    # baseline-diff (finding живого прогона): что правка сломала/починила против базы
    regressions, fixed = _diff_checks(baseline_checks, coll["checks"]) if baseline_diff else ([], [])
    no_regressions = (len(regressions) == 0) if baseline_diff else None
    # P0.1 (аудит v2.79): baseline-режим делает baseline-осведомлённым ТОЛЬКО
    # implementation_verification (красная база не должна блокировать). ВСЕ ОСТАЛЬНЫЕ блокирующие
    # гейты (requirements/specification/plan/code_review/security/треки) остаются обязательными —
    # иначе baseline-diff обходит их и выдаёт ложный ready. unmet_gates уже только блокирующие.
    other_blocking_unmet = [g for g in gates["unmet_gates"] if g != "implementation_verification"]

    # 8. финал: draft PR (только если готово к PR и явно запрошено). Механизм честен offline:
    #    нет токена/remote -> unavailable, PR не имитируется.
    # finding аудита (P0.5): ready_for_pr ТРЕБУЕТ реального коммита (committed_sha),
    # evidence на точном SHA и чистого дерева до/после проверок. dry-run (commit=False) НИКОГДА
    # не бывает ready — нет ревизии, к которой привязать draft PR.
    tree_ok = bool(tree_clean_before_checks) and (tree_clean_after_checks is not False)
    # P0.6 + v2.118: окружение квалифицировано, если install прошёл ЛИБО проверки реально отработали
    # (нет симптомов неподготовленного окружения). Провал install при прошедших проверках больше не
    # даёт false-negative (finding живого прогона: `pip install -e .` падает на не-пакете, а pytest
    # проходит) — при этом сломанное окружение (exit 127 / нет тулчейна) по-прежнему блокирует.
    # v2.121 (P1.4): провал install игнорируется ТОЛЬКО если окружение ДОКАЗАННО рабочее — хотя бы
    # одна проверка реально отработала (pass или честный fail без env-симптома). Нет проверок вовсе
    # ИЛИ все падения — env-симптомы -> НЕ квалифицировано (дыра v2.118 закрыта).
    env_qualified = prepare_ok or _env_proven_ok(coll["checks"])

    # v2.121 (P1.2, п.4): ПОСЛЕ диффа перепроверяем, что человеко-одобрение покрывает РЕАЛЬНО
    # изменённые пути. Preflight проверил наличие одобрения ДО правок; здесь — что scope одобрения
    # накрыл то, что модель реально тронула. scope не покрывает изменения -> одобрено не то -> НЕ ready.
    approval_recheck = {"ok": True, "uncovered": []}
    if commit and committed_sha:
        try:
            import approvals as _appr
            _changed = _committed_changed_files(work_root, committed_sha)
            # v3.0-rc2 (P0.5): recheck по ЭФФЕКТИВНЫМ сигналам (намерение + findings-derived), а не только
            # входным — иначе scope одобрения для НАЙДЕННОЙ зависимости/секрета не перепроверяется на дифф.
            approval_recheck = _appr.recheck_after_diff(child_root, wid, _changed, signals=effective_approval_signals)
            # v3.0-rc5 (P1.2): SEMANTIC dependency approval — каждая НОВАЯ зависимость из диффа должна
            # покрываться ApprovalRecord с covers_packages для ИМЕННО этого пакета (не только путём файла).
            _dep_findings = [f for r in ((security_pack_result or {}).get("results") or [])
                             for f in (r.get("findings") or []) if f.get("type") == "new_dependency"]
            if _dep_findings:
                _dep_rc = _appr.recheck_dependencies(child_root, wid, _dep_findings)
                if not _dep_rc.get("ok"):
                    approval_recheck = {"ok": False,
                                        "uncovered": (approval_recheck.get("uncovered") or []) + _dep_rc["uncovered"],
                                        "dependency_uncovered": _dep_rc["uncovered"]}
        except Exception as _e:  # noqa: BLE001 — v2.123 (P0.2b): approval FAIL-CLOSED. Сбой recheck НЕ
            # трактуется как «покрыто»: для одобрения безопаснее заблокировать, чем пропустить непроверенное.
            approval_recheck = {"ok": False, "uncovered": [{"domain": "*", "reason": f"recheck упал: {_e}"}],
                                "error": str(_e)}
    approvals_cover_ok = bool(approval_recheck.get("ok"))

    # v3.8.4 (finding живой квалификации): reevaluate-only — легитимно завершённый прогон (0 шагов,
    # переоценка существующего committed HEAD после человеко-одобрения). Раньше base_ok требовал
    # stopped=="done" -> reevaluate НИКОГДА не мог достичь ready_for_pr (delivery-after-approval путь был
    # недостижим). Остальные условия base_ok (committed_sha/revision/tree/env/approvals) по-прежнему строги.
    base_ok = (loop["stopped"] in ("done", "reevaluate-only")) and (committed_sha is not None) \
        and revision_matches and tree_ok and env_qualified and approvals_cover_ok
    if baseline_diff:
        # критерий «no-regressions»: implementation_verification baseline-осведомлён (красная база
        # не блокирует), НО все ОСТАЛЬНЫЕ блокирующие гейты обязательны (P0.1). require_fix (для
        # fix-задач): дополнительно требуем, чтобы правка РЕАЛЬНО починила падавшую проверку.
        ready = base_ok and no_regressions and (not other_blocking_unmet) \
            and (not require_fix or len(fixed) > 0) and spec_depth_ok and (not context_overflow) \
            and spec_complete_ok
        ready_criterion = "no-regressions+require-fix" if require_fix else "no-regressions"
    else:
        ready = base_ok and (not gates["blocked"]) and spec_depth_ok and (not context_overflow) \
            and spec_complete_ok
        ready_criterion = "all-green"

    # 8. доставка (P0.4 аудит v2.79): draft PR отделён от ready_for_pr. Если --open-pr запрошен,
    #    УСПЕХ прогона требует реально открытого PR; провал доставки не маскируется зелёным.
    # v3.0.16 Phase A (finding аудита #1): run_pipeline НИКОГДА не выполняет внешнюю доставку — только
    # возвращает DeliveryPlan. Единственный разрешённый вызывающий _deliver_pr — транзакционный контроллер
    # (ai_ops_run), который доставляет ТОЛЬКО после durable-фиксации RunHandoff+report+journal +
    # DeliveryIntent. Так прямой вызов run_pipeline(..., open_pr=True) больше НЕ может обойти lifecycle-
    # барьер (прежде defer_delivery=False давал inline-доставку). Параметр defer_delivery устарел и
    # игнорируется (внешнее действие из pipeline запрещено архитектурно).
    delivery_plan = None
    can_deliver = bool(open_pr and ready and committed_sha and work_branch)
    if can_deliver:
        delivery = {"requested": True, "base_binding": base_binding, "status": "planned",
                    "reason": "доставку выполняет ТОЛЬКО транзакционный контроллер после durable-фиксации "
                              "lifecycle (run_pipeline не открывает PR)"}
        delivery_plan = {"ready_for_delivery": True, "work_root": str(work_root), "work_branch": work_branch,
                         "base_ref": base_ref, "base_sha": base_sha, "committed_sha": committed_sha,
                         "wid": wid, "task": task, "base_binding": base_binding}
    else:
        delivery = {"requested": bool(open_pr), "base_binding": base_binding,
                    "status": ("not-requested" if not open_pr
                               else ("not-attempted" if not ready else None))}
    # ready есть, доставка НЕ выполнена в pipeline: overall — «готово к доставке» (контроллер финализирует).
    overall_status = ("error" if not ready else
                      ("ready-undelivered" if can_deliver
                       else ("delivered" if not open_pr else "delivery-failed")))

    not_yet = ["живой предложитель (swap провайдера)"]
    if spec_prestage_bad:
        not_yet.insert(0, "spec-first (P0.1): author вернул невалидную спецификацию ["
                       + ", ".join(str(e.get("gate")) for e in spec_prestage_bad)
                       + "] — реализация НЕ запускалась (0 tool-loop вызовов); почини author/спеку")
    if not commit:
        not_yet.insert(0, "commit+reverify (запусти с commit=True) — без коммита ready_for_pr всегда False")
    if not env_qualified:
        not_yet.insert(0, "окружение не квалифицировано: install упал И проверки не смогли отработать "
                          "(нет тулчейна/зависимостей) — почини установку стека")
    if not open_pr:
        not_yet.append("draft PR (запусти с open_pr=True + GITHUB_TOKEN)")
    if spec_depth_missing:
        not_yet.append("spec-depth: не закрыты разделы уровня " + ", ".join(spec_depth_missing))
    if spec_incomplete:
        not_yet.append("spec-first: features/<wid>/spec.yaml неполон — заполни разделы: "
                       + ", ".join(spec_incomplete))
    if context_overflow:
        not_yet.append("context budget превышен — задачу нужно декомпозировать (см. work_package)")
    if not approvals_cover_ok:
        not_yet.insert(0, "human-approval: scope одобрения не покрывает изменённые пути ("
                       + ", ".join(u["domain"] for u in approval_recheck.get("uncovered") or [])
                       + ") — переодобри под фактический дифф")

    return {
        "schema_version": 1, "kind": "execution-pipeline",
        "workitem_id": plan["workitem_id"],
        "base_workflow": plan["base_workflow"],
        "profile": {"stacks": [s.get("language") for s in profile.get("stacks", [])],
                    "undetermined": profile.get("undetermined", [])},
        # v2.81 Containment: честная декларация действующей политики изоляции (что реально
        # enforced в этом прогоне) — sandbox сужает shell до allowlist; block_push всегда True.
        "containment": {"sandbox": sandbox, "shell_mode": pol.shell_mode,
                        "block_push": pol.block_push, "allow_network": pol.allow_network,
                        "note": "enforceable-подмножество на уровне брокера; полная FS/сеть/ресурс-"
                                "изоляция — контейнерный runtime"},
        "loop": {"stopped": loop["stopped"], "steps": loop["steps"],
                 "applied_writes": len(applied), "denied": len(loop["denied"]),
                 # observability (finding живого прогона): без трейса не понять, ПОЧЕМУ петля
                 # уткнулась в max_steps (модель флудит read? denied? bad-json?). Компактный трейс.
                 "denied_reasons": [d.get("reason") for d in loop["denied"]][:10],
                 "transcript": [{k: t.get(k) for k in ("step", "op", "allowed", "ok", "done", "reason")
                                 if k in t} for t in (loop.get("transcript") or [])][:40]},
        "isolation": {"worktree": worktree_rel},   # каталог изоляции (None -> прогон в основном дереве)
        "base_binding": base_binding,              # v3.0.1 (P0): base_ref + base_sha, от которого форкнута ветка
        "resume": resume_info,                     # v2.109: продолжение поверх подтверждённой работы (None если resume не запрошен)
        "prepare": prepare,                        # установка зависимостей стека (npm ci/... ) в worktree; None вне изоляции
        "prepare_ok": prepare_ok,                  # install-команды стека прошли (для наблюдаемости)
        "env_qualified": env_qualified,            # v2.118: install прошёл ЛИБО проверки реально отработали
        "prepare_mutated_tree": prepare_mutated_tree,  # P0.6: подготовка меняла tracked -> откачено до модели
        "commit": {"branch": work_branch, "sha": committed_sha,
                   "evidence_revision": evidence_revision,
                   "evidence_on_exact_sha": revision_matches,
                   "tree_clean_before_checks": tree_clean_before_checks,
                   "tree_clean_after_checks": tree_clean_after_checks},
        "checks": coll["checks"],
        "exemptions": sorted(exempt),          # флаги, освобождённые как неприменимые (видно, не тихо)
        "tests_warn": tests_warn,              # громкий сигнал об отсутствии тестов (если есть)
        "gates": {"evaluated": gates["evaluated_gates"], "unmet": gates["unmet_gates"],
                  "blocked": gates["blocked"],
                  "other_blocking_unmet": other_blocking_unmet,   # P0.1: блокирующие ≠ impl_verification
                  # evidence/аудит (аудит v2.79): полные per-gate результаты, не только сводка
                  "gate_results": gates.get("gate_results"),
                  "tested_revision": committed_sha},
        # v2.121 (P1.2 п.4): покрыло ли человеко-одобрение фактически изменённые пути (после диффа)
        "approval_recheck": approval_recheck,
        # v2.83 Full RunPlan: трейс независимых ревью (какие ai-review гейты судились, вердикт,
        # что читал судья, что отклонено). None -> ревью не запускалось (нет --review/reviewer).
        "reviews": reviews,
        # v3.1.9: UIEvidenceBundle, собранный на ТОЧНОМ committed_sha (qualification evidence). None,
        # если калибровка выкл / evidence инжектировано / нет коммита. commit_sha в bundle привязан.
        "ui_evidence": ui_evidence_bundle,
        # v3.7.4: seam-scan advisory (дефект шва по дифу base..committed). would_block=true -> шов без
        # доказанного перехода; пока advisory (не влияет на overall), станет gate после обкатки.
        "seam_scan": seam_advisory,
        # v2.95: детерминированный security-скан (секреты/новые зависимости/injection-флаги). None,
        # если гейта security нет в плане или не коммитили. Закрывает no_secrets/deps_approved (факты);
        # no_injection_surface — судье. Находка -> security блокирует.
        "security_scan": ({"overall": security_pack_result["overall"],
                           "applicable_domains": security_pack_result["applicable_domains"],
                           "blocking": security_pack_result["blocking"],
                           "needs_review": security_pack_result["needs_review"]}
                          if security_pack_result else None),
        # v2.86 Product Authoring: трейс произведённых артефактов (requirements/plan) — что
        # авторизовано, валидна ли форма, какие required_evidence закрыты. None -> без --author.
        "authored": authored,
        # baseline-diff: None вне режима; иначе — статусы проверок на базе + регрессии/починки
        "baseline": ({"checks": {k: (v or {}).get("status") for k, v in (baseline_checks or {}).items()},
                      "regressions": regressions, "fixed": fixed, "no_regressions": no_regressions}
                     if baseline_diff else None),
        "ready_criterion": ready_criterion,    # all-green | no-regressions
        # v2.106 enforcement: spec-depth (незакрытые разделы уровня, мапящиеся на unmet-гейты) и
        # context-budget overflow — блокируют ready наравне с гейтами.
        "spec_depth": {"level": _level, "missing": spec_depth_missing, "ok": spec_depth_ok},
        # v2.110 Real Spec-First: реальный spec.yaml существует, но неполон -> блокирует implementation
        "spec_first": {"artifact_present": bool(spec_incomplete) or _sl._spec_path(child_root, wid).is_file(),
                       "incomplete_sections": spec_incomplete, "ok": spec_complete_ok,
                       # v2.123 (P0.1): pre-authoring запущен ДО реализации; невалидная спека -> 0 кода
                       "prestage": {"ran": bool(author and author_proposer is not None),
                                    "invalid": [e.get("gate") for e in spec_prestage_bad],
                                    "implementation_skipped": bool(spec_prestage_bad)}},
        "context_overflow": context_overflow,
        # honest: «готово к PR» = петля done + коммит + evidence на SHA + prepare_ok + spec-depth +
        # не-overflow + (all-green: гейты не блокируют | no-regressions: нет новых провалов И blocking-гейты пройдены)
        "ready_for_pr": ready,
        "delivery": delivery,                  # P0.4: статус доставки draft PR отдельно от ready
        "delivery_plan": delivery_plan,        # v3.0.15 (P0): план для контроллера при defer_delivery
        "overall_status": overall_status,      # error | delivery-failed | delivered | ready-undelivered
        "draft_pr": delivery.get("pr"),        # результат открытия PR (None если deferred/не открыт)
        "not_yet": not_yet,
    }


def selftest():
    import tempfile
    import subprocess
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        subprocess.run(["git", "-C", td, "init", "-q"])
        subprocess.run(["git", "-C", td, "config", "user.email", "t@t"])
        subprocess.run(["git", "-C", td, "config", "user.name", "t"])
        (root / "src").mkdir()
        # python-профиль БЕЗ тулчейна (нет ruff/mypy/pytest, нет tests/) -> все проверки
        # not_applicable детерминированно (не зависим от наличия pytest в среде selftest).
        (root / "pyproject.toml").write_text(
            "[tool.poetry]\nname='x'\n[tool.poetry.dependencies]\n", encoding="utf-8")
        (root / "f").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", td, "add", "-A"]); subprocess.run(["git", "-C", td, "commit", "-q", "-m", "i"])

        # mock-предложитель: пишет файл в scope, читает его, done
        script = [
            {"op": "write", "path": "src/add.py", "content": "def add(a,b): return a+b\n"},
            {"op": "read", "path": "src/add.py"},
            {"done": True, "summary": "добавил add"},
        ]
        it = iter(script)
        pol = tool_broker.Policy(level="execution", write_scope=["src/"])
        sig = {"task_type": "QUICK", "size": "small", "risk": "low", "affected_areas": ["core"]}
        rep = run_pipeline("добавить функцию add", sig, root, lambda c: next(it),
                           policy=pol, budget={"max_model_calls": 10}, feature="add-fn")

        expect("pipeline: петля дошла до done", rep["loop"]["stopped"] == "done")
        expect("pipeline: изменение применено (write)", rep["loop"]["applied_writes"] == 1
               and (root / "src" / "add.py").exists())
        expect("pipeline: профиль определил python", "python" in rep["profile"]["stacks"])
        expect("pipeline: evidence-проверки собраны", isinstance(rep["checks"], dict) and rep["checks"])
        expect("pipeline: гейты RunPlan оценены (есть вердикт blocked)",
               "blocked" in rep["gates"] and isinstance(rep["gates"]["evaluated"], list))
        expect("pipeline: intake_completeness закрыт evidence из сигналов (finding живого прогона)",
               "intake_completeness" not in rep["gates"]["unmet"])
        expect("pipeline: workitem привязан к именованной фиче", rep["workitem_id"] == "add-fn")
        expect("pipeline: честный not_yet (commit/PR/живой)", len(rep["not_yet"]) == 3)
        # P0.5: dry-run (commit=False) НИКОГДА не ready_for_pr — нет ревизии для draft PR
        expect("P0.5: commit=False -> ready_for_pr всегда False", rep["ready_for_pr"] is False)

        # v2.59 (finding аудита): commit=True -> изменения на рабочей ветке, evidence на ТОЧНОМ SHA
        _, orig_branch, _ = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
        it_c = iter([
            {"op": "write", "path": "src/mul.py", "content": "def mul(a,b): return a*b\n"},
            {"done": True, "summary": "mul"},
        ])
        rep_c = run_pipeline("добавить mul", sig, root, lambda c: next(it_c),
                             policy=pol, budget={"max_model_calls": 10}, feature="mul-fn", commit=True)
        expect("commit: создан коммит на рабочей ветке (не main)",
               rep_c["commit"]["sha"] and rep_c["commit"]["branch"] == "ai-ops/mul-fn")
        expect("commit: evidence собран на ТОЧНОМ зафиксированном SHA",
               rep_c["commit"]["evidence_on_exact_sha"] is True
               and rep_c["commit"]["evidence_revision"] == rep_c["commit"]["sha"])
        expect("commit: main не тронут (работа на ветке ai-ops/*)",
               _git(root, "rev-parse", "--abbrev-ref", "HEAD")[1] == "ai-ops/mul-fn")
        # P0.5: полный SHA (40 hex), не short; дерево чистое до/после проверок
        expect("P0.5: commit SHA полный (40 hex)",
               isinstance(rep_c["commit"]["sha"], str) and len(rep_c["commit"]["sha"]) == 40)
        expect("P0.5: дерево чистое до проверок (все правки в коммите)",
               rep_c["commit"]["tree_clean_before_checks"] is True)
        expect("P0.5: commit=True + чисто + SHA совпал -> ready_for_pr True",
               rep_c["ready_for_pr"] is True)
        # v2.121 (P1.2 п.4): recheck-after-diff присутствует в отчёте; для QUICK одобрений нет -> ok
        expect("v2.121: approval_recheck в отчёте, для QUICK пусто -> ok",
               isinstance(rep_c.get("approval_recheck"), dict) and rep_c["approval_recheck"]["ok"] is True)
        # helper: изменённые коммитом файлы извлекаются
        _chg = _committed_changed_files(root, rep_c["commit"]["sha"])
        expect("v2.121: _committed_changed_files -> src/mul.py в диффе коммита", "src/mul.py" in _chg)
        # интеграция: одобрение со scope, НЕ покрывающим изменённый путь -> recheck uncovered
        import approvals as _appr_t
        _appr_t.write_record(root, "mul-fn", "secrets", "u@x", "config/other.py", "ротация",
                             created_at="2026-07-05T00:00:00Z", binds_to="P",
                             expires_at="2027-01-01T00:00:00Z", risk="secret", source="user")
        _rc_bad = _appr_t.recheck_after_diff(root, "mul-fn", _chg, signals={"secret_boundary": True},
                                             now="2026-07-05T00:00:00Z", plan_hash="P")
        expect("v2.121: scope одобрения не покрывает изменённый путь -> uncovered",
               _rc_bad["ok"] is False and _rc_bad["uncovered"][0]["domain"] == "secrets")
        expect("умное ослабление: нет тестов -> освобождено + громкий tests_warn (allow_missing_tests)",
               "tests_passed" in rep_c["exemptions"] and rep_c["tests_warn"])
        expect("умное ослабление: implementation_verification не заблокирован из-за отсутствия тулчейна",
               "implementation_verification" not in rep_c["gates"]["unmet"])
        _git(root, "checkout", "-q", orig_branch)   # вернуться на исходную ветку

        # require_tests: allow_missing_tests=False -> отсутствие тестов БЛОКИРУЕТ (эскалация политикой)
        it_rt = iter([{"op":"write","path":"src/q.py","content":"x=1\n"}, {"done": True}])
        rep_rt = run_pipeline("нужны тесты", sig, root, lambda c: next(it_rt), policy=pol,
                              budget={"max_model_calls":5}, feature="need-tests", allow_missing_tests=False)
        expect("require_tests: отсутствие тестов блокирует implementation_verification",
               "implementation_verification" in rep_rt["gates"]["unmet"])
        _git(root, "checkout", "-q", orig_branch)

        # v2.62: isolate=True -> весь прогон в отдельном worktree, основное дерево не тронуто
        it_iso = iter([{"op":"write","path":"src/iso.py","content":"y=2\n"}, {"done": True}])
        rep_iso = run_pipeline("в изоляции", sig, root, lambda c: next(it_iso),
                               budget={"max_model_calls":5}, feature="iso-fn",
                               commit=True, isolate=True, install_deps=False)  # offline: не ставим deps
        wt_rel = rep_iso["isolation"]["worktree"]
        expect("isolate: прогон в отдельном worktree (.ai/worktrees/iso-fn)",
               wt_rel == ".ai/worktrees/iso-fn" and (root / wt_rel / "src" / "iso.py").exists())
        expect("isolate: основное дерево НЕ тронуто (нет src/iso.py в корне)",
               not (root / "src" / "iso.py").exists())
        expect("isolate: коммит на ветке ai-ops/iso-fn, evidence на точном SHA",
               rep_iso["commit"]["branch"] == "ai-ops/iso-fn"
               and rep_iso["commit"]["evidence_on_exact_sha"] is True)
        _git(root, "checkout", "-q", orig_branch)

        # P0.3 (аудит v2.79): повторный прогон того же feature с НЕсохранённым коммитом
        # прошлого прогона -> БЕЗ discard останавливается ошибкой (не теряем работу)
        it_iso2 = iter([{"op": "write", "path": "src/iso.py", "content": "y=3\n"}, {"done": True}])
        rep_iso_guard = run_pipeline("в изоляции повторно", sig, root, lambda c: next(it_iso2),
                                     budget={"max_model_calls": 5}, feature="iso-fn",
                                     commit=True, isolate=True, install_deps=False)
        expect("P0.3: повторный прогон без discard -> honest error (работа не потеряна)",
               rep_iso_guard.get("status") == "error" and "discard" in (rep_iso_guard.get("error") or ""))
        _git(root, "checkout", "-q", orig_branch)

        # P0.3: с discard_previous=True повторный прогон перезаписывает и стартует чисто
        it_iso3 = iter([{"op": "write", "path": "src/iso.py", "content": "y=4\n"}, {"done": True}])
        rep_iso3 = run_pipeline("в изоляции c discard", sig, root, lambda c: next(it_iso3),
                                budget={"max_model_calls": 5}, feature="iso-fn",
                                commit=True, isolate=True, install_deps=False, discard_previous=True)
        expect("P0.3: discard=True -> свежий worktree, чистый старт",
               rep_iso3.get("status") != "error"
               and rep_iso3["isolation"]["worktree"] == ".ai/worktrees/iso-fn"
               and rep_iso3["commit"]["evidence_on_exact_sha"] is True)
        _git(root, "checkout", "-q", orig_branch)

        # v2.93 (finding аудита): целостность коммита — хелперы состояния файлов
        with tempfile.TemporaryDirectory() as td2:
            r2 = Path(td2)
            subprocess.run(["git", "-C", td2, "init", "-q"])
            subprocess.run(["git", "-C", td2, "config", "user.email", "t@t"])
            subprocess.run(["git", "-C", td2, "config", "user.name", "t"])
            (r2 / "a.py").write_text("x=1\n", encoding="utf-8")
            subprocess.run(["git", "-C", td2, "add", "-A"]); subprocess.run(["git", "-C", td2, "commit", "-q", "-m", "i"])
            expect("v2.93 _has_changes: чистое дерево -> нет правок", _has_changes(r2) is False)
            # правка через «shell» (прямое изменение файла, не через write-op) -> детектится
            (r2 / "a.py").write_text("x=2\n", encoding="utf-8")
            expect("v2.93 _has_changes: правка tracked-файла (как из shell) детектится", _has_changes(r2) is True)
            _git(r2, "checkout", "--", ".")
            # снимок untracked ДО подготовки; пользовательский untracked существует заранее
            (r2 / "user_note.txt").write_text("mine\n", encoding="utf-8")
            before = _untracked(r2)
            expect("v2.93 _untracked: видит пользовательский untracked", "user_note.txt" in before)
            # подготовка создаёт НОВЫЙ untracked (эмуляция package-lock.json от npm install)
            (r2 / "package-lock.json").write_text("{}\n", encoding="utf-8")
            delta = _untracked(r2) - before
            expect("v2.93 snapshot-delta: новый untracked подготовки в delta", delta == {"package-lock.json"})
            expect("v2.93 snapshot-delta: пользовательский untracked НЕ в delta (не удалим)",
                   "user_note.txt" not in delta)

        # v2.93 интеграция: правка ТОЛЬКО через shell (0 write-op) всё равно коммитится (не теряем работу)
        it_sh = iter([
            {"op": "shell", "command": "python3 -c \"open('shelledit.py','w').write('s=1\\n')\""},
            {"done": True, "summary": "через shell"},
        ])
        pol_sh = tool_broker.Policy(level="execution", write_scope=["src/"])
        rep_sh = run_pipeline("правка через shell", sig, root, lambda c: next(it_sh),
                              policy=pol_sh, budget={"max_model_calls": 5}, feature="shell-fn",
                              commit=True, isolate=True, install_deps=False)
        expect("v2.93: правка через shell (applied_writes=0) всё равно даёт коммит",
               rep_sh["loop"]["applied_writes"] == 0 and bool(rep_sh["commit"]["sha"]))
        _git(root, "checkout", "-q", orig_branch)

        # v2.108 Operational Context: context_prelude РЕАЛЬНО доходит до модели (в base_context петли).
        seen_ctx = {}
        def _capturing(c):
            seen_ctx.setdefault("first", c)
            return {"done": True}
        run_pipeline("проверка prelude", sig, root, _capturing, policy=pol,
                     budget={"max_model_calls": 3}, feature="prelude-fn", isolate=True,
                     install_deps=False, context_prelude="MARKER_CONTEXT_PAYLOAD_XYZ")
        expect("v2.108: context_prelude попал в prompt модели (base_context петли)",
               "MARKER_CONTEXT_PAYLOAD_XYZ" in (seen_ctx.get("first") or ""))
        _git(root, "checkout", "-q", orig_branch)

        # v2.109 Real Resume: первый прогон коммитит работу; resume ПРОДОЛЖАЕТ поверх неё
        # (ветка/коммит НЕ удаляются, worktree переиспользуется, resume_context доходит до модели).
        it_r1 = iter([{"op": "write", "path": "src/first.py", "content": "a=1\n"},
                      {"done": True, "summary": "фаза 1"}])
        rep_r1 = run_pipeline("resume фаза 1", sig, root, lambda c: next(it_r1),
                              budget={"max_model_calls": 5}, feature="resume-fn",
                              commit=True, isolate=True, install_deps=False)
        sha1 = (rep_r1.get("commit") or {}).get("sha")
        expect("v2.109 resume: фаза 1 закоммичена на ветке", bool(sha1))
        _git(root, "checkout", "-q", orig_branch)

        seen_r = {}
        it_r2 = iter([{"op": "write", "path": "src/second.py", "content": "b=2\n"},
                      {"done": True, "summary": "фаза 2"}])
        def _resume_prop(c):
            seen_r.setdefault("ctx", c)
            return next(it_r2)
        rep_r2 = run_pipeline("resume фаза 2", sig, root, _resume_prop,
                              budget={"max_model_calls": 5}, feature="resume-fn",
                              commit=True, isolate=True, install_deps=False,
                              resume=True, resume_context="MARKER_RESUME_STATE_ABC")
        rinfo = rep_r2.get("resume") or {}
        expect("v2.109 resume: НЕ ошибка про несохранённые коммиты (продолжаем, а не падаем)",
               rep_r2.get("status") != "error")
        expect("v2.109 resume: resumed=True + ветка переиспользована (работа не потеряна)",
               rinfo.get("resumed") is True and rinfo.get("reused_branch") is True)
        expect("v2.109 resume: resume_context РЕАЛЬНО в prompt модели",
               "MARKER_RESUME_STATE_ABC" in (seen_r.get("ctx") or ""))
        wt_r = root / ".ai" / "worktrees" / "resume-fn"
        expect("v2.109 resume: работа фазы 1 сохранена в worktree (продолжили поверх, не с нуля)",
               (wt_r / "src" / "first.py").exists() and (wt_r / "src" / "second.py").exists())
        _git(root, "checkout", "-q", orig_branch)

        # v2.109 resume: нечего продолжать (нет ветки) -> честный fresh, resumed=False + причина
        it_r3 = iter([{"op": "write", "path": "src/n.py", "content": "n=1\n"}, {"done": True}])
        rep_r3 = run_pipeline("resume без прошлого", sig, root, lambda c: next(it_r3),
                              budget={"max_model_calls": 5}, feature="resume-none",
                              commit=True, isolate=True, install_deps=False, resume=True)
        rinfo3 = rep_r3.get("resume") or {}
        expect("v2.109 resume: нет прошлого прогона -> честный fresh (resumed=False + причина)",
               rinfo3.get("resumed") is False and bool(rinfo3.get("reason"))
               and rep_r3.get("status") != "error")
        _git(root, "checkout", "-q", orig_branch)

        # v2.110 Real Spec-First: неполный spec.yaml для WorkItem -> «не пускает в implementation»
        import spec_levels as _sl_t
        sig_sf = {"task_type": "QUICK", "size": "small", "risk": "low", "affected_areas": ["core"]}
        _sl_t.create_spec(root, "spec-fn", sig_sf)   # все разделы missing (неполон)
        it_sf = iter([{"op": "write", "path": "src/sf.py", "content": "s=1\n"}, {"done": True}])
        rep_sf = run_pipeline("spec-first блок", sig_sf, root, lambda c: next(it_sf),
                              budget={"max_model_calls": 5}, feature="spec-fn",
                              commit=True, isolate=True, install_deps=False, baseline_diff=True)
        expect("v2.110 spec-first: неполный spec.yaml -> ready_for_pr=False + incomplete_sections",
               rep_sf.get("ready_for_pr") is False
               and rep_sf["spec_first"]["ok"] is False and rep_sf["spec_first"]["incomplete_sections"])
        _git(root, "checkout", "-q", orig_branch)

        # заполнить spec.yaml -> spec-first больше НЕ блокирует (проверяем именно спек-гейт)
        import yaml as _yaml_t
        _sp = root / "features" / "spec-fn2" / "spec.yaml"
        _sp.parent.mkdir(parents=True, exist_ok=True)
        _full_secs = {s: {"status": "complete", "content": "x"} for s in _sl_t.required_sections(0)}
        _sp.write_text(_yaml_t.safe_dump({"schema_version": 1, "kind": "spec", "workitem_id": "spec-fn2",
                                          "level": 0, "sections": _full_secs}), encoding="utf-8")
        it_sf2 = iter([{"op": "write", "path": "src/sf2.py", "content": "s=2\n"}, {"done": True}])
        rep_sf2 = run_pipeline("spec-first полон", sig_sf, root, lambda c: next(it_sf2),
                               budget={"max_model_calls": 5}, feature="spec-fn2",
                               commit=True, isolate=True, install_deps=False, baseline_diff=True)
        expect("v2.110 spec-first: полный spec.yaml -> спек-гейт не блокирует (ok=True)",
               rep_sf2["spec_first"]["ok"] is True and not rep_sf2["spec_first"]["incomplete_sections"])
        _git(root, "checkout", "-q", orig_branch)

        # v2.118 (finding живого прогона): провал install при ПРОШЕДШИХ проверках не блокирует ready
        expect("v2.118 env: проверки прошли (test=pass) -> окружение квалифицировано, install-провал не в счёт",
               _env_unqualified({"test": {"status": "pass"}, "build": {"status": "not_run"}}) is False)
        expect("v2.118 env: exit 127 в упавшей проверке -> окружение НЕ квалифицировано (блок сохраняется)",
               _env_unqualified({"test": {"status": "fail", "runs": [{"ok": False, "exit_code": 127}]}}) is True)
        expect("v2.118 env: 'No module named' в выводе -> окружение НЕ квалифицировано",
               _env_unqualified({"test": {"status": "fail",
                                          "runs": [{"ok": False, "exit_code": 1,
                                                    "output_tail": "ModuleNotFoundError: No module named 'foo'"}]}}) is True)
        expect("v2.118 env: честный fail проверки (exit 1, код сломан) -> НЕ считается env-провалом",
               _env_unqualified({"test": {"status": "fail",
                                          "runs": [{"ok": False, "exit_code": 1,
                                                    "output_tail": "AssertionError: 2 != 3"}]}}) is False)
        # v2.121 (P1.4): install-провал НЕ прощается без доказательства — нет запущенных проверок ->
        # окружение НЕ доказано (раньше _env_unqualified возвращал False при пустых checks — дыра)
        expect("v2.121 env: проверок не запускалось -> окружение НЕ доказано (proven_ok=False)",
               _env_proven_ok({}) is False and _env_proven_ok({"build": {"status": "not_run"},
                                                               "test": {"status": "not_run"}}) is False)
        expect("v2.121 env: хотя бы одна pass -> доказано; только env-симптомы -> НЕ доказано",
               _env_proven_ok({"test": {"status": "pass"}}) is True
               and _env_proven_ok({"test": {"status": "fail", "runs": [{"ok": False, "exit_code": 127}]}}) is False)

        # v2.119 (finding живого прогона): тул-кэши (untracked) не делают дерево «грязным после проверок»
        with tempfile.TemporaryDirectory() as tdc:
            rc = Path(tdc)
            _git(rc, "init", "-q"); _git(rc, "config", "user.email", "t@t"); _git(rc, "config", "user.name", "t")
            (rc / "m.py").write_text("x=1\n", encoding="utf-8")
            _git(rc, "add", "-A"); _git(rc, "commit", "-q", "-m", "i")
            expect("v2.119 tree: чистое дерево -> clean", _tree_clean_after_checks(rc) is True)
            # pytest/npm кэши как untracked -> терпимо (после проверок)
            (rc / "__pycache__").mkdir(); (rc / "__pycache__" / "m.cpython-311.pyc").write_text("x", encoding="utf-8")
            (rc / ".pytest_cache").mkdir(); (rc / ".pytest_cache" / "v").write_text("x", encoding="utf-8")
            (rc / "node_modules").mkdir(); (rc / "node_modules" / "pkg.js").write_text("x", encoding="utf-8")
            expect("v2.119 tree: только тул-кэши (untracked) -> дерево считается чистым (не блок)",
                   _tree_clean_after_checks(rc) is True and _tree_clean(rc) is False)
            # НЕ-кэш untracked файл -> грязь (не прячем реальные артефакты)
            (rc / "leftover.txt").write_text("real", encoding="utf-8")
            expect("v2.119 tree: НЕ-кэш untracked (leftover.txt) -> дерево грязное (честно)",
                   _tree_clean_after_checks(rc) is False)
            (rc / "leftover.txt").unlink()
            # модификация TRACKED файла проверками -> грязь (evidence-целостность сохранена)
            (rc / "m.py").write_text("x=2\n", encoding="utf-8")
            expect("v2.119 tree: правка TRACKED файла проверками -> дерево грязное (P0.5 сохранён)",
                   _tree_clean_after_checks(rc) is False)

        # v2.95: security-скан ловит секрет в изменениях -> гейт security блокирует с деталями
        # (ENGINEERING-план содержит security). Не ложный green: секрет -> security в unmet.
        sig_eng = {"task_type": "ENGINEERING", "size": "small", "risk": "medium", "affected_areas": ["core"]}
        _aws_fx = "AKIA" + "IOSFODNN7EXAMPLE"   # v3.0.4: собрано в рантайме (без статического секрет-литерала)
        it_sec = iter([{"op": "write", "path": "src/leak.py",
                        "content": f'API_KEY = "{_aws_fx}"\n'}, {"done": True}])
        rep_sec = run_pipeline("добавить конфиг", sig_eng, root, lambda c: next(it_sec),
                               policy=pol, budget={"max_model_calls": 5}, feature="sec-fn",
                               commit=True, isolate=True, install_deps=False)
        expect("v2.101: security-pack поймал секрет (домен secrets в blocking)",
               rep_sec.get("security_scan") and "secrets" in rep_sec["security_scan"]["blocking"])
        expect("v2.101: секрет -> security блокирует (в unmet, не ложный green)",
               "security" in rep_sec["gates"]["unmet"])
        _git(root, "checkout", "-q", orig_branch)

        # v3.0.13 (блок C, тест-гэп): _security_scan_error FAIL-CLOSED. Если security pack БРОСАЕТ
        # (git-сбой/инфра), security-гейт обязан стать fail (не тихо пропасть -> ложный green). Прежде
        # эта ветка не имела ассерта. Монкипатчим run_pack на raiser.
        import security_pack as _sp_mod
        _orig_rp = _sp_mod.run_pack
        _sp_mod.run_pack = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("scan boom (git недоступен)"))
        try:
            it_se = iter([{"op": "write", "path": "src/se.py", "content": "s=1\n"}, {"done": True}])
            rep_se = run_pipeline("скан падает", sig_eng, root, lambda c: next(it_se),
                                  policy=pol, budget={"max_model_calls": 5}, feature="scanerr-fn",
                                  commit=True, isolate=True, install_deps=False)
        finally:
            _sp_mod.run_pack = _orig_rp
        expect("v3.0.13 тест-гэп: security scan бросил -> security=fail (fail-closed, не ложный green)",
               "security" in rep_se["gates"]["unmet"] and not rep_se["ready_for_pr"])
        _git(root, "checkout", "-q", orig_branch)

        # v2.125 (finding живого прогона): новая зависимость в QUICK-задаче — security pack ЗАПУСКАЕТСЯ
        # (не только когда security в плане workflow) и security ФОРСИРУЕТСЯ в оценку гейтов -> блокирует
        # без ApprovalRecord даже в QUICK (раньше новая зависимость в QUICK проскакивала).
        sig_q = {"task_type": "QUICK", "size": "small", "risk": "low", "affected_areas": ["core"]}
        pol_dep = tool_broker.Policy(level="execution", block_push=True)   # без write_scope: requirements.txt в корне
        it_dep = iter([{"op": "write", "path": "requirements.txt", "content": "flask\n"}, {"done": True}])
        rep_dep = run_pipeline("добавить зависимость", sig_q, root, lambda c: next(it_dep),
                               policy=pol_dep, budget={"max_model_calls": 5}, feature="dep-fn",
                               commit=True, isolate=True, install_deps=False)
        expect("v2.125: QUICK + новая зависимость -> security pack запущен (домен dependencies)",
               rep_dep.get("security_scan") and "dependencies" in (rep_dep["security_scan"].get("needs_review") or []))
        expect("v2.125: security ФОРСИРОВАН в оценку и блокирует без ApprovalRecord даже в QUICK",
               "security" in rep_dep["gates"]["evaluated"] and "security" in rep_dep["gates"]["unmet"]
               and rep_dep["ready_for_pr"] is False)
        _git(root, "checkout", "-q", orig_branch)

        # v2.106 #1: независимый security-reviewer закрывает needs_review домены -> security НЕ в unmet.
        # Чистая (без секретов) ENGINEERING-правка + --review + mock-ревьюер pass.
        it_secrev = iter([{"op": "write", "path": "src/clean.py", "content": "def f():\n    return 1\n"},
                          {"done": True}])
        sec_reviewer = lambda c: {"kind": "reviewer-result", "status": "pass",
                                  "summary": "injection-surface чист"}  # noqa: E731
        rep_secrev = run_pipeline("чистая правка", sig_eng, root, lambda c: next(it_secrev),
                                  policy=pol, budget={"max_model_calls": 8}, feature="secrev-fn",
                                  commit=True, isolate=True, install_deps=False,
                                  review=True, reviewer_proposer=sec_reviewer)
        expect("v2.106 #1: security-reviewer pass -> security закрыт (не в unmet)",
               "security" not in rep_secrev["gates"]["unmet"])
        _git(root, "checkout", "-q", orig_branch)

        # v3.7.1 STRICT JUDGE (trust alignment): needs_review-домен rate_limiting (только security_reviewer)
        # через сигнал api_change. A/B: qualified-судья закрывает vs НЕТ судьи -> pending_human.
        sig_api = dict(sig_eng, api_change=True)
        it_q = iter([{"op": "write", "path": "src/rl_a.py", "content": "def a():\n    return 1\n"}, {"done": True}])
        rep_q = run_pipeline("api rate strict-on", sig_api, root, lambda c: next(it_q),
                             policy=pol, budget={"max_model_calls": 8}, feature="rl-q-fn",
                             commit=True, isolate=True, install_deps=False,
                             review=True, reviewer_proposer=sec_reviewer, strict_judge_qualified=True)
        _sec_a = next((g for g in rep_q["gates"].get("gate_results", []) if g.get("gate") == "security"), {})

        def _has(g, sub):
            return any(sub in b for b in (g.get("blockers") or []))
        # strict=True (qualified судья) -> reviewer-ветка: #5 pending_human-guard НЕ берётся
        expect("v3.7.3 A: qualified судья НЕ берёт #5-guard (идёт через security-reviewer)",
               not _has(_sec_a, "нет QUALIFIED security-судьи"))
        _git(root, "checkout", "-q", orig_branch)
        it_strict = iter([{"op": "write", "path": "src/rl_b.py", "content": "def b():\n    return 1\n"}, {"done": True}])
        rep_strict = run_pipeline("api rate strict-off", sig_api, root, lambda c: next(it_strict),
                                  policy=pol, budget={"max_model_calls": 8}, feature="rl-b-fn",
                                  commit=True, isolate=True, install_deps=False,
                                  review=True, reviewer_proposer=sec_reviewer, strict_judge_qualified=False)
        _sec_b = next((g for g in rep_strict["gates"].get("gate_results", []) if g.get("gate") == "security"), {})
        # strict=False (нет qualified security-судьи), нет ApprovalRecord -> security pending_human
        expect("v3.7.3 B: нет qualified судьи + нет ApprovalRecord -> security fail (pending_human)",
               "security" in rep_strict["gates"]["unmet"] and _sec_b.get("status") == "fail"
               and _has(_sec_b, "нет QUALIFIED security-судьи"))
        expect("v3.7.3 C: #5-guard даёт человеку путь закрыть (блокер называет ApprovalRecord)",
               _has(_sec_b, "ApprovalRecord"))

        # v3.8.4 RE-EVALUATE-ONLY: green-except-security ветка (QUICK+api_change, без spec-гейтов) ->
        # человек добавил ApprovalRecord -> переоценить гейты БЕЗ переавторинга (loop stopped=reevaluate-only,
        # план/SHA не меняются -> plan-bound approval валиден) -> #5-блок security снят человеком. Закрывает
        # gap: resume --execute переписывал код и инвалидировал plan-bound approvals.
        import security_pack as _sp_re, approvals as _appr_re
        sig_q = {"task_type": "QUICK", "size": "small", "risk": "low", "affected_areas": ["api"], "api_change": True}
        it_q1 = iter([{"op": "write", "path": "rq.py", "content": "def rq():\n    return 1\n"}, {"done": True}])
        run_pipeline("quick api sec", sig_q, root, lambda c: next(it_q1), policy=pol,
                     budget={"max_model_calls": 8}, feature="reeval-fn", commit=True, isolate=True,
                     install_deps=False, review=True, reviewer_proposer=sec_reviewer, strict_judge_qualified=False)
        _nrq = _sp_re.run_pack(str(root / ".ai" / "worktrees" / "reeval-fn"), base=None,
                               signals=sig_q).get("needs_review") or ["rate_limiting"]
        for _d in _nrq:
            _appr_re.write_record(root, "reeval-fn", approval=_d, approved_by="human@owner",
                                  scope=f"security {_d}", reason="человек одобрил (reeval тест)",
                                  created_at="2026-07-29", binds_to="reeval-fn-plan", expires_at="2026-12-31",
                                  risk="medium", source="human")
        rep_re = run_pipeline("quick api sec", sig_q, root, lambda c: {"done": True}, policy=pol,
                              budget={"max_model_calls": 8}, feature="reeval-fn", commit=True, isolate=True,
                              install_deps=False, review=True, reviewer_proposer=sec_reviewer,
                              strict_judge_qualified=False, reevaluate_only=True)
        _sec_re = next((g for g in rep_re["gates"].get("gate_results", []) if g.get("gate") == "security"), {})
        expect("v3.8.4 re-evaluate-only: путь БЕЗ переавторинга (loop stopped=reevaluate-only)",
               (rep_re.get("loop") or {}).get("stopped") == "reevaluate-only")
        expect("v3.8.4 re-evaluate-only: человеко-одобрение сняло #5-блок security (approval закрыл)",
               not any("нет QUALIFIED security-судьи" in b for b in (_sec_re.get("blockers") or [])))
        _git(root, "checkout", "-q", orig_branch)

        # v2.106 #1 (fail-closed): secret_boundary требует человека даже при pass ревьюера
        it_sb = iter([{"op": "write", "path": "src/sb.py", "content": "def g():\n    return 2\n"}, {"done": True}])
        rep_sb = run_pipeline("граница секретов", dict(sig_eng, secret_boundary=True), root,
                              lambda c: next(it_sb), policy=pol, budget={"max_model_calls": 8},
                              feature="sb-fn", commit=True, isolate=True, install_deps=False,
                              review=True, reviewer_proposer=sec_reviewer)
        expect("v2.106 #1: secret_boundary без human_approved -> security остаётся заблокирован",
               "security" in rep_sb["gates"]["unmet"])
        _git(root, "checkout", "-q", orig_branch)

        # v2.106 #2: spec-depth — ENGINEERING без --author -> requirements/plan незакрыты -> в spec_depth.missing
        it_sd = iter([{"op": "write", "path": "src/sd.py", "content": "x=1\n"}, {"done": True}])
        rep_sd = run_pipeline("eng без артефактов", sig_eng, root, lambda c: next(it_sd),
                              policy=pol, budget={"max_model_calls": 5}, feature="sd-fn",
                              commit=True, isolate=True, install_deps=False)
        expect("v2.106 #2: spec-depth блокирует (незакрытые разделы уровня) + в отчёте",
               rep_sd["spec_depth"]["ok"] is False and rep_sd["spec_depth"]["missing"]
               and rep_sd["ready_for_pr"] is False)
        _git(root, "checkout", "-q", orig_branch)

        # v2.106 #3: context budget overflow -> ready False + причина декомпозиции
        it_ov = iter([{"op": "write", "path": "src/ov.py", "content": "y=2\n"}, {"done": True}])
        rep_ov = run_pipeline("overflow", dict(sig, context_budget=1), root, lambda c: next(it_ov),
                              policy=pol, budget={"max_model_calls": 5}, feature="ov-fn",
                              commit=True, isolate=True, install_deps=False)
        expect("v2.106 #3: context overflow -> ready_for_pr False + причина декомпозиции",
               rep_ov["context_overflow"] is True and rep_ov["ready_for_pr"] is False
               and any("декомпоз" in n for n in rep_ov["not_yet"]))
        _git(root, "checkout", "-q", orig_branch)

        # v2.62: open_pr=True вызывает механизм draft PR; без токена -> honest unavailable
        # (токены снимаем, т.к. CI может выставлять GITHUB_TOKEN — иначе тест дёрнет сеть)
        saved = {k: os.environ.pop(k, None) for k in ("GITHUB_TOKEN", "GH_TOKEN")}
        try:
            it_pr = iter([{"op": "write", "path": "src/pr.py", "content": "z=3\n"}, {"done": True}])
            rep_pr = run_pipeline("с PR", sig, root, lambda c: next(it_pr),
                                  budget={"max_model_calls": 5}, feature="pr-fn",
                                  commit=True, isolate=True, open_pr=True, install_deps=False)
            # v3.0.16 Phase A (finding аудита #1): run_pipeline НЕ доставляет — только планирует. open_pr +
            # ready -> delivery.status='planned', delivery_plan заполнен, overall='ready-undelivered' (PR НЕ
            # открыт из pipeline). Фактическую доставку (и unavailable/delivered) выполняет контроллер.
            expect("v3.0.16 #1: run_pipeline(open_pr) НЕ открывает PR — только delivery_plan + planned",
                   rep_pr["delivery"]["status"] == "planned"
                   and rep_pr.get("draft_pr") is None
                   and isinstance(rep_pr.get("delivery_plan"), dict)
                   and rep_pr["delivery_plan"].get("ready_for_delivery") is True)
            expect("v3.0.16 #1: open_pr+ready в pipeline -> overall=ready-undelivered (доставку финализирует контроллер)",
                   rep_pr["delivery"]["requested"] is True
                   and rep_pr["overall_status"] == "ready-undelivered")
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

        # P0.1 (аудит v2.79): baseline-diff НЕ обходит прочие блокирующие гейты. Сигнал ui_changed
        # добавляет трек VISUAL с блокирующим ux_review (без evidence) -> not ready, хоть регрессий нет.
        sig_ui = dict(sig); sig_ui["ui_changed"] = True
        it_p01 = iter([{"op": "write", "path": "src/p01.py", "content": "p=1\n"}, {"done": True}])
        rep_p01 = run_pipeline("baseline не обходит гейты", sig_ui, root, lambda c: next(it_p01),
                               policy=pol, budget={"max_model_calls": 5}, feature="p01-fn",
                               commit=True, baseline_diff=True)
        expect("P0.1: baseline-diff НЕ обходит прочие блокирующие гейты (ux_review unmet -> not ready)",
               rep_p01["gates"]["other_blocking_unmet"] and rep_p01["ready_for_pr"] is False)
        expect("P0.1: gate_results и tested_revision в отчёте (evidence/аудит)",
               isinstance(rep_p01["gates"]["gate_results"], list)
               and rep_p01["gates"]["tested_revision"] == rep_p01["commit"]["sha"])
        _git(root, "checkout", "-q", orig_branch)

        # v2.71 (finding живого прогона): _install_dependencies ставит зависимости стека перед
        # проверками. Детерминированно проверяем механизм безвредной install-командой (true).
        prof_inst = {"stacks": [{"language": "node", "install_command": "true"},
                                {"language": "python", "install_command": "true"},
                                {"language": "go", "install_command": None}]}
        prep = _install_dependencies(prof_inst, root, pol)
        expect("install: install_command выполнены (dedup, None пропущен)",
               len(prep) == 1 and prep[0]["ok"] is True and prep[0]["command"] == "true")

        # v2.72 (finding живого прогона): baseline-diff отличает регрессии от пред-существующих
        base = {"build": {"status": "pass"}, "test": {"status": "fail"}, "lint": {"status": "pass"}}
        after = {"build": {"status": "fail"}, "test": {"status": "pass"}, "lint": {"status": "pass"}}
        regr, fx = _diff_checks(base, after)
        expect("baseline-diff: build pass->fail = регрессия", regr == ["build"])
        expect("baseline-diff: test fail->pass = починка", fx == ["test"])
        expect("baseline-diff: пред-существующий fail->fail (без ухудшения) не в счёт",
               _diff_checks({"x": {"status": "fail"}}, {"x": {"status": "fail"}}) == ([], []))

        # v2.77 (finding живого прогона): fail->fail, но ХУЖЕ (1 failed -> 8 failed) = регрессия
        base_t = {"test": {"status": "fail", "runs": [{"output_tail": "Tests  1 failed | 531 passed"}]}}
        worse_t = {"test": {"status": "fail", "runs": [{"output_tail": "Tests  8 failed | 524 passed"}]}}
        same_t = {"test": {"status": "fail", "runs": [{"output_tail": "Tests  1 failed | 531 passed"}]}}
        expect("within-check: 1 failed -> 8 failed = регрессия", _diff_checks(base_t, worse_t) == (["test"], []))
        expect("within-check: 1 failed -> 1 failed (без роста) = не регрессия",
               _diff_checks(base_t, same_t) == ([], []))
        expect("failure-signal: считает 'N failed'/'N errors'",
               _failure_signal({"runs": [{"output_tail": "Found 5 errors"}]}) == 5)

        # v2.84: структурные id падений — «починил один тест, сломал другой» (1 failed -> 1 failed,
        # но ДРУГОЙ тест) счётчик пропускал; теперь новый id = регрессия.
        base_id = {"test": {"status": "fail", "runs": [{"output_tail":
                   "FAILED tests/test_a.py::test_one\n1 failed, 10 passed"}]}}
        swap_id = {"test": {"status": "fail", "runs": [{"output_tail":
                   "FAILED tests/test_b.py::test_two\n1 failed, 10 passed"}]}}
        same_id = {"test": {"status": "fail", "runs": [{"output_tail":
                   "FAILED tests/test_a.py::test_one\n1 failed, 10 passed"}]}}
        expect("structured-id: тот же счётчик, но ДРУГОЙ упавший тест = регрессия",
               _diff_checks(base_id, swap_id) == (["test"], []))
        expect("structured-id: тот же упавший тест (тот же id) = не регрессия",
               _diff_checks(base_id, same_id) == ([], []))
        expect("failure-ids: извлекает pytest FAILED node id",
               "tests/test_a.py::test_one" in _failure_ids(base_id["test"]))
        # v2.122 (finding обкатки S10): красная база — профильный узел починен, НЕ связанный
        # пред-существующий остаётся красным. Чек в целом red (fail->fail), но fixed должен быть
        # непуст на уровне node-id, а regressions — пуст (нет новых падений). Раньше fixed=[] держал
        # ложный not-ready под --require-fix на легитимном фиксе.
        s10_base = {"test": {"status": "fail", "runs": [{"output_tail":
                    "FAILED test_task.py::test_target\nFAILED test_legacy.py::test_old\n2 failed"}]}}
        s10_after = {"test": {"status": "fail", "runs": [{"output_tail":
                     "FAILED test_legacy.py::test_old\n1 failed, 1 passed"}]}}
        expect("S10 red-base: профильный узел починен, пред-существующий остался = fixed непуст, regress пуст",
               _diff_checks(s10_base, s10_after) == ([], ["test"]))
        # v3.0.15 (аудит P1, явная таблица): baseline test_a=fail,test_b=fail; after test_a=pass,test_b=fail
        # -> fixed=[test], regressions=[] (симметричный diff по structural failure-ids; красный чек НЕ
        # блокирует легитимный фикс одного узла при оставшемся старом падении). Закрыто ещё в v2.122.
        _rb_base = {"test": {"status": "fail", "runs": [{"output_tail":
                    "FAILED tests/t.py::test_a\nFAILED tests/t.py::test_b\n2 failed"}]}}
        _rb_after = {"test": {"status": "fail", "runs": [{"output_tail":
                     "FAILED tests/t.py::test_b\n1 failed, 1 passed"}]}}
        expect("v3.0.15 require_fix: {a:fail,b:fail}->{a:pass,b:fail} = fixed=[test], regressions=[]",
               _diff_checks(_rb_base, _rb_after) == ([], ["test"]))
        expect("S10 guard: непарсибельный after (build-fail без node-id) НЕ фабрикует fixed",
               _diff_checks(s10_base, {"test": {"status": "fail", "runs": [{"output_tail": "BUILD FAILED"}]}}) == ([], []))
        expect("S10 не ломает swap: починил один — сломал другой = регрессия, НЕ fixed",
               _diff_checks(base_id, swap_id) == (["test"], []))
        # стек-квалификация go: РЕАЛЬНЫЙ вывод `go test`. Раньше id схлопывался в {'FAIL'} и swap
        # (починил TestSub, сломал TestAdd в ОДНОМ пакете) не ловился -> ложный green для go-репо.
        go_sub = {"test": {"status": "fail", "runs": [{"output_tail":
                  "--- FAIL: TestSub (0.00s)\n    calc_test.go:13: Sub(5,2) = 3; want 999\nFAIL\nFAIL\tcalc\t0.002s\nFAIL"}]}}
        go_add = {"test": {"status": "fail", "runs": [{"output_tail":
                  "--- FAIL: TestAdd (0.00s)\n    calc_test.go:6: Add(2,3) = 6; want 5\nFAIL\nFAIL\tcalc\t0.003s\nFAIL"}]}}
        expect("go: извлекает имя упавшего теста (--- FAIL: TestSub)",
               "TestSub" in _failure_ids(go_sub["test"]))
        expect("go structured-id: починил TestSub, сломал TestAdd (тот же пакет) = регрессия",
               _diff_checks(go_sub, go_add) == (["test"], []))
        expect("go: тот же упавший тест, другое ВРЕМЯ прогона = НЕ регрессия",
               _diff_checks(go_sub, {"test": {"status": "fail", "runs": [{"output_tail":
                   "--- FAIL: TestSub (0.01s)\n    calc_test.go:13: Sub(5,2) = 3; want 999\nFAIL\nFAIL\tcalc\t0.009s\nFAIL"}]}}) == ([], []))
        # стек-квалификация rust: РЕАЛЬНЫЙ вывод `cargo test`. Раньше id был константой из строки
        # "error: test failed" -> swap (починил test_sub, сломал test_add) не ловился -> ложный green.
        rs_sub = {"test": {"status": "fail", "runs": [{"output_tail":
                  "thread 'tests::test_sub' (13663) panicked at src/lib.rs:10:21:\nassertion `left == right` failed\n"
                  "failures:\n    tests::test_sub\ntest result: FAILED. 1 passed; 1 failed; finished in 0.28s\n"
                  "error: test failed, to rerun pass `--lib`"}]}}
        rs_add = {"test": {"status": "fail", "runs": [{"output_tail":
                  "thread 'tests::test_add' (13999) panicked at src/lib.rs:8:21:\nassertion `left == right` failed\n"
                  "failures:\n    tests::test_add\ntest result: FAILED. 1 passed; 1 failed; finished in 0.19s\n"
                  "error: test failed, to rerun pass `--lib`"}]}}
        expect("rust: извлекает имя упавшего теста (thread 'tests::test_sub' panicked)",
               any("tests::test_sub" in i for i in _failure_ids(rs_sub["test"])))
        expect("rust structured-id: починил test_sub, сломал test_add = регрессия",
               _diff_checks(rs_sub, rs_add) == (["test"], []))
        expect("rust: тот же упавший тест (другой pid) = НЕ регрессия",
               _diff_checks(rs_sub, {"test": {"status": "fail", "runs": [{"output_tail":
                   "thread 'tests::test_sub' (55555) panicked at src/lib.rs:10:21:\nassertion `left == right` failed\n"
                   "failures:\n    tests::test_sub\ntest result: FAILED. 1 passed; 1 failed; finished in 0.30s\n"
                   "error: test failed, to rerun pass `--lib`"}]}}) == ([], []))
        # стек-квалификация java: РЕАЛЬНЫЙ вывод maven-surefire. Раньше НИ один паттерн не ловил
        # java-падение (id пустой), maven печатает "Failures: 1" (слово перед числом -> счётчик 0)
        # -> swap не ловился = ложный green. Теперь берём Class.method упавшего теста.
        jv_sub = {"test": {"status": "fail", "runs": [{"output_tail":
                  "[ERROR] CalcTest.testSub -- Time elapsed: 0.007 s <<< FAILURE!\n"
                  "org.opentest4j.AssertionFailedError: expected: <999> but was: <3>\n"
                  "[ERROR]   CalcTest.testSub:5 expected: <999> but was: <3>\n"
                  "[ERROR] Tests run: 2, Failures: 1, Errors: 0, Skipped: 0"}]}}
        jv_add = {"test": {"status": "fail", "runs": [{"output_tail":
                  "[ERROR] CalcTest.testAdd -- Time elapsed: 0.008 s <<< FAILURE!\n"
                  "org.opentest4j.AssertionFailedError: expected: <999> but was: <5>\n"
                  "[ERROR]   CalcTest.testAdd:4 expected: <999> but was: <5>\n"
                  "[ERROR] Tests run: 2, Failures: 1, Errors: 0, Skipped: 0"}]}}
        expect("java: извлекает Class.method упавшего теста (CalcTest.testSub)",
               any("CalcTest.testSub" in i for i in _failure_ids(jv_sub["test"])))
        expect("java structured-id: починил testSub, сломал testAdd = регрессия",
               _diff_checks(jv_sub, jv_add) == (["test"], []))
        # tsc: новый код ошибки в новом месте = регрессия
        base_ts = {"typecheck": {"status": "fail", "runs": [{"output_tail":
                   "src/a.ts(3,5): error TS2322: Type error"}]}}
        new_ts = {"typecheck": {"status": "fail", "runs": [{"output_tail":
                  "src/a.ts(3,5): error TS2322: Type error\nsrc/b.ts(9,1): error TS2531: Object is possibly null"}]}}
        expect("structured-id: новая tsc-ошибка в новом файле = регрессия",
               _diff_checks(base_ts, new_ts) == (["typecheck"], []))

        # v2.88 (finding живого прогона ii-sreda): vite печатает "Build failed in 1.41s" — ВРЕМЯ
        # волатильно. Раньше id падения включал время -> новый id каждый прогон -> ЛОЖНАЯ регрессия
        # на неизменной красной сборке. Теперь время нормализуется, а реальная строка ошибки — id.
        vite_err = ('src/shared/ui/index.tsx (19:9): "Markdown" is not exported by '
                    '"src/shared/ui/markdown.ts", imported by "src/shared/ui/index.tsx".')
        base_vite = {"build": {"status": "fail", "runs": [{"output_tail": "✗ Build failed in 1.38s\nerror during build:\n" + vite_err}]}}
        after_vite = {"build": {"status": "fail", "runs": [{"output_tail": "✗ Build failed in 1.41s\nerror during build:\n" + vite_err}]}}
        expect("vite: та же ошибка сборки, другое ВРЕМЯ (1.38s->1.41s) = НЕ регрессия (ложный триггер устранён)",
               _diff_checks(base_vite, after_vite) == ([], []))
        new_vite = {"build": {"status": "fail", "runs": [{"output_tail": "✗ Build failed in 1.55s\nerror during build:\nsrc/shared/lib/formatPrice.ts (2:9): \"x\" is not defined"}]}}
        expect("vite: НОВАЯ ошибка сборки в другом файле = регрессия (реальную поломку различаем)",
               _diff_checks(base_vite, new_vite) == (["build"], []))
        expect("failure-ids: время нормализовано (id стабилен между прогонами)",
               _failure_ids(base_vite["build"]) == _failure_ids(after_vite["build"]))

        # v2.85 (finding аудита): потеря покрытия — самый острый ложный green. Модель «чинит»
        # красный тест, УДАЛЯЯ его -> tests_absent -> status warn. Раньше fail->warn/pass->warn не
        # считались регрессией -> ready_for_pr=true на удалённых тестах. Теперь = регрессия.
        expect("coverage-loss: pass->warn (проверка перестала выполняться) = регрессия",
               _diff_checks({"test": {"status": "pass"}}, {"test": {"status": "warn"}}) == (["test"], []))
        expect("coverage-loss: fail->warn (падавший тест удалён, а не починен) = регрессия",
               _diff_checks({"test": {"status": "fail"}}, {"test": {"status": "warn"}}) == (["test"], []))
        expect("coverage: warn->warn (тестов не было и нет) = НЕ регрессия",
               _diff_checks({"test": {"status": "warn"}}, {"test": {"status": "warn"}}) == ([], []))
        expect("coverage: warn->pass (тесты появились) = НЕ регрессия (улучшение)",
               _diff_checks({"test": {"status": "warn"}}, {"test": {"status": "pass"}}) == ([], []))
        # v2.87 (finding аудита): симметрично — warn/not_run -> fail = НОВАЯ краснота = регрессия.
        # На базе тестов не было (warn), правка добавила ПАДАЮЩИЙ тест -> раньше проскакивало
        # (implementation_verification baseline-освобождён) -> ложный green. Теперь ловим.
        expect("new-red: warn->fail (добавлен падающий тест) = регрессия",
               _diff_checks({"test": {"status": "warn"}}, {"test": {"status": "fail"}}) == (["test"], []))
        expect("new-red: not_run->fail = регрессия",
               _diff_checks({"x": {"status": "not_run"}}, {"x": {"status": "fail"}}) == (["x"], []))
        expect("new-red: None(нет в базе)->fail = регрессия",
               _diff_checks({}, {"x": {"status": "fail"}}) == (["x"], []))

        # v2.74: свод падающих проверок базы -> модель видит реальный вывод (что чинить)
        fs = _baseline_failure_summary({
            "test": {"status": "fail", "runs": [
                {"command": "npm test", "exit_code": 1, "ok": False,
                 "output_tail": "expected 'Вчера' got 'Сегодня'"}]},
            "build": {"status": "pass", "runs": [{"command": "npm run build", "ok": True}]}})
        expect("baseline-summary: включает падающий тест с выводом, пропускает прошедший build",
               "expected 'Вчера'" in fs and "npm test" in fs and "npm run build" not in fs)

        # интеграция: baseline_diff на репо без тулчейна (проверки not_run -> нет регрессий) ->
        # правка проходит по критерию no-regressions даже без «всё зелёное»
        it_bd = iter([{"op": "write", "path": "src/bd.py", "content": "b=1\n"}, {"done": True}])
        rep_bd = run_pipeline("baseline-diff", sig, root, lambda c: next(it_bd), policy=pol,
                              budget={"max_model_calls": 5}, feature="bd-fn",
                              commit=True, baseline_diff=True)
        expect("baseline_diff: критерий no-regressions в отчёте",
               rep_bd["ready_criterion"] == "no-regressions" and rep_bd["baseline"] is not None)
        expect("baseline_diff: нет регрессий -> ready_for_pr True",
               rep_bd["baseline"]["no_regressions"] is True and rep_bd["ready_for_pr"] is True)
        _git(root, "checkout", "-q", orig_branch)

        # v2.77 require_fix: no-regressions есть, но fixed пуст -> НЕ ready (правка не починила)
        it_rf = iter([{"op": "write", "path": "src/rf.py", "content": "r=1\n"}, {"done": True}])
        rep_rf = run_pipeline("require-fix", sig, root, lambda c: next(it_rf), policy=pol,
                              budget={"max_model_calls": 5}, feature="rf-fn",
                              commit=True, baseline_diff=True, require_fix=True)
        expect("require_fix: без fixed -> ready_for_pr False (не сломал, но и не починил)",
               rep_rf["baseline"]["no_regressions"] is True and rep_rf["ready_for_pr"] is False
               and rep_rf["ready_criterion"] == "no-regressions+require-fix")
        _git(root, "checkout", "-q", orig_branch)

        # v2.81 Containment: политика ПО УМОЛЧАНИЮ (policy не передан) блокирует git push
        # (block_push) и объявляет действующую изоляцию честно в report["containment"].
        # rep_iso создан без явной policy -> дефолт движка.
        expect("containment: дефолтная политика движка блокирует push + честный report",
               isinstance(rep_iso.get("containment"), dict)
               and rep_iso["containment"]["block_push"] is True
               and rep_iso["containment"]["sandbox"] is False
               and rep_iso["containment"]["shell_mode"] == "unrestricted")
        # sandbox=True -> shell по allowlist (произвольный shell выключен) — видно в отчёте
        it_sb = iter([{"op": "write", "path": "src/sb.py", "content": "s=1\n"}, {"done": True}])
        rep_sb = run_pipeline("в песочнице", sig, root, lambda c: next(it_sb),
                              budget={"max_model_calls": 5}, feature="sb-fn",
                              commit=True, sandbox=True, install_deps=False)
        expect("containment: sandbox=True -> shell_mode=allowlist + block_push в отчёте",
               rep_sb["containment"]["sandbox"] is True
               and rep_sb["containment"]["shell_mode"] == "allowlist"
               and rep_sb["containment"]["block_push"] is True)
        _git(root, "checkout", "-q", orig_branch)

        # v2.83 Full RunPlan: независимый ревью ai-review гейтов (writer ≠ judge).
        # QUICK + ui_changed -> трек VISUAL добавляет ux_review (ai-review). Без ревью он блокирует.
        sig_rv = dict(sig); sig_rv["ui_changed"] = True
        it_nr = iter([{"op": "write", "path": "src/nr.py", "content": "n=1\n"}, {"done": True}])
        rep_nr = run_pipeline("ui без ревью", sig_rv, root, lambda c: next(it_nr),
                              budget={"max_model_calls": 5}, feature="nr-fn",
                              commit=True, isolate=True, install_deps=False)
        expect("review: ui_changed -> ux_review в плане и БЕЗ ревью блокирует (unmet)",
               "ux_review" in rep_nr["gates"]["evaluated"] and "ux_review" in rep_nr["gates"]["unmet"]
               and rep_nr["reviews"] is None)
        _git(root, "checkout", "-q", orig_branch)

        # с независимым ревьюером, который выносит pass -> ux_review закрыт легитимно (вердикт judge).
        # v3.0.11: ревьюер СНАЧАЛА читает изменённый файл (реальная верификация), затем pass — иначе
        # блокирующий гейт не закрывается по рубер-стампу (0 reads).
        def pass_provider(prompt):
            if "--- src/rp.py ---" in prompt:
                return '{"kind":"reviewer-result","status":"pass","checks":[{"id":"ok","status":"pass"}]}'
            return '{"op":"read","path":"src/rp.py"}'
        it_rp = iter([{"op": "write", "path": "src/rp.py", "content": "p=1\n"}, {"done": True}])
        rep_rp = run_pipeline("ui с ревью pass", sig_rv, root, lambda c: next(it_rp),
                              budget={"max_model_calls": 20}, feature="rp-fn",
                              commit=True, isolate=True, install_deps=False,
                              review=True, reviewer_proposer=pass_provider)
        expect("review: независимый reviewer pass -> ux_review НЕ в unmet (закрыт вердиктом)",
               "ux_review" not in rep_rp["gates"]["unmet"]
               and any(r["gate"] == "ux_review" and r["status"] == "pass" for r in (rep_rp["reviews"] or [])))
        _git(root, "checkout", "-q", orig_branch)

        # ревьюер выносит fail -> ux_review блокирует (судья сильнее писателя; writer не переопределяет)
        fail_provider = lambda prompt: '{"kind":"reviewer-result","status":"fail","checks":[{"id":"ux","status":"fail"}],"blockers":["нет состояний экрана"]}'
        it_rf2 = iter([{"op": "write", "path": "src/rf2.py", "content": "f=1\n"}, {"done": True}])
        rep_rf2 = run_pipeline("ui с ревью fail", sig_rv, root, lambda c: next(it_rf2),
                               budget={"max_model_calls": 20}, feature="rf2-fn",
                               commit=True, isolate=True, install_deps=False,
                               review=True, reviewer_proposer=fail_provider)
        expect("review: reviewer fail -> ux_review блокирует (writer не переопределяет судью)",
               "ux_review" in rep_rf2["gates"]["unmet"]
               and any(r["gate"] == "ux_review" and r["status"] == "fail" for r in (rep_rf2["reviews"] or [])))
        _git(root, "checkout", "-q", orig_branch)

        # честная граница: детерминированный артефакт-гейт ревьюер НЕ закрывает (requirements — не ai-review)
        expect("review: детерминированные артефакт-гейты не входят в reviewable (requirements)",
               "requirements" not in _reviewable_gates(["requirements", "specification", "ux_review"], sig_rv)
               and "ux_review" in _reviewable_gates(["requirements", "ux_review"], sig_rv))

        # v2.85 (finding аудита): reviewer WARN с blockers на блокирующем гейте НЕ закрывает его -> блок.
        # rc11: warn ОБЯЗАН нести конкретные blockers (иначе вердикт невалиден — блок без причины).
        warn_provider = lambda prompt: ('{"kind":"reviewer-result","status":"warn",'
                                        '"checks":[{"id":"x","status":"warn"}],'
                                        '"blockers":["состояние загрузки экрана не покрыто"]}')
        it_rw = iter([{"op": "write", "path": "src/rw.py", "content": "w=1\n"}, {"done": True}])
        rep_rw = run_pipeline("ui с ревью warn", sig_rv, root, lambda c: next(it_rw),
                              budget={"max_model_calls": 20}, feature="rw-fn",
                              commit=True, isolate=True, install_deps=False,
                              review=True, reviewer_proposer=warn_provider)
        expect("review: reviewer WARN(c blockers) на блокирующем ux_review -> гейт блокирует (не тихий pass)",
               "ux_review" in rep_rw["gates"]["unmet"]
               and any(r["gate"] == "ux_review" and r["status"] == "warn" for r in (rep_rw["reviews"] or [])))
        _git(root, "checkout", "-q", orig_branch)

        # rc11: contentless warn (без blockers) — НЕвалидный вердикт (блок без причины): гейт НЕ
        # закрывается (остаётся unmet), а трейс помечается errors — вердикт отвергнут, не «тихий блок».
        cwarn_provider = lambda prompt: '{"kind":"reviewer-result","status":"warn","checks":[{"id":"x","status":"warn"}]}'
        it_cw = iter([{"op": "write", "path": "src/cw.py", "content": "c=1\n"}, {"done": True}])
        rep_cw = run_pipeline("ui с ревью warn без причины", sig_rv, root, lambda c: next(it_cw),
                              budget={"max_model_calls": 20}, feature="cw-fn",
                              commit=True, isolate=True, install_deps=False,
                              review=True, reviewer_proposer=cwarn_provider)
        expect("review rc11: warn без blockers -> вердикт невалиден (errors) и ux_review остаётся unmet",
               "ux_review" in rep_cw["gates"]["unmet"]
               and any(r["gate"] == "ux_review" and r.get("errors") for r in (rep_cw["reviews"] or [])))
        _git(root, "checkout", "-q", orig_branch)

        # v3.0-rc9 (finding живого прогона kimi): ревьюеру ОБЯЗАН передаваться контекст изменения
        # (дифф + список файлов). Раньше base_context был пуст -> прилежная модель честно возвращала
        # fail «нечего читать», делая ai-review структурно непроходимым. Ревьюер здесь ставит pass
        # ТОЛЬКО если реально увидел изменённый путь в своём промпте — доказывает доставку диффа.
        def ctx_reviewer(prompt):
            if "--- src/cx.py ---" in prompt:        # v3.0.11: уже прочитал реальный файл -> pass
                return '{"kind":"reviewer-result","status":"pass","checks":[{"id":"seen","status":"pass"}]}'
            if "src/cx.py" in prompt:                # дифф доставлен -> читаем изменённый путь
                return '{"op":"read","path":"src/cx.py"}'
            return ('{"kind":"reviewer-result","status":"fail","checks":[{"id":"seen","status":"fail"}],'
                    '"blockers":["контекст изменения пуст: не дан дифф/список файлов"]}')
        it_cx = iter([{"op": "write", "path": "src/cx.py", "content": "cx=1\n"}, {"done": True}])
        rep_cx = run_pipeline("ui с ревью, проверка доставки диффа", sig_rv, root, lambda c: next(it_cx),
                              budget={"max_model_calls": 20}, feature="cx-fn",
                              commit=True, isolate=True, install_deps=False,
                              review=True, reviewer_proposer=ctx_reviewer)
        expect("review rc9: ревьюер получает контекст изменения (дифф) -> видит src/cx.py и ставит pass",
               "ux_review" not in rep_cx["gates"]["unmet"]
               and any(r["gate"] == "ux_review" and r["status"] == "pass" for r in (rep_cx["reviews"] or [])))
        _git(root, "checkout", "-q", orig_branch)

        # v3.0.11 (finding аудита P2): рубер-стамп — pass БЕЗ единого чтения на БЛОКИРУЮЩЕМ гейте НЕ
        # закрывает его (симметрия с security-путём: «увидел дифф в контексте» != «проверил чтением»).
        rubber = lambda prompt: '{"kind":"reviewer-result","status":"pass","checks":[{"id":"ok","status":"pass"}]}'
        it_rs = iter([{"op": "write", "path": "src/rs.py", "content": "r=1\n"}, {"done": True}])
        rep_rs = run_pipeline("ui рубер-стамп без чтений", sig_rv, root, lambda c: next(it_rs),
                              budget={"max_model_calls": 20}, feature="rs-fn",
                              commit=True, isolate=True, install_deps=False,
                              review=True, reviewer_proposer=rubber)
        expect("v3.0.11 A8: pass БЕЗ чтений (0 reads) на блокирующем ux_review -> НЕ закрыт (рубер-стамп)",
               "ux_review" in rep_rs["gates"]["unmet"]
               and any(r["gate"] == "ux_review" and r.get("closed_as") == "blocked"
                       for r in (rep_rs["reviews"] or [])))
        _git(root, "checkout", "-q", orig_branch)

        # _change_context напрямую: список изменённых файлов + unified-дифф на точной ревизии
        _git(root, "checkout", "-q", "-B", "cx-direct")
        (root / "src" / "cxd.py").write_text("def cxd():\n    return 42\n", encoding="utf-8")
        _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "cxd")
        _, cxsha, _ = _git(root, "rev-parse", "HEAD")
        cc = _change_context(root, cxsha.strip())
        expect("_change_context: содержит изменённый путь и тело диффа",
               "src/cxd.py" in cc and "return 42" in cc)
        expect("_change_context: пустая ревизия -> пустой контекст (прежнее поведение)",
               _change_context(root, None) == "" and _change_context(root, "") == "")

        # v3.0-rc16 (P0): _change_context_range — интегрированный дифф base..head (вся цепочка, не только
        # последний коммит). Два коммита поверх базы -> оба файла в range-контексте.
        _git(root, "checkout", "-q", "-B", "cx-range")
        _, base_r, _ = _git(root, "rev-parse", "HEAD"); base_r = base_r.strip()
        (root / "src" / "rA.py").write_text("A = 1\n", encoding="utf-8")
        _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "pkgA")
        (root / "src" / "rB.py").write_text("B = 2\n", encoding="utf-8")
        _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "pkgB")
        _, head_r, _ = _git(root, "rev-parse", "HEAD"); head_r = head_r.strip()
        cr = _change_context_range(root, base_r, head_r)
        expect("v3.0-rc16 _change_context_range: видит ВСЕ коммиты диапазона (pkgA И pkgB), не только последний",
               "src/rA.py" in cr and "src/rB.py" in cr and "pkgA" in cr and "pkgB" in cr)
        # деградация: без base -> одиночная ревизия (только последний коммит)
        single = _change_context_range(root, None, head_r)
        expect("v3.0-rc16 _change_context_range: без base -> деградация до одиночной ревизии (только rB)",
               "src/rB.py" in single and "src/rA.py" not in single)
        _git(root, "checkout", "-q", orig_branch); _git(root, "branch", "-D", "cx-range")

        # v3.0-rc16 (P0): _security_verdict_errors — валидатор security-вердикта (убивает false-green)
        import validate_reviewer_result as _vrr2
        bad_pass = {"status": "pass"}          # НЕТ checks/gate/schema — раньше принимался как pass
        expect("v3.0-rc16 security-verdict: голый {status:pass} -> невалиден (нет checks/структуры)",
               bool(_security_verdict_errors(bad_pass, "abc123", ["injection"], _vrr2)))
        good_pass = {"schema_version": 1, "kind": "reviewer-result", "gate": "security",
                     "status": "pass", "reviewed_revision": "abc123",
                     "checks": [{"id": "injection", "status": "pass"}],
                     "domain_results": [{"domain": "injection", "status": "pass",
                                         "checks": [{"id": "no_injection_surface", "status": "pass"}],
                                         "evidence": [{"type": "code-read", "path": "a.py", "lines": "1-5"}]}]}
        expect("v3.0-rc16 security-verdict: структурный pass c domain_results -> валиден",
               _security_verdict_errors(good_pass, "abc123", ["injection"], _vrr2) == [])
        expect("v3.0-rc16 security-verdict: reviewed_revision != проверяемой -> невалиден",
               any("revision" in e for e in _security_verdict_errors(
                   {**good_pass, "reviewed_revision": "OTHER"}, "abc123", ["injection"], _vrr2)))
        # v3.0.1 (finding аудита P0): SecurityVerdict v2 — domain_results обязан покрыть КАЖДЫЙ применимый
        # домен. Один общий check по 4 доменам -> невалиден; пропущенный/лишний домен -> невалиден.
        four = ["authentication", "authorization_idol", "input_validation", "data_isolation"]
        one_generic = {"schema_version": 1, "kind": "reviewer-result", "gate": "security",
                       "status": "pass", "reviewed_revision": "abc123",
                       "checks": [{"id": "security-ok", "status": "pass"}]}
        expect("v3.0.1 SecVerdict-v2: 4 применимых домена + нет domain_results -> невалиден",
               any("domain_results" in e for e in _security_verdict_errors(one_generic, "abc123", four, _vrr2)))
        def _dr(doms, st="pass"):   # v3.0.7/v3.0.9 v2.1/v2.3: per-domain checks + evidence для pass
            return [{"domain": d, "status": st, "checks": [{"id": f"{d}_ok", "status": st}],
                     "evidence": [{"type": "code-read", "path": f"{d}.py", "lines": "1-9"}]} for d in doms]
        covered4 = {**one_generic, "domain_results": _dr(four)}
        expect("v3.0.1 SecVerdict-v2: domain_results покрывает все 4 (с per-domain checks) -> валиден",
               _security_verdict_errors(covered4, "abc123", four, _vrr2) == [])

        # v3.6.8 (finding живой квалификации): evidence type='file' с path — это code-read, а не «нераспознанный
        # type». Раньше валидный вердикт k3 (type='file'+path+lines) отвергался -> security ложно блокировал.
        file_ev = {**one_generic, "domain_results": [{"domain": "input_validation", "status": "pass",
                   "checks": [{"id": "iv_ok", "status": "pass"}],
                   "evidence": [{"type": "file", "path": "pricing.py", "lines": "10-11"}]}]}
        expect("v3.6.8 SecVerdict: evidence type='file'+path -> принимается как code-read (валиден)",
               _security_verdict_errors(file_ev, "abc123", ["input_validation"], _vrr2) == [])
        expect("v3.6.8 анти-false-green: type='file' на прочитанный путь -> валиден (reads-сверка ок)",
               _security_verdict_errors(file_ev, "abc123", ["input_validation"], _vrr2,
                                        reviewer_reads=["pricing.py"]) == [])
        expect("v3.6.8 анти-false-green: type='file' на НЕпрочитанный путь -> сфабрикован (невалиден)",
               any("сфабрикован" in e for e in _security_verdict_errors(
                   file_ev, "abc123", ["input_validation"], _vrr2, reviewer_reads=["other.py"])))
        noev = {**one_generic, "domain_results": [{"domain": "input_validation", "status": "pass",
                "checks": [{"id": "x", "status": "pass"}], "evidence": [{"type": "vibes"}]}]}
        expect("v3.6.8: evidence без распознаваемого type И без path -> невалиден (защита цела)",
               bool(_security_verdict_errors(noev, "abc123", ["input_validation"], _vrr2)))
        three = {**one_generic, "domain_results": _dr(four[:3])}
        expect("v3.0.1 SecVerdict-v2: покрыто 3 из 4 доменов -> невалиден (не закрыт)",
               any("не покрывает" in e for e in _security_verdict_errors(three, "abc123", four, _vrr2)))
        warn_dom = {**one_generic, "domain_results": [{"domain": d, "status": ("warn" if d == four[0] else "pass"),
                                                       "checks": [{"id": f"{d}_ok", "status": "pass"}]} for d in four]}
        expect("v3.0.1 SecVerdict-v2: warn в домене при общем pass -> несогласовано (невалиден)",
               any("несогласованно" in e for e in _security_verdict_errors(warn_dom, "abc123", four, _vrr2)))
        # v3.0.7 (finding аудита P1): SecurityVerdict v2.1 — pass домена БЕЗ per-domain checks не закрывает
        no_ev = {**one_generic, "domain_results": [{"domain": d, "status": "pass"} for d in four]}   # нет checks
        expect("v3.0.7 SecVerdict-v2.1: домены без per-domain checks -> невалиден (нет доказательств по домену)",
               any("domain-specific checks" in e for e in _security_verdict_errors(no_ev, "abc123", four, _vrr2)))
        # v3.0.8 SecVerdict-v2.2: пустой nested-check `checks:[{}]` больше НЕ проходит (нужен id+status)
        empty_ck = {**one_generic, "domain_results": [{"domain": d, "status": "pass", "checks": [{}]} for d in four]}
        expect("v3.0.8 SecVerdict-v2.2: nested-check без id/status (checks:[{}]) -> невалиден",
               any("nested-check без id" in e for e in _security_verdict_errors(empty_ck, "abc123", four, _vrr2)))
        # v3.0.9 SecVerdict-v2.3: pass-домен с id+status, но БЕЗ evidence-ссылки -> невалиден
        no_ref = {**one_generic, "domain_results": [{"domain": d, "status": "pass",
                                                     "checks": [{"id": f"{d}_ok", "status": "pass"}]} for d in four]}
        expect("v3.0.9 SecVerdict-v2.3: pass без evidence-ссылки -> невалиден (id+status не доказательство)",
               any("без evidence" in e for e in _security_verdict_errors(no_ref, "abc123", four, _vrr2)))
        # v3.0.10 (finding аудита P1): EvidenceRef — структура + сверка с РЕАЛЬНЫМ trace ревьюера.
        _one = ["injection"]

        def _dom_ev(ev):   # один домен injection pass с заданным evidence
            return {"schema_version": 1, "kind": "reviewer-result", "gate": "security", "status": "pass",
                    "reviewed_revision": "abc123", "checks": [{"id": "c", "status": "pass"}],
                    "domain_results": [{"domain": "injection", "status": "pass",
                                        "checks": [{"id": "injection_ok", "status": "pass"}], "evidence": ev}]}
        # (a) строка вместо структурной ссылки -> невалиден
        expect("v3.0.10 EvidenceRef: строка 'checked' (не структура) -> невалиден",
               any("не структурная ссылка" in e
                   for e in _security_verdict_errors(_dom_ev(["checked"]), "abc123", _one, _vrr2)))
        # (b) code-read без сверки (reviewer_reads=None) -> форма валидна (обратная совместимость)
        expect("v3.0.10 EvidenceRef: code-read с path без trace -> валиден (форма)",
               _security_verdict_errors(_dom_ev([{"type": "code-read", "path": "src/a.py", "lines": "1-9"}]),
                                        "abc123", _one, _vrr2) == [])
        # (c) code-read + trace, файл ДЕЙСТВИТЕЛЬНО прочитан -> валиден
        expect("v3.0.10 EvidenceRef: code-read ссылается на реально прочитанный файл -> валиден",
               _security_verdict_errors(_dom_ev([{"type": "code-read", "path": "src/a.py", "lines": "1-9"}]),
                                        "abc123", _one, _vrr2, reviewer_reads=["src/a.py"]) == [])
        # (d) code-read + trace, файл ревьюер НЕ читал -> невалиден (сфабрикованная ссылка)
        expect("v3.0.10 EvidenceRef: code-read на непрочитанный файл при наличии trace -> невалиден (фабрикация)",
               any("которого нет среди реально прочитанных" in e
                   for e in _security_verdict_errors(_dom_ev([{"type": "code-read", "path": "src/ghost.py"}]),
                                                     "abc123", _one, _vrr2, reviewer_reads=["src/a.py"])))
        # (e) test evidence без command -> невалиден; с command -> валиден (reviewer trace не требуется)
        expect("v3.0.10 EvidenceRef: test evidence без command -> невалиден",
               any("test evidence без command" in e
                   for e in _security_verdict_errors(_dom_ev([{"type": "test"}]), "abc123", _one, _vrr2)))
        expect("v3.0.10 EvidenceRef: test evidence с command -> валиден",
               _security_verdict_errors(_dom_ev([{"type": "test", "command": "pytest tests/"}]),
                                        "abc123", _one, _vrr2) == [])
        # (f) неизвестный type -> невалиден
        expect("v3.0.10 EvidenceRef: неизвестный type -> невалиден",
               any("без распознаваемого type" in e
                   for e in _security_verdict_errors(_dom_ev([{"type": "vibes", "note": "ok"}]),
                                                     "abc123", _one, _vrr2)))
        # (g) v3.0.11: одинаковое имя, РАЗНЫЙ путь — basename-fallback убран -> невалиден (фабрикация)
        expect("v3.0.11 EvidenceRef: same-basename другой путь (tests/config.py vs src/prod/config.py) -> невалиден",
               any("которого нет среди реально прочитанных" in e
                   for e in _security_verdict_errors(_dom_ev([{"type": "code-read", "path": "src/prod/config.py"}]),
                                                     "abc123", _one, _vrr2, reviewer_reads=["tests/config.py"])))
        # v3.0.11 (finding аудита P1): destructive-approval теперь валидируется STRICT (как в run_pipeline).
        # Legacy-«рыхлая» запись (без binds_to/expires_at/risk/trusted source) проходила по дефолтам,
        # но strict её отвергает — ровно та разница, на которую опирается фикс.
        import approvals as _a4
        _loose_destr = {"approval": "destructive", "approved_by": "u@x", "scope": ".", "reason": "ok"}
        expect("v3.0.11 destructive-strict: legacy-запись non-strict-валидна, но STRICT-невалидна",
               _a4._record_valid(_loose_destr) is True
               and _a4._record_valid(_loose_destr, now=_a4._now_iso(), plan_hash="x", strict=True) is False)

        # v3.0-rc20 (finding аудита P0): high-risk домен, применимый ПО ПУТЯМ, требует ApprovalRecord
        # (reviewer не закрывает). Dockerfile/CI -> deployment_config; обычный src -> ничего; catch-all
        # secrets ('.*') НЕ форсирует human на любом файле.
        expect("v3.0-rc20 approval-by-path: Dockerfile -> deployment_config требует ApprovalRecord",
               "deployment_config" in _human_approval_domains_uncovered(str(root), "no-wi", ["Dockerfile", "src/x.py"]))
        expect("v3.0-rc20 approval-by-path: .github/workflows -> deployment_config",
               "deployment_config" in _human_approval_domains_uncovered(str(root), "no-wi", [".github/workflows/deploy.yml"]))
        expect("v3.0-rc20 approval-by-path: обычный src -> human-approval НЕ требуется (нет over-block)",
               _human_approval_domains_uncovered(str(root), "no-wi", ["src/app.py", "tests/t.py"]) == [])
        _git(root, "checkout", "-q", orig_branch)

        # v3.0.1 (finding аудита P0): BASE BINDING — рабочая ветка форкается от --base, а НЕ от текущего
        # HEAD. Делаем ветку feat-base с ДРУГИМ SHA, checkout остаётся на orig_branch, прогон с base=feat-base.
        _git(root, "checkout", "-q", "-B", "feat-base")
        (root / "src" / "on_feat.py").write_text("FEAT = 1\n", encoding="utf-8")
        _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "commit on feat-base")
        _, feat_sha, _ = _git(root, "rev-parse", "HEAD"); feat_sha = feat_sha.strip()
        _git(root, "checkout", "-q", orig_branch)   # текущий checkout НЕ на feat-base
        it_bb = iter([{"op": "write", "path": "src/bb.py", "content": "b=1\n"}, {"done": True}])
        rep_bb = run_pipeline("base binding", {"task_type": "QUICK", "size": "small", "risk": "low",
                              "affected_areas": ["core"]}, root, lambda c: next(it_bb),
                              budget={"max_model_calls": 8}, feature="bb-fn",
                              commit=True, isolate=True, install_deps=False, base="feat-base")
        # ветка ai-ops/bb-fn должна форкнуться от feat_sha (виден on_feat.py в worktree), не от orig
        _wt_bb = root / ".ai" / "worktrees" / "bb-fn"
        _forked_ok = (_wt_bb / "src" / "on_feat.py").exists() if _wt_bb.is_dir() else False
        expect("v3.0.1 base-binding: worktree форкнут от --base (feat-base), а не от текущего HEAD",
               rep_bb.get("status") != "error" and _forked_ok
               and (rep_bb.get("base_binding") or {}).get("base_ref") == "feat-base")
        _git(root, "checkout", "-q", orig_branch)
        try:
            _wt2 = __import__("worktree"); _wt2.remove(root, "bb-fn", force=True)
        except Exception: pass
        _git(root, "worktree", "prune"); _git(root, "branch", "-D", "ai-ops/bb-fn"); _git(root, "branch", "-D", "feat-base")

        # v3.0.1 (P0): high-risk approval — legacy «рыхлая» запись (без binds_to/expires_at/risk/source)
        # НЕ закрывает high-risk домен (strict). Кладём такую запись и всё равно uncovered.
        import approvals as _appr3
        # рыхлая запись: без binds_to/expires_at/risk/source -> для high-risk strict-невалидна
        _appr3.write_record(str(root), "no-wi", "deployment_config", "u@x", ".", "ok")
        expect("v3.0.1 strict-approval: legacy рыхлый ApprovalRecord НЕ закрывает high-risk deployment_config",
               "deployment_config" in _human_approval_domains_uncovered(str(root), "no-wi", ["Dockerfile"]))

        # v3.0.2/v3.0.7 (finding аудита P0): _resolve_base — explicit строго, auto без хардкода main.
        expect("v3.0.7 resolve-base: явная локальная ветка -> resolved + source=explicit-local + SHA",
               (_rb := _resolve_base(root, orig_branch)).get("resolved") is True
               and _rb.get("source") == "explicit-local" and _rb.get("mode") == "explicit"
               and _rb.get("base_sha"))
        expect("v3.0.2 resolve-base: явная несуществующая ветка -> resolved=False (НЕ тихий HEAD)",
               _resolve_base(root, "no-such-branch-xyz").get("resolved") is False)
        # v3.0.7 auto-режим: base=None -> ВСЕГДА резолвится (в пределе — текущая ветка), не хардкод main
        _ab = _resolve_base(root, None)
        expect("v3.0.7 resolve-base: auto (base=None) -> resolved, mode=auto, реальная ветка (не 'main'-хардкод)",
               _ab.get("resolved") is True and _ab.get("mode") == "auto"
               and _ab.get("base_ref") == orig_branch and _ab.get("base_sha"))
        # v3.0.9 (finding аудита P0.1): единый RemoteBaseVerifier fail-closed — репо БЕЗ origin/ветки
        # в origin -> unverifiable (доставка недоступна, НЕ «успех по умолчанию»); одинаково для обеих цепочек.
        _rvb = _verify_remote_base(root, orig_branch, _resolve_base(root, orig_branch).get("base_sha"))
        expect("v3.0.9 verify-remote-base: нет origin -> unverifiable (fail-closed, не открыть PR)",
               _rvb.get("verdict") == "unverifiable" and _rvb.get("reason"))
        # v3.0.7 (P0.2): ЯВНАЯ несуществующая --base -> preflight-БЛОК ДО модели (0 model calls),
        # НЕ выполнение от произвольного HEAD. Раньше только доставка блокировалась; теперь весь прогон.
        it_nb = iter([{"op": "write", "path": "src/nb.py", "content": "n=1\n"}, {"done": True}])
        _model_calls = {"n": 0}
        def _counting_prop(c):
            _model_calls["n"] += 1
            return next(it_nb)
        rep_nb = run_pipeline("несуществующая база", {"task_type": "QUICK", "size": "small",
                              "risk": "low", "affected_areas": ["core"]}, root, _counting_prop,
                              budget={"max_model_calls": 8}, feature="nb-fn",
                              commit=True, isolate=True, open_pr=True, install_deps=False, base="no-such-branch-xyz")
        expect("v3.0.7 base-preflight: явная несуществующая base -> status=error, ready=False, base_binding.resolved=False",
               rep_nb.get("status") == "error" and rep_nb.get("ready_for_pr") is False
               and (rep_nb.get("base_binding") or {}).get("resolved") is False
               and "base-preflight" in (rep_nb.get("error") or ""))
        expect("v3.0.7 base-preflight: НОЛЬ вызовов модели (блок до исполнения) + worktree не создан",
               _model_calls["n"] == 0 and not (root / ".ai" / "worktrees" / "nb-fn").exists())
        _git(root, "checkout", "-q", orig_branch)

        # v2.85 (finding аудита): security НЕ отдаётся self-review той же модели даже без сигналов
        expect("no-self-review: security не в reviewable даже без спец-сигналов",
               "security" not in _reviewable_gates(["security", "ux_review"], sig_rv)
               and "ai_red_team" not in _reviewable_gates(["ai_red_team", "ux_review"], sig_rv))

        # v2.86 Product Authoring: ENGINEERING-план содержит артефакт-гейты requirements/plan_readiness.
        # БЕЗ --author они блокируют; с --author (валидный артефакт) — закрываются формой.
        sig_eng = {"task_type": "ENGINEERING", "size": "small", "risk": "low", "affected_areas": ["core"]}
        it_na = iter([{"op": "write", "path": "src/na.py", "content": "n=1\n"}, {"done": True}])
        rep_na = run_pipeline("рефактор без артефактов", sig_eng, root, lambda c: next(it_na),
                              budget={"max_model_calls": 5}, feature="eng-na",
                              commit=True, isolate=True, install_deps=False)
        has_art_gates = ("requirements" in rep_na["gates"]["evaluated"]
                         and "plan_readiness" in rep_na["gates"]["evaluated"])
        expect("authoring: ENGINEERING-план содержит requirements/plan_readiness",
               has_art_gates)
        expect("authoring: БЕЗ --author артефакт-гейты блокируют (unmet)",
               "requirements" in rep_na["gates"]["unmet"] and "plan_readiness" in rep_na["gates"]["unmet"]
               and rep_na["authored"] is None)
        _git(root, "checkout", "-q", orig_branch)

        def author_provider(prompt):
            if "requirements-artifact" in prompt:
                return ("schema_version: 1\nkind: requirements-artifact\nrequirements:\n"
                        "  - id: R1\n    statement: фильтр по статусу сужает список\n"
                        "    acceptance:\n      - when статус=paid then только оплаченные\n")
            if "spec-change" in prompt:      # v2.89: ENGINEERING-план включает specification
                return ("schema_version: 1\nkind: spec-change\ncapability: catalog\nwhy: нужен фильтр\n"
                        "what_changes:\n  - добавить фильтр по статусу\ntasks:\n  - реализовать\n"
                        "requirements:\n  - name: Filter\n    text: The system SHALL filter by status.\n"
                        "    scenarios:\n      - {name: T, when: статус=paid, then: показаны оплаченные}\n")
            return ("schema_version: 1\nkind: plan-artifact\nwork_packages:\n"
                    "  - id: WP1\n    summary: добавить фильтр\n    depends_on: []\n"
                    "write_scope:\n  - src/\n")
        it_au = iter([{"op": "write", "path": "src/au.py", "content": "a=1\n"}, {"done": True}])
        rep_au = run_pipeline("рефактор с артефактами", sig_eng, root, lambda c: next(it_au),
                              budget={"max_model_calls": 5}, feature="eng-au",
                              commit=True, isolate=True, install_deps=False,
                              author=True, author_proposer=author_provider)
        expect("authoring: валидный артефакт закрывает requirements/plan_readiness (форма)",
               "requirements" not in rep_au["gates"]["unmet"]
               and "plan_readiness" not in rep_au["gates"]["unmet"])
        expect("authoring: трейс authored валиден + артефакт на диске",
               rep_au["authored"] and all(a["valid"] for a in rep_au["authored"])
               and (root / ".ai" / "worktrees" / "eng-au" / ".ai" / "runplan" / "eng-au" / "requirements.yaml").exists())
        _git(root, "checkout", "-q", orig_branch)

        # невалидный артефакт (author вернул мусор) -> гейт НЕ закрывается (форма не подтверждена)
        bad_author = lambda prompt: "это не yaml артефакта, просто текст"
        it_ba = iter([{"op": "write", "path": "src/ba.py", "content": "b=1\n"}, {"done": True}])
        rep_ba = run_pipeline("рефактор с битым артефактом", sig_eng, root, lambda c: next(it_ba),
                              budget={"max_model_calls": 5}, feature="eng-ba",
                              commit=True, isolate=True, install_deps=False,
                              author=True, author_proposer=bad_author)
        expect("authoring: невалидный артефакт -> requirements остаётся блокирующим (нет фабрикации)",
               "requirements" in rep_ba["gates"]["unmet"]
               and any(not a["valid"] for a in (rep_ba["authored"] or [])))
        _git(root, "checkout", "-q", orig_branch)

        # v3.0-rc14 (finding живой квалификации kimi): author ФЛАКНУЛ на первой попытке (пустой/битый
        # YAML) -> ретрай с нуджем -> валидный артефакт. Без ретрая флаки-провайдер ложно оставлял
        # гейт незакрытым (не движковый дефект — но на multi-package прогоне почти всегда кто-то падал).
        def flaky_author(prompt):
            if "[повтор" not in prompt:          # первая попытка — битый вывод (как флаки-провайдер)
                return "(пустой ответ модели)"
            return author_provider(prompt)       # на ретрае с нуджем — валидный артефакт
        it_fk = iter([{"op": "write", "path": "src/fk.py", "content": "f=1\n"}, {"done": True}])
        rep_fk = run_pipeline("рефактор с флаки-автором", sig_eng, root, lambda c: next(it_fk),
                              budget={"max_model_calls": 20}, feature="eng-fk",
                              commit=True, isolate=True, install_deps=False,
                              author=True, author_proposer=flaky_author)
        # ФОРМА всех артефактов восстановлена ретраем (valid=True); закрытие specification-гейта
        # отдельно зависит от openspec CLI (в CI его нет) — потому проверяем requirements + valid-форму,
        # а не "specification not in unmet" (это готча openspec, не про ретрай).
        expect("v3.0-rc14 authoring: флак на 1-й попытке -> ретрай восстанавливает валидную ФОРМУ артефактов",
               "requirements" not in rep_fk["gates"]["unmet"]
               and rep_fk["authored"] and all(a["valid"] for a in rep_fk["authored"]))
        _git(root, "checkout", "-q", orig_branch)

        # v3.0-rc14: но если author флакает ВСЕГДА — гейт честно НЕ закрывается (ретрай не фабрикует)
        always_bad = lambda prompt: "(пустой ответ модели)"
        it_ab = iter([{"op": "write", "path": "src/ab.py", "content": "b=1\n"}, {"done": True}])
        rep_ab = run_pipeline("рефактор с вечно-битым автором", sig_eng, root, lambda c: next(it_ab),
                              budget={"max_model_calls": 20}, feature="eng-ab",
                              commit=True, isolate=True, install_deps=False,
                              author=True, author_proposer=always_bad)
        expect("v3.0-rc14 authoring: вечный флак -> гейт остаётся блокирующим после ретраев (честно)",
               "requirements" in rep_ab["gates"]["unmet"]
               and any(not a["valid"] for a in (rep_ab["authored"] or [])))
        # v3.0-rc5 (finding живого прогона kimi): парсер терпим к прозе/несколькими блокам
        expect("v3.0-rc5 parse: YAML после прозы (без ограды) извлекается",
               (_parse_yaml_block("Вот артефакт:\n\nschema_version: 1\nkind: requirements-artifact\n"
                                  "requirements:\n  - id: R1\n") or {}).get("kind") == "requirements-artifact")
        expect("v3.0-rc5 parse: несколько ```-блоков — берётся первый валидный dict",
               (_parse_yaml_block("```text\nбла\n```\nтекст\n```yaml\nschema_version: 1\nkind: plan-artifact\n```")
                or {}).get("kind") == "plan-artifact")
        expect("v3.0-rc5 parse: мусор без YAML -> None (нет фабрикации)",
               _parse_yaml_block("просто текст без артефакта") is None)
        # v2.123 (P0.1) НАСТОЯЩИЙ Spec-First: невалидная author-спека -> tool loop НЕ запущен (0 кода)
        expect("v2.123 (P0.1): невалидная спека -> tool loop НЕ запущен (spec-prestage-failed, 0 impl)",
               rep_ba["loop"]["stopped"] == "spec-prestage-failed"
               and rep_ba["spec_first"]["prestage"]["implementation_skipped"] is True
               and rep_ba["ready_for_pr"] is False)
        expect("v2.123 (P0.1): при невалидной спеке код НЕ записан (src/ba.py отсутствует)",
               not (root / ".ai" / "worktrees" / "eng-ba" / "src" / "ba.py").exists())
        # позитив: валидная спека -> реализация ЗАПУСКАЕТСЯ (src/au.py записан), prestage не пропущен
        expect("v2.123 (P0.1): валидная спека -> реализация запущена (src/au.py записан)",
               (root / ".ai" / "worktrees" / "eng-au" / "src" / "au.py").exists()
               and rep_au["spec_first"]["prestage"]["implementation_skipped"] is False)
        _git(root, "checkout", "-q", orig_branch)

        # v2.89: specification authoring (OpenSpec). Тестируем _run_authoring напрямую со стабом
        # openspec_validate (реальный CLI в CI может отсутствовать — стаб делает тест детерминированным).
        spec_author = lambda prompt: (
            "schema_version: 1\nkind: spec-change\ncapability: pricing\nwhy: нужна утилита цены\n"
            "what_changes:\n  - добавить formatPrice\ntasks:\n  - реализовать\n  - тест\n"
            "requirements:\n  - name: Formatting\n    text: The system SHALL format price.\n"
            "    scenarios:\n      - {name: T, when: formatPrice(1000), then: returns 1 000}\n")
        gev_ok, auth_ok, _ = _run_authoring(spec_author, root, ["specification"], {}, "spec-ok",
                                            "форматирование цены", {"max_model_calls": 5},
                                            openspec_validate=lambda wr, cid: (True, True, "valid"))
        expect("spec-authoring: CLI доступен + strict OK -> specification закрыт (openspec_valid)",
               "specification" in gev_ok
               and gev_ok["specification"]["provided"] == ["openspec_valid", "requirements_covered"]
               and (root / "openspec" / "changes" / "spec-ok" / "proposal.md").exists())
        gev_absent, auth_absent, _ = _run_authoring(spec_author, root, ["specification"], {}, "spec-abs",
                                                    "форматирование", {"max_model_calls": 5},
                                                    openspec_validate=lambda wr, cid: (False, False, "нет CLI"))
        expect("spec-authoring: CLI отсутствует -> specification НЕ закрыт (честный блок, нет фабрикации)",
               "specification" not in gev_absent
               and any(a["gate"] == "specification" and a.get("closed") is False for a in auth_absent))
        gev_bad, auth_bad, _ = _run_authoring(lambda p: "не yaml", root, ["specification"], {}, "spec-bad",
                                              "x", {"max_model_calls": 5},
                                              openspec_validate=lambda wr, cid: (True, True, "valid"))
        expect("spec-authoring: битый spec от автора -> не закрыт (форма не прошла)",
               "specification" not in gev_bad
               and any(a["gate"] == "specification" and not a["valid"] for a in auth_bad))
        # v3.0-rc8 (finding живого прогона kimi): task-строка с двоеточием («Написать тесты: A, B») YAML
        # парсит как mapping -> раньше vsa.check «список строк» падал. Нормализация -> валиден.
        colon_author = lambda prompt: (
            "schema_version: 1\nkind: spec-change\ncapability: pricing\nwhy: нужна утилита\n"
            "what_changes:\n  - добавить formatPrice\n"
            "tasks:\n  - Написать unit-тесты: все ветвления, граничные значения, ошибочный ввод\n  - реализовать\n"
            "requirements:\n  - name: Fmt\n    text: The system SHALL format price.\n"
            "    scenarios:\n      - {name: T, when: x, then: y}\n")
        gev_colon, auth_colon, _ = _run_authoring(colon_author, root, ["specification"], {}, "spec-colon",
                                                  "цена", {"max_model_calls": 5},
                                                  openspec_validate=lambda wr, cid: (True, True, "valid"))
        expect("v3.0-rc8: task-строка с двоеточием нормализуется -> specification валиден (не ложный блок)",
               any(a["gate"] == "specification" and a["valid"] for a in auth_colon))

        # write вне scope -> denied, файл не создан, но pipeline не падает
        it2 = iter([{"op": "write", "path": "config/x", "content": "y"}, {"done": True}])
        rep2 = run_pipeline("вне scope", sig, root, lambda c: next(it2), policy=pol,
                            budget={"max_model_calls": 5})
        expect("pipeline: out-of-scope запись отклонена (denied>0)", rep2["loop"]["denied"] >= 1
               and not (root / "config" / "x").exists())

    print("execution_pipeline selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
