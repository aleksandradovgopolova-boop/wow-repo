#!/usr/bin/env python3
"""engineering_advisor.py (v3.23 Engineering Advisor) — слой «как лучше сделать», а не очередной
набор проверок. Композиция из существующих модулей: environment_map, deploy_readiness,
project_detector, commit_policy, branch_policy, economic_preflight.

Три типа рекомендаций:
1. Environment Recommendation — какие окружения использовать (local → preview → prod)
2. Delivery Plan — branch strategy, commit boundaries, affected tests, deploy, rollback
3. Economic Alternatives — не делать / настройка / переиспользование / небольшое изменение /
   новая функция / новая зависимость / новый сервис

Следует паттерну cost_method.py: собирает {priority, category, advice, source} tuples.
Advise, не block — рекомендации, а не требования.

CLI:  engineering_advisor.py <child_root> [--task-type TYPE] [--json]
      engineering_advisor.py --selftest
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402
def _env_recommendation(child_root):
    """Layer 1: какие окружения использовать."""
    from ai_ops_kit.engops import environment_map
    from ai_ops_kit.engops import deploy_readiness
    recs = []
    try:
        env_map = environment_map.assess(child_root)
        deploy = deploy_readiness.assess(child_root)
        maturity = deploy.get("deploy_maturity", "absent")
        envs = env_map.get("environments", [])
        kinds = {e.get("kind") for e in envs if e.get("kind")}
        # Рекомендация progression
        if maturity == "absent":
            recs.append({
                "priority": 1, "category": "environment",
                "advice": "Проект не имеет declared окружений. Для библиотеки/CLI это нормально. "
                          "Если это сервис — добавьте хотя бы local + production в .ai-ops.yaml.",
                "source": "deploy_readiness",
            })
        elif maturity == "configured":
            recs.append({
                "priority": 1, "category": "environment",
                "advice": f"Окружения обнаружены ({', '.join(kinds) or 'unknown'}), но исполняемого "
                          "пути доставки нет. Добавьте CI deploy job или deploy script.",
                "source": "deploy_readiness",
            })
        elif maturity == "runnable":
            if "preview" not in kinds and "staging" not in kinds:
                recs.append({
                    "priority": 2, "category": "environment",
                    "advice": "Доставка runnable, но нет preview/staging. Рассмотрите PR-based "
                              "preview окружения для безопасной проверки перед production.",
                    "source": "environment_map",
                })
            if "production" in kinds:
                prod_envs = [e for e in envs if e.get("kind") == "production"]
                for pe in prod_envs:
                    if not pe.get("approvers"):
                        recs.append({
                            "priority": 1, "category": "environment",
                            "advice": f"Production окружение '{pe.get('name')}' не имеет approvers. "
                                      "Добавьте минимум одного approver для безопасности.",
                            "source": "environment_map",
                        })
        elif maturity == "verified":
            recs.append({
                "priority": 3, "category": "environment",
                "advice": "Доставка verified — окружения и rollback подтверждены. "
                          "Текущая конфигурация безопасна.",
                "source": "deploy_readiness",
            })
        # Findings
        for f in env_map.get("findings", []):
            recs.append({
                "priority": 1, "category": "environment",
                "advice": f"{f.get('rule')}: {f.get('detail', '')}",
                "source": "environment_map",
            })
    except Exception as e:  # noqa: BLE001
        recs.append({
            "priority": 3, "category": "environment",
            "advice": f"Environment assessment unavailable: {e}",
            "source": "error",
        })
    return recs


def _delivery_plan(child_root, task_type=None):
    """Layer 2: delivery plan — branch, commits, tests, deploy, rollback."""
    from ai_ops_kit.shared import project_detector
    from ai_ops_kit.engops import commit_policy
    from ai_ops_kit.engops import branch_policy
    recs = []
    try:
        # единая точка детекции: свежий кеш профиля или пере-детект при изменившихся манифестах
        profile = project_detector.load_or_detect(child_root)
        stacks = profile.get("stacks", [])
        # Branch strategy
        recs.append({
            "priority": 2, "category": "delivery_plan",
            "advice": "Branch strategy: создавайте ветку `ai-ops/<workitem-id>` от свежей main. "
                      "Один WorkItem на ветку. Доставка только через PR (не direct commit).",
            "source": "branch_policy",
        })
        # Commit boundaries
        recs.append({
            "priority": 2, "category": "delivery_plan",
            "advice": "Commit boundaries: один коммит = один revertible шаг. Не смешивайте зоны "
                      "(kit-managed + product). Включайте WorkItem ID в сообщение. "
                      "Не коммитьте .env, ключи, runtime artifacts.",
            "source": "commit_policy",
        })
        # Affected tests
        for stack in stacks:
            lang = stack.get("language", "unknown")
            cmds = stack.get("commands", {})
            test_cmd = cmds.get("test")
            if test_cmd:
                recs.append({
                    "priority": 2, "category": "delivery_plan",
                    "advice": f"Тесты ({lang}): запускайте `{test_cmd}` перед каждым коммитом. "
                              "Affected tests определяются автоматически при прогоне движка.",
                    "source": "project_detector",
                })
            else:
                recs.append({
                    "priority": 3, "category": "delivery_plan",
                    "advice": f"Тесты ({lang}): тестовая команда не обнаружена. "
                              "Рассмотрите добавление тестов для критичного кода.",
                    "source": "project_detector",
                })
        # Deploy/rollback
        from ai_ops_kit.engops import deploy_readiness
        deploy = deploy_readiness.assess(child_root)
        maturity = deploy.get("deploy_maturity", "absent")
        if maturity in ("runnable", "verified"):
            if deploy.get("rollback_declared"):
                recs.append({
                    "priority": 2, "category": "delivery_plan",
                    "advice": "Rollback: объявлен. Перед merge убедитесь, что rollback работает "
                              "на актуальной версии. Post-release smoke — обязателен.",
                    "source": "deploy_readiness",
                })
            else:
                recs.append({
                    "priority": 1, "category": "delivery_plan",
                    "advice": "Rollback: не объявлен. Добавьте rollback script/command. "
                              "Без отката verified недостижим.",
                    "source": "deploy_readiness",
                })
        # Feature flag
        tt = (task_type or "").upper()
        if tt in ("PRODUCT", "CRITICAL"):
            recs.append({
                "priority": 2, "category": "delivery_plan",
                "advice": "Feature flag: для продуктовой/критичной задачи рассмотрите feature flag "
                          "для постепенного rollout и быстрого отката без revert.",
                "source": "engineering_advisor",
            })
    except Exception as e:  # noqa: BLE001
        recs.append({
            "priority": 3, "category": "delivery_plan",
            "advice": f"Delivery plan assessment unavailable: {e}",
            "source": "error",
        })
    return recs


def _economic_alternatives(child_root, task_type=None):
    """Layer 3: экономические альтернативы — не делать / настройка / переиспользование / build."""
    from ai_ops_kit.gates import economic_preflight
    recs = []
    try:
        est, verdict = economic_preflight.assess(child_root)
        status = est.get("status", "unavailable")
        cost_max = est.get("cost_max")
        note = est.get("note", "")
        if status == "measured_history" and cost_max is not None:
            recs.append({
                "priority": 2, "category": "economic_alternatives",
                "advice": f"Оценка стоимости: медиана {est.get('cost_median', '?')}, "
                          f"худший {cost_max} ({est.get('sample_tasks', 0)} задач в истории). "
                          "Перед написанием кода проверьте: (1) можно ли достичь цели настройкой "
                          "без нового кода; (2) есть ли переиспользуемый компонент; (3) можно ли "
                          "сузить scope до меньшего изменения.",
                "source": "economic_preflight",
            })
        elif status == "estimated_lower_bound":
            recs.append({
                "priority": 2, "category": "economic_alternatives",
                "advice": f"Оценка стоимости: нижняя граница (часть вызовов без cost data). "
                          f"{note} Рассмотрите более узкий scope для снижения риска.",
                "source": "economic_preflight",
            })
        elif status == "unavailable":
            recs.append({
                "priority": 3, "category": "economic_alternatives",
                "advice": "История расходов недоступна (первый прогон или нет ledger). "
                          "Оценка невозможна — это не блок, но и не гарантия. "
                          "Начните с QUICK-задачи для калибровки.",
                "source": "economic_preflight",
            })
        # Verdict-based advice
        v = verdict.get("verdict", "proceed")
        if v == "confirm_required":
            recs.append({
                "priority": 1, "category": "economic_alternatives",
                "advice": "Экономический preflight требует подтверждения: стоимость может "
                          "превысить лимит. Проверьте, можно ли сузить scope или использовать "
                          "дешёвую модель для части работы.",
                "source": "economic_preflight",
            })
        elif v == "block":
            recs.append({
                "priority": 1, "category": "economic_alternatives",
                "advice": "Экономический preflight блокирует: стоимость превышает лимит. "
                          "Альтернативы: (1) не делать сейчас; (2) настройка вместо кода; "
                          "(3) переиспользование существующего; (4) меньший scope.",
                "source": "economic_preflight",
            })
    except Exception as e:  # noqa: BLE001
        recs.append({
            "priority": 3, "category": "economic_alternatives",
            "advice": f"Economic assessment unavailable: {e}",
            "source": "error",
        })
    return recs


def advise(child_root, task_type=None):
    """Полный advisory: environment + delivery_plan + economic_alternatives.
    -> {kind, recommendations[], summary}."""
    recs = []
    recs.extend(_env_recommendation(child_root))
    recs.extend(_delivery_plan(child_root, task_type))
    recs.extend(_economic_alternatives(child_root, task_type))
    # Sort by priority
    recs.sort(key=lambda r: r.get("priority", 3))
    # Summary
    p1 = sum(1 for r in recs if r.get("priority") == 1)
    p2 = sum(1 for r in recs if r.get("priority") == 2)
    summary = f"{len(recs)} рекомендаций"
    if p1:
        summary += f" ({p1} критических)"
    if p2:
        summary += f" ({p2} важных)"
    return {
        "kind": "EngineeringAdvice",
        "schema_version": 1,
        "repository": str(Path(child_root).resolve().name),
        "task_type": task_type,
        "recommendations": recs,
        "summary": summary,
    }


def check(data):
    """Валидация формы."""
    e = []
    if data.get("kind") != "EngineeringAdvice":
        e.append(f"kind must be EngineeringAdvice, got {data.get('kind')}")
    if not isinstance(data.get("recommendations"), list):
        e.append("recommendations must be a list")
    else:
        for i, r in enumerate(data["recommendations"]):
            if "priority" not in r:
                e.append(f"recommendation[{i}] missing priority")
            if "category" not in r:
                e.append(f"recommendation[{i}] missing category")
            if "advice" not in r:
                e.append(f"recommendation[{i}] missing advice")
    return e


if __name__ == "__main__":
    # Ветки `--selftest` здесь нет (ревизия 2026-08-11): функция удалена в v3.30 вместе с
    # переносом селфтестов в pytest, а вызов остался и мог только упасть с `NameError`.
    # CLI
    child_root = Path(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else Path.cwd()
    task_type = None
    as_json = "--json" in sys.argv
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--task-type" and i < len(sys.argv) - 1:
            task_type = sys.argv[i + 1]
    result = advise(str(child_root), task_type)
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"ENGINEERING ADVISOR: {result['summary']}")
        print(f"Repository: {result['repository']}")
        if task_type:
            print(f"Task type: {task_type}")
        print()
        for r in result["recommendations"]:
            p = r.get("priority", 3)
            marker = "⚠" if p == 1 else "·" if p == 2 else " "
            print(f"  {marker} [{r.get('category')}] {r['advice']}")
            print(f"    (source: {r.get('source')})")
