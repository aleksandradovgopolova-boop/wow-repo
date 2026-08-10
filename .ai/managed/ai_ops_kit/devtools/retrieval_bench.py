#!/usr/bin/env python3
"""retrieval_bench.py (v3.6.2) — оффлайн golden-корпус precision/recall retrieval-стратегий.

Закрывает research-gap FL-003 / RR-009 (сравнить retrieval-стратегии под бюджет) — но ЧЕСТНО:
измеряет то, что уже реализовано БЕЗ vector-DB — full-text vs graph-augmented (full-text +
соседи по Repository Graph: зависимости и зависимые). Семантическая стратегия появится в цепочке
позже (semantic fallback) и войдёт в тот же Bench.

Метрики на golden-корпусе (query -> known relevant files): precision/recall/F1 (macro-avg).
Гипотеза (FL-003): граф-дополнение поднимает recall на релевантных-по-зависимости файлах, у которых
мало ключевых слов, не разрушая precision.

CLI:  retrieval_bench.py --selftest
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402
from ai_ops_kit.context import context_retrieval as cr   # noqa: E402
from ai_ops_kit.context import repo_graph as rg          # noqa: E402
from ai_ops_kit.context import semantic_lite as sl       # noqa: E402


def _fulltext(root, query, subdirs, k=3):
    return [r["file"] for r in cr.full_text_search(root, query, subdirs)[:k]]


def _semantic_lite(root, query, subdirs, k=3):
    return [r["file"] for r in sl.search(sl.build_index(root, subdirs), query, k)]


def _graph_augmented(root, query, subdirs, k=3):
    ft = _fulltext(root, query, subdirs, k)
    if not ft:
        return []
    g = rg.build_graph(root, subdirs)
    top = ft[0]
    neighbors = set(rg.impact(g, top)) | set(g.get("import_edges", {}).get(top, []))
    return ft + [n for n in sorted(neighbors) if n not in ft]


def _metrics(retrieved, relevant):
    ret, rel = set(retrieved), set(relevant)
    tp = len(ret & rel)
    precision = tp / len(ret) if ret else 0.0
    recall = tp / len(rel) if rel else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {"precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3)}


def run_bench(root, golden, subdirs):
    strategies = {"fulltext": _fulltext, "graph_augmented": _graph_augmented,
                  "semantic_lite": _semantic_lite}
    report = {}
    for name, fn in strategies.items():
        per_q, ps, rs, fs = [], [], [], []
        for case in golden:
            retrieved = fn(root, case["query"], subdirs)
            m = _metrics(retrieved, case["relevant"])
            per_q.append({"query": case["query"], **m, "retrieved": retrieved})
            ps.append(m["precision"]); rs.append(m["recall"]); fs.append(m["f1"])
        n = len(golden) or 1
        report[name] = {"precision": round(sum(ps) / n, 3), "recall": round(sum(rs) / n, 3),
                        "f1": round(sum(fs) / n, 3), "per_query": per_q}
    best = max(report, key=lambda s: report[s]["f1"])
    return {"kind": "retrieval-bench", "strategies": report, "best_by_f1": best}


def main(argv):
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
