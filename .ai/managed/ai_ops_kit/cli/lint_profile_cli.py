"""`./ai-ops lint-profile` — единый стиль кода дочки держит машина, а не внимательность (#1183).

Три действия, одна команда:
  * `lint-profile`            — сухой прогон: что будет записано и какие правила, ничего не пишет;
  * `lint-profile --apply`    — записать профиль поверх конфигов линтеров дочки и заморозить то, что
                                 уже есть в коде (повторный вызов линию не трогает; `--force` —
                                 заморозить заново, осознанно);
  * `lint-profile check`      — новое расхождение краснеет (код 1), исправленное можно ужать
                                 (`check --apply`); проверить нечем — код 2, а не «зелено».

Границы слоёв берутся из того же объявления, что читает проверка архитектурных инвариантов
(`planning.architecture_invariants.load_declaration`) — второго объявления не заводится.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ai_ops_kit.checks import lint_baseline

_STACK_RU = {"javascript": "JavaScript", "typescript": "TypeScript", "python": "Python", "go": "Go"}
_TOOL_RU = {"eslint": "ESLint", "ruff": "Ruff", "import-linter": "import-linter",
            "golangci-lint": "golangci-lint", "ast-grep": "ast-grep"}

TIMEOUT_S = 900


def _stacks_ru(stacks) -> str:
    names = [_STACK_RU.get(s, "прочих языков (" + s.split(":", 1)[-1] + ")") for s in stacks]
    return ", ".join(names) or "—"


# ── Запуск инструментов: процесс живёт здесь, в точке входа; argv, разбор и линия — в checks. ──
def _run(argv, cwd: Path, timeout=TIMEOUT_S) -> tuple:
    """-> (код, вывод). Не запустился / не уложился — код 2 и причина."""
    try:
        proc = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 2, f"не запустился: {e}"
    return proc.returncode, proc.stdout if proc.stdout.strip() else proc.stdout + proc.stderr


def measure(root: Path, spec: dict, extra=(), targets=None, timeout=TIMEOUT_S) -> tuple:
    """Прогнать инструмент профиля -> (находки|None, подавлено, причина-если-не-вышло).

    targets — только эти файлы (хук правки); timeout — потолок времени вызывающего (у хука свой)."""
    return lint_baseline.run_tool(root, spec, lambda argv, cwd: _run(argv, cwd, timeout),
                                  extra, targets)


def _say(root: Path, msg: dict) -> None:
    from ai_ops_kit.ui import presenter
    print(presenter.render(msg, audience=presenter.audience_from_config(root)))


def _message(*args, **kwargs) -> dict:
    from ai_ops_kit.ui import presenter
    return presenter.message(*args, **kwargs)


def _what(profile: dict) -> str:
    parts = []
    if profile["identifiers"]["value"] == "latin":
        parts.append("имена в коде только латиницей")
    if "python" in profile["stacks"] or "go" in profile["stacks"]:
        parts.append("соглашения об именах языка")
    if profile["architecture_declared"]:
        parts.append("границы слоёв из объявленной архитектуры")
    return ", ".join(parts) or "добавлять нечего"


def _technical(profile: dict, extra=None) -> dict:
    return {"files": sorted(profile["files"]), "tools": profile["tools"],
            "identifiers": profile["identifiers"], "notes": profile["notes"], **(extra or {})}


def _preview(root: Path, profile: dict) -> int:
    from ai_ops_kit.checks import lint_profile
    stale = lint_profile.drift(root, profile)
    notes = " ".join(n.rstrip(".") + "." for n in profile["notes"])
    _say(root, _message(
        "needs_input",
        f"Подготовлен единый стиль кода для {_stacks_ru(profile['stacks'])}: {_what(profile)}. "
        "Пока ничего не записано.",
        why_it_matters=("Сейчас стиль держится на внимательности людей и агентов, а с профилем его "
                        "проверяет машина. Всё, что уже есть в коде, замораживается — текущая "
                        "версия не сломается, а новое расхождение не пройдёт. " + notes).strip(),
        decision={"question": "Включить единый стиль кода?",
                  "recommendation": "Да: существующий код не ломается, счётчик расхождений может "
                                    "только уменьшаться",
                  "on_approve": "./ai-ops lint-profile --apply"},
        technical=_technical(profile, {"would_write": stale})))
    return 0


def _apply(root: Path, profile: dict, force: bool) -> int:
    from ai_ops_kit.checks import lint_profile
    written = lint_profile.apply(root, profile)
    results = lint_baseline.freeze(root, profile["tools"], measure, force=force)
    missing = [r for r in results if r["status"] == lint_baseline.NOT_CHECKED]
    frozen = sum(r.get("frozen", 0) for r in results)
    kept = [r for r in results if r["status"] == "kept"]
    summary = (f"Единый стиль кода включён для {_stacks_ru(profile['stacks'])}: {_what(profile)}. "
               + (f"Уже существующие расхождения заморожены: {frozen}. " if frozen or not kept else "")
               + ("Прежняя заморозка сохранена — повторная сборка её не трогает." if kept else ""))
    status = "degraded" if missing else "done"
    why = ("Новое расхождение теперь не пройдёт проверку, а исправленное уменьшает счётчик — вырасти "
           "он не может.")
    if missing:
        why += " Но проверить удалось не всё: " + "; ".join(r["reason"] for r in missing) + "."
    _say(root, _message(status, summary.strip(), why_it_matters=why,
                        next_steps=["Сохраните в репозитории записанные файлы (" + ", ".join(written
                                    or ["без изменений"]) + ") и заморозку в .ai/project/lint/",
                                    "Перед слиянием: ./ai-ops lint-profile check"],
                        technical=_technical(profile, {"written": written, "freeze": results})))
    return 1 if missing else 0


def _check(root: Path, profile: dict, tighten: bool) -> int:
    from ai_ops_kit.checks import lint_profile
    stale = lint_profile.drift(root, profile)
    res = lint_baseline.check(root, profile["tools"], measure, tighten=tighten)
    rows = res["tools"]
    grown = lint_baseline.grown(res)
    shrunk = sum(s["was"] - s["now"] for r in rows for s in r.get("shrunk") or [])
    frozen = sum(r.get("frozen", 0) for r in rows)
    unchecked = [f"{_TOOL_RU.get(r['tool'], r['tool'])}: {r['reason']}" for r in rows
                 if r["status"] == lint_baseline.NOT_CHECKED]
    tech = _technical(profile, {"check": res, "stale_files": stale})
    if res["status"] == lint_baseline.NOT_CHECKED:
        _say(root, _message("blocked", "Проверить стиль кода нечем: " + "; ".join(unchecked or [
            "профиль не записан"]) + ".", why_it_matters="Отсутствие находок здесь не значит, что "
            "расхождений нет — проверка просто не выполнялась.",
            next_steps=["./ai-ops lint-profile --apply"], technical=tech))
        return 2
    if grown:
        files = len({g["file"] for g in grown})
        _say(root, _message("blocked", f"Появились новые расхождения со стилем кода: {len(grown)} "
                            f"в {files} файл(ах).", why_it_matters="Стиль заморожен и может только "
                            "улучшаться: эти изменения не пройдут проверку, пока их не исправить.",
                            next_steps=[lint_baseline.address(g) for g in grown[:8]]
                            + ["Исправьте и запустите снова: ./ai-ops lint-profile check"],
                            technical=tech))
        return 1
    partial = bool(unchecked or stale)
    summary = f"Новых расхождений со стилем кода нет (заморожено {frozen})."
    if shrunk:
        summary += (f" Исправлено {shrunk} — заморозка ужата." if tighten else
                    f" Исправлено {shrunk} — ужать заморозку: ./ai-ops lint-profile check --apply")
    why = []
    if unchecked:
        why.append("Но проверено не всё: " + "; ".join(unchecked) + ".")
    if stale:
        why.append("Профиль устарел: в коде появились новые слои или срезы, которых он ещё не видит "
                   "— пересоберите: ./ai-ops lint-profile --apply.")
    _say(root, _message("degraded" if partial else "done", summary,
                        why_it_matters=" ".join(why) or None, technical=tech))
    return 1 if partial else 0


def run_lint_profile(child_root, verb=None, apply=False, force=False, js=False) -> int:
    """Точка входа команды. verb: None — профиль (сухой прогон/--apply), `check` — сверка с линией."""
    from ai_ops_kit.checks import lint_profile
    from ai_ops_kit.planning import architecture_invariants
    root = Path(child_root)
    if verb not in (None, "", "check"):
        _say(root, _message("blocked", f"Не знаю действия «{verb}» у lint-profile.",
                            next_steps=["./ai-ops lint-profile — показать профиль",
                                        "./ai-ops lint-profile check — проверить новые расхождения"]))
        return 2
    profile = lint_profile.render(root, architecture_invariants.load_declaration(root))
    if js:
        out = {"kind": "LintProfile", **{k: v for k, v in profile.items() if k != "files"},
               "files": sorted(profile["files"])}
        if verb == "check":
            out["check"] = lint_baseline.check(root, profile["tools"], measure, tighten=apply)
            out["stale_files"] = lint_profile.drift(root, profile)
        elif apply:
            out["written"] = lint_profile.apply(root, profile)
            out["freeze"] = lint_baseline.freeze(root, profile["tools"], measure, force=force)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        if any(r["status"] == lint_baseline.NOT_CHECKED for r in out.get("freeze") or []):
            return 1
        st = (out.get("check") or {}).get("status")
        stale = bool(out.get("stale_files"))
        return {"grew": 1, "not_checked": 2, "partial": 1}.get(st, 1 if stale else 0)
    if not profile["stacks"]:
        _say(root, _message("blocked", "Не нашёл в репозитории кода, для которого у кита есть "
                            "профиль стиля.", why_it_matters="Профиль строится по стеку: JavaScript/"
                            "TypeScript, Python, Go или язык, который понимает ast-grep."))
        return 2
    if verb == "check":
        return _check(root, profile, tighten=apply)
    return _apply(root, profile, force) if apply else _preview(root, profile)


def _intent_lint_profile(task, child_root, signals, a) -> int:
    """Обработчик интента `lint-profile` (регистрируется в ai_ops_cli)."""
    return run_lint_profile(Path(child_root), verb=(task or None), apply=bool(getattr(a, "apply", False)),
                            force=bool(getattr(a, "force", False)), js=bool(getattr(a, "json", False)))
