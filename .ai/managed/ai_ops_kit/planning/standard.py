#!/usr/bin/env python3
"""SR-1..4: стандарт репозитория как ВЕРСИОНИРУЕМЫЙ объект, отдельный от версии пакета.

Проблема (SR-1): версия стандарта и версия кита были одним числом, поэтому любой патч кита выглядел
как изменение требований к репозиторию, а «Child A на 1.4, Child B на 1.3» сказать было нечем.

Что здесь:
  * `standard_version` (registry/standard.yaml) — растёт ТОЛЬКО при изменении требований к
    репозиторию: новый обязательный артефакт, новая обязательная секция, изменение силы проверки.
  * `requirements_fingerprint` — отпечаток состава требований, СЧИТАННОГО из существующих реестров
    (SR-2: манифест стандарта ССЫЛАЕТСЯ на реестры, а не заводит второй список). Тест краснеет,
    если состав изменился, а версия/отпечаток — нет (ратчет, как footprint/layering/dormant).
  * `status(child_version)` (SR-4) — установленная версия стандарта, доступная, изменилось ли.

Границы честны: отпечаток покрывает МАШИНОЧИТАЕМУЮ поверхность требований — обязательные артефакты,
обязательные секции и силу гейтов. Свободные текстовые инварианты (AGENTS.md) сюда не входят: их
изменение — отдельное решение, а отпечаток не притворяется, что видит их.

Использование: python3 -m ai_ops_kit.planning.standard [<repo_root>] [--json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[2]
STANDARD_FILE = "registry/standard.yaml"


def _y(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def requirement_set(pkg_root: Path = PKG) -> dict:
    """Состав требований к репозиторию, СЧИТАННЫЙ из существующих реестров (SR-2). -> dict.

    Не второй список: читаем product-operating-model (контуры), artifact-registry (слой .ai-ops/),
    manifest (context/repo-артефакты) и gates (сила). Именно по этому составу считается отпечаток.
    """
    root = Path(pkg_root)
    pom = _y(root / "registry" / "product-operating-model.yaml")
    ar = _y(root / "registry" / "artifact-registry.yaml")
    man = _y(root / "manifest" / "ai-ops-manifest.yaml")
    gates = _y(root / "quality" / "gates.yaml")

    required_artifacts = set()
    required_sections = set()

    # контуры продукта
    for c in pom.get("contours") or []:
        for s in c.get("source_of_truth") or []:
            if s.get("required"):
                required_artifacts.add(s["path"])
            for sec in s.get("required_sections") or []:
                required_sections.add(f"{s['path']}::{sec}")

    # слой .ai-ops/ (artifact-registry)
    for a in ar.get("artifacts") or []:
        if a.get("required"):
            required_artifacts.add(a.get("path", a.get("id")))
        for sec in ((a.get("structure") or {}).get("required_sections") or []):
            required_sections.add(f"{a.get('path', a.get('id'))}::{sec}")

    # #609: каталог стандарта по ярусам — часть версионируемой поверхности стандарта (SR-1/SR-2).
    # Учитываем только элементы, чей ярус применим к профилю по умолчанию: смена состава ярусов или
    # их применимости к default-профилю меняет отпечаток и обязана поднять standard_version.
    default_profile = _y(root / "registry" / "standard.yaml").get("default_profile")
    prof = (ar.get("profiles") or {}).get(default_profile) or {}
    default_tiers = set(prof.get("includes_tiers") or [])
    for c in ar.get("standard_catalog") or []:
        if c.get("tier") in default_tiers:
            required_artifacts.add(f"tier:{c.get('tier')}:{c.get('id')}")

    # manifest: context-доки + repo-артефакты продуктовой модели
    so = man.get("session_orchestration") or {}
    ls = (so.get("living_status") or {})
    for d in ls.get("required_context_docs") or []:
        required_artifacts.add(f"context/{d}")
    pom_man = (so.get("product_operating_model") or {})
    for a in pom_man.get("required_repo_artifacts") or []:
        required_artifacts.add(a)

    # сила проверок (change of check strength — триггер версии): id -> blocking из полных
    # определений `gates` (`mvp_blocking_gates` — лишь список id блокирующего набора).
    gate_strength = {}
    gdict = gates.get("gates") or {}
    _iter = gdict.values() if isinstance(gdict, dict) else gdict
    for g in _iter:
        if isinstance(g, dict) and g.get("id"):
            gate_strength[g["id"]] = bool(g.get("blocking"))

    return {
        "required_artifacts": sorted(required_artifacts),
        "required_sections": sorted(required_sections),
        "gate_strength": dict(sorted(gate_strength.items())),
    }


def compute_fingerprint(pkg_root: Path = PKG) -> str:
    """sha256-отпечаток состава требований (первые 16 hex). Меняется — обязана вырасти версия."""
    payload = json.dumps(requirement_set(pkg_root), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def load(pkg_root: Path = PKG) -> dict:
    """registry/standard.yaml -> dict (пустой, если файла нет)."""
    return _y(Path(pkg_root) / STANDARD_FILE)


def current_version(pkg_root: Path = PKG) -> int:
    return int(load(pkg_root).get("standard_version") or 0)


def status(child_version, pkg_root: Path = PKG) -> dict:
    """SR-4: сверка версии стандарта дочки с доступной в пакете. -> {installed, available, changed}."""
    available = current_version(pkg_root)
    try:
        installed = int(child_version) if child_version is not None else None
    except (TypeError, ValueError):
        installed = None
    return {
        "installed": installed,
        "available": available,
        "changed": installed is not None and installed != available,
        "behind": installed is not None and installed < available,
    }


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="Стандарт репозитория как версия (SR-1..4)")
    ap.add_argument("repo_root", nargs="?", default=str(PKG))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    ver = current_version()
    fp_declared = load().get("requirements_fingerprint")
    fp_now = compute_fingerprint()
    rep = {"standard_version": ver, "fingerprint_declared": fp_declared,
           "fingerprint_now": fp_now, "in_sync": fp_declared == fp_now}
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(f"Версия стандарта: {ver}")
        print(f"Отпечаток требований: объявлен {fp_declared}, сейчас {fp_now} — "
              + ("совпадает" if rep["in_sync"] else "РАСХОЖДЕНИЕ: поднимите standard_version"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
