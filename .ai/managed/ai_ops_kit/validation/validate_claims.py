#!/usr/bin/env python3
"""Проверка claims — контрактов «документ утверждает о коде» (v2.9).

Идея из team-os-toolkit (init-team-brain, claims.yaml + drift checker):
документация делает утверждения о коде (существует символ, набор enum-значений,
файл). Со временем код меняется, документ — нет; расхождение остаётся невидимым.
Claims делают утверждение машинно-проверяемым: если код разошёлся с документом —
проверка падает и рассинхрон становится виден (в CI/commit).

Поддерживаемые типы (детерминированные, без исполнения кода):
  - file-exists    : source.path существует;
  - symbol-exists  : source.path содержит source.symbol (подстрока/regex);
  - enum-values    : каждое значение из source.values встречается в source.path;
  - count          : число строк source.path, матчащих source.pattern (regex, MULTILINE),
                     равно source.expected (пиннинг счётчиков против прозы-дрифта).

Формат claims.yaml:
  schema_version: 1
  kind: claims
  claims:
    - id: material-status-values
      doc: knowledge/object-model.md      # кто делает утверждение (информационно)
      type: enum-values
      source: {path: ../app/src/materials/types.ts, symbol: MaterialStatus,
               values: [DRAFT, ORDERED, DELIVERED]}

Пути source.path — относительно каталога claims.yaml (child может ссылаться на
соседний репозиторий кода через ../). Утверждение о том, чего нельзя проверить
(нет source.path), помечается как ERROR, а не молча пропускается.

Использование:  validate_claims.py [claims.yaml] [--json]   (default: knowledge/claims.yaml)
                validate_claims.py --selftest
Возврат 0 — все claims выполняются (или файла нет — нечего проверять), 1 — есть drift.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
def check_claim(base: Path, c: dict):
    """Вернуть (status, detail): status in {ok, drift, error}."""
    ctype = c.get("type")
    src = c.get("source") or {}
    rel = src.get("path")
    if not rel:
        return "error", "нет source.path — утверждение непроверяемо"
    p = (base / rel)
    if ctype == "file-exists":
        return ("ok", "") if p.exists() else ("drift", f"файл отсутствует: {rel}")
    if not p.exists():
        return "drift", f"источник отсутствует: {rel}"
    text = p.read_text(encoding="utf-8", errors="replace")
    if ctype == "symbol-exists":
        sym = src.get("symbol", "")
        try:
            found = bool(re.search(re.escape(sym), text)) if sym else False
        except re.error:
            found = sym in text
        return ("ok", "") if found else ("drift", f"символ не найден: {sym}")
    if ctype == "enum-values":
        missing = [v for v in (src.get("values") or []) if v not in text]
        return ("ok", "") if not missing else ("drift", f"значения отсутствуют в коде: {missing}")
    if ctype == "count":
        pat = src.get("pattern")
        expected = src.get("expected")
        if pat is None or expected is None:
            return "error", "count требует source.pattern и source.expected"
        try:
            actual = len(re.findall(pat, text, re.M))
        except re.error as e:
            return "error", f"некорректный pattern: {e}"
        return ("ok", "") if actual == expected else (
            "drift", f"счётчик {pat!r}: фактически {actual}, ожидалось {expected}")
    return "error", f"неизвестный тип claim: {ctype}"


def build(claims_file: Path):
    data = yaml.safe_load(claims_file.read_text(encoding="utf-8")) or {}
    base = claims_file.parent
    results = []
    for c in (data.get("claims") or []):
        status, detail = check_claim(base, c)
        results.append({"id": c.get("id"), "type": c.get("type"),
                        "status": status, "detail": detail})
    return results


def run(claims_file: Path, as_json=False):
    if not claims_file.exists():
        print(f"claims не найдены: {claims_file} — нечего проверять (это не ошибка).")
        return 0
    results = build(claims_file)
    bad = [r for r in results if r["status"] != "ok"]
    if as_json:
        print(json.dumps({"schema_version": 1, "kind": "claims-report",
                          "file": str(claims_file), "results": results},
                         ensure_ascii=False, indent=2))
    else:
        print(f"=== claims: {claims_file} ({len(results)} утверждений) ===")
        for r in results:
            mark = {"ok": "OK", "drift": "DRIFT", "error": "ERROR"}[r["status"]]
            print(f"  [{mark}] {r['id']} ({r['type']})" + (f" — {r['detail']}" if r["detail"] else ""))
        print("CLAIMS-OK: документация согласована с кодом." if not bad
              else f"CLAIMS: {len(bad)} расхождений — документация разошлась с кодом.")
    return 1 if bad else 0


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    claims_file = Path(args[0]).resolve() if args else (PKG / "knowledge" / "claims.yaml")
    return run(claims_file, as_json="--json" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
