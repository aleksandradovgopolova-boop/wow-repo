"""SequencePlan — модель плана последовательности WorkPackages.

Детерминированные хэши ОПРЕДЕЛЕНИЯ пакета/плана и ПОЛНАЯ integrity-валидация SequencePlan при каждом
чтении (schema/id/order/depends_on/циклы/пересчёт хэшей). Вынесено из workpackage_executor чистым
рефакторингом (поведение байт-в-байт): самодостаточный кластер целостности плана без git/lifecycle-
зависимостей. workpackage_executor ре-экспортирует эти имена из своей шапки, поэтому внешние импортёры
(`from ai_ops_kit.engine.workpackage_executor import _pkg_hash`) продолжают работать без изменений.
"""
from __future__ import annotations


def _pkg_hash(pkg):
    """v3.0-rc4 (P0.3): стабильный хэш ОПРЕДЕЛЕНИЯ WorkPackage (id/scope/deps/order/write_scope).
    Отчёт пакета привязывается к нему; при дрейфе определения старый отчёт не принимается за выполненный."""
    import hashlib, json as _j
    payload = _j.dumps({"id": pkg.get("id"), "scope": pkg.get("scope"),
                        "depends_on": sorted(pkg.get("depends_on") or []), "order": pkg.get("order"),
                        "write_scope": pkg.get("write_scope")}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _plan_hash(ordered):
    """Хэш всего SequencePlan = хэш последовательности хэшей пакетов (порядко-зависимый)."""
    import hashlib
    return hashlib.sha256("".join(_pkg_hash(p) for p in ordered).encode("utf-8")).hexdigest()[:16]


def _ordered(packages):
    return sorted(packages, key=lambda p: (p.get("order", 0), p.get("id", "")))


_SUPPORTED_PLAN_SCHEMA = 1


def _validate_sequence_plan_schema(doc, expected_wid=None):
    """v3.0.9/v3.0.10 (finding аудита P0/P1): ПОЛНАЯ integrity-валидация SequencePlan при КАЖДОМ чтении.
    Проверяем не только НАЛИЧИЕ полей, но и ЦЕЛОСТНОСТЬ: поддерживаемая schema_version, совпадение
    workitem_id (если задан expected_wid — иначе чужой план в каталоге WorkItem прошёл бы), уникальность
    package id и order, корректность depends_on (ссылки на существующие пакеты), отсутствие циклов,
    пересчёт КАЖДОГО pkg_hash и общего plan_hash. Любое расхождение -> lifecycle-corrupted.
    -> None (валиден) | причина."""
    if not isinstance(doc, dict):
        return "не dict"
    if doc.get("kind") != "SequencePlan":
        return f"kind != SequencePlan ({doc.get('kind')})"
    for k in ("schema_version", "workitem_id", "plan_hash", "base_ref", "sequence_base_sha", "packages"):
        if doc.get(k) in (None, ""):
            return f"нет обязательного поля '{k}'"
    if doc.get("schema_version") != _SUPPORTED_PLAN_SCHEMA:
        return f"schema_version {doc.get('schema_version')} не поддерживается (нужна {_SUPPORTED_PLAN_SCHEMA})"
    if expected_wid is not None and doc.get("workitem_id") != expected_wid:
        return (f"workitem_id плана '{doc.get('workitem_id')}' != текущего WorkItem '{expected_wid}' "
                "— чужой SequencePlan в каталоге")
    pkgs = doc.get("packages")
    if not isinstance(pkgs, list) or not pkgs:
        return "packages пуст/не список"
    ids, orders = [], []
    for i, p in enumerate(pkgs):
        if not isinstance(p, dict):
            return f"packages[{i}] не dict"
        for k in ("id", "pkg_hash", "order"):
            if p.get(k) in (None, ""):
                return f"packages[{i}] без '{k}'"
        if "depends_on" not in p:
            return f"packages[{i}] без depends_on"
        deps = p.get("depends_on")
        if not isinstance(deps, list):
            return f"packages[{i}] depends_on не список"
        ids.append(p["id"])
        orders.append(p["order"])
        # пересчёт pkg_hash из определения (id/scope/deps/order/write_scope) — дрейф определения ловится
        if _pkg_hash(p) != p.get("pkg_hash"):
            return f"packages[{i}] ('{p['id']}') pkg_hash не сходится с определением (подмена/дрейф)"
    if len(set(ids)) != len(ids):
        return f"дубли package id: {sorted({x for x in ids if ids.count(x) > 1})}"
    if len(set(orders)) != len(orders):
        return f"дубли order: {sorted({x for x in orders if orders.count(x) > 1})}"
    idset = set(ids)
    # depends_on обязаны ссылаться на существующие пакеты; самоссылка запрещена
    dep_map = {}
    for p in pkgs:
        for d in (p.get("depends_on") or []):
            if d == p["id"]:
                return f"пакет '{p['id']}' зависит от себя"
            if d not in idset:
                return f"пакет '{p['id']}' зависит от несуществующего '{d}'"
        dep_map[p["id"]] = list(p.get("depends_on") or [])
    # отсутствие циклов (обход в глубину с тремя состояниями)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {i: WHITE for i in idset}

    def _has_cycle(node, stack):
        color[node] = GRAY
        for nxt in dep_map.get(node, []):
            if color[nxt] == GRAY:
                return stack + [nxt]
            if color[nxt] == WHITE:
                r = _has_cycle(nxt, stack + [nxt])
                if r:
                    return r
        color[node] = BLACK
        return None

    for i in idset:
        if color[i] == WHITE:
            cyc = _has_cycle(i, [i])
            if cyc:
                return f"цикл зависимостей: {' -> '.join(cyc)}"
    # пересчёт общего plan_hash из упорядоченных пакетов
    recomputed = _plan_hash(_ordered(pkgs))
    if recomputed != doc.get("plan_hash"):
        return f"plan_hash не сходится с пакетами (сохранён {doc.get('plan_hash')}, пересчитан {recomputed})"
    return None
