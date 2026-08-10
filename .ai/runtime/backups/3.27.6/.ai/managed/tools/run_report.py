#!/usr/bin/env python3
"""Оценка прогона фичи (v2.2) — «хорошо это или плохо» одной командой.

Собирает по каталогу функции честный отчёт:
  1. валидность blueprint (validate_feature_blueprint);
  2. покрытие стадий: заполнено / draft / planned / declined (с причинами) / файла нет;
  3. скелеты-пустышки: артефакт помечен done/draft, но содержимое не менялось
     после генерации (.generation.json);
  4. сверка с knowledge graph: если у feature-узла есть ребро delivered-by (релиз),
     а current_stage раньше release — реальность обогнала blueprint;
  5. retrospective и memory: закрыт ли цикл уроков.

Вердикт: PROBLEM-находки -> exit 1 (процесс не пройден честно), WARN — сигналы
для внимания, OK — прогон чист по форме. Качество СОДЕРЖАНИЯ артефактов оценивают
ревьюеры (gates), не скрипт.

Использование:  run_report.py <feature-dir> [--graph <graph.yaml>] [--json]
                           [--record [dir]]   — дописать срез отчёта в историю
                                                (по умолчанию <child>/.ai/project/report-history/)
                run_report.py --selftest
История (JSONL, по файлу на фичу) коммитится с PR и служит сырьём для
tools/effect_metrics.py («метрики эффекта»). Требует pyyaml.
"""

# PEP 563: ленивые аннотации — `Path | None` (PEP 604) не вычисляется при импорте,
# поэтому модуль грузится и на Python 3.9 (дефолт macOS CommandLineTools).
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
STAGES = ["discovery", "definition", "ux", "architecture", "delivery",
          "analytics", "documentation", "release", "monitoring", "adoption", "retrospective"]

_spec = importlib.util.spec_from_file_location(
    "vfb", PKG / "validation" / "validate_feature_blueprint.py")
vfb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vfb)

_ga_spec = importlib.util.spec_from_file_location("ga", PKG / "tools" / "generate_artifacts.py")
ga = importlib.util.module_from_spec(_ga_spec)
_ga_spec.loader.exec_module(ga)

_ca_spec = importlib.util.spec_from_file_location(
    "vca", PKG / "validation" / "validate_cross_artifacts.py")
vca = importlib.util.module_from_spec(_ca_spec)
_ca_spec.loader.exec_module(vca)


