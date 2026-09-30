---
read_tier: 3   # ярус чтения: 3 по явной необходимости (крупные контракты/архивы)
---

# Metric Catalog

<!-- Семантический слой: каждая метрика определена ровно один раз.
     TrackingPlan, DashboardSpec, ProductAnalyticsPlan и Product Health ссылаются сюда; analytics-reviewer сверяет с этим (гейт analytics_readiness). -->
## Metrics
<!-- на каждую метрику:
     name | definition (точная формула) | events used | grain | filters/exclusions
     | owner | source | good/bad direction | current baseline -->

Семейство **Time to Verified Outcome (TTVO)**: путь идея→проверенный результат. Все данные
снимаются ЛОКАЛЬНО из артефактов прогона, git и PR — телеметрия запрещена, в сеть ничего не
уходит. Ни одного придуманного значения: baseline у каждой метрики = «ещё не измерено».

### intent→verified-PR time  (NORTH STAR)
- **name:** intent→verified-PR time
- **definition (формула):** `t_verified_pr − t_specify_start` — астрономическое (календарное)
  время в минутах от отметки старта `specify` до отметки готовности проверенного PR (все
  обязательные проверки/гейты зелёные). Агрегат по множеству работ — медиана.
- **events used:** `specify.started` (старт формулировки), `verified_pr.ready` (все обязательные
  гейты зелёные).
- **grain:** на работу (work-item); агрегируется медианой за период.
- **filters/exclusions:** только работы, реально стартовавшие через `specify`; отменённые до
  готовности PR исключаются из числителя времени (учитываются в verified-PR rate). Пауза в
  ожидании ответа человека НЕ вычитается — она часть пути к результату.
- **owner:** product-team
- **source:** отметки времени прогона из локальных артефактов (старт specify → готовность
  verified PR); ничего не отправляется в сеть.
- **direction:** меньше — лучше.
- **current baseline:** ещё не измерено — снимается первым живым прогоном (часть 2 работы
  product-value-measurement).

### verified-PR rate  (input)
- **name:** verified-PR rate
- **definition (формула):** `works_reached_verified_pr_without_manual_code / works_started` —
  доля начатых работ, дошедших до проверенного PR (все обязательные гейты зелёные) без ручной
  доводки кода человеком.
- **events used:** `specify.started`, `verified_pr.ready`, `manual_code_intervention` (признак
  ручной доводки кода/конфига человеком).
- **grain:** на работу (0/1), агрегируется долей за период.
- **filters/exclusions:** знаменатель — все стартовавшие работы; из числителя исключаются работы,
  где была ручная доводка кода/конфига человеком (см. human-intervention rate).
- **owner:** product-team
- **source:** исходы прогонов + локальный журнал доставки.
- **direction:** больше — лучше.
- **current baseline:** ещё не измерено — снимается первым живым прогоном (часть 2).

### human-intervention rate  (input)
- **name:** human-intervention rate
- **definition (формула):** `works_with_human_intervention / works_started` — доля работ, где
  потребовалось ≥1 вмешательство человека сверх исходной формулировки и обязательных продуктовых
  решений (человек правит код или конфиг, чтобы разблокировать работу).
- **events used:** `specify.started`, `manual_code_intervention`.
- **grain:** на работу (0/1), агрегируется долей за период.
- **filters/exclusions:** ответы на продуктовые вопросы, которые кит по контракту адресует
  человеку (обязательные продуктовые решения), вмешательством НЕ считаются; считается только
  правка кода/конфига ради разблокировки.
- **owner:** product-team
- **source:** артефакты прогона (зафиксированные вмешательства) + локальный журнал доставки.
- **direction:** меньше — лучше.
- **current baseline:** ещё не измерено — снимается первым живым прогоном (часть 2).

### rework rate  (guardrail)
- **name:** rework rate
- **definition (формула):** `merged_works_with_corrective_change_within_7d / merged_works` —
  доля слитых работ, потребовавших корректирующего изменения по той же работе в течение окна
  N=7 дней после слияния.
- **events used:** `work.merged`, `corrective_change.merged` (изменение, атрибутированное той же
  работе).
- **grain:** на слитую работу (0/1), агрегируется долей за период.
- **filters/exclusions:** окно фиксировано N=7 дней от даты слияния; учитываются только
  корректирующие изменения, привязанные к той же работе (work-item), а не любые последующие
  правки в тех же файлах.
- **owner:** product-team
- **source:** история git + связь корректирующих изменений с исходной работой (локально).
- **direction:** меньше — лучше.
- **current baseline:** ещё не измерено — снимается первым живым прогоном (часть 2).

### post-merge defect rate  (guardrail)
- **name:** post-merge defect rate
- **definition (формула):** `merged_works_with_attributed_defect / merged_works` — доля слитых
  работ, где после слияния найден дефект, относящийся к этому изменению.
- **events used:** `work.merged`, `defect.attributed` (дефект, атрибутированный слитому
  изменению).
- **grain:** на слитую работу (0/1), агрегируется долей за период.
- **filters/exclusions:** учитываются только дефекты, атрибутированные конкретному слитому
  изменению; несвязанные дефекты и дефекты, уже существовавшие до изменения, исключаются.
- **owner:** product-team
- **source:** локальный журнал доставки + последующие исправления/дефекты, атрибутированные
  изменению (git/PR, локально).
- **direction:** меньше — лучше.
- **current baseline:** ещё не измерено — снимается первым живым прогоном (часть 2).

## Metric relationships
<!-- дерево: North Star ← input metrics ← события -->

```
North Star: intent→verified-PR time   (время идея→проверенный PR; меньше — лучше)
  ← input: verified-PR rate            (доля работ до проверенного PR без ручной доводки; больше — лучше)
  ← input: human-intervention rate     (доля работ с вмешательством человека; меньше — лучше)
      ── ограждают (скорость не в ущерб качеству) ──
  ← guardrail: rework rate             (переделки в окне 7 дней; меньше — лучше)
  ← guardrail: post-merge defect rate  (дефекты после слияния; меньше — лучше)
```

Чтение дерева: north star (intent→verified-PR time) объясняется входными метриками —
verified-PR rate и human-intervention rate (быстрее и без ручной доводки → короче путь). Обе
guardrail-метрики ограждают north star: улучшение времени засчитывается только при неухудшении
rework rate и post-merge defect rate — иначе скорость куплена ценой качества.

## Deprecated metrics
<!-- устаревшие определения с датой и заменой (не удалять молча) -->
