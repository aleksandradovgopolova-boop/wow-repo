#!/usr/bin/env python3
"""Product Decision Loop — цикл принятия продуктовых решений.

Механизм: AI предлагает решение → владелец одобряет/отклоняет → цикл замыкается.

Структура решения:
- id: уникальный идентификатор
- proposal: что предлагается (текстом)
- context: почему это важно (ссылки на данные, метрики)
- options: альтернативы (если есть)
- recommendation: что рекомендует AI
- status: pending | approved | rejected | deferred
- decided_at: когда принято решение
- decided_by: кто принял
- outcome: что получилось после реализации
- feature_target: (опц.) три измеримых обязательства фичи — baseline (где мы
  сейчас), target (куда идём) и guardrails (что не должно сломаться). Форму
  проверяет check_feature_target; при её наличии решение получает
  kind: feature-decision. Механизм ПРОВЕРЯЕТ форму, а не верит на слово.

Хранение: .ai/project/decisions/YYYY-MM-DD-<id>.yaml

Использование:
    decision_loop.py <child_root> propose --id <id> --proposal "текст" [--options "a,b,c"]
        [--feature-target '{"baseline": {...}, "target": {...}, "guardrails": [...]}']
    decision_loop.py <child_root> decide --id <id> --status approved [--by "owner"]
    decision_loop.py <child_root> list [--status pending]
    decision_loop.py <child_root> --selftest
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

# Проверяющая логика формы feature_target и гейт каталога решений вынесены ВНИЗ в пакет `checks`
# (слой primitives, #541): контур гейтов (`gates.gate_executor`) не вправе импортировать
# `intelligence` (слой выше + kernel-boundary), поэтому чистая/read-only проверка живёт в `checks`,
# а оба вызывателя тянут её ВНИЗ. Имена ре-экспортируются для обратной совместимости API и тестов.
from ai_ops_kit.checks.feature_decision import (  # noqa: F401
    DIRECTIONS,
    check_feature_target,
    gate_feature_decisions,
)


def _decisions_dir(root: Path) -> Path:
    """Get decisions directory."""
    d = root / ".ai" / "project" / "decisions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _decision_path(root: Path, decision_id: str) -> Path:
    """Get path for a specific decision."""
    today = datetime.now().strftime("%Y-%m-%d")
    return _decisions_dir(root) / f"{today}-{decision_id}.yaml"


def propose(root: Path, decision_id: str, proposal: str, context: str = "",
            options: list[str] | None = None, recommendation: str = "",
            feature_target: dict | None = None) -> dict:
    """Propose a new decision.

    Если передан feature_target, его форма проверяется ДО записи: невалидный
    контракт возвращает error и файл НЕ пишется (fail-closed). Валидный —
    сохраняется в решении, а kind становится feature-decision (это отличает
    измеримое обязательство от свободного текста для гейта validate_decisions).
    """
    path = _decision_path(root, decision_id)

    if path.exists():
        return {"error": f"Decision {decision_id} already exists"}

    kind = "product-decision"
    if feature_target is not None:
        ft_errors = check_feature_target(feature_target)
        if ft_errors:
            return {"error": "invalid feature_target", "feature_target_errors": ft_errors}
        kind = "feature-decision"

    decision = {
        "schema_version": 1,
        "kind": kind,
        "id": decision_id,
        "created_at": datetime.now().isoformat(),
        "proposal": proposal,
        "context": context,
        "options": options or [],
        "recommendation": recommendation,
        "status": "pending",
        "decided_at": None,
        "decided_by": None,
        "outcome": None,
    }
    if feature_target is not None:
        decision["feature_target"] = feature_target

    path.write_text(yaml.dump(decision, allow_unicode=True, default_flow_style=False),
                    encoding="utf-8")

    return {"status": "proposed", "path": str(path), "decision": decision}


def decide(root: Path, decision_id: str, status: str, decided_by: str = "owner",
           outcome: str = "") -> dict:
    """Record a decision on a proposal."""
    if status not in ("approved", "rejected", "deferred"):
        return {"error": f"Invalid status: {status}. Must be approved|rejected|deferred"}

    # Find the decision file
    decisions_dir = _decisions_dir(root)
    decision_files = list(decisions_dir.glob(f"*-{decision_id}.yaml"))

    if not decision_files:
        return {"error": f"Decision {decision_id} not found"}

    path = decision_files[0]
    decision = yaml.safe_load(path.read_text(encoding="utf-8"))

    decision["status"] = status
    decision["decided_at"] = datetime.now().isoformat()
    decision["decided_by"] = decided_by
    if outcome:
        decision["outcome"] = outcome

    path.write_text(yaml.dump(decision, allow_unicode=True, default_flow_style=False),
                    encoding="utf-8")

    return {"status": "decided", "decision": decision}


def list_decisions(root: Path, status_filter: str | None = None) -> list[dict]:
    """List all decisions."""
    decisions_dir = _decisions_dir(root)
    if not decisions_dir.exists():
        return []

    decisions = []
    for f in sorted(decisions_dir.glob("*.yaml")):
        try:
            d = yaml.safe_load(f.read_text(encoding="utf-8"))
            if status_filter and d.get("status") != status_filter:
                continue
            decisions.append(d)
        except (yaml.YAMLError, OSError):
            continue

    return decisions


def has_feature_decision_contract(root: Path) -> bool:
    """Есть ли в каталоге решений хотя бы один ВАЛИДНЫЙ Decision-контракт фичи.

    Контракт = решение kind=feature-decision, несущее feature_target, который
    проходит check_feature_target (baseline/target/guardrails). Пустой или
    отсутствующий каталог -> False. Проверка READ-ONLY: каталог не создаётся
    (в отличие от list_decisions, которая идёт через propose-путь), чтобы
    fail-closed преамбула маршрута ничего не писала в дочку до старта.

    #564 (built≠wired): это ТА функция, через которую decision_loop проведён в
    рантайм-маршрут. Присутствие контракта проверяет она; КАЧЕСТВО объявленных
    контрактов — блокирующий гейт feature_decision_quality в пайплайне (тот же
    check_feature_target, вынесенный вниз в checks.feature_decision).
    """
    ddir = Path(root) / ".ai" / "project" / "decisions"
    if not ddir.exists():
        return False
    for f in sorted(ddir.glob("*.yaml")):
        try:
            d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError):
            continue
        if not isinstance(d, dict) or d.get("kind") != "feature-decision":
            continue
        ft = d.get("feature_target")
        if ft is not None and not check_feature_target(ft):
            return True
    return False


def decision_contract_gate(signals, root, task=None, feature=None) -> dict | None:
    """#564: fail-closed присутствие Decision-контракта для ТРИГГЕРНОГО профиля работы.

    Триггер — сигнал `feature_decision_declared` (тот же, что делает трек FEATURE_DECISIONS
    обязательным). Под ним работа не стартует без Decision-контракта — валидного фича-решения в
    `.ai/project/decisions`. Для остальных работ сигнал не взведён -> None (не мешаем): governance
    подключается ПО РИСКУ (три кольца). Проверяется ПРИСУТСТВИЕ (fail-closed, до траты); КАЧЕСТВО
    объявленных контрактов — блокирующий гейт feature_decision_quality в пайплайне.

    Возвращает payload-словарь для блокировки (exit=2) либо None, если продвижение разрешено.
    Маршрут (cli) зовёт это на входе `run --execute`/`do` — это и есть проводка decision_loop в
    рантайм: до #564 модуль импортировали только тесты.
    """
    if not (signals or {}).get("feature_decision_declared"):
        return None
    if has_feature_decision_contract(Path(root)):
        return None
    wid = feature or "<wid>"
    propose_cmd = (
        f"python3 -m ai_ops_kit.intelligence.decision_loop {root} propose "
        f"--id {wid} --proposal \"{task or '<что предлагается>'}\" "
        "--feature-target '{\"baseline\": {\"metric\": \"...\", \"value\": ...}, "
        "\"target\": {\"value\": ..., \"direction\": \"decrease\"}, "
        "\"guardrails\": [{\"metric\": \"...\", \"bound\": \"...\"}]}'")
    return {
        "kind": "decision-contract-missing", "exit": 2,
        "message": (
            "работа заявлена как фича-решение, но для неё нет Decision-контракта — измеримого "
            "обязательства (где мы сейчас, куда идём, что не должно сломаться). Без него запускать "
            "нельзя: результат будет нечем признать успехом. Заведите решение и повторите запуск."),
        "propose_command": propose_cmd,
    }


def format_decisions(decisions: list[dict]) -> str:
    """Format decisions into human-readable output."""
    if not decisions:
        return "No decisions found."

    lines = ["# Product Decisions\n"]

    # Group by status
    by_status = {}
    for d in decisions:
        s = d.get("status", "unknown")
        by_status.setdefault(s, []).append(d)

    for status in ["pending", "approved", "rejected", "deferred"]:
        items = by_status.get(status, [])
        if items:
            lines.append(f"\n## {status.upper()} ({len(items)})\n")
            for d in items:
                lines.append(f"### {d.get('id', '?')}\n")
                lines.append(f"**Proposal:** {d.get('proposal', '?')}\n")
                if d.get("context"):
                    lines.append(f"**Context:** {d['context']}\n")
                if d.get("recommendation"):
                    lines.append(f"**Recommendation:** {d['recommendation']}\n")
                if d.get("decided_at"):
                    lines.append(f"**Decided:** {d['decided_at']} by {d.get('decided_by', '?')}\n")
                if d.get("outcome"):
                    lines.append(f"**Outcome:** {d['outcome']}\n")

    return "\n".join(lines)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Product Decision Loop — цикл принятия решений")
    ap.add_argument("root", nargs="?", default=".", help="Repository root")

    sub = ap.add_subparsers(dest="command")

    # Propose
    propose_p = sub.add_parser("propose", help="Propose a new decision")
    propose_p.add_argument("--id", required=True, help="Decision ID")
    propose_p.add_argument("--proposal", required=True, help="What is proposed")
    propose_p.add_argument("--context", default="", help="Why it matters")
    propose_p.add_argument("--options", default="", help="Comma-separated alternatives")
    propose_p.add_argument("--recommendation", default="", help="AI recommendation")
    propose_p.add_argument("--feature-target", default="",
                           help="JSON: {baseline, target, guardrails} — измеримое обязательство фичи")

    # Decide
    decide_p = sub.add_parser("decide", help="Record a decision")
    decide_p.add_argument("--id", required=True, help="Decision ID")
    decide_p.add_argument("--status", required=True, choices=["approved", "rejected", "deferred"])
    decide_p.add_argument("--by", default="owner", help="Who decided")
    decide_p.add_argument("--outcome", default="", help="What happened after")

    # List
    list_p = sub.add_parser("list", help="List decisions")
    list_p.add_argument("--status", choices=["pending", "approved", "rejected", "deferred"])
    list_p.add_argument("--json", action="store_true")

    ap.add_argument("--selftest", action="store_true")

    args = ap.parse_args()

    if args.selftest:
        # ЧЕСТНЫЙ --selftest (фаза 0, 19.08.2026). Здесь печаталась строка о пройденном
        # селфтесте и три строки «... : OK» — без единого вызова проверяемых функций. То есть
        # модуль УТВЕРЖДАЛ проверку, которой не было: ровно класс «объявлено, но не
        # исполняется», против которого стоит весь кит (ср. R-31/R-32 — две фиктивные проверки
        # в валидаторах). Образец честной формы — devtools/mutation_probe.py: модуль объясняет
        # себя и называет, где лежат его настоящие проверки. Правило репозитория (AGENTS.md):
        # тест модуля живёт в tests/, а не в продакшн-модуле, который едет в child-репозиторий.
        print(__doc__)
        print("Проверки модуля — в tests/unit/ (AGENTS.md: selftest не живёт в продакшн-модуле).")
        return 0

    root = Path(args.root).resolve()

    if args.command == "propose":
        options = [o.strip() for o in args.options.split(",") if o.strip()] if args.options else []
        feature_target = None
        if getattr(args, "feature_target", ""):
            try:
                feature_target = json.loads(args.feature_target)
            except json.JSONDecodeError as exc:
                print(json.dumps({"error": f"--feature-target: невалидный JSON ({exc})"},
                                 indent=2, ensure_ascii=False))
                return 1
        result = propose(root, args.id, args.proposal, args.context, options,
                         args.recommendation, feature_target)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if "error" in result:
            return 1

    elif args.command == "decide":
        result = decide(root, args.id, args.status, args.by, args.outcome)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "list":
        decisions = list_decisions(root, args.status)
        if hasattr(args, "json") and args.json:
            print(json.dumps(decisions, indent=2, ensure_ascii=False))
        else:
            print(format_decisions(decisions))

    else:
        ap.print_help()

    return 0


if __name__ == "__main__":
    sys.exit(main())
