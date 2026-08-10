#!/usr/bin/env python3
"""Целостность ссылок внутри пакета (v2.9) — drift-control, наведённый на себя.

Идея из team-os-toolkit (claims/drift): ссылка, которую никто не проверяет,
тихо протухает. Кит за релизы 2.7–2.8 оброс ссылками `uses_skills`, `checklist:`,
`source_of_truth:`, `owner:` — и ни одна из них не проверялась. Этот валидатор
детерминированно проверяет, что каждая ссылка резолвится:

  1. workflows.yaml: stage.owner / stage.writer  -> id в registry/agents.yaml;
  2. workflows.yaml: stage.uses_skills[*]        -> shipped-скилл (manifest) или
                                                    внешний скилл раннера (allowlist);
  3. workflows.yaml: quality_gates[*]            -> gate в quality/gates.yaml;
  4. gates.yaml: checklist / source_of_truth     -> существующий файл (без #anchor);
  5. rules/**/*.yaml: skill / source_of_truth    -> существующий файл;
  6. skills/*/SKILL.md frontmatter: checklist     -> существующий файл;
  7. manifest.skills.shipped[*].path / .checklist -> существующий файл.

Использование:  validate_references.py [--json] | --selftest
Возврат 0 — все ссылки резолвятся, 1 — есть висячая ссылка (или ошибка чтения).
"""

import json
import re
import sys
import tempfile
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
# Внешние скиллы, которые предоставляет раннер (не поставляются китом):
EXTERNAL_SKILLS = {"deep-research"}


def load_yaml(p: Path):
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def frontmatter(md: str):
    if md.startswith("---"):
        parts = md.split("---", 2)
        if len(parts) >= 3:
            return yaml.safe_load(parts[1]) or {}
    return {}


def collect(root: Path):
    """Собрать множества id и функцию проверки путей относительно root."""
    agents = load_yaml(root / "registry" / "agents.yaml")
    agent_ids = {a["id"] for a in (agents.get("agents") or agents.get("registry") or []) if isinstance(a, dict) and "id" in a}
    gates = load_yaml(root / "quality" / "gates.yaml")
    gate_ids = set((gates.get("gates") or {}).keys())
    manifest = load_yaml(root / "manifest" / "ai-ops-manifest.yaml")
    shipped = (manifest.get("skills") or {}).get("shipped") or []
    skill_ids = {s["id"] for s in shipped if isinstance(s, dict) and "id" in s}
    return agent_ids, gate_ids, skill_ids, shipped


def path_exists(root: Path, ref: str) -> bool:
    ref = ref.split("#", 1)[0].strip()      # отбросить #anchor
    if not ref:
        return True
    return (root / ref).exists()


def check(root: Path):
    findings = []

    def bad(kind, ref, where):
        findings.append({"kind": kind, "ref": ref, "where": where})

    agent_ids, gate_ids, skill_ids, shipped = collect(root)

    # --- workflows.yaml ---
    wf = load_yaml(root / "registry" / "workflows.yaml")
    for wid, w in (wf.get("workflows") or {}).items():
        for g in (w.get("quality_gates") or []):
            if g not in gate_ids:
                bad("gate", g, f"workflow {wid}.quality_gates")
        for st in (w.get("stages") or []):
            for role_key in ("owner", "writer"):
                who = st.get(role_key)
                if who and who not in agent_ids:
                    bad("agent", who, f"workflow {wid}.{st.get('id')}.{role_key}")
            for sk in (st.get("uses_skills") or []):
                if sk not in skill_ids and sk not in EXTERNAL_SKILLS:
                    bad("skill", sk, f"workflow {wid}.{st.get('id')}.uses_skills")

    # --- gates.yaml: checklist / source_of_truth ---
    gates = load_yaml(root / "quality" / "gates.yaml")
    for gid, g in (gates.get("gates") or {}).items():
        for key in ("checklist", "source_of_truth"):
            ref = g.get(key)
            if isinstance(ref, str) and not path_exists(root, ref):
                bad("path", ref, f"gate {gid}.{key}")

    # --- rules/**/*.yaml: skill / source_of_truth ---
    for rp in sorted((root / "rules").rglob("*.yaml")):
        try:
            d = load_yaml(rp) or {}
        except yaml.YAMLError:
            continue
        for key in ("skill", "source_of_truth"):
            ref = d.get(key)
            if isinstance(ref, str) and not path_exists(root, ref):
                bad("path", ref, f"{rp.relative_to(root)}.{key}")

    # --- skills/*/SKILL.md frontmatter: checklist ---
    for sp in sorted((root / "skills").glob("*/SKILL.md")):
        fm = frontmatter(sp.read_text(encoding="utf-8"))
        ref = fm.get("checklist")
        if isinstance(ref, str) and not path_exists(root, ref):
            bad("path", ref, f"{sp.relative_to(root)}.checklist")

    # --- manifest.skills.shipped: path / checklist ---
    for s in shipped:
        for key in ("path", "checklist"):
            ref = s.get(key)
            if isinstance(ref, str) and not path_exists(root, ref):
                bad("path", ref, f"manifest.skills.shipped[{s.get('id')}].{key}")
        # конвенция авторинга (rules/meta/skill-authoring): frontmatter name + description
        sp = s.get("path")
        if isinstance(sp, str) and (root / sp).exists():
            fm = frontmatter((root / sp).read_text(encoding="utf-8"))
            for req in ("name", "description"):
                if not fm.get(req):
                    bad("skill-frontmatter", f"{s.get('id')}: нет '{req}'", f"{sp} frontmatter")

    # --- manifest: package-относительные пути (инструменты/контракты/примеры) резолвятся ---
    # Раньше валидатор смотрел только shipped skills — routing engine, updater, examples,
    # compatibility check и т.п. могли тихо протухнуть (02_tools/... после переезда структуры).
    man_p = root / "manifest" / "ai-ops-manifest.yaml"
    if man_p.exists():
        man = load_yaml(man_p)
        for tok in _manifest_path_tokens(man):
            if not path_exists(root, tok.rstrip("/")):
                bad("manifest-path", tok, "manifest/ai-ops-manifest.yaml")

    return findings


