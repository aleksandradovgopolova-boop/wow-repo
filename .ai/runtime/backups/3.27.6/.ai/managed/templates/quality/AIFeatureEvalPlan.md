# AI Feature Eval Plan

<!-- Измерение AI-фичи, отданной пользователям. Дополняет templates/analytics/TrackingPlan.md (продуктовые события)
     слоем качества модели/промпта. Заполняется ДО релиза, ревьюится как спецификация. -->
## Feature and AI surface
<!-- где именно LLM/агент в продукте: генерация, классификация, извлечение, агентный flow -->

## Success criteria
<!-- specific / measurable / achievable / relevant. Пример: "<0.1% ответов помечены
     токсичными на 10k прогонов". Многомерно: task fidelity, consistency, faithfulness,
     tone/style, privacy, context utilization, latency, cost. -->

## Eval dataset
<!-- набор кейсов, отражающий реальное распределение задач + edge cases;
     объём важнее ручной вылизанности; хранится как код, версионируется -->

## Offline evals (pre-release)
<!-- метод грейдинга на каждый критерий:
     code-based (exact/string/regex) — быстро и надёжно;
     LLM-as-judge — с чёткой рубрикой, шкалой (correct/incorrect или 1–5), reasoning-then-score;
     human — минимально, для валидации. Judge проверен против человеческих меток. -->

## Guardrails
<!-- безопасность, галлюцинации/faithfulness к источнику, утечки PII, prompt injection;
     пороги, ниже которых релиз блокируется -->

## Online / production
<!-- что меряем в проде: latency, cost/токены, доля fallback/ошибок, thumbs up/down,
     online-качество на семпле трафика; трейсинг вход→выход для разбора инцидентов -->

## Regression policy
<!-- при смене модели/промпта/инструмента — прогон eval-набора; релиз только при не-регрессии -->

## Owner and cadence
<!-- владелец eval'ов; частота прогонов offline и ревью online -->
