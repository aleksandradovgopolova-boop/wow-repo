#!/usr/bin/env python3
"""Product Registry — реестр НЕСКОЛЬКИХ продуктов и сводный флит-вид (Product Contract, срез 2).

ЗАЧЕМ. `product_contract` отвечает про ОДИН продукт. Но платформенная ценность — «увидеть состояние
ВСЕХ продуктов разом»: десять репозиториев перестают быть десятью интеграциями и становятся одной
моделью ×10. Для этого нужен список продуктов и один проход по нему.

ЧЕЙ ЭТО ФАЙЛ. Реестр флота — ОПЕРАТОРСКОЕ состояние (какие продукты ведёт человек), а НЕ часть
стандарта кита. Поэтому он не лежит в `registry/` кита (тот описывает форму и едет в дочку) и не
привязан к одной дочке: это отдельный YAML, на который оператор указывает киту. Форма проста:

    schema_version: 1
    kind: product-registry
    products:
      - {id: niti, name: "Niti", path: ~/niti}
      - {id: garden, name: "Garden", path: ~/garden}

`fleet()` идёт по списку и для КАЖДОГО продукта зовёт `product_contract.validate` — вердикт и грани
собираются существующим объектом, реестр только перечисляет и сводит. Ошибка одного продукта не
роняет флот: она становится его строкой со статусом `error`, а не исключением наружу.

СЛОЙ. Живёт в `planning` (capabilities), зовёт `product_contract` (сосед по слою). Здоровье, как и в
контракте, впрыскивается СВЕРХУ (health_map: id -> health-отчёт) — из planning intelligence не тянем.

Запуск:  python -m ai_ops_kit.planning.product_registry <registry.yaml> [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from ai_ops_kit.planning import product_contract


def validate_registry(data) -> list:
    """Ошибки ФОРМЫ реестра флота (пустой список = валиден). Чистая проверка."""
    e = []
    if not isinstance(data, dict):
        return ["реестр продуктов не объект"]
    if data.get("schema_version") != 1:
        e.append("schema_version должен быть 1")
    if data.get("kind") != "product-registry":
        e.append("kind должен быть 'product-registry'")
    products = data.get("products")
    if not isinstance(products, list) or not products:
        return e + ["products непустой список обязателен"]
    seen = set()
    for i, p in enumerate(products):
        if not isinstance(p, dict):
            e.append(f"product[{i}] не объект"); continue
        pid = p.get("id")
        if not pid:
            e.append(f"product[{i}]: нет id")
        elif pid in seen:
            e.append(f"дубликат id продукта: {pid}")
        else:
            seen.add(pid)
        if not p.get("path"):
            e.append(f"product '{pid or i}': нет path (где лежит репозиторий продукта)")
    return e


def load(registry_path) -> dict:
    """Прочитать реестр флота. Бросает FileNotFoundError, если файла нет (вызыватель решает, как
    сообщить — «нет реестра» это не «флот пуст»)."""
    p = Path(registry_path).expanduser()
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def fleet(registry_path, *, health_map: dict | None = None) -> dict:
    """Сводный вид: вердикт по каждому продукту реестра + rollup. Ошибка продукта -> его строка
    со status='error', а не падение всего флота."""
    data = load(registry_path)
    reg_errors = validate_registry(data)
    products = []
    if not reg_errors:
        for p in data.get("products", []):
            pid = p.get("id")
            root = Path(p.get("path", "")).expanduser()
            row = {"id": pid, "name": p.get("name", pid), "path": str(root)}
            if not root.is_dir():
                row.update({"status": "error", "verdict": None,
                            "reason": f"каталог не найден: {root}"})
                products.append(row); continue
            try:
                hv = (health_map or {}).get(pid)
                v = product_contract.validate(root, health=hv)
                row.update({"status": "ok", "verdict": v["verdict"],
                            "worst_artifact_state": v["worst_artifact_state"],
                            "contours_ok": v["contours_ok"], "health_band": v["health_band"],
                            "blocking_count": len(v["blocking"])})
            except Exception as ex:  # noqa: BLE001 — один битый продукт не роняет флот; честный статус
                row.update({"status": "error", "verdict": None,
                            "reason": f"{type(ex).__name__}: {ex}"})
            products.append(row)

    counts = {}
    for r in products:
        key = r.get("verdict") or r.get("status")
        counts[key] = counts.get(key, 0) + 1
    return {"schema_version": 1, "kind": "product-fleet",
            "registry": str(Path(registry_path).expanduser()),
            "registry_errors": reg_errors, "products": products, "counts": counts}


_MANAGED_HEADER = ("# products.yaml — реестр флота продуктов (операторское состояние, НЕ часть\n"
                   "# стандарта кита). Управляется командой `ai-ops products register`; при записи\n"
                   "# ручные комментарии в блоке products не сохраняются. Правьте вручную ИЛИ через\n"
                   "# register, не смешивая.\n")


def register(registry_path, path, *, pid: str | None = None, name: str | None = None) -> dict:
    """Добавить/обновить продукт в реестре флота. Upsert по id. Создаёт файл, если его нет.

    Возвращает {status: created|added|updated, product, verdict}. verdict — немедленная обратная
    связь: `product_contract.validate` по пути (чтобы register не был «записал и молчит»).
    """
    reg_path = Path(registry_path).expanduser()
    root = Path(path).expanduser()
    pid = pid or root.name
    name = name or pid
    entry = {"id": pid, "name": name, "path": str(root)}

    if reg_path.is_file():
        data = load(reg_path)
        if not isinstance(data, dict):
            data = {}
    else:
        data = {}
    data.setdefault("schema_version", 1)
    data.setdefault("kind", "product-registry")
    products = data.get("products")
    if not isinstance(products, list):
        products = []
    idx = next((i for i, p in enumerate(products)
                if isinstance(p, dict) and p.get("id") == pid), None)
    if idx is None:
        products.append(entry)
        status = "created" if not reg_path.is_file() else "added"
    else:
        products[idx] = entry
        status = "updated"
    data["products"] = products

    errs = validate_registry(data)
    if errs:
        return {"status": "invalid", "errors": errs, "product": entry}

    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text(_MANAGED_HEADER + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")

    verdict = None
    if root.is_dir():
        try:
            verdict = product_contract.validate(root)["verdict"]
        except Exception:  # noqa: BLE001 — register не обязан падать из-за состояния продукта
            verdict = None
    return {"status": status, "product": entry, "registry": str(reg_path), "verdict": verdict}


def product_path(registry_path, pid: str) -> Path | None:
    """Путь продукта флота по id, либо None. Нужен вызывателю (CLI), чтобы посчитать health/risks
    ДО inspect (их считает слой выше и впрыскивает вниз)."""
    data = load(registry_path)
    products = data.get("products", []) if isinstance(data, dict) else []
    p = next((x for x in products if isinstance(x, dict) and x.get("id") == pid), None)
    return Path(p.get("path", "")).expanduser() if p else None


def inspect(registry_path, pid: str, *, health: dict | None = None,
            risks: dict | None = None) -> dict:
    """Подробная карточка ОДНОГО продукта флота по id: полный контракт + вердикт.

    health/risks впрыскиваются СВЕРХУ (их считает intelligence через CLI). Если id нет в реестре —
    {status: not_found, known: [...]} (не выдумываем продукт)."""
    data = load(registry_path)
    products = data.get("products", []) if isinstance(data, dict) else []
    p = next((x for x in products if isinstance(x, dict) and x.get("id") == pid), None)
    if p is None:
        return {"status": "not_found", "id": pid,
                "known": [x.get("id") for x in products if isinstance(x, dict)]}
    root = Path(p.get("path", "")).expanduser()
    if not root.is_dir():
        return {"status": "error", "id": pid, "path": str(root),
                "reason": f"каталог продукта не найден: {root}"}
    return {"status": "ok", "id": pid, "name": p.get("name", pid),
            "contract": product_contract.resolve(root, health=health, risks=risks),
            "verdict": product_contract.validate(root, health=health)}


def _default_registry(cwd: Path) -> Path | None:
    """Где искать реестр флота без явного пути: $AI_OPS_PRODUCTS, затем products.yaml рядом."""
    import os
    env = os.environ.get("AI_OPS_PRODUCTS")
    if env:
        return Path(env).expanduser()
    for name in ("products.yaml", ".ai-ops-products.yaml"):
        cand = cwd / name
        if cand.is_file():
            return cand
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="product_registry.py",
                                 description="Product Registry: сводный вердикт по всем продуктам")
    ap.add_argument("registry", nargs="?", help="путь к реестру флота (иначе $AI_OPS_PRODUCTS / products.yaml)")
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])

    reg_path = Path(ns.registry).expanduser() if ns.registry else _default_registry(Path.cwd())
    if reg_path is None or not Path(reg_path).is_file():
        print("НЕТ РЕЕСТРА ПРОДУКТОВ. Создайте products.yaml со списком продуктов:")
        print("  schema_version: 1\n  kind: product-registry\n  products:\n"
              "    - {id: niti, name: Niti, path: ~/niti}")
        print("и укажите путь: product_registry.py <файл> (или переменная AI_OPS_PRODUCTS).")
        return 1

    rep = fleet(reg_path)
    if ns.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    if rep["registry_errors"]:
        print(f"РЕЕСТР ПРОДУКТОВ {rep['registry']}: ошибки формы:")
        for x in rep["registry_errors"]:
            print(f"  - {x}")
        return 1
    print(f"ФЛОТ ({len(rep['products'])} продукт(ов)) — {rep['counts']}:")
    for r in rep["products"]:
        if r["status"] == "error":
            print(f"  ✗ {r['id']}: ОШИБКА — {r.get('reason')}")
        else:
            mark = "✓" if r["verdict"] == "valid" else "•"
            print(f"  {mark} {r['id']} ({r['name']}): {r['verdict']} "
                  f"[артефакт={r['worst_artifact_state']}, контуры={'ok' if r['contours_ok'] else 'неполны'}, "
                  f"health={r['health_band']}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
