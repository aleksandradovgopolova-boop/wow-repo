---
description: Workflow ADOPTION — Довести выпущенную функцию до активации и удержания; оценить фактический эффект против baseline.
---
# ai-adoption — Довести выпущенную функцию до активации и удержания; оценить фактический эффект против baseline.

Сгенерировано из registry/workflows.yaml — НЕ редактировать вручную
(перегенерация: python3 tools/generate_runtime.py).

## Что делает
Проводит задачу по workflow **ADOPTION** (native / минимум
sequential). Пользователь описывает задачу обычным языком;
стадии и проверки ниже выполняются по порядку, состояние — в TaskState.

## Стадии (owner и роль)
1. intake — intake-classifier (исполнитель): Единая точка приёма запроса на естественном языке. Превращает свободную формулировку в machine-readable intake и классификацию (тип задачи, размер, риск), затем предлагает workflow. Пользователь не выбирает агентов, команды и workflow вручную — это делает классификатор
2. launch-readiness — adoption-manager (исполнитель): Стадия adoption: активация (aha), онбординг, retention, петля обратной связи, Post-Launch Review; инсайты возвращаются в discovery. Owner workflow ADOPTION
3. analytics-review — analytics-reviewer (судья (read-only)): Независимая проверка аналитики: tracking plan, event schema, воронки, PII, dashboard-спецификация. Гейт analytics_readiness
4. adoption-plan — adoption-manager (исполнитель): Стадия adoption: активация (aha), онбординг, retention, петля обратной связи, Post-Launch Review; инсайты возвращаются в discovery. Owner workflow ADOPTION
5. user-docs — documentation-steward (исполнитель): Обновляет только релевантные источники истины, устраняет расхождения, помечает устаревшие документы и готовит handoff
6. docs-review — documentation-reviewer (судья (read-only)): Независимая проверка, что документация обновлена вместе с изменением и пригодна читателю. Поддерживает гейт documentation_updated
7. feedback-loop — adoption-manager (исполнитель): Стадия adoption: активация (aha), онбординг, retention, петля обратной связи, Post-Launch Review; инсайты возвращаются в discovery. Owner workflow ADOPTION
8. post-launch-review — adoption-manager (исполнитель): Стадия adoption: активация (aha), онбординг, retention, петля обратной связи, Post-Launch Review; инсайты возвращаются в discovery. Owner workflow ADOPTION
9. review — product-reviewer (судья (read-only)): Независимая проверка продуктовых артефактов Discovery/Definition: проблема, JTBD, гипотезы, метрики успеха, PRD. Гейт discovery_completeness
10. memory-capture — repository-memory-curator (исполнитель): Превращает завершённые задачи и инциденты в долговременные знания репозитория

## Обязательные артефакты
- intake
- LaunchPlan
- AdoptionPlan
- FeedbackLoop
- PostLaunchReview
- TaskState
- VerificationEvidence

## Blocking gates
intake_completeness, analytics_design_readiness, documentation_updated

## Правила
- Writer и judge разделены; judge read-only к проверяемому артефакту.
- Judge получает только опубликованные артефакты (handoff), не рассуждения автора.
- Состояние в TaskState — возобновление после прерывания сессии.
- Human approval: {"human_approval_required": "conditional", "when": ["rollback_decision"]}.
