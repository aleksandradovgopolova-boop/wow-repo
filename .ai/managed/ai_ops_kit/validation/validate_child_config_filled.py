#!/usr/bin/env python3
"""Заготовки в `.ai-ops.yaml` дочки не выданы за заполненный конфиг (B2-25, поле 19.08.2026).

ЗАМЕР. В живом продукте с 14.08 в конфиге стоял `project.name: <project-name>` — заготовка,
которую установка ПРОСИТ заменить. `doctor` при этом возвращал 0 и печатал «можно ставить задачу»,
и ни один валидатор этого не требовал. То есть кит просит человека отредактировать файл и не
проверяет, сделано ли это, — а сам же требует от других: «правило без исполнения — пожелание».

ЧТО ИМЕННО ПРОВЕРЯЕТСЯ И ЧТО НЕТ. Заготовка отличается от настоящего значения ровно тем, что кит
её и написал: `<...>`. Осмысленность имени не проверяется и проверяться не может — это была бы
догадка о продукте. Проверяется одно: значение больше не равно тому, что положил установщик.

ПОЧЕМУ НЕ «ЛЮБАЯ УГЛОВАЯ СКОБКА». Матчится значение ЦЕЛИКОМ (`<что-то>`), а не подстрока: строка
вроде `команда работает с <legacy> API` — это описание, а не незаполненное поле, и краснеть на нём
значило бы разучить человека читать эту проверку.

Использование:  validate_child_config_filled.py [repo] [--json]
Возврат 0 — заготовок нет ЛИБО конфига нет вовсе (репозиторий без кита — проверять нечего);
1 — остались незаполненные поля.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

CONFIG_REL = ".ai-ops.yaml"
# значение целиком — угловые скобки установщика; пустые `<>` не считаем (это не заготовка)
PLACEHOLDER = re.compile(r"^<[^<>]+>$")


def _walk(node, path, out):
    if isinstance(node, dict):
        for k, v in node.items():
            _walk(v, path + [str(k)], out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk(v, path + [f"[{i}]"], out)
    elif isinstance(node, str) and PLACEHOLDER.match(node.strip()):
        out.append({"field": ".".join(path), "value": node.strip()})


def assess(root="."):
    """-> {"config_exists", "readable", "placeholders": [{"field","value"}], "reason"}.

    Нечитаемый конфиг НЕ выдаём за «заготовок нет»: это разные вещи, и вторая — молчание.
    """
    p = Path(root) / CONFIG_REL
    if not p.is_file():
        return {"config_exists": False, "readable": None, "placeholders": [],
                "reason": f"{CONFIG_REL} нет — кит здесь не установлен, проверять нечего"}
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"config_exists": True, "readable": False, "placeholders": [],
                "reason": f"{CONFIG_REL} не читается ({type(exc).__name__}) — это НЕ «заготовок нет»"}
    found = []
    _walk(doc, [], found)
    return {"config_exists": True, "readable": True, "placeholders": found,
            "reason": ("незаполненные поля конфига: "
                       + ", ".join(f"{f['field']} = {f['value']}" for f in found)
                       if found else "все поля конфига заполнены")}


def summary_line(root="."):
    """Строка для `doctor`. Разметку ставит тот, кто печатает, — вердикт следует за ней."""
    r = assess(root)
    if not r["config_exists"]:
        return "конфиг дочки: — не установлен (проверять нечего)"
    if r["readable"] is False:
        return f"конфиг дочки: ✗ {r['reason']}"
    if r["placeholders"]:
        fields = ", ".join(f["field"] for f in r["placeholders"])
        return (f"конфиг дочки: ✗ остались заготовки установки ({fields}) — "
                f"замените их на значения проекта в {CONFIG_REL}")
    return "конфиг дочки: ✓ заготовок установки не осталось"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="validate_child_config_filled.py")
    ap.add_argument("repo", nargs="?", default=".")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r = assess(a.repo)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print(summary_line(a.repo))
    return 1 if r["placeholders"] or r["readable"] is False else 0


if __name__ == "__main__":
    sys.exit(main())
