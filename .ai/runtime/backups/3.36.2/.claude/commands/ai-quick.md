---
description: Workflow QUICK — Малое локальное изменение с низким риском.
---
# ai-quick — Малое локальное изменение с низким риском.

Сгенерировано из registry/workflows.yaml — НЕ редактировать вручную
(перегенерация: python3 tools/generate_runtime.py).

## Что делает
Проводит задачу по workflow **QUICK** (native / минимум
sequential). Пользователь описывает задачу обычным языком;
стадии и проверки ниже выполняются по порядку, состояние — в TaskState.

## Стадии (owner и роль)
1. intake — intake-classifier (исполнитель): Единая точка приёма запроса на естественном языке. Превращает свободную формулировку в machine-readable intake и классификацию (тип задачи, размер, риск), затем предлагает workflow. Пользователь не выбирает агентов, команды и workflow вручную — это делает классификатор
2. local-change — implementation-integrator (исполнитель): Объединяет результаты нескольких исполнителей в согласованное изменение и отвечает за интеграционную целостность
3. local-verify — final-verifier (судья (read-only)): Независимо проверяет, что первоначальная цель задачи достигнута, критерии приёмки доказаны, scope не нарушен, а задача действительно готова к завершению
4. result — documentation-steward (исполнитель): Обновляет только релевантные источники истины, устраняет расхождения, помечает устаревшие документы и готовит handoff

## Обязательные артефакты
- intake
- TaskState
- VerificationEvidence

## Blocking gates
intake_completeness, concurrency_preflight, implementation_verification

## Правила
- Writer и judge разделены; judge read-only к проверяемому артефакту.
- Judge получает только опубликованные артефакты (handoff), не рассуждения автора.
- Состояние в TaskState — возобновление после прерывания сессии.
- Human approval: {"human_approval_required": false, "escalate_if": ["protected_paths", "destructive"]}.
