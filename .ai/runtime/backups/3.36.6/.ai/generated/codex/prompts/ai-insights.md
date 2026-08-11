# ai-insights — Непрерывное улучшение — из данных после релиза к инсайтам и гипотезам следующего Discovery.

Сгенерировано из registry/workflows.yaml — НЕ редактировать вручную
(перегенерация: python3 tools/generate_runtime.py).

## Что делает
Проводит задачу по workflow **INSIGHTS** (native / минимум
sequential). Пользователь описывает задачу обычным языком;
стадии и проверки ниже выполняются по порядку, состояние — в TaskState.

## Стадии (owner и роль)
1. intake — intake-classifier (исполнитель): Единая точка приёма запроса на естественном языке. Превращает свободную формулировку в machine-readable intake и классификацию (тип задачи, размер, риск), затем предлагает workflow. Пользователь не выбирает агентов, команды и workflow вручную — это делает классификатор
2. data-collection — product-analyst (исполнитель): Определяет метрики, события, сегменты, методику измерения и дизайн оценки эффекта изменения
3. health-report — product-analyst (исполнитель): Определяет метрики, события, сегменты, методику измерения и дизайн оценки эффекта изменения
4. insight-synthesis — product-analyst (исполнитель): Определяет метрики, события, сегменты, методику измерения и дизайн оценки эффекта изменения
5. insight-review — product-reviewer (судья (read-only)): Независимая проверка продуктовых артефактов Discovery/Definition: проблема, JTBD, гипотезы, метрики успеха, PRD. Гейт discovery_completeness
6. hypotheses-for-discovery — experiment-designer (исполнитель): Проектирует проверку продуктовой гипотезы до масштабной разработки
7. memory-capture — repository-memory-curator (исполнитель): Превращает завершённые задачи и инциденты в долговременные знания репозитория

## Обязательные артефакты
- intake
- ProductHealthReport
- insights
- hypotheses
- TaskState
- VerificationEvidence

## Blocking gates
intake_completeness, evidence

## Правила
- Writer и judge разделены; judge read-only к проверяемому артефакту.
- Judge получает только опубликованные артефакты (handoff), не рассуждения автора.
- Состояние в TaskState — возобновление после прерывания сессии.
- Human approval: {"human_approval_required": false}.
