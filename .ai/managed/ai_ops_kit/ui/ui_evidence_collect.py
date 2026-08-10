#!/usr/bin/env python3
"""ui_evidence_collect.py (v3.7 UI-CI, подтянуто в v3.6.8) — сбор РЕАЛЬНОГО UI-evidence на точном SHA.

StorybookPolicy: UI-evidence (.ai/ui-evidence/{interaction,a11y,visual,design-system}.json) производит
UI-CI child-репо, а pipeline читает его на committed_sha. Раньше коллектора не было (только
implementation_verification build/lint/test). Здесь — оркестратор:

  1. детект UI-стека (package.json со скриптом `ui-evidence` или storybook-зависимостью);
  2. запуск child UI-CI (`npm ci` в свежем worktree, затем `npm run ui-evidence`), который РЕАЛЬНО
     гоняет vitest (interaction) + vitest-axe (a11y, jsdom) + storybook test-runner (visual, headless
     chromium) + design-system reuse и пишет сырые *.json;
  3. АВТОРИТЕТНАЯ запись `.ai/ui-evidence/meta.json{commit_sha}` — SHA-binding контролирует KIT
     (не child), чтобы evidence гарантированно привязано к committed_sha прогона.

НИКАКОЙ фабрикации: если UI-CI не отработал (нет node/ошибка) — evidence не появляется, гейты честно
`not_run` (build_bundle ничего не найдёт). Не-UI child (python) -> skipped, поведение не меняется.

Только stdlib. CLI: ui_evidence_collect.py <work_root> --sha SHA | --selftest
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def is_ui_stack(root) -> bool:
    pj = Path(root) / "package.json"
    if not pj.exists():
        return False
    try:
        d = json.loads(pj.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    scripts = d.get("scripts", {}) or {}
    deps = {**(d.get("dependencies", {}) or {}), **(d.get("devDependencies", {}) or {})}
    return "ui-evidence" in scripts or any("storybook" in str(k) for k in deps)


def write_meta(root, commit_sha) -> Path:
    """АВТОРИТЕТНАЯ мета: commit_sha от KIT (не от child) -> exact-revision binding для evidence_for_gate."""
    ed = Path(root) / ".ai" / "ui-evidence"
    ed.mkdir(parents=True, exist_ok=True)
    p = ed / "meta.json"
    p.write_text(json.dumps({"commit_sha": commit_sha, "collected_by": "ui_evidence_collect"},
                            ensure_ascii=False), encoding="utf-8")
    return p


def collect(work_root, commit_sha, run_npm=True, timeout=600) -> dict:
    """Собрать UI-evidence на committed_sha. UI-стек обязателен; иначе skipped (no-op для python child)."""
    root = Path(work_root)
    if not is_ui_stack(root):
        return {"skipped": True, "reason": "не UI-стек (нет package.json со storybook/ui-evidence)"}
    if not commit_sha:
        return {"skipped": True, "reason": "нет commit_sha — не привязать evidence к ревизии"}
    ran, errors = [], []
    if run_npm:
        import shutil
        if not shutil.which("npm"):
            return {"skipped": True, "reason": "npm недоступен — UI-CI не запущен (evidence not_run)"}
        try:
            if not (root / "node_modules").is_dir():   # worktree свежий: node_modules gitignore'н
                subprocess.run(["npm", "ci"], cwd=str(root), capture_output=True, timeout=timeout, check=True)
                ran.append("npm ci")
            subprocess.run(["npm", "run", "ui-evidence"], cwd=str(root),
                           capture_output=True, timeout=timeout, check=True)
            ran.append("npm run ui-evidence")
        except (subprocess.SubprocessError, OSError) as e:
            errors.append(f"{type(e).__name__}: {str(e)[:160]}")
    meta = write_meta(root, commit_sha)   # SHA-binding пишем ВСЕГДA (даже если UI-CI частично упал —
    #                                        build_bundle сам решит по наличию/статусу сырых *.json)
    return {"skipped": False, "commit_sha": commit_sha, "ran": ran, "errors": errors,
            "meta": str(meta.relative_to(root))}


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1

    def _opt(n, d=None):
        return argv[argv.index(n) + 1] if n in argv else d
    res = collect(args[0], _opt("--sha"))
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
