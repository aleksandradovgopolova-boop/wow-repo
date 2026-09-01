#!/usr/bin/env python3
"""changelog_gen.py (v3.28.0 R20) — автоматизация CHANGELOG.

Две функции:
1. generate() — генерирует черновик записи CHANGELOG из git commits с последнего тега.
   Категоризирует коммиты по префиксам (release:, fix:, feat:, refactor:, docs:, test:, ci:).
   Формат — в стиле существующего CHANGELOG кита.

2. validate() — проверяет, что CHANGELOG.md содержит запись для текущей VERSION.
   Это catch для случая, когда разработчик забыл обновить CHANGELOG перед релизом.

Честность:
  - generate() не перезаписывает CHANGELOG — только выводит черновик (человек решает)
  - validate() детерминированно: VERSION из файла vs записи в CHANGELOG
  - Нет внешних зависимостей (git CLI + stdlib)

CLI:
  changelog_gen.py generate [--from-ref TAG] [--to-ref HEAD]   # черновик записи
  changelog_gen.py validate                                     # проверка VERSION в CHANGELOG
  changelog_gen.py --selftest
"""
from __future__ import annotations

import re
import subprocess
import sys
from datetime import date
from pathlib import Path

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
CHANGELOG_PATH = PKG / "CHANGELOG.md"
VERSION_PATH = PKG / "VERSION"

# Категории коммитов по префиксу
COMMIT_CATEGORIES = {
    "release": "Релиз",
    "fix": "Исправления",
    "feat": "Добавлено",
    "refactor": "Рефакторинг",
    "docs": "Документация",
    "test": "Тесты",
    "ci": "CI/CD",
    "perf": "Производительность",
    "chore": "Обслуживание",
}

# Паттерн для извлечения категории из commit message
COMMIT_RE = re.compile(r"^(release|fix|feat|refactor|docs|test|ci|perf|chore)\s*(?:\(([^)]+)\))?\s*:\s*(.*)", re.I)


def _git(*args) -> str:
    """Выполнить git-команду, вернуть stdout."""
    result = subprocess.run(
        ["git"] + list(args),
        capture_output=True, text=True, cwd=str(PKG), timeout=30
    )
    return result.stdout.strip()


def _last_tag() -> str:
    """Последний тег (по дате)."""
    return _git("describe", "--tags", "--abbrev=0")


def _commits_since(from_ref: str, to_ref: str = "HEAD") -> list[dict]:
    """Коммиты между from_ref и to_ref."""
    log = _git("log", f"{from_ref}..{to_ref}", "--format=%H|%s|%an|%aI", "--no-merges")
    if not log:
        return []
    commits = []
    for line in log.splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            sha, subject, author, ts = parts
            commits.append({
                "sha": sha[:8],
                "subject": subject,
                "author": author,
                "timestamp": ts,
            })
    return commits


def _categorize(subject: str) -> tuple[str, str]:
    """Извлечь категорию и описание из subject. -> (category_key, description)."""
    m = COMMIT_RE.match(subject)
    if m:
        cat = m.group(1).lower()
        desc = m.group(3).strip()
        return cat, desc
    return "other", subject


def generate(from_ref: str = None, to_ref: str = "HEAD") -> str:
    """Сгенерировать черновик записи CHANGELOG из git commits.

    -> строка в формате CHANGELOG (готовая к вставке после ## [Unreleased]).
    """
    if from_ref is None:
        from_ref = _last_tag()
    version = VERSION_PATH.read_text(encoding="utf-8").strip()
    today = date.today().isoformat()

    commits = _commits_since(from_ref, to_ref)
    if not commits:
        return f"## [{version}] — {today}\n\nНет коммитов с {from_ref}.\n"

    # Группировка по категориям
    groups: dict[str, list[dict]] = {}
    for c in commits:
        cat, desc = _categorize(c["subject"])
        groups.setdefault(cat, []).append({**c, "description": desc})

    # Формирование записи
    lines = [f"## [{version}] — {today}"]
    lines.append("")

    # Заголовок — из первого release-коммита или авто
    release_commits = groups.get("release", [])
    if release_commits:
        lines.append(f"{release_commits[0]['description']}")
    else:
        n = len(commits)
        lines.append(f"Автоматическая генерация ({n} коммитов с {from_ref}).")
    lines.append("")

    # Секции по категориям (в порядке важности)
    category_order = ["feat", "fix", "refactor", "perf", "test", "ci", "docs", "chore", "other"]
    for cat in category_order:
        items = groups.get(cat, [])
        if not items:
            continue
        title = COMMIT_CATEGORIES.get(cat, cat.capitalize())
        lines.append(f"{title}:")
        for item in items:
            lines.append(f"- `{item['sha']}` {item['description']}")
        lines.append("")

    return "\n".join(lines)


def validate() -> dict:
    """Проверить, что CHANGELOG содержит запись для текущей VERSION.

    -> {ok: bool, version: str, found: bool, details: str}
    """
    version = VERSION_PATH.read_text(encoding="utf-8").strip()
    changelog = CHANGELOG_PATH.read_text(encoding="utf-8")

    # Ищем запись [VERSION] в CHANGELOG
    pattern = rf"^\## \[{re.escape(version)}\]"
    found = bool(re.search(pattern, changelog, re.MULTILINE))

    if found:
        return {"ok": True, "version": version, "found": True,
                "details": f"CHANGELOG содержит запись для v{version}"}
    else:
        return {"ok": False, "version": version, "found": False,
                "details": f"CHANGELOG НЕ содержит запись для v{version} — добавьте перед коммитом"}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="AI Ops Kit CHANGELOG automation")
    sub = parser.add_subparsers(dest="command")

    gen = sub.add_parser("generate", help="Generate changelog draft from git commits")
    gen.add_argument("--from-ref", help="Start ref (tag/branch/SHA). Default: last tag")
    gen.add_argument("--to-ref", default="HEAD", help="End ref. Default: HEAD")

    sub.add_parser("validate", help="Check CHANGELOG has entry for current VERSION")

    parser.add_argument("--selftest", action="store_true", help="Run selftest")
    args = parser.parse_args()

    if args.command == "generate":
        print(generate(from_ref=args.from_ref, to_ref=args.to_ref))
    elif args.command == "validate":
        result = validate()
        print(result["details"])
        return 0 if result["ok"] else 1
    else:
        parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
