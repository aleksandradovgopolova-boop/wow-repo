#!/usr/bin/env python3
"""Граф зависимостей задач backlog: блокирующие, критический путь, скрытые/циклические (PR-17).

Строится из зависимостей, извлечённых классификатором (`backlog_classify`): ребро A→B означает «A
зависит от B» (B блокирует A). По графу считаются:

  * blocking — задачи, от которых зависит наибольшее число других (их разблокировка двигает многое);
  * critical_path — самая длинная цепочка зависимостей (её длина ограничивает срок снизу);
  * cycles — циклы: A зависит от B, B (через кого-то) от A — противоречие, доставить нельзя;
  * transitive — СКРЫТЫЕ зависимости: A зависит от B, B от C, но A про C не знает (импlicit);
  * dangling — ссылки на задачи вне рассматриваемого набора (закрытые/чужие/несуществующие).

Вывод объясним: у каждого пункта — из каких рёбер он получен. Живого GitHub здесь нет; на входе —
классифицированные задачи (`backlog_classify.classify_backlog(...).items`) или их dict-форма.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: F401


@dataclass
class DepGraph:
    ok: bool
    reason: str = ""
    nodes: list = field(default_factory=list)          # номера задач в наборе
    edges: list = field(default_factory=list)          # (a, b): a зависит от b
    blocking: list = field(default_factory=list)       # {number, dependents, title}
    critical_path: list = field(default_factory=list)  # [номера] — самая длинная цепочка
    cycles: list = field(default_factory=list)         # [[номера цикла]]
    transitive: list = field(default_factory=list)     # {number, hidden:[...], via}
    dangling: list = field(default_factory=list)       # {number, missing:[...]}

    def to_dict(self) -> dict:
        return asdict(self)


def _as_records(items: list) -> list:
    """Привести вход к [{number, dependencies, title}] — принимаем Classification или dict."""
    out = []
    for it in items:
        if hasattr(it, "number"):
            out.append({"number": it.number,
                        "dependencies": list(getattr(it, "dependencies", []) or []),
                        "title": getattr(it, "title", "")})
        else:
            out.append({"number": it.get("number"),
                        "dependencies": list(it.get("dependencies") or []),
                        "title": it.get("title", "")})
    return out


def build(items: list) -> DepGraph:
    recs = _as_records(items)
    nodes = [r["number"] for r in recs if r["number"] is not None]
    node_set = set(nodes)
    titles = {r["number"]: r["title"] for r in recs}
    deps = {r["number"]: [d for d in r["dependencies"]] for r in recs}

    edges, dangling = [], []
    for n in nodes:
        missing = []
        for d in deps.get(n, []):
            if d in node_set:
                edges.append((n, d))
            else:
                missing.append(d)
        if missing:
            dangling.append({"number": n, "missing": sorted(set(missing))})

    # blocking: сколько задач зависит от B (учитываем только рёбра внутри набора).
    dependents = {n: 0 for n in nodes}
    who = {n: [] for n in nodes}
    for a, b in edges:
        dependents[b] += 1
        who[b].append(a)
    blocking = sorted(
        ({"number": b, "dependents": c, "blocks": sorted(who[b]), "title": titles.get(b, "")}
         for b, c in dependents.items() if c > 0),
        key=lambda x: x["dependents"], reverse=True,
    )

    cycles = _find_cycles(nodes, deps, node_set)
    critical = [] if cycles else _longest_path(nodes, deps, node_set)
    transitive = _hidden_transitive(nodes, deps, node_set)

    return DepGraph(True, "", nodes=sorted(nodes), edges=edges, blocking=blocking,
                    critical_path=critical, cycles=cycles, transitive=transitive, dangling=dangling)


def _find_cycles(nodes, deps, node_set) -> list:
    """Все простые циклы через DFS с покраской. Возвращает список циклов (списков номеров)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in nodes}
    cycles, stack = [], []
    seen_cycles = set()

    def dfs(u):
        color[u] = GRAY
        stack.append(u)
        for v in deps.get(u, []):
            if v not in node_set:
                continue
            if color[v] == GRAY:                       # ребро в серую вершину — цикл
                i = stack.index(v)
                cyc = stack[i:]
                key = tuple(sorted(cyc))
                if key not in seen_cycles:
                    seen_cycles.add(key)
                    cycles.append(list(cyc))
            elif color[v] == WHITE:
                dfs(v)
        stack.pop()
        color[u] = BLACK

    for n in nodes:
        if color[n] == WHITE:
            dfs(n)
    return cycles


def _longest_path(nodes, deps, node_set) -> list:
    """Самая длинная цепочка зависимостей в DAG (мемоизированный DFS). Граф без циклов."""
    best_from = {}

    def walk(u):
        if u in best_from:
            return best_from[u]
        best = [u]
        for v in deps.get(u, []):
            if v in node_set:
                cand = [u] + walk(v)
                if len(cand) > len(best):
                    best = cand
        best_from[u] = best
        return best

    longest = []
    for n in nodes:
        p = walk(n)
        if len(p) > len(longest):
            longest = p
    return longest if len(longest) > 1 else []


def _hidden_transitive(nodes, deps, node_set) -> list:
    """Скрытые зависимости: транзитивно достижимые задачи, НЕ объявленные прямо у истока."""
    out = []
    for n in nodes:
        direct = {d for d in deps.get(n, []) if d in node_set}
        reach, frontier = set(), list(direct)
        while frontier:
            cur = frontier.pop()
            for d in deps.get(cur, []):
                if d in node_set and d not in reach and d != n:
                    reach.add(d)
                    frontier.append(d)
        hidden = sorted(reach - direct - {n})
        if hidden:
            out.append({"number": n, "hidden": hidden,
                        "note": "достижимы транзитивно, но прямо не объявлены"})
    return out


def graph_from_backlog(repo_or_root: str = ".", state: str = "open", client=None) -> DepGraph:
    from ai_ops_kit.planning import backlog_classify as bc
    rep = bc.classify_backlog(repo_or_root, state=state, client=client)
    if not rep.ok:
        return DepGraph(False, reason=rep.reason)
    return build(rep.items)


# ── CLI ───────────────────────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="backlog_depgraph.py")
    ap.add_argument("target", nargs="?", default=".")
    ap.add_argument("--state", default="open", choices=("open", "closed", "all"))
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])
    g = graph_from_backlog(ns.target, state=ns.state)
    if ns.json:
        print(json.dumps(g.to_dict(), ensure_ascii=False, indent=2))
        return 0 if g.ok else 2
    if not g.ok:
        print(f"НЕ проверено: {g.reason}")
        return 2
    print(f"Граф зависимостей: {len(g.nodes)} задач, {len(g.edges)} рёбер")
    if g.cycles:
        print(f"  ⚠ циклы (доставить нельзя): {g.cycles}")
    print(f"  блокирующие: " + (", ".join(f"#{b['number']}×{b['dependents']}" for b in g.blocking) or "нет"))
    print(f"  критический путь: " + (" → ".join(f"#{n}" for n in g.critical_path) or "нет"))
    for t in g.transitive:
        print(f"  скрытая: #{t['number']} → {t['hidden']}")
    for d in g.dangling:
        print(f"  ссылка вне набора: #{d['number']} → {d['missing']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
