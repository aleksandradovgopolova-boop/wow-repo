#!/usr/bin/env python3
"""Четыре управляющих объекта продукта: проверка СУЩЕСТВА, а не наличия разделов (2026-08-14).

ПОВОД. Кит уже умеет потребовать Problem Statement, JTBD, гипотезы и требования — и в этом же его
слабость: он проверяет, что раздел ЗАПОЛНЕН. Ровно из этой разницы вырос B2-14: `spec-coverage`
сообщал `acceptance_criteria: complete`, а критерий не был выполнен, потому что `complete` там
означает «раздел заполнен», а не «требование выполнено». Продуктовый слой рискует повторить это в
большем масштабе: хорошо оформленный, но слабый продуктовый пакет.

ЧЕТЫРЕ ОБЪЕКТА ВМЕСТО НОВЫХ ДОКУМЕНТОВ (решение владельца `ep-2026-08-14-product-os`). Существующие
шаблоны (`templates/discovery/*`, `templates/product/*`, `templates/analytics/*`) остаются входом и
никуда не деваются — они сворачиваются в четыре машиночитаемых объекта, у которых есть контракт:

  OpportunityBrief      — какую проблему решаем, чем это подтверждено и чего мы НЕ знаем;
  ProductDecisionRecord — какие варианты рассматривали, что выбрано, чего сознательно НЕ делаем;
  OutcomeContract       — baseline, target, guardrails и правило решения ДО работы;
  OutcomeReadout        — что произошло на самом деле и какое знание вернулось в discovery.

ЧТО ИМЕННО ПРОВЕРЯЕТСЯ (и почему именно это):
  * утверждение о продукте обязано НАЗЫВАТЬ основание. Нет доказательства — это законно, но тогда
    обязан быть назван ПРОБЕЛ (`evidence_gap`). «Не знаю» и «не сказал» — разные состояния, и
    первое полезно, второе опасно;
  * решение с ОДНИМ вариантом — не решение, а оформление уже принятого. Требуется ≥2 варианта с
    плюсами и минусами, названный выбор владельца и `not_doing`;
  * `revisit_when` обязателен: решение без условия пересмотра нельзя ни подтвердить, ни отменить;
  * baseline без даты и источника — не baseline, а число из головы;
  * readout без контракта — рассказ без базы сравнения, поэтому связь обязательна, а guardrails
    контракта обязаны быть отчитаны все до одного (`cross_check`).

Использование:  validate_product_objects.py <файл.yaml> [--against contract.yaml] [--json]
Возврат 0 — валиден, 1 — ошибки.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

KINDS = ("OpportunityBrief", "ProductDecisionRecord", "OutcomeContract", "OutcomeReadout")
CONFIDENCE = ("low", "medium", "high")
HYPOTHESIS = ("confirmed", "refuted", "inconclusive")
TARGET_MET = ("yes", "no", "unknown")
# Вердикт по РЕАЛЬНОМУ замеру (evaluate_outcome), в отличие от объявленного человеком target_met.
MEASURED_VERDICT = ("met", "failed", "unknown")


def _text(v) -> str:
    return str(v or "").strip()


def _need(data, keys, errors, where):
    for k in keys:
        if not _text(data.get(k)):
            errors.append(f"{where}: нет {k}")


def _check_brief(d, e):
    w = "OpportunityBrief"
    _need(d, ("user", "situation", "problem", "desired_outcome", "why_now"), e, w)
    ev = d.get("evidence")
    if ev is None:
        e.append(f"{w}: нет evidence — даже пустой список объявляется явно, вместе с `evidence_gap`")
    elif not isinstance(ev, list):
        e.append(f"{w}: evidence должен быть списком")
    else:
        for i, item in enumerate(ev, 1):
            if not isinstance(item, dict) or not _text(item.get("claim")):
                e.append(f"{w}: evidence[{i}] без claim"); continue
            if not _text(item.get("source")):
                e.append(f"{w}: evidence[{i}] «{_text(item['claim'])[:40]}…» без source — "
                         f"утверждение о продукте обязано называть основание")
        if not ev and not _text(d.get("evidence_gap")):
            e.append(f"{w}: доказательств нет и `evidence_gap` не назван — «не знаю» и «не сказал» "
                     f"это разные состояния, и второе опаснее")
    for k in ("unknowns", "assumptions"):
        if not isinstance(d.get(k), list):
            e.append(f"{w}: {k} должен быть списком (пустой — законно, отсутствующий — нет)")


def _check_decision(d, e):
    w = "ProductDecisionRecord"
    _need(d, ("question", "opportunity", "owner_decision", "not_doing", "revisit_when"), e, w)
    if d.get("confidence") not in CONFIDENCE:
        e.append(f"{w}: confidence '{d.get('confidence')}' не в {list(CONFIDENCE)}")
    opts = d.get("options")
    if not isinstance(opts, list) or len(opts) < 2:
        e.append(f"{w}: вариантов {len(opts) if isinstance(opts, list) else 0} — решение с одним "
                 f"вариантом это не решение, а оформление уже принятого")
        opts = opts if isinstance(opts, list) else []
    ids = []
    for i, o in enumerate(opts, 1):
        if not isinstance(o, dict) or not _text(o.get("id")):
            e.append(f"{w}: options[{i}] без id"); continue
        ids.append(_text(o["id"]))
        for k in ("pros", "cons"):
            if not (isinstance(o.get(k), list) and o[k]):
                e.append(f"{w}: вариант '{o['id']}' без {k} — вариант без минусов не рассматривали, "
                         f"а описывали")
    for k in ("recommendation", "owner_decision"):
        v = _text(d.get(k))
        if v and ids and v not in ids and v != "deferred":
            e.append(f"{w}: {k} '{v}' не соответствует ни одному варианту ({', '.join(ids)})")
    if _text(d.get("owner_decision")) == "deferred" and not _text(d.get("deferred_reason")):
        e.append(f"{w}: решение отложено без причины — отложенное решение тоже решение и требует "
                 f"названного основания")


def _check_contract(d, e):
    w = "OutcomeContract"
    _need(d, ("decision", "evaluation_period"), e, w)
    pm = d.get("primary_metric")
    if not isinstance(pm, dict) or not _text(pm.get("name")) or not _text(pm.get("source")):
        e.append(f"{w}: primary_metric требует name и source — метрика без источника не считается")
    base = d.get("baseline")
    if not isinstance(base, dict):
        e.append(f"{w}: нет baseline")
    else:
        for k in ("value", "measured_at", "source"):
            if base.get(k) in (None, ""):
                e.append(f"{w}: baseline без {k} — база без даты и источника это число из головы")
    tgt = d.get("target")
    if not isinstance(tgt, dict) or tgt.get("value") in (None, "") or not _text(tgt.get("by")):
        e.append(f"{w}: target требует value и by (к какому сроку)")
    gr = d.get("guardrails")
    if not isinstance(gr, list) or not gr:
        e.append(f"{w}: нет guardrails — без них «цель достигнута» может означать «сломали соседнее»")
    else:
        for i, g in enumerate(gr, 1):
            if not isinstance(g, dict) or not _text(g.get("name")):
                e.append(f"{w}: guardrails[{i}] без name"); continue
            if not any(_text(g.get(k)) for k in ("must_not_exceed", "must_not_drop_below")):
                e.append(f"{w}: guardrail '{g['name']}' без порога — что именно нельзя ухудшить?")
    if not (isinstance(d.get("events"), list) and d["events"]):
        e.append(f"{w}: нет events — нечем мерить: метрика объявлена, а сигнала нет")
    rules = d.get("decision_rules")
    if not isinstance(rules, dict) or not all(_text(rules.get(k)) for k in ("continue", "change", "stop")):
        e.append(f"{w}: decision_rules требует continue/change/stop — правило решения принимается ДО "
                 f"работы, иначе результат всегда толкуется в пользу сделанного")


def _check_readout(d, e):
    w = "OutcomeReadout"
    _need(d, ("contract", "next_decision", "back_to_discovery"), e, w)
    if d.get("target_met") not in TARGET_MET:
        e.append(f"{w}: target_met '{d.get('target_met')}' не в {list(TARGET_MET)}")
    elif d.get("target_met") == "unknown" and not _text(d.get("unknown_reason")):
        e.append(f"{w}: target_met=unknown без причины — неизмеренное обязано называть, почему")
    if d.get("hypothesis") not in HYPOTHESIS:
        e.append(f"{w}: hypothesis '{d.get('hypothesis')}' не в {list(HYPOTHESIS)}")
    m = d.get("measured")
    if not isinstance(m, dict) or m.get("value") in (None, "") or not _text(m.get("measured_at")):
        e.append(f"{w}: measured требует value и measured_at")
    for k in ("guardrails_observed", "unexpected_effects"):
        if not isinstance(d.get(k), list):
            e.append(f"{w}: {k} должен быть списком (пустой — законно, отсутствующий — нет)")


_CHECKERS = {"OpportunityBrief": _check_brief, "ProductDecisionRecord": _check_decision,
             "OutcomeContract": _check_contract, "OutcomeReadout": _check_readout}


def check(data: dict) -> list:
    """Ошибки объекта. Вид определяется полем `kind`; чужой артефакт отвергается."""
    if not isinstance(data, dict):
        return ["артефакт не является объектом"]
    errors = []
    if data.get("schema_version") is None:
        errors.append("нет schema_version")
    kind = data.get("kind")
    if kind not in KINDS:
        return errors + [f"kind '{kind}' не в {list(KINDS)}"]
    _CHECKERS[kind](data, errors)
    return errors


def cross_check(contract: dict, readout: dict) -> list:
    """Сверка readout с его контрактом. Отчёт обязан закрыть ВСЁ, что контракт объявил заранее.

    Без этой сверки readout честно заполняется по удобным метрикам: отчитались по цели, промолчали
    про guardrail, который просел. Тот же класс, что «раздел заполнен» вместо «критерий выполнен».
    """
    errors = []
    cname = _text((contract.get("primary_metric") or {}).get("name"))
    rname = _text((readout.get("measured") or {}).get("metric") or cname)
    if cname and rname and cname != rname:
        errors.append(f"readout измеряет '{rname}', а контракт объявлял '{cname}' — подмена метрики")
    declared = {_text(g.get("name")) for g in (contract.get("guardrails") or [])
                if isinstance(g, dict) and _text(g.get("name"))}
    reported = {_text(g.get("name")) for g in (readout.get("guardrails_observed") or [])
                if isinstance(g, dict) and _text(g.get("name"))}
    missing = sorted(declared - reported)
    if missing:
        errors.append(f"guardrails не отчитаны: {', '.join(missing)} — умолчание о том, что было "
                      f"объявлено заранее, читается как «всё в порядке»")
    return errors


def _num(v):
    """Извлечь число из значения: int/float как есть; из строки — первое число ('86 сек' -> 86.0).

    Единицы у guardrail-порогов пишутся текстом ('90 сек'), поэтому парсим первое число, а не
    требуем чистый numeric. bool числом НЕ считается. -> float | None (не число — None, не ноль)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = _text(v)
    m = re.search(r"-?\d+(?:[.,]\d+)?", s) if s else None
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def _direction(baseline: float, target: float) -> str:
    """Направление улучшения по знаку target-baseline. -> 'increase' | 'decrease' | 'hold'."""
    if target > baseline:
        return "increase"
    if target < baseline:
        return "decrease"
    return "hold"


