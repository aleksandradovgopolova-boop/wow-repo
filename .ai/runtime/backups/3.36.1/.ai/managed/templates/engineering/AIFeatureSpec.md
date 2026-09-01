# AI Feature Spec

<!-- Архитектура AI-возможности под целевой сценарий. Владелец — llm-architect.
     Бюджеты отсюда — вход для AIFeatureEvalPlan (гейт ai_eval) и online-мониторинга. -->

## Target scenario (измеримо)
<!-- вход, ожидаемый выход, допустимая ошибка, кто и как страдает при неверном ответе -->
## AI surface
<!-- где именно LLM/агент: генерация / классификация / извлечение / агентный flow / RAG -->
## Model class & provider (через routing кита, не хардкод)
<!-- model_class + обоснование; конфиденциальность и residency — по routing-policy -->
## Context strategy
<!-- RAG (источники, chunking, свежесть) / few-shot / system prompt / tools; что в контекст НЕ попадает -->
## Budgets (числа, не прилагательные)
| Измерение | Порог | Как меряем |
|---|---|---|
| Quality на golden set | | прогон AIFeatureEvalPlan |
| Latency p95 | | замер на целевом объёме контекста |
| Стоимость на запрос | | токены x тариф model_class |

## Degradation & fallback
<!-- fallback-модель/каскад; поведение при недоступности; человеческий обход -->
## Guardrails surface
<!-- что проверяет ai-red-teamer: входы пользователя, документы, retrieved-контент, выходы инструментов -->
## Trade-offs (зафиксированные решения)
