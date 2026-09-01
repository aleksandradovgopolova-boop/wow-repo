#!/usr/bin/env python3
"""Usage recording for orchestrator — call stats accumulator, context, pricing.

Extracted from orchestrator.py. Global state (_CALL_STATS, _CALL_CONTEXT) lives here;
orchestrator.py re-exports for backward compatibility.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
# v3.1 (trace v0.2): аккумулятор статистики вызовов модели — tokens/latency/cost для трейсе. Вызывающий
# (ai_ops_run) дренирует после прогона и эмитит в journal + отчёт. Наблюдаемость, не источник истины.
_CALL_STATS = []
# Приблизительные цены $/1M токенов (только ОЦЕНКА cost; при отсутствии модели cost_usd_est=None).
_PRICE_PER_MTOK = {
    "claude-sonnet-5": {"in": 3.0, "out": 15.0},
    "deepseek-chat": {"in": 0.27, "out": 1.10},
}


def drain_call_stats():
    """Забрать и очистить накопленную статистику вызовов модели (per-call {model,in,out,latency_s,cost})."""
    global _CALL_STATS
    s, _CALL_STATS = _CALL_STATS, []
    return s


# v3.10.0 Usage Truth: контекст вызова (role/trigger/provider/runtime/run_id/workitem_id/package_id).
# ai_ops_run ставит его через provider-обёртку ПЕРЕД вызовом; _record_call вмерживает в каждую запись.
_CALL_CONTEXT = {}


def set_call_context(**ctx):
    _CALL_CONTEXT.update({k: v for k, v in ctx.items() if v is not None})


def clear_call_context():
    _CALL_CONTEXT.clear()


def _record_call(model, in_tok, out_tok, latency_s, provider=None, cost=None, cost_status=None):
    # v3.10.0 Usage Truth. cost: ИЗМЕРЕННАЯ стоимость (напр. claude -p total_cost_usd). Иначе оценка по
    # прайс-таблице, если есть токены+цена (cost_status=estimated); иначе None (unavailable).
    if cost is not None:
        cost_status = cost_status or "measured"
    else:
        price = _PRICE_PER_MTOK.get(model)
        if price and in_tok is not None and out_tok is not None:
            cost = round(in_tok / 1e6 * price["in"] + out_tok / 1e6 * price["out"], 6)
            cost_status = "estimated"
        else:
            cost_status = "unavailable"
    # ЧЕСТНО: неизвестное использование -> unavailable, НЕ 0. measured, если провайдер вернул токены.
    usage_status = "measured" if (in_tok is not None or out_tok is not None) else "unavailable"
    rec = {"model": model, "provider": provider, "runtime": None,
           "input_tokens": in_tok, "output_tokens": out_tok, "usage_status": usage_status,
           "cost": cost, "cost_status": cost_status,
           "latency": (round(latency_s, 3) if latency_s is not None else None),
           "cost_usd_est": cost}   # cost_usd_est — назад-совместимо (budget/cost_account/trace)
    rec.update(_CALL_CONTEXT)      # role/trigger/run_id/workitem_id/package_id/runtime/provider
    _CALL_STATS.append(rec)


def main(argv):
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
