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
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
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
    for f in manifest_path_findings(root):
        bad(f["kind"], f["ref"], f["where"])

    return findings


# package-относительные префиксы: токены с ними — это пути к файлам/каталогам пакета
_MANIFEST_PATH_PREFIXES = (
    "registry/", "installer/", "validation/", "tools/", "examples/", "schemas/",
    "quality/", "security/", "agents/", "rules/", "templates/", "context/",
    "manifest/", "evaluations/", "config/", "knowledge/", "decisions/",
    "governance/", "skills/", "openspec/", "workflows/", "commands/",
)


# ЧТО ДОСТАВЛЯЕТСЯ ДОЧКЕ — СПРАШИВАЕМ У МАНИФЕСТА, А НЕ ПОМНИМ СПИСКОМ.
#
# ЗАМЕР 20.08.2026 (ночной обзор, первый живой прогон на трёх подключённых репозиториях). В КАЖДОЙ
# дочке валидатор ссылок давал 33 «не резолвится» подряд: `skills/`, `governance/`, `openspec/`,
# `decisions/`, `knowledge/`, `examples/`, `installer/`. Все эти каталоги есть в ките и НЕ входят в
# `update_policy.managed_set` — то есть у дочки их не будет никогда, и красный не гасится никакой
# работой.
#
# Красный, который нельзя погасить, не проверка: отчёт с ним перестают читать построчно, и вместе
# с вечной строкой пролистываются настоящие находки. Из тех же 33 настоящих оказалось ровно две —
# их и должно быть видно.
#
# Список доставки НЕ ДУБЛИРУЕТСЯ здесь константой: он уже записан в манифесте, и вторая копия
# разошлась бы с первой на первом же изменении поставки. Тот же принцип, что «реестр — источник
# истины»: спрашиваем у него, а не помним.
def _delivered_tops(man) -> set:
    """Верхние каталоги из `update_policy.managed_set`. -> {"registry/", "tools/", …}."""
    out = set()
    for pat in ((man.get("update_policy") or {}).get("managed_set") or []):
        top = str(pat).split("/", 1)[0].strip()
        if top and "*" not in top and "." not in top:
            out.add(top + "/")
    return out


def _is_kit_repo(root) -> bool:
    """Кит это или установленная копия у дочки. -> bool. Признак кита — установщик рядом."""
    return (Path(root) / "installer").is_dir()


def manifest_path_findings(root):
    """Пути манифеста, которые не резолвятся. -> list[dict].

    Вынесено из `collect` отдельной функцией, чтобы правило «kit-only не краснеет у дочки»
    проверялось пробой напрямую, а не через сборку целого репозитория: проверку, которую дорого
    позвать, перестают звать.
    """
    man_p = Path(root) / "manifest" / "ai-ops-manifest.yaml"
    if not man_p.exists():
        return []
    man = load_yaml(man_p)
    kit = _is_kit_repo(root)
    delivered = _delivered_tops(man)
    out = []
    for tok in _manifest_path_tokens(man):
        # У дочки проверяем только то, что ей ДОСТАВЛЯЮТ. Остальное — инструменты кита, и их
        # отсутствие у дочки не дефект, а замысел (см. комментарий у `_delivered_tops`).
        if not kit and delivered and not tok.startswith(tuple(delivered)):
            continue
        if not path_exists(root, tok.rstrip("/")):
            out.append({"kind": "manifest-path", "ref": tok,
                        "where": "manifest/ai-ops-manifest.yaml"})
    return out


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


def main(argv):
    return run(PKG, as_json="--json" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