def _primary_met(direction: str, target: float, measured: float) -> bool:
    """Дотянула ли основная метрика до цели С УЧЁТОМ направления улучшения."""
    if direction == "increase":
        return measured >= target
    if direction == "decrease":
        return measured <= target
    return measured == target


def _guardrail_breaches(contract: dict, readout: dict) -> list:
    """Список пробитых guardrail'ов по observed-значениям отчёта.

    within=False в отчёте — прямой сигнал пробоя (сильнее нашего парсинга единиц). Если within не
    проставлен — сверяем численно против порога контракта (`must_not_exceed`/`must_not_drop_below`),
    но только когда обе стороны парсятся как числа; иначе тихо пропускаем (не выдумываем пробой)."""
    declared = {_text(g.get("name")): g for g in (contract.get("guardrails") or [])
                if isinstance(g, dict) and _text(g.get("name"))}
    breaches: list[str] = []
    for obs in (readout.get("guardrails_observed") or []):
        if not isinstance(obs, dict):
            continue
        name = _text(obs.get("name"))
        if not name:
            continue
        if obs.get("within") is False:
            breaches.append(name)
            continue
        if obs.get("within") is True:
            continue
        decl = declared.get(name)
        ov = _num(obs.get("value"))
        if not decl or ov is None:
            continue
        hi, lo = _num(decl.get("must_not_exceed")), _num(decl.get("must_not_drop_below"))
        if hi is not None and ov > hi:
            breaches.append(name)
        elif lo is not None and ov < lo:
            breaches.append(name)
    return breaches


