---
description: Workflow AI_FEATURE — AI-возможность продукта — качество, скорость и стоимость ИИ-части в целевом сценарии, eval-driven.
---
# ai-ai_feature — AI-возможность продукта — качество, скорость и стоимость ИИ-части в целевом сценарии, eval-driven.

Сгенерировано из registry/workflows.yaml — НЕ редактировать вручную
(перегенерация: python3 tools/generate_runtime.py).

## Что делает
Проводит задачу по workflow **AI_FEATURE** (native / минимум
sequential). Пользователь описывает задачу обычным языком;
стадии и проверки ниже выполняются по порядку, состояние — в TaskState.

## Стадии (owner и роль)
1. intake — intake-classifier (исполнитель): Единая точка приёма запроса на естественном языке. Превращает свободную формулировку в machine-readable intake и классификацию (тип задачи, размер, риск), затем предлагает workflow. Пользователь не выбирает агентов, команды и workflow вручную — это делает классификатор
2. target-scenario — llm-architect (исполнитель): Архитектура AI-части продукта под целевой сценарий: model_class через routing, контекстная стратегия, бюджеты качества/latency/стоимости, деградация. Владелец target-scenario в AI_FEATURE
3. eval-dataset — ai-feature-engineer (исполнитель): Строит AI-часть eval-driven: golden set до реализации, промпты/RAG/tool use как код, итерации меряются прогоном на наборе против бюджетов spec'а
4. implementation — ai-feature-engineer (исполнитель): Строит AI-часть eval-driven: golden set до реализации, промпты/RAG/tool use как код, итерации меряются прогоном на наборе против бюджетов spec'а
5. offline-evals — ai-evaluator (судья (read-only)): Оценка AI-фич продукта против success criteria: eval-наборы, LLM-as-judge с валидацией, guardrails, regression при смене модели/промпта. Гейт ai_eval
6. red-team — ai-red-teamer (судья (read-only)): Адверсариальная проверка AI-фичи по rules/ai/red-team-checklist.yaml (OWASP LLM Top 10): инъекции, jailbreak, утечки PII/промпта, границы агентности. Гейт ai_red_team
7. verify — final-verifier (судья (read-only)): Независимо проверяет, что первоначальная цель задачи достигнута, критерии приёмки доказаны, scope не нарушен, а задача действительно готова к завершению
8. memory-capture — repository-memory-curator (исполнитель): Превращает завершённые задачи и инциденты в долговременные знания репозитория

## Обязательные артефакты
- intake
- AIFeatureSpec
- GoldenDataset
- AIFeatureEvalPlan
- RedTeamReport
- TaskState
- VerificationEvidence

## Blocking gates
intake_completeness, concurrency_preflight, ai_eval, ai_red_team, event_contract_consistency, implementation_verification, architecture_review, deploy_readiness

## Правила
- Writer и judge разделены; judge read-only к проверяемому артефакту.
- Judge получает только опубликованные артефакты (handoff), не рассуждения автора.
- Состояние в TaskState — возобновление после прерывания сессии.
- Human approval: {"human_approval_required": "conditional", "when": ["guardrail_threshold_change", "external_provider_for_confidential_data"]}.
