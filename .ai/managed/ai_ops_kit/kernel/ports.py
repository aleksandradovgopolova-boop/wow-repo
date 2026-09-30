#!/usr/bin/env python3
"""ports.py — КОНТРАКТ ТИПОВ ядра AI Ops (Protocol'ы для структурной типизации).

Это контракт ТИПОВ, а НЕ шов с внедряемыми реализациями. Ядро сверяет свои входы/выходы
против этих Protocol'ов и TypedDict'ов структурно (`isinstance` по `@runtime_checkable`,
проверка дрейфа полей ExecutionSpec в engine/execution_pipeline) — так контракт не расходится
с кодом на каждом прогоне, в том числе в дочке. Реализации портов НЕ регистрируются здесь и НЕ
внедряются через DI: каждая живёт в своём пакете ядра и вызывается прямым импортом (адреса ниже —
ориентир «где искать реализацию», а не точка внедрения).

Phase B (dependency injection: контейнер портов, подстановка реализаций на входе транзакции) НЕ
преследуется. Ранее рядом лежали четыре модуля-заготовки под такой шов (engops/{delivery_size,
merge_lifecycle,refusal_paths,session_thresholds}) — их сняли (2026-09-05): 0 импортеров, порту
не соответствовали, дормантный инвентарь. Понадобится Phase B — реализации восстановят против
этих Protocol'ов, а не воскрешением заготовок.

Порты (v3.38, trustworthy-core K0) — и где лежит соответствующая реализация ядра:
  ExecutorPort       — вероятностный исполнитель (модель предлагает → broker исполняет).
                       providers/orchestrator (или внешний рантайм).
  ContextPort        — сборка контекста для WorkItem.
                       context/context_compiler.
  EvidenceProvider   — детерминированный сбор evidence (build/lint/test/scan).
                       gates/evidence_collector, security/security_scan, checks/.
  GatePort           — оценка quality gates (fail-closed, writer≠judge).
                       gates/gate_executor.
  DeliveryPort       — верифицированная доставка (draft PR, SHA-проверка).
                       delivery/pr_open (акт доставки); ревью ветки запускает движковые гейты —
                       engine/review_branch.
  PolicyPort         — решение о допустимости действия (автономия/HITL).
                       governance/policy_engine, engine/tool_broker.
  ClassifierPort     — классификация задачи (роль/workflow/риск).
                       shared/ai_route (foundation: чистая классификация, зовут lifecycle+engine).

Только аннотации, без runtime-логики. Structural typing (Protocol), stdlib только.
"""
from __future__ import annotations

from typing import Any, Protocol, TypedDict, runtime_checkable

from ai_ops_kit.shared.contracts import (
    ContextBundle,
    DeliveryReceipt,
    GateResultV2,
    RunReport,
)


# ============================================================================
# TypedDict-контракты для входов/выходов портов (не Protocol, просто данные)
# ============================================================================

# ExecutionSpec — что исполнять (вход ExecutorPort).
# Поля намеренно широкие (total=False): конкретный набор зависит от workflow.
class ExecutionSpec(TypedDict, total=False):
    """Спецификация исполнения — вход ExecutorPort.

    Минимальный набор: task (что сделать), signals (контекст задачи), child_root (корень
    child-репозитория). Остальное — опциональные параметры прогона.
    """
    task: str
    signals: dict[str, Any]
    child_root: str
    feature: str
    write_scope: list[str]
    max_steps: int
    commit: bool
    baseline_diff: bool
    require_fix: bool
    sandbox: bool
    review: bool
    author: bool
    resume: bool
    base: str


# ExecutionResult — результат исполнения (выход ExecutorPort).
class ExecutionResult(TypedDict, total=False):
    """Результат исполнения — выход ExecutorPort.

    Совместим с RunReport (основной контракт pipeline).
    """
    overall_status: str           # done|blocked|error
    ready_for_pr: bool
    report: RunReport
    error: str | None
    changed_files: list[str]
    committed_sha: str | None
    cost_usd: float | None


# Evidence — результат сбора доказательств.
class Evidence(TypedDict, total=False):
    """Evidence — набор проверок для оценки гейтов.

    Ключи: build_passed, lint_passed, typecheck_passed, tests_passed + security,
    ui_evidence, и т.д. Форма определяется schemas/gate-evidence.schema.json.
    """
    checks: dict[str, Any]
    security: dict[str, Any]
    ui: dict[str, Any]
    changed_files: list[str]
    tested_revision: str | None


# Change — описание изменения (вход EvidenceProvider).
class Change(TypedDict, total=False):
    """Описание изменения — вход EvidenceProvider.

    Минимальный: child_root + опционально changed_files для targeted testing.
    """
    child_root: str
    changed_files: list[str]
    profile: dict[str, Any]


