#!/usr/bin/env python3
"""Движок маршрутизации AI-first системы (Фаза 6).

По входным признакам задачи выбирает workflow / provider / model_class / runtime /
execution_mode / fallbacks и возвращает МАШИНОЧИТАЕМОЕ ОБЪЯСНИМОЕ решение
(schemas/route-decision.schema.json). Правила берутся из registry/routing-policy.yaml
(декларативно), возможности — из registry/{providers,runtimes,workflows}.yaml.
Названия конкретных моделей в workflow не зашиты — используется model_class.

Использование:
  ai_route.py '<json-инпуты>'   — вывести решение (JSON)
  ai_route.py --selftest        — прогнать примеры и проверить форму решения (exit 1 при ошибке)

Требует pyyaml.
"""

import json
import sys
from pathlib import Path

import yaml

PKG_ROOT = Path(__file__).resolve().parents[1]
REG = PKG_ROOT / "registry"


def load(name):
    return yaml.safe_load((REG / name).read_text(encoding="utf-8"))


def _match(when, ctx):
    if not isinstance(when, dict):
        return False
    for k, v in when.items():
        if ctx.get(k) != v:
            return False
    return True


def route(inp):
    policy = load("routing-policy.yaml")
    providers = load("providers.yaml")["providers"]
    runtimes = load("runtimes.yaml")["runtimes"]
    workflows = load("workflows.yaml")["workflows"]

    available_providers = inp.get("available_providers", list(providers.keys()))
    available_runtimes = inp.get("available_runtimes", list(runtimes.keys()))
    reasons = []
    low_confidence = False   # v2.123: классификация без явного сигнала тяжести -> подтвердить preview

    # 1. workflow: правила policy (эскалации) -> точное имя контракта ->
    # selection_criteria.task_type из workflows.yaml (единый источник) -> честный default.
    # Инвариант: движок НИКОГДА не возвращает workflow, которого нет в реестре.
    # Правило, целящее в ещё не зарегистрированный контракт (напр. CRITICAL, post-MVP),
    # применяется как ЭСКАЛАЦИЯ к базовому workflow (reason + ручное одобрение), а не
    # как отдельный workflow — иначе route ссылается на несуществующую стадийную схему.
    workflow = None
    escalations = []
    for rule in policy.get("workflow_rules", []):
        if "when" in rule and _match(rule["when"], inp):
            target = rule.get("workflow")
            if target in workflows:
                workflow = target
                if rule.get("escalate_reason"):
                    reasons.append(rule["escalate_reason"])
                break
            # объявленный, но незарегистрированный контракт -> эскалация, не workflow
            if rule.get("escalate_reason"):
                escalations.append(
                    f"{rule['escalate_reason']} (контракт {target} ещё не зарегистрирован — "
                    f"эскалация применена к базовому workflow, требуется ручное одобрение)")
            # НЕ break: продолжаем искать реальный базовый workflow ниже
    if workflow is None:
        tt = inp.get("task_type")
        if tt in workflows:
            workflow = tt
        else:
            for wid, w in workflows.items():
                crit = (w.get("selection_criteria") or {}).get("task_type")
                if isinstance(crit, list) and tt in crit:
                    workflow = wid
                    reasons.append(f"task_type '{tt}' -> {wid} (selection_criteria)")
                    break
    if workflow is None:
        # v2.123 калибровка классификатора (finding обкатки: тривиальные задачи слепо уходили в
        # тяжёлый ENGINEERING -> Spec-First блок). Тяжёлый ENGINEERING — ТОЛЬКО при ЯВНОМ сигнале
        # тяжести (size medium+/risk medium+). При неопределённости (size/risk неизвестны ИЛИ низкие)
        # НЕ эскалируем автоматически: уходим в QUICK и помечаем low-confidence (движок предлагает
        # подтвердить/уточнить через preview, а не молча блокировать Spec-First). Риск critical уже
        # переопределён в CRITICAL выше.
        size, risk = inp.get("size"), inp.get("risk")
        heavy = size in ("medium", "large", "xl") or risk in ("medium", "high", "critical")
        if heavy and "ENGINEERING" in workflows:
            workflow = "ENGINEERING"
            reasons.append(f"unknown task_type + size={size}/risk={risk} -> ENGINEERING (явный сигнал тяжести)")
        elif "QUICK" in workflows:
            workflow = "QUICK"
            low_confidence = True
            reasons.append(f"unknown task_type + size={size or 'unknown'}/risk={risk or 'unknown'} -> QUICK "
                           f"(нет сигнала тяжести; при неопределённости не эскалируем в ENGINEERING — "
                           f"подтверди/уточни через preview)")
        else:
            workflow = "ENGINEERING"
            reasons.append(f"unknown task_type '{inp.get('task_type')}' -> ENGINEERING (QUICK недоступен)")

    # 2. provider prefer/forbid + model_class (из provider_rules)
    prefer_provider, forbid_provider, model_class = [], [], None
    for rule in policy.get("provider_rules", []):
        applies = ("default" in rule) or _match(rule.get("when", {}), inp)
        if not applies:
            continue
        if rule.get("prefer_provider"):
            for p in rule["prefer_provider"]:
                if p not in prefer_provider:
                    prefer_provider.append(p)
            if rule.get("reason"):
                reasons.append(rule["reason"])
        if rule.get("forbid_provider"):
            for p in rule["forbid_provider"]:
                if p not in forbid_provider:
                    forbid_provider.append(p)
        if rule.get("prefer_model_class") and model_class is None:
            model_class = rule["prefer_model_class"]
    model_class = model_class or "balanced"

    # выбор провайдера: prefer ∩ available, не forbidden; иначе первый available не forbidden
    def pick_provider():
        for p in prefer_provider:
            if p in available_providers and p not in forbid_provider:
                return p
        for p in available_providers:
            if p not in forbid_provider:
                return p
        return None
    selected_provider = pick_provider()
    if selected_provider is None:
        reasons.append("no allowed provider available")

    # 3. runtime
    prefer_runtime = []
    for rule in policy.get("runtime_rules", []):
        ctx = dict(inp)
        # спец-ключ available_runtime: применимо, если рантайм доступен
        if "when" in rule and "available_runtime" in rule["when"]:
            ar = rule["when"]["available_runtime"]
            if ar in available_runtimes:
                prefer_runtime += [r for r in rule.get("prefer_runtime", []) if r not in prefer_runtime]
            continue
        if ("default" in rule) or _match(rule.get("when", {}), ctx):
            prefer_runtime += [r for r in rule.get("prefer_runtime", []) if r not in prefer_runtime]
    selected_runtime = next((r for r in prefer_runtime if r in available_runtimes),
                            (available_runtimes[0] if available_runtimes else None))

    # 4. execution_mode из возможностей выбранного рантайма
    rt = runtimes.get(selected_runtime, {})
    caps = rt.get("capabilities", {})
    def cap_true(name):
        c = caps.get(name)
        return isinstance(c, dict) and c.get("value") is True
    if cap_true("native_subagents"):
        execution_mode = "native"
    else:
        execution_mode = rt.get("preferred_mode", "sequential")

    # 5. approval
    human_approval = "conditional"
    for rule in policy.get("approval_rules", []):
        if ("default" in rule) or _match(rule.get("when", {}), inp):
            if "human_approval_required" in rule:
                human_approval = rule["human_approval_required"]
                break
    human_approval_required = (human_approval is True)
    # эскалация в незарегистрированный контракт (напр. critical->CRITICAL, post-MVP)
    # всегда требует ручного одобрения, даже если approval_rules сказали иначе
    if escalations:
        human_approval_required = True

    # 6. required/missing capabilities (из workflow-контракта)
    required = workflows.get(workflow, {}).get("required_capabilities", [])
    prov_caps = providers.get(selected_provider, {}).get("capabilities", {}) if selected_provider else {}
    def provider_supports(capname):
        c = prov_caps.get(capname)
        if c is True:
            return True
        if isinstance(c, dict):
            return c.get("value") is True or c.get("status") in ("documented", "verified")
        # file_read/write/web_access часто на стороне runtime
        return cap_true(capname) or capname in ("file_read", "file_write")
    missing = [c for c in required if not provider_supports(c)]

    # 7. fallbacks
    fallbacks = []
    for p in prefer_provider:
        if p != selected_provider and p in available_providers and p not in forbid_provider:
            fallbacks.append({"provider": p, "runtime": selected_runtime})
    if "local" in available_providers and selected_provider != "local" and not any(f["provider"] == "local" for f in fallbacks):
        fallbacks.append({"provider": "local", "runtime": "generic-orchestrator"})

    reasons.extend(escalations)
    if not reasons:
        reasons.append("default routing")

    return {
        "schema_version": 1,
        "workflow": workflow,
        "selected_provider": selected_provider,
        "selected_runtime": selected_runtime,
        "selected_model_class": model_class,
        "execution_mode": execution_mode,
        "reasons": reasons,
        "classification_confidence": "low" if low_confidence else "normal",
        "required_capabilities": required,
        "missing_capabilities": missing,
        "fallbacks": fallbacks,
        "human_approval_required": human_approval_required,
    }


