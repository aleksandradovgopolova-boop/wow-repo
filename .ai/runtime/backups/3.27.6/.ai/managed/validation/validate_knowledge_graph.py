#!/usr/bin/env python3
"""Валидатор Knowledge Graph (Ф4 roadmap, v2.0).

Словарь типов и связей — registry/entities.yaml; граф проекта — knowledge/graph.yaml
(schemas/knowledge-graph.schema.json). Ловит:
  1. невалидный YAML / не тот kind / нет обязательных полей;
  2. узел с типом вне словаря; дубликат id узла;
  3. ребро с несуществующим концом (dangling reference);
  4. ребро со связью (from-type, relation, to-type) вне словаря relations;
  5. узел type=feature с атрибутом blueprint, указывающим на несуществующий файл.

Использование:  python3 validation/validate_knowledge_graph.py <graph.yaml> [...]
                python3 validation/validate_knowledge_graph.py --selftest
Возврат 0 — чисто, 1 — есть ошибки. Требует pyyaml.
"""

import sys
import tempfile
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
ENTITIES = PKG / "registry" / "entities.yaml"


def load_dictionary():
    data = yaml.safe_load(ENTITIES.read_text(encoding="utf-8"))
    types = set((data.get("entity_types") or {}).keys())
    rels = {(r["from"], r["type"], r["to"]) for r in data.get("relations") or []}
    return types, rels


def validate_graph(graph_path: Path, types, rels):
    errors = []

    def fail(msg):
        errors.append(f"{graph_path.name}: {msg}")

    try:
        g = yaml.safe_load(graph_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [f"{graph_path.name}: не читается/невалидный YAML: {exc}"]
    if not isinstance(g, dict) or g.get("kind") != "knowledge-graph":
        fail(f"kind '{(g or {}).get('kind')}' != knowledge-graph")
        return errors
    if g.get("schema_version") != 1:
        fail(f"schema_version '{g.get('schema_version')}' != 1")

    nodes = {}
    for n in g.get("nodes") or []:
        if not isinstance(n, dict) or not n.get("id") or not n.get("type"):
            fail(f"узел без id/type: {n}")
            continue
        if n["id"] in nodes:
            fail(f"дубликат узла '{n['id']}'")
        if n["type"] not in types:
            fail(f"узел '{n['id']}': тип '{n['type']}' вне registry/entities.yaml")
        nodes[n["id"]] = n
        if n["type"] == "feature" and n.get("blueprint"):
            if not (graph_path.parent / n["blueprint"]).exists():
                fail(f"узел '{n['id']}': blueprint '{n['blueprint']}' не существует "
                     f"(путь относительно {graph_path.parent})")
    if not nodes:
        fail("нет непустого nodes")
        return errors

    for e in g.get("edges") or []:
        if not isinstance(e, dict) or not all(e.get(k) for k in ("from", "type", "to")):
            fail(f"ребро без from/type/to: {e}")
            continue
        for end in ("from", "to"):
            if e[end] not in nodes:
                fail(f"ребро {e['from']} -{e['type']}-> {e['to']}: узел '{e[end]}' не существует")
        if e["from"] in nodes and e["to"] in nodes:
            key = (nodes[e["from"]]["type"], e["type"], nodes[e["to"]]["type"])
            if key not in rels:
                fail(f"связь {key[0]} -{key[1]}-> {key[2]} не разрешена registry/entities.yaml")
    return errors


def make_demo(root: Path, *, dangling=False, bad_relation=False):
    p = root / "graph.yaml"
    g = {
        "schema_version": 1, "kind": "knowledge-graph",
        "nodes": [
            {"id": "grow-repeat", "type": "goal", "title": "Рост повторных покупок"},
            {"id": "express-checkout", "type": "feature", "title": "Экспресс-чекаут"},
            {"id": "checkout-funnel", "type": "metric"},
            {"id": "slow-insight", "type": "insight"},
        ],
        "edges": [
            {"from": "express-checkout", "type": "measured-by", "to": "checkout-funnel"},
            {"from": "slow-insight", "type": "derived-from", "to": "checkout-funnel"},
            {"from": "slow-insight", "type": "feeds", "to": "grow-repeat"},
        ],
    }
    if dangling:
        g["edges"].append({"from": "express-checkout", "type": "delivered-by", "to": "no-such-release"})
    if bad_relation:
        g["edges"].append({"from": "checkout-funnel", "type": "contains", "to": "grow-repeat"})
    root.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(g, allow_unicode=True), encoding="utf-8")
    return p


def selftest():
    ok = True
    types, rels = load_dictionary()

    def expect(name, errs, want_errors):
        nonlocal ok
        good = bool(errs) == want_errors
        ok = ok and good
        print(f"{'PASS' if good else 'FAIL'} {name}" + ("" if good else f" -> {errs}"))

    with tempfile.TemporaryDirectory() as td:
        expect("валидный граф", validate_graph(make_demo(Path(td) / "a"), types, rels), False)
        expect("dangling reference -> fail",
               validate_graph(make_demo(Path(td) / "b", dangling=True), types, rels), True)
        expect("недопустимая связь -> fail",
               validate_graph(make_demo(Path(td) / "c", bad_relation=True), types, rels), True)
    print("knowledge-graph selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    if not argv:
        print("использование: validate_knowledge_graph.py <graph.yaml> [...] | --selftest")
        return 1
    types, rels = load_dictionary()
    all_errors = []
    for p in argv:
        all_errors += validate_graph(Path(p).resolve(), types, rels)
    if all_errors:
        print(f"НАЙДЕНЫ ПРОБЛЕМЫ В KNOWLEDGE GRAPH ({len(all_errors)}):")
        for e in all_errors:
            print(f"  - {e}")
        return 1
    print(f"OK: knowledge graph валиден ({len(argv)} файлов проверено).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
