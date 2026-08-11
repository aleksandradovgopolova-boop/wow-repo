#!/usr/bin/env python3
"""Regression Evidence (v3.30, находка раунда C) — доказательство того, что правка ЧИНИТ.

Проблема. Цикл останавливается, когда writer сам объявляет `{"done": true}`, а гейт
`implementation_verification` проверяет, что build/lint/typecheck/tests проходят на ревизии — то
есть что НИЧЕГО НЕ СЛОМАЛОСЬ. Проверки «то, что чинили, теперь работает» нет нигде. В раунде C
живой квалификации (ии-среда, 2026-08-06) на задачах T1/T2/T4 writer правил код, тесты и до правки
проходили, гейты вставали зелёными — а доказательства исправления не производилось вообще. Кит
ловил только регрессии (T3, T7), и это ровно то, что он умел.

Доказательство. Тест, добавленный вместе с правкой, прогоняется на БАЗОВОЙ ревизии — на коде ДО
правки. Он обязан там упасть: тест, который проходит и без исправления, ничего не доказывает
(побочно ловится off-scope вроде T5, где writer добавил тест в чужой модуль). На HEAD тот же тест
обязан пройти — это уже проверяет implementation_verification.

Инварианты честности:
  * нечем прогнать (стек не даёт команду тестов) -> unverifiable, НЕ «доказано»;
  * теста нет -> not_proven с прямой причиной, а не молчаливый пропуск;
  * правок кода нет (доки/конфиги) -> not_applicable, гейт не выдумывает работу;
  * рефакторинг без смены поведения закрывается ЯВНЫМ объявлением writer'а с причиной
    (`{"done": true, "behavior_unchanged": "..."}`), которое видно в отчёте — не тихим обходом.

Использование:  regression_evidence.py prove <root> <base_sha> <head_sha> [--json]
                regression_evidence.py --selftest
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402,F401

# Пути, которые считаем тестами. Намеренно широко: язык не угадываем, ориентируемся на
# общепринятые соглашения именования. Ложноположительное «это тест» безопаснее обратного —
# оно лишь смягчает гейт до обычной проверки прогоном на базе.
_TEST_MARKERS = ("test_", "_test.", ".test.", ".spec.", "_spec.")
_TEST_DIRS = ("tests/", "test/", "__tests__/", "spec/")

# Документация поведения не меняет — требовать на неё регрессионный тест бессмысленно. Список
# намеренно узкий: конфиги и манифесты сюда НЕ входят, они поведение менять умеют.
_DOC_SUFFIXES = (".md", ".markdown", ".rst", ".txt", ".adoc")
_DOC_NAMES = ("LICENSE", "NOTICE", "CODEOWNERS")


def is_doc_path(path: str) -> bool:
    p = str(path).replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    return name in _DOC_NAMES or any(name.endswith(x) for x in _DOC_SUFFIXES)


def is_test_path(path: str) -> bool:
    p = str(path).replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    if any(seg in p for seg in _TEST_DIRS):
        return True
    return name.startswith("test_") or any(m in name for m in _TEST_MARKERS)


def classify_changed(files):
    """Изменённые файлы -> {'tests': [...], 'code': [...], 'docs': [...]}.

    Код — всё, что не тест и не документация. Конфиги и манифесты считаются кодом сознательно:
    они меняют поведение, и «это же просто конфиг» — типовая формулировка, за которой едет
    непроверенная правка."""
    tests, code, docs = [], [], []
    for f in files or []:
        if is_test_path(f):
            tests.append(f)
        elif is_doc_path(f):
            docs.append(f)
        else:
            code.append(f)
    return {"tests": tests, "code": code, "docs": docs}


def _git(root, *args):
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _test_command(profile):
    """Команда тестов из RepositoryProfile. None — стек её не дал (и выдумывать нельзя)."""
    for stack in (profile or {}).get("stacks") or []:
        cmd = (stack.get("commands") or {}).get("test")
        if cmd:
            return cmd
    return None


def prove(root, base_sha, head_sha, profile, changed_files=None, runner=None, timeout=900):
    """-> RegressionEvidence: доказан ли тестом факт исправления.

    runner(cmd, cwd) -> returncode — инъекция для тестов (без реального прогона тулчейна)."""
    root = Path(root)
    result = {"kind": "RegressionEvidence", "status": "not_proven", "base_sha": base_sha,
              "head_sha": head_sha, "checks": [], "tests_changed": [], "code_changed": []}

    files = changed_files if changed_files is not None else []
    split = classify_changed(files)
    result["tests_changed"], result["code_changed"] = split["tests"], split["code"]
    result["docs_changed"] = split["docs"]

    if not split["code"]:
        result["status"] = "not_applicable"
        result["reason"] = "правок кода нет (только тесты и/или документация) — доказывать нечего"
        return result

    if not split["tests"]:
        result["status"] = "not_proven"
        result["reason"] = ("код изменён, но ни одного теста не добавлено и не изменено — "
                            "исправление ничем не подтверждено")
        return result

    cmd = _test_command(profile)
    if not cmd:
        result["status"] = "unverifiable"
        result["reason"] = ("стек не дал команду тестов — прогнать новый тест на базовой ревизии "
                            "нечем; отсутствие проверки НЕ засчитывается за доказательство")
        return result
    result["test_command"] = cmd

    if not (base_sha and head_sha):
        result["status"] = "unverifiable"
        result["reason"] = "нет base_sha/head_sha — не от чего отсчитывать «до правки»"
        return result

    # Временное дерево на БАЗОВОЙ ревизии + только тестовые файлы из коммита: изолируем вопрос
    # «ловит ли новый тест старую ошибку» от всего остального содержимого правки.
    import tempfile
    wt = Path(tempfile.mkdtemp(prefix="ai-ops-regression-"))
    rc_add, _, err_add = _git(root, "worktree", "add", "--detach", str(wt), base_sha)
    if rc_add != 0:
        result["status"] = "unverifiable"
        result["reason"] = f"не удалось создать дерево на базовой ревизии: {err_add[:200]}"
        return result
    try:
        # ШАГ 0 (обязательный): набор тестов должен отрабатывать на базе САМ ПО СЕБЕ. Без этого
        # ненулевой код после подсадки теста ничего не значит.
        # Поймано живым прогоном на ии-среде: во временном дереве нет node_modules, `npm run test`
        # вернул 127 «command not found», и это было засчитано за «тест падает на базе» —
        # сфабрикованное доказательство ровно того класса, который кит обязан не допускать.
        if runner is not None:
            rc_clean = runner(cmd, str(wt))
        else:
            try:
                rc_clean = subprocess.run(cmd, shell=True, cwd=str(wt), capture_output=True,
                                          timeout=timeout).returncode
            except subprocess.TimeoutExpired:
                rc_clean = "timeout"
        result["checks"].append({"id": "suite_runs_on_base", "command": cmd,
                                 "returncode": rc_clean,
                                 "status": "pass" if rc_clean == 0 else "fail"})
        if rc_clean != 0:
            result["status"] = "unverifiable"
            result["reason"] = (
                f"на базовой ревизии набор тестов не отрабатывает сам по себе (код {rc_clean}) — "
                "среда не готова (нет зависимостей/тулчейна) или база уже красная; "
                "падение после подсадки теста в таких условиях ничего не доказывает")
            return result

        rc_co, _, err_co = _git(wt, "checkout", head_sha, "--", *split["tests"])
        if rc_co != 0:
            result["status"] = "unverifiable"
            result["reason"] = f"не удалось перенести тесты на базовую ревизию: {err_co[:200]}"
            return result
        if runner is not None:
            rc_test = runner(cmd, str(wt))
        else:
            try:
                rc_test = subprocess.run(cmd, shell=True, cwd=str(wt), capture_output=True,
                                         timeout=timeout).returncode
            except subprocess.TimeoutExpired:
                result["status"] = "unverifiable"
                result["reason"] = f"прогон тестов на базовой ревизии не уложился в {timeout}с"
                return result
        result["checks"].append({"id": "test_fails_on_base", "command": cmd,
                                 "returncode": rc_test,
                                 "status": "pass" if rc_test != 0 else "fail"})
        if rc_test != 0:
            result["status"] = "proven"
            result["reason"] = ("новый тест падает на коде ДО правки и проходит после — "
                                "исправление подтверждено")
        else:
            result["status"] = "not_proven"
            result["reason"] = ("новый тест проходит и на коде ДО правки — он не покрывает "
                                "исправление (или правка ничего не меняла)")
        return result
    finally:
        _git(root, "worktree", "remove", "--force", str(wt))


def gate_evidence(proof, behavior_unchanged=None):
    """RegressionEvidence -> evidence для гейта regression_test_evidence.

    behavior_unchanged — объявление writer'а «поведение не менялось» с причиной. Закрывает гейт,
    но ГРОМКО: причина уходит в warnings и в отчёт. Молчаливого обхода нет."""
    proof = proof or {}
    st = proof.get("status")
    if st == "proven":
        return {"status": "pass", "provided": ["regression_proven"],
                "checks": proof.get("checks") or [],
                "evidence": [proof.get("reason") or "исправление подтверждено тестом на базе"]}
    if st == "not_applicable":
        return {"status": "pass", "provided": ["regression_proven"], "checks": [],
                "evidence": [proof.get("reason") or "правок кода нет"]}
    if behavior_unchanged:
        return {"status": "pass", "provided": ["regression_proven"], "checks": [],
                "warnings": [f"поведение объявлено неизменным: {behavior_unchanged}"],
                "evidence": ["объявление writer'а вместо доказательства тестом"]}
    return {"status": "fail", "checks": proof.get("checks") or [],
            "blockers": [proof.get("reason") or "исправление не подтверждено тестом",
                         "если поведение не менялось (рефакторинг) — заверши цикл как "
                         '{"done": true, "behavior_unchanged": "<причина>"}']}


def main(argv):
    ap = argparse.ArgumentParser(description="Regression Evidence")
    ap.add_argument("cmd", nargs="?", choices=["prove"], default=None)
    ap.add_argument("root", nargs="?")
    ap.add_argument("base_sha", nargs="?")
    ap.add_argument("head_sha", nargs="?")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd != "prove" or not (a.root and a.base_sha and a.head_sha):
        ap.print_help()
        return 1
    from ai_ops_kit.shared import project_detector
    from ai_ops_kit.engine.pipeline_git import _committed_changed_files
    prof = project_detector.load_or_detect(a.root, write=False)
    res = prove(a.root, a.base_sha, a.head_sha, prof, _committed_changed_files(a.root, a.head_sha))
    print(json.dumps(res, ensure_ascii=False, indent=2) if a.json
          else f"{res['status']}: {res.get('reason', '')}")
    return 0 if res["status"] in ("proven", "not_applicable") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
