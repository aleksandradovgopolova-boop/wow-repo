#!/usr/bin/env python3
"""Генератор артефактов по Feature Blueprint (Ф3 roadmap, v1.5).

Принцип 27 (единый источник): скелеты артефактов создаются детерминированно из
templates/ по blueprint.yaml; СОДЕРЖАНИЕ пишут агенты соответствующих стадий.
Генератор никогда не перезаписывает существующие файлы.

Стадийные наборы шаблонов покрывают генераторы из VISION.md:
  discovery -> Discovery Generator; definition -> PRD Generator; ux -> UX Generator;
  analytics -> Analytics + Dashboard Generators; documentation -> Documentation
  Generator; release -> Release Generator; monitoring -> Monitoring Generator;
  retrospective -> Retrospective Generator. Experiment Generator — команда add
  (например: add <dir> discovery experiments/exp-1.md templates/product/Experiment.md).

Команды:
  new <features-dir> <feature-id> [имя] [--profile lean|full]
                                            — новый blueprint из templates/blueprint/;
                                              lean (прототип/MVP) = 5 стадий, full = 11
  scaffold <feature-dir> [--stage <stage>]  — создать недостающие скелеты (все стадии
                                              или одну); фиксирует хэши в .generation.json
  add <feature-dir> <stage> <path> <template> — добавить артефакт в blueprint и создать скелет
  check <feature-dir>                       — drift-статус: edited / untouched / template-updated;
                                              возврат 1, если в достигнутых стадиях остались
                                              незаполненные скелеты
  --selftest

Возврат 0 — успех, 1 — ошибка/незаполненные скелеты. Требует pyyaml.
"""

# PEP 563: ленивые аннотации — `str | None` (PEP 604) не вычисляется при импорте,
# поэтому модуль грузится и на Python 3.9 (дефолт macOS CommandLineTools). finding
# квалификационного прогона: без этого импорт падал TypeError на 3.9.
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

import yaml

PKG = Path(__file__).resolve().parents[1]
BLUEPRINT_TEMPLATE = PKG / "templates" / "blueprint" / "FeatureBlueprint.yaml"
GENERATION_FILE = ".generation.json"
STAGES = ["discovery", "definition", "ux", "architecture", "delivery",
          "analytics", "documentation", "release", "monitoring", "adoption", "retrospective"]
HEADER = ("<!-- скелет сгенерирован ai-ops generate_artifacts из {template} "
          "для функции {fid}; заполните содержание -->\n")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_blueprint(feature_dir: Path):
    bp_path = feature_dir / "blueprint.yaml"
    if not bp_path.exists():
        raise SystemExit(f"нет {bp_path}")
    return yaml.safe_load(bp_path.read_text(encoding="utf-8"))


def load_generation(feature_dir: Path):
    p = feature_dir / GENERATION_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"artifacts": {}}


