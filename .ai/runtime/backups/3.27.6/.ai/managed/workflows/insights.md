# Workflow: insights (continuous improvement)

```text
Intake (какая функция/период, есть ли данные)
→ data collection (источники — tracking plan и monitoring-spec функции)
→ product health report (детерминированно: tools/product_health.py, не LLM)
→ insight synthesis (что данные говорят; каждый вывод привязан к данным)
→ insight review (product-reviewer, read-only)
→ hypotheses for next discovery (experiment-designer)
→ memory update (запись обязательна: learning_output = required)
```

Инсайты и гипотезы записываются в knowledge graph (`insight -> derived-from ->
metric/experiment/incident`, `insight -> feeds -> goal/feature`) — так следующий
Discovery начинается не с чистого листа, а с накопленных связей.

Цикл: Discovery → Delivery → Release → Measurement → **Insights** → Discovery.
