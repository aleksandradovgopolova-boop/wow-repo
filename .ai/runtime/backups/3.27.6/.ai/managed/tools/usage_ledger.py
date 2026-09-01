#!/usr/bin/env python3
"""usage_ledger.py (v3.10.0 Usage Truth) — ЧЕСТНЫЙ учёт ВСЕХ модельных/runtime-вызовов.

Каждый вызов модели (writer/reviewer/fix-loop/fallback/escalation/parallel-пакеты) пишет UsageRecord.
Источник записей — orchestrator._record_call (+ _CALL_CONTEXT, который ставит ai_ops_run per-вызов).
Здесь — персист (append-only JSONL), агрегация по ЗАДАЧЕ и по ПРОДУКТУ, и валидатор ЧЕСТНОСТИ:
неизвестное использование = usage_status:unavailable с input/output=None (НИКОГДА 0), не «бесплатный» вызов.

UsageRecord:
  run_id, workitem_id, package_id, role, provider, model, runtime,
  input_tokens, output_tokens, usage_status (measured|estimated|unavailable),
  cost, cost_status (measured|estimated|unavailable), latency, trigger (initial|retry|review|escalation|fallback)

Ledger:
  features/<wid>/usage-ledger.jsonl   — по задаче (append-only)
  .ai/usage/product-ledger.jsonl      — агрегат по всему child-продукту (append-only)

CLI:  usage_ledger.py <child_root> [--workitem <wid>] [--json]   # показать стоимость задачи/продукта
      usage_ledger.py --selftest
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

FIELDS = ("run_id", "workitem_id", "package_id", "role", "provider", "model", "runtime",
          "input_tokens", "output_tokens", "usage_status", "cost", "cost_status", "latency", "trigger",
          # v3.24.0 Cost & Architecture Accuracy — контекстные поля для economic alternatives и cost-per-decision
          "task_type", "workflow", "risk", "size", "writer_tier", "execution_mode", "stack", "architecture_impact")
USAGE_STATUS = ("measured", "estimated", "unavailable")
TRIGGERS = ("initial", "retry", "review", "escalation", "fallback", "reevaluate")


def check(rec):
    """Валидация одной UsageRecord + ЧЕСТНОСТЬ. -> список ошибок."""
    e = []
    if not isinstance(rec, dict):
        return ["record не объект"]
    us = rec.get("usage_status")
    if us not in USAGE_STATUS:
        e.append(f"usage_status ∉ {USAGE_STATUS} (got {us!r})")
    it, ot = rec.get("input_tokens"), rec.get("output_tokens")
    # ЧЕСТНОСТЬ #1: unavailable -> токены НЕ заполнены (None), НЕ 0 (0 = «точно ноль», это ложь для неизвестного).
    if us == "unavailable" and (it is not None or ot is not None):
        e.append("usage_status=unavailable, но input/output_tokens заданы (неизвестное != 0; должно быть None)")
    # ЧЕСТНОСТЬ #2: measured -> хотя бы один счётчик токенов присутствует.
    if us == "measured" and it is None and ot is None:
        e.append("usage_status=measured, но токенов нет (нечего измерять)")
    for k in ("input_tokens", "output_tokens"):
        v = rec.get(k)
        if v is not None and not (isinstance(v, int) and v >= 0):
            e.append(f"{k} должен быть неотрицательным int или None (got {v!r})")
    cs = rec.get("cost_status")
    if cs is not None and cs not in USAGE_STATUS:
        e.append(f"cost_status ∉ {USAGE_STATUS} (got {cs!r})")
    if rec.get("cost") is None and cs == "measured":
        e.append("cost_status=measured, но cost=None")
    tr = rec.get("trigger")
    if tr is not None and tr not in TRIGGERS:
        e.append(f"trigger ∉ {TRIGGERS} (got {tr!r})")
    return e


def normalize(rec, run_id=None, workitem_id=None):
    """Привести сырую запись _record_call к UsageRecord (заполнить недостающие ключи None, влить контекст)."""
    out = {k: rec.get(k) for k in FIELDS}
    if out.get("run_id") is None:
        out["run_id"] = run_id
    if out.get("workitem_id") is None:
        out["workitem_id"] = workitem_id
    # cost может прийти как cost_usd_est (назад-совместимость)
    if out.get("cost") is None and rec.get("cost_usd_est") is not None:
        out["cost"] = rec.get("cost_usd_est")
    return out


def _task_path(child_root, wid):
    return Path(child_root) / "features" / str(wid) / "usage-ledger.jsonl"


def _product_path(child_root):
    return Path(child_root) / ".ai" / "usage" / "product-ledger.jsonl"


def append(child_root, workitem_id, records, run_id=None, extra_context=None):
    """Записать UsageRecords в ledger задачи И продукта (append-only). Возвращает число записанных.
    v3.24.0: extra_context — dict дополнительных полей (task_type, workflow, stack, risk, size, writer_tier,
    execution_mode, architecture_impact), которые штампуется на ВСЕ записи (если в записи поле None)."""
    recs = [normalize(r, run_id=run_id, workitem_id=workitem_id) for r in (records or [])]
    # v3.24.0: штампуем extra_context на записи, где поля ещё не заполнены
    if extra_context:
        for rec in recs:
            for k, v in extra_context.items():
                if k in FIELDS and rec.get(k) is None and v is not None:
                    rec[k] = v
    if not recs:
        return 0
    for p in (_task_path(child_root, workitem_id), _product_path(child_root)):
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(recs)


def _load(path):
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    return out


def load_task(child_root, wid):
    return _load(_task_path(child_root, wid))


def load_product(child_root):
    return _load(_product_path(child_root))


def aggregate(records):
    """Агрегат ЧЕСТНО: суммы токенов/стоимости ТОЛЬКО по записям с данными; unavailable считается отдельно
    (не как 0). -> {calls, measured/estimated/unavailable counts, input/output_tokens, cost,
    cost_complete (нет ли unavailable-стоимости), by_role/by_trigger/by_provider/by_task_type/by_workflow/by_writer_tier}."""
    agg = {"calls": 0, "usage_measured": 0, "usage_unavailable": 0,
           "input_tokens": 0, "output_tokens": 0, "cost": 0.0,
           "cost_measured": 0, "cost_estimated": 0, "cost_unavailable": 0,
           "by_role": {}, "by_trigger": {}, "by_provider": {},
           # v3.24.0: новые измерения для economic alternatives и cost-per-decision
           "by_task_type": {}, "by_workflow": {}, "by_writer_tier": {}}
    for r in records or []:
        agg["calls"] += 1
        us = r.get("usage_status")
        if us == "unavailable":
            agg["usage_unavailable"] += 1
        else:
            agg["usage_measured"] += 1
            agg["input_tokens"] += int(r.get("input_tokens") or 0)
            agg["output_tokens"] += int(r.get("output_tokens") or 0)
        cs = r.get("cost_status")
        if cs == "measured":
            agg["cost_measured"] += 1
        elif cs == "estimated":
            agg["cost_estimated"] += 1
        else:
            agg["cost_unavailable"] += 1
        if r.get("cost") is not None:
            agg["cost"] += float(r["cost"])
        for dim, key in (("by_role", "role"), ("by_trigger", "trigger"), ("by_provider", "provider"),
                         ("by_task_type", "task_type"), ("by_workflow", "workflow"),
                         ("by_writer_tier", "writer_tier")):
            k = r.get(key) or "unknown"
            agg[dim][k] = agg[dim].get(k, 0) + 1
    agg["cost"] = round(agg["cost"], 6)
    # ЧЕСТНОСТЬ: стоимость ПОЛНАЯ только если ни одного вызова с неизвестной стоимостью.
    agg["cost_complete"] = agg["cost_unavailable"] == 0
    return agg


def merge_ledgers(child_root, source_roots):
    """v3.24.0 Parallel Ledger Fan-In: свести usage-ledger из нескольких клонов (source_roots) в основной
    child_root. Читает features/*/usage-ledger.jsonl и .ai/usage/product-ledger.jsonl из каждого клона,
    дедуплицирует по (run_id, role, latency, input_tokens, output_tokens), дозаписывает в основной.
    Возвращает число добавленных записей."""
    import json as _json
    added = 0
    seen = set()
    # Загружаем существующие записи из основного для дедупликации
    main_product = _product_path(child_root)
    if main_product.exists():
        for rec in _load(main_product):
            key = (rec.get("run_id"), rec.get("role"), rec.get("input_tokens"), rec.get("output_tokens"), rec.get("latency"))
            seen.add(key)
    # Собираем из клонов
    for src in source_roots:
        src_path = Path(src)
        # Product ledger
        src_product = src_path / ".ai" / "usage" / "product-ledger.jsonl"
        if src_product.exists():
            for rec in _load(src_product):
                key = (rec.get("run_id"), rec.get("role"), rec.get("input_tokens"), rec.get("output_tokens"), rec.get("latency"))
                if key not in seen:
                    seen.add(key)
                    main_product.parent.mkdir(parents=True, exist_ok=True)
                    with main_product.open("a", encoding="utf-8") as f:
                        f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
                    added += 1
        # Task ledgers (features/*/usage-ledger.jsonl)
        for task_ledger in src_path.glob("features/*/usage-ledger.jsonl"):
            wid = task_ledger.parent.name
            main_task = _task_path(child_root, wid)
            main_task.parent.mkdir(parents=True, exist_ok=True)
            task_seen = set()
            if main_task.exists():
                for rec in _load(main_task):
                    key = (rec.get("run_id"), rec.get("role"), rec.get("input_tokens"))
                    task_seen.add(key)
            for rec in _load(task_ledger):
                key = (rec.get("run_id"), rec.get("role"), rec.get("input_tokens"))
                if key not in task_seen:
                    task_seen.add(key)
                    with main_task.open("a", encoding="utf-8") as f:
                        f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    return added


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    good = {"run_id": "r1", "workitem_id": "w1", "role": "implementation", "provider": "claude-cli",
            "model": "claude-code-local", "input_tokens": 100, "output_tokens": 50,
            "usage_status": "measured", "cost": 0.07, "cost_status": "measured", "trigger": "initial"}
    expect("валидная measured-запись -> без ошибок", check(good) == [])
    expect("unavailable с токенами -> ошибка (неизвестное != 0)",
           any("unavailable" in x for x in check({**good, "usage_status": "unavailable"})))
    expect("unavailable с None-токенами -> ok",
           check({**good, "usage_status": "unavailable", "input_tokens": None, "output_tokens": None,
                  "cost": None, "cost_status": "unavailable"}) == [])
    expect("measured без токенов -> ошибка",
           any("нечего измерять" in x for x in check({**good, "input_tokens": None, "output_tokens": None})))
    expect("input_tokens=0 при measured -> валиден (0 измерено — не то же, что неизвестно)",
           check({**good, "input_tokens": 0, "output_tokens": 1}) == [])
    expect("неизвестный trigger -> ошибка", any("trigger" in x for x in check({**good, "trigger": "bogus"})))

    # агрегат честный: unavailable не топит суммы и помечает cost неполной
    recs = [good,
            {**good, "role": "code_review", "provider": "deepseek", "trigger": "review", "cost": 0.001,
             "cost_status": "estimated"},
            {"usage_status": "unavailable", "input_tokens": None, "output_tokens": None, "cost": None,
             "cost_status": "unavailable", "role": "implementation", "provider": "kimi", "trigger": "escalation"}]
    a = aggregate(recs)
    expect("агрегат: 3 вызова, 2 measured + 1 unavailable",
           a["calls"] == 3 and a["usage_measured"] == 2 and a["usage_unavailable"] == 1)
    expect("агрегат: токены только по measured (200/100)",
           a["input_tokens"] == 200 and a["output_tokens"] == 100)
    expect("агрегат: cost неполон (есть unavailable-стоимость)", a["cost_complete"] is False)
    expect("агрегат: by_role/by_trigger разложены",
           a["by_role"]["implementation"] == 2 and a["by_trigger"]["review"] == 1)

    # round-trip append/load/aggregate
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        n = append(td, "w1", recs, run_id="r1")
        expect("append записал 3", n == 3)
        expect("load_task читает 3", len(load_task(td, "w1")) == 3)
        expect("load_product читает 3 (агрегат по продукту)", len(load_product(td)) == 3)
        expect("нормализация влила run_id/workitem_id", load_task(td, "w1")[0].get("run_id") == "r1")

    print("usage_ledger selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _fmt_agg(title, a):
    lines = [f"{title}: вызовов {a['calls']} (measured {a['usage_measured']}, unavailable {a['usage_unavailable']})"]
    lines.append("  токены (measured): in=%d out=%d" % (a["input_tokens"], a["output_tokens"]))
    _c = "$%.4f" % a["cost"]
    lines.append("  стоимость: %s%s" % (_c, "" if a["cost_complete"] else "  ⚠ НЕПОЛНАЯ (есть вызовы с неизвестной стоимостью)"))
    lines.append("  cost-статусы: measured %d / estimated %d / unavailable %d" %
                 (a["cost_measured"], a["cost_estimated"], a["cost_unavailable"]))
    lines.append("  по ролям: " + ", ".join("%s=%d" % kv for kv in sorted(a["by_role"].items())))
    lines.append("  по триггерам: " + ", ".join("%s=%d" % kv for kv in sorted(a["by_trigger"].items())))
    lines.append("  по провайдерам: " + ", ".join("%s=%d" % kv for kv in sorted(a["by_provider"].items())))
    return "\n".join(lines)


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print("usage: usage_ledger.py <child_root> [--workitem <wid>] [--json]"); return 2
    child_root = args[0]
    wid = None
    if "--workitem" in argv:
        wid = argv[argv.index("--workitem") + 1]
    prod = load_product(child_root)
    out = {"product": aggregate(prod)}
    if wid:
        out["task"] = aggregate(load_task(child_root, wid))
    if "--json" in argv:
        print(json.dumps(out, ensure_ascii=False, indent=2)); return 0
    if wid:
        print(_fmt_agg(f"ЗАДАЧА {wid}", out["task"]))
        print()
    print(_fmt_agg("ПРОДУКТ (все задачи)", out["product"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
