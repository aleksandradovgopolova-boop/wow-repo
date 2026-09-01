"""Чистая проверка структурного результата ревьюера. Вынесена из
`validation/validate_reviewer_result.py` вниз (лента №5), чтобы рантайм (engine.pipeline_evidence,
providers.orchestrator) звал её ВНИЗ, без восходящих рёбер engine/providers -> validation.

Reviewer возвращает структуру (schemas/reviewer-result.schema.json): status/checks/blockers.
Инварианты: schema_version/kind/gate/status на месте; status ∈ pass|warn|fail; gate резолвится
(если передан gate_ids); каждый check несёт id+status; fail/warn обязаны иметь blockers; check
fail ⇒ общий status не pass. Только stdlib — никакого ввода-вывода (чтение реестра гейтов остаётся
в CLI-обёртке).
"""
from __future__ import annotations

ST = {"pass", "warn", "fail"}


def check(data: dict, gate_ids=None):
    errors = []
    if data.get("schema_version") is None:
        errors.append("нет schema_version")
    if data.get("kind") != "reviewer-result":
        errors.append("kind должен быть 'reviewer-result'")
    if data.get("status") not in ST:
        errors.append(f"status '{data.get('status')}' не в {sorted(ST)}")
    gid = data.get("gate")
    if not gid:
        errors.append("нет gate")
    elif gate_ids is not None and gid not in gate_ids:
        errors.append(f"gate '{gid}' отсутствует в quality/gates.yaml")

    checks = data.get("checks")
    if not isinstance(checks, list) or not checks:
        errors.append("checks должен быть непустым списком")
        checks = []
    any_fail = False
    for c in checks:
        if not isinstance(c, dict) or not c.get("id") or c.get("status") not in ST:
            errors.append("check требует id:str + status∈[pass,warn,fail]")
            continue
        if c["status"] == "fail":
            any_fail = True

    # v3.0-rc11 (finding живого прогона kimi): warn на блокирующем гейте БЛОКИРУЕТ (v2.85) — значит
    # это тоже блокирующий вердикт и обязан назвать конкретику. Contentless warn = «блок без причины»
    # (унфальсифицируемый). Симметрия честности: fail и warn одинаково требуют непустой blockers.
    if data.get("status") in ("fail", "warn") and not (data.get("blockers")):
        errors.append(f"status={data.get('status')} требует непустой blockers (блокирующий вердикт без причины)")
    if any_fail and data.get("status") == "pass":
        errors.append("есть check со status=fail, но общий status=pass — несогласованно")
    return errors
