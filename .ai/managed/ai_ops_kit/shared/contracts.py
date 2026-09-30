#!/usr/bin/env python3
"""contracts.py (v3.28.0 R4) — TypedDict контракты для ключевых структур данных.

Типизация границ между модулями: вызывающий видит, какие ключи ожидать в возвращаемых dict'ах.
Runtime-поведение не меняется (TypedDict — это dict с аннотациями, не новый тип).
IDE и mypy получают навигацию и проверку; старый код продолжает работать.

Контракты:
  UsageRecord       — одна запись usage ledger (стоимость/токены/роль)
  PreflightResult   — результат preflight-проверок ДО запуска модели
  GateResultV2      — результат quality gate (v2 с калибровкой)
  GateCheck         — одна проверка внутри gate
  RunReport         — отчёт execution pipeline (финальный артефакт прогона)
  DeliveryIntent    — намерение доставки (PR)
  DeliveryReceipt   — подтверждение доставки (PR смержён/не смержён)
  ContextBundle     — пакет контекста для WorkItem
  WorkItemState      — состояние WorkItem

Только аннотации, без runtime-логики. Импортируется как:
  from ai_ops_kit.shared.contracts import UsageRecord, PreflightResult
"""
from __future__ import annotations

from typing import Any, Literal, Optional, TypedDict


# ============================================================================
# Usage Ledger
# ============================================================================

class UsageRecord(TypedDict, total=False):
    """Одна запись учёта модельного/runtime-вызова.

    Честность: usage_status=unavailable -> input_tokens/output_tokens = None (НЕ 0).
    cost_status=measured -> cost не None.
    """
    run_id: str
    workitem_id: str
    package_id: Optional[str]
    role: str                           # implementation|review|fix_loop|escalation|...
    provider: str                       # anthropic|openai|openai-compatible|claude-cli|mock
    model: str
    runtime: Optional[str]
    input_tokens: Optional[int]         # None если unavailable
    output_tokens: Optional[int]        # None если unavailable
    usage_status: Literal["measured", "estimated", "unavailable"]
    cost: Optional[float]               # USD; None если unavailable
    cost_status: Literal["measured", "estimated", "unavailable"]
    latency: Optional[float]            # секунды
    trigger: Literal["initial", "retry", "review", "escalation", "fallback", "reevaluate"]
    # v3.24.0 контекстные поля
    task_type: Optional[str]
    workflow: Optional[str]
    risk: Optional[str]
    size: Optional[str]
    writer_tier: Optional[str]
    execution_mode: Optional[str]
    stack: Optional[str]
    architecture_impact: Optional[str]


# ============================================================================
# Preflight
# ============================================================================

class PreflightCheck(TypedDict):
    """Одна проверка preflight."""
    ok: bool
    # Дополнительные поля зависят от проверки (task_type, artifact_present, ...)


class PreflightResult(TypedDict):
    """Результат preflight-проверок (ai_ops_kit/gates/preflight.py assess()).

    ok=True -> можно запускать tool loop.
    blocked=True -> модель НЕ запускается, правки/коммит НЕ создаются.
    """
    kind: str                           # "preflight"
    ok: bool                            # общий вердикт
    blocked: bool                       # есть ли блокирующие причины
    checks: dict[str, PreflightCheck]   # имя_проверки -> результат
    reasons: list[str]                  # человеческие описания блоков


# ============================================================================
# Gate Result V2
# ============================================================================

class GateCheck(TypedDict):
    """Одна проверка внутри quality gate."""
    id: str
    status: Literal["pass", "warn", "fail"]


