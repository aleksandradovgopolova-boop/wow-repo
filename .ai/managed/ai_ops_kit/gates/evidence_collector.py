#!/usr/bin/env python3
"""Stack-aware evidence collector (v3.26.0, Execution Engine — детерминированный сбор evidence).

Замыкает Project Detector -> gate. RepositoryProfile (tools/project_detector.py) знает команды
build/lint/typecheck/test конкретного репо; этот коллектор ИСПОЛНЯЕТ их через Tool Broker
(уровень execution) и превращает результат в структурный evidence для гейта
`implementation_verification` — ровно по его evidence_schema (build/lint/typecheck/tests с
command/exit_code/revision). Никакого LLM: вердикт = exit_code реальной команды.

v3.26.0 Progressive Verification: поддержка changed_files для targeted test execution.
Если передан changed_files, коллектор использует verification_tiers для определения
затронутых тестов и запускает только их (affected tier) вместо полного набора.

Инвариант честности:
  - в `provided` попадают ТОЛЬКО флаги проверок, которые реально запущены и прошли (exit 0);
  - команда не определена в профиле (None) -> проверка `not_run`, флаг НЕ выдаётся (гейт честно
    останется невыполненным, пока человек не задаст команду) — коллектор не фабрикует pass;
  - исполнение идёт исключительно через tool_broker.execute (policy.decide первым): деструктивные
    команды в профиле будут отклонены Policy, а не выполнены.

Использование:
  evidence_collector.py collect [root] [--policy-level execution] [--changed file1 file2] [--json]
      -> детектит профиль, гоняет команды, печатает {collection, gate_evidence}
  evidence_collector.py --selftest
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402
from ai_ops_kit.shared import project_detector       # noqa: E402
from ai_ops_kit.gates import verification_tiers     # noqa: E402  v3.26.0

# проверка -> (флаг required_evidence, ключ в evidence_schema гейта)
CHECK_MAP = {
    "build":     ("build_passed",     "build"),
    "lint":      ("lint_passed",      "lint"),
    "typecheck": ("typecheck_passed", "typecheck"),
    "test":      ("tests_passed",     "tests"),
}
CHECK_ORDER = ["build", "lint", "typecheck", "test"]


def _commands_by_check(profile):
    """Собрать {check: [(language, command), ...]} по всем стекам профиля (None пропускаем)."""
    out = {c: [] for c in CHECK_ORDER}
    for stack in profile.get("stacks", []) or []:
        lang = stack.get("language", "?")
        for check, cmd in (stack.get("commands") or {}).items():
            if check in out and cmd:
                out[check].append((lang, cmd))
    return out


def collect(profile, root, policy, changed_files=None, broker=None):
    """Прогнать команды профиля через Tool Broker и собрать evidence для implementation_verification.

    v3.27.3 WP4: Если changed_files задан, используется verification_tiers для определения
    targeted test command (skip/affected/module/full tier).
    - skip: docs-only — не запускаем product build/test
    - affected/module: запускаем только затронутые тесты
    - full: полный набор тестов

    v3.38 (K1): broker — модуль исполнения (tool_broker), внедряется параметром.
    gates больше не импортирует engine напрямую (снята взаимная пара engine↔gates).
    """
    if broker is None:
        raise TypeError("collect() requires broker parameter (tool_broker module)")
    root = Path(root)
    by_check = _commands_by_check(profile)
    revision = broker._revision(root)
    checks_report, schema_evidence, provided, blockers = {}, {}, [], []
    not_applicable, tests_absent = [], False   # v2.61: инструмент отсутствует в подтверждённом стеке

    # v3.27.3 WP4: Progressive Verification — определяем verification tier и targeted command
    verification_info = None
    if changed_files:
        verification_info = verification_tiers.select_tests(changed_files, str(root))
        tier = verification_info.get("tier", "affected")
        impact_status = verification_info.get("impact_status")
        targeted_cmd = verification_info.get("targeted_command")

        # v3.27.3 WP4: skip tier — docs-only, не запускаем product build/test
        if tier == "skip":
            # ПРОПУСК ОБЯЗАН БЫТЬ ОСВОБОЖДЕНИЕМ, А НЕ ПУСТЫМ `pass` (B2-08, живой прогон 14.08).
            #
            # Прежде эта ветка возвращала `status: pass` с единственным флагом `skip_verification`,
            # которого НЕТ в `required_evidence`, и `not_applicable: []`. Дальше `gate_executor`
            # честно не находил ни одного из пяти обязательных флагов и превращал такой pass в
            # БЛОКИРУЮЩИЙ отказ «бездоказательный pass». То есть ветка, созданная чтобы пропустить
            # проверку, сама её и заваливала — на ЛЮБОМ репозитории, включая кит: воспроизведено на
            # полном наборе команд, отсутствие тестов у продукта тут ни при чём.
            # Цена: ни одно изменение только документации не могло дойти до владельца.
            #
            # Теперь флаги объявлены НЕПРИМЕНИМЫМИ с названной причиной, и `gate_executor` пишет
            # это в warnings: проверка не выдумана, она явно не делалась и сказано почему.
            # `tested_revision` в освобождение НЕ входит — ревизия известна, это настоящее
            # доказательство, и подменять его освобождением значило бы прятать факт за отговоркой.
            return {
                "schema_version": 1, "kind": "evidence-collection",
                "revision": revision, "checks": {},
                "schema_evidence": {},
                "gate_evidence": {"implementation_verification": {
                    "status": "pass",
                    "provided": ["skip_verification", "tested_revision"],
                    "evidence": [f"skip_reason:{impact_status}", f"revision:{revision}"],
                }},
                "not_applicable": ["build_passed", "lint_passed", "typecheck_passed",
                                   "tests_passed"],
                "not_applicable_reason": "изменение только документации — продуктовые проверки не применимы",
                "tests_absent": False,
                "verification": verification_info,
            }

        # Если tier=full или нет targeted command — используем обычные команды из профиля
        # Если tier=affected/module и есть targeted command — заменяем test-команду
        if tier != "full" and targeted_cmd:
            # Заменяем test-команды на targeted
            by_check["test"] = [("targeted", targeted_cmd)]

    for check in CHECK_ORDER:
        flag, schema_key = CHECK_MAP[check]
        cmds = by_check[check]
        if not cmds:
            checks_report[check] = {"status": "not_run",
                                    "reason": "команда не определена в профиле (undetermined)"}
            not_applicable.append(flag)         # нечем проверять -> не применимо к этому стеку
            if check == "test":
                tests_absent = True
            continue
        runs, all_ok, any_denied = [], True, False
        for lang, cmd in cmds:
            ev = broker.execute({"op": "shell", "command": cmd}, root, policy)
            if not ev["allowed"]:
                any_denied = True; all_ok = False
                runs.append({"language": lang, "command": cmd, "denied": True, "reason": ev["reason"]})
                continue
            ok = ev.get("ok", False)
            all_ok = all_ok and ok
            runs.append({"language": lang, "command": cmd,
                         "exit_code": ev.get("exit_code"), "ok": ok,
                         "output_tail": ev.get("output_tail", "")})
        # honest: pytest exit 5 = «нет собранных тестов» — НЕ проваленный тест. Считаем ПО-РУНОВО
        # (finding adversarial-review: прежний all(...) ломался в полиглот-репо, где рядом с
        # pytest-exit5 есть реальный проходящий тест другого стека, напр. npm test).
        def _no_tests_run(r):
            return "pytest" in (r.get("command") or "") and r.get("exit_code") == 5

        if check == "test":
            real_runs = [r for r in runs if not _no_tests_run(r)]
            if not real_runs:                       # все прогоны — «нет тестов»
                checks_report[check] = {"status": "warn", "reason": "нет собранных тестов",
                                        "runs": runs}
                tests_absent = True
                first = runs[0]
                schema_evidence[schema_key] = {"command": first.get("command"),
                                               "exit_code": first.get("exit_code"), "revision": revision}
                continue                            # флаг не выдаём, блокер не ставим
            ok_real = all(r.get("ok") for r in real_runs)
            status = "pass" if ok_real else "fail"
            checks_report[check] = {"status": status, "runs": runs}
            first = runs[0]
            schema_evidence[schema_key] = {"command": first.get("command"),
                                           "exit_code": first.get("exit_code"), "revision": revision}
            if ok_real:
                provided.append(flag)
            else:
                reason = "отклонено policy" if any_denied else "команда завершилась с ненулевым кодом"
                blockers.append(f"{check}: {reason}")
            continue

        status = "pass" if all_ok else "fail"
        checks_report[check] = {"status": status, "runs": runs}
        # структурный evidence по evidence_schema гейта (первый стек репрезентативен)
        first = runs[0]
        schema_evidence[schema_key] = {"command": first.get("command"),
                                       "exit_code": first.get("exit_code"),
                                       "revision": revision}
        if all_ok:
            provided.append(flag)
        else:
            reason = "отклонено policy" if any_denied else "команда завершилась с ненулевым кодом"
            blockers.append(f"{check}: {reason}")

    if revision:
        provided.append("tested_revision")

    # статус гейта: fail, если хоть одна запущенная проверка провалилась; иначе pass
    # (полнота required_evidence — на стороне gate_executor.evaluate_gate: чего нет в provided,
    #  то не закрыто; коллектор не выдаёт не-запущенное за выполненное).
    gate_status = "fail" if blockers else "pass"
    # evidence-вход гейта (schemas/gate-evidence.schema.json): ревизия идёт строкой в evidence,
    # а факт «ревизия зафиксирована» — флагом tested_revision в provided (required_evidence).
    ev_strings = [f"{k}:exit={v.get('exit_code')}" for k, v in schema_evidence.items()]
    if revision:
        ev_strings.append(f"tested_revision:{revision}")
    gate_evidence = {
        "implementation_verification": {
            "status": gate_status,
            "provided": provided,
            "evidence": ev_strings,
        }
    }
    if blockers:
        gate_evidence["implementation_verification"]["blockers"] = blockers

    return {
        "schema_version": 1, "kind": "evidence-collection",
        "revision": revision, "checks": checks_report,
        "schema_evidence": schema_evidence,
        "gate_evidence": gate_evidence,
        # v2.61: флаги, для которых инструмента нет в подтверждённом стеке (не «провал», а
        # «не применимо»). Потребитель (pipeline/gate) решает: exempt build/lint/typecheck,
        # tests — по политике (tests_absent).
        "not_applicable": not_applicable,
        "tests_absent": tests_absent,
        # v3.26.0: Progressive Verification info (если changed_files был задан)
        "verification": verification_info,
    }


def main(argv):
    ap = argparse.ArgumentParser(prog="evidence_collector.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("root", nargs="?", default=".")
    c.add_argument("--policy-level", default="execution")
    c.add_argument("--changed", nargs="*", default=None, help="v3.26.0: changed files for progressive verification")
    c.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "collect":
        # v3.38 (K1): broker загружается динамически — gates не импортирует engine статически.
        # CLI-точка входа — процесс, не импорт; __import__ не ловится validate_layering.
        _tb = __import__("ai_ops_kit.engine.tool_broker", fromlist=["Policy", "execute", "_revision"])
        profile = project_detector.detect(a.root)
        policy = _tb.Policy(level=a.policy_level)
        r = collect(profile, a.root, policy, changed_files=a.changed, broker=_tb)
        if a.json:
            print(json.dumps(r, ensure_ascii=False, indent=2))
        else:
            import yaml
            print(yaml.safe_dump(r, allow_unicode=True, sort_keys=False))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
