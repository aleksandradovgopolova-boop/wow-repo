#!/usr/bin/env python3
"""architecture_baseline.py (v3.15.0 Architecture Baseline) — ДЕШЁВЫЙ ДЕТЕРМИНИРОВАННЫЙ снимок
архитектуры репозитория на ТОЧНОМ SHA (read-only, без модели). Не судит «хорошо/плохо» — фиксирует
НАБЛЮДАЕМЫЕ факты по осям, оставляя оценку independent architecture-reviewer'у (полный AI-review отдельно).

Оси (по спеке Architecture Baseline):
  module_map · boundaries · dependencies · api_surface · data_and_migrations · integrations ·
  failure_modes · deployment · observability · security_boundaries · adr_and_drift · risks

ЧЕСТНОСТЬ: детерминированный анализатор видит НЕ всё. Чего эвристика не обнаружила — помечается
`not_detected` (а не «нет» и не выдумка). Это baseline для сравнения во времени и вход для reviewer'а,
НЕ полный архитектурный обзор. Дорогой AI-review — отдельно (гейт architecture_review, срез B).

CLI:  architecture_baseline.py <root> [--sha SHA] [--json]   |   architecture_baseline.py --selftest
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

AXES = ("module_map", "boundaries", "dependencies", "api_surface", "data_and_migrations",
        "integrations", "failure_modes", "deployment", "observability", "security_boundaries",
        "adr_and_drift", "risks")

_SKIP_DIRS = {".git", "node_modules", ".ai", "dist", "build", "storybook-static", "__pycache__",
              ".venv", "venv", ".next", "coverage", ".idea", ".vscode"}
_CODE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte", ".go", ".rs", ".java", ".rb"}


def _git_sha(root: Path):
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, ValueError):
        return None


def _iter_files(root: Path):
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        yield p


def _read(p: Path, limit=200_000):
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _module_map(root: Path, files):
    top = {}
    for p in files:
        parts = p.relative_to(root).parts
        if len(parts) > 1 and p.suffix in _CODE_EXT:
            top[parts[0]] = top.get(parts[0], 0) + 1
    return {"top_level_code_dirs": dict(sorted(top.items(), key=lambda x: -x[1]))} if top \
        else {"top_level_code_dirs": "not_detected"}


def _boundaries(root: Path):
    hints, checked = [], {
        "FSD (feature-sliced)": ["src/features", "src/shared", "src/entities", "src/widgets"],
        "layered src": ["src/domain", "src/application", "src/infrastructure"],
        "backend/frontend split": ["backend", "frontend"],
        "python package": ["setup.py", "pyproject.toml"],
        "monorepo packages": ["packages", "apps"],
    }
    for name, paths in checked.items():
        if any((root / pth).exists() for pth in paths):
            hints.append(name)
    return {"detected": hints or "not_detected"}


def _dependencies(root: Path):
    deps = {}
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            d = json.loads(_read(pkg))
            deps["node"] = {"dependencies": len(d.get("dependencies") or {}),
                            "devDependencies": len(d.get("devDependencies") or {})}
        except (json.JSONDecodeError, ValueError):
            deps["node"] = "unparseable package.json"
    for req in ("requirements.txt", "pyproject.toml", "Pipfile"):
        if (root / req).is_file():
            deps.setdefault("python", []).append(req)
    if (root / "go.mod").is_file():
        deps["go"] = "go.mod"
    if (root / "Cargo.toml").is_file():
        deps["rust"] = "Cargo.toml"
    return deps or {"manifests": "not_detected"}


def _api_surface(root: Path, files):
    markers = {"fastapi/flask route": ("@app.", "@router.", "APIRouter("),
               "express route": ("app.get(", "app.post(", "router.get(", "router.post("),
               "openapi/swagger spec": (),
               "graphql": ("type Query", "type Mutation")}
    found = {}
    for p in files:
        if p.suffix not in _CODE_EXT:
            continue
        txt = _read(p, 40_000)
        for name, subs in markers.items():
            if subs and any(s in txt for s in subs):
                found[name] = found.get(name, 0) + 1
    for spec in ("openapi.yaml", "openapi.json", "swagger.yaml"):
        if (root / spec).is_file():
            found["openapi/swagger spec"] = spec
    return found or {"endpoints": "not_detected"}


def _data_and_migrations(root: Path, files):
    out = {}
    mig_dirs = [d for d in ("migrations", "alembic", "prisma/migrations", "db/migrate")
                if (root / d).is_dir()]
    if mig_dirs:
        out["migration_dirs"] = mig_dirs
    schema = [str(p.relative_to(root)) for p in files
              if p.name in ("schema.prisma", "schema.sql") or p.suffix == ".sql"][:10]
    if schema:
        out["schema_files"] = schema
    orm = set()
    for p in files:
        if p.suffix not in _CODE_EXT:
            continue
        t = _read(p, 20_000)
        for name, sub in (("sqlalchemy", "sqlalchemy"), ("prisma", "@prisma/client"),
                          ("typeorm", "typeorm"), ("drizzle", "drizzle-orm")):
            if sub in t:
                orm.add(name)
    if orm:
        out["orm"] = sorted(orm)
    return out or {"data_layer": "not_detected"}


def _integrations(root: Path, files):
    """Внешние интеграции по ИМЕНАМ env-переменных и SDK — секреты НЕ читаем (только факт наличия)."""
    envs = set()
    for p in files:
        if p.name.startswith(".env") or p.name in ("env.example", ".env.example"):
            for line in _read(p, 20_000).splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    envs.add(line.split("=", 1)[0])
    providers = set()
    sdk_map = {"anthropic": "anthropic", "openai": "openai", "stripe": "stripe",
               "@neondatabase": "neon", "aws-sdk": "aws", "@aws-sdk": "aws",
               "supabase": "supabase", "firebase": "firebase"}
    pkg = root / "package.json"
    reqs = _read(root / "requirements.txt", 20_000) if (root / "requirements.txt").is_file() else ""
    blob = (_read(pkg, 40_000) if pkg.is_file() else "") + reqs
    for sub, name in sdk_map.items():
        if sub in blob:
            providers.add(name)
    out = {}
    if providers:
        out["sdk_providers"] = sorted(providers)
    if envs:
        out["env_var_names"] = sorted(envs)[:40]      # ТОЛЬКО имена, без значений
    return out or {"integrations": "not_detected"}


def _scan_any(files, subs, ext=_CODE_EXT, cap=30_000):
    for p in files:
        if p.suffix not in ext:
            continue
        t = _read(p, cap)
        if any(s in t for s in subs):
            return True
    return False


def _failure_modes(root, files):
    out = {}
    out["error_handling_present"] = _scan_any(files, ("try:", "except ", "try {", "catch (", ".catch("))
    out["retry_or_timeout_present"] = _scan_any(files, ("retry", "timeout", "backoff", "circuit"))
    return out


def _deployment(root: Path):
    hits = [f for f in ("Dockerfile", "docker-compose.yml", "docker-compose.yaml", "Procfile",
                        "vercel.json", "netlify.toml", "fly.toml", "render.yaml", "Makefile")
            if (root / f).is_file()]
    ci = []
    if (root / ".github" / "workflows").is_dir():
        ci = [p.name for p in (root / ".github" / "workflows").glob("*.y*ml")][:10]
    out = {}
    if hits:
        out["deploy_configs"] = hits
    if ci:
        out["ci_workflows"] = ci
    return out or {"deployment": "not_detected"}


def _observability(root, files):
    return {"logging_present": _scan_any(files, ("logging", "logger", "console.log", "winston", "pino")),
            "metrics_or_tracing_present": _scan_any(files, ("prometheus", "opentelemetry", "sentry",
                                                            "datadog", "metrics"))}


def _security_boundaries(root, files):
    return {"authn_authz_present": _scan_any(files, ("jwt", "oauth", "authenticate", "authorize",
                                                     "@login_required", "getServerSession", "passport")),
            "secrets_via_env": _scan_any(files, ("os.environ", "process.env", "getenv")),
            "input_validation_present": _scan_any(files, ("pydantic", "zod", "joi", "validator",
                                                          "schema.validate"))}


def _adr_and_drift(root: Path):
    out = {}
    for d in ("decisions", "docs/adr", "docs/decisions", "adr", ".research/decisions"):
        if (root / d).exists():
            out.setdefault("adr_locations", []).append(d)
    reg = root / "decisions" / "registry.yaml"
    if reg.is_file():
        out["decision_registry"] = "decisions/registry.yaml"
    return out or {"adr": "not_detected"}


def _risks(baseline: dict):
    """Производные РИСКИ из наблюдаемых фактов — честные сигналы «стоит посмотреть», не вердикты."""
    r = []
    if baseline["adr_and_drift"].get("adr") == "not_detected":
        r.append("нет ADR/реестра решений — архитектурные решения не зафиксированы (drift невидим)")
    if baseline["data_and_migrations"].get("migration_dirs") and \
            not baseline["security_boundaries"].get("input_validation_present"):
        r.append("есть миграции/данные, но валидация ввода не обнаружена — проверить границу данных")
    if baseline["api_surface"].get("endpoints") != "not_detected" and \
            not baseline["security_boundaries"].get("authn_authz_present"):
        r.append("обнаружены API-эндпоинты без явной authn/authz — проверить защиту поверхности")
    if not baseline["failure_modes"].get("error_handling_present"):
        r.append("обработка ошибок не обнаружена — проверить режимы отказа")
    if baseline["deployment"].get("deployment") == "not_detected":
        r.append("конфигурация деплоя не обнаружена — как и куда катится продукт?")
    return r or ["явных детерминированных рисков не обнаружено (не заменяет AI-review)"]


def analyze(root, sha=None):
    """Детерминированный снимок архитектуры на текущем дереве, помеченный SHA (read-only)."""
    root = Path(root)
    files = list(_iter_files(root))
    b = {
        "kind": "ArchitectureBaseline", "schema_version": 1,
        "root": str(root), "sha": sha or _git_sha(root) or "unknown",
        "analyzer": "deterministic", "file_count": len(files),
        "module_map": _module_map(root, files),
        "boundaries": _boundaries(root),
        "dependencies": _dependencies(root),
        "api_surface": _api_surface(root, files),
        "data_and_migrations": _data_and_migrations(root, files),
        "integrations": _integrations(root, files),
        "failure_modes": _failure_modes(root, files),
        "deployment": _deployment(root),
        "observability": _observability(root, files),
        "security_boundaries": _security_boundaries(root, files),
        "adr_and_drift": _adr_and_drift(root),
    }
    b["risks"] = _risks(b)
    return b


def check(b):
    """Валидация формы baseline + честности (все оси присутствуют; sha помечен)."""
    e = []
    if not isinstance(b, dict) or b.get("kind") != "ArchitectureBaseline":
        return ["kind должен быть ArchitectureBaseline"]
    for ax in AXES:
        if ax not in b:
            e.append(f"нет оси {ax}")
    if not b.get("sha"):
        e.append("baseline не помечен sha (read-only на точном SHA — обязателен)")
    return e


def _fmt(b):
    L = [f"=== Architecture Baseline (детерминированный) — {b['root']} @ {b['sha'][:12]} ===",
         f"файлов проанализировано: {b['file_count']}"]
    for ax in AXES:
        L.append(f"\n▸ {ax}:")
        v = b[ax]
        if isinstance(v, list):
            for it in v:
                L.append(f"    - {it}")
        elif isinstance(v, dict):
            for k, val in v.items():
                L.append(f"    {k}: {val}")
        else:
            L.append(f"    {v}")
    L.append("\n(детерминированный baseline — НЕ полный AI-review; глубокую оценку выносит "
             "architecture-reviewer на гейте architecture_review при архитектурных сигналах.)")
    return "\n".join(L)


def diff_baselines(baseline_old, baseline_new):
    """v3.24.0 Architecture Baseline Drift Detection: сравнить два baseline (old vs new) по осям.
    Возвращает {axis: {added, removed, changed}} для каждой оси, где есть различия.
    Пустой dict = нет дрейфа. Используется для обновления baseline при значимых изменениях."""
    drift = {}
    for axis in AXES:
        old_data = baseline_old.get(axis, {})
        new_data = baseline_new.get(axis, {})
        if old_data == new_data:
            continue
        # Простое сравнение: что добавилось, что исчезло, что изменилось
        old_keys = set(old_data.keys()) if isinstance(old_data, dict) else set()
        new_keys = set(new_data.keys()) if isinstance(new_data, dict) else set()
        added = new_keys - old_keys
        removed = old_keys - new_keys
        changed = {k for k in old_keys & new_keys if old_data.get(k) != new_data.get(k)}
        if added or removed or changed:
            drift[axis] = {"added": sorted(added), "removed": sorted(removed), "changed": sorted(changed)}
    return drift


def architecture_signals_from_diff(changed_files, baseline=None):
    """v3.24.0 автоматически выводить architecture signals из списка изменённых файлов.
    Используется для определения, нужен ли architecture_review gate.
    -> set of signal names (architecture_change, new_service, cross_boundary_change, breaking_api,
    data_migration, new_integration, deployment_change)."""
    signals = set()
    if not changed_files:
        return signals
    for f in changed_files:
        fp = str(f)
        # API changes
        if any(p in fp for p in ("api/", "routes", "endpoints", "graphql", "proto/")):
            signals.add("breaking_api")
        # Data/migrations
        if any(p in fp for p in ("migrations/", "schema", "models/", "entities/")):
            signals.add("data_migration")
        # Deployment
        if any(p in fp for p in ("deploy", "docker", "k8s/", "terraform/", "infrastructure/",
                                  ".github/workflows", "Dockerfile", "docker-compose")):
            signals.add("deployment_change")
        # New service/integration
        if any(p in fp for p in ("services/", "integrations/", "adapters/", "clients/")):
            signals.add("new_integration")
            signals.add("new_service")
        # Cross-boundary
        if any(p in fp for p in ("shared/", "contracts/", "interfaces/", "boundary/")):
            signals.add("cross_boundary_change")
        # General architecture change
        if any(p in fp for p in ("architecture/", "core/", "domain/", "kernel/")):
            signals.add("architecture_change")
    # Если baseline доступен, проверяем, изменились ли границы/модули
    if baseline and signals:
        # Если есть drift в boundaries или module_map — усиливаем сигнал
        pass  # baseline comparison handled separately via diff_baselines()
    return signals


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        b = analyze(td, sha="deadbeef")
        expect("все 12 осей присутствуют", all(ax in b for ax in AXES))
        expect("check валиден на пустом дереве", check(b) == [])
        expect("пустой репо -> module_map not_detected",
               b["module_map"]["top_level_code_dirs"] == "not_detected")
        expect("пустой репо -> риск про отсутствие ADR",
               any("ADR" in r for r in b["risks"]))
        expect("sha помечен (read-only на SHA)", b["sha"] == "deadbeef")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "src" / "features").mkdir(parents=True)
        (root / "src" / "shared").mkdir(parents=True)
        (root / "src" / "features" / "api.ts").write_text(
            "import { z } from 'zod';\nconst r = router.get('/x', () => {});\n", encoding="utf-8")
        (root / "package.json").write_text(
            '{"dependencies":{"anthropic":"^1","zod":"^3"},"devDependencies":{"vitest":"^1"}}',
            encoding="utf-8")
        (root / "migrations").mkdir()
        (root / "Dockerfile").write_text("FROM node", encoding="utf-8")
        b = analyze(td, sha="abc123")
        expect("FSD boundary детектится", "FSD (feature-sliced)" in b["boundaries"]["detected"])
        expect("node deps посчитаны", b["dependencies"]["node"]["dependencies"] == 2)
        expect("express route детектится", "express route" in b["api_surface"])
        expect("миграции детектятся", "migrations" in b["data_and_migrations"]["migration_dirs"])
        expect("anthropic SDK-интеграция детектится", "anthropic" in b["integrations"]["sdk_providers"])
        expect("Dockerfile -> deployment", "Dockerfile" in b["deployment"]["deploy_configs"])
        expect("zod -> input_validation_present", b["security_boundaries"]["input_validation_present"])
        expect("check валиден на реальном дереве", check(b) == [])

    # честность: не выдумывает секреты — только имена env-переменных
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / ".env.example").write_text("ANTHROPIC_API_KEY=sk-REAL-SECRET-VALUE\nDB_URL=x\n", encoding="utf-8")
        b = analyze(td)
        names = b["integrations"].get("env_var_names", [])
        expect("env: только имена, значение секрета не утекает",
               "ANTHROPIC_API_KEY" in names and not any("sk-REAL" in str(x) for x in names))

    print("architecture_baseline selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    sha = None
    args, it = [], iter(argv)
    for a in it:
        if a == "--sha":
            sha = next(it, None)
        elif not a.startswith("--"):
            args.append(a)
    root = args[0] if args else "."
    b = analyze(root, sha=sha)
    if "--json" in argv:
        print(json.dumps(b, ensure_ascii=False, indent=2))
    else:
        print(_fmt(b))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
