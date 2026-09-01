#!/usr/bin/env python3
"""ui_readiness.py (v3.11.0 UI Evidence Readiness) — дешёвый ДЕТЕРМИНИРОВАННЫЙ baseline готовности
UI-evidence (Storybook), + gating «UI-CI только на UI-изменениях/VISUAL», + шаблон скрипта БЕЗ установки.

Превращает «адаптер умеет прочитать артефакты» в честный onboarding. Лестница зрелости:
  absent      — Storybook не настроен (нет .storybook/ и нет storybook-зависимости);
  configured  — конфиг/зависимость есть, но нет build-скрипта (нельзя надёжно собрать статику);
  runnable    — есть build-скрипт (build-storybook/storybook) -> можно произвести evidence;
  verified    — есть статик-билд (storybook-static/index.json) И ≥1 артефакт evidence
                (interaction/a11y/visual) -> адаптер строит реальный UIEvidenceBundle.

ЧЕСТНОСТЬ: отсутствие Storybook НЕ маскируется (absent — явный статус, не «ok»); build/interaction/axe/
visual имеют ОТДЕЛЬНЫЕ статусы (pass|fail|not_run|absent) через storybook_adapter. Кит ПРЕДЛАГАЕТ шаблон
скрипта, но НЕ ставит зависимости (решение владельца).

CLI:  ui_readiness.py <child_root> [--json]   |   ui_readiness.py --selftest
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

MATURITY = ("absent", "configured", "runnable", "verified")

# UI-файлы: изменение любого из них (или VISUAL-задача) включает UI-CI. Иначе — skip (не запускаем зря).
_UI_EXT = (".tsx", ".jsx", ".vue", ".svelte", ".css", ".scss", ".less")
_UI_STORY = (".stories.", ".story.")
_UI_DIR_HINTS = ("/components/", "/ui/", "/.storybook/", "/stories/")


def should_run_ui_evidence(changed_files, signals=None):
    """UI-CI запускать ТОЛЬКО если: изменён UI-файл ИЛИ задача VISUAL. -> (bool, reason)."""
    s = dict(signals or {})
    tt = str(s.get("task_type") or "").upper()
    if tt == "VISUAL" or s.get("visual") is True:
        return True, "VISUAL-задача"
    for f in (changed_files or []):
        fl = str(f).lower()
        if any(k in fl for k in _UI_STORY) or fl.endswith(_UI_EXT) or any(h in ("/" + fl) for h in _UI_DIR_HINTS):
            return True, f"изменён UI-файл: {f}"
    return False, "нет UI-изменений и не VISUAL-задача — UI-CI пропущен (не применимо, не маскируем)"


def _pkg_json(root: Path):
    p = root / "package.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _has_sb_dep(pkg):
    if not isinstance(pkg, dict):
        return False
    for sect in ("dependencies", "devDependencies"):
        for name in (pkg.get(sect) or {}):
            if name == "storybook" or name.startswith("@storybook/"):
                return True
    return False


def _build_script(pkg):
    scripts = (pkg or {}).get("scripts") or {} if isinstance(pkg, dict) else {}
    for name, cmd in scripts.items():
        c = str(cmd)
        if "build-storybook" in c or "storybook build" in c or name in ("build-storybook",):
            return name
    return None


def _static_build(root: Path):
    for rel in ("storybook-static/index.json", "storybook-static/stories.json", ".storybook-out/index.json"):
        if (root / rel).is_file():
            return rel
    return None


def assess(child_root):
    """Детерминированный baseline готовности UI-evidence. -> dict со статусом зрелости + честными сигналами."""
    root = Path(child_root)
    pkg = _pkg_json(root)
    config_dir = (root / ".storybook").is_dir()
    dep = _has_sb_dep(pkg)
    build_script = _build_script(pkg)
    static_build = _static_build(root)

    # per-artifact честные статусы через storybook_adapter (build/interaction/a11y/visual)
    per_status = {"story_index": "absent", "interaction": "not_run", "a11y": "not_run", "visual": "not_run"}
    evidence_present = False
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import storybook_adapter as _sb
        bundle = _sb.build_bundle(root)
        per_status = {
            "story_index": "present" if (bundle.get("story_index") or bundle.get("components")) else "absent",
            "interaction": (bundle.get("interaction") or {}).get("status", "not_run"),
            "a11y": (bundle.get("a11y") or {}).get("status", "not_run"),
            "visual": (bundle.get("visual") or {}).get("status", "not_run"),
        }
        evidence_present = any(per_status[k] in ("pass", "fail") for k in ("interaction", "a11y", "visual"))
    except Exception:   # noqa: BLE001 — адаптер недоступен/нет артефактов -> честно absent/not_run
        pass

    if not config_dir and not dep:
        maturity = "absent"
    elif static_build and evidence_present:
        maturity = "verified"
    elif build_script or dep:
        maturity = "runnable"
    else:
        maturity = "configured"

    rec = {
        "absent": "Storybook не настроен. Кит предлагает шаблон (script_template) — установку зависимостей делает ВЛАДЕЛЕЦ.",
        "configured": "Есть конфиг/зависимость, но нет build-скрипта. Добавьте build-storybook + evidence-скрипты (script_template).",
        "runnable": "Можно собрать evidence. Прогоните build + interaction/axe/visual, чтобы получить verified.",
        "verified": "UI-evidence собирается — адаптер строит реальный UIEvidenceBundle. Готово.",
    }[maturity]
    return {"kind": "UIReadiness", "storybook_maturity": maturity,
            "signals": {"config_dir": config_dir, "storybook_dep": dep, "build_script": build_script,
                        "static_build": static_build, "evidence_present": evidence_present},
            "evidence_status": per_status, "recommendation": rec,
            "installs_dependencies": False}   # кит НИКОГДА не ставит deps сам


def script_template():
    """Готовый npm-скрипт-набор для UI-evidence (владелец добавляет в package.json сам; deps не ставим)."""
    return {
        "build-storybook": "storybook build -o storybook-static",
        "ui:interaction": "vitest run --reporter=json --outputFile=interaction.json",
        "ui:a11y": "test-storybook --json > axe.json || true",
        "ui:visual": "echo '{\"status\":\"not_run\"}' > visual.json  # подключите свой visual-раннер",
        "_note": "Кит НЕ устанавливает зависимости. Добавьте @storybook/*, test-storybook, vitest сами (npm i -D ...).",
    }


def check(a):
    """Валидация UIReadiness-отчёта + честность (absent не маскируется как ok)."""
    e = []
    if not isinstance(a, dict) or a.get("kind") != "UIReadiness":
        return ["kind должен быть UIReadiness"]
    if a.get("storybook_maturity") not in MATURITY:
        e.append(f"storybook_maturity ∉ {MATURITY}")
    if a.get("installs_dependencies") is not False:
        e.append("installs_dependencies обязан быть False — кит не ставит зависимости за владельца")
    for k, v in (a.get("evidence_status") or {}).items():
        if v not in ("pass", "fail", "not_run", "absent", "present"):
            e.append(f"evidence_status.{k}: недопустимый статус {v!r}")
    return e


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    import tempfile
    # gating
    r, _ = should_run_ui_evidence(["src/features/x.tsx"])
    expect("gating: изменён .tsx -> UI-CI ON", r is True)
    r, _ = should_run_ui_evidence(["server/api.py"])
    expect("gating: не-UI изменение -> UI-CI OFF", r is False)
    r, _ = should_run_ui_evidence(["docs/readme.md"], {"task_type": "VISUAL"})
    expect("gating: VISUAL-задача -> UI-CI ON даже без UI-файла", r is True)
    r, _ = should_run_ui_evidence([".storybook/main.ts"])
    expect("gating: .storybook/ файл -> UI-CI ON", r is True)

    # maturity absent (пустой репо)
    with tempfile.TemporaryDirectory() as td:
        a = assess(td)
        expect("пустой репо -> maturity=absent (не маскируем)", a["storybook_maturity"] == "absent")
        expect("check валиден + installs_dependencies=False", check(a) == [] and a["installs_dependencies"] is False)

    # maturity configured (есть .storybook, нет build-скрипта/dep)
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / ".storybook").mkdir()
        (Path(td) / "package.json").write_text('{"name":"x"}', encoding="utf-8")
        a = assess(td)
        expect("есть .storybook, нет скрипта -> configured", a["storybook_maturity"] == "configured")

    # maturity runnable (есть storybook dep + build-скрипт)
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "package.json").write_text(
            '{"devDependencies":{"storybook":"^8"},"scripts":{"build-storybook":"storybook build -o storybook-static"}}',
            encoding="utf-8")
        a = assess(td)
        expect("dep + build-скрипт, нет evidence -> runnable", a["storybook_maturity"] == "runnable")

    expect("check: installs_dependencies=True -> ошибка",
           any("не ставит зависимости" in x for x in check({"kind": "UIReadiness",
               "storybook_maturity": "absent", "installs_dependencies": True, "evidence_status": {}})))
    expect("script_template не ставит deps (есть предупреждение)", "_note" in script_template())

    print("ui_readiness selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _fmt(a):
    s = a["signals"]
    L = [f"Storybook maturity: {a['storybook_maturity'].upper()}"]
    L.append("  сигналы: config=%s dep=%s build_script=%s static_build=%s" %
             (s["config_dir"], s["storybook_dep"], s["build_script"], s["static_build"]))
    es = a["evidence_status"]
    L.append("  evidence (честно): story_index=%s interaction=%s a11y=%s visual=%s" %
             (es["story_index"], es["interaction"], es["a11y"], es["visual"]))
    L.append("  → " + a["recommendation"])
    if a["storybook_maturity"] in ("absent", "configured"):
        L.append("  шаблон скрипта (добавьте в package.json; зависимости ставит ВЛАДЕЛЕЦ):")
        for k, v in script_template().items():
            L.append(f"    {k}: {v}")
    return "\n".join(L)


def main(argv):
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print("usage: ui_readiness.py <child_root> [--json]"); return 2
    a = assess(args[0])
    if "--json" in argv:
        print(json.dumps(a, ensure_ascii=False, indent=2)); return 0
    print(_fmt(a))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
