#!/usr/bin/env python3
"""Stack-aware evidence collector (v3.26.0, Execution Engine — детерминированный сбор evidence).

Замыкает Project Detector -> gate. RepositoryProfile (ai_ops_kit/shared/project_detector.py) знает команды
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

Библиотечный модуль: `collect()` принимает broker ПАРАМЕТРОМ (DI). CLI-обёртка, которая строит
broker (`engine.tool_broker`) и печатает результат, вынесена в точку входа
`ai_ops_kit/devtools/evidence_collect_cli.py` (K1, F-03): гейт не зависит от engine ни статически,
ни динамически.
"""
from __future__ import annotations

import shlex
import shutil
from pathlib import Path

import yaml

from ai_ops_kit.shared import _bootstrap  # noqa: E402
from ai_ops_kit.shared import project_detector       # noqa: E402
from ai_ops_kit.gates import verification_tiers     # noqa: E402  v3.26.0
# профиль стиля дочки (#1183): линия и её сравнение — в checks (слой ниже gates, импорт вниз разрешён)
from ai_ops_kit.checks import lint_baseline, lint_profile
# текст находки «стиль кода никто не держит» — в слое речи (ui ниже gates, импорт вниз разрешён)
from ai_ops_kit.ui.presenter_report_formatters import CODE_STYLE_POLICIES, code_style_unguarded

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


def _docs_only_result(revision, impact_status, verification_info):
    """Изменение только документации -> ОСВОБОЖДЕНИЕ с названной причиной (не пустой pass).

    ПРОПУСК ОБЯЗАН БЫТЬ ОСВОБОЖДЕНИЕМ, А НЕ ПУСТЫМ `pass` (B2-08, живой прогон 14.08).

    Прежде эта ветка возвращала `status: pass` с единственным флагом `skip_verification`,
    которого НЕТ в `required_evidence`, и `not_applicable: []`. Дальше `gate_executor`
    честно не находил ни одного из пяти обязательных флагов и превращал такой pass в
    БЛОКИРУЮЩИЙ отказ «бездоказательный pass». То есть ветка, созданная чтобы пропустить
    проверку, сама её и заваливала — на ЛЮБОМ репозитории, включая кит: воспроизведено на
    полном наборе команд, отсутствие тестов у продукта тут ни при чём.
    Цена: ни одно изменение только документации не могло дойти до владельца.

    Теперь флаги объявлены НЕПРИМЕНИМЫМИ с названной причиной, и `gate_executor` пишет
    это в warnings: проверка не выдумана, она явно не делалась и сказано почему.
    `tested_revision` в освобождение НЕ входит — ревизия известна, это настоящее
    доказательство, и подменять его освобождением значило бы прятать факт за отговоркой."""
    return {
        "schema_version": 1, "kind": "evidence-collection",
        "revision": revision, "checks": {},
        "schema_evidence": {},
        "gate_evidence": {"implementation_verification": {
            "status": "pass",
            # веха 4.2 (#588): коллектор — детерминированный путь (реальные exit-коды),
            # честно помечаем источник как deterministic.
            "source": "deterministic",
            "provided": ["skip_verification", "tested_revision"],
            "evidence": [f"skip_reason:{impact_status}", f"revision:{revision}"],
        }},
        "not_applicable": ["build_passed", "lint_passed", "typecheck_passed",
                           "tests_passed"],
        "not_applicable_reason": "изменение только документации — продуктовые проверки не применимы",
        "tests_absent": False,
        "verification": verification_info,
    }


