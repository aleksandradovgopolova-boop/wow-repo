# Кто закрывает гейт: машина, судья, писатель или человек

Этот документ едет в продуктовый репозиторий вместе с китом. Он отвечает на один вопрос: **когда
кит говорит «гейт пройден» — кто это проверил.**

## Почему вопрос вообще есть

Замер 19.08.2026: из 35 гейтов **19 не имели исполняемого валидатора** (на 20.08.2026 — 18, см.
ниже). Их закрывает заключение
LLM-судьи или решение человека. В отчёте прогона все гейты выглядели одинаково — «пройден» от
детерминированной проверки было неотличимо от «пройден» по чьему-то мнению. Читатель отчёта
достраивал разницу сам, а чаще не достраивал вовсе.

Слово «зелёный» без имени того, кто его поставил, стоит ровно столько, сколько стоит самый слабый
способ его получить.

## Четыре ответа, а не три

| Значение | Что означает | Чего стоит |
|---|---|---|
| `validator` | Гейт закрывает **детерминированный валидатор** — исполняемая проверка с кодом возврата | Воспроизводимо: тот же вход даёт тот же ответ у вас и в CI |
| `judge` | Гейт закрывает **заключение независимой роли-судьи** (read-only относительно проверяемого) | Мнение, но постороннее. Судья не писал того, что проверяет |
| `writer` | Гейт закрывает **собственная стадия**, которая работу и сделала | Самозаявление. Слабейшая форма: писатель подтверждает себя |
| `human` | Гейт закрывает **решение человека** | Ответственность названа поимённо |

**Четвёртое значение существует не ради полноты таксономии.** Гейт с `review_mode: writer` и без
валидатора подтверждает сам себя. Назвать это `judge` значило бы напечатать в отчёте ровно то
утверждение, против которого стоит инвариант кита «writer ≠ judge».

## Как значение получается

`closed_by` в `quality/gates.yaml` **не объявляется отдельно от поведения**: оно выводится из той
же классификации, по которой гейт исполняется (`gate_executor.classify`), и тест сверяет реестр с
кодом. Второе объявление рядом с первым разошлось бы с ним на первой же правке — этот класс
дефектов кит ловит у себя же.

Условное одобрение человека (`human_approval.required_when`) значение не переписывает: гейт
`security` закрывается судьёй, а при названных сигналах — привилегии, разрушительные операции,
изменение границы секретов — поднимается до человека в момент прогона.

## Где это видно

- **в отчёте прогона** — `run-report.json` → `gates.closure`: разбивка по числам, список
  проверенного машиной и список закрытого мнением;
- **в выводе прогона** — строка «гейты: проверено машиной N из M; остальное — мнение: …»;
- **в реестре** — поле `closed_by` у каждого гейта.

## Карта на 20.08.2026

| Гейт | Закрывает | Роль в MVP | Валидатор / ответственная роль |
|---|---|---|---|
| `archive_readiness` | validator | блокирующий | validate-archive-readiness |
| `concurrency_preflight` | validator | совещательный | validate-concurrency |
| `contour_consistency` | validator | совещательный | validate-product-model |
| `deploy_readiness` | validator | блокирующий | validate-deploy-readiness |
| `event_contract_consistency` | validator | совещательный | validate-event-catalog |
| `implementation_verification` | validator | блокирующий | validate-evidence |
| `intake_completeness` | validator | блокирующий | validate-intake |
| `knowledge_freshness` | validator | совещательный | validate-freshness |
| `knowledge_integrity` | validator | совещательный | validate-references + validate-claims |
| `own_medicine` | validator | совещательный | validate-own-medicine |
| `plan_readiness` | validator | блокирующий | validate-plan |
| `regression_test_evidence` | validator | совещательный | validate-regression-evidence |
| `requirements` | validator | блокирующий | validate-requirements |
| `documentation_updated` | validator | совещательный | validate-documentation-updated |
| `spec_synchronization` | validator | блокирующий | openspec-validate-and-guard |
| `specification` | validator | блокирующий | openspec-validate-strict |
| `surface_wiring_consistency` | validator | совещательный | validate-surface-wiring |
| `accessibility_review` | judge | блокирующий | accessibility-reviewer |
| `ai_eval` | judge | блокирующий | ai-evaluator |
| `ai_red_team` | judge | блокирующий | ai-red-teamer |
| `analytics_design_readiness` | judge | блокирующий | analytics-reviewer |
| `analytics_runtime_verification` | judge | блокирующий | analytics-reviewer |
| `architecture_review` | judge | блокирующий | architecture-reviewer |
| `code_review` | judge | блокирующий | code-reviewer |
| `decision_quality` | judge | совещательный | product-reviewer |
| `design_system_usage` | judge | блокирующий | design-system-reviewer |
| `discovery_completeness` | judge | блокирующий | product-reviewer |
| `evidence` | judge | блокирующий | final-verifier |
| `observability_readiness` | judge | совещательный | observability-reviewer |
| `release_safety` | judge | совещательный | release-manager |
| `security` | judge | блокирующий | security-reviewer |
| `stakeholder_readiness` | judge | совещательный | product-manager |
| `ux_review` | judge | блокирующий | ux-reviewer |
| `visual_regression` | judge | блокирующий | final-verifier |
| `documentation_drift` | writer | совещательный | documentation-steward |


**Итого: 17 машиной, 17 судьёй, 1 писателем, 0 человеком** (гейт `security` поднимается до
человека сигналами задачи, а не постоянно).

**Что изменилось 20.08.2026.** `documentation_updated` переведён из самозаявления в машинный:
оба его доказательства — факты о дифе (документация в изменении затронута; запись для CHANGELOG
добавлена), а не суждение, и спрашивать о них стадию, которая работу и сделала, было лишним.
Проверка — `ai_ops_kit/gates/documentation_evidence.py`. Ненужность документации по-прежнему
закрывается объявлением writer'а с причиной, но громко: причина уходит в warnings и в отчёт.

## Что с этим делать

Число `judge` не является дефектом само по себе: часть проверок принципиально не сводится к
детерминированному прогону — доступность, UX, качество решения. Дефектом было бы другое: **не
знать, какая проверка какая.** Теперь это видно из отчёта, и решение — оставить мнение мнением или
довести до валидатора — принимается с открытыми глазами.

Один гейт в состоянии `writer` (`documentation_drift`) — самый слабый случай, и он назван прямо:
его закрывает та же стадия, что и писала. Список самозаявляющихся гейтов — ратчет, и он ходит
вниз: 20.08 их стало на один меньше.

**У кого «мнение» временное, а у кого по существу — разобрано отдельно** и проверяется кодом:
`quality/gate-machinability.yaml` плюс `python3 -m ai_ops_kit.devtools.gate_machinability`. Замер на
20.08.2026: из восемнадцати восемь могут стать машинными (все доказательства фактические, у
каждого записано, чего для этого не хватает), семь — частично (машина закроет свою часть и сузит
мнение до остатка), четыре — человеческие по существу (`code_review`, `architecture_review`,
`decision_quality`, `stakeholder_readiness`): там машинной остаётся только форма, и объявить их
машинными значило бы поставить код возврата под вопрос, на который код не отвечает.
