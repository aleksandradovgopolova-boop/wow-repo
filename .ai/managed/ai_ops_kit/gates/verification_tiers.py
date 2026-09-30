#!/usr/bin/env python3
"""verification_tiers.py (v3.27.3 WP4 Progressive Verification Truth) — определение уровня
верификации и выбор тестов на основе изменённых файлов и lifecycle_intent.

Verification tiers:
- skip: только документация/конфиги — не запускаем product build/test
- affected: только тесты, затронутые изменениями (быстро, для итерации)
- module: все тесты затронутых модулей/пакетов (средне, для checkpoint)
- full: полный набор тестов (медленно, для merge/release)

Test selection engine:
changed_files → repo_graph.impact() → affected_tests() → targeted test command
Команды берутся из project_detector (child config), не угадываются.

Impact status:
- targeted_tests_found: найдены затронутые тесты
- targeted_tests_not_found: граф не нашёл тестов (impact_unknown, не "не влияет")
- docs_only: только документация — skip verification

CLI: verification_tiers.py --changed file1 file2 [--tier affected|module|full|skip] [--intent draft|merge|release] [--json]
     verification_tiers.py --selftest
"""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402
# ============================================================================
# Verification Tiers
# ============================================================================

TIERS = ("skip", "affected", "module", "full")

# Impact status — честный статус влияния изменений на тесты
IMPACT_TARGETED_TESTS_FOUND = "targeted_tests_found"
IMPACT_TARGETED_TESTS_NOT_FOUND = "targeted_tests_not_found"  # impact_unknown, не "не влияет"
IMPACT_DOCS_ONLY = "docs_only"

# Файлы, которые всегда требуют full verification (критическая инфраструктура)
ALWAYS_FULL_PATTERNS = (
    "registry/", "schemas/", "quality/", ".github/", "scripts/",
    "templates/", "skills/", "agents/", "commands/", "prompts/",
    "workflows/", "rules/", "memory/", "evaluations/", "AGENTS.md",
    "VERSION", "pyproject.toml", "package.json", "package-lock.json",
    "uv.lock", "requirements*.txt", "conftest.py", "fixtures/", "*config*",
    "ai_ops_kit/gates/verification_tiers.py", "ai_ops_kit/context/repo_graph.py",
    "ai_ops_kit/providers/orchestrator.py", "ai_ops_kit/engine/execution_pipeline.py",
    "ai_ops_kit/gates/preflight.py", "ai_ops_kit/gates/gate_executor.py",
    "ai_ops_kit/engine/tool_broker.py", "ai_ops_kit/shared/lifecycle_store.py",
)

# Файлы, которые не требуют verification (документация, конфиги)
SKIP_VERIFICATION_PATTERNS = ("README.md", "ROADMAP.md", "CHANGELOG.md", "CONTRIBUTING.md", "FILE_INDEX.md", "LICENSE", "docs/", "newsfragments/")


def _prose_only(path: str) -> bool:
    # Markdown in executable kit assets is configuration, not exempt prose.
    return ((path.endswith(".md") or path == "LICENSE")
            and not _matches_any(path, ALWAYS_FULL_PATTERNS)
            and (_matches_any(path, SKIP_VERIFICATION_PATTERNS) or
                 (path.endswith(".md") and path.startswith(("docs/", "newsfragments/")))))


# Lifecycle intent → verification tier mapping
INTENT_TO_TIER = {
    "explore": "skip",
    "draft": "affected",
    "ready_for_review": "module",
    "merge_candidate": "full",
    "release_candidate": "full",
}


def _matches_any(path: str, patterns: tuple) -> bool:
    """Проверить, соответствует ли путь любому из паттернов."""
    for p in patterns:
        if p.endswith("/"):
            if path.startswith(p) or f"/{p}" in path:
                return True
        elif "*" in p:
            if Path(path).match(p):
                return True
        elif path == p or path.endswith(f"/{p}"):
            return True
    return False


