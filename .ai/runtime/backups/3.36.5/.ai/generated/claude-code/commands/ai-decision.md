---
description: Workflow DECISION — Значимое решение по recommendation-first — человек формулирует позицию, система проверяет мышление, необратимое эскалирует.
---
# ai-decision — Значимое решение по recommendation-first — человек формулирует позицию, система проверяет мышление, необратимое эскалирует.

Сгенерировано из registry/workflows.yaml — НЕ редактировать вручную
(перегенерация: python3 tools/generate_runtime.py).

## Что делает
Проводит задачу по workflow **DECISION** (native / минимум
sequential). Пользователь описывает задачу обычным языком;
стадии и проверки ниже выполняются по порядку, состояние — в TaskState.

## Стадии (owner и роль)
1. intake — intake-classifier (исполнитель): Единая точка приёма запроса на естественном языке. Превращает свободную формулировку в machine-readable intake и классификацию (тип задачи, размер, риск), затем предлагает workflow. Пользователь не выбирает агентов, команды и workflow вручную — это делает классификатор
2. recommendation — product-manager (исполнитель): Формулирует продуктовую проблему, целевого пользователя, ожидаемый эффект, приоритет и границы решения
3. principle-review — product-reviewer (судья (read-only)): Независимая проверка продуктовых артефактов Discovery/Definition: проблема, JTBD, гипотезы, метрики успеха, PRD. Гейт discovery_completeness
4. one-way-door-brief — product-manager (исполнитель): Формулирует продуктовую проблему, целевого пользователя, ожидаемый эффект, приоритет и границы решения
5. decision-record — documentation-steward (исполнитель): Обновляет только релевантные источники истины, устраняет расхождения, помечает устаревшие документы и готовит handoff
6. outcome-review — product-analyst (исполнитель): Определяет метрики, события, сегменты, методику измерения и дизайн оценки эффекта изменения
7. memory-capture — repository-memory-curator (исполнитель): Превращает завершённые задачи и инциденты в долговременные знания репозитория

## Обязательные артефакты
- intake
- DecisionEpisode
- VerificationEvidence

## Blocking gates
intake_completeness, decision_quality

## Правила
- Writer и judge разделены; judge read-only к проверяемому артефакту.
- Judge получает только опубликованные артефакты (handoff), не рассуждения автора.
- Состояние в TaskState — возобновление после прерывания сессии.
- Human approval: {"human_approval_required": true, "applies_to": ["one-way-door-brief"]}.