def lint_policy(root) -> tuple:
    """`.ai-ops.yaml -> standard.lint` СУДИМОЙ ревизии -> (policy, как объявлено).

    Читается из дерева, которое судит гейт: настройка — часть той же ревизии, что и код. Ключа нет
    -> `advisory` (новый ключ с рабочим значением по умолчанию не ломает дочку). Незнакомое значение
    -> `invalid`, и потребитель трактует его строже, а не мягче (fail-closed): опечатка в
    `required` не должна тихо превращать обязательную проверку в совет. Нечитаемый файл — как у
    прочих читателей конфига кита: намерения владельца из него не извлечь, работает умолчание."""
    for fname in (".ai-ops.yaml", ".ai-ops.yml"):
        cfg = Path(root) / fname
        if not cfg.is_file():
            continue
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return "advisory", None
        std = data.get("standard") if isinstance(data, dict) else None
        declared = std.get("lint") if isinstance(std, dict) else None
        if declared is None:
            return "advisory", None
        return (declared if declared in CODE_STYLE_POLICIES else "invalid"), declared
    return "advisory", None


def style_finding(profile, root):
    """Находка владельцу «стиль кода никто не проверяет» (#1183) -> dict | None.

    Прежде отсутствие линтера молча уходило в `not_applicable`, и гейт писал «освобождено (нет
    инструмента в стеке): lint_passed» — внутреннее имя и причину без последствия. Последствие
    видно только в дочке: каждый агент пишет по-своему, стиль расползается.

    Находка только для стеков, у которых детектор УМЕЕТ искать линтер: для остальных «не нашёл»
    значило бы «не искал», и сказать «никто не проверяет» было бы неправдой (неизвестно ≠ нет).
    Нет стека вовсе — тоже не находка: о коде, которого кит не распознал, судить нечем."""
    bare = project_detector.lint_unguarded(profile)
    if not bare:
        return None
    policy, declared = lint_policy(root)
    return {"kind": "code-style-unguarded", "languages": bare, "policy": policy,
            "declared": declared, "blocking": policy != "advisory",
            "text": code_style_unguarded(bare, policy, declared)}


def _broker_run(broker, policy):
    """`run(argv, cwd)` для `lint_baseline.run_tool` через Tool Broker: процесс гейт сам не запускает.

    Отказ политики или обрезанный исполнителем вывод — код 2 с причиной: инструмент «не отработал»,
    а не «нашёл ноль». Результат ESLint/Ruff/golangci-lint идёт файлом, обрезка их не касается."""
    limit = getattr(broker, "SHELL_OUTPUT_TAIL", None)

    def run(argv, cwd):
        exe = Path(argv[0])
        if exe.parent.name == ".bin" and exe.parent.parent.name == "node_modules":
            # локальный бинарь node — через `npx --offline` (бинарь уже стоит, сеть не нужна): так
            # команду пропускает и allowlist песочницы, где путь к node_modules/.bin не значится
            argv = ["npx", "--offline", exe.name, *argv[1:]]
        elif shutil.which(exe.name) == str(exe):
            argv = [exe.name, *argv[1:]]         # найден в PATH — зовём по имени, как команды профиля
        ev = broker.execute({"op": "shell", "command": shlex.join(argv)}, cwd, policy)
        if not ev.get("allowed"):
            return 2, f"запуск отклонён политикой исполнения ({str(ev.get('reason'))[:120]})"
        tail = ev.get("output_tail") or ""
        if ev.get("exit_code") is None:
            return 2, f"не запустился: {tail}"
        if limit and len(tail) >= limit and argv and Path(argv[0]).name in ("ast-grep", "lint-imports"):
            return 2, "вывод длиннее, чем отдаёт исполнитель — находки не сосчитать"
        return ev["exit_code"], tail
    return run


def style_profile(root, broker, policy):
    """Профиль стиля дочки (`./ai-ops lint-profile --apply`) против замороженной линии -> dict | None.

    None — профиль не включён: доказательство `lint_passed` считается как прежде. Иначе результат
    `lint_baseline.check` (то же сравнение, что у команды `check`, хука и CI) + адреса роста."""
    tools = lint_profile.applied_tools(root)
    if not tools:
        return None
    res = lint_baseline.check(root, tools, lambda r, spec, extra=(), targets=None: lint_baseline.run_tool(
        r, spec, _broker_run(broker, policy), extra, targets))
    return {**res, "addresses": [lint_baseline.address(g) for g in lint_baseline.grown(res)]}


