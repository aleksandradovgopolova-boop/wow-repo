#!/usr/bin/env python3
"""storybook_adapter.py (v3.1.7) — сборка UIEvidenceBundle из ЛОКАЛЬНЫХ артефактов child-репо.

Зачем (маршрут v3.1): reviewer-false-fail сконцентрирован в 4 UI-review-гейтах (см. bench_lite /
gate_policy). Снижать его надо НЕ «довериться модели», а заменой части субъективного ревью
ПРОВЕРЯЕМЫМ UI-evidence. Этот адаптер агрегирует то, что реально производит UI-CI child-продукта:

    Storybook static build  ->  story index/manifest (какие компоненты/истории есть)
    interaction tests (vitest/play)  ->  прошли ли сценарии
    axe / a11y  ->  критические нарушения доступности
    visual report  ->  визуальные диффы
    design-system manifest  ->  переиспользование vs новые компоненты

в нормализованный, валидируемый `UIEvidenceBundle` (schemas/ui-evidence-bundle.schema.json).

Границы (важно, по решению владельца):
- БЕЗ внешнего SaaS и БЕЗ MCP: источник истины — локальные manifests и test-artifacts. Storybook MCP
  подключится позже как ИНТЕРФЕЙС для агентов (v3.6), а не как зависимость ядра enforcement.
- Сам AI Ops Kit НЕ становится React-приложением: это адаптер для child-продуктов с UI.
- Только stdlib. Аккуратно к ОТСУТСТВИЮ артефактов: нет артефакта -> status=not_run/absent
  (НЕ выдаём «нет данных» за «чисто»). enforcement по этому evidence — отдельный инкремент v3.1.8.

CLI:
  storybook_adapter.py --build <child_root> [--sha SHA] [--changed a.tsx,b.tsx] [--out bundle.json]
  storybook_adapter.py --selftest
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BUNDLE_SCHEMA_VERSION = 1

# Состояния UI, покрытие которых история обязана показывать (агрегат по затронутым компонентам).
REQUIRED_STATES = ("default", "loading", "empty", "error")
# Дополнительно отслеживаем (не обязательно): app-специфичные состояния.
EXTRA_STATES = ("restricted",)
ALL_STATES = REQUIRED_STATES + EXTRA_STATES

# Конвенциональные места артефактов в child-репо (первый существующий побеждает).
_STORY_INDEX = ("storybook-static/index.json", "storybook-static/stories.json",
                ".storybook-out/index.json")
_EVIDENCE_DIRS = (".ai/ui-evidence", "test-results", ".ui-evidence")
# v3.1.9: провенанс evidence — SHA, на котором артефакты РЕАЛЬНО собраны (кладёт UI-CI child-репо).
# bundle.commit_sha берётся ОТСЮДА (не от вызывающего), чтобы устаревшее evidence нельзя было
# выдать за свежее. Нет meta -> commit_sha=None -> unbound -> при проверке SHA гейт не освобождается.
_META = (".ai/ui-evidence/meta.json", "test-results/ui-evidence-meta.json", ".ui-evidence/meta.json")
_ARTIFACTS = {
    "interaction": ("interaction.json", "interaction-tests.json", "vitest.json"),
    "a11y": ("a11y.json", "axe.json", "accessibility.json"),
    "visual": ("visual.json", "visual-regression.json"),
    "design_system": ("design-system.json", "design_system.json"),
}


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _find(root: Path, rels) -> Path | None:
    for r in rels:
        p = root / r
        if p.exists():
            return p
    return None


def _find_artifact(root: Path, key) -> Path | None:
    for d in _EVIDENCE_DIRS:
        hit = _find(root, [f"{d}/{name}" for name in _ARTIFACTS[key]])
        if hit:
            return hit
    return None


# --- story index ----------------------------------------------------------------------------------

def _parse_story_index(data) -> list[dict]:
    """Нормализация Storybook index: v7 {entries:{id:{title,name,importPath}}} и v6 {stories:{...}}."""
    if not isinstance(data, dict):
        return []
    entries = data.get("entries")
    if not isinstance(entries, dict):
        entries = data.get("stories")
    out = []
    if isinstance(entries, dict):
        for sid, e in entries.items():
            if not isinstance(e, dict):
                continue
            if e.get("type") and e.get("type") != "story":
                continue   # docs-entry v7 — не история
            out.append({"id": e.get("id", sid), "title": e.get("title", ""),
                        "name": e.get("name", ""), "importPath": e.get("importPath", "")})
    return out


def _component_of(story: dict) -> str:
    """Компонент истории = title (напр. 'Components/MetricCard')."""
    return story.get("title") or story.get("importPath") or story.get("id", "")


def _component_base(name: str) -> str:
    """Нормализованное имя компонента для сравнения каталога/новых: последний сегмент, lower."""
    return (name or "").rstrip("/").split("/")[-1].strip().lower()


def _norm_path(p: str) -> str:
    """Нормализация относительного пути: убрать ведущие ./ и / для сравнения по суффиксу компонент."""
    return (p or "").lstrip("./").lstrip("/")


def _matches_changed(import_path: str, changed: list[str]) -> bool:
    """Story затронута, если её importPath и изменённый файл — ОДИН путь по суффиксу компонент.
    v3.1.9: убрано loose stem-matching (bare basename): `a/Card.tsx` и `b/Card.tsx` НЕ матчатся,
    иначе истории одного компонента ложно «покрывали» бы другой изменённый компонент."""
    ip = _norm_path(import_path)
    if not ip:
        return False
    ip_parts = ip.split("/")
    for c in changed:
        cc = _norm_path(c)
        if not cc:
            continue
        cc_parts = cc.split("/")
        n = min(len(ip_parts), len(cc_parts))
        # совпадение по суффиксу пути (полные компоненты пути с конца), НЕ по голому basename
        if n >= 1 and ip_parts[-n:] == cc_parts[-n:]:
            return True
    return False


# --- нормализация секций evidence -----------------------------------------------------------------

def _norm_interaction(data) -> dict:
    if not isinstance(data, dict):
        return {"status": "not_run"}
    if "status" in data:                      # уже нормализованный
        s = data["status"] if data["status"] in ("pass", "fail", "not_run") else "not_run"
        r = {"status": s}
        if "total" in data:
            r["total"] = int(data["total"])
        if "passed" in data:
            r["passed"] = int(data["passed"])
        return r
    if "numTotalTests" in data:               # vitest/jest json
        total = int(data.get("numTotalTests", 0))
        failed = int(data.get("numFailedTests", 0))
        passed = int(data.get("numPassedTests", total - failed))
        status = "not_run" if total == 0 else ("pass" if failed == 0 else "fail")
        return {"status": status, "total": total, "passed": passed}
    return {"status": "not_run"}


def _norm_a11y(data) -> dict:
    if not isinstance(data, dict):
        return {"status": "not_run", "blocking_violations": 0}
    if "blocking_violations" in data:
        bv = int(data["blocking_violations"])
        r = {"status": data.get("status") if data.get("status") in ("pass", "fail", "not_run")
             else ("pass" if bv == 0 else "fail"), "blocking_violations": bv}
        if "total_violations" in data:
            r["total_violations"] = int(data["total_violations"])
        return r
    if "violations" in data and isinstance(data["violations"], list):   # axe raw
        vs = data["violations"]
        blocking = sum(1 for v in vs if isinstance(v, dict)
                       and v.get("impact") in ("critical", "serious"))
        return {"status": "pass" if blocking == 0 else "fail",
                "blocking_violations": blocking, "total_violations": len(vs)}
    return {"status": "not_run", "blocking_violations": 0}


def _norm_visual(data) -> dict:
    if not isinstance(data, dict):
        return {"status": "not_run"}
    if "status" in data and data["status"] in ("pass", "fail", "not_run"):
        r = {"status": data["status"]}
        if "changed" in data:
            r["changed"] = int(data["changed"])
        return r
    if "changed" in data:
        ch = int(data["changed"])
        return {"status": "pass" if ch == 0 else "fail", "changed": ch}
    return {"status": "not_run"}


def _norm_design_system(data) -> dict:
    if not isinstance(data, dict):
        return {"status": "not_run", "reused_components": [], "new_components": [],
                "new_components_justified": True}
    reused = [str(x) for x in (data.get("reused_components") or [])]
    new = [str(x) for x in (data.get("new_components") or [])]
    justified = bool(data.get("new_components_justified", len(new) == 0))
    if "status" in data and data["status"] in ("pass", "fail", "not_run"):
        status = data["status"]
    else:
        status = "pass" if (not new or justified) else "fail"
    return {"status": status, "reused_components": reused, "new_components": new,
            "new_components_justified": justified}


# --- сборка bundle --------------------------------------------------------------------------------

def build_bundle(child_root, commit_sha=None, changed_files=None) -> dict:
    """Собрать UIEvidenceBundle из локальных артефактов.

    v3.1.9 (trust-фикс): bundle.commit_sha берётся из ПРОВЕНАНС-меты артефактов
    (.ai/ui-evidence/meta.json -> commit_sha), т.е. из SHA, на котором evidence РЕАЛЬНО собрано, а не
    от вызывающего. commit_sha-параметр — лишь fallback для CLI-диагностики, когда меты нет. Так
    устаревшее evidence нельзя выдать за свежее: при связывании (evidence_for_gate(..., expected_sha))
    несовпадение SHA -> гейт не освобождается.
    """
    root = Path(child_root)
    changed = [c.strip() for c in (changed_files or []) if c.strip()]
    provenance = []

    # 0) провенанс: SHA сборки evidence из меты артефактов (авторитетнее переданного commit_sha)
    meta_path = _find(root, _META)
    meta_sha = None
    if meta_path:
        meta = _load_json(meta_path)
        if isinstance(meta, dict) and meta.get("commit_sha"):
            meta_sha = str(meta["commit_sha"])
            provenance.append(str(meta_path.relative_to(root)))
    evidence_sha = meta_sha or commit_sha

    # 1) Storybook detection + story index
    idx_path = _find(root, _STORY_INDEX)
    has_config = (root / ".storybook").is_dir()
    stories = []
    if idx_path:
        stories = _parse_story_index(_load_json(idx_path))
        provenance.append(str(idx_path.relative_to(root)))
        build_status = "pass" if stories else "fail"
        detected = True
    elif has_config:
        detected, build_status = True, "fail"     # конфиг есть, а build/index нет -> сборка не удалась
    else:
        detected, build_status = False, "absent"
    storybook = {"detected": detected, "build_status": build_status, "version": None,
                 "story_count": len(stories)}

    # 2) affected components/stories (по changed files через importPath; нет changed -> все)
    if changed and stories:
        aff = [s for s in stories if _matches_changed(s.get("importPath", ""), changed)]
    else:
        aff = stories
    affected_components = sorted({_component_of(s) for s in aff if _component_of(s)})
    affected_stories = sorted({s["id"] for s in aff if s.get("id")})
    # v3.2.3: каталог дизайн-системы — нормализованные имена ВСЕХ компонентов индекса (для reuse-чека)
    component_catalog = sorted({_component_base(_component_of(s)) for s in stories if _component_of(s)})

    # 3) state coverage (агрегат по затронутым историям: состояние покрыто, если его показывает
    #    хотя бы одна затронутая история — по ключевому слову в name/id)
    covered = {}
    hay = " ".join((s.get("name", "") + " " + s.get("id", "")).lower() for s in aff)
    for st in ALL_STATES:
        covered[st] = st in hay
    required = list(REQUIRED_STATES) if affected_stories else []
    missing = [st for st in required if not covered.get(st)]
    state_coverage = {"required": required, "states": covered, "missing": missing,
                      "complete": not missing}

    # 4) секции из артефактов
    def _load_section(key, norm):
        p = _find_artifact(root, key)
        if p:
            provenance.append(str(p.relative_to(root)))
            return norm(_load_json(p))
        return norm(None)

    interaction = _load_section("interaction", _norm_interaction)
    a11y = _load_section("a11y", _norm_a11y)
    visual = _load_section("visual", _norm_visual)
    design_system = _load_section("design_system", _norm_design_system)

    return {"schema_version": BUNDLE_SCHEMA_VERSION, "kind": "UIEvidenceBundle",
            "commit_sha": evidence_sha, "generated_from": provenance,
            "affected_components": affected_components, "affected_stories": affected_stories,
            "component_catalog": component_catalog,
            "storybook": storybook, "state_coverage": state_coverage,
            "interaction_tests": interaction, "accessibility": a11y,
            "visual_regression": visual, "design_system": design_system}


def reuse_violations(bundle: dict) -> list:
    """v3.2.3 component-reuse enforcement: имена в design_system.new_components, которые УЖЕ есть в
    каталоге дизайн-системы (component_catalog) -> дублирование существующего компонента (дефект):
    надо переиспользовать, а не создавать заново. Возвращает список конфликтующих имён."""
    catalog = {_component_base(c) for c in (bundle.get("component_catalog") or [])}
    new = (bundle.get("design_system") or {}).get("new_components") or []
    return [n for n in new if _component_base(n) in catalog]


# --- мост к gate_policy: какое ДЕТЕРМИНИРОВАННОЕ evidence bundle даёт по каждому UI-гейту ----------

def evidence_for_gate(bundle: dict, expected_sha=None) -> dict:
    """Маппинг UIEvidenceBundle -> детерминированный статус по каждому UI-гейту.

    v3.1.9 EXACT-SHA BINDING (trust-фикс): если задан expected_sha (проверяемая ревизия) и
    bundle.commit_sha != expected_sha (evidence устарело / не привязано / чужой SHA) -> ВСЕ гейты
    получают deterministic_status='not_run'. То есть устаревшее/непривязанное evidence НЕ освобождает
    гейт (fail-closed), а не тихо разблокирует новый код старым pass'ом."""
    bound = expected_sha is None or bundle.get("commit_sha") == expected_sha
    if not bound:
        reason = (f"evidence не привязано к проверяемой ревизии "
                  f"(bundle.commit_sha={bundle.get('commit_sha')!r} != {expected_sha!r}) -> not_run")
        return {g: {"deterministic_status": "not_run", "residual_review": True,
                    "basis": ["exact_sha_binding_failed"], "unbound": True, "reason": reason}
                for g in ("visual_regression", "design_system_usage",
                          "accessibility_review", "ux_review")}
    vis = bundle.get("visual_regression", {})
    ds = bundle.get("design_system", {})
    a11y = bundle.get("accessibility", {})
    sc = bundle.get("state_coverage", {})
    inter = bundle.get("interaction_tests", {})
    # v3.1.9: покрытие состояний доказано ТОЛЬКО если есть затронутые истории. Пустой affected при
    # UI-правке = у изменённого компонента нет историй -> complete «вакуумно True» НЕ считается pass.
    has_affected = bool(bundle.get("affected_stories"))
    ux_pass = has_affected and sc.get("complete") and inter.get("status") == "pass"
    return {
        "visual_regression": {
            "deterministic_status": vis.get("status", "not_run"),
            "residual_review": False,             # визуальный дифф — полностью детерминирован
            "basis": ["visual_regression.status"]},
        "design_system_usage": {
            "deterministic_status": ds.get("status", "not_run"),
            "residual_review": bool(ds.get("new_components")),  # новые компоненты -> ревью обоснования
            "basis": ["design_system.status", "design_system.new_components"]},
        "accessibility_review": {
            "deterministic_status": a11y.get("status", "not_run"),   # автоматическая критическая часть
            "residual_review": True,              # семантическая доступность — за ревьюером (hybrid)
            "basis": ["accessibility.blocking_violations"]},
        "ux_review": {
            "deterministic_status": ("pass" if ux_pass
                                     else ("fail" if (inter.get("status") == "fail" or sc.get("missing"))
                                           else "not_run")),
            "residual_review": True,              # flow/copy/tone — за ревьюером (hybrid)
            "basis": ["affected_stories", "state_coverage.complete", "interaction_tests.status"]},
    }


# --- selftest -------------------------------------------------------------------------------------

def _write(root: Path, rel: str, obj):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj), encoding="utf-8")


def main(argv):
    ap = argparse.ArgumentParser(prog="storybook_adapter.py")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--build", metavar="CHILD_ROOT", help="собрать UIEvidenceBundle из child-репо")
    ap.add_argument("--sha", help="commit_sha для bundle")
    ap.add_argument("--changed", help="список изменённых файлов через запятую")
    ap.add_argument("--out", help="сохранить bundle в JSON-файл")
    a = ap.parse_args(argv)
    if a.build:
        changed = a.changed.split(",") if a.changed else None
        bundle = build_bundle(a.build, commit_sha=a.sha, changed_files=changed)
        text = json.dumps(bundle, ensure_ascii=False, indent=2)
        if a.out:
            Path(a.out).write_text(text, encoding="utf-8")
            print(f"UIEvidenceBundle -> {a.out}")
        else:
            print(text)
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
