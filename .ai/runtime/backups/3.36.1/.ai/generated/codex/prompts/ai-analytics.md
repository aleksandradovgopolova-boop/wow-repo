# ai-analytics — Аналитика функции — метрики, события, tracking plan, dashboard-спецификация.

Сгенерировано из registry/workflows.yaml — НЕ редактировать вручную
(перегенерация: python3 tools/generate_runtime.py).

## Что делает
Проводит задачу по workflow **ANALYTICS** (native / минимум
sequential). Пользователь описывает задачу обычным языком;
стадии и проверки ниже выполняются по порядку, состояние — в TaskState.

## Стадии (owner и роль)
1. intake — intake-classifier (исполнитель): Единая точка приёма запроса на естественном языке. Превращает свободную формулировку в machine-readable intake и классификацию (тип задачи, размер, риск), затем предлагает workflow. Пользователь не выбирает агентов, команды и workflow вручную — это делает классификатор
2. metrics-definition — product-analyst (исполнитель): Определяет метрики, события, сегменты, методику измерения и дизайн оценки эффекта изменения
3. event-design — product-analyst (исполнитель): Определяет метрики, события, сегменты, методику измерения и дизайн оценки эффекта изменения
4. dashboard-spec — product-analyst (исполнитель): Определяет метрики, события, сегменты, методику измерения и дизайн оценки эффекта изменения
5. analytics-review — analytics-reviewer (судья (read-only)): Независимая проверка аналитики: tracking plan, event schema, воронки, PII, dashboard-спецификация. Гейт analytics_readiness
6. implementation — implementation-integrator (исполнитель): Объединяет результаты нескольких исполнителей в согласованное изменение и отвечает за интеграционную целостность
7. verify — final-verifier (судья (read-only)): Независимо проверяет, что первоначальная цель задачи достигнута, критерии приёмки доказаны, scope не нарушен, а задача действительно готова к завершению
8. memory-capture — repository-memory-curator (исполнитель): Превращает завершённые задачи и инциденты в долговременные знания репозитория

## Обязательные артефакты
- intake
- TrackingPlan
- EventSchema
- DashboardSpec
- TaskState
- VerificationEvidence

## Blocking gates
intake_completeness, analytics_design_readiness, event_contract_consistency, implementation_verification

## Правила
- Writer и judge разделены; judge read-only к проверяемому артефакту.
- Judge получает только опубликованные артефакты (handoff), не рассуждения автора.
- Состояние в TaskState — возобновление после прерывания сессии.
- Human approval: {"human_approval_required": false}.
