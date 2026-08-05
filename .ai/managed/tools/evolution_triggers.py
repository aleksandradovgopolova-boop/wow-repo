#!/usr/bin/env python3
"""evolution_triggers.py (v3.2.4) — замыкание governance-петли ADR ↔ Product Health.

ADR декларируют влияние на quality attributes (improves/tradeoff/degrades). Product Health
(tools/product_health.py) меряет РЕАЛЬНОСТЬ в продакшене. Триггер эволюции срабатывает, когда
реальность расходится с обещанием решения — сигнал пересмотреть ADR (advisory, НЕ gate):

  - promise_broken: метрика, отображающаяся на атрибут, деградирует (normalized < 0.7), а активный
    ADR обещал этот атрибут improve -> обещание не держится, ADR пора пересмотреть;
  - cost_realized: та же деградация, но ADR принимал атрибут как tradeoff/degrades -> принятая цена
    реализовалась -> проверить, приемлема ли она ещё.

Это SIGNAL для workflow пересмотра решений (evolution), не блокирующая проверка. Источник истины —
ADR-реестр (decisions/adr) + отчёт product_health (schemas/product-health.schema.json).

CLI:  evolution_triggers.py <product-health-report.json> [--adr decisions/adr] [-o out.json]
      evolution_triggers.py --selftest
Возврат 0 всегда при успешном расчёте (триггеры — данные; решение за людьми/workflow).
"""
import json
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / "validation"))
import validate_adr_registry as reg          # noqa: E402
import validate_quality_attributes as qa     # noqa: E402

DEGRADED = 0.7   # normalized ниже -> метрика деградирует (== порог findings в product_health)

# отображение метрик product_health на quality attributes ADR (ISO-25010-класс)
METRIC_TO_ATTR = {
    "reliability": "reliability",
    "errors": "reliability",
    "performance": "performance",
    "support_load": "usability",
    "adoption": "usability",
    "activation": "usability",
    "retention": "usability",
    "feature_usage": "usability",
}


def triggers(adrs: dict, health_report: dict) -> list:
    """Список evolution-триггеров из активных ADR + отчёта product_health."""
    active = {aid: d for aid, d in adrs.items() if d.get("status") == "accepted"}
    prof = qa.profile(active)
    out = []
    for mname, m in (health_report.get("metrics") or {}).items():
        norm = m.get("normalized")
        if not isinstance(norm, (int, float)) or norm >= DEGRADED:
            continue
        attr = METRIC_TO_ATTR.get(mname)
        effs = prof.get(attr)
        if not attr or not effs:
            continue
        if effs.get("improves"):
            out.append({"kind": "promise_broken", "attribute": attr, "metric": mname,
                        "normalized": round(float(norm), 3), "adrs": sorted(effs["improves"]),
                        "recommendation": f"пересмотреть {sorted(effs['improves'])}: обещали improve "
                                          f"'{attr}', но метрика '{mname}' деградирует (normalized={norm})"})
        costed = sorted(set(effs.get("tradeoff", []) + effs.get("degrades", [])))
        if costed:
            out.append({"kind": "cost_realized", "attribute": attr, "metric": mname,
                        "normalized": round(float(norm), 3), "adrs": costed,
                        "recommendation": f"пересмотреть {costed}: принятая цена по '{attr}' "
                                          f"реализовалась (метрика '{mname}' normalized={norm})"})
    return out


