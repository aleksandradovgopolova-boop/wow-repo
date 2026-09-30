#!/usr/bin/env python3
"""storybook_query.py (v3.6.6) — минимальный READ-ONLY Storybook-адаптер (не новый центр системы).

Ревью владельца: Storybook MCP — минимальный адаптер, а не центр. Первая версия преимущественно
read-only. Здесь — детерминированный read-only слой запросов поверх story-index (тот же индекс, что
парсит storybook_adapter, v3.1.7); БЕЗ внешнего MCP-сервера/SaaS (полноценный MCP — не сейчас).

Возможности: список компонентов, список stories, stories компонента, related-stories для изменённых
файлов, метаданные story (title/name/importPath/tags). UI-evidence и exact-SHA — уже в
storybook_adapter; здесь read-only навигация по каталогу для контекста агентов.

MCP-ДОСТУП. UI-система объявлена MCP-доступным инструментом `storybook-query` в registry/tools.yaml
(protocol: mcp, permission_level: read-only) и в registry/capability-index.yaml (tool/mcp). Доступ =
этот минимальный read-only адаптер через MCP-декларацию: MCP-runtime оборачивает детерминированный
вход `--json` как tool. Это НЕ отдельный MCP-сервер/SaaS — полноценный MCP-сервер сознательно отложен
(ревью владельца: «Storybook MCP — минимальный адаптер, а не центр; полноценный сервер — не сейчас»).
Инструмент строго read-only: не даёт записи (fail-closed по устройству — здесь нет мутирующих путей).

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


# Суффиксы «файла компонента/истории», которые снимаем, чтобы получить стем компонента.
_COMPONENT_SUFFIXES = (".stories.tsx", ".stories.ts", ".stories.jsx", ".stories.js",
                       ".tsx", ".ts", ".jsx", ".js")


def _component_stem(path: str) -> str:
    """Стем компонента из пути файла: базовое имя без расширения и без `.stories`, в нижнем регистре.
    `src/shared/ui/ds/Button.tsx` -> `button`; `.../Button.stories.tsx` -> `button`."""
    base = sa._norm_path(path).split("/")[-1]
    for suf in _COMPONENT_SUFFIXES:
        if base.endswith(suf):
            return base[: -len(suf)].strip().lower()
    return base.strip().lower()


def _changed_stems(changed):
    return {st for c in changed if (st := _component_stem(c))}


def related_stories(stories, changed_files):
    """Stories, относящиеся к изменённым файлам — для НАВИГАЦИИ агента (не evidence гейта).

    Живой прогон на ии-среде (07.09.2026) показал: строгий суффиксный матч `storybook_adapter`
    почти всегда даёт пусто на реальной дизайн-системе — компоненты (`ds/Button.tsx`) и их stories
    (сгруппированные в `ds/foundations.stories.tsx`) лежат в РАЗНЫХ файлах, поэтому суффикс пути не
    совпадает. Здесь — более широкое связывание, уместное для КОНТЕКСТА (advisory, не гейт):
      (а) строгий суффиксный матч пути (как в evidence-пути) — не трогаем его строгость там;
      (б) имя компонента в title story == стем изменённого файла (`Button.tsx` -> «Компоненты/Button»,
          включая сгруппированные stories);
      (в) стем story-файла == стем изменённого файла (классический `Button.tsx` <-> `Button.stories.tsx`).
    Строгий матчер evidence-пути (`storybook_adapter._matches_changed`) НЕ меняется: там широта дала бы
    ложное «покрытие» гейта; здесь широта лишь показывает агенту, что переиспользовать."""
    changed = [c.strip() for c in (changed_files or []) if c and c.strip()]
    if not changed:
        return []
    stems = _changed_stems(changed)
    out = set()
    for s in stories:
        sid = s.get("id")
        if not sid:
            continue
        ip = s.get("importPath", "")
        if sa._matches_changed(ip, changed):                       # (а)
            out.add(sid)
        elif sa._component_base(sa._component_of(s)) in stems:      # (б)
            out.add(sid)
        elif _component_stem(ip) in stems:                         # (в)
            out.add(sid)
    return sorted(out)


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


def has_index(child_root) -> bool:
    """У дочки есть собранный story-index (Storybook доступен для навигации)?"""
    return sa._find(Path(child_root), sa._STORY_INDEX) is not None


def navigation_context(child_root, changed_files=None, max_components=60):
    """Текст навигации по дизайн-системе для КОНТЕКСТА пишущего агента (не evidence гейта).

    Задача — чтобы агент, трогая UI, переиспользовал существующие компоненты/паттерны Storybook, а
    не изобретал заново (дисциплина дизайн-системы + a11y). Возвращает None, если Storybook у дочки
    нет (не шумим на не-UI дочке). Только чтение.

    Если известны изменённые файлы — добавляет СВЯЗАННЫЕ с ними stories (широкое связывание, см.
    related_stories); иначе даёт каталог компонентов дизайн-системы."""
    if not has_index(child_root):
        return None
    stories = load_stories(child_root)
    if not stories:
        return None
    components = list_components(stories)
    lines = [f"=== Storybook дочки: {len(components)} компонентов дизайн-системы, {len(stories)} stories ===",
             "Переиспользуй существующие компоненты/варианты/паттерны (в т.ч. состояния и a11y) — "
             "не изобретай заново то, что уже есть в дизайн-системе."]
    related = related_stories(stories, changed_files) if changed_files else []
    if related:
        lines.append("Stories, связанные с затронутыми файлами (посмотри их API и варианты):")
        lines.extend(f"  - {sid}" for sid in related[:40])
    shown = components[:max_components]
    lines.append("Компоненты дизайн-системы: " + ", ".join(shown)
                 + ("" if len(components) <= max_components else f" … (+{len(components) - max_components})"))
    return "\n".join(lines)


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
