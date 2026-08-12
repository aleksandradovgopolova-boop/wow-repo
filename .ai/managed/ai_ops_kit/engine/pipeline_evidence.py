#!/usr/bin/env python3
"""Evidence collection, authoring, and review functions for the execution pipeline.

Extracted from execution_pipeline.py — artifact authoring, independent reviews,
security review, evidence re-evaluation, dependency installation.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
for _p in (PKG / "tools", PKG / "validation"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ai_ops_kit.engine import tool_loop       # noqa: E402
from ai_ops_kit.engine import tool_broker     # noqa: E402
from ai_ops_kit.gates import gate_executor   # noqa: E402
from ai_ops_kit.gates import gate_policy     # noqa: E402

from ai_ops_kit.engine.pipeline_helpers import (   # noqa: E402
    _parse_yaml_block, _openspec_validate, _authoring_specs,
    _reviewable_gates, _gate_checklist,
)
from ai_ops_kit.engine.pipeline_git import _change_context  # noqa: E402
from ai_ops_kit.engine.pipeline_failure import _security_verdict_errors, _evidence_ref_errors  # noqa: E402


def _install_dependencies(profile, root, policy):
    """Поставить зависимости стеков (install_command) через Broker перед сбором evidence."""
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


def _author_with_retry(author_proposer, base_prompt, check_fn, bud, attempts=3):
    """v3.0-rc14 (finding живой квалификации kimi): author-вызов ретраится при невалидном/пустом
    артефакте."""
    from ai_ops_kit.engine import budget as _budget_mod
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
        prompt = base_prompt + (
            f"\n\n[повтор {attempt + 1}/{attempts}] Твой предыдущий ответ НЕ прошёл валидацию: "
            f"{'; '.join(str(e) for e in (errs or [])[:3])}. Верни ТОЛЬКО валидный YAML строго по схеме "
            "выше — без прозы, без markdown-ограды, все обязательные поля заполнены.")
    return data, errs


def _run_spec_authoring(author_proposer, work_root, gate_ev, wid, task, bud, openspec_validate):
    """v2.89: произвести OpenSpec change для гейта specification."""
    from ai_ops_kit.shared import _bootstrap  # noqa: F401 — кладёт validation/ в sys.path ДО плоских импортов ниже
    from ai_ops_kit.validation import validate_spec_artifact as vsa
    prompt = (
        "Ты автор OpenSpec-изменения (spec-change) для задачи. Верни ТОЛЬКО YAML со схемой:\n"
        "  schema_version: 1\n  kind: spec-change\n  capability: <slug>\n  why: <зачем>\n"
        "  what_changes: [<что меняется>]\n  tasks: [<шаг>, ...]\n"
        "  requirements:\n    - name: <имя>\n      text: <нормативное требование со словом SHALL>\n"
        "      scenarios:\n        - {name: <имя>, when: <условие>, then: <результат>}\n"
        "Требования конкретные и проверяемые. Только JSON/YAML.\n\n=== ЗАДАЧА ===\n" + task)

    def _spec_check(data):
        if not isinstance(data, dict):
            return ["author не вернул валидный YAML spec-change"]
        for _k in ("tasks", "what_changes"):
            _v = data.get(_k)
            if isinstance(_v, list):
                data[_k] = [(x if isinstance(x, str)
                             else "; ".join(f"{k}: {vv}" for k, vv in x.items()) if isinstance(x, dict)
                             else str(x)) for x in _v]
        return vsa.check(data)

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
    """v2.86 Product Authoring: движок производит артефакты requirements/plan."""
    from ai_ops_kit.engine import budget as _budget_mod
    bud = budget if isinstance(budget, _budget_mod.Budget) else _budget_mod.Budget.from_dict(budget)
    out_dir = Path(work_root) / ".ai" / "runplan" / wid
    gate_ev = dict(gate_ev)
    authored, wrote = [], False
    for gid, (fname, mod, kind, shape) in _authoring_specs().items():
        if gid not in gate_ids or gid in gate_ev:
            continue
        prompt = (
            f"Ты автор артефакта '{kind}' для задачи. Верни ТОЛЬКО YAML (без пояснений) со схемой:\n"
            f"  schema_version: 1\n  kind: {kind}\n  workitem_id: {wid}\n  {shape}\n"
            f"Артефакт должен точно отражать задачу ниже. Требования/пакеты — конкретные и "
            f"тестируемые, не общие слова.\n\n=== ЗАДАЧА ===\n{task}")

        # `mod=mod` связывает модуль ЗДЕСЬ, а не в момент вызова: сейчас `_check` зовётся
        # синхронно в этой же итерации и потому работает, но замыкание на переменную цикла
        # ломается молча, если вызов когда-нибудь станет отложенным (ревизия 2026-08-11).
        def _check(data, mod=mod):
            return mod.check(data) if isinstance(data, dict) else ["author не вернул валидный YAML артефакта"]

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
            continue
        p = out / fn
        try:
            if p.is_file():
                parts.append(f"# {e.get('gate')} ({fn})\n" + p.read_text(encoding="utf-8")[:2000])
        except OSError:
            pass
    return ("=== СПЕЦИФИКАЦИЯ ЗАДАЧИ (создана ДО реализации; следуй ей) ===\n" + "\n\n".join(parts)
            if parts else "")


def contour_consistency_evidence(child_root, wid, changed_files, timeout=120):
    """Evidence гейта `contour_consistency` (v3.35): сверка затронутых контуров с объявленным.

    ПОЧЕМУ ПОДПРОЦЕСС, А НЕ ИМПОРТ. Прямой вызов `ai_ops_kit.planning.contours` из движка добавил бы
    ребро `engine -> planning`, а вместе с ним новые циклы через `planning -> lifecycle`; ратчет
    `packages/layering.yaml` поймал бы это сразу. Шов тот же, что у `validate_product_model`:
    точка входа `tools/contours.py` + машиночитаемый вывод.

    Гейт ADVISORY: несогласованность даёт `warn`, а не `fail` (правило движения по roadmap —
    blocking только после обкатки на child-репозиториях). Недоступность инструмента тоже `warn`,
    НИКОГДА `pass`: молчаливое «всё согласовано» на непроведённой проверке — ложный зелёный.
    -> {"status": "pass"|"warn", "provided": [...], "evidence": [...], "report": {...}|None}
    """
    import subprocess as _sp
    from pathlib import Path as _P
    root = _P(child_root)
    entry = _P(__file__).resolve()
    pkg = next((x for x in entry.parents if (x / "VERSION").is_file()), entry.parents[2])
    tool = pkg / "tools" / "contours.py"
    if not tool.is_file():
        return {"status": "warn", "provided": [], "report": None,
                "evidence": ["инструмент связности контуров недоступен — проверка НЕ проведена "
                             "(это не 'согласовано')"]}
    args = [sys.executable, str(tool), "reconcile", str(root),
            "--files", ",".join(changed_files or []), "--json"]
    wi = root / "features" / str(wid) / "workitem.yaml"
    if wi.is_file():
        args += ["--workitem", str(wi)]
    try:
        r = _sp.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, _sp.SubprocessError) as e:
        return {"status": "warn", "provided": [], "report": None,
                "evidence": [f"сверка контуров не выполнена ({type(e).__name__}) — "
                             f"проверка НЕ проведена"]}
    # КОД ВОЗВРАТА ОБЯЗАТЕЛЕН. Прежде он игнорировался, и недостоверный реестр (ModelCorrupt внутри
    # подпроцесса) давал пустой stdout -> дальше срабатывала ветка «изменений не предъявлено», хотя
    # файлы изменены. Битый реестр становился неотличим от пустого диффа: признание подменялось
    # утверждением. Сбой проверки — это `warn` с честной причиной, а не молчание про дифф.
    if r.returncode != 0:
        why = ((r.stderr or r.stdout or "").strip().splitlines() or ["без сообщения"])[0][:200]
        return {"status": "warn", "provided": [], "report": None,
                "evidence": [f"сверка контуров НЕ ПРОВЕДЕНА (код {r.returncode}): {why}"]}
    try:
        rep = json.loads((r.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        return {"status": "warn", "provided": [], "report": None,
                "evidence": ["сверка контуров вернула неразбираемый ответ — проверка НЕ проведена"]}
    major = [f for f in (rep.get("findings") or []) if f.get("severity") == "major"]
    behind = [f for f in major if f.get("id") == "source_of_truth_behind"]
    unknown = [f for f in (rep.get("findings") or []) if f.get("id") == "unknown_contour"]
    lines = [f"изменённых путей {len(changed_files or [])} · вердикт {rep.get('verdict')}"]
    if behind:
        lines.append("описание контура отстало от кода: "
                     + ", ".join(f["contour"] for f in behind))
    lines += [f"{f['id']} / {f['contour']}: {f['detail']}" for f in major[:6]]
    if unknown:
        lines.append(f"контуров без сигнальных путей (состояние не определяется): "
                     f"{', '.join(f['contour'] for f in unknown)}")
    if not rep.get("comparable"):
        return {"status": "warn", "provided": ["changed_files"], "report": rep,
                "evidence": ["изменений не предъявлено — сверять нечего (это не 'согласовано')"]}
    return {"status": "pass" if not major else "warn",
            "provided": ["affects", "changed_files", "verdict"],
            "evidence": lines, "report": rep}


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


def _run_reviews(reviewer_proposer, work_root, gate_ids, gate_ev, signals, revision, budget,
                 max_reads=10, change_context=None,
                 calibrated_enforcement=False, ui_evidence=None):
    """Прогнать независимые ревью для ai-review гейтов плана, у которых ещё нет evidence."""
    from ai_ops_kit.validation import validate_reviewer_result as vrr
    gates = gate_executor.load_gates()
    ro_policy = tool_broker.Policy(level="read-only", child_root=str(work_root))
    reviews = []
    gate_ev = dict(gate_ev)
    valid_ids = None
    try:
        valid_ids = set(gates)
    except Exception:
        valid_ids = None
    change_ctx = change_context if change_context is not None else _change_context(work_root, revision)
    for gid in _reviewable_gates(gate_ids, signals):
        if gid in gate_ev:
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
                 "blockers": (res or {}).get("blockers") if isinstance(res, dict) else None,
                 "errors": errs or None}
        reviews.append(entry)
        if errs:
            continue
        status = res.get("status")
        blocking = bool(g.get("blocking"))
        ev_ref = f"independent reviewer verdict @ {revision or 'HEAD'}"
        ev_status = "not_run"
        calib_ui = calibrated_enforcement and gid in gate_policy.UI_GATES
        if calib_ui and isinstance(ui_evidence, dict):
            ev_status = (ui_evidence.get(gid) or {}).get("deterministic_status", "not_run")
        if status == "pass" and blocking and not (rv.get("reads")):
            gate_ev[gid] = {"status": "fail",
                            "blockers": [f"reviewer вынес pass без единого чтения (0 reads) — рубер-штамп "
                                         f"не закрывает блокирующий гейт @ {gid}; требуется верификация чтением"],
                            "checks": res.get("checks", []), "evidence": [ev_ref]}
            entry["closed_as"] = "blocked"
            entry["status"] = "fail"
            continue
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
            blockers = res.get("blockers") or (
                [f"reviewer WARN на блокирующем гейте — не чистый pass @ {gid}"] if status == "warn"
                else [f"reviewer FAIL @ {gid}"])
            gate_ev[gid] = {"status": "fail", "blockers": blockers,
                            "checks": res.get("checks", []), "evidence": [ev_ref]}
            entry["closed_as"] = "blocked"
        else:
            gate_ev[gid] = {"status": status, "provided": list(req),
                            "checks": res.get("checks", []), "evidence": [ev_ref]}
            entry["closed_as"] = status
    return gate_ev, reviews


def _review_security(reviewer_proposer, work_root, pack_result, revision, budget, change_context=None):
    """v2.106: независимый security-reviewer выносит вердикт по needs_review доменам."""
    from ai_ops_kit.security import security_pack
    from ai_ops_kit.validation import validate_reviewer_result as vrr
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
# `tools/pipeline_evidence.py` остаётся объявленной точкой входа, и молчаливый выход с кодом 0 — тот
# самый дефект, который ловит `tests/unit/test_alias_entry_points.py` («ноль и есть симптом»).
# Поэтому вход делает осмысленную работу — печатает назначение модуля, как `invariants.py`.
# Проверки модуля — в `tests/unit/`.
if __name__ == "__main__":
    print(__doc__)
    print("Проверки этого модуля — в tests/unit/ (pytest), отдельного --selftest нет с v3.30.")
