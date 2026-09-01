#!/usr/bin/env python3
"""Sequential-mode оркестратор (минимальный общий знаменатель, принцип 29).

Исполняет workflow-контракт последовательно одной моделью с изоляцией ролей:
  - для каждой стадии строится ОТДЕЛЬНЫЙ role prompt из markdown-агента;
  - judge-стадии (review_mode: read-only) получают ТОЛЬКО опубликованные артефакты
    предыдущих стадий (handoff), без рассуждений автора;
  - промежуточные результаты сохраняются на диск (возобновляемость);
  - состояние — TaskState.yaml; при прерывании перезапуск продолжает с next_action.

Провайдер подключается как callable "role prompt -> text" (provider-agnostic):
  - mock (по умолчанию): детерминированный ответ без сети — для selftest/CI;
  - anthropic: api.anthropic.com, ключ ANTHROPIC_API_KEY;
  - openai: api.openai.com, ключ OPENAI_API_KEY;
  - openai-compatible: любой OpenAI-совместимый endpoint (DeepSeek, local, GigaChat-gw…)
    через env OPENAI_COMPATIBLE_BASE_URL + OPENAI_COMPATIBLE_API_KEY + --model.
  Ключ — ТОЛЬКО из env (не в репо/логах); без ключа — честная ошибка, не тихий mock.

Использование:
  orchestrator.py run <WF> "<задача>" [child_root] [--workitem-id <id>] [--evidence <file>] [--collect-evidence] [--fresh|--resume]
  # состояние: .ai/runtime/workitems/<id>/ (по WorkItem, не по workflow — параллельные задачи не делят состояние)
        — прогон (mock-провайдер). --evidence <file>: gate-evidence по
          schemas/gate-evidence.schema.json (валидируется). --collect-evidence: вывести evidence из
          вердиктов reviewer-стадий. --fresh: начать заново; без него — resume из TaskState.
          Без evidence блокирующие гейты честно не пройдены -> status blocked.

Требует pyyaml.

Модульная структура (v3.x):
  - orchestrator_http.py      — HTTP client (_http_post_json + retry)
  - orchestrator_providers.py — Provider implementations (mock/anthropic/openai/claude-cli)
  - orchestrator_usage.py     — Usage recording (_CALL_STATS, _CALL_CONTEXT, _record_call)
  - orchestrator.py           — Workflow execution (state, core, run_workflow)
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
from ai_ops_kit.shared import _bootstrap  # noqa: E402

# ---------------- re-exports from submodules ----------------
# Backward compatibility: `import orchestrator; orchestrator.make_provider(...)` continues to work.


from ai_ops_kit.providers.orchestrator_http import _http_post_json
from ai_ops_kit.providers.orchestrator_usage import (
    _CALL_STATS, _CALL_CONTEXT, _PRICE_PER_MTOK,
    _record_call, drain_call_stats, set_call_context, clear_call_context,
)
from ai_ops_kit.providers.orchestrator_providers import (
    mock_provider, DEFAULT_MODELS, _MAX_TOKENS,
    _anthropic_call, _openai_call, _claude_cli_call,
    make_provider, make_claude_cli_provider, make_openai_provider,
    claude_binary, claude_lookup, claude_found_reason,
    for_contract,
)
from ai_ops_kit.providers.response_contract import REVIEWER_RESULT, ProviderRefusal


# ---------------- state ----------------

def load_state(run_dir: Path):
    p = run_dir / "TaskState.yaml"
    if p.exists():
        return yaml.safe_load(p.read_text(encoding="utf-8"))
    return None


def save_state(run_dir: Path, state: dict):
    (run_dir / "TaskState.yaml").write_text(
        yaml.safe_dump(state, allow_unicode=True, sort_keys=False), encoding="utf-8")


def append_interaction_log(child_root: Path, record: dict):
    """Append-only аудит действий ИИ (security-posture: audit-log). Пишет одну JSONL-запись
    в <child>/.ai/runtime/interaction-log.jsonl: кто/что/когда/итог. Секреты/сырые данные
    не пишем (только имена и статусы). Только дозапись — не перезапись."""
    from datetime import datetime, timezone
    log = child_root / ".ai" / "runtime" / "interaction-log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now(timezone.utc).isoformat(), **record}
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return log


# ---------------- core ----------------

def agent_body(agent_id: str, agents_index: dict):
    rel = (agents_index.get(agent_id) or {}).get("file")
    if rel and (PKG / rel).exists():
        return (PKG / rel).read_text(encoding="utf-8")
    return f"# {agent_id}\n(тело роли не найдено в пакете — используется контракт из registry)"


def build_role_prompt(stage, agent_id, agents_index, task_text, published):
    """Изолированный промпт роли: тело агента + задача + ТОЛЬКО опубликованные артефакты."""
    is_judge = stage.get("review_mode") == "read-only"
    pub = "\n".join(f"--- {name} ---\n{content}" for name, content in published.items()) or "(пока нет)"
    guard = ("\nВНИМАНИЕ: ты judge (read-only). Не изменяй проверяемые артефакты; "
             "верни только заключение. Тебе доступны ТОЛЬКО опубликованные артефакты ниже — "
             "рассуждения предыдущих ролей тебе не передаются.\n"
             "В КОНЦЕ ответа верни СТРУКТУРНОЕ заключение одним JSON-блоком (source of truth, "
             "не проза): {\"schema_version\":1,\"kind\":\"reviewer-result\",\"gate\":\"<gate id>\","
             "\"status\":\"pass|warn|fail\",\"checks\":[{\"id\":\"...\",\"status\":\"pass|warn|fail\"}],"
             "\"blockers\":[\"...при fail...\"]}.\n") if is_judge else ""
    return (f"{agent_body(agent_id, agents_index)}\n"
            f"{guard}\n## Задача\n{task_text}\n\n## Опубликованные артефакты\n{pub}\n")


def _write_reviewer_json(run_dir, sid, text):
    """Из ответа judge-роли достать JSON reviewer-result и, если валиден по схеме, записать
    stage-<sid>.reviewer.json (структурный источник истины). Иначе — ничего (фолбэк на markdown)."""
    # Разбор — ОДИН на весь кит (v3.37): `gates.gate_executor.extract_reviewer_json`. Прежде эта
    # эвристика жила здесь, а корпус gate-евалов мерил бы её копию — то есть мерил бы не то, что
    # решает вердикт в бою. Схему сверяем ниже, как и раньше: здесь вердикт ПРИНИМАЮТ.
    from ai_ops_kit.gates.gate_executor import extract_reviewer_json
    obj = extract_reviewer_json(text)
    if obj is None:
        return False
    try:
        # Чистая проверка ФОРМЫ вердикта живёт ВНИЗ, в пакете `checks` (слой primitives): зовём её
        # вниз, без восходящего ребра providers -> validation (лента №5). Ни sys.path, ни bootstrap
        # больше не нужны — импорт идёт по пакетному имени.
        from ai_ops_kit.checks import reviewer_result as _vrr
        if _vrr.check(obj):           # непустой список ошибок -> невалидно
            return False
    # Причина подавления (срез providers ратчета, 2026-08-12): FAIL-CLOSED и это верно. Здесь
    # решается, принять ли вердикт РЕВЬЮЕРА; если проверить его нечем — не принимаем. Тип широкий
    # намеренно: сюда попадает и недоступный валидатор, и битый ввод, и любой отказ импорта, и
    # каждый из них обязан дать «не принято», а не пропуск. Сужать нельзя: неожиданный тип стал бы
    # исключением наружу, то есть «падение вместо отказа» — хуже для writer≠judge.
    except Exception:  # noqa: BLE001 — нечем проверить -> вердикт ревьюера не принимается
        return False
    (run_dir / f"stage-{sid}.reviewer.json").write_text(
        json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def run_workflow(workflow_id: str, task_text: str, child_root: Path,
                 provider=mock_provider, verbose=True, gate_evidence=None,
                 collect=False, fresh=False, provider_name="mock", workitem_id=None,
                 budget=None, gate_ids=None, signals=None):
    wf_all = yaml.safe_load((PKG / "registry" / "workflows.yaml").read_text(encoding="utf-8"))["workflows"]
    ag = yaml.safe_load((PKG / "registry" / "agents.yaml").read_text(encoding="utf-8"))
    agents_index = {a["id"]: a for a in ag.get("agents", [])}
    if workflow_id not in wf_all:
        raise SystemExit(f"неизвестный workflow '{workflow_id}' (есть: {', '.join(wf_all)})")
    w = wf_all[workflow_id]

    # Per-WorkItem состояние (Ф0): путь по id задачи, не по workflow — иначе две задачи
    # одного workflow делят состояние. Без явного id — детерминированный из хэша задачи.
    task_hash = hashlib.sha256(task_text.encode("utf-8")).hexdigest()[:12]
    wid = workitem_id or f"wi-{task_hash}"
    run_dir = child_root / ".ai" / "runtime" / "workitems" / wid
    if fresh and run_dir.exists():        # --fresh: начать с чистого состояния (иначе — resume)
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    existing = load_state(run_dir)
    # resume-идентичность: нельзя «продолжить» чужую задачу под тем же id
    if existing and existing.get("task_hash") and existing["task_hash"] != task_hash:
        raise SystemExit(f"resume-конфликт: под '{wid}' сохранена другая задача "
                         f"(task_hash {existing['task_hash']} != {task_hash}). "
                         f"Используйте другой --workitem-id или --fresh.")
    state = existing or {
        "schema_version": 1, "task_id": wid, "workitem_id": wid, "task_hash": task_hash,
        "status": "in-progress", "workflow": workflow_id, "goal": task_text,
        "execution_mode": "sequential", "current_phase": None,
        "completed_checks": [], "artifacts": [], "next_action": w["stages"][0]["id"],
    }
    # resume-идентичность по workflow тоже (та же задача, но другой маршрут — не resume)
    if existing and existing.get("workflow") != workflow_id:
        raise SystemExit(f"resume-конфликт: под '{wid}' сохранён workflow "
                         f"{existing.get('workflow')} != {workflow_id}. Используйте --fresh.")

    # execution budget (v2.38): жёсткий потолок вызовов модели; enforcement ДО вызова
    from ai_ops_kit.shared import budget as _budget_mod
    bud = budget if isinstance(budget, _budget_mod.Budget) else _budget_mod.Budget.from_dict(budget)
    budget_exceeded = None

    stages = w["stages"]
    done_ids = {s for s in state.get("completed_checks", [])}
    published = {}  # name -> content (опубликованные артефакты)
    # восстановить опубликованное с диска (resume)
    for f in sorted(run_dir.glob("stage-*.md")):
        published[f.stem] = f.read_text(encoding="utf-8")

    for stage in stages:
        sid = stage["id"]
        if sid in done_ids:
            continue
        owner = stage.get("owner")
        state["current_phase"] = sid
        state["next_action"] = sid
        save_state(run_dir, state)

        prompt = build_role_prompt(stage, owner, agents_index, task_text, published)
        try:
            bud.charge_call()          # потолок проверяется ДО вызова — превышение = не вызываем
        except _budget_mod.BudgetExceeded as e:
            budget_exceeded = str(e)
            if verbose:
                print(f"  BUDGET: остановка перед стадией {sid}: {e}")
            break
        is_judge_stage = stage.get("review_mode") == "read-only"
        if is_judge_stage:
            # ТАМ, ГДЕ ОТВЕТ СТАНОВИТСЯ ВЕРДИКТОМ, ФОРМУ ОБЕСПЕЧИВАЕТ ПРОВАЙДЕР (C2, v3.37).
            # Где механизма нет (claude-cli, mock) — вызов идёт как раньше; режим при этом
            # записывается словом, а не остаётся умолчанием.
            judge_fn, shape = for_contract(provider, REVIEWER_RESULT)
            state.setdefault("verdict_shape", {})[sid] = {
                "mode": shape.get("mode"), "mechanism": shape.get("mechanism")}
            try:
                result = judge_fn(prompt)
            except ProviderRefusal as refusal:
                # ОТКАЗ — НЕ ВЕРДИКТ. Прежде здесь возвращалась строка «(пустой ответ модели)»:
                # она доезжала до разбора, вердикта в ней не находилось, и гейт краснел с
                # формулировкой «нет заключения reviewer» — правда по существу и ложь по причине.
                rec = refusal.as_dict()
                (run_dir / f"stage-{sid}.refusal.json").write_text(
                    json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
                result = (f"# Заключения нет\n\nСудья ({owner}) вердикта не вынес: "
                          f"{rec['reason_text']}"
                          f"{'. ' + rec['detail'] if rec['detail'] else ''}\n\n"
                          f"Это ОТКАЗ, а не «пусто»: гейт остаётся незакрытым, и причина названа.\n")
                state["verdict_shape"][sid]["refusal"] = rec
                if verbose:
                    print(f"  stage {sid} [{owner}/judge] -> ОТКАЗ: {rec['reason_text']}")
        else:
            result = provider(prompt)

        # опубликовать результат стадии (это и есть handoff-артефакт)
        out = run_dir / f"stage-{sid}.md"
        out.write_text(result, encoding="utf-8")
        published[out.stem] = result

        # judge-стадии: извлечь СТРУКТУРНОЕ reviewer-result из ответа (source of truth для гейтов,
        # не regex по прозе — finding аудита). Пишем stage-<sid>.reviewer.json только если он
        # валиден по схеме; иначе остаётся markdown-фолбэк в collect_evidence.
        if is_judge_stage and not (run_dir / f"stage-{sid}.refusal.json").exists():
            _write_reviewer_json(run_dir, sid, result)

        # handoff: judge следующей стадии увидит только published
        handoff = {
            "schema_version": 1, "from_agent": owner,
            "to_agent": stages[stages.index(stage) + 1]["owner"] if stages.index(stage) + 1 < len(stages) else None,
            "stage_from": sid,
            "published_artifacts": sorted(p.relative_to(child_root).as_posix() for p in run_dir.glob("stage-*.md")),
        }
        (run_dir / "TaskHandoff.json").write_text(
            json.dumps(handoff, indent=2, ensure_ascii=False), encoding="utf-8")

        state["completed_checks"].append(sid)
        state["artifacts"] = handoff["published_artifacts"]
        nxt = stages.index(stage) + 1
        state["next_action"] = stages[nxt]["id"] if nxt < len(stages) else None
        save_state(run_dir, state)
        if verbose:
            role = "judge" if is_judge_stage else "writer"
            print(f"  stage {sid} [{owner}/{role}] -> stage-{sid}.md")

    # gate executor: контур замыкается здесь — workflow НЕ done, пока блокирующие
    # гейты контракта не выполнены (writer ≠ judge; честный отказ вместо тихого done).
    sys.path.insert(0, str(PKG / "tools"))
    from ai_ops_kit.gates import gate_executor
    gate_ev = dict(gate_evidence or {})
    if collect:      # вывести evidence из вердиктов reviewer-стадий; явный --evidence имеет приоритет
        gate_ev = {**gate_executor.collect_evidence(workflow_id, run_dir), **gate_ev}
    gates = gate_executor.evaluate(workflow_id, gate_ev,
                                   tested_revision=state.get("tested_revision"),
                                   gate_ids=gate_ids,   # RunPlan-гейты (треки), если переданы
                                   signals=signals)     # условный human_approval по сигналам
    (run_dir / "GateReport.json").write_text(
        json.dumps(gates, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    state["current_phase"] = None
    state["gate_report"] = "GateReport.json"
    state["budget"] = bud.to_dict()
    if budget_exceeded:
        # бюджет исчерпан до завершения стадий — честный blocked с причиной
        state["status"] = "blocked"
        state["budget_exceeded"] = budget_exceeded
        state["unmet_gates"] = gates.get("unmet_gates", [])
    elif gates["blocked"]:
        state["status"] = "blocked"
        state["unmet_gates"] = gates["unmet_gates"]
    else:
        state["status"] = "done"
        state.pop("unmet_gates", None)
    save_state(run_dir, state)
    # append-only аудит-лог действия ИИ (security-posture: audit-log)
    # Ф0: НЕ писать сырой task_text (может содержать ПДн/секреты) — только id и хэш.
    append_interaction_log(child_root, {
        "workitem_id": wid, "task_hash": task_hash,
        "workflow": workflow_id, "status": state["status"],
        "unmet_gates": gates.get("unmet_gates", []), "provider": provider_name,
        "stages": len(state.get("completed_checks", [])),
        "model_calls": bud.model_calls,
        "budget_exceeded": bool(budget_exceeded)})
    if verbose:
        if gates["blocked"]:
            print(f"BLOCKED: workflow {workflow_id} прошёл {len(state['completed_checks'])} стадий, "
                  f"но блокирующие гейты не выполнены: {', '.join(gates['unmet_gates'])}. "
                  f"Отчёт гейтов: {run_dir / 'GateReport.json'}")
        else:
            print(f"OK: workflow {workflow_id} завершён sequential-режимом; "
                  f"{len(state['completed_checks'])} стадий, все блокирующие гейты выполнены; "
                  f"состояние: {run_dir / 'TaskState.yaml'}")
    return state, run_dir


def main(argv):
    if len(argv) >= 3 and argv[1] == "run":
        rest = list(argv[2:])
        collect = "--collect-evidence" in rest
        fresh = "--fresh" in rest
        # --resume — поведение по умолчанию (продолжение из TaskState); принимаем явно
        for fl in ("--collect-evidence", "--fresh", "--resume"):
            while fl in rest:
                rest.remove(fl)
        gate_evidence = None
        if "--evidence" in rest:            # JSON по schemas/gate-evidence.schema.json (валидируется)
            i = rest.index("--evidence")
            sys.path.insert(0, str(PKG / "tools"))
            from ai_ops_kit.gates import gate_executor
            gate_evidence = gate_executor.load_evidence(rest[i + 1])
            del rest[i:i + 2]
        # провайдер: mock (по умолчанию, офлайн) | anthropic | openai (живая модель по ключу env)
        prov_name, model = "mock", None
        if "--provider" in rest:
            i = rest.index("--provider"); prov_name = rest[i + 1]; del rest[i:i + 2]
        if "--model" in rest:
            i = rest.index("--model"); model = rest[i + 1]; del rest[i:i + 2]
        workitem_id = None
        if "--workitem-id" in rest:
            i = rest.index("--workitem-id"); workitem_id = rest[i + 1]; del rest[i:i + 2]
        provider = make_provider(prov_name, model)
        wf = rest[0]
        task = rest[1] if len(rest) > 1 else ""
        root = Path(rest[2]).resolve() if len(rest) > 2 else Path.cwd()
        if prov_name != "mock":
            print(f"[live] провайдер {prov_name}, модель {model or DEFAULT_MODELS.get(prov_name)} "
                  f"— реальная модель, gates принудительны.")
        run_workflow(wf, task, root, provider=provider, provider_name=prov_name,
                     gate_evidence=gate_evidence, collect=collect, fresh=fresh,
                     workitem_id=workitem_id)
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
