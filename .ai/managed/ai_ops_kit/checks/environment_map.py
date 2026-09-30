#!/usr/bin/env python3
"""environment_map.py (v3.20.0 Engineering Operating Model, WP4) — честная карта окружений продукта.

Аудит: у кита не было понятия «окружение» вообще. Гейты рассуждали о коде и ревью, а вопросы «куда это
поедет», «кто вправе туда деплоить», «какие секреты там нужны» не задавал никто — при том что именно на
этом шве живут самые дорогие ошибки. Срез 2 вводит окружения как ДАННЫЕ: read-only детерминированный
снимок того, что объявлено в конфиге и что реально видно в репозитории.

Ключевая ценность — не список, а РАСХОЖДЕНИЯ между объявленным и обнаруженным:
  detected_not_declared   CI деплоит в окружение, которого никто не объявлял (самое опасное:
                          поставка идёт туда, где нет ни владельца, ни правил доступа);
  declared_not_detected   окружение объявлено, но в репозитории нет ни одного его признака
                          (документация рассказывает о продукте, которого нет).

СЕКРЕТЫ: модуль работает ТОЛЬКО с ИМЕНАМИ (`ANTHROPIC_API_KEY`), никогда со значениями. Значения не
читаются, не логируются и не попадают в отчёт ни при каких условиях — это проверяется selftest'ом.
Поиск утечек в содержимом — не здесь, а в ai_ops_kit/security/security_scan.py.

Честность: отсутствие окружений — явный статус `not_detected`, а не «ок». Kind окружения выводится из
имени детерминированно; неузнанное имя даёт `unknown`, а не угаданный `production`.

CLI:  environment_map.py <root> [--json]
      environment_map.py --selftest
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

KINDS = ("local", "ci", "preview", "staging", "production", "unknown")

# Имя -> kind. Порядок важен: сначала более специфичные подстроки.
_KIND_HINTS = (
    ("prod", "production"), ("release", "production"), ("live", "production"),
    ("stag", "staging"), ("preprod", "staging"), ("uat", "staging"), ("qa", "staging"),
    ("preview", "preview"), ("pr-", "preview"), ("review", "preview"),
    ("dev", "local"), ("local", "local"), ("test", "ci"), ("ci", "ci"),
)

_SECRET_REF_RE = re.compile(r"secrets\.([A-Z_][A-Z0-9_]*)")
_ENV_FILE_RE = re.compile(r"^\.env\.([A-Za-z0-9_-]+)$")
_ENV_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=")
# Имена файлов .env, которые НЕ являются окружением (примеры/шаблоны).
_ENV_FILE_SKIP = ("example", "sample", "template", "dist", "local")


def kind_of(name) -> str:
    """Тип окружения по имени. Неузнанное -> 'unknown' (не угадываем production)."""
    n = str(name or "").strip().lower()
    if not n:
        return "unknown"
    for hint, kind in _KIND_HINTS:
        if hint in n:
            return kind
    return "unknown"


def _declared(root: Path):
    """Окружения из `engineering_operating_model.environments` в .ai-ops.yaml. -> {name: decl}."""
    for fname in (".ai-ops.yaml", ".ai-ops.yml"):
        cfg = root / fname
        if not cfg.is_file():
            continue
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 — битый конфиг не роняет снимок
            return {}
        envs = ((data.get("engineering_operating_model") or {}).get("environments") or [])
        out = {}
        if isinstance(envs, list):
            for e in envs:
                if isinstance(e, dict) and e.get("name"):
                    out[str(e["name"])] = e
                elif isinstance(e, str):
                    out[e] = {"name": e}
        return out
    return {}


def _workflow_files(root: Path):
    wf = root / ".github" / "workflows"
    if not wf.is_dir():
        return []
    return sorted([p for p in wf.iterdir()
                   if p.is_file() and p.suffix in (".yml", ".yaml")])


def _detect_from_ci(root: Path):
    """Окружения из `environment:` в GitHub Actions + ИМЕНА секретов из ${{ secrets.X }}.

    -> ({name: [sources]}, {secret_name: [sources]})
    """
    envs, secrets = {}, {}
    for p in _workflow_files(root):
        try:
            raw = p.read_text(encoding="utf-8")
        except OSError:
            continue
        rel = f".github/workflows/{p.name}"
        for m in _SECRET_REF_RE.finditer(raw):        # только ИМЕНА, значения недоступны в принципе
            secrets.setdefault(m.group(1), []).append(rel)
        try:
            data = yaml.safe_load(raw) or {}
        except yaml.YAMLError:
            continue                                  # нечитаемый workflow не роняет снимок
        if not isinstance(data, dict):
            continue
        for job_id, job in (data.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            env = job.get("environment")
            names = []
            if isinstance(env, str):
                names = [env]
            elif isinstance(env, dict) and env.get("name"):
                names = [str(env["name"])]
            elif isinstance(env, list):
                names = [str(e.get("name")) if isinstance(e, dict) else str(e) for e in env]
            for n in [x for x in names if x]:
                envs.setdefault(n, []).append(f"{rel}:{job_id}")
    return envs, secrets


def _detect_from_env_files(root: Path):
    """Окружения из файлов `.env.<name>` (кроме примеров/шаблонов). -> {name: [sources]}."""
    out = {}
    try:
        entries = sorted(p for p in root.iterdir() if p.is_file())
    except OSError:
        return out
    for p in entries:
        m = _ENV_FILE_RE.match(p.name)
        if m and m.group(1).lower() not in _ENV_FILE_SKIP:
            out.setdefault(m.group(1), []).append(p.name)
    return out


def _secret_names_from_example(root: Path):
    """ИМЕНА переменных из .env.example/.env.sample. Значения игнорируются полностью."""
    out = {}
    for name in (".env.example", ".env.sample", ".env.template"):
        p = root / name
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = _ENV_KEY_RE.match(line)
            if m:                                     # берём ТОЛЬКО имя до '='
                out.setdefault(m.group(1), []).append(name)
    return out


def assess(root):
    """Read-only снимок окружений. -> EnvironmentMap (dict)."""
    root = Path(root)
    declared = _declared(root)
    ci_envs, ci_secrets = _detect_from_ci(root)
    file_envs = _detect_from_env_files(root)
    example_secrets = _secret_names_from_example(root)

    detected = {}
    for src_map in (ci_envs, file_envs):
        for name, sources in src_map.items():
            detected.setdefault(name, []).extend(sources)

    environments, findings = [], []
    for name in sorted(set(declared) | set(detected)):
        decl = declared.get(name) or {}
        is_decl, is_det = name in declared, name in detected
        status = ("declared_and_detected" if is_decl and is_det
                  else "declared_not_detected" if is_decl else "detected_not_declared")
        approvers = [str(a) for a in (decl.get("approvers") or []) if a]
        env_kind = str(decl.get("kind") or "").strip().lower() or kind_of(name)
        if env_kind not in KINDS:
            env_kind = "unknown"
        environments.append({
            "name": name, "kind": env_kind, "status": status,
            "declared": is_decl, "detected": is_det,
            "sources": sorted(set(detected.get(name, []))),
            "approvers": approvers,
            "deploy_ref": decl.get("deploy_ref"),
        })
        if status == "detected_not_declared":
            findings.append({"rule": "detected_not_declared", "environment": name,
                             "detail": f"окружение '{name}' обнаружено в репозитории "
                                       f"({', '.join(sorted(set(detected[name]))[:3])}), но не объявлено "
                                       f"в engineering_operating_model.environments — у поставки туда нет "
                                       f"ни владельца, ни правил доступа"})
        elif status == "declared_not_detected":
            findings.append({"rule": "declared_not_detected", "environment": name,
                             "detail": f"окружение '{name}' объявлено, но в репозитории нет ни одного "
                                       f"его признака (ни CI-environment, ни .env.{name})"})
        if env_kind == "production" and is_decl and not approvers:
            findings.append({"rule": "production_without_approvers", "environment": name,
                             "detail": f"'{name}' — production, но approvers не объявлены: кто вправе "
                                       f"туда деплоить, неизвестно"})

    secret_names = {}
    for src_map in (ci_secrets, example_secrets):
        for n, sources in src_map.items():
            secret_names.setdefault(n, []).extend(sources)

    return {
        "kind": "EnvironmentMap",
        "environments_status": "not_detected" if not environments else "detected",
        "environments": environments,
        # ТОЛЬКО имена. Значений здесь нет и быть не может.
        "secret_names": sorted(secret_names),
        "secret_sources": {k: sorted(set(v)) for k, v in sorted(secret_names.items())},
        "findings": findings,
        "counts": {"declared": len(declared), "detected": len(detected),
                   "total": len(environments), "secret_names": len(secret_names)},
    }


def _fmt(rep):
    lines = []
    if rep["environments_status"] == "not_detected":
        lines.append("окружения: not_detected — ни объявленных, ни обнаруженных "
                     "(честный статус, не «ок»)")
    else:
        c = rep["counts"]
        lines.append(f"окружения: {c['total']} (объявлено {c['declared']}, обнаружено {c['detected']}); "
                     f"имён секретов: {c['secret_names']}")
        for e in rep["environments"]:
            mark = {"declared_and_detected": "✓", "declared_not_detected": "⚠",
                    "detected_not_declared": "✗"}[e["status"]]
            src = f" ← {', '.join(e['sources'][:2])}" if e["sources"] else ""
            lines.append(f"  {mark} {e['name']} [{e['kind']}] {e['status']}{src}")
    for f in rep["findings"]:
        lines.append(f"  ! {f['rule']}: {f['detail']}")
    return "\n".join(lines)


def summary_line(root):
    """Строка для doctor. Ничего не запускает и не меняет."""
    try:
        rep = assess(root)
    except Exception as e:  # noqa: BLE001 — снимок окружений не роняет doctor
        return f"окружения: недоступно ({e})"
    if rep["environments_status"] == "not_detected":
        return "окружения: not_detected — ни объявленных, ни обнаруженных (не маскируем)"
    bad = [f for f in rep["findings"] if f["rule"] == "detected_not_declared"]
    c = rep["counts"]
    tail = (f"; ✗ не объявлено: {', '.join(sorted({f['environment'] for f in bad}))}" if bad else "")
    return (f"окружения: {c['total']} (объявлено {c['declared']}, обнаружено {c['detected']}), "
            f"имён секретов {c['secret_names']}{tail}")


def main(argv):
    root = argv[0] if argv and not argv[0].startswith("--") else "."
    rep = assess(root)
    print(json.dumps(rep, ensure_ascii=False, indent=2) if "--json" in argv else _fmt(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
