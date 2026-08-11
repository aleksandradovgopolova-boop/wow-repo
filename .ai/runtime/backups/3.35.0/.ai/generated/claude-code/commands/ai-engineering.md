---
description: Workflow ENGINEERING — Инженерная задача с требованиями, спецификацией и ревью.
---
# ai-engineering — Инженерная задача с требованиями, спецификацией и ревью.

Сгенерировано из registry/workflows.yaml — НЕ редактировать вручную
(перегенерация: python3 tools/generate_runtime.py).

## Что делает
Проводит задачу по workflow **ENGINEERING** (orchestrated / минимум
sequential). Пользователь описывает задачу обычным языком;
стадии и проверки ниже выполняются по порядку, состояние — в TaskState.

## Стадии (owner и роль)
1. requirements — requirements-writer (исполнитель): Единый писатель требований. Формулирует проверяемые требования и acceptance-сценарии на основе проверенного контекста. Отделён от проверяющего (requirements-reviewer) — writer и judge не совмещаются
2. requirements-review — requirements-reviewer (судья (read-only)): Критически проверяет требования до разработки
3. specification — solution-architect (исполнитель): Проектирует варианты решения, оценивает системные риски и фиксирует архитектурное решение без преждевременной детализации кода
4. plan — task-planner (исполнитель): Преобразует подтверждённый контекст и требования в исполнимый план с ограниченным scope, зависимостями, проверками и точками согласования
5. plan-critique — plan-reviewer (судья (read-only)): Независимо и read-only критикует план до начала выполнения (стадия PLAN CRITIQUE). Не изменяет план — возвращает заключение. Writer плана (task-planner) и judge плана (plan-reviewer) разделены
6. implementation — implementation-integrator (исполнитель): Объединяет результаты нескольких исполнителей в согласованное изменение и отвечает за интеграционную целостность
7. verify — final-verifier (судья (read-only)): Независимо проверяет, что первоначальная цель задачи достигнута, критерии приёмки доказаны, scope не нарушен, а задача действительно готова к завершению
8. review — code-reviewer (судья (read-only)): Проводит независимый review diff относительно задачи, утверждённого плана, архитектуры и правил
9. memory-capture — repository-memory-curator (исполнитель): Превращает завершённые задачи и инциденты в долговременные знания репозитория

## Обязательные артефакты
- intake
- requirements
- specification
- TaskPlan
- TaskState
- VerificationEvidence

## Blocking gates
intake_completeness, concurrency_preflight, requirements, specification, event_contract_consistency, plan_readiness, implementation_verification, code_review, security, architecture_review, deploy_readiness

## Правила
- Writer и judge разделены; judge read-only к проверяемому артефакту.
- Judge получает только опубликованные артефакты (handoff), не рассуждения автора.
- Состояние в TaskState — возобновление после прерывания сессии.
- Human approval: {"human_approval_required": "conditional", "when": ["protected_paths", "medium_risk_release"]}.
