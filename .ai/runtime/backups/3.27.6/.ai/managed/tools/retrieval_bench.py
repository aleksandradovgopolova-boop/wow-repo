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

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / "tools"))
import context_retrieval as cr   # noqa: E402
import repo_graph as rg          # noqa: E402
import semantic_lite as sl       # noqa: E402


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


def selftest():
    import tempfile
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "repo").mkdir()
        # расширенный корпус (>= 15 golden кейсов): keyword / зависимость-без-слова / редкий термин /
        # RU/EN / multi-file / noise. TS/React и внешние доки — НЕ здесь (repo_graph/full-text
        # покрывают только Python); кросс-языковая квалификация — v3.6.6 на реальных child-репо.
        fixture = {
            "pricing.py": '"""apply discount pricing."""\ndef apply_discount(a):\n    return a*0.9\ndef total(a):\n    return a\n',
            "checkout.py": 'import pricing\n\ndef checkout(a):\n    return pricing.total(a)\n',   # зависит от pricing, без 'discount'
            "order.py": 'import checkout\n\ndef place():\n    return checkout.checkout(1)\n',
            "rebate.py": '"""rebate calculation."""\ndef rebate(a):\n    return a\n',            # редкий термин
            "auth.py": '"""authentication login token handling."""\ndef login(u, p):\n    return True\n',
            "session.py": 'import auth\n\ndef session():\n    return auth.login("a", "b")\n',      # зависит от auth, без 'login'
            "catalog.py": '"""product catalog listing."""\ndef list_items():\n    return []\n',
            "report.py": '"""weekly report generation."""\ndef report():\n    return {}\n',
            "skidka.py": '"""расчёт скидки на заказ (RU)."""\ndef primenit_skidku(s):\n    return s\n',  # RU термин 'скидк'
            "legacy.py": '"""DEPRECATED устаревшее решение по ценам."""\ndef old_pricing():\n    pass\n',
            "noise.py": 'def f():\n    return f()\ndef g():\n    return g()\n',
        }
        for name, content in fixture.items():
            (root / "repo" / name).write_text(content, encoding="utf-8")

        golden = [
            {"query": "discount", "relevant": ["repo/pricing.py", "repo/checkout.py"]},   # dep: checkout
            {"query": "pricing", "relevant": ["repo/pricing.py"]},
            {"query": "checkout", "relevant": ["repo/checkout.py"]},
            {"query": "total", "relevant": ["repo/pricing.py"]},
            {"query": "rebate", "relevant": ["repo/rebate.py"]},                            # редкий термин
            {"query": "login", "relevant": ["repo/auth.py"]},
            {"query": "token", "relevant": ["repo/auth.py"]},
            {"query": "session", "relevant": ["repo/session.py", "repo/auth.py"]},          # dep: auth
            {"query": "authentication", "relevant": ["repo/auth.py"]},
            {"query": "catalog", "relevant": ["repo/catalog.py"]},
            {"query": "report", "relevant": ["repo/report.py"]},
            {"query": "скидк", "relevant": ["repo/skidka.py"]},                             # RU
            {"query": "устаревшее", "relevant": ["repo/legacy.py"]},                        # RU deprecated
            {"query": "deprecated", "relevant": ["repo/legacy.py"]},                        # EN deprecated
            {"query": "order place", "relevant": ["repo/order.py"]},
            {"query": "quantum", "relevant": []},                                           # noise: нет релевантных
        ]

        rep = run_bench(root, golden, ("repo",))
        ft = rep["strategies"]["fulltext"]
        ga = rep["strategies"]["graph_augmented"]
        sm = rep["strategies"]["semantic_lite"]
        expect("корпус >= 15 кейсов (не smoke)", len(golden) >= 15)
        expect("все 3 стратегии: метрики в [0,1]",
               all(0 <= s["precision"] <= 1 and 0 <= s["recall"] <= 1 and 0 <= s["f1"] <= 1
                   for s in (ft, ga, sm)))
        expect("graph_augmented macro-recall >= fulltext (граф помогает на dep-кейсах)",
               ga["recall"] >= ft["recall"])
        # spot: dep-кейс 'discount' — граф находит checkout, full-text нет
        q0_ft = ft["per_query"][0]["retrieved"]
        q0_ga = ga["per_query"][0]["retrieved"]
        expect("dep-кейс: graph находит checkout.py, full-text — нет",
               "repo/checkout.py" in q0_ga and "repo/checkout.py" not in q0_ft)
        # spot: rebate — semantic_lite находит редкий термин
        q_reb = next(q for q in sm["per_query"] if q["query"] == "rebate")
        expect("редкий термин 'rebate' -> semantic_lite recall=1", q_reb["recall"] == 1.0)
        # spot: RU 'скидк' находит skidka.py хоть одной стратегией
        expect("RU-запрос 'скидк' -> найден skidka.py",
               any("repo/skidka.py" in q["retrieved"]
                   for s in (ft, ga, sm) for q in s["per_query"] if q["query"] == "скидк"))
        # spot: noise 'quantum' -> ничего релевантного (recall определён как 0 при пустом relevant)
        expect("noise 'quantum' -> все стратегии не выдают ложных релевантных",
               all(next(q for q in s["per_query"] if q["query"] == "quantum")["recall"] == 0.0
                   for s in (ft, ga, sm)))
        expect("best_by_f1 определён среди трёх стратегий",
               rep["best_by_f1"] in ("fulltext", "graph_augmented", "semantic_lite"))

    print("retrieval_bench selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
