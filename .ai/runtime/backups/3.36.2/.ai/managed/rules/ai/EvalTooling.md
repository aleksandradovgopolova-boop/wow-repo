# Eval Tooling — открытый инструментарий AI-фич (карта артефактов на инструменты)

Принцип тот же, что с OpenSpec: **CLI-and-protocol, не импорт**. Инструменты — опции
(в registry/tools.yaml со status: declared), кит не зависит ни от одного; артефакты
кита остаются источником истины, конфиги инструментов генерируются/пишутся из них.
Все варианты — открытые и бесплатные (self-hosted).

| Артефакт / задача кита | Инструмент (лицензия) | Как связывается |
|---|---|---|
| AIFeatureEvalPlan -> offline-прогон в CI (gate ai_eval) | promptfoo (MIT) | критерии и golden set из GoldenDataset.md -> promptfooconfig.yaml; exit-код в CI |
| Quality-метрики как unit-тесты (Python-стек) | DeepEval (Apache-2.0) | кейсы golden set -> pytest-тесты; pass/fail на пороги бюджетов из AIFeatureSpec |
| RAG-фичи: faithfulness / relevancy / context precision | Ragas (Apache-2.0) | метрики RAG в offline-evals; пороги — из бюджетов spec'а |
| red-team-checklist.yaml -> автоматизированные атаки (gate ai_red_team) | promptfoo red team; garak (Apache-2.0) | пункты чек-листа -> плагины/пробы; ручные атаки ai-red-teamer'а остаются обязательными |
| Online: качество/стоимость/latency на живом трафике | Langfuse (OSS, self-hosted) | traces + cost + latency; экспорт -> вход product_health.py и workflow INSIGHTS |

## Правила

1. **Выбор не навязан**: promptfoo — если стек Node/нужен red team; DeepEval —
   если Python/pytest-культура; Ragas — добавка для RAG. Достаточно одного
   offline-инструмента + Langfuse для online.
2. **Конфиденциальность**: прогоны evals ходят в провайдеров — на них действует
   routing-policy кита так же, как на продукт (конфиденциальные данные в golden set
   не уходят внешним провайдерам).
3. **Автоматизация не заменяет судью**: инструмент прогоняет, вердикт по gate выносит
   ai-evaluator / ai-red-teamer (LLM-as-judge валидируется против человеческих меток —
   EvalPolicy).
4. **Версии инструментов фиксируются** в отчётах (AIFeatureEvalPlan, RedTeamReport) —
   прогоны сравнимы.
5. При верификации инструмента в бою — поднять status в registry/tools.yaml
   declared -> documented (честные capability-декларации).
