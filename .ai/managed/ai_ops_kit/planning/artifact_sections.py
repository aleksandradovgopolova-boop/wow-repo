#!/usr/bin/env python3
"""SR-5/6: секционная валидация обязательных артефактов — ТРИ состояния секции.

SR-6: секция ЕСТЬ и заполнена; секции НЕТ; секция ЕСТЬ и ПУСТА. Третье — отдельная находка:
заголовок без содержания хуже отсутствия заголовка, потому что выглядит закрытым. Сворачивать
«пусто» в «есть» запрещено так же, как `unknown` в `ok`.

SR-5: список обязательных секций объявлен ДАННЫМИ — `registry/product-operating-model.yaml`
→ `contours[].source_of_truth[].required_sections`, а не в этом коде. Добавление секции в
требования не требует правки Python.

Одна правда о том, что такое «заголовок» и «пустая секция»: логика переиспользована из
`product_templates` (слой `.ai-ops/` уже так проверяет свои артефакты), а не заведена второй раз.

Использование: python3 -m ai_ops_kit.planning.artifact_sections <repo_root> [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ai_ops_kit.planning.product_templates import (
    _empty_sections,
    _has_section,
    _markdown_headers,
)

FILLED = "filled"
MISSING = "missing"
EMPTY = "empty"


def section_states(text: str, required_sections: list) -> list:
    """Состояние каждой требуемой секции в тексте артефакта. -> [{name, state}].

    state: filled (есть и заполнена), missing (заголовка нет), empty (заголовок есть, тело пусто —
    комментарии и пробелы телом не считаются).
    """
    headers = _markdown_headers(text)
    empty = set(_empty_sections(text, list(required_sections)))
    out = []
    for name in required_sections:
        if not _has_section(headers, name):
            state = MISSING
        elif name in empty:
            state = EMPTY
        else:
            state = FILLED
        out.append({"name": name, "state": state})
    return out


def _resolve(root: Path, rel: str):
    """Путь артефакта: корень репо, затем оверлеи .ai/project и .ai/custom (как в repo_audit)."""
    for pre in ("", ".ai/project/", ".ai/custom/"):
        p = root / (pre + rel)
        if p.is_file():
            return p
    return None


def report(child_root, model: dict | None = None) -> dict:
    """Секционные состояния всех обязательных артефактов, объявивших required_sections. -> отчёт."""
    from ai_ops_kit.planning import contours as _contours
    model = model or _contours.load_model()
    root = Path(child_root)
    artifacts = []
    for c in model.get("contours") or []:
        for s in c.get("source_of_truth") or []:
            secs = s.get("required_sections")
            if not secs:
                continue
            path = _resolve(root, s["path"])
            if path is None:
                artifacts.append({"contour": c["id"], "path": s["path"], "present": False,
                                  "sections": [{"name": n, "state": MISSING} for n in secs]})
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            artifacts.append({"contour": c["id"], "path": s["path"], "present": True,
                              "sections": section_states(text, secs)})
    return {"kind": "artifact-sections-report", "artifacts": artifacts}


def _render(rep: dict) -> str:
    lines = ["Секции обязательных артефактов (SR-5/6): заполнено / пусто / нет"]
    for a in rep["artifacts"]:
        empty = [x["name"] for x in a["sections"] if x["state"] == EMPTY]
        missing = [x["name"] for x in a["sections"] if x["state"] == MISSING]
        filled = [x["name"] for x in a["sections"] if x["state"] == FILLED]
        head = f"  {a['path']}: заполнено {len(filled)}/{len(a['sections'])}"
        if empty:
            head += f"; ПУСТЫ (выглядят закрытыми, но пусты): {', '.join(empty)}"
        if missing:
            head += f"; НЕТ раздела: {', '.join(missing)}"
        lines.append(head)
    return "\n".join(lines)


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="Секционная валидация обязательных артефактов (SR-5/6)")
    ap.add_argument("repo_root", nargs="?", default=".")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rep = report(a.repo_root)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(_render(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
