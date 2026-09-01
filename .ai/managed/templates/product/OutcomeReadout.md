# Outcome Readout

Четвёртый управляющий объект. Отвечает на вопрос **«что произошло на самом деле»** и возвращает
знание в discovery. Без него функция исчезает после мержа, а следующая гипотеза строится на памяти.

Кладётся в `features/<workitem-id>/outcome-readout.yaml`. Вход: `templates/product/PostLaunchReview.md`.

```yaml
schema_version: 1
kind: OutcomeReadout

contract: features/<workitem-id>/outcome-contract.yaml   # отчёт без базы сравнения — рассказ

measured:
  metric: completion_rate_provider_step
  value: 0.71
  measured_at: '2026-09-16'

target_met: 'no'          # yes | no | unknown; unknown ТРЕБУЕТ unknown_reason
unknown_reason: ''
hypothesis: inconclusive  # confirmed | refuted | inconclusive

# Отчитываются ВСЕ guardrails контракта: умолчание о заранее объявленном читается как «всё хорошо».
guardrails_observed:
  - {name: время до первого агента, value: '86 сек', within: true}
  - {name: доля ошибок провайдера, value: '1.4%', within: true}

unexpected_effects: []

next_decision: >
  Продлить окно на две недели по правилу `change`: метрика выросла на 13 п.п., но цели не достигла.
back_to_discovery: >
  Отказы сместились со второго шага на третий — это новая возможность, а не остаток старой.
```
