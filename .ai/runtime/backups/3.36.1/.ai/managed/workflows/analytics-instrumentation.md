# Workflow: analytics instrumentation

```text
Intake
→ metrics definition (North Star, product metrics, guardrails)
→ event design (taxonomy, event schema, tracking plan)
→ dashboard specification (blocks, funnels, alerts)
→ implementation (инструментирование)
→ verify (события летят, схема соблюдена, QA checklist)
→ memory update
```

Шаблоны: `templates/analytics/TrackingPlan.md`, `EventSchema.md`, `DashboardSpec.md`,
`templates/product/ProductAnalyticsPlan.md`. Gate: `analytics_readiness`.

События проектируются до реализации функции, а не после: tracking plan — входной
артефакт implementation, dashboards проверяются на реальном потоке до закрытия задачи.