def _apply_style_profile(checks_report, provided, blockers, not_applicable, warnings, style):
    """Рост сверх линии профиля роняет `lint_passed`; «проверено не всё» — предупреждение."""
    lint = checks_report["lint"]
    lint["style_profile"] = style
    if style["status"] == lint_baseline.GREW:
        lint["status"] = "fail"
        for bucket in (provided, not_applicable):
            if "lint_passed" in bucket:
                bucket.remove("lint_passed")
        head = "; ".join(style["addresses"][:8]) + (" …" if len(style["addresses"]) > 8 else "")
        blockers.append(f"lint: профиль стиля — новые расхождения сверх замороженного: {head}")
    missed = [f"{r['tool']}: {r.get('reason')}" for r in style["tools"]
              if r["status"] == lint_baseline.NOT_CHECKED]
    if missed:
        warnings.append("профиль стиля проверен не весь — " + "; ".join(missed))


def _code_style(profile, root, broker, policy, checks_report, provided, blockers, not_applicable):
    """Стиль кода поверх exit-кода линтера (#1183) -> (находка «никто не держит» | None, предупреждения).

    Два источника: нет линтера — находка владельцу (при `standard.lint: required` — блокер без
    освобождения); включён профиль стиля — рост сверх его линии снимает `lint_passed`. Списки
    правятся на месте: их дальше читает `collect`."""
    style = style_finding(profile, root)
    if style:
        checks_report["lint"]["finding"] = style
        if style["blocking"]:
            blockers.append(style["text"])
            if "lint_passed" in not_applicable:
                not_applicable.remove("lint_passed")
    warnings: list = []
    prof = style_profile(root, broker, policy)
    if prof:
        _apply_style_profile(checks_report, provided, blockers, not_applicable, warnings, prof)
    return style, warnings


def _add_warnings(iv: dict, extra: list) -> None:
    if extra:
        iv["warnings"] = list(iv.get("warnings") or []) + extra


def collect(profile, root, policy, changed_files=None, broker=None, lifecycle_intent=None):
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
        verification_info = verification_tiers.select_tests(changed_files, str(root), lifecycle_intent=lifecycle_intent, profile=profile)
        tier = verification_info.get("tier", "affected")
        impact_status = verification_info.get("impact_status")
        targeted_cmd = verification_info.get("targeted_command")

        # v3.27.3 WP4: skip tier — docs-only, не запускаем product build/test
        if tier == "skip":
            return _docs_only_result(revision, impact_status, verification_info)

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

    # стиль кода без линтера — находка владельцу, а не молчаливое освобождение (#1183); при
    # `standard.lint: required` (или непонятом значении) — отказ без освобождения
    style, style_warnings = _code_style(profile, root, broker, policy, checks_report, provided,
                                        blockers, not_applicable)
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
            # веха 4.2 (#588): источник этого evidence — детерминированный (exit-коды реальных
            # build/lint/typecheck/test-команд), не AI-суждение.
            "source": "deterministic",
            "provided": provided,
            "evidence": ev_strings,
        }
    }
    if blockers:
        gate_evidence["implementation_verification"]["blockers"] = blockers
    if style and not style["blocking"]:
        # освобождение по lint_passed ОБЪЯСНЕНО последствием — общая строка «нет инструмента»
        # его больше не называет (gate_executor, explained_exemptions)
        gate_evidence["implementation_verification"].update(
            warnings=[style["text"]], explained_exemptions=["lint_passed"])
    _add_warnings(gate_evidence["implementation_verification"], style_warnings)

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


# CLI-обёртка (argparse + построение broker + печать) вынесена в
# ai_ops_kit/devtools/evidence_collect_cli.py — см. докстринг модуля (K1, F-03).
