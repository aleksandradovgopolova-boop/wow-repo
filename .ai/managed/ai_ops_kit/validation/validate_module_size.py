#!/usr/bin/env python3
"""Ратчет размера МОДУЛЯ (файла) в ai_ops_kit/ — заморозить монолиты вниз.

Считает СТРОКИ каждого `ai_ops_kit/**/*.py` И `installer/**/*.py` (F-06). Порог — 700 строк: файл на пороге или выше обязан
быть перечислен в baseline (`packages/module-size-baseline.yaml`, секция `ceilings:`) с потолком,
равным его текущему размеру. Правило семантики — ПОТОЛОК ПО «≤»:

  * файл ≥ порога, которого НЕТ в baseline  -> FAIL: новый монолит обязан появиться ОСОЗНАННО
    (через запись в ленте `raises:`), а не просочиться незамеченным;
  * файл ≥ порога, чьи строки > его потолка -> FAIL: монолит вырос сверх замороженного размера;
  * файл ≥ порога, чьи строки ≤ потолка     -> OK (в том числе строго меньше: усыхать МОЖНО
    свободно — потолок держит рост, не заставляет держать размер);
  * файл НИЖЕ порога — не ограничен вовсе (в baseline его нет, и это не ошибка).

Ратчет дополняет func-size (`validate_func_size.py`): тот стережёт максимум ФУНКЦИИ, этот — размер
ФАЙЛА. Мы дорого ужимали крупнейшие модули (ai_ops_run.py, execution_pipeline.py, presenter.py);
эта заморозка не даёт им отрасти обратно. Потолок = текущий размер: он не требует резать существующее
прямо сейчас, а останавливает НОВЫЙ рост монолитов и появление новых.

Снижение потолка (усохший файл) НЕ краснит прогон — в отличие от func-size. Baseline при этом всё
же держат в согласии с фактом: тест `tests/unit/test_module_size.py` сверяет, что каждый файл
сверх порога записан с потолком == текущему размеру, и просит пере-снять baseline, если разошлось.

Использование:
  validate_module_size.py              # проверить все файлы сверх порога против baseline
  validate_module_size.py --report     # напечатать все файлы сверх порога без проверки
  validate_module_size.py --baseline   # пере-снять baseline текущим замером (лента raises цела)

Возврат 0 — ни один монолит не превысил потолок и все они записаны, 1 — хотя бы один пробой.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
           Path(__file__).resolve().parents[1])
# ДВА корня, а не один (F-06 внешнего аудита): installer/ai_ops.py — крупнейший файл репозитория и
# единственный код, который пользователь запускает ДО появления кита, а SOURCE_ROOT="ai_ops_kit"
# оставлял его вне гейта. Ключи baseline остаются относительными к pkg_root (installer/ai_ops.py).
SOURCE_ROOTS = ("ai_ops_kit", "installer")
BASELINE_FILE = PKG / "packages" / "module-size-baseline.yaml"

# Порог: файл на пороге или выше обязан быть заморожен в baseline. Ниже порога — не ограничен.
THRESHOLD = 700


def measure_modules(pkg_root: Path = PKG) -> list[dict]:
    """Все `ai_ops_kit/**/*.py` с числом строк. -> список {'path': относит. posix, 'lines': int}.

    Путь — относительно pkg_root (например `ai_ops_kit/cli/ai_ops_cli.py`), чтобы baseline не
    зависел от места дерева. Строки считаются `splitlines()` (не зависит от финального перевода
    строки). Нечитаемый файл пропускается, обход не роняет.
    """
    results = []
    for source_root in SOURCE_ROOTS:
        root = pkg_root / source_root
        for f in sorted(root.rglob("*.py")):
            try:
                lines = len(f.read_text(encoding="utf-8").splitlines())
            except OSError:
                continue
            results.append({"path": f.relative_to(pkg_root).as_posix(), "lines": lines})
    return results


def over_threshold(modules: list[dict], threshold: int = THRESHOLD) -> list[dict]:
    """Файлы на пороге или выше, по убыванию размера."""
    big = [m for m in modules if m["lines"] >= threshold]
    return sorted(big, key=lambda m: m["lines"], reverse=True)


def load_baseline(path: Path = BASELINE_FILE) -> dict:
    """Загрузить baseline из YAML. -> dict (пустой, если файла нет)."""
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def ceilings_of(baseline: dict) -> dict:
    """Карта потолков {путь: строк} из baseline. -> dict (пустой, если секции нет)."""
    ceilings = baseline.get("ceilings")
    return dict(ceilings) if isinstance(ceilings, dict) else {}


def check(modules: list[dict], baseline: dict, threshold: int = THRESHOLD) -> list[str]:
    """Сверить файлы сверх порога с их потолками. -> список ошибок (пустой = ОК).

    Отсутствие секции `ceilings` — сама ошибка ТОЛЬКО когда есть что стеречь: если ни один файл не
    достиг порога, стеречь нечего и пустой baseline не криминал. Файл сверх порога без записи —
    красный сигнал: новый монолит обязан появиться осознанно.
    """
    ceilings = ceilings_of(baseline)
    big = over_threshold(modules, threshold)
    if big and "ceilings" not in baseline:
        return ["ратчет module-size: в baseline нет секции ceilings — монолиты не заморожены"]
    errors = []
    for m in big:
        path, lines = m["path"], m["lines"]
        ceiling = ceilings.get(path)
        if ceiling is None:
            errors.append(
                f"ратчет module-size: {path} = {lines} строк (≥ порога {threshold}) НЕ в baseline "
                f"— новый монолит; внести осознанно записью в ленте raises "
                f"packages/module-size-baseline.yaml")
        elif not isinstance(ceiling, int) or isinstance(ceiling, bool):
            errors.append(
                f"ратчет module-size: потолок {path} не число ({ceiling!r}) — потолка не существует")
        elif lines > ceiling:
            errors.append(
                f"ратчет module-size: {path} = {lines} строк превышает потолок {ceiling} — монолит "
                f"отрос; разбить файл либо осознанно поднять потолок записью в raises")
    return errors


def render_report(modules: list[dict], threshold: int = THRESHOLD) -> str:
    """Человекочитаемый отчёт: все файлы сверх порога, по убыванию размера."""
    big = over_threshold(modules, threshold)
    lines = [f"Файлов сверх порога {threshold}: {len(big)} (из {len(modules)} всего)"]
    for m in big:
        lines.append(f"  {m['lines']:5d}  {m['path']}")
    return "\n".join(lines)


def write_baseline(modules: list[dict], path: Path = BASELINE_FILE,
                   threshold: int = THRESHOLD) -> None:
    """Пере-снять baseline текущим замером. Лента `raises` сохраняется, если была."""
    prev = load_baseline(path)
    ceilings = {m["path"]: m["lines"] for m in over_threshold(modules, threshold)}
    data = {
        "schema_version": 1,
        "kind": "module-size-ratchet",
        "threshold": threshold,
        "ceilings": ceilings,
        "raises": prev.get("raises", []) or [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or sys.argv[1:])
    modules = measure_modules()

    if "--report" in argv:
        print(render_report(modules))
        return 0

    if "--baseline" in argv:
        write_baseline(modules)
        big = over_threshold(modules)
        print(f"Baseline обновлён: {len(big)} файл(ов) сверх порога {THRESHOLD}.")
        return 0

    baseline = load_baseline()
    errors = check(modules, baseline)
    for e in errors:
        print(f"  [FAIL] {e}")
    if errors:
        print(f"MODULE-SIZE-FAIL: {len(errors)} нарушение(ий)")
        return 1
    big = over_threshold(modules)
    print(f"MODULE-SIZE-OK: {len(big)} монолит(ов) сверх порога {THRESHOLD}, "
          f"все в пределах замороженного размера:")
    for m in big:
        ceiling = ceilings_of(baseline).get(m["path"])
        print(f"  {m['lines']:5d}/{ceiling}  {m['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
