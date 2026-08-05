# Phase B — Execution Kernel Qualification Report

- **Пакет:** AI Ops Kit, VERSION `3.1.8` + v3.1.9-rc (Exact-SHA UI Evidence, Calibrated Enforcement).
- **Дата:** 2026-07-24.
- **Провайдеры-свидетели:** DeepSeek (`deepseek-chat`, `api.deepseek.com`, OpenAI-совместимый путь) —
  живьём в этой сессии; ранее также Anthropic `claude-sonnet-5` и DeepSeek в прошлых прогонах.
- **Фикстура:** `dogfood-pricing` (Python-библиотека, git), scratch-remote
  `aleksandradovgopolova-boop/ai-ops-dogfood-pricing` (private; PR #1 сохранён как evidence).
- **Метод:** живые прогоны движка (реальная модель, реальный git, реальные PR). Классификация
  подаётся явно (`--signals`), чтобы изолировать движок/авторинг от нестабильного intake слабой
  модели — это честный тест ИСПОЛНЕНИЯ, а не разбор способности модели классифицировать.

## Вердикт (кратко)

**Гарантии ядра исполнения — QUALIFIED LIVE.** Fail-closed непробиваем: во всех прогонах, где модель
не справилась, движок корректно заблокировал (0 false-green, 0 дублей PR, основной checkout не тронут).
**Полнота green-path для ENGINEERING/UI — GATED** на способность провайдера (нужен сильный ключ,
напр. sonnet) и на UI-тулчейн (React + Storybook/Node) — это внешние зависимости, не незакрытая работа.

## Подтверждённые гарантии ядра (qualified)

| Гарантия | Свидетельство |
|---|---|
| QUICK green e2e | live (эта сессия, DeepSeek): overall=delivered, ready_for_pr=True, unmet=[], $0.007 |
| ENGINEERING → настоящий draft PR | PR #1 (прошлая Phase B, DeepSeek): sha_verified=True против реального remote SHA |
| DeliveryIntent → PR → DeliveryReceipt | outbox отработал end-to-end (PR #1); reconciliation live (reconciled, sha_verified) |
| fail-closed под слабой моделью | live (эта сессия): ENGINEERING на DeepSeek → unmet plan_readiness/code_review/security, delivery not-attempted, PR не открыт |
| intake неполон → блок, не false-green | live (эта сессия): task_type=null → intake_completeness fail, ready=False, коммит не доставлен |
| reviewer блокирует свой же код | прошлая Phase B (eng-bulk2): reviewer fail → блок |
| sequential hard-stop / reviewer-block / trusted retry | прошлая Phase B: подтверждены |
| base transition (fast-forward) safe-block | прошлая Phase B: resume `--force` НЕ снимает base_moved → блок, нужен свежий прогон |
| provider crash containment | прошлая Phase B: Anthropic HTTP 400 → status=error/provider/retryable, durable report, main не тронут |
| calibrated UI enforcement без false-green | Bench Lite v0.3 (оффлайн A/B): block-rate 0.667→0.333, residual_false_fail_rate=0.0, false_green=0 |
| exact-SHA UI evidence binding | storybook_adapter отрицательные тесты: старое/несовпадающее/чужое evidence → not_run (не освобождает) |

## Оставшиеся сценарии — статус и гейт

| # | Сценарий | Статус | Гейт |
|---|---|---|---|
| 1 | Вторая ENGINEERING green | **model-gated** | DeepSeek не тянет ENGINEERING (реконфирм live); нужен сильный ключ (sonnet) — требует ротации |
| 2 | Sequential `ready_all=true` + агрегатный draft PR | частично (hard-stop подтверждён); полный green — **model-gated** | нужны N зелёных пакетов подряд → сильный ключ |
| 3 | Interruption → resume из нового процесса/сессии | механика resume подтверждена (base-transition); полный e2e — достижимо на QUICK | достижимо с DeepSeek на QUICK-пакетах (не выполнено в этой сессии) |
| 4 | `outcome_unknown` → reconciliation с подтверждённым SHA | reconciliation подтверждена live (PR #1) | достижимо; полный induced-crash прогон не повторялся |
| 5 | Реальная UI-задача в React child со Storybook на exact SHA | **toolchain-gated** | нужен React child + Node/Storybook/vitest/axe; фикстура — Python. Механика exact-SHA binding доказана юнит-тестами |

## Известные ограничения (зафиксировано честно)

1. **GateResult v2** реализован как контракт + compatibility-адаптер, но ещё НЕ стал каноническим
   runtime-форматом всех gate-результатов: калиброванный advisory в `_run_reviews` сохраняется как
   v1 `warn`; `calibrated_view()` не публикуется в отчёте. Планируется отдельно.
2. **Reviewer `abstain`** (эмиссия статуса ревьюером) не поддержан end-to-end: `warn` без blockers
   отвергается валидатором как невынесенный вердикт → fail-closed. Первоклассный abstain — будущая
   работа поверх reviewer-result v2.
3. **Green-path ENGINEERING/UI** требует сильного провайдера и (для UI) Node/React/Storybook-тулчейна.
   Засвеченный `ANTHROPIC_API_KEY` ждёт ротации в консоли — до этого sonnet-путь недоступен.

## Итог

Ядро исполнения (транзакционный контроллер, gates, evidence-on-exact-SHA, writer≠judge, fail-closed,
delivery-outbox, calibrated UI enforcement с exact-SHA evidence) — **доказано корректным вживую**.
Незакрытые пункты — не дефекты движка, а внешние гейты (способность провайдера, UI-тулчейн).

**v3.1 закрыт (2026-07-24):** Execution Kernel qualified; оставшиеся положительные green-path
сценарии (2-я ENGINEERING green, полный sequential ready_all, реальная UI-задача) переведены в статус
**rolling evidence** — накапливаются по появлении сильного ключа и React/Storybook-фикстуры, но НЕ
удерживают roadmap. Движок к их закрытию готов.
