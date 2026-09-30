"""Резолвер настроек-как-намерений и честность реестра (#644, вторая половина #632).

Настройки задаются intent-level ВЫБОРОМ по оси (registry/capability-policy.yaml), а не набором
внутренних флагов: человек выбирает намерение, кит раскладывает его на внутренние флаги. Семь осей
(communication/autonomy/watch/quality/team/design/cost).

Честность (инвариант деклараций): у каждого выбора `status ∈ implemented|planned`; planned-выбор
резолвер НЕ выдаёт за готовый (value=None), а `default` обязан быть implemented. Логика живёт в
`checks` (слой primitives): зависит только от stdlib+pyyaml, вызыватели импортируют её вниз (вход
validate_capability_policy, а позже — CLI-поверхность). Тот же приём, что у feature_decision.
"""
from __future__ import annotations

from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
           Path(__file__).resolve().parents[2])
POLICY = PKG / "registry" / "capability-policy.yaml"

STATUSES = ("implemented", "planned")


def load_policy(path=None) -> dict:
    """Прочитать реестр осей. path — для тестов; по умолчанию registry/capability-policy.yaml кита."""
    p = Path(path) if path else POLICY
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def resolve(axis: str, choice: str, policy=None) -> dict:
    """Намерение (ось+выбор) -> внутреннее разрешение. planned НЕ выдаётся за готовое.

    -> {axis, choice, status, resolves_to, value, note}. value = выбор ТОЛЬКО для implemented,
    иначе None (planned/unknown не притворяются включённым флагом). Неизвестная ось/выбор ->
    status: unknown (честный отказ, не молчаливое «ок»).
    """
    pol = policy or load_policy()
    a = (pol.get("axes") or {}).get(axis)
    if not isinstance(a, dict):
        return {"axis": axis, "choice": choice, "status": "unknown", "resolves_to": None,
                "value": None, "note": f"ось '{axis}' не объявлена в capability-policy"}
    c = (a.get("choices") or {}).get(choice)
    if not isinstance(c, dict):
        return {"axis": axis, "choice": choice, "status": "unknown", "resolves_to": a.get("resolves_to"),
                "value": None, "note": f"выбор '{choice}' не объявлен для оси '{axis}'"}
    status = c.get("status")
    return {"axis": axis, "choice": choice, "status": status, "resolves_to": a.get("resolves_to"),
            "value": (choice if status == "implemented" else None), "note": c.get("note")}


def resolve_all(chosen=None, policy=None) -> dict:
    """Разложить выбор человека по всем осям на внутренние разрешения. Ось без выбора -> её default.

    chosen: {axis: choice} (например, из .ai-ops.yaml). -> {axis: resolution}. Готовые оси несут
    value; planned — статус без value, чтобы вызыватель не принял непостроенное за включённое.
    """
    pol = policy or load_policy()
    chosen = chosen or {}
    return {axis: resolve(axis, chosen.get(axis, a.get("default")), pol)
            for axis, a in (pol.get("axes") or {}).items()}


def honesty_errors(policy=None, pkg=PKG) -> list[str]:
    """Честность реестра: оси форма-валидны, статусы из словаря, default implemented, mechanism есть.

    Пустой список = реестр честен. Ключевая проверка — `default` обязан быть implemented-выбором:
    по умолчанию нельзя включать непостроенный переключатель; и mechanism-файл обязан резолвиться,
    иначе ось объявляет механизм, которого нет.
    """
    pol = policy or load_policy()
    axes = pol.get("axes") or {}
    if not axes:
        return ["capability-policy: axes пусты — ни одной оси"]
    errs: list[str] = []
    for axis, a in axes.items():
        if not isinstance(a, dict):
            errs.append(f"{axis}: ось не mapping"); continue
        if not a.get("resolves_to"):
            errs.append(f"{axis}: нет resolves_to (внутренний флаг не назван)")
        choices = a.get("choices") or {}
        if not choices:
            errs.append(f"{axis}: нет choices"); continue
        for name, c in choices.items():
            st = c.get("status") if isinstance(c, dict) else None
            if st not in STATUSES:
                errs.append(f"{axis}.{name}: status='{st}' вне {list(STATUSES)}")
        default = a.get("default")
        dc = choices.get(default)
        if not isinstance(dc, dict):
            errs.append(f"{axis}: default '{default}' не среди choices")
        elif dc.get("status") != "implemented":
            errs.append(f"{axis}: default '{default}' status={dc.get('status')} — по умолчанию "
                        "нельзя включать непостроенный выбор (нужен implemented)")
        mech = a.get("mechanism")
        if not mech:
            errs.append(f"{axis}: не назван mechanism")
        elif not (Path(pkg) / mech).exists():
            errs.append(f"{axis}: mechanism '{mech}' не резолвится (файла нет)")
    return errs
