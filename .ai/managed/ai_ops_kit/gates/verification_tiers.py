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
    "registry/", "schemas/", "quality/gates.yaml",
    "tools/orchestrator.py", "tools/execution_pipeline.py",
    "tools/preflight.py", "tools/gate_executor.py",
    "tools/tool_broker.py", "tools/lifecycle_store.py",
)

# Файлы, которые не требуют verification (документация, конфиги)
SKIP_VERIFICATION_PATTERNS = (
    "*.md", "*.txt", "LICENSE", "VERSION",
    ".gitignore", ".dockerignore",
    "docs/", "context/", "decisions/",
)

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
    if force_tier and force_tier in TIERS:
        return force_tier

    # v3.27.3 WP4: lifecycle_intent определяет tier
    if lifecycle_intent and lifecycle_intent in INTENT_TO_TIER:
        return INTENT_TO_TIER[lifecycle_intent]

    if not changed_files:
        return "skip"  # v3.27.3 WP4: нет изменений — skip

    # v3.27.3 WP4: docs-only → skip (не запускаем product build/test)
    all_skip = all(_matches_any(f, SKIP_VERIFICATION_PATTERNS) for f in changed_files)
    if all_skip:
        return "skip"

    # Критическая инфраструктура -> full
    for f in changed_files:
        if _matches_any(f, ALWAYS_FULL_PATTERNS):
            return "full"

    # Много файлов (>20) -> module
    if len(changed_files) > 20:
        return "module"

    # По умолчанию — affected
    return "affected"


# ============================================================================
# Test Selection Engine
# ============================================================================

def select_tests(changed_files: list, child_root: str, tier: str = None, lifecycle_intent: str = None) -> dict:
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
            "note": "Skip verification: только документация/конфиги",
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

    # Строим граф
    graph = repo_graph.build_graph(child_root, subdirs=None, include_js=True)

    # Находим затронутые тесты
    affected = repo_graph.affected_tests(graph, changed_files)

    # v3.27.3 WP4: impact status — честный статус влияния
    if not affected:
        return {
            "tier": tier,
            "changed_files": changed_files,
            "affected_tests": [],
            "targeted_command": None,
            "full_command": False,
            "impact_status": IMPACT_TARGETED_TESTS_NOT_FOUND,  # impact_unknown, не "не влияет"
            "note": "Impact unknown: граф не нашёл затронутых тестов (может быть dynamic import, fixture, generated code)",
        }

    # v3.27.3 WP4: команды из project_detector (child config), не угадываем
    profile = project_detector.detect(child_root)
    test_commands = _get_test_commands_from_profile(profile, affected)

    return {
        "tier": tier,
        "changed_files": changed_files,
        "affected_tests": affected,
        "targeted_command": " && ".join(test_commands) if test_commands else None,
        "full_command": False,
        "impact_status": IMPACT_TARGETED_TESTS_FOUND,
        "note": f"{tier.capitalize()} verification: {len(affected)} тестов затронуто",
    }


def _get_test_commands_from_profile(profile: dict, affected_tests: list) -> list:
    """v3.27.3 WP4: Получить команды тестов из project_detector profile для затронутых тестов.
    Не угадываем pytest/jest — берём из child config."""
    commands = []
    for stack in profile.get("stacks", []):
        lang = stack.get("language", "")
        test_cmd = (stack.get("commands") or {}).get("test")
        if not test_cmd:
            continue

        # Фильтруем затронутые тесты по языку
        if lang == "python":
            py_tests = [t for t in affected_tests if t.endswith(".py")]
            if py_tests:
                # Модифицируем команду: добавляем конкретные файлы
                # Если команда содержит 'pytest', добавляем файлы
                if "pytest" in test_cmd:
                    commands.append(f"{test_cmd} {' '.join(py_tests)}")
                else:
                    commands.append(test_cmd)  # используем как есть
        elif lang in ("javascript", "typescript", "node"):
            js_tests = [t for t in affected_tests if any(t.endswith(ext) for ext in
                        (".test.js", ".test.ts", ".test.jsx", ".test.tsx", ".spec.js", ".spec.ts"))]
            if js_tests:
                # Для Jest/Vitest используем --testPathPattern
                if "jest" in test_cmd or "vitest" in test_cmd:
                    patterns = "|".join(Path(t).stem for t in js_tests)
                    commands.append(f"{test_cmd} --testPathPattern=\"{patterns}\"")
                else:
                    commands.append(test_cmd)  # используем как есть
        else:
            # Другие языки — используем команду как есть
            commands.append(test_cmd)

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
