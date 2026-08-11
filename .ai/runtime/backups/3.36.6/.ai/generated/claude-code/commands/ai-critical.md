---
description: Workflow CRITICAL — Критическое/необратимое изменение с максимальной строгостью — high/critical risk, hotfix, security-sensitive. Цель critical-эскалации маршрутизатора.
---
# ai-critical — Критическое/необратимое изменение с максимальной строгостью — high/critical risk, hotfix, security-sensitive. Цель critical-эскалации маршрутизатора.

Сгенерировано из registry/workflows.yaml — НЕ редактировать вручную
(перегенерация: python3 tools/generate_runtime.py).

## Что делает
Проводит задачу по workflow **CRITICAL** (orchestrated / минимум
sequential). Пользователь описывает задачу обычным языком;
стадии и проверки ниже выполняются по порядку, состояние — в TaskState.

## Стадии (owner и роль)
1. intake — intake-classifier (исполнитель): Единая точка приёма запроса на естественном языке. Превращает свободную формулировку в machine-readable intake и классификацию (тип задачи, размер, риск), затем предлагает workflow. Пользователь не выбирает агентов, команды и workflow вручную — это делает классификатор
2. risk-assessment — solution-architect (исполнитель): Проектирует варианты решения, оценивает системные риски и фиксирует архитектурное решение без преждевременной детализации кода
3. plan — task-planner (исполнитель): Преобразует подтверждённый контекст и требования в исполнимый план с ограниченным scope, зависимостями, проверками и точками согласования
4. plan-critique — plan-reviewer (судья (read-only)): Независимо и read-only критикует план до начала выполнения (стадия PLAN CRITIQUE). Не изменяет план — возвращает заключение. Writer плана (task-planner) и judge плана (plan-reviewer) разделены
5. implementation — implementation-integrator (исполнитель): Объединяет результаты нескольких исполнителей в согласованное изменение и отвечает за интеграционную целостность
6. verify — final-verifier (судья (read-only)): Независимо проверяет, что первоначальная цель задачи достигнута, критерии приёмки доказаны, scope не нарушен, а задача действительно готова к завершению
7. security-review — security-reviewer (судья (read-only)): Проверяет изменения, затрагивающие authentication, authorization, персональные данные, секреты, внешние интеграции и границы доверия
8. code-review — code-reviewer (судья (read-only)): Проводит независимый review diff относительно задачи, утверждённого плана, архитектуры и правил
9. human-approval — product-manager (исполнитель): Формулирует продуктовую проблему, целевого пользователя, ожидаемый эффект, приоритет и границы решения
10. memory-capture — repository-memory-curator (исполнитель): Превращает завершённые задачи и инциденты в долговременные знания репозитория

## Обязательные артефакты
- intake
- RiskAssessment
- TaskPlan
- TaskState
- VerificationEvidence
- SecurityReview

## Blocking gates
intake_completeness, concurrency_preflight, plan_readiness, implementation_verification, security, code_review, architecture_review, deploy_readiness

## Правила
- Writer и judge разделены; judge read-only к проверяемому артефакту.
- Judge получает только опубликованные артефакты (handoff), не рассуждения автора.
- Состояние в TaskState — возобновление после прерывания сессии.
- Human approval: {"human_approval_required": true, "stage": "human-approval"}.