def report(adrs: dict, health_report: dict) -> dict:
    trg = triggers(adrs, health_report)
    return {"schema_version": 1, "kind": "evolution-triggers",
            "scope": health_report.get("scope"), "period": health_report.get("period"),
            "health_band": (health_report.get("health_score") or {}).get("band"),
            "trigger_count": len(trg), "triggers": trg}


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    def _adr(aid, qas):
        return {"id": aid, "status": "accepted", "quality_attributes": qas}

    adrs = {
        "ADR-A": _adr("ADR-A", [{"attribute": "reliability", "effect": "improves"}]),
        "ADR-B": _adr("ADR-B", [{"attribute": "performance", "effect": "tradeoff"}]),
        "ADR-OLD": {"id": "ADR-OLD", "status": "superseded",
                    "quality_attributes": [{"attribute": "reliability", "effect": "improves"}]},
    }

    def _hr(metrics, band="warning"):
        return {"scope": "s", "period": "p", "health_score": {"band": band},
                "metrics": {k: {"normalized": v} for k, v in metrics.items()}}

    # reliability деградирует, ADR-A обещал improve -> promise_broken на ADR-A
    t = triggers(adrs, _hr({"reliability": 0.5, "performance": 0.9}))
    expect("promise_broken при деградации обещанного improve",
           any(x["kind"] == "promise_broken" and x["adrs"] == ["ADR-A"] for x in t))
    expect("нет cost_realized когда performance здоров",
           not any(x["kind"] == "cost_realized" for x in t))

    # performance деградирует, ADR-B принимал tradeoff -> cost_realized на ADR-B
    t = triggers(adrs, _hr({"performance": 0.4}))
    expect("cost_realized при деградации tradeoff-атрибута",
           any(x["kind"] == "cost_realized" and x["adrs"] == ["ADR-B"] for x in t))

    # маппинг метрики errors -> атрибут reliability
    t = triggers(adrs, _hr({"errors": 0.3}))
    expect("метрика errors отображается на reliability -> promise_broken ADR-A",
           any(x["attribute"] == "reliability" and x["metric"] == "errors" for x in t))

    # здоровые метрики -> нет триггеров
    expect("всё здорово -> нет триггеров",
           triggers(adrs, _hr({"reliability": 0.95, "performance": 0.9}, band="healthy")) == [])

    # superseded ADR не порождает триггеров (только активные)
    t = triggers({"ADR-OLD": adrs["ADR-OLD"]}, _hr({"reliability": 0.2}))
    expect("superseded ADR не порождает evolution-триггеров", t == [])

    # интеграция с РЕАЛЬНЫМ реестром + здоровым демо-health -> петля замыкается без триггеров
    real_errs, real_adrs = reg.check_registry(reg.DEFAULT_DIR)
    expect("реальный ADR-реестр целостен (предусловие)", real_errs == [])
    healthy = _hr({"reliability": 0.95, "performance": 0.95, "errors": 0.95}, band="healthy")
    expect("реальные ADR + здоровый health -> 0 триггеров (петля замыкается чисто)",
           triggers(real_adrs, healthy) == [])
    # а при деградации reliability реальные ADR-002/003 (improve reliability) дают promise_broken
    degraded = _hr({"reliability": 0.4}, band="critical")
    rt = triggers(real_adrs, degraded)
    expect("деградация reliability -> promise_broken на реальных ADR (002/003 обещали improve)",
           any(x["kind"] == "promise_broken" and "ADR-002" in x["adrs"] for x in rt))

    print("evolution_triggers selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("-")]
    if not args:
        print(__doc__)
        return 1
    adr_dir = reg.DEFAULT_DIR
    if "--adr" in argv:
        adr_dir = Path(argv[argv.index("--adr") + 1])
    reg_errs, adrs = reg.check_registry(adr_dir)
    if reg_errs:
        print("EVOLUTION: сначала почините реестр ADR:")
        for x in reg_errs:
            print(f"  - {x}")
        return 1
    health = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    rep = report(adrs, health)
    out = None
    if "-o" in argv:
        out = argv[argv.index("-o") + 1]
    text = json.dumps(rep, ensure_ascii=False, indent=2)
    if out:
        Path(out).write_text(text, encoding="utf-8")
        print(f"evolution-triggers -> {out}")
    print(f"EVOLUTION: {rep['trigger_count']} триггер(ов) (band={rep['health_band']})")
    for t in rep["triggers"]:
        print(f"  - [{t['kind']}] {t['attribute']} <- {t['metric']} (norm={t['normalized']}): {t['adrs']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