# PolicyContext — контекст РЕШЕНИЯ о допустимости действия (вход PolicyPort).
# Не путать с engine.run_context.RunContext (dataclass, источник истины прогона).
class PolicyContext(TypedDict, total=False):
    """Контекст принятия решения о допустимости действия.

    Включает: write_scope, sandbox (policy enforcement, не security isolation:
    broker решает, что можно, но агент не изолирован контейнером), risk, workflow, task_type.
    """
    workitem_id: str
    workflow: str
    risk: str
    write_scope: list[str]
    sandbox: bool
    child_root: str


# Action — действие, запрашивающее разрешение (вход PolicyPort).
class Action(TypedDict, total=False):
    """Действие, которое исполнитель хочет совершить.

    Формат совместим с tool_broker: {op, path, command, content}.
    """
    op: str                       # read|write|shell|git
    path: str
    command: str
    content: str


# Autonomy — решение о допустимости (выход PolicyPort).
class Autonomy(TypedDict, total=False):
    """Решение политики: разрешено/запрещено/требует подтверждения.

    Совместимо с tool_broker Policy decision.
    """
    allowed: bool
    level: str                    # autonomous|controlled-write|read-only|deny
    reason: str
    write_scope: list[str]


# Classification — результат классификации задачи.
class Classification(TypedDict, total=False):
    """Результат классификации задачи (роль/workflow/риск).

    Совместимо с ai_route.route() output.
    """
    workflow: str
    provider: str
    model_class: str
    runtime: str
    execution_mode: str
    risk: str
    task_type: str
    fallbacks: list[str]


# ============================================================================
# Protocol'ы портов
# ============================================================================

@runtime_checkable
class ExecutorPort(Protocol):
    """Вероятностный исполнитель — ЕДИНСТВЕННЫЙ недетерминированный шаг ядра.

    Реализация: providers/orchestrator (или внешний рантайм — Claude Code, Codex, и т.д.).
    Ядро не знает, КАК модель генерирует код; оно знает, ЧТО исполнить (ExecutionSpec)
    и получает РЕЗУЛЬТАТ (ExecutionResult).
    """

    def run(self, spec: ExecutionSpec) -> ExecutionResult:
        ...


@runtime_checkable
class ContextPort(Protocol):
    """Сборка контекста для WorkItem — компиляция правил/спек/решений в prompt.

    Реализация: context/context_compiler.
    Ядро принимает готовый ContextBundle, не строит его само.
    """

    def build(self, task: str, signals: dict[str, Any], child_root: str) -> ContextBundle:
        ...


@runtime_checkable
class EvidenceProvider(Protocol):
    """Детерминированный сбор evidence (build/lint/test/scan).

    Реализации: gates/evidence_collector, security/security_scan, checks/.
    Ядро зовёт collect() и получает Evidence — не знает, какие команды запускались.
    """

    def collect(self, change: Change) -> Evidence:
        ...


@runtime_checkable
class GatePort(Protocol):
    """Оценка quality gates — fail-closed, writer≠judge.

    Реализация: gates/gate_executor.
    Ядро передаёт Evidence + workflow и получает список GateResultV2.
    """

    def evaluate(self, evidence: Evidence, workflow: str,
                 tested_revision: str | None = None) -> list[GateResultV2]:
        ...


@runtime_checkable
class DeliveryPort(Protocol):
    """Верифицированная доставка — draft PR с SHA-проверкой.

    Реализация: delivery/pr_open (акт доставки) + engine/review_branch (ревью ветки).
    Ядро зовёт deliver() ПОСЛЕ durable-фиксации RunHandoff.
    """

    def deliver(self, work_root: str, work_branch: str, base_ref: str,
                base_sha: str, committed_sha: str, workitem_id: str,
                task: str) -> DeliveryReceipt:
        ...


@runtime_checkable
class PolicyPort(Protocol):
    """Решение о допустимости действия — автономия/HITL, fail-closed.

    Реализация: governance/policy_engine, engine/tool_broker.
    Ядро зовёт decide() ПЕРЕД каждым действием исполнителя.
    """

    def decide(self, action: Action, ctx: PolicyContext) -> Autonomy:
        ...


@runtime_checkable
class ClassifierPort(Protocol):
    """Классификация задачи — детерминированно, по реестрам.

    Реализация: shared/ai_route (foundation).
    Ядро зовёт classify() для определения workflow/риск/роль ДО исполнения.
    """

    def classify(self, signals: dict[str, Any]) -> Classification:
        ...