def evaluate_outcome(contract: dict | None, readout: dict | None) -> dict:
    """ФЛИП вердикта по итогу из `unknown` в `met`/`failed` по РЕАЛЬНОМУ замеру baseline→после релиза.

    Ключевое отличие от `project_outcome` (#566): здесь вердикт СЧИТАЕТСЯ ИЗ ЧИСЕЛ (baseline →
    measured против target + guardrails), а НЕ берётся из человеческого поля `target_met`. Декларация
    «мы считаем, что цель достигнута» и измерение «метрика с 42% ушла на 41%» — разные вещи; петля
    итога обязана опираться на второе. Пока замера нет — вердикт честно `unknown`, ровно как у
    contract.baseline «без даты и источника это число из головы».

    Реальный замер = число + дата (`measured.value`, `measured.measured_at`). Без даты замер не
    считается измерением и вердикт остаётся `unknown` — это и есть барьер против «числа из головы»,
    который иначе флипнул бы итог по выдуманным данным.

    Правила (в порядке силы): guardrail пробит -> `failed` (сломали соседнее, даже если основная
    метрика дотянула); основная метрика достигла цели -> `met`; не достигла -> `failed`.

    -> {"verdict": met|failed|unknown, "reason", "measured", "measured_at", "baseline", "target",
        "direction", "primary_met": bool|None, "guardrail_breaches": [...],
        "is_real_measurement": bool}.
    """
    contract = contract or {}
    baseline = _num((contract.get("baseline") or {}).get("value"))
    target = _num((contract.get("target") or {}).get("value"))
    result = {"verdict": "unknown", "reason": None, "measured": None, "measured_at": None,
              "baseline": baseline, "target": target, "direction": None,
              "primary_met": None, "guardrail_breaches": [], "is_real_measurement": False}

    if not isinstance(readout, dict):
        result["reason"] = "замера нет — итог по релизу ещё не измерен, флипать нечем"
        return result

    measured_block = readout.get("measured") or {}
    measured = _num(measured_block.get("value"))
    measured_at = _text(measured_block.get("measured_at"))
    result["measured"], result["measured_at"] = measured, measured_at or None

    if measured is None or not measured_at:
        result["reason"] = ("замер без значения или без даты — это ещё не измерение "
                            "(как baseline без даты: число из головы итог не флипает)")
        return result
    if baseline is None or target is None:
        result["reason"] = "в контракте нет числового baseline/target — замер сравнить не с чем"
        return result

    result["is_real_measurement"] = True
    direction = _direction(baseline, target)
    result["direction"] = direction
    primary_met = _primary_met(direction, target, measured)
    result["primary_met"] = primary_met
    breaches = _guardrail_breaches(contract, readout)
    result["guardrail_breaches"] = breaches

    if breaches:
        result["verdict"] = "failed"
        result["reason"] = (f"защитная метрика просела: {', '.join(breaches)} — цель не считается "
                            f"достигнутой, даже если основная метрика дотянула")
    elif primary_met:
        result["verdict"] = "met"
        result["reason"] = (f"основная метрика достигла цели ({measured} к цели {target} "
                            f"от baseline {baseline}), защитные метрики удержаны")
    else:
        result["verdict"] = "failed"
        result["reason"] = (f"основная метрика не достигла цели ({measured} к цели {target} "
                            f"от baseline {baseline})")
    return result


