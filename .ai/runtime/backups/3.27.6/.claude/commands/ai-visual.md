---
description: Workflow VISUAL — Пользовательская функция с UI — flow, состояния, дизайн-система, доступность.
---
# ai-visual — Пользовательская функция с UI — flow, состояния, дизайн-система, доступность.

Сгенерировано из registry/workflows.yaml — НЕ редактировать вручную
(перегенерация: python3 tools/generate_runtime.py).

## Что делает
Проводит задачу по workflow **VISUAL** (native / минимум
sequential). Пользователь описывает задачу обычным языком;
стадии и проверки ниже выполняются по порядку, состояние — в TaskState.

## Стадии (owner и роль)
1. intake — intake-classifier (исполнитель): Единая точка приёма запроса на естественном языке. Превращает свободную формулировку в machine-readable intake и классификацию (тип задачи, размер, риск), затем предлагает workflow. Пользователь не выбирает агентов, команды и workflow вручную — это делает классификатор
2. ux-flow — ui-ux-designer (исполнитель): Проектирует пользовательский сценарий и интерфейс на основе существующей дизайн-системы, включая loading, empty, error, success и permission states
3. states — ui-ux-designer (исполнитель): Проектирует пользовательский сценарий и интерфейс на основе существующей дизайн-системы, включая loading, empty, error, success и permission states
4. design-review — ux-reviewer (судья (read-only)): Независимая проверка UX: flow, состояния Empty/Loading/Error/Success, эвристики Nielsen (rules/design/ux-heuristics.yaml), copy. Гейт ux_review
5. design-system-review — design-system-reviewer (судья (read-only)): Проверка соответствия дизайн-системе: существующие компоненты и токены, новые — только обоснованно (rules/design/design-system-checklist.yaml). Гейт design_system_usage
6. accessibility-review — accessibility-reviewer (судья (read-only)): Проверяет semantic structure, keyboard navigation, focus, labels, errors, contrast, scaling и screen reader flow. Автоматический сканер считается только одной из проверок
7. implementation — implementation-integrator (исполнитель): Объединяет результаты нескольких исполнителей в согласованное изменение и отвечает за интеграционную целостность
8. verify — final-verifier (судья (read-only)): Независимо проверяет, что первоначальная цель задачи достигнута, критерии приёмки доказаны, scope не нарушен, а задача действительно готова к завершению
9. memory-capture — repository-memory-curator (исполнитель): Превращает завершённые задачи и инциденты в долговременные знания репозитория

## Обязательные артефакты
- intake
- UXFlow
- ScreenStates
- DesignReview
- TaskState
- VerificationEvidence

## Blocking gates
intake_completeness, concurrency_preflight, ux_review, design_system_usage, accessibility_review, visual_regression, implementation_verification

## Правила
- Writer и judge разделены; judge read-only к проверяемому артефакту.
- Judge получает только опубликованные артефакты (handoff), не рассуждения автора.
- Состояние в TaskState — возобновление после прерывания сессии.
- Human approval: {"human_approval_required": false, "escalate_if": ["protected_paths"]}.
