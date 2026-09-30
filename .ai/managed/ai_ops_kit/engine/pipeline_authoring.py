#!/usr/bin/env python3
"""Артефакт-авторинг и contour-evidence для execution-пайплайна.

Сателлит `pipeline_evidence.py` (разрез монолита при потолке размера). Здесь живёт кластер
«авторинг + contour-evidence» — производство requirements/plan/spec-change артефактов
author-моделью и сверка затронутых контуров подпроцессом. Пробо-свободен: мутационные пробы
(guard/seam) остаются в фасаде `pipeline_evidence.py`. Между кластерами нет перекрёстных вызовов;
фасад ре-экспортирует эти имена, сателлит фасад НЕ импортирует (ни одного обратного ребра).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ai_ops_kit.engine import tool_broker

from ai_ops_kit.engine.pipeline_helpers import (
    _parse_yaml_block, _openspec_validate, _authoring_specs,
)


def _install_dependencies(profile, root, policy):
    """Поставить зависимости стеков (install_command) через Broker перед сбором evidence.

    P0 (аудит 04.09): tool-loop модели теперь под shell_scope_guard=True (запись мимо write_scope
    откатывается). Установка зависимостей — ДРУГАЯ фаза: `npm ci`/`pip`/сборка законно пишут
    lock-файлы и артефакты ВНЕ write_scope, поэтому здесь берём install-политику со СНЯТЫМ
    scope-guard. protected_paths и shell_path_guard остаются в силе — install по-прежнему НЕ может
    молча писать в security/production/движок; ослабляется ровно write_scope, ничего больше."""
    import copy
    install_policy = copy.copy(policy)
    install_policy.shell_scope_guard = False
    results = []
    seen = set()
    for stack in profile.get("stacks", []) or []:
        cmd = stack.get("install_command")
        if not cmd or cmd in seen:
            continue
        seen.add(cmd)
        ev = tool_broker.execute({"op": "shell", "command": cmd, "timeout": 600}, root, install_policy)
        results.append({"language": stack.get("language"), "command": cmd,
                        "allowed": ev.get("allowed"), "ok": ev.get("ok", False),
                        "exit_code": ev.get("exit_code"),
                        "output_tail": (ev.get("output_tail") or "")[-200:]})
    return results


def _raw_author_tail(raw, limit=500):
    """Обрезанный хвост СЫРОГО ответа author-модели — для диагностики, когда YAML не разобрался.

    ПОЧЕМУ. При провале парсинга движок писал только «author не вернул валидный YAML» и ВЫБРАСЫВАЛ
    то, что модель реально вернула (живой прогон #587: спека забракована, а чем — не видно в отчёте).
    Здесь — увидеть ответ: пусто / проза / обрезано. -> строка ≤limit или "" .
    """
    s = (raw if isinstance(raw, str) else "" if raw is None else str(raw)).strip()
    if not s:
        return "(пусто)"
    return s if len(s) <= limit else s[:limit] + f"… (+{len(s) - limit} симв.)"


def _author_with_retry(author_proposer, base_prompt, check_fn, bud, attempts=3):
    """v3.0-rc14 (finding живой квалификации kimi): author-вызов ретраится при невалидном/пустом
    артефакте.

    -> (data, errs, raw_tail). raw_tail — обрезанный сырой ответ последней попытки при провале
    (для диагностики в отчёте), иначе None. Сигнатура из 3 значений с #587: раньше сырой ответ
    выбрасывался, и причина невалидной спеки была не видна."""
    from ai_ops_kit.shared import budget as _budget_mod
    prompt = base_prompt
    data, errs, raw = None, ["author не вызван"], None
    for attempt in range(attempts):
        try:
            bud.charge_call()
        except _budget_mod.BudgetExceeded as e:
            return data, [f"budget: {e}"], None
        raw = author_proposer(prompt)
        data = _parse_yaml_block(raw)
        errs = check_fn(data)
        if not errs:
            return data, errs, None
        prompt = base_prompt + (
            f"\n\n[повтор {attempt + 1}/{attempts}] Твой предыдущий ответ НЕ прошёл валидацию: "
            f"{'; '.join(str(e) for e in (errs or [])[:3])}. Верни ТОЛЬКО валидный YAML строго по схеме "
            "выше — без прозы, без markdown-ограды, все обязательные поля заполнены.")
    return data, errs, _raw_author_tail(raw)


def _run_spec_authoring(author_proposer, work_root, gate_ev, wid, task, bud, openspec_validate):
    """v2.89: произвести OpenSpec change для гейта specification."""
    # Проверка формы и рендер СОДЕРЖИМОГО живут ВНИЗ, в пакете `checks` (слой primitives): движок
    # зовёт их вниз, без восходящего ребра engine -> validation (лента №5). Запись файлов — ниже.
    from ai_ops_kit.checks import spec_artifact as vsa
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

    data, errs, raw_tail = _author_with_retry(author_proposer, prompt, _spec_check, bud)
    entry = {"gate": "specification", "artifact": f"openspec/changes/{wid}", "valid": not errs,
             "errors": errs or None}
    if errs:
        # #587: показать, ЧТО вернула модель, когда YAML не разобрался — иначе причина невалидной
        # спеки не видна в отчёте (было только «author не вернул валидный YAML»).
        if raw_tail:
            entry["author_output_tail"] = raw_tail
        return gate_ev, entry
    # Запись (I/O) — забота движка: чистая render_content строит содержимое, движок пишет файлы под
    # openspec-корень. Так рендер живёт вниз (в `checks`), а запись остаётся в слое ядра.
    _openspec_root = Path(work_root) / "openspec"
    for _rel, _content in vsa.render_content(data, wid):
        _target = _openspec_root / _rel
        _target.parent.mkdir(parents=True, exist_ok=True)
        _target.write_text(_content, encoding="utf-8")
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
    from ai_ops_kit.shared import budget as _budget_mod
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

        data, errs, raw_tail = _author_with_retry(author_proposer, prompt, _check, bud)
        if errs and any("budget:" in str(e) for e in errs):
            authored.append({"gate": gid, "valid": False, "errors": errs})
            break
        entry = {"gate": gid, "artifact": fname, "valid": not errs, "errors": errs or None}
        # #587: если YAML не разобрался — приложить обрезанный сырой ответ модели к записи, чтобы
        # причина (пусто/проза/обрезано) была видна в run-report, а не терялась.
        if errs and raw_tail:
            entry["author_output_tail"] = raw_tail
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
    точка входа `ai_ops_kit/planning/contours.py` + машиночитаемый вывод.

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
    # v4.0: плоский слой tools/ снят — инструмент связности контуров зовётся пакетно.
    import os as _os
    tool = pkg / "ai_ops_kit" / "planning" / "contours.py"
    if not tool.is_file():
        return {"status": "warn", "provided": [], "report": None,
                "evidence": ["инструмент связности контуров недоступен — проверка НЕ проведена "
                             "(это не 'согласовано')"]}
    args = [sys.executable, "-m", "ai_ops_kit.planning.contours", "reconcile", str(root),
            "--files", ",".join(changed_files or []), "--json"]
    wi = root / "features" / str(wid) / "workitem.yaml"
    if wi.is_file():
        args += ["--workitem", str(wi)]
    env = dict(_os.environ)
    env["PYTHONPATH"] = str(pkg) + ((_os.pathsep + env["PYTHONPATH"]) if env.get("PYTHONPATH") else "")
    try:
        r = _sp.run(args, capture_output=True, text=True, timeout=timeout, check=False, env=env)
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
