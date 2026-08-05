#!/usr/bin/env python3
"""Синхронность пред-коммит-чеклиста AGENTS.md с CI (v2.15) — drift-control.

AGENTS.md обещает: «Прогнать полный набор проверок (тот же, что в CI
`.github/workflows/package-quality.yml`)». Но список команд ведётся руками
в двух местах, поэтому расходится: агент, честно выполнивший чеклист и
получивший все PASS, всё равно может запушить PR, красный в CI (если в CI
добавили проверку, а в чеклист — забыли).

Этот валидатор детерминированно проверяет инвариант **CI ⊆ AGENTS.md**:
каждая команда `python3 ...`, которую запускает workflow, обязана
присутствовать в bash-чеклисте AGENTS.md. Обратное направление
(в чеклисте команд БОЛЬШЕ, чем в CI) — допустимо: локально можно гонять
дополнительные проверки.

Сравнение по нормализованной команде: отбрасываются перенаправления
(`> /dev/null`), хвостовые комментарии и лишние пробелы; учитываются путь
скрипта и его аргументы (`--selftest` и пример — разные проверки).

Использование:  validate_agents_checklist.py [--json] | --selftest
Возврат 0 — чеклист покрывает CI, 1 — есть непокрытая проверка (или ошибка чтения).
"""

import json
import re
import sys
import tempfile
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
WORKFLOW = Path(".github/workflows/package-quality.yml")
CHECKLIST = Path("AGENTS.md")


def normalize(line: str) -> str:
    """Нормализовать shell-команду: убрать redirect, комментарий, лишние пробелы."""
    line = line.split(">", 1)[0]      # отбросить перенаправление вывода
    line = line.split("#", 1)[0]      # отбросить хвостовой комментарий
    return " ".join(line.split())


def ci_commands(root: Path) -> set:
    """Множество нормализованных `python3 ...` команд из CI-workflow."""
    doc = yaml.safe_load((root / WORKFLOW).read_text(encoding="utf-8"))
    cmds = set()
    for job in (doc.get("jobs") or {}).values():
        for step in (job.get("steps") or []):
            run = step.get("run")
            if not isinstance(run, str):
                continue
            for raw in run.splitlines():
                norm = normalize(raw)
                if norm.startswith("python3 "):
                    cmds.add(norm)
    return cmds


def checklist_commands(root: Path) -> set:
    """Множество нормализованных `python3 ...` команд из bash-блоков AGENTS.md."""
    text = (root / CHECKLIST).read_text(encoding="utf-8")
    cmds = set()
    for block in re.findall(r"```(?:bash|sh)?\n(.*?)```", text, re.DOTALL):
        for raw in block.splitlines():
            norm = normalize(raw)
            if norm.startswith("python3 "):
                cmds.add(norm)
    return cmds


def check(root: Path):
    """Вернуть список команд, которые CI запускает, а чеклист AGENTS.md — нет."""
    ci = ci_commands(root)
    doc = checklist_commands(root)
    return sorted(ci - doc)


def run(root: Path, as_json=False):
    missing = check(root)
    if as_json:
        print(json.dumps({"schema_version": 1, "kind": "agents-checklist-sync",
                          "missing_from_checklist": missing}, ensure_ascii=False, indent=2))
    elif missing:
        print(f"CHECKLIST-DRIFT: {len(missing)} проверок CI отсутствуют в чеклисте AGENTS.md:")
        for c in missing:
            print(f"  CI запускает, но AGENTS.md не перечисляет:  {c}")
    else:
        print("CHECKLIST-OK: чеклист AGENTS.md покрывает все проверки CI.")
    return 1 if missing else 0


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    # 1) реальный пакет: чеклист покрывает CI
    expect("реальный пакет: CI ⊆ AGENTS.md", check(PKG) == [])

    # 2) нормализация: redirect/комментарий не мешают совпадению
    expect("redirect отбрасывается",
           normalize("python3 tools/x.py a > /dev/null") == "python3 tools/x.py a")
    expect("комментарий отбрасывается",
           normalize("python3 x.py  # note") == "python3 x.py")

    # 3) искусственный слом: CI знает проверку, которой нет в чеклисте
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / ".github" / "workflows").mkdir(parents=True)
        (root / WORKFLOW).write_text(
            "jobs:\n"
            "  quality:\n"
            "    steps:\n"
            "      - run: python3 validation/shared.py\n"
            "      - run: |\n"
            "          python3 validation/only_in_ci.py --selftest\n"
            "          python3 validation/shared.py > /dev/null\n",
            encoding="utf-8")
        (root / CHECKLIST).write_text(
            "# AGENTS\n\n```bash\npython3 validation/shared.py\n```\n",
            encoding="utf-8")
        missing = check(root)
        expect("ловит проверку, которой нет в чеклисте",
               missing == ["python3 validation/only_in_ci.py --selftest"])
        expect("общую проверку (даже с redirect) НЕ считает пропущенной",
               "python3 validation/shared.py" not in missing)

    print("validate_agents_checklist selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    return run(PKG, as_json="--json" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