def _slug(value: str) -> str:
    """Приводит строку к id узла графа (`^[a-z0-9][a-z0-9-]*$`)."""
    out = []
    for ch in _text(value).lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    s = "".join(out).strip("-")
    return s or "outcome"


def project_outcome(contract: dict, readout: dict | None = None, *,
                    outcome_id: str | None = None, feature: str | None = None) -> dict:
    """Проецирует OutcomeContract (+ опциональный OutcomeReadout) в фрагмент Knowledge Graph.

    Возвращает `{"nodes": [...], "edges": [...]}` в формате knowledge-graph.schema.json: outcome —
    узел первого класса, а не документ сбоку. Атрибуты узла зеркалят контракт и отчёт (baseline,
    target, guardrails, measured, verdict) — это ПРОЕКЦИЯ существующих артефактов, не новое хранилище.

    `verdict` берётся из readout.target_met, а без отчёта честно помечается `pending` (не «unknown»
    и не «met»): результат ещё не измерен — это состояние, а не оценка.
    """
    contract = contract or {}
    metric = (contract.get("primary_metric") or {})
    oid = _slug(outcome_id or metric.get("name") or contract.get("decision") or "outcome")
    node = {
        "id": oid,
        "type": "outcome",
        "title": _text(metric.get("name")) or oid,
        "ref": _text(contract.get("decision")) or None,
        "baseline": (contract.get("baseline") or {}).get("value"),
        "target": (contract.get("target") or {}).get("value"),
        "guardrails": [_text(g.get("name")) for g in (contract.get("guardrails") or [])
                       if isinstance(g, dict) and _text(g.get("name"))],
        "measured": None,
        "verdict": "pending",
    }
    if readout:
        node["measured"] = (readout.get("measured") or {}).get("value")
        node["verdict"] = _text(readout.get("target_met")) or "pending"
    node = {k: v for k, v in node.items() if v is not None}

    fragment = {"nodes": [node], "edges": []}
    if _text(feature):
        fragment["edges"].append({"from": _text(feature), "type": "targets", "to": oid})
    return fragment


