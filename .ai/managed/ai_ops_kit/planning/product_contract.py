#!/usr/bin/env python3
"""ProductContract — ЕДИНЫЙ объект продукта поверх разрозненных подсистем (Product Contract, срез 1).

ЗАЧЕМ. Кит уже умеет описывать каждую грань продукта по отдельности: идентичность (Product Passport),
обязательные артефакты и их состояние (artifact-registry + product_templates), источники истины по
контурам (product-operating-model + contours), здоровье (intelligence.health_*). Но «десять продуктов»
превращаются в десять уникальных интеграций, пока нет ОДНОГО объекта, который отвечает: кто этот
продукт, какому стандарту он следует и удовлетворяет ли он ему СЕЙЧАС. Этот модуль — тот объект.

ЧТО ЭТО НЕ. Не второй реестр и не новая правда о форме: `resolve()` НИЧЕГО не считает сам — он
СОБИРАЕТ ответ из существующих вычислителей (`product_templates.report`, `contours.sot_state`,
`passport_generator.sections`) и двух реестров. Единственный источник истины формы по-прежнему
`registry/artifact-registry.yaml` и `registry/product-operating-model.yaml`; здесь только агрегация.

СЛОЙ. Модуль живёт в `planning` (capabilities) и зовёт только соседей по слою и ниже. Здоровье
(`intelligence`, слой ВЫШЕ) сюда не импортируется — оно ВПРЫСКИВАЕТСЯ сверху (из CLI) параметром
`health`, иначе агрегатор потянул бы intelligence вверх. Нет health — контракт честно говорит
`not_computed`, а не выдумывает зелёное.

Запуск:  python -m ai_ops_kit.planning.product_contract <repo> [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from ai_ops_kit.planning import artifact_registry as AR
from ai_ops_kit.planning import contours, passport_generator, product_templates

# Разделы паспорта, относящиеся к ИДЕНТИЧНОСТИ продукта (остальные — здоровье/milestone — грани
# health/lifecycle, у них свои вычислители). Берём по известным ключам с .get — если раздел
# переименован в шаблоне, он просто не попадёт, а не уронит сборку.
_IDENTITY_SECTIONS = ["Название и описание", "Repository и окружения", "Owner и команда"]

# Порядок ухудшения из artifact-registry.lifecycle_states: 0 — хуже всего.
_STATE_ORDER = {product_templates.MISSING: 0, product_templates.INVALID: 1,
                product_templates.OUTDATED: 2, product_templates.VALID: 3}


def _identity(repo_root: Path) -> dict:
    """Идентичность продукта — из генератора паспорта (с уровнями доверия verified/inferred/unknown)."""
    try:
        secs = passport_generator.sections(repo_root)
    except Exception as e:  # noqa: BLE001 — идентичность не должна ронять контракт; честный пробел
        return {"_error": f"идентичность не собрана: {type(e).__name__}: {e}"}
    return {name: secs[name] for name in _IDENTITY_SECTIONS if name in secs}


def _required_gates(pkg_root: Path | None = None) -> list:
    """Блокирующие гейты стандарта — читаем quality/gates.yaml КАК ДАННЫЕ (без импорта пакета gates,
    чтобы не заводить межпакетное ребро planning->gates ради списка)."""
    root = pkg_root or AR.PKG
    gp = Path(root) / "quality" / "gates.yaml"
    try:
        data = yaml.safe_load(gp.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    gates = data.get("gates") or {}
    out = []
    for gid, g in gates.items() if isinstance(gates, dict) else []:
        if isinstance(g, dict) and g.get("blocking"):
            out.append(gid)
    return sorted(out)


def resolve(repo_root, *, health: dict | None = None, risks: dict | None = None,
            reg: dict | None = None) -> dict:
    """Собрать ProductContract для репозитория. Ничего не изобретает — агрегирует существующее.

    health — отчёт intelligence.health (band/reasons/complete), впрыснутый СВЕРХУ; None -> not_computed.
    risks — реестр рисков intelligence.risk_register (count_by_severity/risks/blind_spots), тоже
    впрыснутый СВЕРХУ (risk_register живёт в intelligence, выше planning); None -> not_computed.
    """
    root = Path(repo_root)
    reg = reg or AR.load()

    tmpl = product_templates.report(root, reg)            # {artifacts:{id:{state,reason}}, counts, valid}
    by_id = {a["id"]: a for a in AR.artifacts(reg)}
    artifacts = {}
    for aid, st in tmpl["artifacts"].items():
        a = by_id.get(aid, {})
        artifacts[aid] = {"required": bool(a.get("required")), "title": a.get("title", aid),
                          "owner_role": a.get("owner_role"), "source_contour": a.get("source_contour"),
                          "state": st["state"], "reason": st["reason"]}

    sot = contours.sot_state(root)                        # {cid:{ok, present, required_missing, ...}}

    hb = ({"band": health.get("band"), "reasons": health.get("reasons", []),
           "complete": health.get("complete")} if isinstance(health, dict)
          else {"state": "not_computed",
                "reason": "health меряет intelligence; впрысни отчёт параметром health= или зови "
                          "через CLI, который его считает"})

    return {
        "schema_version": 1,
        "kind": "product-contract",
        "repository": str(root),
        "identity": _identity(root),
        "standard": {"contract_version": reg.get("contract_version"),
                     "registry": "artifact-registry.yaml + product-operating-model.yaml"},
        "artifacts": {"items": artifacts, "counts": tmpl["counts"]},
        "contours": {cid: {"title": v.get("title"), "owner_role": v.get("owner_role"),
                           "ok": v.get("ok"), "present": v.get("present", []),
                           "required_missing": v.get("required_missing", [])}
                     for cid, v in sot.items()},
        "quality": {"required_gates": _required_gates()},
        "health": hb,
        "risks": ({"count_by_severity": risks.get("count_by_severity"),
                   "count_by_category": risks.get("count_by_category"),
                   "blind_spots": risks.get("blind_spots", []),
                   "items": risks.get("risks", [])} if isinstance(risks, dict)
                  else {"state": "not_computed",
                        "reason": "риски считает intelligence.risk_register; впрысни через risks= "
                                  "или зови через CLI"}),
    }


def validate(repo_root, *, health: dict | None = None, reg: dict | None = None) -> dict:
    """Единый вердикт: удовлетворяет ли продукт своему контракту СЕЙЧАС.

    verdict='valid' ТОЛЬКО если все обязательные артефакты valid, все обязательные источники истины
    контуров на месте и здоровье не красное (если посчитано). Иначе 'not_ready' с ПРИЧИНАМИ — не
    сглаживаем: недоказанное и неполное называется своим словом.
    """
    c = resolve(repo_root, health=health, reg=reg)
    blocking = []
    worst = product_templates.VALID
    for aid, v in c["artifacts"]["items"].items():
        if v["state"] != product_templates.VALID and _STATE_ORDER[v["state"]] < _STATE_ORDER[worst]:
            worst = v["state"]
        if v["required"] and v["state"] in (product_templates.MISSING, product_templates.INVALID):
            blocking.append(f"артефакт '{aid}' ({v['title']}): {v['state']} — {v['reason']}")
    contours_ok = True
    for cid, cv in c["contours"].items():
        if cv.get("required_missing"):
            contours_ok = False
            blocking.append(f"контур '{cid}': нет обязательных источников истины: "
                            f"{', '.join(cv['required_missing'])}")

    band = c["health"].get("band")
    if band == "red":
        blocking.append(f"здоровье красное: {'; '.join(c['health'].get('reasons', []) or ['без причины'])}")

    verdict = "valid" if (not blocking and worst == product_templates.VALID) else "not_ready"
    return {
        "schema_version": 1,
        "kind": "product-contract-verdict",
        "repository": str(Path(repo_root)),
        "verdict": verdict,
        "worst_artifact_state": worst,
        "contours_ok": contours_ok,
        "health_band": band or c["health"].get("state"),
        "artifact_counts": c["artifacts"]["counts"],
        "blocking": blocking,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(prog="product_contract.py",
                                 description="ProductContract: единый объект продукта + вердикт")
    ap.add_argument("repo", help="путь к репозиторию продукта")
    ap.add_argument("--verdict", action="store_true", help="только вердикт validate(), не весь контракт")
    ap.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])

    out = validate(ns.repo) if ns.verdict else resolve(ns.repo)
    if ns.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if ns.verdict:
        print(f"PRODUCT-CONTRACT {out['repository']}: вердикт = {out['verdict'].upper()} "
              f"(артефакты: {out['artifact_counts']}, контуры {'OK' if out['contours_ok'] else 'НЕПОЛНЫ'}, "
              f"health={out['health_band']})")
        for b in out["blocking"]:
            print(f"  - {b}")
        return 0 if out["verdict"] == "valid" else 1

    print(f"PRODUCT-CONTRACT {out['repository']} (стандарт v{out['standard']['contract_version']}):")
    print(f"  артефакты: {out['artifacts']['counts']}")
    for cid, cv in out["contours"].items():
        mark = "ok" if cv["ok"] else f"НЕПОЛН ({', '.join(cv['required_missing'])})"
        print(f"  контур {cid}: {mark}")
    print(f"  здоровье: {out['health'].get('band') or out['health'].get('state')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