# package-относительные префиксы: токены с ними — это пути к файлам/каталогам пакета
_MANIFEST_PATH_PREFIXES = (
    "registry/", "installer/", "validation/", "tools/", "examples/", "schemas/",
    "quality/", "security/", "agents/", "rules/", "templates/", "context/",
    "manifest/", "evaluations/", "config/", "knowledge/", "decisions/",
    "governance/", "skills/", "openspec/", "workflows/", "commands/",
)


def _manifest_path_tokens(node):
    """Собрать из манифеста токены, похожие на package-относительные пути.
    Пропускает glob (*), URL (://), child-пути (.ai/…) и плейсхолдеры (<…>)."""
    out = []

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, str):
            s = v.strip()
            if "*" in s or "://" in s or s.startswith(".ai") or "<" in s:
                return
            for tok in s.split():
                tok = tok.rstrip(":").strip()
                if tok.startswith(_MANIFEST_PATH_PREFIXES):
                    out.append(tok)

    walk(node)
    return out


def run(root: Path, as_json=False):
    findings = check(root)
    if as_json:
        print(json.dumps({"schema_version": 1, "kind": "reference-integrity",
                          "findings": findings}, ensure_ascii=False, indent=2))
    elif findings:
        print(f"REFERENCES: {len(findings)} висячих ссылок:")
        for f in findings:
            print(f"  [{f['kind']}] '{f['ref']}' -> не резолвится ({f['where']})")
    else:
        print("REFERENCES-OK: все ссылки (агенты/гейты/скиллы/чек-листы/источники) резолвятся.")
    return 1 if findings else 0


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    # 1) реальный пакет: ссылок быть не должно
    real = check(PKG)
    expect("реальный пакет без висячих ссылок", real == [])

    # 2) искусственный слом: гейт видят падающим (принцип team-os)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "registry").mkdir(parents=True)
        (root / "quality").mkdir()
        (root / "manifest").mkdir()
        (root / "rules").mkdir()
        (root / "skills").mkdir()
        (root / "registry" / "agents.yaml").write_text(
            "agents:\n  - id: real-agent\n", encoding="utf-8")
        (root / "quality" / "gates.yaml").write_text(
            "gates:\n  real_gate: {id: real_gate}\n", encoding="utf-8")
        (root / "manifest" / "ai-ops-manifest.yaml").write_text(
            "skills:\n  shipped:\n    - id: real-skill\n      path: skills/real-skill/SKILL.md\n"
            "update_policy:\n  updater: installer/does_not_exist.py\n",   # протухший путь
            encoding="utf-8")
        (root / "skills" / "real-skill").mkdir()
        (root / "skills" / "real-skill" / "SKILL.md").write_text(
            "---\nname: real-skill\nchecklist: rules/missing.yaml\n---\n", encoding="utf-8")
        (root / "registry" / "workflows.yaml").write_text(
            "workflows:\n"
            "  W:\n"
            "    quality_gates: [ghost_gate]\n"
            "    stages:\n"
            "      - {id: s1, owner: ghost-agent, uses_skills: [ghost-skill]}\n",
            encoding="utf-8")
        f = check(root)
        kinds = {x["kind"] for x in f}
        expect("ловит несуществующий gate", "gate" in kinds)
        expect("ловит несуществующего agent", "agent" in kinds)
        expect("ловит несуществующий skill", "skill" in kinds)
        expect("ловит битый checklist-путь", "path" in kinds)
        expect("deep-research (внешний) НЕ ложно-битый",
               all(x["ref"] != "deep-research" for x in f))
        expect("ловит протухший путь в манифесте", "manifest-path" in kinds)
    print("validate_references selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    return run(PKG, as_json="--json" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
