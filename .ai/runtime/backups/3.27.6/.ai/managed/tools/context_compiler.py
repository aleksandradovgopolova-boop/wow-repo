#!/usr/bin/env python3
"""Context Compiler -> ContextBundle (v2.97, эпик Context Engineering, этап 1).

Перед прогоном формируем МИНИМАЛЬНЫЙ релевантный пакет контекста для WorkItem, а не грузим в модель
весь репозиторий, все правила и всех агентов. Селекция ДЕТЕРМИНИРОВАННА и обоснована реальными
данными (RunPlan/workflows/gates/tracks/registry), а не догадками:

  * agents   — владельцы стадий base_workflow + responsible_role гейтов плана (∩ registry);
  * skills   — uses_skills по стадиям base_workflow;
  * rules    — категории правил по workflow + трекам (core всегда);
  * repository_context — RepositoryProfile (project_detector);
  * files    — манифесты стека (evidence_source профиля) — детерминированный релевантный набор;
  * specifications/decisions — существующие артефакты features/<wid>/ и .ai/ (если есть).

Что ИСКЛЮЧАЕМ (с причиной): агентов не из RunPlan, правила не по задаче, skills не из плана.
Бюджет: estimated_tokens считается ДО вызова модели; превышение НЕ обрезает контекст молча —
поднимается overflow-флаг + open_question. Устаревшие артефакты помечаются (stale-warning).
Инвариант: тот же WorkItem при тех же входах -> ВОСПРОИЗВОДИМЫЙ пакет (сортировки, без времени/рандома).

Использование:
  context_compiler.py <child_root> --signals '{...}' [--feature name] [--json]
  context_compiler.py --selftest
Возврат 0 — ок, 1 — ошибка (или overflow при --strict).
"""

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
for _p in (PKG / "tools",):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import run_plan            # noqa: E402
import project_detector    # noqa: E402

CONTEXT_BUDGET_DEFAULT = 120_000   # токенов; override через signals["context_budget"] или config

# workflow -> категории правил (rules/<category>). core — всегда.
WORKFLOW_RULES = {
    "QUICK": ["core", "engineering"],
    "ENGINEERING": ["core", "engineering", "quality", "thinking"],
    "PRODUCT": ["core", "product", "research", "content", "thinking"],
    "RESEARCH": ["core", "research", "thinking"],
    "AI_FEATURE": ["core", "ai", "engineering", "quality"],
    "CRITICAL": ["core", "engineering", "quality", "thinking"],
    "VISUAL": ["core", "design"],
    "ANALYTICS": ["core", "product"],
    "INSIGHTS": ["core", "research"],
    "DECISION": ["core", "thinking"],
    "ADOPTION": ["core", "product"],
}
# трек -> дополнительные категории правил
TRACK_RULES = {
    "VISUAL": ["design"], "ANALYTICS": ["product"], "SECURITY": ["quality"],
    "DOCUMENTATION": ["documentation"], "EVENTS": ["engineering"], "AI": ["ai"], "RELEASE": ["engineering"],
}


def _load(path, default):
    try:
        return yaml.safe_load((PKG / path).read_text(encoding="utf-8")) or default
    except OSError:
        return default


def _agent_index():
    """id -> запись реестра агентов (с полем file)."""
    data = _load("registry/agents.yaml", {})
    agents = data.get("agents", data) if isinstance(data, dict) else data
    idx = {}
    if isinstance(agents, list):
        for a in agents:
            if isinstance(a, dict) and a.get("id"):
                idx[a["id"]] = a
    elif isinstance(agents, dict):
        for k, v in agents.items():
            if isinstance(v, dict):
                idx[v.get("id", k)] = v
    return idx


def _workflow_def(wf_id):
    wfs = _load("registry/workflows.yaml", {}).get("workflows", {})
    return wfs.get(wf_id, {})


def _gate_roles(gate_ids):
    gates = _load("quality/gates.yaml", {}).get("gates", {})
    roles = set()
    for gid in gate_ids:
        role = (gates.get(gid) or {}).get("responsible_role")
        if role:
            roles.add(role)
    return roles


def _est_tokens(text):
    # грубая, детерминированная оценка: ~4 символа на токен
    return (len(text) + 3) // 4


