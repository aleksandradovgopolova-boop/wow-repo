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

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / "tools"))
import storybook_adapter as sa   # noqa: E402


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


def selftest():
    import tempfile
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "storybook-static").mkdir()
        (root / "storybook-static" / "index.json").write_text(json.dumps({"v": 5, "entries": {
            "components-metriccard--default": {"type": "story", "id": "components-metriccard--default",
                "title": "Components/MetricCard", "name": "Default", "importPath": "./src/MetricCard.tsx"},
            "components-metriccard--loading": {"type": "story", "id": "components-metriccard--loading",
                "title": "Components/MetricCard", "name": "Loading", "importPath": "./src/MetricCard.tsx"},
            "components-button--default": {"type": "story", "id": "components-button--default",
                "title": "Components/Button", "name": "Default", "importPath": "./src/Button.tsx"},
            "docs-intro": {"type": "docs", "id": "docs-intro", "title": "Intro"}}}), encoding="utf-8")
        st = load_stories(root)
        expect("story-index распарсен, docs-entry не история (3 story)", len(st) == 3)
        expect("list_components -> MetricCard, Button",
               list_components(st) == ["Components/Button", "Components/MetricCard"])
        expect("component_stories(MetricCard) -> 2 story",
               len(component_stories(st, "Components/MetricCard")) == 2)
        expect("related_stories по изменённому MetricCard.tsx -> его stories, без Button",
               set(related_stories(st, ["src/MetricCard.tsx"]))
               == {"components-metriccard--default", "components-metriccard--loading"})
        expect("related_stories по чужому файлу -> пусто",
               related_stories(st, ["src/Unknown.tsx"]) == [])
        expect("story_meta возвращает importPath",
               story_meta(st, "components-button--default")["importPath"] == "./src/Button.tsx")
        c = catalog(root)
        expect("catalog read_only=True + story_count=3", c["read_only"] is True and c["story_count"] == 3)

    # нет Storybook -> пустой каталог, без падения
    with tempfile.TemporaryDirectory() as td2:
        expect("нет Storybook -> пустой каталог (без ошибки)", catalog(td2)["story_count"] == 0)

    print("storybook_query selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
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
