#!/usr/bin/env python3
"""Проверка ContextArchitectureDecision (CAD) — v3.3.2 Operational Architecture Backbone.

CAD (schemas/context-architecture-decision.schema.json) — контракт retrieval-архитектуры контекста
(развитие Context Compiler, НЕ векторная память). Валидатор держит контракт честным:

  1. schema_version=1, kind=CAD; id формата CAD-NNN; title непуст; status ∈ 4-enum;
  2. retrieval_pipeline — ПОДПОСЛЕДОВАТЕЛЬНОСТЬ канонического порядка (repository-first):
     policy_metadata -> repository_graph -> full_text -> semantic_fallback -> reranking ->
     budgeted_role_view (нельзя начинать с semantic_fallback — это не «сначала вектор»);
  3. для status=accepted: все инварианты true; pipeline заканчивается budgeted_role_view И НЕ
     начинается с semantic_fallback; role_views покрывают все 5; cache_key = {repository,sha,policy,view};
     builds_on ссылается на context_compiler (CAD развивает основание, не вводит новую систему);
  4. supersede-ссылки формата CAD-NNN.

Использование:  validate_context_architecture.py <cad.(yaml|json)> [--json] | --selftest
"""
import json
import re
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
SCHEMA = PKG / "schemas" / "context-architecture-decision.schema.json"

STATUS = {"proposed", "accepted", "superseded", "deprecated"}
CANON = ["policy_metadata", "repository_graph", "full_text", "semantic_fallback",
         "reranking", "budgeted_role_view"]
ROLES = {"planner", "executor", "ui_reviewer", "security_reviewer", "integration"}
CACHE = {"repository", "sha", "policy", "view"}
_ID = re.compile(r"^CAD-[0-9]{3,}$")


def check(data: dict):
    e = []
    if not isinstance(data, dict):
        return ["CAD не объект"]
    if data.get("schema_version") != 1:
        e.append("schema_version должен быть 1")
    if data.get("kind") != "ContextArchitectureDecision":
        e.append("kind должен быть 'ContextArchitectureDecision'")
    if not (isinstance(data.get("id"), str) and _ID.match(data["id"])):
        e.append("id должен быть формата CAD-NNN")
    if not (isinstance(data.get("title"), str) and data["title"].strip()):
        e.append("title обязателен и непуст")
    status = data.get("status")
    if status not in STATUS:
        e.append(f"status ∉ {sorted(STATUS)}")

    pipe = data.get("retrieval_pipeline")
    if not isinstance(pipe, list) or len(pipe) < 2:
        e.append("retrieval_pipeline — список из ≥2 стадий")
        pipe = []
    else:
        idx = []
        for s in pipe:
            if s not in CANON:
                e.append(f"retrieval_pipeline: неизвестная стадия '{s}'")
            else:
                idx.append(CANON.index(s))
        if idx != sorted(idx):
            e.append("retrieval_pipeline не в каноническом порядке (repository-first)")
        if len(set(pipe)) != len(pipe):
            e.append("retrieval_pipeline: дубликаты стадий")

    inv = data.get("invariants")
    if not isinstance(inv, dict):
        e.append("invariants обязателен")
        inv = {}

    rv = data.get("role_views") or []
    ck = data.get("cache_key") or []

    for f in ("supersedes", "superseded_by"):
        v = data.get(f)
        if v is not None and not (isinstance(v, str) and _ID.match(v)):
            e.append(f"{f} должен быть CAD-NNN или null")

    # усиленные требования для принятого решения
    if status == "accepted":
        for k in ("exact_revision_binding", "provenance", "access_filter_before_retrieval",
                  "stale_detection"):
            if inv.get(k) is not True:
                e.append(f"accepted CAD требует invariants.{k}=true")
        if pipe and pipe[0] == "semantic_fallback":
            e.append("accepted CAD не может начинать retrieval с semantic_fallback (не «сначала вектор»)")
        if pipe and pipe[-1] != "budgeted_role_view":
            e.append("accepted CAD должен заканчивать retrieval budgeted_role_view")
        if set(rv) != ROLES:
            e.append(f"accepted CAD: role_views должны покрывать все 5 {sorted(ROLES)}")
        if set(ck) != CACHE:
            e.append(f"accepted CAD: cache_key должен быть {sorted(CACHE)}")
        if "context_compiler" not in (data.get("builds_on") or ""):
            e.append("accepted CAD должен builds_on существующий context_compiler (развитие, не новая система)")
    return e


def _load(p: Path):
    t = p.read_text(encoding="utf-8")
    return yaml.safe_load(t) if p.suffix in (".yaml", ".yml") else json.loads(t)


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    ex = json.loads(SCHEMA.read_text(encoding="utf-8"))["examples"][0]
    expect("пример из схемы валиден", check(ex) == [])
    expect("нарушение порядка pipeline -> ошибка",
           any("порядк" in x for x in check({**ex,
               "retrieval_pipeline": ["full_text", "repository_graph", "budgeted_role_view"]})))
    expect("accepted начинает с semantic_fallback -> ошибка",
           any("сначала вектор" in x for x in check({**ex,
               "retrieval_pipeline": ["semantic_fallback", "reranking", "budgeted_role_view"]})))
    expect("accepted без инварианта -> ошибка",
           any("exact_revision_binding" in x for x in check({**ex,
               "invariants": {**ex["invariants"], "exact_revision_binding": False}})))
    expect("accepted без всех role_views -> ошибка",
           any("role_views" in x for x in check({**ex, "role_views": ["planner"]})))
    expect("accepted с неполным cache_key -> ошибка",
           any("cache_key" in x for x in check({**ex, "cache_key": ["repository", "sha"]})))
    expect("accepted без builds_on context_compiler -> ошибка",
           any("context_compiler" in x for x in check({**ex, "builds_on": "новая vector-db"})))
    expect("proposed мягче: неполные role_views допустимы",
           check({**ex, "status": "proposed", "role_views": ["planner"]}) == [])
    expect("битый id -> ошибка", any("id" in x for x in check({**ex, "id": "CAD1"})))
    expect("enum stage == схема (нет дрейфа)",
           set(json.loads(SCHEMA.read_text(encoding="utf-8"))["properties"]["retrieval_pipeline"]
               ["items"]["enum"]) == set(CANON))

    print("validate_context_architecture selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    errors = check(_load(Path(args[0])))
    if "--json" in argv:
        print(json.dumps({"errors": errors}, ensure_ascii=False, indent=2))
    elif errors:
        print("CAD: ошибки:")
        for x in errors:
            print(f"  - {x}")
    else:
        print("CAD-OK: структура валидна.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