def _file_tokens(rel):
    p = PKG / rel
    if p.is_file():
        try:
            return _est_tokens(p.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            return 0
    return 0


def compile_bundle(signals, child_root, plan=None, context_budget=None):
    """Собрать ContextBundle. Детерминированно; без времени/рандома -> воспроизводимо."""
    child_root = Path(child_root)
    signals = dict(signals or {})
    if plan is None:
        plan = run_plan.build_plan(signals, workitem_id=signals.get("feature"))
    wid = plan["workitem_id"]
    base_wf = plan["base_workflow"]
    gate_ids = list(plan.get("gates", []))
    tracks = [t["track"] for t in plan.get("required_tracks", [])] + \
             [t["track"] for t in plan.get("conditional_tracks", [])]

    agent_idx = _agent_index()
    wf = _workflow_def(base_wf)

    # --- agents: владельцы/ревьюеры стадий + responsible_role гейтов, только реальные из registry ---
    role_ids, why_agent = set(), {}
    for st in wf.get("stages", []) or []:
        for key in ("owner", "writer"):
            r = st.get(key)
            if r:
                role_ids.add(r); why_agent.setdefault(r, f"стадия {st.get('id')} ({key})")
    for r in _gate_roles(gate_ids):
        role_ids.add(r); why_agent.setdefault(r, "responsible_role гейта RunPlan")
    included_agents = sorted(r for r in role_ids if r in agent_idx)
    # роли, которых нет в registry как агентов (напр. final-verifier) — честно отметим в допущениях
    unknown_roles = sorted(r for r in role_ids if r not in agent_idx)

    # --- skills: uses_skills по стадиям ---
    skills = set()
    for st in wf.get("stages", []) or []:
        for sk in st.get("uses_skills", []) or []:
            skills.add(sk)
    included_skills = sorted(skills)

    # --- rules: категории по workflow + трекам, core всегда ---
    rule_cats = set(WORKFLOW_RULES.get(base_wf, ["core"]))
    for tr in tracks:
        rule_cats |= set(TRACK_RULES.get(tr, []))
    rule_cats.add("core")
    included_rules = sorted(c for c in rule_cats if (PKG / "rules" / c).is_dir())

    # --- repository_context: RepositoryProfile ---
    profile = project_detector.detect(child_root)
    repo_ctx = [f"{s['language']} ({', '.join(s.get('frameworks') or []) or 'no-fw'})"
                for s in profile.get("stacks", [])]
    files = sorted({src for s in profile.get("stacks", []) for src in (s.get("evidence_source") or [])})

    # --- specifications: артефакты в features/<wid> И в .ai/runplan/<wid> (v2.108 finding аудита:
    #     authoring пишет часть артефактов в .ai/runplan/<wid> — раньше их не находили) ---
    specs = []
    for base in (child_root / "features" / wid, child_root / ".ai" / "runplan" / wid):
        if base.is_dir():
            for name in ("requirements.md", "requirements.yaml", "plan.md", "plan.yaml", "spec.md"):
                if (base / name).is_file():
                    specs.append(str((base / name).relative_to(child_root)))
    # --- decisions: relevance-фильтр (v2.108 finding аудита: раньше включались ВСЕ разом). Берём те,
    #     чей текст пересекается с affected_areas/ключевыми словами задачи; иначе — 3 самых свежих ---
    decisions = []
    dec_dir = child_root / ".ai" / "project" / "decisions"
    if dec_dir.is_dir():
        all_dec = sorted(dec_dir.glob("*.md"))
        kws = {a.lower() for a in (signals.get("affected_areas") or [])}
        kws |= {w.lower() for w in re.findall(r"\w{4,}", signals.get("task_text", ""))}
        relevant = []
        for p in all_dec:
            txt = p.read_text(encoding="utf-8", errors="ignore").lower()
            if any(k in txt for k in kws):
                relevant.append(p)
        chosen = relevant or all_dec[-3:]     # релевантные; иначе 3 самых свежих (не все разом)
        decisions = [str(p.relative_to(child_root)) for p in chosen]

    # --- excluded (с причиной): агенты не из RunPlan, правила и skills не по задаче ---
    all_rule_cats = sorted(p.name for p in (PKG / "rules").iterdir() if p.is_dir()) if (PKG / "rules").is_dir() else []
    excluded = []
    for aid in sorted(agent_idx):
        if aid not in included_agents:
            excluded.append({"source": f"agent:{aid}", "reason": "не участвует в RunPlan этого WorkItem"})
    for c in all_rule_cats:
        if c not in included_rules:
            excluded.append({"source": f"rules/{c}", "reason": "категория правил не применима к workflow/трекам задачи"})

    # --- токены: агенты + правила (то, что реально попадёт в контекст) ---
    tok = 0
    tok += _est_tokens(signals.get("task_text", ""))
    for aid in included_agents:
        tok += _file_tokens(agent_idx[aid].get("file", ""))
    for c in included_rules:
        for p in sorted((PKG / "rules" / c).glob("*.md")):
            tok += _file_tokens(str(p.relative_to(PKG)))
    for rel in specs:
        tok += _est_tokens((child_root / rel).read_text(encoding="utf-8", errors="ignore")) if (child_root / rel).is_file() else 0

    budget = context_budget or signals.get("context_budget") or CONTEXT_BUDGET_DEFAULT

    assumptions, open_questions = [], []
    if unknown_roles:
        assumptions.append("роли без отдельного агента в registry (исполняет рантайм/верификатор): "
                           + ", ".join(unknown_roles))
    # overflow: НЕ обрезаем молча — поднимаем вопрос
    overflow = tok > budget
    if overflow:
        open_questions.append(
            f"контекст ({tok} ток.) превышает бюджет ({budget}) — нужна декомпозиция задачи или "
            "сокращение включённого (этап 4 Atomic Planning); контекст НЕ обрезан молча")
    # stale-предупреждение: артефакты плана ссылаются на прошлую версию кита
    cur_ver = (PKG / "VERSION").read_text(encoding="utf-8").strip() if (PKG / "VERSION").is_file() else "?"
    for rel in specs:
        try:
            txt = (child_root / rel).read_text(encoding="utf-8", errors="ignore")
            if "installed_version" in txt and cur_ver not in txt:
                open_questions.append(f"{rel}: возможно устарел (installed_version не совпадает с {cur_ver}) — проверить")
        except OSError:
            pass

    return {
        "schema_version": 1, "kind": "ContextBundle",
        "workitem_id": wid, "base_workflow": base_wf,
        "revision": _git_head(child_root),
        "included": {
            "project_context": [signals.get("task_text", "")[:200]] if signals.get("task_text") else [],
            "repository_context": repo_ctx,
            "specifications": specs,
            "decisions": decisions,
            "files": files,
            "rules": included_rules,
            "skills": included_skills,
            "agents": included_agents,
        },
        "included_reasons": {"agents": why_agent},
        "excluded": excluded,
        "assumptions": assumptions,
        "open_questions": open_questions,
        "estimated_tokens": tok,
        "context_budget": budget,
        "overflow": overflow,
    }


def _git_head(root):
    import subprocess
    r = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


# Грубые контекстные окна моделей (токены). Бюджет payload режем по МЕНЬШЕМУ из заданного и окна модели.
MODEL_CONTEXT = {
    "deepseek-chat": 64_000, "gpt-4o": 128_000, "gpt-4o-mini": 128_000,
    "claude-3-5-sonnet": 200_000, "claude-3-5-haiku": 200_000,
}
_PER_FILE_TOKEN_CAP = 1500        # один файл не съедает весь бюджет payload
_OUTPUT_RESERVE_FRAC = 0.25       # резерв под вывод модели
_TOOLLOOP_RESERVE_FRAC = 0.15     # резерв под tool-loop (шаги/наблюдения)


def _sha(text):
    import hashlib
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def build_payload(signals, child_root, plan=None, bundle=None, context_budget=None, model=None):
    """v2.108 Operational Context: собрать РЕАЛЬНЫЙ compiled payload для prompt модели из ContextBundle
    (содержимое правил/решений/спек/repo+project context), с бюджетом (output/tool-loop reserve + окно
    модели) и манифестом (source, hash, revision, tokens, reason). Превышение НЕ обрезает молча —
    вытесненное фиксируется в excluded_for_budget. -> dict с text (для инъекции) + манифест."""
    child_root = Path(child_root)
    signals = dict(signals or {})
    if bundle is None:
        bundle = compile_bundle(signals, child_root, plan=plan, context_budget=context_budget)
    budget = context_budget or bundle.get("context_budget") or CONTEXT_BUDGET_DEFAULT
    if model and model in MODEL_CONTEXT:
        budget = min(budget, MODEL_CONTEXT[model])
    payload_budget = int(budget * (1 - _OUTPUT_RESERVE_FRAC - _TOOLLOOP_RESERVE_FRAC))
    rev = bundle.get("revision")

    # источники в порядке приоритета: (kind, source_id, loader) -> текст
    inc = bundle.get("included", {})
    candidates = []
    task_text = signals.get("task_text", "")
    if task_text:
        candidates.append(("project_context", "task", task_text, "текст задачи"))
    if inc.get("repository_context"):
        candidates.append(("repository_context", "RepositoryProfile",
                           "Стек: " + "; ".join(inc["repository_context"]), "профиль репозитория"))
    for rel in inc.get("specifications", []):
        p = child_root / rel
        if p.is_file():
            candidates.append(("specification", rel, p.read_text(encoding="utf-8", errors="ignore"),
                               "спецификация задачи"))
    for rel in inc.get("decisions", []):
        p = child_root / rel
        if p.is_file():
            candidates.append(("decision", rel, p.read_text(encoding="utf-8", errors="ignore"),
                               "архитектурное решение"))
    for cat in inc.get("rules", []):
        for p in sorted((PKG / "rules" / cat).glob("*.md")):
            candidates.append(("rule", str(p.relative_to(PKG)),
                               p.read_text(encoding="utf-8", errors="ignore"),
                               f"правило категории {cat} (из RunPlan)"))
    if inc.get("skills"):
        candidates.append(("skills", "skills", "Нужные skills: " + ", ".join(inc["skills"]),
                           "релевантные skills (по стадиям)"))

    included_items, excluded_for_budget, parts, used = [], [], [], 0
    for kind, source, text, reason in candidates:
        capped = text
        tok = _est_tokens(capped)
        if tok > _PER_FILE_TOKEN_CAP:                 # обрезаем ОДИН источник до кэпа (честно помечаем)
            capped = capped[: _PER_FILE_TOKEN_CAP * 4] + "\n…[обрезано до кэпа]"
            tok = _est_tokens(capped)
        if used + tok > payload_budget and kind not in ("project_context",):
            excluded_for_budget.append({"source": source, "kind": kind, "tokens": tok,
                                        "reason": "вытеснено бюджетом payload"})
            continue
        used += tok
        included_items.append({"source": source, "kind": kind, "hash": _sha(text),
                               "revision": rev, "tokens": tok, "reason": reason})
        parts.append(f"=== [{kind}] {source} (причина: {reason}) ===\n{capped}")

    text = ("=== СОБРАННЫЙ КОНТЕКСТ (Context Compiler) ===\n"
            "Ниже — только релевантный этому WorkItem контекст (правила/решения/спеки/стек), "
            "отобранный из RunPlan и урезанный по бюджету. Учитывай его при работе.\n\n"
            + "\n\n".join(parts)) if parts else ""

    return {
        "schema_version": 1, "kind": "ContextPayload", "workitem_id": bundle.get("workitem_id"),
        "revision": rev, "model": model,
        "context_budget": budget, "payload_budget": payload_budget,
        "output_reserve": int(budget * _OUTPUT_RESERVE_FRAC),
        "tool_loop_reserve": int(budget * _TOOLLOOP_RESERVE_FRAC),
        "payload_tokens": used,
        "included_items": included_items,
        "excluded_for_budget": excluded_for_budget,
        "text": text,
    }


def selftest():
    import tempfile
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "package.json").write_text('{"dependencies":{"react":"^18"}}', encoding="utf-8")
        eng = {"task_type": "ENGINEERING", "risk": "medium", "affected_areas": ["core"],
               "task_text": "отрефакторить модуль расчёта"}
        b = compile_bundle(eng, root)
        expect("kind=ContextBundle", b["kind"] == "ContextBundle")
        expect("включены агенты из RunPlan (непусто)", len(b["included"]["agents"]) > 0)
        expect("для каждого включённого агента есть причина",
               all(a in b["included_reasons"]["agents"] for a in b["included"]["agents"]))
        expect("ENGINEERING включает правила engineering+core",
               {"core", "engineering"} <= set(b["included"]["rules"]))
        expect("repository_context определил node", any("node" in r for r in b["included"]["repository_context"]))
        expect("excluded непуст и с причинами",
               b["excluded"] and all("reason" in e and "source" in e for e in b["excluded"]))
        expect("estimated_tokens измерен ДО модели (>0)", b["estimated_tokens"] > 0)
        expect("context_budget присутствует", b["context_budget"] == CONTEXT_BUDGET_DEFAULT)
        # воспроизводимость: тот же вход -> тот же пакет (без времени/рандома; revision может отличаться в не-git)
        b2 = compile_bundle(eng, root)
        expect("воспроизводимость: included идентичен при тех же входах",
               b["included"] == b2["included"] and b["excluded"] == b2["excluded"])

        # overflow: маленький бюджет -> overflow=True + open_question, контекст НЕ обрезан
        b_of = compile_bundle(eng, root, context_budget=10)
        expect("overflow: бюджет превышен -> overflow=True", b_of["overflow"] is True)
        expect("overflow: поднят open_question (не обрезано молча)",
               any("бюджет" in q for q in b_of["open_questions"])
               and b_of["included"]["agents"] == b["included"]["agents"])

        # QUICK легче ENGINEERING по правилам (меньше категорий)
        q = compile_bundle({"task_type": "QUICK", "risk": "low", "affected_areas": ["core"], "task_text": "мелкая правка"}, root)
        expect("QUICK: правил не больше, чем у ENGINEERING (минимальность)",
               len(q["included"]["rules"]) <= len(b["included"]["rules"]))

        # PRODUCT включает product-правила
        p = compile_bundle({"task_type": "PRODUCT", "risk": "medium", "affected_areas": ["catalog"],
                            "measurable_behavior": True, "task_text": "новая фича"}, root)
        expect("PRODUCT включает правила product", "product" in p["included"]["rules"])

        # v2.108 Operational Context: build_payload даёт РЕАЛЬНЫЙ текст для prompt + манифест
        pay = build_payload(eng, root)
        expect("payload: kind=ContextPayload + непустой text", pay["kind"] == "ContextPayload"
               and len(pay["text"]) > 0)
        expect("payload: содержит РЕАЛЬНОЕ содержимое правил (не только пути)",
               "=== [rule]" in pay["text"] and pay["payload_tokens"] > 0)
        expect("payload: у каждого элемента hash+revision+reason+tokens",
               all({"hash", "revision", "reason", "tokens", "source"} <= set(i) for i in pay["included_items"]))
        expect("payload: бюджет с резервами (output+tool-loop < полный бюджет)",
               pay["payload_budget"] < pay["context_budget"]
               and pay["output_reserve"] > 0 and pay["tool_loop_reserve"] > 0)
        # маленький бюджет -> вытеснение фиксируется (не молча), задача остаётся
        pay_of = build_payload(eng, root, context_budget=60)
        expect("payload: превышение бюджета -> excluded_for_budget непуст (не молча), task остался",
               pay_of["excluded_for_budget"]
               and any(i["kind"] == "project_context" for i in pay_of["included_items"]))
        # модель сужает бюджет по окну
        pay_m = build_payload(eng, root, context_budget=500_000, model="deepseek-chat")
        expect("payload: окно модели сужает бюджет (deepseek-chat=64k)",
               pay_m["context_budget"] == MODEL_CONTEXT["deepseek-chat"])

    print("context_compiler selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    ap = argparse.ArgumentParser(prog="context_compiler.py")
    ap.add_argument("child_root", nargs="?", default=".")
    ap.add_argument("--signals", default="{}")
    ap.add_argument("--feature")
    ap.add_argument("--budget", type=int)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="ненулевой код при overflow")
    a = ap.parse_args(argv)
    signals = json.loads(a.signals)
    if a.feature:
        signals["feature"] = a.feature
    b = compile_bundle(signals, Path(a.child_root), context_budget=a.budget)
    if a.json:
        print(json.dumps(b, ensure_ascii=False, indent=2))
    else:
        inc = b["included"]
        print(f"CONTEXT-BUNDLE {b['workitem_id']} ({b['base_workflow']}) · ~{b['estimated_tokens']}/"
              f"{b['context_budget']} ток.{' ⚠OVERFLOW' if b['overflow'] else ''}")
        print(f"  агенты ({len(inc['agents'])}): {', '.join(inc['agents']) or '—'}")
        print(f"  правила: {', '.join(inc['rules']) or '—'} · skills: {', '.join(inc['skills']) or '—'}")
        print(f"  стек: {', '.join(inc['repository_context']) or 'не определён'}")
        print(f"  исключено источников: {len(b['excluded'])}")
        for q in b["open_questions"]:
            print(f"  ? {q}")
    return 1 if (a.strict and b["overflow"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