def graph_findings(feature_dir: Path, graph_path: Path, current_stage: str):
    """Реальность vs blueprint: релиз в графе при ранней стадии blueprint."""
    findings = []
    try:
        g = yaml.safe_load(graph_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [("WARN", f"knowledge graph не читается: {exc}")]
    nodes = {n["id"]: n for n in g.get("nodes") or [] if isinstance(n, dict) and n.get("id")}
    my_node = None
    for n in nodes.values():
        bp = n.get("blueprint")
        if n.get("type") == "feature" and bp:
            if (graph_path.parent / bp).resolve() == (feature_dir / "blueprint.yaml").resolve():
                my_node = n
                break
    if my_node is None:
        return [("WARN", "feature не привязана к knowledge graph (нет узла с blueprint на этот каталог)")]
    released = any(e.get("from") == my_node["id"] and e.get("type") == "delivered-by"
                   for e in g.get("edges") or [])
    if released and current_stage in STAGES and STAGES.index(current_stage) < STAGES.index("release"):
        findings.append(("PROBLEM",
                         f"реальность обогнала blueprint: в графе фича delivered-by (выпущена), "
                         f"а current_stage='{current_stage}' — стадии между "
                         f"{current_stage} и release не заполнены и не declined"))
    return findings


def build_report(feature_dir: Path, graph_path: Path | None):
    report = {"feature_dir": str(feature_dir), "blueprint_errors": [],
              "stages": {}, "findings": [], "verdict": None}

    report["blueprint_errors"] = vfb.validate_dir(feature_dir)
    for e in report["blueprint_errors"]:
        report["findings"].append(("PROBLEM", f"blueprint: {e}"))

    bp = {}
    bp_path = feature_dir / "blueprint.yaml"
    if bp_path.exists():
        try:
            bp = yaml.safe_load(bp_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            bp = {}
    current = ((bp.get("feature") or {}).get("current_stage")) or "?"
    report["current_stage"] = current
    reached_set = set(STAGES[:STAGES.index(current) + 1]) if current in STAGES else set()
    gen = ga.load_generation(feature_dir).get("artifacts", {})

    filled = declined = missing = skeletons = total = 0
    for st in STAGES:
        entries = (bp.get("artifacts") or {}).get(st) or []
        row = {"filled": 0, "draft_or_done_untouched": 0, "planned": 0, "declined": 0, "missing": 0}
        for e in entries:
            if not isinstance(e, dict) or not e.get("path"):
                continue
            total += 1
            status = e.get("status", "planned")
            path = feature_dir / e["path"]
            if status == "declined":
                declined += 1
                row["declined"] += 1
                continue
            if not path.exists():
                missing += 1
                row["missing" if status != "planned" else "planned"] += 1
                if status in ("done", "draft"):
                    report["findings"].append(
                        ("PROBLEM", f"{st}/{e['path']}: status={status}, но файла нет"))
                continue
            rec = gen.get(e["path"])
            untouched = bool(rec) and ga.sha(path.read_bytes()) == rec.get("generated_sha")
            if untouched:
                skeletons += 1
                row["draft_or_done_untouched" if status in ("done", "draft") else "planned"] += 1
                if status in ("done", "draft"):
                    report["findings"].append(
                        ("PROBLEM", f"{st}/{e['path']}: помечен {status}, но это незаполненный скелет"))
                elif st in reached_set:
                    report["findings"].append(
                        ("PROBLEM", f"{st}/{e['path']}: незаполненный скелет достигнутой стадии — "
                                    "заполните или пометьте declined с причиной"))
            else:
                filled += 1
                row["filled"] += 1
        report["stages"][st] = row

    report["coverage"] = {"total": total, "filled": filled, "declined": declined,
                          "skeletons": skeletons, "missing_or_planned": total - filled - declined - skeletons}

    if graph_path and graph_path.exists():
        report["findings"] += graph_findings(feature_dir, graph_path, current)

    # кросс-артефактная консистентность (v2.3): tracking-plan <-> dashboard-spec
    ca_problems, ca_warns, _skip = vca.check_feature(feature_dir)
    report["findings"] += [("PROBLEM", p) for p in ca_problems]
    report["findings"] += [("WARN", w) for w in ca_warns]

    retro = (bp.get("artifacts") or {}).get("retrospective") or []
    retro_done = any(isinstance(e, dict) and e.get("status") in ("done", "draft")
                     and (feature_dir / e.get("path", "")).exists() for e in retro)
    if not retro_done:
        report["findings"].append(("WARN", "retrospective не заполнена — уроки прогона не зафиксированы"))

    problems = [f for lvl, f in report["findings"] if lvl == "PROBLEM"]
    report["verdict"] = "PROBLEM" if problems else ("WARN" if report["findings"] else "OK")
    return report


def find_child_root(feature_dir: Path):
    p = feature_dir
    for _ in range(8):
        if (p / ".ai-ops.yaml").exists():
            return p
        if p.parent == p:
            break
        p = p.parent
    return None


def record_report(r, feature_dir: Path, hist_dir: Path | None):
    """Дописать компактный срез отчёта в историю (JSONL, файл на фичу)."""
    if hist_dir is None:
        base = find_child_root(feature_dir) or feature_dir.parent.parent
        hist_dir = base / ".ai" / "project" / "report-history"
    hist_dir.mkdir(parents=True, exist_ok=True)
    fid = Path(r["feature_dir"]).name
    entry = {
        "schema_version": 1,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "feature": fid,
        "verdict": r["verdict"],
        "current_stage": r["current_stage"],
        "coverage": r["coverage"],
        "problems": sum(1 for lvl, _ in r["findings"] if lvl == "PROBLEM"),
        "warns": sum(1 for lvl, _ in r["findings"] if lvl == "WARN"),
    }
    out = hist_dir / f"{fid}.jsonl"
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"срез записан: {out}")
    return out


def print_report(r):
    print(f"=== Оценка прогона: {Path(r['feature_dir']).name} "
          f"(current_stage={r['current_stage']}) ===")
    c = r["coverage"]
    print(f"покрытие артефактов: {c['filled']} заполнено, {c['declined']} declined (осознанно), "
          f"{c['skeletons']} скелетов, {c['missing_or_planned']} не начато — из {c['total']}")
    reached = STAGES[:STAGES.index(r["current_stage"]) + 1] if r["current_stage"] in STAGES else []
    for st in STAGES:
        row = r["stages"].get(st, {})
        if not any(row.values()):
            continue
        mark = "*" if st in reached else " "
        print(f"  {mark} {st:14} заполнено={row['filled']} declined={row['declined']} "
              f"planned={row['planned']} проблемных={row['draft_or_done_untouched'] + row['missing']}")
    if r["findings"]:
        print("находки:")
        for lvl, f in r["findings"]:
            print(f"  [{lvl}] {f}")
    print(f"ВЕРДИКТ: {r['verdict']}"
          + ("" if r["verdict"] == "OK" else " — детали выше; качество содержания оценивают ревьюеры (gates)"))


def selftest():
    ok = True

    def expect(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"{'PASS' if good else 'FAIL'} {name}" + ("" if good else f" (got {got})"))

    with tempfile.TemporaryDirectory() as td:
        feats = Path(td) / "features"
        ga.cmd_new(feats, "demo-r", "Demo R")
        fdir = feats / "demo-r"
        ga.cmd_scaffold(fdir, "discovery")
        r = build_report(fdir, None)
        expect("незаполненные скелеты достигнутой стадии -> PROBLEM", r["verdict"], "PROBLEM")
        # заполняем discovery по-настоящему
        for f in ("problem-statement", "hypotheses"):
            p = fdir / "discovery" / f"{f}.md"
            p.write_text(p.read_text(encoding="utf-8") + "\nсодержание\n", encoding="utf-8")
        r = build_report(fdir, None)
        expect("честный discovery -> без PROBLEM", r["verdict"] in ("OK", "WARN"), True)
        expect("retro не заполнена -> WARN присутствует",
               any("retrospective" in f for _, f in r["findings"]), True)

        # артефакт помечен done, но остался скелетом
        bp = yaml.safe_load((fdir / "blueprint.yaml").read_text(encoding="utf-8"))
        ga.cmd_scaffold(fdir, "definition")
        for e in bp["artifacts"]["definition"]:
            e["status"] = "done"
        bp["feature"]["current_stage"] = "definition"
        (fdir / "blueprint.yaml").write_text(yaml.safe_dump(bp, allow_unicode=True, sort_keys=False),
                                             encoding="utf-8")
        r = build_report(fdir, None)
        expect("done-скелет -> PROBLEM", r["verdict"], "PROBLEM")

        # реальность обогнала blueprint (граф говорит released)
        graph = Path(td) / "knowledge" / "graph.yaml"
        graph.parent.mkdir()
        graph.write_text(yaml.safe_dump({
            "schema_version": 1, "kind": "knowledge-graph",
            "nodes": [{"id": "f1", "type": "feature",
                       "blueprint": "../features/demo-r/blueprint.yaml"},
                      {"id": "r1", "type": "release"}],
            "edges": [{"from": "f1", "type": "delivered-by", "to": "r1"}],
        }, allow_unicode=True), encoding="utf-8")
        r = build_report(fdir, graph)
        expect("delivered-by при ранней стадии -> PROBLEM 'реальность обогнала'",
               any("обогнала" in f for _, f in r["findings"]), True)
        # запись истории: два среза -> две JSONL-строки
        hist = Path(td) / "hist"
        record_report(r, fdir, hist)
        record_report(r, fdir, hist)
        lines = (hist / "demo-r.jsonl").read_text(encoding="utf-8").strip().split("\n")
        expect("история: 2 записи JSONL", len(lines), 2)
        expect("запись содержит verdict", "verdict" in json.loads(lines[0]), True)
    print("run-report selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    if not argv:
        print(__doc__)
        return 1
    feature_dir = Path(argv[0]).resolve()
    graph = Path(argv[argv.index("--graph") + 1]).resolve() if "--graph" in argv else None
    r = build_report(feature_dir, graph)
    if "--record" in argv:
        i = argv.index("--record")
        nxt = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("--") else None
        record_report(r, feature_dir, Path(nxt).resolve() if nxt else None)
    if "--json" in argv:
        print(json.dumps({**r, "findings": [list(f) for f in r["findings"]]},
                         ensure_ascii=False, indent=2))
    else:
        print_report(r)
    return 1 if r["verdict"] == "PROBLEM" else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
