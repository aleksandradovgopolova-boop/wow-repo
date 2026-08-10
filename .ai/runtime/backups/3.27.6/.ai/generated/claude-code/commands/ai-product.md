---
description: Workflow PRODUCT — Продуктовая задача от проблемы к ценности и валидации.
---
# ai-product — Продуктовая задача от проблемы к ценности и валидации.

Сгенерировано из registry/workflows.yaml — НЕ редактировать вручную
(перегенерация: python3 tools/generate_runtime.py).

## Что делает
Проводит задачу по workflow **PRODUCT** (orchestrated / минимум
sequential). Пользователь описывает задачу обычным языком;
стадии и проверки ниже выполняются по порядку, состояние — в TaskState.

## Стадии (owner и роль)
1. problem — product-manager (исполнитель): Формулирует продуктовую проблему, целевого пользователя, ожидаемый эффект, приоритет и границы решения
2. users — product-analyst (исполнитель): Определяет метрики, события, сегменты, методику измерения и дизайн оценки эффекта изменения
3. value — product-manager (исполнитель): Формулирует продуктовую проблему, целевого пользователя, ожидаемый эффект, приоритет и границы решения
4. hypotheses — experiment-designer (исполнитель): Проектирует проверку продуктовой гипотезы до масштабной разработки
5. discovery-review — product-reviewer (судья (read-only)): Независимая проверка продуктовых артефактов Discovery/Definition: проблема, JTBD, гипотезы, метрики успеха, PRD. Гейт discovery_completeness
6. requirements — requirements-writer (исполнитель): Единый писатель требований. Формулирует проверяемые требования и acceptance-сценарии на основе проверенного контекста. Отделён от проверяющего (requirements-reviewer) — writer и judge не совмещаются
7. specification — solution-architect (исполнитель): Проектирует варианты решения, оценивает системные риски и фиксирует архитектурное решение без преждевременной детализации кода
8. plan — task-planner (исполнитель): Преобразует подтверждённый контекст и требования в исполнимый план с ограниченным scope, зависимостями, проверками и точками согласования
9. plan-critique — plan-reviewer (судья (read-only)): Независимо и read-only критикует план до начала выполнения (стадия PLAN CRITIQUE). Не изменяет план — возвращает заключение. Writer плана (task-planner) и judge плана (plan-reviewer) разделены
10. implementation — implementation-integrator (исполнитель): Объединяет результаты нескольких исполнителей в согласованное изменение и отвечает за интеграционную целостность
11. validation — final-verifier (судья (read-only)): Независимо проверяет, что первоначальная цель задачи достигнута, критерии приёмки доказаны, scope не нарушен, а задача действительно готова к завершению
12. stakeholder-handoff — documentation-steward (исполнитель): Обновляет только релевантные источники истины, устраняет расхождения, помечает устаревшие документы и готовит handoff
13. memory-capture — repository-memory-curator (исполнитель): Превращает завершённые задачи и инциденты в долговременные знания репозитория

## Обязательные артефакты
- intake
- requirements
- specification
- TaskPlan
- VerificationEvidence

## Blocking gates
intake_completeness, concurrency_preflight, discovery_completeness, requirements, specification, event_contract_consistency, plan_readiness, implementation_verification, code_review, security, architecture_review, deploy_readiness, stakeholder_readiness

## Правила
- Writer и judge разделены; judge read-only к проверяемому артефакту.
- Judge получает только опубликованные артефакты (handoff), не рассуждения автора.
- Состояние в TaskState — возобновление после прерывания сессии.
- Human approval: {"human_approval_required": true, "stage": "stakeholder-handoff"}.
