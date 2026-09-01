# AI Eval Policy

- ни одна AI-фича не выходит без eval'ов (AIFeatureEvalPlan приложен и прогнан);
- success criteria — specific/measurable/achievable/relevant, многомерные;
- eval-набор как код: версионируется, отражает реальное распределение + edge cases;
- LLM-as-judge допустим только с чёткой рубрикой и после валидации против человеческих меток;
- writer и judge разделены; judge read-only к оцениваемому выходу (принцип kit);
- guardrails обязательны: безопасность, faithfulness/галлюцинации, PII, prompt injection;
- смена модели / промпта / инструмента = regression-прогон eval'ов до релиза;
- online-качество и стоимость измеряются в проде, не только offline;
- offline-evals дополняют, а не заменяют продуктовые метрики из MeasurementBaseline.