class GateResultV2(TypedDict, total=False):
    """Результат quality gate v2 (с калибровкой UI-enforcement).

    Расширяет v1 (pass|warn|fail) двумя новыми статусами:
    - not_applicable: гейт не применялся к этому прогону
    - abstain: ревьюер воздержался (субъективное сомнение, не блок)
    """
    schema_version: int                 # = 2
    gate: str                           # id гейта
    status: Literal["pass", "warn", "fail", "not_applicable", "abstain"]
    blocking: bool
    applicability: Literal["applicable", "not_applicable"]
    enforcement: Literal["advisory", "blocking"]
    evidence_mode: Literal["deterministic", "ai_review", "hybrid", "human"]
    human_signoff: bool
    scope: Optional[str]
    checks: list[GateCheck]
    blockers: list[str]
    warnings: list[str]
    evidence: list[str]
    calibration_reason: Optional[str]
    affected_files: list[str]
    tested_revision: Optional[str]
    owner: str
    review_mode: Literal["read-only", "writer"]
    created_at: Optional[str]
    expires_at: Optional[str]
    override: Optional[dict]
    reviewer_outcome: Optional[Literal["pass", "warn", "fail", "abstain"]]
    resolution: Optional[Literal["resolved", "pending_human"]]
    delivery_allowed: bool
    human_handoff: bool


# ============================================================================
# Execution Pipeline Report
# ============================================================================

class RunReportCheck(TypedDict, total=False):
    """Одна проверка в отчёте прогона."""
    status: Literal["pass", "fail", "skip"]
    output_tail: str
    command: str
    duration_s: float


class RunReportReview(TypedDict, total=False):
    """Результат ревью в отчёте прогона."""
    gate: str
    status: Literal["pass", "warn", "fail"]
    blockers: list[str]


class RunReportGates(TypedDict, total=False):
    """Сводка гейтов в отчёте прогона."""
    met: list[str]
    unmet: list[str]
    not_applicable: list[str]
    # Кто закрыл каждый гейт: validator | judge | writer | human. «Зелёное» от машины и «зелёное»
    # по мнению судьи — разные утверждения, и отчёт обязан их различать.
    closure: dict[str, Any]


class RunReport(TypedDict, total=False):
    """Финальный отчёт execution pipeline.

    Основной контракт между pipeline и вызывающим (ai_ops_run, CLI).
    ready_for_pr=True -> можно открывать draft PR.
    """
    # Идентификация
    workitem_id: str
    run_id: str
    task: str

    # Статус
    overall_status: str                 # done|blocked|blocked-preflight|error
    ready_for_pr: bool
    error: Optional[str]

    # Проверки
    checks: dict[str, RunReportCheck]
    reviews: list[RunReportReview]
    gates: RunReportGates

    # Evidence
    security_scan: dict[str, Any]
    evidence: dict[str, Any]
    changed_files: list[str]
    committed_sha: Optional[str]

    # Delivery
    delivery: dict[str, Any]
    handoff: dict[str, Any]

    # Cost
    total_cost_usd: Optional[float]
    total_tokens_in: Optional[int]
    total_tokens_out: Optional[int]
    call_count: int


# ============================================================================
# Delivery
# ============================================================================

class DeliveryIntent(TypedDict, total=False):
    """Намерение доставки (PR) — записывается ДО открытия PR."""
    schema_version: int                 # = 1
    kind: str                           # = "DeliveryIntent"
    delivery_id: str
    workitem_id: str
    repository: str
    branch: str
    commit_sha: str
    base_ref: str
    created_at: str


class DeliveryReceipt(TypedDict, total=False):
    """Подтверждение доставки — записывается ПОСЛЕ проверки remote."""
    schema_version: int                 # = 1
    kind: str                           # = "DeliveryReceipt"
    delivery_id: str
    workitem_id: str
    repository: Optional[str]
    branch: str
    commit_sha: Optional[str]
    base_ref: Optional[str]
    status: str                         # reconciled|not-delivered|mismatch|reconciled-absent
    remote_sha: Optional[str]
    sha_verified: bool
    pr_url: Optional[str]
    pr_number: Optional[int]
    pr_state: Optional[str]
    merged: Optional[bool]
    reconciled: bool
    # R-41: проверки на том же SHA. `sha_verified` отвечает «это наш коммит», эти поля — «его кто-то
    # проверял». Раньше второго вопроса не существовало, и «прогонов не было» читалось как «зелено».
    checks_status: Optional[str]         # found|absent|unavailable — «нет» и «не знаю» РАЗНЫЕ
    checks_total: Optional[int]
    checks_failed: Optional[int]
    checks_verified: Optional[bool]      # True только при found + 0 упавших + 0 незавершённых


