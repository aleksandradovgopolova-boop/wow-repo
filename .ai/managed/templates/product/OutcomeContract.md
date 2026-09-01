# Outcome Contract

Третий управляющий объект. Отвечает на вопрос **«как мы узнаем, что это сработало»** — и отвечает ДО
работы, а не после. Правило решения, принятое после результата, всегда толкуется в пользу сделанного.

Кладётся в `features/<workitem-id>/outcome-contract.yaml`. Входы: `templates/analytics/*`
(`ProductAnalyticsPlan`, `TrackingPlan`), `templates/product/LaunchPlan.md`.

```yaml
schema_version: 1
kind: OutcomeContract

decision: features/<workitem-id>/product-decision.yaml

primary_metric:
  name: completion_rate_provider_step
  source: продуктовая аналитика, событие provider_selected

# База без даты и источника — это число из головы.
baseline:
  value: 0.58
  measured_at: '2026-08-14'
  source: дашборд onboarding, окно 01–14.08

target:
  value: 0.75
  by: '2026-09-15'

# Без guardrails «цель достигнута» может означать «сломали соседнее».
guardrails:
  - {name: время до первого агента, must_not_exceed: '90 сек'}
  - {name: доля ошибок провайдера, must_not_exceed: '2%'}

events: [provider_selected, provider_step_abandoned]

evaluation_period: 4 недели после релиза

# Правило решения принимается ЗАРАНЕЕ.
decision_rules:
  continue: цель достигнута, guardrails в норме — раскатываем на всех
  change: цель не достигнута, но растёт — продлеваем окно на две недели
  stop: guardrail пробит либо метрика ниже baseline — откатываем и возвращаемся в discovery
```
