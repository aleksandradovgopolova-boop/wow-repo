#!/usr/bin/env python3
"""Проверка свежести знаний (v2.9) — по классам устаревания из FreshnessPolicy.

Сканирует markdown-документы с frontmatter `stability` и помечает протухшие:
reviewed_at + срок < сегодня. Срок берётся из документа (expires_after_days) или из
класса по умолчанию (volatile=14, evolving=90, stable=не истекает). Документы без
`stability` не проверяются (freshness — opt-in на документ).

Advisory: возврат 0 всегда (протухший документ — сигнал, не блок), 1 — ошибка чтения
или (при --strict) наличие протухших. Дата «сегодня» задаётся --now YYYY-MM-DD
(для детерминированности в CI/селфтесте); без него берётся системная дата.

Использование:  validate_freshness.py [dir] [--now YYYY-MM-DD] [--strict] [--json]
                validate_freshness.py --selftest

v3.12.0 Startup Context Budget: БЕЗ аргумента дефолт — контекст РЕПОЗИТОРИЯ
(.ai/project/context, при отсутствии .ai/custom/context), а НЕ шаблоны кита. Шаблоны кита
(template: true во frontmatter) исключены из проверки (протухает копия в репо, не шаблон).
"""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
DEFAULT_EXPIRY = {"stable": None, "evolving": 90, "volatile": 14}


def frontmatter(md: str):
    if md.startswith("---"):
        parts = md.split("---", 2)
        if len(parts) >= 3:
            try:
                return yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                return {}
    return {}


def parse_date(s):
    try:
        return datetime.strptime(str(s), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def assess(fm: dict, today: date):
    """Вернуть (status, detail) для документа с frontmatter."""
    # v3.12.0 Startup Context Budget: шаблоны кита (template: true) НЕ проверяются на свежесть —
    # у них зашита дата-заглушка, они не «протухают» (протухает копия в репозитории, а не шаблон).
    if fm.get("template") is True:
        return None
    stab = fm.get("stability")
    if stab not in DEFAULT_EXPIRY:
        return None
    reviewed = parse_date(fm.get("reviewed_at"))
    if reviewed is None:
        return "no-review-date", f"stability={stab}, но нет валидного reviewed_at"
    days = fm.get("expires_after_days", DEFAULT_EXPIRY[stab])
    if days is None:
        return "ok", f"{stab} (не истекает)"
    expires = reviewed + timedelta(days=int(days))
    if today > expires:
        overdue = (today - expires).days
        return "stale", f"{stab}: протух {overdue}д назад (проверен {reviewed}, срок {days}д)"
    return "ok", f"{stab}: свеж до {expires}"


def _default_child_context(base: Path = None):
    """Дефолт БЕЗ аргумента — контекст РЕПОЗИТОРИЯ (не шаблоны кита): .ai/project/context, при
    отсутствии — .ai/custom/context. Если ни того ни другого нет (напр. чистый клон кита) — вернуть
    project/context как есть (rglob по несуществующему -> пусто -> честно «нет документов»)."""
    b = base or Path.cwd()
    for rel in (".ai/project/context", ".ai/custom/context"):
        p = b / rel
        if p.is_dir():
            return p.resolve()
    return (b / ".ai/project/context").resolve()


def build(root: Path, today: date):
    results = []
    for p in sorted(root.rglob("*.md")):
        res = assess(frontmatter(p.read_text(encoding="utf-8", errors="replace")), today)
        if res:
            results.append({"path": p.relative_to(root).as_posix(), "status": res[0], "detail": res[1]})
    return results


def run(root: Path, today: date, strict=False, as_json=False):
    results = build(root, today)
    bad = [r for r in results if r["status"] != "ok"]
    if as_json:
        print(json.dumps({"schema_version": 1, "kind": "freshness-report",
                          "today": str(today), "results": results}, ensure_ascii=False, indent=2))
    else:
        print(f"=== свежесть знаний ({root}, сегодня {today}) ===")
        for r in results:
            mark = {"ok": "OK", "stale": "STALE", "no-review-date": "WARN"}[r["status"]]
            print(f"  [{mark}] {r['path']} — {r['detail']}")
        if not results:
            print("  нет документов с frontmatter stability (freshness — opt-in).")
        elif bad:
            print(f"ВНИМАНИЕ: {len(bad)} протухших/без даты — не считать актуальным источником.")
        else:
            print("FRESHNESS-OK: все размеченные документы свежи.")
    return 1 if (strict and bad) else 0


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    today = date(2026, 7, 13)
    expect("stable не истекает",
           assess({"stability": "stable", "reviewed_at": "2020-01-01"}, today)[0] == "ok")
    expect("volatile 35д назад — протух",
           assess({"stability": "volatile", "reviewed_at": "2026-06-08"}, today)[0] == "stale")
    expect("volatile 3д назад — свеж",
           assess({"stability": "volatile", "reviewed_at": "2026-07-10"}, today)[0] == "ok")
    expect("evolving 100д назад — протух",
           assess({"stability": "evolving", "reviewed_at": "2026-04-04"}, today)[0] == "stale")
    expect("нет reviewed_at — WARN",
           assess({"stability": "volatile"}, today)[0] == "no-review-date")
    expect("без stability — не проверяется",
           assess({"title": "x"}, today) is None)
    expect("кастомный expires_after_days уважается",
           assess({"stability": "evolving", "reviewed_at": "2026-07-01",
                   "expires_after_days": 5}, today)[0] == "stale")
    # v3.12.0: шаблон кита (template: true) не проверяется, даже с протухшей датой
    expect("template: true — не проверяется (протух шаблон != протух репо)",
           assess({"template": True, "stability": "volatile", "reviewed_at": "2020-01-01"}, today) is None)
    # v3.12.0: дефолтный путь — контекст РЕПОЗИТОРИЯ, не шаблоны кита
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        (base / ".ai/project/context").mkdir(parents=True)
        expect("дефолт -> .ai/project/context репозитория",
               _default_child_context(base) == (base / ".ai/project/context").resolve())
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        (base / ".ai/custom/context").mkdir(parents=True)
        expect("дефолт -> .ai/custom/context при отсутствии project",
               _default_child_context(base) == (base / ".ai/custom/context").resolve())
    print("validate_freshness selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    now_arg = None
    args = []
    it = iter(argv)
    for a in it:
        if a == "--now":
            now_arg = next(it, None)
        elif not a.startswith("--"):
            args.append(a)
    today = parse_date(now_arg) if now_arg else date.today()
    if today is None:
        print("неверный --now (ожидается YYYY-MM-DD)"); return 1
    root = Path(args[0]).resolve() if args else _default_child_context()
    return run(root, today, strict="--strict" in argv, as_json="--json" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
