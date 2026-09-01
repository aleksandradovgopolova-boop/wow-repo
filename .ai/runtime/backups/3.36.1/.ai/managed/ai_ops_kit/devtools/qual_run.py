#!/usr/bin/env python3
"""qual_run.py — квалификационный прогон канонического пути на живой модели (v2.68).

Гоняет N реальных задач через собранный движок (`ai-ops run --engine pipeline --execute`)
на child-репозитории С ТУЛЧЕЙНОМ, складывает JSON-отчёты и печатает сводку pass/fail по
критериям квалификации из p0_backlog: «одна обычная задача -> проверяемый draft PR».
Это НЕ имитация — реальный прогон живого провайдера; поэтому запускается с машины, откуда
доступна модель (напр. DeepSeek с Mac), а ключ берётся только из env.

Критерии успеха задачи (все обязаны выполниться):
  - status != error;
  - loop.stopped == done и denied == 0;
  - commit.sha есть и evidence_on_exact_sha == true (evidence на точной ревизии);
  - gates.blocked == false;
  - ready_for_pr == true.

Использование (с Mac; ключ в env, НЕ в аргументах):
  export OPENAI_COMPATIBLE_BASE_URL="https://api.deepseek.com/chat/completions"
  export OPENAI_COMPATIBLE_API_KEY="…"        # только в терминале
  qual_run.py <child_root> --tasks tasks.txt [--out qual-reports] \
      [--provider openai-compatible] [--model deepseek-chat] [--open-pr]
  qual_run.py --selftest

tasks.txt — по одной задаче на строку; пустые строки и строки, начинающиеся с '#', игнор.
Отчёты: <out>/<NN>-<slug>.json на задачу + <out>/summary.json.
Секреты в отчёт не попадают (Broker вырезает env через scrub_env).
Возврат 0 — все задачи прошли критерии; 1 — есть провал/ошибка; 2 — конфиг/окружение.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402
# Кириллица -> латиница: задачи на русском иначе схлопываются в один slug (коллизия
# workitem_id/имени отчёта). Стандартная транслитерация, только stdlib.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
}


def _translit(text):
    return "".join(_TRANSLIT.get(ch, ch) for ch in (text or "").lower())


def slugify(text, maxlen=48):
    """Безопасный slug для имени файла и workitem_id (совпадает с run_plan.validate_workitem_id).

    Кириллица транслитерируется; если после очистки пусто (символы/emoji) — стабильный
    хэш-суффикс, чтобы разные задачи не коллизировали в один workitem_id/отчёт.
    """
    s = re.sub(r"[^a-z0-9]+", "-", _translit(text)).strip("-")
    s = re.sub(r"-+", "-", s)
    if not s or not re.match(r"^[a-z0-9]", s):
        import hashlib
        h = hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:8]
        s = f"task-{h}"
    return (s[:maxlen].rstrip("-.")) or "task"


def evaluate_report(rep):
    """Вердикт по отчёту движка. Источник истины — ready_for_pr (движок сам учёл критерий:
    all-green или no-regressions в baseline-diff). При не-готовности собираем диагностику."""
    if not isinstance(rep, dict):
        return {"ok": False, "reasons": ["нет отчёта (None) — прогон не вернул результат"]}
    if rep.get("status") == "error":
        return {"ok": False, "reasons": [f"status=error: {rep.get('error')}"]}
    ok = bool(rep.get("ready_for_pr"))
    if ok:
        return {"ok": True, "reasons": []}
    reasons = []
    loop = rep.get("loop") or {}
    commit = rep.get("commit") or {}
    gates = rep.get("gates") or {}
    base = rep.get("baseline") or {}
    if loop.get("stopped") != "done":
        reasons.append(f"loop.stopped={loop.get('stopped')} (ожидалось done)")
    if loop.get("denied"):
        reasons.append(f"denied={loop.get('denied')} (>0)")
    if not commit.get("sha"):
        reasons.append("нет commit.sha (нечего коммитить/коммит не создан)")
    elif not commit.get("evidence_on_exact_sha"):
        reasons.append("evidence НЕ на точном SHA")
    if base.get("regressions"):
        reasons.append(f"регрессии против базы: {base['regressions']}")
    elif gates.get("blocked"):
        reasons.append(f"gates.blocked=true, unmet={gates.get('unmet')}")
    if not reasons:
        reasons.append("ready_for_pr=false")
    return {"ok": False, "reasons": reasons}


def read_tasks(path):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def default_runner(child_root, provider, model, open_pr, task_type="QUICK", baseline_diff=True,
                   require_fix=False, max_steps=40, discard_previous=True, sandbox=False,
                   review=False, author=False):
    """Боевой раннер: одна задача -> отчёт движка через ai_ops_run.run (engine=pipeline, execute).

    task_type по умолчанию QUICK — класс, который pipeline РЕАЛЬНО поддерживает сегодня
    (tool-loop + intake + implementation_verification). ENGINEERING/PRODUCT добавляют гейты
    requirements/specification/plan_readiness/code_review, для которых pipeline пока не
    производит evidence (backlog P0.4 — постадийное исполнение RunPlan) -> они честно
    заблокируют. Класс задаётся флагом --task-type осознанно, не для обхода гейтов.
    """
    from ai_ops_kit.engine import ai_ops_run

    def run_one(task):
        signals = {"task_text": task, "task_type": task_type, "size": "small", "risk": "low",
                   "affected_areas": ["core"]}
        return ai_ops_run.run(task, signals, Path(child_root), provider_name=provider,
                              model=model, engine="pipeline", execute=True,
                              open_pr=open_pr, feature=slugify(task), baseline_diff=baseline_diff,
                              require_fix=require_fix, max_steps=max_steps,
                              discard_previous=discard_previous, sandbox=sandbox, review=review,
                              author=author)
    return run_one


def run_qualification(tasks, out_dir, run_one):
    """Прогнать все задачи, записать отчёты, вернуть список вердиктов."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for i, task in enumerate(tasks, 1):
        slug = slugify(task)
        try:
            rep = run_one(task)
        except SystemExit as e:               # honest error провайдера/конфига (не глотаем)
            rep = {"kind": "execution-pipeline", "status": "error", "error": str(e)}
        except Exception as e:                 # noqa: BLE001 — прогон одной задачи не должен ронять серию
            rep = {"kind": "execution-pipeline", "status": "error",
                   "error": f"{type(e).__name__}: {e}"}
        verdict = evaluate_report(rep)
        (out / f"{i:02d}-{slug}.json").write_text(
            json.dumps({"task": task, "verdict": verdict, "report": rep},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        base = (rep or {}).get("baseline") or {}
        results.append({"index": i, "task": task, "slug": slug,
                        "ok": verdict["ok"], "reasons": verdict["reasons"],
                        "fixed": base.get("fixed"), "regressions": base.get("regressions"),
                        "applied_writes": ((rep or {}).get("loop") or {}).get("applied_writes")})
    (out / "summary.json").write_text(
        json.dumps({"schema_version": 1, "kind": "qual-summary", "results": results},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return results


def print_summary(results):
    passed = sum(1 for r in results if r["ok"])
    for r in results:
        mark = "PASS" if r["ok"] else "FAIL"
        line = f"  [{mark}] {r['index']:02d} {r['slug']}"
        # видно РЕАЛЬНЫЙ эффект правки: что починилось / что сломалось (не только pass/fail)
        tags = []
        if r.get("fixed"):
            tags.append(f"fixed={r['fixed']}")
        if r.get("regressions"):
            tags.append(f"regressions={r['regressions']}")
        if tags:
            line += " (" + ", ".join(tags) + ")"
        if not r["ok"]:
            line += " — " + "; ".join(r["reasons"])
        print(line)
    print(f"QUAL: {passed}/{len(results)} задач прошли критерии квалификации.")
    return len(results) > 0 and passed == len(results)


def main(argv):
    ap = argparse.ArgumentParser(prog="qual_run.py")
    ap.add_argument("child_root")
    ap.add_argument("--tasks", required=True, help="файл со списком задач (по одной на строку)")
    ap.add_argument("--out", default="qual-reports", help="каталог для JSON-отчётов")
    ap.add_argument("--provider", default="openai-compatible")
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--task-type", default="QUICK",
                    help="класс задачи для RunPlan (по умолчанию QUICK — то, что pipeline "
                         "поддерживает сегодня; ENGINEERING/PRODUCT заблокируют гейты без "
                         "evidence — backlog P0.4)")
    ap.add_argument("--open-pr", action="store_true", help="открыть draft PR (нужен GITHUB_TOKEN)")
    ap.add_argument("--strict-green", action="store_true",
                    help="требовать ВСЕ проверки зелёными (по умолчанию — baseline-diff: правка "
                         "лишь не должна вносить НОВЫХ провалов; пред-существующие красные репо не блокируют)")
    ap.add_argument("--require-fix", action="store_true",
                    help="для fix-задач: PASS только если правка РЕАЛЬНО починила падавшую проверку "
                         "(fixed непустой), а не просто 'не сломала'")
    ap.add_argument("--max-steps", type=int, default=40,
                    help="потолок шагов tool-loop (по умолчанию 40; поднимай для reasoning-моделей)")
    ap.add_argument("--sandbox", action="store_true",
                    help="containment (v2.81): shell модели — только allowlist dev-инструментов, "
                         "сеть/push из петли запрещены. Рекомендуется для живых прогонов на "
                         "недоверенных моделях. Полная FS/сеть/ресурс-изоляция — контейнер.")
    ap.add_argument("--review", action="store_true",
                    help="full RunPlan (v2.83): независимый ревью ai-review гейтов отдельным "
                         "вызовом модели под read-only (writer ≠ judge). Нужно для ENGINEERING/"
                         "PRODUCT задач с code_review/ux_review в плане.")
    ap.add_argument("--author", action="store_true",
                    help="product authoring (v2.86): движок производит артефакты requirements/plan "
                         "и подтверждает форму -> закрывает артефакт-гейты. Нужно для ENGINEERING/"
                         "PRODUCT; качество судит --review/человек.")
    a = ap.parse_args(argv)

    # окружение: для живого провайдера ключ и base обязаны быть в env (не в аргументах)
    if a.provider == "openai-compatible":
        missing = [v for v in ("OPENAI_COMPATIBLE_BASE_URL", "OPENAI_COMPATIBLE_API_KEY")
                   if not os.environ.get(v)]
        if missing:
            print(f"КОНФИГ: не заданы env {missing} — экспортируй их в терминале "
                  f"(ключ НЕ передавай аргументом/в чат).")
            return 2
    if a.open_pr and not (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")):
        print("КОНФИГ: --open-pr требует GITHUB_TOKEN/GH_TOKEN в env.")
        return 2

    tasks_path = Path(a.tasks)
    if not tasks_path.exists():
        print(f"КОНФИГ: файл задач не найден: {tasks_path}")
        return 2
    tasks = read_tasks(tasks_path)
    if not tasks:
        print(f"КОНФИГ: в {tasks_path} нет задач (пустые/# игнорируются).")
        return 2

    criterion = "all-green" if a.strict_green else "no-regressions (baseline-diff)"
    print(f"QUAL: {len(tasks)} задач через {a.provider}/{a.model} "
          f"(класс {a.task_type}, критерий {criterion}) на {a.child_root} -> отчёты в {a.out}")
    runner = default_runner(a.child_root, a.provider, a.model, a.open_pr, a.task_type,
                            baseline_diff=not a.strict_green, require_fix=a.require_fix,
                            max_steps=a.max_steps, sandbox=a.sandbox, review=a.review,
                            author=a.author)
    results = run_qualification(tasks, a.out, runner)
    overall = print_summary(results)
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
