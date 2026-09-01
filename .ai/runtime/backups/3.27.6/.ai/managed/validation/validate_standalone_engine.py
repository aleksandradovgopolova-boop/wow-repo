#!/usr/bin/env python3
"""Validate standalone engine (v2.82 Standalone Child).

Аудит v2.79 (P0.3 / standalone): движок раньше жил только в клоне кита — child не мог
запустить `ai-ops run` без внешнего `git clone` parent-пакета. v2.82 кладёт движок
(tools/ + validation/ + их данные) в managed-слой (`.ai/managed/`), и PKG движка резолвится
как `Path(__file__).parents[1]` == `.ai/managed/`. Этот валидатор ДОКАЗЫВАЕТ самодостаточность,
а не декларирует её:

  1. completeness: строит managed-слой из manifest.update_policy.managed_set и проверяет, что
     в нём присутствует ВЕСЬ рантайм-замыкание движка (ENGINE_CLOSURE) — модули и данные.
     Если из managed_set выпадет файл замыкания — тест падает громко (не тихо в проде).
  2. runtime: запускает движок как ОТДЕЛЬНЫЙ процесс из `.ai/managed/tools/ai_ops_run.py`
     с ЧИСТЫМ окружением (PYTHONPATH снят, cwd = временный child, parent-кит НЕ на path) и
     scripted-proposer'ом, который пишет файл и завершает. Успех = движок дошёл до реального
     коммита на ветке ai-ops/*, собрал evidence на ТОЧНОМ SHA и вернул ready_for_pr=True —
     всё без единого обращения к клону кита.

Честная граница: child-CI валидация (ai-ops-validate.yml) по-прежнему клонирует parent по
тегу — это отдельный контур (пин версии), не путь исполнения движка. Standalone здесь —
про `ai-ops run`, не про CI-валидатор.

Использование:
  validate_standalone_engine.py --selftest        # построить managed из PKG и доказать
  validate_standalone_engine.py <child_root>      # проверить УЖЕ установленный child (.ai/managed)
Возврат 0 — самодостаточен; 1 — нет.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]

# Рантайм-замыкание `ai-ops run --engine pipeline` (получено трассировкой открытых файлов и
# импортированных модулей). Держим ЯВНО, чтобы регресс managed_set падал этим тестом.
ENGINE_MODULES = [
    "tools/ai_ops_run.py", "tools/execution_pipeline.py", "tools/tool_broker.py",
    "tools/tool_loop.py", "tools/orchestrator.py", "tools/run_plan.py",
    "tools/run_report.py", "tools/gate_executor.py", "tools/evidence_collector.py",
    "tools/project_detector.py", "tools/budget.py", "tools/workitem.py",
    "tools/active_work.py", "tools/worktree.py", "validation/ai_route.py",
]
ENGINE_DATA = [
    "config/protected-paths.yaml", "quality/gates.yaml",
    "registry/providers.yaml", "registry/routing-policy.yaml",
    "registry/runtimes.yaml", "registry/tracks.yaml", "registry/workflows.yaml",
    # v3.0-rc2: approvals.load_domains() / security_pack грузят это на ЛЮБОМ коммите (universal security)
    "security/security-domains.yaml",
]
ENGINE_CLOSURE = ENGINE_MODULES + ENGINE_DATA


def build_managed(pkg_root: Path, dest_managed: Path):
    """Скопировать managed_set пакета в dest_managed (как это делает installer).
    Возвращает число скопированных файлов."""
    manifest = yaml.safe_load((pkg_root / "manifest" / "ai-ops-manifest.yaml").read_text(encoding="utf-8"))
    patterns = manifest.get("update_policy", {}).get("managed_set", []) or []
    n = 0
    for pat in patterns:
        eff = pat + "/*" if pat.endswith("/**") else pat
        for src in sorted(pkg_root.glob(eff)):
            if src.is_file():
                dst = dest_managed / src.relative_to(pkg_root)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                n += 1
    return n


def missing_closure(managed: Path):
    """Каких файлов рантайм-замыкания движка не хватает в managed-слое."""
    return [rel for rel in ENGINE_CLOSURE if not (managed / rel).exists()]


DRIVER = r'''
import sys, json
from pathlib import Path
MANAGED = Path(sys.argv[1]).resolve()
CHILD = Path(sys.argv[2]).resolve()
# ЕДИНСТВЕННЫЙ источник кода — managed-слой; parent-кит на path НЕ добавляем.
sys.path.insert(0, str(MANAGED / "tools"))
sys.path.insert(0, str(MANAGED / "validation"))
import execution_pipeline
script = iter([
    {"op": "write", "path": "src/add.py", "content": "def add(a, b):\n    return a + b\n"},
    {"done": True, "summary": "add"},
])
rep = execution_pipeline.run_pipeline(
    "добавить add", {"task_type": "QUICK", "size": "small", "risk": "low",
                     "affected_areas": ["core"]},
    CHILD, lambda c: next(script), feature="standalone-add",
    commit=True, isolate=True, sandbox=True, install_deps=False)
print(json.dumps(rep, ensure_ascii=False))
'''


def run_standalone(managed: Path, child: Path):
    """Запустить движок из managed-слоя отдельным процессом с чистым окружением. Отчёт (dict) или None."""
    driver = managed.parent.parent / "_standalone_driver.py"   # вне .ai/managed, чтобы не путать closure
    driver.write_text(DRIVER, encoding="utf-8")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)                # ни одного указателя на реальный кит на path
    r = subprocess.run([sys.executable, str(driver), str(managed), str(child)],
                       cwd=str(child), env=env, capture_output=True, text=True)
    if r.returncode != 0 and not r.stdout.strip():
        sys.stderr.write("standalone-driver stderr:\n" + r.stderr[-1500:] + "\n")
        return None
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        sys.stderr.write("standalone-driver: не удалось разобрать JSON:\n" + r.stdout[-800:] + "\n")
        return None


def verify_child(child_root: Path):
    """Проверить уже установленный child: движок в .ai/managed цел (для doctor/CI)."""
    managed = Path(child_root) / ".ai" / "managed"
    if not managed.is_dir():
        print(f"STANDALONE: {managed} отсутствует — кит не установлен?")
        return 1
    miss = missing_closure(managed)
    if miss:
        print(f"STANDALONE: в .ai/managed не хватает файлов движка: {miss}")
        return 1
    print(f"STANDALONE-OK: движок цел в {managed} ({len(ENGINE_CLOSURE)} файлов замыкания).")
    return 0


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        managed = root / ".ai" / "managed"
        n = build_managed(PKG, managed)
        expect(f"managed-слой построен из managed_set ({n} файлов)", n > 0)

        miss = missing_closure(managed)
        expect(f"рантайм-замыкание движка целиком в managed (нет пропусков: {miss or 'ok'})", not miss)
        expect("движок присутствует (.ai/managed/tools/ai_ops_run.py)",
               (managed / "tools" / "ai_ops_run.py").exists())

        # временный child-репозиторий
        child = root / "childrepo"
        child.mkdir()
        subprocess.run(["git", "-C", str(child), "init", "-q"])
        subprocess.run(["git", "-C", str(child), "config", "user.email", "t@t"])
        subprocess.run(["git", "-C", str(child), "config", "user.name", "t"])
        (child / "src").mkdir()
        (child / "pyproject.toml").write_text("[tool.poetry]\n", encoding="utf-8")
        (child / "f").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(child), "add", "-A"])
        subprocess.run(["git", "-C", str(child), "commit", "-q", "-m", "init"])

        rep = run_standalone(managed, child)
        expect("движок отработал из managed БЕЗ parent-клона (валидный отчёт)",
               rep is not None and rep.get("kind") == "execution-pipeline")
        if rep:
            commit = rep.get("commit") or {}
            expect("standalone: реальный коммит на ветке ai-ops/* (SHA 40 hex)",
                   isinstance(commit.get("sha"), str) and len(commit.get("sha") or "") == 40
                   and (commit.get("branch") or "").startswith("ai-ops/"))
            expect("standalone: evidence на ТОЧНОМ зафиксированном SHA",
                   commit.get("evidence_on_exact_sha") is True)
            expect("standalone: ready_for_pr=True (движок довёл прогон до готовности)",
                   rep.get("ready_for_pr") is True)
            expect("standalone: containment активен (sandbox + block_push)",
                   (rep.get("containment") or {}).get("block_push") is True
                   and (rep.get("containment") or {}).get("sandbox") is True)
            expect("standalone: файл действительно записан движком в child",
                   (child / ".ai" / "worktrees" / "standalone-add" / "src" / "add.py").exists())

        # негатив: убрать файл замыкания -> completeness ловит
        (managed / "tools" / "tool_broker.py").unlink()
        expect("completeness ловит пропажу файла движка (tool_broker удалён)",
               "tools/tool_broker.py" in missing_closure(managed))

    print("validate_standalone_engine selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("-")]
    if args:
        return verify_child(Path(args[0]))
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
