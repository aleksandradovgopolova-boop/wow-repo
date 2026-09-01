#!/usr/bin/env python3
"""Контроль целостности managed-слоя (.ai/managed) AI-first системы — Фаза 4.

Обнаруживает прямую (ручную) правку managed-файлов: parent записывает sha256 всех
managed-файлов в `.checksums.json`; перед обновлением суммы пересчитываются, и любое
расхождение (changed / added / removed) означает, что managed-файл трогали вручную —
обновление в этом случае должно остановиться (Section 28), а не перезаписать молча.

Режимы:
  generate [root]  — пересчитать и записать <root>/.checksums.json
  verify   [root]  — пересчитать и сравнить с <root>/.checksums.json (exit 1 при drift)

root по умолчанию — пример-фикстура child-install (для CI).
Метаданные install (.checksums.json, .provenance.json, .update-lock) в сумму не входят.
Требует только стандартную библиотеку.
"""

import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path.cwd()
DEFAULT_ROOT = REPO_ROOT / ".ai" / "managed"
CHECKSUMS_NAME = ".checksums.json"
EXCLUDE = {CHECKSUMS_NAME, ".provenance.json", ".update-lock"}
ALGO = "sha256"


def sha256(path):
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def content_files(root):
    files = []
    for p in sorted(root.rglob("*")):
        if p.is_dir():
            continue
        if p.name in EXCLUDE or p.name == ".gitkeep":
            continue
        files.append(p)
    return files


def compute(root):
    return {p.relative_to(root).as_posix(): sha256(p) for p in content_files(root)}


def generate(root):
    sums = compute(root)
    doc = {"schema_version": 1, "algorithm": ALGO, "managed_root": str(root.name), "files": sums}
    (root / CHECKSUMS_NAME).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"OK: записаны контрольные суммы для {len(sums)} managed-файлов -> {root / CHECKSUMS_NAME}")
    return 0


def verify(root):
    cs = root / CHECKSUMS_NAME
    if not cs.exists():
        print(f"ОШИБКА: нет {cs} — сначала generate.")
        return 1
    try:
        recorded = json.loads(cs.read_text(encoding="utf-8")).get("files", {})
    except json.JSONDecodeError as e:
        print(f"ОШИБКА: невалидный {CHECKSUMS_NAME}: {e}")
        return 1
    actual = compute(root)
    changed = [f for f in actual if f in recorded and actual[f] != recorded[f]]
    added = [f for f in actual if f not in recorded]
    removed = [f for f in recorded if f not in actual]
    if changed or added or removed:
        print(f"ОБНАРУЖЕНА ПРЯМАЯ ПРАВКА MANAGED-СЛОЯ (drift) в {root}:")
        for f in changed:
            print(f"  - изменён: {f}")
        for f in added:
            print(f"  - добавлен (не managed): {f}")
        for f in removed:
            print(f"  - удалён: {f}")
        print("Обновление parent->child должно остановиться; правку перенести в custom/-overlay.")
        return 1
    print(f"OK: managed-слой целостен ({len(actual)} файлов сверено с {CHECKSUMS_NAME}).")
    return 0


def main(argv):
    mode = argv[1] if len(argv) > 1 else "verify"
    root = Path(argv[2]).resolve() if len(argv) > 2 else DEFAULT_ROOT
    if not root.exists():
        print(f"managed-root не найден: {root} — пропуск.")
        return 0
    if mode == "generate":
        return generate(root)
    if mode == "verify":
        return verify(root)
    print(f"неизвестный режим '{mode}' (ожидалось generate|verify)")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
