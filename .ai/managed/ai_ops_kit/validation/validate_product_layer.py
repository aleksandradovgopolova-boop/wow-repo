#!/usr/bin/env python3
"""Валидация Product Operating Layer дочки: отчёт Missing / Invalid / Outdated / Valid (PR-5).

`ai-ops validate product-layer` отвечает на вопрос «в каком состоянии обязательные продуктовые
артефакты дочки» — и отвечает ЧЕТЫРЬМЯ состояниями, а не «есть/нет». Различение обязательно:

  Missing  — файла нет;
  Invalid  — есть, но структура нарушена ИЛИ раздел присутствует пустым (`is_file()` != заполнен,
             F-018/F-027): пустая секция не равна отсутствующей, но и не равна заполненной;
  Outdated — структура цела, но версия шаблона старее реестра — нужна миграция;
  Valid    — есть, структура полна, содержимое есть, версия актуальна.

Состояние считает `ai_ops_kit/planning/product_templates` по реестру артефактов. ЗОВЁМ ЕГО
ПОДПРОЦЕССОМ, а не импортом: прямой импорт добавил бы ребро `validation -> planning` и с ним циклы
через `lifecycle -> validation` — тот же приём развязки, что в `validate_product_model` (3.34).

  validate_product_layer.py <repo> [--json]
Тесты валидатора — в tests/unit/ (selftest не живёт в продакшн-модуле, AGENTS.md).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
           Path(__file__).resolve().parents[1])

STATES = ("missing", "invalid", "outdated", "valid")


def _report(repo_root) -> tuple[dict | None, str | None]:
    """Отчёт состояния слоя ПОДПРОЦЕССОМ. -> (dict|None, ошибка|None).

    PYTHONPATH=PKG, чтобы `-m ai_ops_kit.planning.product_templates` резолвился и у кита, и в дочке
    (там PKG = .ai/managed). Развязка через процесс — осознанная, см. докстроку модуля.
    """
    try:
        r = subprocess.run(
            [sys.executable, "-m", "ai_ops_kit.planning.product_templates", "state",
             str(repo_root), "--json"],
            capture_output=True, text=True, timeout=120, check=False,
            env={**os.environ, "PYTHONPATH": str(PKG)})
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"product_templates не запустился: {e}"
    out = (r.stdout or "").strip()
    if not out:
        return None, ((r.stderr or "").strip().splitlines() or ["пустой вывод"])[0][:200]
    try:
        return json.loads(out), None
    except json.JSONDecodeError:
        return None, (out.splitlines() or [""])[0]


def check(repo_root) -> list:
    """Ошибки продуктового слоя: каждый НЕ-Valid обязательный артефакт — строка. -> список строк.

    Необязательные артефакты в статусе, отличном от Valid, ошибкой не считаются (их отсутствие
    законно); но здесь все объявленные обязательными, поэтому любой не-Valid — ошибка слоя.
    """
    rep, err = _report(repo_root)
    if err:
        return [f"product-layer: {err}"]
    errs = []
    for aid, v in (rep.get("artifacts") or {}).items():
        if v.get("state") != "valid":
            errs.append(f"{aid}: {v.get('state')} — {v.get('reason')}")
    return errs


def report(repo_root) -> dict:
    """Полный отчёт (для CLI/машины): состояния всех артефактов + свод. -> dict."""
    rep, err = _report(repo_root)
    if err:
        return {"ok": False, "error": err, "artifacts": {}, "counts": {}}
    rep["ok"] = bool(rep.get("valid"))
    return rep


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print("использование: validate_product_layer.py <repo> [--json]")
        return 2
    repo = Path(args[0])
    as_json = "--json" in argv
    rep = report(repo)
    if as_json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep.get("ok") else 1
    if rep.get("error"):
        print(f"PRODUCT-LAYER: не удалось получить отчёт: {rep['error']}")
        return 1
    for aid, v in (rep.get("artifacts") or {}).items():
        mark = {"valid": "✓", "outdated": "~", "invalid": "✗", "missing": "∅"}.get(v["state"], "?")
        print(f"  {mark} {v['state']:9} {aid}: {v['reason']}")
    c = rep.get("counts") or {}
    print(f"\nСЛОЙ: valid {c.get('valid', 0)} · outdated {c.get('outdated', 0)} · "
          f"invalid {c.get('invalid', 0)} · missing {c.get('missing', 0)}")
    return 0 if rep.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
