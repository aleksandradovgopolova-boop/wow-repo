#!/usr/bin/env python3
"""semantic_lite.py (v3.6.3) — детерминированный semantic-fallback (TF-IDF/cosine), БЕЗ vector-DB.

Следующая ступень retrieval-цепочки ContextArchitectureDecision после full-text: лексический
semantic fallback на классическом sparse TF-IDF (offline, stdlib math) — НЕ эмбеддинги, НЕ vector-DB,
НЕ внешний индекс (по явной границе владельца «не начинаем с векторной базы»). Ценность над сырым
full-text: понижает вес частых токенов и повышает редкие -> лучше ранжирование/precision, чем
подсчёт голых вхождений.

CLI:  semantic_lite.py <root> --query "..." [--k N] [--json] | --selftest
"""
from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
_TOK = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")


def _tokens(text: str):
    return [t.lower() for t in _TOK.findall(text)]


def build_index(root=PKG, subdirs=("ai_ops_kit",), path_filter=None) -> dict:
    root = Path(root)
    docs = {}
    for sd in subdirs:
        d = root / sd
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*.py")):
            rel = str(f.relative_to(root))
            # v3.7.0: access pre-filter — denied-путь НЕ индексируется/не читается
            if path_filter is not None and not path_filter(rel):
                continue
            try:
                docs[rel] = Counter(_tokens(f.read_text(encoding="utf-8")))
            except OSError:
                continue
    n = len(docs) or 1
    df = Counter()
    for c in docs.values():
        for tok in c:
            df[tok] += 1
    idf = {tok: math.log((n + 1) / (df[tok] + 1)) + 1 for tok in df}
    default_idf = math.log(n + 1) + 1
    tfidf = {}
    for rel, c in docs.items():
        tfidf[rel] = {tok: (1 + math.log(cnt)) * idf[tok] for tok, cnt in c.items()}
    return {"tfidf": tfidf, "idf": idf, "default_idf": default_idf, "n": n}


def _cosine(qv: dict, dv: dict) -> float:
    if not qv or not dv:
        return 0.0
    dot = sum(w * dv.get(t, 0.0) for t, w in qv.items())
    if dot == 0:
        return 0.0
    nq = math.sqrt(sum(w * w for w in qv.values()))
    nd = math.sqrt(sum(w * w for w in dv.values()))
    return dot / (nq * nd) if nq and nd else 0.0


def search(index: dict, query: str, k: int = 5):
    qc = Counter(_tokens(query))
    qv = {t: (1 + math.log(c)) * index["idf"].get(t, index["default_idf"]) for t, c in qc.items()}
    scored = [(rel, _cosine(qv, dv)) for rel, dv in index["tfidf"].items()]
    scored = [(r, s) for r, s in scored if s > 0]
    scored.sort(key=lambda x: (-x[1], x[0]))
    return [{"file": r, "score": round(s, 4)} for r, s in scored[:k]]


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    root = args[0]
    query = argv[argv.index("--query") + 1] if "--query" in argv else ""
    k = int(argv[argv.index("--k") + 1]) if "--k" in argv else 5
    res = search(build_index(root), query, k)
    if "--json" in argv:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"SEMANTIC-LITE query='{query}' -> {[r['file'] for r in res]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
