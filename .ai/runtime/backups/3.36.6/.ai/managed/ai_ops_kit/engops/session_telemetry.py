#!/usr/bin/env python3
"""session_telemetry.py (v3.16.0 Development Culture Guardrails, WP1) — машинный СНИМОК сессии:
объём/стоимость/гигиена контекста. Собирается из usage-ledger AI Ops (+ usage Claude CLI, который туда
пишется), данных WorkItem и — при наличии — runtime-значения контекста (`/context`). Всё честно помечено
usage_status: measured | estimated | unavailable. НИКОГДА не показываем unknown как 0 (критерий владельца).

ЧЕСТНЫЕ ГРАНИЦЫ (Python-движок не видит живую сессию рантайма):
  - input/output_tokens, стоимость — measured из ledger;
  - context_current/peak — ESTIMATED из input_tokens вызовов (вход вызова ≈ прочитанный контекст),
    либо measured, если рантайм передал `--context N` (из /context);
  - cache_read/write, started_at, last_compaction_at — ledger их не хранит -> unavailable (не 0);
  - turns — число ЗАПИСЕЙ (модельных вызовов) — прокси ходов, честно помечен.

CLI:  session_telemetry.py <child_root> [--workitem WID] [--session SID] [--context N] [--json]
      session_telemetry.py --selftest
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402
from ai_ops_kit.providers import usage_ledger  # noqa: E402


def _context_estimate(records):
    """context ≈ вход последнего вызова (модель читает весь контекст как input); peak = max входа.
    Оценка, не измерение — помечается estimated. Нет данных -> None (unavailable)."""
    ins = [int(r.get("input_tokens") or 0) for r in records
           if r.get("usage_status") != "unavailable" and r.get("input_tokens")]
    if not ins:
        return None, None
    return ins[-1], max(ins)


def snapshot(child_root, workitem_id=None, session_id=None, context_current=None):
    """Машинный снимок сессии. context_current (из рантайма /context) -> measured; иначе estimated.
    v3.22: opt-in чтение реальной Claude session metadata через session_telemetry_provider."""
    records = (usage_ledger.load_task(child_root, workitem_id) if workitem_id
               else usage_ledger.load_product(child_root))
    agg = usage_ledger.aggregate(records)
    ctx_cur_est, ctx_peak_est = _context_estimate(records)

    if context_current is not None:
        ctx_cur, ctx_status = int(context_current), "measured"
    elif ctx_cur_est is not None:
        ctx_cur, ctx_status = ctx_cur_est, "estimated"
    else:
        ctx_cur, ctx_status = None, "unavailable"

    # v3.22: opt-in provider — попытаться прочитать реальную Claude session metadata
    provider_data = None
    try:
        from ai_ops_kit.engops import session_telemetry_provider
        provider_data = session_telemetry_provider.read_session_metadata(session_id=session_id)
    except Exception:  # noqa: BLE001
        pass  # provider unavailable — честно продолжаем без него

    wids = sorted({r.get("workitem_id") for r in records if r.get("workitem_id")})
    # started_at: из provider (measured) или unavailable
    started_at = provider_data.get("started_at") if provider_data else None
    started_at_status = "measured" if started_at else "unavailable"

    return {
        "kind": "SessionTelemetry", "schema_version": 1,
        "session_id": session_id or (provider_data.get("session_id") if provider_data else None) or "unlabelled",
        "repository": str(Path(child_root).resolve().name),
        "workitem_id": workitem_id or (wids[0] if len(wids) == 1 else None),
        "started_at": started_at,
        "started_at_status": started_at_status,
        "turns": agg["calls"],              # прокси: число модельных вызовов
        "input_tokens": agg["input_tokens"],
        "output_tokens": agg["output_tokens"],
        "cache_read_tokens": None, "cache_write_tokens": None,   # ledger не хранит -> unavailable, не 0
        "cache_status": "unavailable",
        "context_current": ctx_cur,
        "context_peak": ctx_peak_est,
        "context_status": ctx_status,
        "estimated_cost": agg["cost"],
        "cost_complete": agg["cost_complete"],
        "usage_status": "measured" if agg["usage_unavailable"] == 0 and agg["calls"] else
                        ("unavailable" if agg["calls"] == 0 else "partial"),
        "usage_unavailable_calls": agg["usage_unavailable"],
        "last_compaction_at": None, "last_compaction_status": "unavailable",
        "tasks_in_session": wids,
        "by_role": agg["by_role"], "by_provider": agg["by_provider"],
    }


def check(s):
    """Валидация формы + ЧЕСТНОСТИ: unknown НЕ как 0 (cache/context при unavailable = None)."""
    e = []
    if not isinstance(s, dict) or s.get("kind") != "SessionTelemetry":
        return ["kind должен быть SessionTelemetry"]
    if s.get("context_status") == "unavailable" and s.get("context_current") not in (None,):
        e.append("context_status=unavailable, но context_current не None (unknown как значение)")
    if s.get("cache_status") == "unavailable" and s.get("cache_read_tokens") not in (None,):
        e.append("cache_status=unavailable, но cache_read_tokens не None (unknown как 0/значение)")
    for f in ("turns", "input_tokens", "output_tokens", "estimated_cost", "tasks_in_session"):
        if f not in s:
            e.append(f"нет поля {f}")
    return e


def _fmt(s):
    def tok(n):
        return "н/д" if n is None else (f"{n/1000:.0f}k" if n >= 1000 else str(n))
    L = [f"=== Session Telemetry — {s['repository']} / session {s['session_id']} ==="]
    ctx = s["context_current"]
    L.append(f"  контекст (тек/пик): {tok(ctx)}/{tok(s['context_peak'])} [{s['context_status']}]")
    L.append(f"  ходов (вызовов): {s['turns']}   задач в сессии: {len(s['tasks_in_session'])} "
             f"{s['tasks_in_session'] or ''}")
    L.append(f"  токены вх/вых: {tok(s['input_tokens'])}/{tok(s['output_tokens'])}   "
             f"cache: {s['cache_status']}")
    L.append(f"  стоимость: ${s['estimated_cost']:.4f} "
             + ("(полная)" if s["cost_complete"] else "(НЕПОЛНАЯ — есть unavailable)"))
    if s["usage_unavailable_calls"]:
        L.append(f"  ⚠ вызовов с неизвестным usage: {s['usage_unavailable_calls']} (не считаны как 0)")
    return "\n".join(L)


def main(argv):
    wid = sid = ctx = None
    args, it = [], iter(argv)
    for a in it:
        if a == "--workitem":
            wid = next(it, None)
        elif a == "--session":
            sid = next(it, None)
        elif a == "--context":
            nxt = next(it, None); ctx = int(nxt) if nxt and nxt.isdigit() else None
        elif not a.startswith("--"):
            args.append(a)
    root = args[0] if args else "."
    s = snapshot(root, workitem_id=wid, session_id=sid, context_current=ctx)
    print(json.dumps(s, ensure_ascii=False, indent=2) if "--json" in argv else _fmt(s))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
