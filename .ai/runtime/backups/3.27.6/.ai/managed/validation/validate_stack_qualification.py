#!/usr/bin/env python3
"""Стек-квалификация движка на РЕАЛЬНЫХ фикстурах — детерминированно, без живой модели (v2.91).

Живые сценарии (qualification/scenarios.yaml) требуют модель и гоняются на машине пользователя.
Но самое хрупкое в движке — стек-СПЕЦИФИЧНЫЙ разбор: детект стека и извлечение id падений из
вывода РАЗНЫХ раннеров. Именно там жил баг vite (ложная регрессия по времени сборки) и баг go
(имя упавшего теста не извлекалось -> id схлопывался в мусорный {'FAIL'} -> «починил один тест,
сломал другой» в одном пакете не ловилось = ложный green для go-репо).

Этот харнесс закрывает разрыв БЕЗ модели и работает в CI кита:
  1. project_detector на настоящих фикстурах (python+go) -> верный язык и команда тестов;
  2. _failure_ids/_diff_checks на РЕАЛЬНОМ выводе раннеров (qualification/fixtures/golden/*) ->
     извлекается стабильный id упавшего теста, а swap (починил A, сломал B) = регрессия;
  3. если тулчейн (pytest/go) доступен — ДОП. живой прогон фикстур: вывод всё ещё парсится
     (страховка от дрейфа формата раннера). Нет тулчейна -> честный SKIP, golden-проверки остаются.

Инвариант честности: golden-файлы — снятый живьём вывод раннеров, не выдумка; при отсутствии
тулчейна харнесс не притворяется, что прогнал вживую.

Использование:
  validate_stack_qualification.py            # прогнать все проверки
  validate_stack_qualification.py --selftest # то же (для чек-листа CI)
Возврат 0 — ок, 1 — ошибки.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / "tools"))

import project_detector as pd          # noqa: E402
import execution_pipeline as ep        # noqa: E402

FIX = PKG / "qualification" / "fixtures"
GOLDEN = FIX / "golden"


def _check(tail):
    return {"status": "fail", "runs": [{"output_tail": tail}]}


def _stack(root, lang):
    for s in pd.detect(root).get("stacks", []):
        if s.get("language") == lang:
            return s
    return None


def run_checks():
    """-> (results[(name, ok)], skips[str]). results — детерминированные (всегда); skips — живые
    прогоны, пропущенные из-за отсутствия тулчейна."""
    r, skips = [], []

    def ok(name, cond):
        r.append((name, bool(cond)))

    # 1. Детект стека на настоящих фикстурах
    py = _stack(FIX / "python", "python")
    ok("detect: python-фикстура определена как python", py is not None)
    ok("detect: python test-команда = pytest", py and py["commands"].get("test") == "pytest")
    go = _stack(FIX / "go", "go")
    ok("detect: go-фикстура определена как go", go is not None)
    ok("detect: go test-команда = go test ./...", go and go["commands"].get("test") == "go test ./...")
    ok("detect: go build-команда = go build ./...", go and go["commands"].get("build") == "go build ./...")
    rs = _stack(FIX / "rust", "rust")
    ok("detect: rust-фикстура определена как rust", rs is not None)
    ok("detect: rust test-команда = cargo test", rs and rs["commands"].get("test") == "cargo test")
    jv = _stack(FIX / "java", "java")
    ok("detect: java-фикстура определена как java", jv is not None)
    ok("detect: java test-команда = mvn -q test", jv and jv["commands"].get("test") == "mvn -q test")

    # 2. Разбор РЕАЛЬНОГО вывода раннеров (golden) — извлечение id + swap = регрессия
    pytest_out = (GOLDEN / "pytest.txt").read_text(encoding="utf-8")
    ok("golden pytest: извлечён стабильный node-id test_calc.py::test_sub",
       "test_calc.py::test_sub" in ep._failure_ids(_check(pytest_out)))

    go_sub = (GOLDEN / "go-test-sub.txt").read_text(encoding="utf-8")
    go_add = (GOLDEN / "go-test-add.txt").read_text(encoding="utf-8")
    ok("golden go: извлечено имя упавшего теста TestSub",
       "TestSub" in ep._failure_ids(_check(go_sub)))
    ok("golden go: извлечено имя упавшего теста TestAdd",
       "TestAdd" in ep._failure_ids(_check(go_add)))
    # ключевой инвариант: тот же счётчик падений (1->1), но ДРУГОЙ тест в ОДНОМ пакете = регрессия
    reg, _fixed = ep._diff_checks({"test": _check(go_sub)}, {"test": _check(go_add)})
    ok("golden go: swap (починил TestSub, сломал TestAdd) = регрессия (не ложный green)",
       reg == ["test"])
    # тот же упавший тест -> не регрессия
    reg2, _ = ep._diff_checks({"test": _check(go_sub)}, {"test": _check(go_sub)})
    ok("golden go: тот же упавший тест = НЕ регрессия", reg2 == [])

    rs_sub = (GOLDEN / "rust-test-sub.txt").read_text(encoding="utf-8")
    rs_add = (GOLDEN / "rust-test-add.txt").read_text(encoding="utf-8")
    ok("golden rust: извлечено имя упавшего теста tests::test_sub",
       any("tests::test_sub" in i for i in ep._failure_ids(_check(rs_sub))))
    rreg, _ = ep._diff_checks({"test": _check(rs_sub)}, {"test": _check(rs_add)})
    ok("golden rust: swap (починил test_sub, сломал test_add) = регрессия", rreg == ["test"])

    jv_sub = (GOLDEN / "java-test-sub.txt").read_text(encoding="utf-8")
    jv_add = (GOLDEN / "java-test-add.txt").read_text(encoding="utf-8")
    ok("golden java: извлечён Class.method упавшего теста CalcTest.testSub",
       any("CalcTest.testSub" in i for i in ep._failure_ids(_check(jv_sub))))
    jreg, _ = ep._diff_checks({"test": _check(jv_sub)}, {"test": _check(jv_add)})
    ok("golden java: swap (починил testSub, сломал testAdd) = регрессия", jreg == ["test"])

    # 3. Живой прогон фикстур (если тулчейн доступен) — страховка от дрейфа формата раннера
    if shutil.which("pytest"):
        out = subprocess.run(["pytest", "-q"], cwd=FIX / "python",
                             capture_output=True, text=True).stdout
        ok("live pytest: реальный вывод фикстуры парсится в test_calc.py::test_sub",
           "test_calc.py::test_sub" in ep._failure_ids(_check(out)))
    else:
        skips.append("live pytest: тулчейн pytest недоступен -> только golden")

    if shutil.which("go"):
        with tempfile.TemporaryDirectory() as td:
            for f in ("go.mod", "calc.go", "calc_test.go"):
                shutil.copy(FIX / "go" / f, Path(td) / f)
            out = subprocess.run(["go", "test", "./..."], cwd=td,
                                 capture_output=True, text=True).stdout
        ok("live go: реальный вывод фикстуры содержит --- FAIL: TestSub -> id извлечён",
           "TestSub" in ep._failure_ids(_check(out)))
    else:
        skips.append("live go: тулчейн go недоступен -> только golden")

    if shutil.which("cargo"):
        with tempfile.TemporaryDirectory() as td:
            dst = Path(td) / "rs"
            shutil.copytree(FIX / "rust", dst)
            res = subprocess.run(["cargo", "test"], cwd=dst, capture_output=True, text=True)
            out = res.stdout + res.stderr  # cargo пишет падения в stderr
        ok("live rust: реальный вывод cargo test -> имя теста tests::test_sub извлечено",
           any("tests::test_sub" in i for i in ep._failure_ids(_check(out))))
    else:
        skips.append("live rust: тулчейн cargo недоступен -> только golden")

    # java: живой прогон требует Maven Central (junit5) -> в CI кита не гоняем; golden снят живьём.
    skips.append("live java: прогон требует сети к Maven Central -> проверка по golden (снят живьём)")

    return r, skips


def selftest():
    return main([])


def main(argv):
    if not FIX.is_dir():
        print(f"STACK-QUAL: нет каталога фикстур: {FIX}")
        return 1
    results, skips = run_checks()
    ok = True
    for name, passed in results:
        ok = ok and passed
        print(f"{'PASS' if passed else 'FAIL'} {name}")
    for s in skips:
        print(f"SKIP {s}")
    print("validate_stack_qualification selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