def trace_feature_rationale(graph: dict, feature_id: str) -> dict:
    """«Зачем существует эта функция»: цепочка goal → … → feature → outcome по рёбрам графа.

    Идёт ВВЕРХ по рёбрам `contains` (родитель функции — вплоть до цели) и ВПЕРЁД по ребру `targets`
    к outcome. Возвращает честную цепочку: если данных на полный путь нет, отдаётся лучший доступный
    фрагмент с НАЗВАННЫМИ пробелами (`gaps`), а не выдуманными связями.

    Результат: `{"feature", "chain": [{id,type,title}], "outcome": {...}|None, "gaps": [...]}`.
    chain упорядочена сверху вниз (цель первой), заканчивается запрошенной функцией.
    """
    nodes = {n.get("id"): n for n in (graph.get("nodes") or []) if isinstance(n, dict) and n.get("id")}
    edges = [e for e in (graph.get("edges") or []) if isinstance(e, dict)]
    gaps: list[str] = []

    def summary(nid: str) -> dict:
        n = nodes.get(nid, {})
        return {"id": nid, "type": _text(n.get("type")) or "?", "title": _text(n.get("title")) or nid}

    if feature_id not in nodes:
        return {"feature": feature_id, "chain": [], "outcome": None,
                "gaps": [f"узел '{feature_id}' отсутствует в графе — цепочку строить не от чего"]}

    # Вверх по contains до цели.
    chain_ids = [feature_id]
    current, seen = feature_id, {feature_id}
    while nodes.get(current, {}).get("type") != "goal":
        parents = [e["from"] for e in edges if e.get("type") == "contains"
                   and e.get("to") == current and e.get("from") in nodes]
        parent = next((p for p in parents if p not in seen), None)
        if parent is None:
            gaps.append(f"нет родителя выше '{current}' — путь до цели (goal) неполон")
            break
        seen.add(parent)
        chain_ids.insert(0, parent)
        current = parent

    # Вперёд к outcome.
    outcome_ids = [e["to"] for e in edges if e.get("type") == "targets"
                   and e.get("from") == feature_id and e.get("to") in nodes]
    outcome = None
    if outcome_ids:
        onode = nodes[outcome_ids[0]]
        outcome = {"id": outcome_ids[0], "verdict": _text(onode.get("verdict")) or "pending",
                   "target": onode.get("target"), "measured": onode.get("measured"),
                   "title": _text(onode.get("title")) or outcome_ids[0]}
    else:
        gaps.append(f"у '{feature_id}' нет outcome (ребро targets) — зачем функция существует, "
                    f"не подтверждается измеримым результатом")

    return {"feature": feature_id, "chain": [summary(i) for i in chain_ids],
            "outcome": outcome, "gaps": gaps}


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    data = yaml.safe_load(Path(args[0]).read_text(encoding="utf-8")) or {}
    errors = check(data)
    against = next((a.split("=", 1)[1] for a in argv if a.startswith("--against=")), None)
    if against:
        other = yaml.safe_load(Path(against).read_text(encoding="utf-8")) or {}
        pair = ((other, data) if data.get("kind") == "OutcomeReadout" else (data, other))
        errors += cross_check(*pair)
    if "--json" in argv:
        print(json.dumps({"errors": errors}, ensure_ascii=False, indent=2))
    elif errors:
        print("PRODUCT-OBJECT: ошибки:")
        for e in errors:
            print(f"  - {e}")
    else:
        print(f"PRODUCT-OBJECT-OK: {data.get('kind')} валиден.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