REQUIRED_KEYS = ["schema_version", "workflow", "selected_provider", "selected_runtime",
                 "selected_model_class", "execution_mode", "reasons", "human_approval_required"]

SCENARIOS = [
    {"name": "confidential RU product",
     "inp": {"task_type": "PRODUCT", "risk": "medium", "language": "ru",
             "confidentiality": "confidential", "data_residency_required": "ru",
             "available_providers": ["gigachat", "local"],
             "available_runtimes": ["generic-orchestrator", "generic-api"]},
     "expect": {"workflow": "PRODUCT", "selected_model_class": "enterprise-russian",
                "execution_mode": "sequential"}},
    {"name": "confidential RU, gigachat NOT enabled -> local",
     "inp": {"task_type": "ENGINEERING", "risk": "medium", "language": "ru",
             "confidentiality": "confidential", "data_residency_required": "ru",
             "available_providers": ["local"],
             "available_runtimes": ["generic-orchestrator"]},
     "expect": {"selected_provider": "local", "execution_mode": "sequential"}},
    {"name": "normal web feature",
     "inp": {"task_type": "ENGINEERING", "risk": "low", "reasoning_complexity": "high",
             "confidentiality": "internal",
             "available_providers": ["anthropic", "openai"],
             "available_runtimes": ["claude-code", "generic-orchestrator"]},
     "expect": {"workflow": "ENGINEERING", "selected_provider": "anthropic",
                "selected_runtime": "claude-code", "execution_mode": "native",
                "selected_model_class": "high-reasoning"}},
    {"name": "quick fix",
     "inp": {"task_type": "QUICK", "risk": "low", "reasoning_complexity": "low",
             "confidentiality": "internal",
             "available_providers": ["anthropic"], "available_runtimes": ["claude-code"]},
     "expect": {"workflow": "QUICK", "human_approval_required": False,
                "selected_model_class": "fast"}},
    # v2.1.1: детальные task_type маршрутизируются по selection_criteria из workflows.yaml
    {"name": "ui-change -> VISUAL",
     "inp": {"task_type": "ui-change", "risk": "low", "confidentiality": "internal",
             "available_providers": ["anthropic"], "available_runtimes": ["claude-code"]},
     "expect": {"workflow": "VISUAL", "execution_mode": "native"}},
    {"name": "instrumentation -> ANALYTICS",
     "inp": {"task_type": "instrumentation", "risk": "low", "confidentiality": "internal",
             "available_providers": ["anthropic"], "available_runtimes": ["claude-code"]},
     "expect": {"workflow": "ANALYTICS"}},
    {"name": "post-release-analysis -> INSIGHTS",
     "inp": {"task_type": "post-release-analysis", "risk": "low", "confidentiality": "internal",
             "available_providers": ["anthropic"], "available_runtimes": ["claude-code"]},
     "expect": {"workflow": "INSIGHTS"}},
    {"name": "onboarding -> ADOPTION",
     "inp": {"task_type": "onboarding", "risk": "low", "confidentiality": "internal",
             "available_providers": ["anthropic"], "available_runtimes": ["claude-code"]},
     "expect": {"workflow": "ADOPTION"}},
    {"name": "bug-fix -> QUICK (по selection_criteria)",
     "inp": {"task_type": "bug-fix", "risk": "low", "confidentiality": "internal",
             "available_providers": ["anthropic"], "available_runtimes": ["claude-code"]},
     "expect": {"workflow": "QUICK"}},
    {"name": "ai-feature -> AI_FEATURE (v2.4)",
     "inp": {"task_type": "ai-feature", "risk": "low", "confidentiality": "internal",
             "available_providers": ["anthropic"], "available_runtimes": ["claude-code"]},
     "expect": {"workflow": "AI_FEATURE"}},
    {"name": "v2.123: неизвестный task_type + low risk, нет сигнала тяжести -> QUICK (не эскалируем)",
     "inp": {"task_type": "something-strange", "risk": "low", "confidentiality": "internal",
             "available_providers": ["anthropic"], "available_runtimes": ["claude-code"]},
     "expect": {"workflow": "QUICK"}},
    {"name": "v2.123: неопределённость (нет size/risk) -> QUICK + classification_confidence=low",
     "inp": {"task_type": None, "confidentiality": "internal",
             "available_providers": ["anthropic"], "available_runtimes": ["claude-code"]},
     "expect": {"workflow": "QUICK", "classification_confidence": "low"}},
    # finding обкатки 4: неизвестный task_type + мелкий размер -> QUICK, не тяжёлый ENGINEERING
    {"name": "unknown task_type + size small/low risk -> QUICK (finding 4)",
     "inp": {"task_type": None, "size": "small", "risk": "low", "ui_changed": True,
             "confidentiality": "internal",
             "available_providers": ["anthropic"], "available_runtimes": ["claude-code"]},
     "expect": {"workflow": "QUICK"}},
    {"name": "unknown task_type + size large -> остаётся ENGINEERING",
     "inp": {"task_type": None, "size": "large", "risk": "low", "confidentiality": "internal",
             "available_providers": ["anthropic"], "available_runtimes": ["claude-code"]},
     "expect": {"workflow": "ENGINEERING"}},
    # critical risk переопределяет task_type -> зарегистрированный контракт CRITICAL (v2.15)
    {"name": "critical PRODUCT -> CRITICAL (override) + ручное одобрение",
     "inp": {"task_type": "PRODUCT", "risk": "critical", "confidentiality": "internal",
             "available_providers": ["anthropic"], "available_runtimes": ["claude-code"]},
     "expect": {"workflow": "CRITICAL", "human_approval_required": True}},
    {"name": "critical QUICK -> CRITICAL (override) + ручное одобрение",
     "inp": {"task_type": "QUICK", "risk": "critical", "confidentiality": "internal",
             "available_providers": ["anthropic"], "available_runtimes": ["claude-code"]},
     "expect": {"workflow": "CRITICAL", "human_approval_required": True}},
]


def selftest():
    ok = True
    for sc in SCENARIOS:
        d = route(sc["inp"])
        missing = [k for k in REQUIRED_KEYS if k not in d or d[k] in (None, "")]
        # selected_provider may legitimately be None only if no provider; here always set
        problems = []
        if missing:
            problems.append(f"нет ключей {missing}")
        for k, v in sc["expect"].items():
            if d.get(k) != v:
                problems.append(f"{k}={d.get(k)!r} != ожидалось {v!r}")
        if not d.get("reasons"):
            problems.append("пустые reasons")
        status = "OK  " if not problems else "FAIL"
        if problems:
            ok = False
        print(f"{status} [{sc['name']}] -> wf={d['workflow']} prov={d['selected_provider']} "
              f"rt={d['selected_runtime']} class={d['selected_model_class']} mode={d['execution_mode']} "
              f"approval={d['human_approval_required']}")
        for p in problems:
            print(f"       - {p}")
    print("routing self-test:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if len(argv) > 1 and argv[1] == "--selftest":
        return selftest()
    if len(argv) > 1:
        inp = json.loads(argv[1])
        print(json.dumps(route(inp), ensure_ascii=False, indent=2))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
