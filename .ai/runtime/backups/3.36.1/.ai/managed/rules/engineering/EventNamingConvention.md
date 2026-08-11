# EventNamingConvention — единое имя события во всех слоях

## Зачем

Класс дефектов: событие названо по-разному в трёх местах. Контракт (domain events):
`task.completed`, `object.version_created`. Код (audit): `task.complete`,
`object.version.save`. Metric catalog: `catalog.publish`. Плюс концептуальная подмена —
контракт описывает **domain event** (envelope `{eventId, type, occurredAt, payload}`), а
в коде persist-ится **AuditEvent** (`{actorId, action, objectType, result, createdAt}`):
разные сущности выдаются за одно. Итог — рассинхрон, ломающиеся дашборды, ложные метрики.

## Правило

1. **Единый каталог** — `.ai/project/contracts/events.yaml` (схема
   `event-catalog.schema.json`): каждое событие названо **один раз**. Контракт EVENTS.md,
   код и MetricCatalog ссылаются на каталог, а не выдумывают своё имя.
2. **Грамматика имени** — lowercase, dot-нотация, прошедшее время:
   `noun.past_tense_verb` (`object.version_created`, `task.completed`). Не смешивать
   разделители (нет `object.version.save`, нет camelCase, нет kebab).
3. **Три слоя различать явно.** `kind: domain | audit | analytics`. domain-событие —
   предметная область; audit — запись действия (actor/action/result); analytics —
   метрика. Не описывать domain-событие audit-полями (это подмена сущности).
4. **audit/analytics ссылаются на domain через `maps_to`.** Если audit-запись или метрика
   отражают доменное событие — указать каноничное domain-имя. Нет соответствия — только
   явный `standalone: true` + `reason`. Так три «языка» сходятся к одному имени, а не
   расходятся молча.
5. **Проверка механическая** — гейт `event_contract_consistency`
   (`validation/validate_event_catalog.py`), опционально `--scan` по коду ловит литералы
   событий, которых нет в каталоге (drift кода).

## Признак здоровья

Одно событие — одно каноничное имя во всех слоях; audit/analytics явно связаны с domain;
`validate_event_catalog.py` зелёный, `--scan` не находит незнакомых литералов в коде.
