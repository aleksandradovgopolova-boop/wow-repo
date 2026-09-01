"""Чистая проверка формы plan-artifact. Вынесена из `validation/validate_plan_artifact.py` вниз
(лента №5), чтобы движок (pipeline_helpers) звал её ВНИЗ, без восходящего ребра engine -> validation.

Форма: work_packages — непустой список {id (строка, уникальный), summary, depends_on (список id,
может быть пустым; ссылки резолвятся)}; write_scope — непустой список путей. Только stdlib.
"""
from __future__ import annotations

REQUIRED_EVIDENCE = ["work_packages", "dependencies", "write_scope"]


def check(data):
    errors = []
    if not isinstance(data, dict) or data.get("kind") != "plan-artifact":
        errors.append("kind должен быть 'plan-artifact'")
        data = data if isinstance(data, dict) else {}
    if data.get("schema_version") is None:
        errors.append("нет schema_version")
    wps = data.get("work_packages")
    if not isinstance(wps, list) or not wps:
        errors.append("work_packages должен быть непустым списком")
        wps = []
    seen = set()
    for i, wp in enumerate(wps):
        if not isinstance(wp, dict):
            errors.append(f"work_package[{i}] должен быть объектом"); continue
        wid = wp.get("id", f"#{i}")
        _wid = wp.get("id")
        if not _wid:
            errors.append(f"work_package[{i}]: нет id")
        elif not isinstance(_wid, str):
            # author вернул id не-строкой (напр. dict) -> ЧЕСТНАЯ ошибка валидации, не краш set-membership
            errors.append(f"work_package[{i}]: id должен быть строкой, получено {type(_wid).__name__}")
        elif _wid in seen:
            errors.append(f"дублирующийся id work_package: {_wid}")
        else:
            seen.add(_wid)
        if not (isinstance(wp.get("summary"), str) and wp["summary"].strip()):
            errors.append(f"{wid}: пустой/отсутствующий summary")
        # dependencies: поле depends_on обязано присутствовать и быть списком (может быть пустым);
        # каждая зависимость должна ссылаться на существующий work_package.
        dep = wp.get("depends_on")
        if not isinstance(dep, list):
            errors.append(f"{wid}: depends_on должен быть списком (может быть пустым)")
    # проверка ссылочной целостности зависимостей (после сбора всех id) — только строковые id хешируемы
    ids = {wp.get("id") for wp in wps if isinstance(wp, dict) and isinstance(wp.get("id"), str)}
    for wp in wps:
        if isinstance(wp, dict):
            for d in wp.get("depends_on", []) or []:
                if not isinstance(d, str):
                    # author вернул зависимость не-строкой (напр. dict) -> ошибка валидации, не краш `in set`
                    errors.append(f"{wp.get('id')}: элемент depends_on должен быть строкой-id, получено {type(d).__name__}")
                elif d not in ids:
                    errors.append(f"{wp.get('id')}: depends_on ссылается на несуществующий work_package '{d}'")
    ws = data.get("write_scope")
    if not (isinstance(ws, list) and ws and all(isinstance(p, str) and p.strip() for p in ws)):
        errors.append("write_scope должен быть непустым списком путей")
    return errors


def provided_evidence(data):
    return list(REQUIRED_EVIDENCE) if not check(data) else []
