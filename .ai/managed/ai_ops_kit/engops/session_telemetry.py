#!/usr/bin/env python3
"""session_telemetry.py (v3.16.0 Development Culture Guardrails, WP1) — машинный СНИМОК сессии:
объём/стоимость/гигиена контекста. Собирается из ТРАНСКРИПТА живой сессии рантайма (measured),
usage-ledger AI Ops, данных WorkItem и — при наличии — runtime-значения контекста (`/context`).
Всё честно помечено статусом: measured | estimated | unavailable. НИКОГДА не показываем unknown
как 0 (критерий владельца).

ОТКУДА ЧИСЛА (по приоритету для контекста):
  1. `--context N` из `/context` рантайма                                   -> measured;
  2. транскрипт сессии Claude Code (`session_telemetry_provider`)           -> measured;
  3. input_tokens вызовов кита из ledger (вход вызова ≈ прочитанный контекст) -> ESTIMATED;
  4. нет ничего                                                            -> unavailable (не 0).

ЧТО ИЗМЕНИЛОСЬ 2026-08-13. Пункт 2 существовал, но не работал: провайдер искал транскрипт по
несуществующему пути (см. его docstring), а снимок брал из его ответа ТОЛЬКО `started_at`. Поэтому
контекст всегда был estimated по ledger'у — а в ledger лежат вызовы моделей самого кита, а не ходы
сессии Claude Code, то есть число не имело отношения к тому, сколько сессия реально читает. Теперь
контекст, кэш, ходы, компакция и суммарный расход сессии приходят измеренными, а `by_role`/стоимость
по-прежнему считаются по ledger — это разные вещи, и они не смешиваются.

ГРАНИЦА: `input_tokens`/`output_tokens`/`estimated_cost` — это расход МОДЕЛЬНЫХ ВЫЗОВОВ КИТА из
ledger. Расход самой сессии рантайма лежит отдельно: `session_total_tokens` и блок
`session_runtime`. Складывать их нельзя — это разные счётчики.

CLI:  session_telemetry.py <child_root> [--workitem WID] [--session SID] [--context N] [--json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402
from ai_ops_kit.shared import usage_ledger  # noqa: E402


def _context_estimate(records):
    """context ≈ вход последнего вызова (модель читает весь контекст как input); peak = max входа.
    Оценка, не измерение — помечается estimated. Нет данных -> None (unavailable)."""
    ins = [int(r.get("input_tokens") or 0) for r in records
           if r.get("usage_status") != "unavailable" and r.get("input_tokens")]
    if not ins:
        return None, None
    return ins[-1], max(ins)


def _read_runtime_session(child_root, session_id):
    """Измеренный расход живой сессии или None. Провайдер opt-in: нет транскрипта — нет чисел.

    Каталог репозитория передаётся ОБЯЗАТЕЛЬНО: без него провайдер не знает, какая сессия наша, а
    угадывать нельзя — прежняя версия брала первый найденный проект и подставляла чужие числа.
    """
    try:
        from ai_ops_kit.engops import session_telemetry_provider as _p
        data = _p.read_session_metadata(session_id=session_id, project_dir=str(child_root))
        # «Не нашли» обязано отличаться от «сессия не идёт»: причина называется словами.
        return data or {"unavailable_reason": _p.lookup_reason(session_id, str(child_root))}
    # Причина подавления (срез ратчета, 2026-08-12): провайдер телеметрии — OPT-IN, его отсутствие
    # штатно. Утраты данных нет: ниже статус контекста остаётся `estimated`/`unavailable`, а не
    # подменяется нулём — тот самый инвариант `unavailable != 0`.
    except Exception as exc:  # noqa: BLE001 — opt-in провайдер отсутствует; статус остаётся честным
        return {"unavailable_reason": f"{type(exc).__name__}: {exc}"}


def snapshot(child_root, workitem_id=None, session_id=None, context_current=None):
    """Машинный снимок сессии.

    Контекст: `/context` рантайма -> measured; транскрипт живой сессии -> measured;
    ledger -> estimated; ничего -> unavailable (None, НЕ 0).
    """
    records = (usage_ledger.load_task(child_root, workitem_id) if workitem_id
               else usage_ledger.load_product(child_root))
    agg = usage_ledger.aggregate(records)
    ctx_cur_est, ctx_peak_est = _context_estimate(records)

    rt = _read_runtime_session(child_root, session_id) or {}
    rt_measured = rt.get("context_status") == "measured"

    if context_current is not None:
        ctx_cur, ctx_peak, ctx_status = int(context_current), ctx_peak_est, "measured"
        ctx_source = "runtime-context-command"
    elif rt_measured:
        ctx_cur, ctx_peak, ctx_status = rt["context_current"], rt.get("context_peak"), "measured"
        ctx_source = "session-transcript"
    elif ctx_cur_est is not None:
        ctx_cur, ctx_peak, ctx_status = ctx_cur_est, ctx_peak_est, "estimated"
        ctx_source = "usage-ledger"
    else:
        ctx_cur, ctx_peak, ctx_status = None, None, "unavailable"
        ctx_source = None

    wids = sorted({r.get("workitem_id") for r in records if r.get("workitem_id")})
    started_at = rt.get("started_at")
    cache_measured = rt.get("cache_status") == "measured"

    return {
        "kind": "SessionTelemetry", "schema_version": 1,
        "session_id": session_id or rt.get("session_id") or "unlabelled",
        "repository": str(Path(child_root).resolve().name),
        "workitem_id": workitem_id or (wids[0] if len(wids) == 1 else None),
        "started_at": started_at,
        "started_at_status": "measured" if started_at else "unavailable",
        # Ходы сессии — измеренные, если транскрипт прочитан; иначе прокси по модельным вызовам кита.
        "turns": rt["turns"] if rt_measured else agg["calls"],
        "turns_source": "session-transcript" if rt_measured else "usage-ledger",
        # Расход МОДЕЛЬНЫХ ВЫЗОВОВ КИТА (ledger) — не путать с расходом сессии рантайма ниже.
        "input_tokens": agg["input_tokens"],
        "output_tokens": agg["output_tokens"],
        "cache_read_tokens": rt.get("cache_read_tokens") if cache_measured else None,
        "cache_write_tokens": rt.get("cache_write_tokens") if cache_measured else None,
        "cache_status": "measured" if cache_measured else "unavailable",
        "context_current": ctx_cur,
        "context_peak": ctx_peak,
        "context_status": ctx_status,
        "context_source": ctx_source,
        # Расход САМОЙ СЕССИИ рантайма: суммарные оплачиваемые токены всех её ходов.
        "session_total_tokens": rt.get("total_tokens"),
        "session_tokens_status": rt.get("total_tokens_status") or "unavailable",
        "session_source": rt.get("source"),
        "session_unavailable_reason": rt.get("unavailable_reason"),
        "session_runtime": rt or None,
        "estimated_cost": agg["cost"],
        "cost_complete": agg["cost_complete"],
        "usage_status": "measured" if agg["usage_unavailable"] == 0 and agg["calls"] else
                        ("unavailable" if agg["calls"] == 0 else "partial"),
        "usage_unavailable_calls": agg["usage_unavailable"],
        "last_compaction_at": rt.get("last_compaction_at"),
        "last_compaction_status": rt.get("last_compaction_status") or "unavailable",
        "compactions": rt.get("compactions") if rt else None,
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
    if s.get("context_status") == "measured" and s.get("context_current") is None:
        e.append("context_status=measured, но context_current отсутствует")
    if s.get("session_tokens_status") == "unavailable" and s.get("session_total_tokens") is not None:
        e.append("session_tokens_status=unavailable, но session_total_tokens не None")
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
    L.append(f"  ходов: {s['turns']} [{s.get('turns_source')}]   задач в сессии: "
             f"{len(s['tasks_in_session'])} {s['tasks_in_session'] or ''}")
    L.append(f"  расход сессии всего: {tok(s.get('session_total_tokens'))} токенов "
             f"[{s.get('session_tokens_status')}]")
    L.append(f"  токены вх/вых (вызовы кита): {tok(s['input_tokens'])}/{tok(s['output_tokens'])}   "
             f"cache чт/зп: {tok(s.get('cache_read_tokens'))}/{tok(s.get('cache_write_tokens'))} "
             f"[{s['cache_status']}]")
    if s.get("last_compaction_at"):
        L.append(f"  последняя компакция: {s['last_compaction_at']} (всего {s.get('compactions')})")
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