def decide_tier(changed_files: list, force_tier: str = None, lifecycle_intent: str = None) -> str:
    """Определить уровень верификации на основе изменённых файлов и lifecycle_intent.
    force_tier — принудительный уровень (если задан).
    lifecycle_intent — стадия жизненного цикла (explore/draft/ready_for_review/merge_candidate).
    -> 'skip' | 'affected' | 'module' | 'full'"""
    # Risk floors precede explicit tiers and lifecycle: draft/explore cannot exempt infra.
    if lifecycle_intent in ("merge_candidate", "release_candidate", "merge", "release"):
        return "full"
    if any(_matches_any(f, ALWAYS_FULL_PATTERNS) for f in changed_files):
        return "full"
    if force_tier == "full":
        return "full"
    if not changed_files or all(_prose_only(f) for f in changed_files):
        return "skip"
    if force_tier == "module" or lifecycle_intent == "ready_for_review" or len(changed_files) > 20:
        return "module"
    return "affected"


# ============================================================================
# Test Selection Engine
# ============================================================================

def select_tests(changed_files: list, child_root: str, tier: str = None, lifecycle_intent: str = None, profile: dict = None) -> dict:
    """Выбрать тесты на основе изменённых файлов, уровня верификации и lifecycle_intent.
    -> {tier, changed_files, affected_tests, targeted_command, full_command, impact_status, note}

    v3.27.3 WP4:
    - skip tier: docs-only — не запускаем product build/test
    - impact_status: targeted_tests_found | targeted_tests_not_found | docs_only
    - команды берутся из project_detector (child config), не угадываются
    """
    from ai_ops_kit.context import repo_graph
    from ai_ops_kit.shared import project_detector

    tier = decide_tier(changed_files, tier, lifecycle_intent)
    child_root = Path(child_root)

    # v3.27.3 WP4: skip tier — docs-only, не запускаем verification
    if tier == "skip":
        return {
            "tier": "skip",
            "changed_files": changed_files,
            "affected_tests": [],
            "targeted_command": None,
            "full_command": False,
            "impact_status": IMPACT_DOCS_ONLY,
            "note": "Product tests exempt: only allowlisted prose; validators/lint still apply",
        }

    if tier == "full":
        return {
            "tier": "full",
            "changed_files": changed_files,
            "affected_tests": None,  # все тесты
            "targeted_command": None,  # полная команда из project_detector
            "full_command": True,
            "impact_status": IMPACT_TARGETED_TESTS_FOUND,
            "note": "Full verification: критическая инфраструктура или много изменений",
        }

    def fallback(reason):
        return {"tier": "full", "changed_files": changed_files, "affected_tests": [],
                "targeted_command": None, "full_command": True,
                "impact_status": IMPACT_TARGETED_TESTS_NOT_FOUND, "note": f"Full fallback: {reason}"}

    sources = [f for f in changed_files if not _prose_only(f)]
    if any(Path(f).is_absolute() or ".." in Path(f).parts for f in sources):
        return fallback("path outside repository")
    if any(not (child_root / f).is_file() for f in sources):
        return fallback("deleted, missing or renamed source")
    if any(Path(f).suffix not in (".py", *repo_graph.JS_TS_EXTENSIONS) for f in sources):
        return fallback("non-source assets/configuration need the complete configured suite")
    # Test modules can be imported as helpers by other tests: include their
    # importers too. Unknown dependencies anywhere retain the full fallback.
    graph = repo_graph.build_graph(child_root, subdirs=None, include_js=True)
    if any(f not in graph["files"] for f in sources):
        return fallback("non-source assets/configuration need the complete configured suite")
    if graph.get("uncertainties"):
        return fallback("dependency graph incomplete: " + ", ".join(graph["uncertainties"][:3]))
    if any(not repo_graph.affected_tests(graph, [f]) for f in sources):
        return fallback("at least one changed source has unknown test impact")
    impacted = sources
    if tier == "module":
        parents = {str(Path(f).parent) for f in sources}
        impacted = [f for f in graph["files"] if str(Path(f).parent) in parents]
    affected = repo_graph.affected_tests(graph, impacted)
    profile = profile if profile is not None else project_detector.detect(child_root)
    test_commands = _get_test_commands_from_profile(profile, affected, child_root)
    if not test_commands:
        return fallback("runner command cannot be safely narrowed")
    return {"tier": tier, "changed_files": changed_files, "affected_tests": affected,
            "targeted_command": " && ".join(test_commands), "full_command": False,
            "impact_status": IMPACT_TARGETED_TESTS_FOUND,
            "note": f"Iteration only: {len(affected)} test files; full regression remains required for merge/release"}


