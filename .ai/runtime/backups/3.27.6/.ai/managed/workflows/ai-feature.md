# Workflow: AI feature (LLM/агентная часть продукта)

Применяется **только** к фичам с LLM/агентным компонентом (opt-in: preset
ai-product + task_type ai-feature/llm-integration/rag-pipeline/...). Обычная
разработка идёт через ENGINEERING.

```text
Intake (есть ли LLM-компонент; иначе -> ENGINEERING)
→ target scenario (llm-architect: сценарий измеримо, model_class через routing,
  бюджеты качества / p95 latency / стоимости, деградация)
→ golden dataset (ai-feature-engineer: набор ДО реализации — eval-driven)
→ implementation (промпты/RAG/tools как код; итерации меряются прогоном)
→ offline evals (ai-evaluator, read-only: gate ai_eval против бюджетов)
→ red team (ai-red-teamer, read-only: gate ai_red_team по OWASP LLM Top 10)
→ verify (final-verifier)
→ memory update
```

Ключевые правила:
- **Качество и скорость ИИ-части — числа целевого сценария**, а не впечатление:
  порог на golden set, p95 latency, стоимость на запрос — задаются в target-scenario
  и проверяются в offline-evals.
- **Writer ≠ judge дважды**: ai-feature-engineer строит, ai-evaluator меряет
  качество в сценарии, ai-red-teamer ломает вне сценария.
- **Смена модели/промпта/инструмента = regression-прогон** eval-набора до релиза
  (rules/ai/EvalPolicy.md).
- Online-слой после релиза (качество/стоимость/latency на живом трафике) —
  инструментами из rules/ai/EvalTooling.md; интерпретация — workflow INSIGHTS.

Шаблоны: `templates/engineering/AIFeatureSpec.md`,
`templates/quality/{GoldenDataset,AIFeatureEvalPlan,RedTeamReport}.md`.