def save_generation(feature_dir: Path, gen):
    (feature_dir / GENERATION_FILE).write_text(
        json.dumps(gen, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def cmd_new(features_dir: Path, fid: str, name: str | None, profile: str = "full"):
    tmpl = (PKG / "templates" / "blueprint" / "FeatureBlueprint.lean.yaml"
            if profile == "lean" else BLUEPRINT_TEMPLATE)
    if not tmpl.exists():
        raise SystemExit(f"нет шаблона {tmpl}")
    fdir = features_dir / fid
    if (fdir / "blueprint.yaml").exists():
        print(f"{fdir}/blueprint.yaml уже существует — используйте scaffold.")
        return 1
    bp = yaml.safe_load(tmpl.read_text(encoding="utf-8"))
    bp["feature"]["id"] = fid
    bp["feature"]["name"] = name or fid
    bp["feature"]["status"] = "planned"
    bp["feature"]["current_stage"] = "discovery"
    fdir.mkdir(parents=True, exist_ok=True)
    (fdir / "blueprint.yaml").write_text(
        yaml.safe_dump(bp, allow_unicode=True, sort_keys=False, width=110), encoding="utf-8")
    print(f"создан {fdir}/blueprint.yaml (профиль {profile}) — запустите scaffold для "
          "скелетов discovery." + ("" if profile == "lean" else
          " Для прототипа/MVP есть --profile lean (5 стадий вместо 11)."))
    return 0


def scaffold_entry(feature_dir: Path, fid: str, entry: dict, gen: dict, created: list):
    path = feature_dir / entry["path"]
    tmpl_rel = entry.get("template")
    if path.exists() or not tmpl_rel:
        return
    tmpl = PKG / tmpl_rel
    if not tmpl.exists():
        raise SystemExit(f"шаблон {tmpl_rel} не найден в пакете")
    body = HEADER.format(template=tmpl_rel, fid=fid) + tmpl.read_text(encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    gen["artifacts"][entry["path"]] = {
        "template": tmpl_rel,
        "template_sha": sha(tmpl.read_bytes()),
        "generated_sha": sha(body.encode("utf-8")),
    }
    created.append(entry["path"])


def cmd_scaffold(feature_dir: Path, stage: str | None):
    bp = load_blueprint(feature_dir)
    fid = bp["feature"]["id"]
    gen = load_generation(feature_dir)
    created = []
    for st, entries in (bp.get("artifacts") or {}).items():
        if stage and st != stage:
            continue
        for e in entries or []:
            if isinstance(e, dict) and e.get("status") != "declined":
                scaffold_entry(feature_dir, fid, e, gen, created)
    save_generation(feature_dir, gen)
    if created:
        print(f"создано скелетов: {len(created)}")
        for c in created:
            print(f"  + {c}")
    else:
        print("все артефакты уже существуют — ничего не создано.")
    return 0


def cmd_add(feature_dir: Path, stage: str, rel_path: str, template: str):
    if stage not in STAGES:
        raise SystemExit(f"стадия '{stage}' вне словаря: {STAGES}")
    bp_path = feature_dir / "blueprint.yaml"
    bp = load_blueprint(feature_dir)
    entries = bp.setdefault("artifacts", {}).setdefault(stage, [])
    if any(e.get("path") == rel_path for e in entries if isinstance(e, dict)):
        print(f"артефакт {rel_path} уже в blueprint.")
        return 1
    entry = {"path": rel_path, "template": template, "status": "planned"}
    entries.append(entry)
    bp_path.write_text(yaml.safe_dump(bp, allow_unicode=True, sort_keys=False, width=110),
                       encoding="utf-8")
    gen = load_generation(feature_dir)
    created = []
    scaffold_entry(feature_dir, bp["feature"]["id"], entry, gen, created)
    save_generation(feature_dir, gen)
    print(f"добавлен и создан: {rel_path}" if created else f"добавлен в blueprint: {rel_path}")
    return 0


def cmd_check(feature_dir: Path):
    bp = load_blueprint(feature_dir)
    gen = load_generation(feature_dir)
    stage = bp["feature"]["current_stage"]
    reached = set(STAGES[:STAGES.index(stage) + 1]) if stage in STAGES else set()
    art_stage = {e["path"]: st for st, es in (bp.get("artifacts") or {}).items()
                 for e in es or [] if isinstance(e, dict) and e.get("path")}
    untouched_reached = []
    for rel, rec in gen.get("artifacts", {}).items():
        path = feature_dir / rel
        tmpl = PKG / rec["template"]
        notes = []
        if tmpl.exists() and sha(tmpl.read_bytes()) != rec["template_sha"]:
            notes.append("template-updated")
        if not path.exists():
            state = "removed"
        elif sha(path.read_bytes()) == rec["generated_sha"]:
            state = "untouched-skeleton"
            if art_stage.get(rel) in reached:
                untouched_reached.append(rel)
        else:
            state = "edited"
        print(f"  {state:18} {rel}" + (f"  [{', '.join(notes)}]" if notes else ""))
    if untouched_reached:
        print(f"НЕЗАПОЛНЕННЫЕ СКЕЛЕТЫ достигнутых стадий (current_stage={stage}):")
        for r in untouched_reached:
            print(f"  - {r}")
        return 1
    print(f"OK: drift-статус чист (current_stage={stage}).")
    return 0


def selftest():
    ok = True

    def expect(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"{'PASS' if good else 'FAIL'} {name}" + ("" if good else f" (got {got})"))

    with tempfile.TemporaryDirectory() as td:
        feats = Path(td) / "features"
        expect("new создаёт blueprint", cmd_new(feats, "demo-x", "Demo X"), 0)
        fdir = feats / "demo-x"
        expect("scaffold discovery", cmd_scaffold(fdir, "discovery"), 0)
        ps = fdir / "discovery" / "problem-statement.md"
        expect("скелет problem-statement создан", ps.exists(), True)
        expect("check: незаполненные скелеты discovery -> 1", cmd_check(fdir), 1)
        ps.write_text(ps.read_text(encoding="utf-8") + "\nНастоящее содержание.\n", encoding="utf-8")
        hyp = fdir / "discovery" / "hypotheses.md"
        hyp.write_text(hyp.read_text(encoding="utf-8") + "\nH1.\n", encoding="utf-8")
        expect("check: после заполнения -> 0", cmd_check(fdir), 0)
        expect("scaffold идемпотентен (не перезаписывает)",
               "Настоящее содержание." in ps.read_text(encoding="utf-8") if cmd_scaffold(fdir, "discovery") == 0 else False,
               True)
        expect("add experiment", cmd_add(fdir, "discovery", "experiments/exp-1.md",
                                         "templates/product/Experiment.md"), 0)
        expect("файл эксперимента создан", (fdir / "experiments" / "exp-1.md").exists(), True)
    print("generate_artifacts selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    if not argv:
        print(__doc__)
        return 1
    cmd, args = argv[0], argv[1:]
    if cmd == "new" and len(args) >= 2:
        profile = "full"
        if "--profile" in args:
            i = args.index("--profile")
            profile = args[i + 1]
            args = args[:i] + args[i + 2:]
        if profile not in ("lean", "full"):
            raise SystemExit(f"--profile '{profile}' вне [lean, full]")
        return cmd_new(Path(args[0]), args[1], args[2] if len(args) > 2 else None, profile)
    if cmd == "scaffold" and args:
        stage = args[args.index("--stage") + 1] if "--stage" in args else None
        return cmd_scaffold(Path(args[0]).resolve(), stage)
    if cmd == "add" and len(args) == 4:
        return cmd_add(Path(args[0]).resolve(), args[1], args[2], args[3])
    if cmd == "check" and args:
        return cmd_check(Path(args[0]).resolve())
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
