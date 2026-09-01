#!/usr/bin/env python3
"""storybook_query.py (v3.6.6) — минимальный READ-ONLY Storybook-адаптер (не новый центр системы).

Ревью владельца: Storybook MCP — минимальный адаптер, а не центр. Первая версия преимущественно
read-only. Здесь — детерминированный read-only слой запросов поверх story-index (тот же индекс, что
парсит storybook_adapter, v3.1.7); БЕЗ внешнего MCP-сервера/SaaS (полноценный MCP — не сейчас).

Возможности: список компонентов, список stories, stories компонента, related-stories для изменённых
файлов, метаданные story (title/name/importPath/tags). UI-evidence и exact-SHA — уже в
storybook_adapter; здесь read-only навигация по каталогу для контекста агентов.

CLI: storybook_query.py <child_root> [--related a.tsx,b.tsx] [--json] | --selftest
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402
from ai_ops_kit.ui import storybook_adapter as sa   # noqa: E402


def load_stories(child_root):
    idx = sa._find(Path(child_root), sa._STORY_INDEX)
    return sa._parse_story_index(sa._load_json(idx)) if idx else []


def list_components(stories):
    return sorted({sa._component_of(s) for s in stories if sa._component_of(s)})


def list_stories(stories):
    return sorted(s["id"] for s in stories if s.get("id"))


def component_stories(stories, component):
    return sorted(s["id"] for s in stories if sa._component_of(s) == component)


def related_stories(stories, changed_files):
    changed = [c.strip() for c in (changed_files or []) if c and c.strip()]
    return sorted(s["id"] for s in stories
                  if s.get("id") and sa._matches_changed(s.get("importPath", ""), changed))


def story_meta(stories, story_id):
    for s in stories:
        if s.get("id") == story_id:
            return {"id": s.get("id"), "title": s.get("title"), "name": s.get("name"),
                    "importPath": s.get("importPath")}
    return None


def catalog(child_root):
    stories = load_stories(child_root)
    return {"kind": "storybook-catalog", "read_only": True, "story_count": len(stories),
            "components": list_components(stories), "stories": list_stories(stories)}


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    stories = load_stories(args[0])
    if "--related" in argv:
        changed = argv[argv.index("--related") + 1].split(",")
        out = {"related_stories": related_stories(stories, changed)}
    else:
        out = catalog(args[0])
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
