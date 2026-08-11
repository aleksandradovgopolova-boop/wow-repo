#!/usr/bin/env python3
"""validate_runtime_surface.py (v3.14.0 Startup Context Budget, срез 3) — бюджет стоимости самого кита:
(1) описание КАЖДОГО поставляемого скилла ≤ 300 символов (описание — это триггеры и границы; подробности
живут в теле, которое грузится по вызову, а не в стартовом листинге); (2) well-formedness декларации
`runtime_surface` в `.ai-ops.yaml` (что репозиторий экспортирует в .claude/skills и .claude/commands).

Причина: описания скиллов/команд рантайм держит в контексте на КАЖДОМ старте — раздутые описания и
неотключаемая поверхность делают старт дорогим (см. tools/context_cost.py). Бюджет держит их компактными.

Advisory-строгость: rc 1 при нарушении бюджета описаний (это инвариант качества поставки кита).

Использование:  validate_runtime_surface.py [skills_dir]     — проверить бюджет описаний
                validate_runtime_surface.py --selftest
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
MAX_DESC_CHARS = 300


def _frontmatter(md: str):
    if md.startswith("---"):
        parts = md.split("---", 2)
        if len(parts) >= 3:
            try:
                return yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                return {}
    return {}


def check_skill_descriptions(skills_dir: Path):
    """-> список нарушений {skill, chars} у скиллов с описанием длиннее бюджета."""
    over = []
    for sk in sorted(skills_dir.glob("*/SKILL.md")):
        fm = _frontmatter(sk.read_text(encoding="utf-8", errors="replace"))
        desc = str(fm.get("description") or "").strip()
        if len(desc) > MAX_DESC_CHARS:
            over.append({"skill": sk.parent.name, "chars": len(desc)})
    return over


def check_runtime_surface(cfg: dict):
    """Well-formedness runtime_surface: {skills:{enabled:[...]|'all'}, commands:{enabled:[...]|'all'}}."""
    errs = []
    rs = (cfg or {}).get("runtime_surface")
    if rs is None:
        return errs                                  # отсутствие = экспортируется всё (валидно)
    if not isinstance(rs, dict):
        return ["runtime_surface должен быть mapping"]
    for key in ("skills", "commands"):
        sect = rs.get(key)
        if sect is None:
            continue
        if not isinstance(sect, dict):
            errs.append(f"runtime_surface.{key} должен быть mapping"); continue
        en = sect.get("enabled")
        if en is None:
            continue
        if en != "all" and not (isinstance(en, list) and all(isinstance(x, str) for x in en)):
            errs.append(f"runtime_surface.{key}.enabled должен быть 'all' или списком строк")
    return errs


def run(skills_dir=None):
    sd = Path(skills_dir) if skills_dir else (PKG / "skills")
    over = check_skill_descriptions(sd)
    print(f"=== бюджет описаний скиллов (≤{MAX_DESC_CHARS} символов, {sd}) ===")
    for sk in sorted(sd.glob("*/SKILL.md")):
        fm = _frontmatter(sk.read_text(encoding="utf-8", errors="replace"))
        d = len(str(fm.get("description") or "").strip())
        mark = "OK" if d <= MAX_DESC_CHARS else "OVER"
        print(f"  [{mark}] {d:>4}  {sk.parent.name}")
    if over:
        print(f"НАРУШЕНИЕ БЮДЖЕТА: {len(over)} описаний >{MAX_DESC_CHARS} символов — "
              f"сократите до триггеров и границ, детали в тело скилла: "
              + ", ".join(f"{o['skill']}({o['chars']})" for o in over))
        return 1
    print("RUNTIME-SURFACE-OK: все описания скиллов в бюджете.")
    return 0


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "ok-skill").mkdir()
        (root / "ok-skill" / "SKILL.md").write_text(
            "---\nname: ok\ndescription: короткое описание в бюджете\n---\n# тело", encoding="utf-8")
        (root / "fat-skill").mkdir()
        (root / "fat-skill" / "SKILL.md").write_text(
            "---\nname: fat\ndescription: " + ("очень длинное " * 40) + "\n---\n# тело", encoding="utf-8")
        over = check_skill_descriptions(root)
        expect("раздутое описание детектируется", any(o["skill"] == "fat-skill" for o in over))
        expect("короткое описание проходит", not any(o["skill"] == "ok-skill" for o in over))

    expect("runtime_surface отсутствует -> валидно (экспорт всего)", check_runtime_surface({}) == [])
    expect("runtime_surface enabled='all' валиден",
           check_runtime_surface({"runtime_surface": {"skills": {"enabled": "all"}}}) == [])
    expect("runtime_surface enabled=[список] валиден",
           check_runtime_surface({"runtime_surface": {"commands": {"enabled": ["ai-run"]}}}) == [])
    expect("runtime_surface enabled=число -> ошибка",
           check_runtime_surface({"runtime_surface": {"skills": {"enabled": 5}}}) != [])

    # ключевой инвариант: РЕАЛЬНЫЕ поставляемые скиллы кита в бюджете
    real_over = check_skill_descriptions(PKG / "skills")
    expect("все поставляемые скиллы кита ≤300 символов",
           not real_over)
    if real_over:
        print("  превышают:", ", ".join(f"{o['skill']}({o['chars']})" for o in real_over))

    print("validate_runtime_surface selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    return run(args[0] if args else None)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