# ============================================================================
# Context
# ============================================================================

class ContextBundle(TypedDict, total=False):
    """Пакет контекста для WorkItem (ai_ops_kit/context/context_compiler.py).

    included: источники, включённые в контекст (с причиной).
    excluded: источники, исключённые (с причиной).
    """
    kind: str                           # = "ContextBundle"
    workitem_id: str
    included: list[dict[str, str]]      # [{source, reason, hash, revision}]
    excluded: list[dict[str, str]]      # [{source, reason}]
    assumptions: list[str]
    open_questions: list[str]
    estimated_tokens: int
    context_budget: int
    text: str                           # compiled payload


# ============================================================================
# WorkItem
# ============================================================================

class WorkItemState(TypedDict, total=False):
    """Состояние WorkItem — единая сущность задачи."""
    id: str
    task: str
    status: str                         # draft|done|blocked|cancelled|...
    lifecycle_intent: str               # discovery|implementation|review|delivery|completed
    workflow: str                       # QUICK|ENGINEERING|PRODUCT|...
    base_workflow: str
    created_at: str
    updated_at: str
    evidence: dict[str, Any]
    run_id: Optional[str]
    branch: Optional[str]
    pr_url: Optional[str]


# ============================================================================
# Kernel Events (v3.38, trustworthy-core Wave 3)
# ============================================================================

class KernelEvent(TypedDict, total=False):
    """Событие ядра — контракт между ядром и спутниками.

    Ядро испускает события через shared.events.emit(); спутники подписываются через
    shared.events.subscribe(). Спутник не импортируется ядром напрямую.

    Типы событий:
      run_completed      — ИСПУСКАЕТСЯ (ai_ops_run) и подписан (engops/session_events).
                           Несёт весь report — из него доступны и гейты, и итог доставки.
      gate_evaluated     — ОБЪЯВЛЕН, НО НЕ ИСПУСКАЕТСЯ: отдельного потребителя, которому нужен
                           момент «сразу после гейтов» без полного report, пока нет. Не эмитим
                           событие без потребителя — это была бы декоративная поверхность
                           (тот же класс, что мёртвый каталог инвариантов до K7). Появится ПО
                           ДАННЫМ реального спроса, аддитивно, вместе со своим подписчиком.
      delivery_completed — ОБЪЯВЛЕН, НО НЕ ИСПУСКАЕТСЯ (та же причина; итог доставки уже в report
                           события run_completed). Поля ниже — заготовка контракта, не активный шов.
    """
    event_type: str
    workitem_id: str
    timestamp: str
    # run_completed
    status: str
    report: dict[str, Any]
    child_root: str
    # gate_evaluated
    gate_results: list[dict[str, Any]]
    tested_revision: str
    # delivery_completed
    receipt: dict[str, Any]


# Запуск скриптом ОБЪЯСНЯЕТ модуль, а не молчит (ревизия 2026-08-11).
#
# Здесь стояло `sys.exit(selftest())`, а сама функция удалена в v3.30 вместе с переносом
# селфтестов в pytest: любой запуск падал с `NameError`. Просто убрать блок — тоже неверно:
# `tools/contracts.py` остаётся объявленной точкой входа, и молчаливый выход с кодом 0 — тот
# самый дефект, который ловит `tests/unit/test_alias_entry_points.py` («ноль и есть симптом»).
# Поэтому вход делает осмысленную работу — печатает назначение модуля, как `invariants.py`.
# Проверки модуля — в `tests/unit/`.
if __name__ == "__main__":
    print(__doc__)
    print("Проверки этого модуля — в tests/unit/ (pytest), отдельного --selftest нет с v3.30.")