def _get_test_commands_from_profile(profile: dict, affected_tests: list, child_root=None) -> list:
    """v3.27.3 WP4: Получить команды тестов из project_detector profile для затронутых тестов.
    Не угадываем pytest/jest — берём из child config."""
    commands = []
    # Only simple declared pytest / Vitest invocations are rewritten. Shell wrappers,
    # coverage and unknown runners retain their full configured command through fallback.
    for stack in profile.get("stacks", []):
        cmd = (stack.get("commands") or {}).get("test")
        if not cmd:
            continue
        try:
            tokens = shlex.split(cmd)
        except ValueError:
            return []
        if any(c in cmd for c in ("&", ";", "|", "$", "`", "\n", ">", "<")):
            return []
        if child_root is not None and tokens in (["npm", "test"], ["npm", "run", "test"]):
            try:
                script = json.loads((Path(child_root) / "package.json").read_text()).get("scripts", {}).get("test")
            except (OSError, ValueError):
                return []
            if script != "vitest run":
                return []
            paths = [t for t in affected_tests if any(t.endswith(e) for e in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"))]
            if not paths:
                return []
            commands.append(shlex.join(tokens + ["--"] + paths))
            continue
        lang = stack.get("language", "")
        paths = [t for t in affected_tests if t.endswith(".py")] if lang == "python" else [
            t for t in affected_tests if any(t.endswith(e) for e in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"))]
        if not paths:
            # Multi-stack project: never silently drop an unaffected/unknown stack's full suite.
            return []
        if tokens and (tokens[0] == "pytest" or
                       (len(tokens) >= 3 and tokens[1:3] == ["-m", "pytest"] and
                        Path(tokens[0]).name.startswith("python"))):
            start = 1 if tokens[0] == "pytest" else 3
            out = tokens[:start]
            args = tokens[start:]
            value_options = {"-m", "-k", "-n", "--dist", "--tb", "--maxfail", "--basetemp", "-p"}
            flags = {"-q", "-v", "-vv", "-x", "-s", "--disable-warnings", "--strict-markers", "--strict-config"}
            i = 0
            while i < len(args):
                arg = args[i]
                if arg in value_options:
                    if i + 1 >= len(args):
                        return []
                    out.extend(args[i:i + 2]); i += 2; continue
                if arg in flags or any(arg.startswith(o + "=") for o in value_options):
                    out.append(arg)
                elif arg.startswith("-"):
                    return []
                # Existing positional test roots are REPLACED, not appended.
                i += 1
            commands.append(shlex.join(out + paths))
        elif tokens[:2] == ["vitest", "run"] and len(tokens) == 2:
            commands.append(shlex.join(tokens + paths))
        else:
            return []
    return commands


if __name__ == "__main__":
    # Ветки `--selftest` здесь нет (ревизия 2026-08-11): функция удалена в v3.30 вместе с
    # переносом селфтестов в pytest, а вызов остался и мог только упасть с `NameError`.

    # CLI
    changed_files = []
    tier = None
    lifecycle_intent = None
    as_json = "--json" in sys.argv

    if "--changed" in sys.argv:
        idx = sys.argv.index("--changed")
        for i in range(idx + 1, len(sys.argv)):
            if sys.argv[i].startswith("--"):
                break
            changed_files.append(sys.argv[i])

    if "--tier" in sys.argv:
        idx = sys.argv.index("--tier")
        if idx + 1 < len(sys.argv):
            tier = sys.argv[idx + 1]

    if "--intent" in sys.argv:
        idx = sys.argv.index("--intent")
        if idx + 1 < len(sys.argv):
            lifecycle_intent = sys.argv[idx + 1]

    if not changed_files:
        print("Usage: verification_tiers.py --changed file1 file2 [--tier skip|affected|module|full] [--intent explore|draft|ready_for_review|merge_candidate] [--json]")
        sys.exit(1)

    # Для CLI нужен child_root — используем текущую директорию
    child_root = str(Path.cwd())
    result = select_tests(changed_files, child_root, tier, lifecycle_intent)

    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"VERIFICATION: tier={result['tier']}, impact={result['impact_status']}")
        print(f"  Changed: {len(result['changed_files'])} файлов")
        if result["affected_tests"]:
            print(f"  Affected tests: {len(result['affected_tests'])}")
            for t in result["affected_tests"][:5]:
                print(f"    - {t}")
            if len(result["affected_tests"]) > 5:
                print(f"    ... и ещё {len(result['affected_tests']) - 5}")
        if result["targeted_command"]:
            print(f"  Command: {result['targeted_command']}")
        print(f"  Note: {result['note']}")
