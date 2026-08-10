#!/usr/bin/env python3
"""validate_context_completeness.py (v3.12.0 Startup Context Budget) — проверка ПОЛНОТЫ контекста
репозитория: объявленные обязательными документы (session_orchestration.living_status.required_context_docs)
должны присутствовать в project/custom-оверлее репозитория, а не только в managed-наборе кита.

Причина: новые обязательные документы контекста (ProductStatus.md, now.md) доезжают в
.ai/managed/context/**, но в .ai/project/context/** их может не быть — и «читать первым» тогда исполняет
случайный тяжёлый документ. Здесь мы проверяем, что обязательный набор РЕАЛЬНО заполнен в оверлее.

Список обязательного берётся из манифеста (session_orchestration.living_status.required_context_docs),
НЕ хардкодом. При отсутствии манифеста — консервативный дефолт.

Advisory: rc 0 (пробел — сигнал, не блок), 1 — при --strict и наличии пробелов, либо ошибка чтения.

Использование:  validate_context_completeness.py <child_root> [--strict] [--json]
                validate_context_completeness.py --selftest
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
_DEFAULT_REQUIRED = ["product/ProductStatus.md", "now.md"]


def required_docs(manifest_path: Path = None):
    """Обязательные документы контекста из манифеста (living_status.required_context_docs)."""
    mp = manifest_path or (PKG / "manifest" / "ai-ops-manifest.yaml")
    try:
        mani = yaml.safe_load(mp.read_text(encoding="utf-8")) or {}
        r = (((mani.get("session_orchestration") or {}).get("living_status") or {})
             .get("required_context_docs"))
        if r:
            return list(r)
    except (OSError, yaml.YAMLError):
        pass
    return list(_DEFAULT_REQUIRED)


def check_completeness(child_root, required=None, manifest_path: Path = None):
    """-> dict: required / present / missing / complete. Документ считается present, если есть в
    project/context ИЛИ custom/context оверлея (managed — это шаблон кита, не заполненный репозиторием)."""
    root = Path(child_root)
    required = required if required is not None else required_docs(manifest_path)
    present, missing = [], []
    for doc in required:
        in_proj = (root / ".ai/project/context" / doc).is_file()
        in_cust = (root / ".ai/custom/context" / doc).is_file()
        (present if (in_proj or in_cust) else missing).append(doc)
    return {"kind": "context-completeness", "required": required,
            "present": present, "missing": missing, "complete": not missing}


def run(child_root, strict=False, as_json=False, manifest_path: Path = None):
    rep = check_completeness(child_root, manifest_path=manifest_path)
    if as_json:
        print(json.dumps({"schema_version": 1, **rep}, ensure_ascii=False, indent=2))
    else:
        print(f"=== полнота контекста репозитория ({child_root}) ===")
        for doc in rep["required"]:
            mark = "OK" if doc in rep["present"] else "MISSING"
            print(f"  [{mark}] {doc}")
        if rep["complete"]:
            print("CONTEXT-COMPLETE: все обязательные документы контекста присутствуют в оверлее.")
        else:
            print(f"ВНИМАНИЕ: {len(rep['missing'])} обязательных документов НЕТ в project/custom — "
                  f"`ai-ops update` создаст их черновики из шаблонов: {', '.join(rep['missing'])}")
    return 1 if (strict and rep["missing"]) else 0


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    import tempfile
    req = ["product/ProductStatus.md", "now.md"]
    # пусто -> всё missing
    with tempfile.TemporaryDirectory() as td:
        r = check_completeness(td, required=req)
        expect("пустой репо -> все обязательные missing", r["missing"] == req and not r["complete"])
    # заполнено в project -> complete
    with tempfile.TemporaryDirectory() as td:
        pc = Path(td) / ".ai/project/context"
        (pc / "product").mkdir(parents=True)
        (pc / "product" / "ProductStatus.md").write_text("x", encoding="utf-8")
        (pc / "now.md").write_text("x", encoding="utf-8")
        r = check_completeness(td, required=req)
        expect("оба в project/context -> complete", r["complete"] and not r["missing"])
    # частично: только now.md в custom -> ProductStatus missing
    with tempfile.TemporaryDirectory() as td:
        cc = Path(td) / ".ai/custom/context"
        cc.mkdir(parents=True)
        (cc / "now.md").write_text("x", encoding="utf-8")
        r = check_completeness(td, required=req)
        expect("custom/context засчитывается; частичный -> ProductStatus missing",
               "now.md" in r["present"] and "product/ProductStatus.md" in r["missing"])
    # required из манифеста кита не пустой
    expect("required_docs() читает манифест кита (не пусто)", len(required_docs()) >= 1)
    # managed-only НЕ засчитывается (документ только в managed -> всё равно missing в оверлее)
    with tempfile.TemporaryDirectory() as td:
        mc = Path(td) / ".ai/managed/context"
        (mc / "product").mkdir(parents=True)
        (mc / "product" / "ProductStatus.md").write_text("x", encoding="utf-8")
        (mc / "now.md").write_text("x", encoding="utf-8")
        r = check_completeness(td, required=req)
        expect("managed-only -> оверлей пуст -> всё ещё missing (managed != заполнено репо)",
               r["missing"] == req)

    # v3.13.0 Startup Context Budget: ВСЕ шаблоны контекста кита размечены read_tier (ярусы чтения)
    missing_tier = []
    ctx = PKG / "context"
    if ctx.is_dir():
        for p in sorted(ctx.rglob("*.md")):
            txt = p.read_text(encoding="utf-8", errors="replace")
            fm = {}
            if txt.startswith("---"):
                seg = txt.split("---", 2)
                if len(seg) >= 3:
                    try:
                        fm = yaml.safe_load(seg[1]) or {}
                    except yaml.YAMLError:
                        fm = {}
            if fm.get("read_tier") not in (1, 2, 3):
                missing_tier.append(p.relative_to(ctx).as_posix())
    expect("все шаблоны context/ кита размечены read_tier 1|2|3 (v3.13.0)", not missing_tier)
    if missing_tier:
        print("  без read_tier:", ", ".join(missing_tier))

    print("validate_context_completeness selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print("usage: validate_context_completeness.py <child_root> [--strict] [--json]"); return 2
    return run(args[0], strict="--strict" in argv, as_json="--json" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
