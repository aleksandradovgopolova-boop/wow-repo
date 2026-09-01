#!/usr/bin/env python3
"""context_cost.py (v3.13.0 Startup Context Budget, срез 2) — ОЦЕНКА стоимости стартового набора
контекста сессии в токенах, с поправкой на кириллицу, разбивкой по файлам и вердиктом против бюджета.

Стартовый набор (то, что сессия платит на КАЖДОМ старте) = три корзины:
  1. Ярус 1 контекста — документы с `read_tier: 1` (читаются всегда): ProductStatus.md, now.md;
  2. CLAUDE.md — вечные правила репозитория;
  3. Описания экспортируемой поверхности — `description` скиллов (.claude/skills/*/SKILL.md) +
     краткие описания команд (.claude/commands/*.md). Это листинг, который рантайм держит в контексте.

Оценка ПРИБЛИЖЁННАЯ (по символам): латиница ~4 символа/токен, кириллица ~2 (RU дороже), прочее ~3.
Не претендует на точность токенайзера — цель сделать стоимость старта НАБЛЮДАЕМОЙ величиной и ловить
её рост до того, как о нём спросит человек. Бюджет — `context_budget.session_start_tokens` из
`.ai-ops.yaml` (по умолчанию 5000).

Advisory: rc 0 (превышение — сигнал, не блок); с `--strict` rc 1 при превышении бюджета.

Использование:  context_cost.py <child_root> [--json] [--budget N] [--strict]
                context_cost.py --selftest
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
DEFAULT_BUDGET = 5000


def estimate_tokens(text: str) -> int:
    """Грубая оценка токенов по символам с поправкой на язык (кириллица дороже латиницы)."""
    if not text:
        return 0
    cyr = sum(1 for c in text if "Ѐ" <= c <= "ӿ")
    asc = sum(1 for c in text if ord(c) < 128)
    other = len(text) - cyr - asc
    return int(asc / 4 + cyr / 2 + other / 3)


def _frontmatter(md: str):
    if md.startswith("---"):
        parts = md.split("---", 2)
        if len(parts) >= 3:
            try:
                return yaml.safe_load(parts[1]) or {}, parts[2]
            except yaml.YAMLError:
                return {}, parts[2]
    return {}, md


def _context_root(root: Path):
    """Где живёт контекст репозитория (или шаблоны, если оверлей пуст) — для оценки яруса 1."""
    for rel in (".ai/project/context", ".ai/custom/context", ".ai/managed/context", "context"):
        p = root / rel
        if p.is_dir():
            return p
    return None


def _skills_root(root: Path):
    for rel in (".claude/skills", "skills"):
        p = root / rel
        if p.is_dir():
            return p
    return None


def _commands_root(root: Path):
    for rel in (".claude/commands", "commands/task"):
        p = root / rel
        if p.is_dir():
            return p
    return None


def _command_desc(text: str) -> str:
    """Краткое описание команды для листинга: первый содержательный абзац (после ## Назначение)."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().lower().startswith("## назначение"):
            for nxt in lines[i + 1:]:
                if nxt.strip():
                    return nxt.strip()
    for ln in lines:
        s = ln.strip()
        if s and not s.startswith("#"):
            return s
    return lines[0].strip() if lines else ""


def estimate(child_root, budget=None):
    """Оценка стартового набора. -> dict с разбивкой по файлам, суммами по корзинам и вердиктом."""
    root = Path(child_root)
    budget = budget if budget is not None else _budget_from_config(root)
    items = []

    # 1) ярус 1 контекста (read_tier == 1)
    croot = _context_root(root)
    if croot:
        for p in sorted(croot.rglob("*.md")):
            fm, _ = _frontmatter(p.read_text(encoding="utf-8", errors="replace"))
            if fm.get("read_tier") == 1:
                txt = p.read_text(encoding="utf-8", errors="replace")
                items.append({"bucket": "tier1_context", "path": p.relative_to(root).as_posix()
                              if root in p.parents or p.is_relative_to(root) else p.name,
                              "tokens": estimate_tokens(txt)})

    # 2) CLAUDE.md
    cl = root / "CLAUDE.md"
    if cl.is_file():
        items.append({"bucket": "claude_md", "path": "CLAUDE.md",
                      "tokens": estimate_tokens(cl.read_text(encoding="utf-8", errors="replace"))})

    # 3) описания экспортируемой поверхности: скиллы (description) + команды (краткое описание)
    sroot = _skills_root(root)
    if sroot:
        for sk in sorted(sroot.glob("*/SKILL.md")):
            fm, _ = _frontmatter(sk.read_text(encoding="utf-8", errors="replace"))
            desc = str(fm.get("description") or "")
            items.append({"bucket": "skill_descriptions", "path": sk.parent.name,
                          "tokens": estimate_tokens(desc)})
    croot2 = _commands_root(root)
    if croot2:
        for cm in sorted(croot2.glob("*.md")):
            desc = _command_desc(cm.read_text(encoding="utf-8", errors="replace"))
            items.append({"bucket": "command_descriptions", "path": cm.stem,
                          "tokens": estimate_tokens(cm.stem + " — " + desc)})

    buckets = {}
    for it in items:
        buckets[it["bucket"]] = buckets.get(it["bucket"], 0) + it["tokens"]
    total = sum(it["tokens"] for it in items)
    return {"kind": "context-cost", "child_root": str(root), "budget": budget,
            "total_tokens": total, "within_budget": total <= budget,
            "buckets": buckets, "items": items}


def _budget_from_config(root: Path):
    cfg = root / ".ai-ops.yaml"
    if cfg.is_file():
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            b = (data.get("context_budget") or {}).get("session_start_tokens")
            if isinstance(b, int) and b > 0:
                return b
        except (yaml.YAMLError, OSError):
            pass
    return DEFAULT_BUDGET


def _fmt(rep):
    L = [f"=== стоимость стартового контекста ({rep['child_root']}) ==="]
    for bucket in ("tier1_context", "claude_md", "skill_descriptions", "command_descriptions"):
        b_items = [i for i in rep["items"] if i["bucket"] == bucket]
        if not b_items:
            continue
        L.append(f"  {bucket}: {rep['buckets'].get(bucket, 0)} токенов")
        for it in sorted(b_items, key=lambda x: -x["tokens"])[:8]:
            L.append(f"      {it['tokens']:>6}  {it['path']}")
    verdict = "OK" if rep["within_budget"] else "ПРЕВЫШЕНИЕ"
    L.append(f"ИТОГО: {rep['total_tokens']} токенов / бюджет {rep['budget']} — {verdict}"
             + ("" if rep["within_budget"]
                else f" (на {rep['total_tokens'] - rep['budget']} больше — сократите ярус 1 / поверхность)"))
    return "\n".join(L)


def summary_line(child_root, budget=None):
    """Одна строка для doctor."""
    rep = estimate(child_root, budget=budget)
    v = "✓" if rep["within_budget"] else f"✗ +{rep['total_tokens'] - rep['budget']}"
    return f"стоимость старта (оценка): {rep['total_tokens']} токенов / бюджет {rep['budget']} {v}"


def main(argv):
    budget = None
    args, it = [], iter(argv)
    for a in it:
        if a == "--budget":
            nxt = next(it, None)
            budget = int(nxt) if nxt and nxt.isdigit() else None
        elif not a.startswith("--"):
            args.append(a)
    if not args:
        print("usage: context_cost.py <child_root> [--json] [--budget N] [--strict]"); return 2
    rep = estimate(args[0], budget=budget)
    if "--json" in argv:
        print(json.dumps({"schema_version": 1, **rep}, ensure_ascii=False, indent=2))
    else:
        print(_fmt(rep))
    return 1 if ("--strict" in argv and not rep["within_budget"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
