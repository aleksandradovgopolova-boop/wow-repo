#!/usr/bin/env python3
"""Метрики эффекта по истории прогонов (v2.5) — слой «Метрики эффекта» из внешнего ревью.

Вход: .ai/project/report-history/ — срезы run_report --record. Формат — файл на ПРОГОН
(<feature>/<run-id>.jsonl, #148); старый плоский <feature>.jsonl тоже читается.
Считает ДЕТЕРМИНИРОВАННО, по накопленной истории, а не по впечатлению:
  - на фичу: число срезов, период, доля срезов с PROBLEM, последний вердикт/стадия,
    динамика покрытия (заполнено первый срез -> последний), days-in-flight
    (первый срез -> последний), продвижение по стадиям;
  - агрегат: фич/срезов всего, PROBLEM-rate, медиана days-in-flight фич,
    дошедших до retrospective.

Честность: фича с < {MIN_RUNS} срезов помечается insufficient-data и не искажает
агрегат; при < {MIN_FEATURES} фич с достаточной историей агрегат сопровождается
предупреждением (условие из memory: метрикам эффекта нужно 3-5 прогонов).

Использование:  effect_metrics.py [history-dir] [--json]   (default: .ai/project/report-history)
                effect_metrics.py --selftest
Возврат 0 всегда (отчёт — данные; решения за людьми/INSIGHTS), 1 — только при ошибке чтения.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from statistics import median

STAGES = ["discovery", "definition", "ux", "architecture", "delivery",
          "analytics", "documentation", "release", "monitoring", "adoption", "retrospective"]
MIN_RUNS = 3
MIN_FEATURES = 3


def load_history(hist_dir: Path):
    """Журналы срезов -> {фича: [срез, ...]} по возрастанию `ts`. Дубли строк снимаются.

    ПОЧЕМУ СНИМАЮТСЯ ДУБЛИ (18.08.2026, вместе с `.gitattributes merge=union`). Журналы объявлены
    append-only, и слияние им теперь сводит git сам — склейкой СТРОК. У склейки есть цена: одна и та
    же строка может прийти с двух сторон и остаться в файле дважды. Ниже `runs = len(entries)`, и
    `problem_rate` делит на это число — то есть дубль не «лишняя строка в журнале», а ИСКАЖЁННАЯ
    МЕТРИКА: повтор поднимает `runs`, может сам по себе перевести `sufficient` в true и сдвинуть
    долю проблем. Убрать ручной конфликт и молча испортить измерение было бы обменом одного дефекта
    на другой, который не видно.
    ДУБЛЬ — ЭТО ПОЛНОСТЬЮ СОВПАВШИЙ СРЕЗ: одна фича, одна секунда, тот же вердикт, стадия и
    покрытие. Два РАЗНЫХ прогона так совпасть не могут — у них отличается хотя бы одно поле.
    СНЯТОЕ НЕ ПРЯЧЕТСЯ: число снятых дублей возвращается в `load_history.dropped_duplicates` и
    попадает в отчёт `build`. Молчаливая чистка данных — тот же ложный green, только в измерении.
    """
    features = {}
    dropped = {}
    # Сырые строки собираются по фиче из ДВУХ источников (#148, шардирование по прогону):
    #   - шарды `report-history/<feature>/<run-id>.jsonl` — новый формат, файл на прогон:
    #     параллельные прогоны пишут в разные файлы, поэтому git-merge-конфликта нет;
    #   - плоский `report-history/<feature>.jsonl` — старый формат, ещё встречается в
    #     дочках; читаем и его, чтобы не потерять уже накопленную историю.
    # Дедуп ниже — по ФИЧЕ (через все её источники), а не по файлу: срез, пришедший с двух
    # сторон слияния плоского файла, всё так же снимается и НАЗЫВАЕТСЯ в `dropped`.
    raw: dict[str, list[str]] = {}
    for f in sorted(hist_dir.glob("*.jsonl")):
        raw.setdefault(f.stem, []).extend(f.read_text(encoding="utf-8").splitlines())
    for d in sorted(p for p in hist_dir.iterdir() if p.is_dir()) if hist_dir.is_dir() else []:
        for shard in sorted(d.glob("*.jsonl")):
            raw.setdefault(d.name, []).extend(shard.read_text(encoding="utf-8").splitlines())
    for fid, lines in sorted(raw.items()):
        entries, seen, dup = [], set(), 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            key = json.dumps(entry, sort_keys=True, ensure_ascii=False)
            if key in seen:
                dup += 1
                continue
            seen.add(key)
            entries.append(entry)
        if dup:
            dropped[fid] = dup
        if entries:
            features[fid] = sorted(entries, key=lambda e: e.get("ts", ""))
    load_history.dropped_duplicates = dropped
    return features


def feature_metrics(entries):
    first, last = entries[0], entries[-1]
    runs = len(entries)
    problem_rate = round(sum(1 for e in entries if e.get("verdict") == "PROBLEM") / runs, 2)
    try:
        t0 = datetime.fromisoformat(first["ts"])
        t1 = datetime.fromisoformat(last["ts"])
        days = round((t1 - t0).total_seconds() / 86400, 1)
    except (KeyError, ValueError):
        days = None
    def stage_idx(e):
        s = e.get("current_stage")
        return STAGES.index(s) if s in STAGES else None
    i0, i1 = stage_idx(first), stage_idx(last)
    return {
        "runs": runs,
        "sufficient": runs >= MIN_RUNS,
        "period_days": days,
        "problem_rate": problem_rate,
        "last_verdict": last.get("verdict"),
        "last_stage": last.get("current_stage"),
        "stages_advanced": (i1 - i0) if (i0 is not None and i1 is not None) else None,
        "coverage_filled_first_to_last": [
            (first.get("coverage") or {}).get("filled"),
            (last.get("coverage") or {}).get("filled")],
        "reached_retrospective": last.get("current_stage") == "retrospective",
    }


def build(hist_dir: Path):
    features = load_history(hist_dir)
    dropped = getattr(load_history, "dropped_duplicates", {}) or {}
    per_feature = {fid: feature_metrics(es) for fid, es in features.items()}
    sufficient = {f: m for f, m in per_feature.items() if m["sufficient"]}
    total_runs = sum(m["runs"] for m in per_feature.values())
    flights = [m["period_days"] for m in sufficient.values()
               if m["reached_retrospective"] and m["period_days"] is not None]
    agg = {
        "features": len(per_feature),
        "features_with_sufficient_history": len(sufficient),
        "total_runs": total_runs,
        "problem_rate": (round(sum(1 for es in features.values() for e in es
                                   if e.get("verdict") == "PROBLEM") / total_runs, 2)
                         if total_runs else None),
        "median_days_to_retrospective": (round(median(flights), 1) if flights else None),
        "baseline_ready": len(sufficient) >= MIN_FEATURES,
        # Снятые дубли НАЗВАНЫ, а не проглочены: 0 — это «дублей не было», а не «мы не смотрели».
        # Непустое значение читается как след слияния журналов (`merge=union`), а не как ошибка.
        "duplicate_slices_dropped": sum(dropped.values()),
    }
    return {"schema_version": 1, "kind": "effect-metrics-report",
            "history_dir": str(hist_dir), "per_feature": per_feature, "aggregate": agg,
            "duplicate_slices_dropped_per_feature": dropped}


def print_human(r):
    agg = r["aggregate"]
    print(f"=== Метрики эффекта ({r['history_dir']}) ===")
    for fid, m in r["per_feature"].items():
        note = "" if m["sufficient"] else f"  [insufficient-data: {m['runs']} < {MIN_RUNS} срезов]"
        print(f"  {fid}: срезов={m['runs']}, PROBLEM-rate={m['problem_rate']}, "
              f"последний={m['last_verdict']}@{m['last_stage']}, "
              f"период={m['period_days']}д, стадий пройдено={m['stages_advanced']}{note}")
    print(f"агрегат: фич={agg['features']} (с достаточной историей: "
          f"{agg['features_with_sufficient_history']}), срезов={agg['total_runs']}, "
          f"PROBLEM-rate={agg['problem_rate']}, "
          f"медиана до retrospective={agg['median_days_to_retrospective']}д")
    if not agg["baseline_ready"]:
        print(f"ВНИМАНИЕ: baseline не готов — нужно >= {MIN_FEATURES} фич с >= {MIN_RUNS} "
              "срезами; выводы по текущим числам преждевременны.")


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    hist_dir = Path(args[0]).resolve() if args else Path(".ai/project/report-history").resolve()
    if not hist_dir.exists():
        print(f"история не найдена: {hist_dir} — запускайте run_report с --record.")
        return 1
    r = build(hist_dir)
    if "--json" in argv:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print_human(r)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
