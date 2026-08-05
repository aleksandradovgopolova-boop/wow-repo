# Definition of Done — UI (v3.2 Architecture, Product & UI Governance)

Управленческий слой поверх технического адаптера (v3.1.7 Storybook Evidence) и калиброванной политики
(v3.1.6/8 `gate_policy`). Определяет, что значит «UI-изменение готово» — по риск-тиру, а не «на глаз».
Тир берётся из `ui_impact` (см. `tools/gate_policy.py`); архитектурные UI-решения фиксируют тир в
`ArchitectureDecision.ui_impact` (`schemas/architecture-decision.schema.json`).

## Риск-тиры и требования

| Требование | `internal` (low-risk) | `user_facing` | `critical` (ключевой flow) |
|---|---|---|---|
| Story на изменённый компонент | обязательна | обязательна | обязательна |
| Покрытие состояний (default/loading/empty/error) | рекомендуется | **обязательно** | **обязательно** |
| Interaction-тесты (vitest/play) | рекомендуется | **обязательно** | **обязательно** |
| Accessibility (axe, critical/serious = 0) | автоматическая часть **блокирует** | **блокирует** | **блокирует** + human-review |
| Visual regression | advisory | **блокирует** при виз. изменении | **блокирует** |
| Design-system: reuse > новый; новый — обоснован | advisory | **обязательно** | **обязательно** |
| UX-ревью (flow/copy/tone) | advisory | обязательно | **обязательно** + human sign-off |

Правило enforcement (из `gate_policy.effective_review_outcome`): субъективный `warn` ревьюера НЕ
блокирует, когда тир advisory ИЛИ механика подтверждена детерминированным evidence (`evidence=pass`);
`evidence=fail` (реальная регрессия/дефект) блокирует ВСЕГДА; `critical` ux/a11y требуют human sign-off
(evidence не заменяет человека).

## Evidence — источник истины (не мнение)

Готовность доказывается `UIEvidenceBundle` (`schemas/ui-evidence-bundle.schema.json`), собранным
`tools/storybook_adapter.py` **на точном committed_sha** (см. StorybookPolicy, Exact-SHA binding):

- evidence привязано к проверяемой ревизии (`bundle.commit_sha == committed_sha`), иначе `not_run`;
- evidence относится ТОЛЬКО к затронутым историям (scoping по изменённым файлам);
- отсутствующее/устаревшее/чужое evidence НЕ закрывает гейт (fail-closed).

## Definition of Done — чек-лист

UI-изменение **done**, когда для его тира:

1. `ui_impact` определён и (для нетривиальных решений) зафиксирован в `ArchitectureDecision`;
2. затронутые компоненты имеют stories; обязательные состояния покрыты (по тиру);
3. `UIEvidenceBundle` собран на текущем SHA и **валиден** (`validation/validate_storybook_evidence.py`):
   interaction `pass`, a11y `blocking_violations=0`, visual `pass`, design-system без необоснованных
   новых компонентов;
4. блокирующие по тиру гейты закрыты (evidence или ревью); `critical` — с human sign-off;
5. safety-гейты (`security`, `code_review`, auth, секреты, данные) — не ослабляются никогда;
6. `false_green == 0` (движок не отдаёт ready при незакрытом блокирующем гейте).

## Component reuse (правило дизайн-системы)

- переиспользование существующего компонента — предпочтительно;
- новый компонент допустим, только если `design_system.new_components_justified = true` (обоснование в
  PR/ADR); иначе `design_system_usage` → `fail`;
- дублирование существующего компонента новым — дефект (ловится design-system evidence).

## Связи

- `tools/gate_policy.py` — тиры и enforcement; `templates/quality/StorybookPolicy.md` — маппинг
  evidence→гейт; `schemas/ui-evidence-bundle.schema.json` — контракт evidence;
  `schemas/architecture-decision.schema.json` — фиксация UI-решений и тира.
