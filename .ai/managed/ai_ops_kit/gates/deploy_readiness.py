#!/usr/bin/env python3
"""deploy_readiness.py (v3.20.0 Engineering Operating Model, WP5) — ЧЕСТНАЯ зрелость поставки.

Кит НЕ деплоит и не собирается: он не даёт **врать о готовности деплоя**. Лестница — та же, что у
UI-evidence (tools/ui_readiness.py), потому что болезнь та же: «у нас всё готово» без единого
проверяемого признака.

  absent      — ни объявленных окружений, ни признаков поставки в репозитории;
  configured  — признаки есть (Dockerfile/compose/платформенный конфиг/объявленное окружение),
                но ИСПОЛНЯЕМОГО пути деплоя нет — задеплоить нечем;
  runnable    — исполняемый путь есть (CI-job с environment и шагами, либо deploy-скрипт) ->
                поставку можно произвести;
  verified    — runnable + evidence фактической поставки (DeployRecord в .ai/runtime/deploy/)
                И объявленный откат. Без отката «verified» не бывает: поставка, которую нельзя
                отменить, — не готовность, а риск.

`absent` НЕ маскируется под «ок»: для библиотеки или CLI это норма, и гейт применяется только при
сигнале изменения поставки (`should_run_deploy_readiness`), а не на каждой задаче.

Шаблон кит предлагает только ДЕКЛАРАТИВНЫЙ (блок в .ai-ops.yaml) — писать чужой deploy-скрипт он не
вправе: последствия несёт владелец.

CLI:  deploy_readiness.py <root> [--json]
      deploy_readiness.py --selftest
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

# v3.38 (K4): environment_map загружается лениво — gates не импортирует engops статически.
def _env_assess(root):
    _em = __import__("ai_ops_kit.engops.environment_map", fromlist=["assess"])
    return _em.assess(root)

MATURITY = ("absent", "configured", "runnable", "verified")

# Признаки того, что поставка вообще существует как замысел (но ещё не обязательно исполнима).
_CONFIG_MARKERS = ("Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yaml",
                   "vercel.json", "netlify.toml", "fly.toml", "render.yaml", "Procfile",
                   "app.yaml", "railway.json", "captain-definition")
# Конфиги, которые ЧАСТО (но не обязательно) связаны с внешней платформой поставки. Их наличие —
# ПОДСКАЗКА, а НЕ доказательство: файл может быть наследием или использоваться не для деплоя.
# Инструмент не вправе заявлять, что поставку ведёт эта платформа — он этого не знает.
_PLATFORM_HINT_MARKERS = ("vercel.json", "netlify.toml", "fly.toml", "render.yaml", "railway.json",
                          "app.yaml", "captain-definition")
_CONFIG_DIRS = ("k8s", "kubernetes", "helm", "charts", "terraform", "deploy", "infra", "infrastructure")

# Файлы, изменение которых означает «поставка меняется» -> гейт применим.
_DEPLOY_PATH_HINTS = (".github/workflows/", "dockerfile", "docker-compose", "compose.yaml",
                      "vercel.json", "netlify.toml", "fly.toml", "render.yaml", "procfile",
                      "k8s/", "kubernetes/", "helm/", "charts/", "terraform/", "deploy/",
                      "infra/", "infrastructure/", ".env.")
_DEPLOY_EXT = (".tf", ".tfvars")
_DEPLOY_SIGNALS = ("deployment_change", "new_service", "infrastructure_change")

_DEPLOY_SCRIPT_RE = re.compile(r"(^|/)(deploy|release|publish|ship)[-_.a-z0-9]*\.(sh|py|ts|js|mjs)$",
                               re.IGNORECASE)
_ROLLBACK_SCRIPT_RE = re.compile(r"(^|/)(rollback|revert|undeploy)[-_.a-z0-9]*\.(sh|py|ts|js|mjs)$",
                                 re.IGNORECASE)


def should_run_deploy_readiness(changed_files, signals=None):
    """Гейт поставки применим ТОЛЬКО при сигнале деплоя ИЛИ изменении файла поставки. -> (bool, reason)."""
    s = dict(signals or {})
    for sig in _DEPLOY_SIGNALS:
        if s.get(sig) is True:
            return True, f"сигнал {sig}"
    if str(s.get("task_type") or "").upper() == "DEPLOY":
        return True, "DEPLOY-задача"
    for f in (changed_files or []):
        fl = str(f).lower()
        if fl.endswith(_DEPLOY_EXT) or any(h in ("/" + fl) or fl.startswith(h)
                                           for h in _DEPLOY_PATH_HINTS):
            return True, f"изменён файл поставки: {f}"
    return False, ("нет сигналов деплоя и изменений файлов поставки — гейт неприменим "
                   "(honest skip, не маскируем)")


def _deploy_config(root: Path):
    """`engineering_operating_model.deploy` из .ai-ops.yaml. -> dict (возможно пустой)."""
    for fname in (".ai-ops.yaml", ".ai-ops.yml"):
        cfg = root / fname
        if not cfg.is_file():
            continue
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            return {}
        d = (data.get("engineering_operating_model") or {}).get("deploy") or {}
        return d if isinstance(d, dict) else {}
    return {}


def _config_markers(root: Path):
    """Признаки замысла поставки. -> список относительных путей."""
    found = []
    for name in _CONFIG_MARKERS:
        if (root / name).is_file():
            found.append(name)
    for d in _CONFIG_DIRS:
        p = root / d
        if p.is_dir():
            try:
                if any(p.iterdir()):
                    found.append(f"{d}/")
            except OSError:
                pass
    return sorted(found)


def _package_scripts(root: Path):
    p = root / "package.json"
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return scripts if isinstance(scripts, dict) else {}


def _repo_scripts(root: Path):
    """Скрипты поставки/откатa в репозитории. -> (deploy_paths, rollback_paths)."""
    deploy, rollback = [], []
    roots = [root] + [root / d for d in ("scripts", "bin", "tools", "deploy") if (root / d).is_dir()]
    for base in roots:
        try:
            entries = sorted(p for p in base.iterdir() if p.is_file())
        except OSError:
            continue
        for p in entries:
            rel = str(p.relative_to(root)).replace("\\", "/")
            if _DEPLOY_SCRIPT_RE.search("/" + rel):
                deploy.append(rel)
            if _ROLLBACK_SCRIPT_RE.search("/" + rel):
                rollback.append(rel)
    return sorted(set(deploy)), sorted(set(rollback))


def _ci_deploy_jobs(root: Path):
    """CI-jobs с `environment:` И непустыми шагами = исполняемый путь поставки."""
    out = []
    wf = root / ".github" / "workflows"
    if not wf.is_dir():
        return out
    for p in sorted(x for x in wf.iterdir() if x.is_file() and x.suffix in (".yml", ".yaml")):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        for job_id, job in (data.get("jobs") or {}).items():
            if not isinstance(job, dict) or not job.get("environment"):
                continue
            steps = job.get("steps")
            if isinstance(steps, list) and steps:
                out.append(f".github/workflows/{p.name}:{job_id}")
    return sorted(out)


def _deploy_records(root: Path):
    """Evidence фактических поставок: .ai/runtime/deploy/*.json. -> список путей."""
    d = root / ".ai" / "runtime" / "deploy"
    if not d.is_dir():
        return []
    try:
        return sorted(str(p.relative_to(root)).replace("\\", "/")
                      for p in d.iterdir() if p.is_file() and p.suffix == ".json")
    except OSError:
        return []


def assess(child_root):
    """Зрелость поставки. -> DeployReadiness (dict). Ничего не запускает и не меняет."""
    root = Path(child_root)
    cfg = _deploy_config(root)
    env_map = _env_assess(root)
    declared_envs = [e for e in env_map["environments"] if e["declared"]]

    markers = _config_markers(root)
    deploy_scripts, rollback_scripts = _repo_scripts(root)
    pkg = _package_scripts(root)
    pkg_deploy = sorted(n for n in pkg if re.fullmatch(r"(deploy|release|publish|ship)(:.+)?", n))
    pkg_rollback = sorted(n for n in pkg if re.fullmatch(r"(rollback|revert|undeploy)(:.+)?", n))
    ci_jobs = _ci_deploy_jobs(root)
    records = _deploy_records(root)

    runnable_paths = ci_jobs + deploy_scripts + [f"package.json:scripts.{n}" for n in pkg_deploy]
    if cfg.get("deploy_command"):
        runnable_paths.append("config:deploy_command")

    rollback_declared = bool(cfg.get("rollback")) or bool(rollback_scripts) or bool(pkg_rollback)
    rollback_sources = ([f"config:rollback"] if cfg.get("rollback") else []) + rollback_scripts \
        + [f"package.json:scripts.{n}" for n in pkg_rollback]

    has_intent = bool(markers or declared_envs or env_map["environments"])
    if runnable_paths and records and rollback_declared:
        maturity = "verified"
    elif runnable_paths:
        maturity = "runnable"
    elif has_intent:
        maturity = "configured"
    else:
        maturity = "absent"

    platform_hints = [m for m in markers if m in _PLATFORM_HINT_MARKERS]
    # ЧТО ИМЕННО МЫ ЗНАЕМ: исполняемого пути поставки в репозитории нет. Происходит ли поставка вне
    # репозитория и чем именно — НЕИЗВЕСТНО. Не выдаём подсказку за вывод.
    deploy_path_unknown = not runnable_paths and bool(markers)

    reason = {
        "absent": "Ни объявленных окружений, ни признаков поставки. Для библиотеки/CLI это НОРМА — "
                  "не маскируем и не требуем. Объявите окружения, если поставка есть (declaration_template).",
        "configured": (
            "Исполняемого пути поставки в репозитории НЕТ. Значит одно из двух, и различить это по коду "
            "нельзя: поставки нет вовсе, либо она производится вне репозитория."
            + (f" Найдены конфиги, которые ИНОГДА связаны с внешней поставкой: "
               f"{', '.join(platform_hints)} — но из кода НЕ следует, что деплой идёт через них "
               f"(файл может быть наследием)." if platform_hints else "")
            + " Объявите deploy_command и rollback в .ai-ops.yaml — тогда поставка станет описанной "
              "и воспроизводимой."),
        "runnable": "Поставку можно произвести. До verified не хватает: "
                    + ("evidence фактической поставки (.ai/runtime/deploy/*.json)" if not records else "")
                    + ("; " if not records and not rollback_declared else "")
                    + ("объявленного отката (rollback)" if not rollback_declared else "") + ".",
        "verified": "Поставка исполнима, есть evidence фактических поставок и объявленный откат.",
    }[maturity]

    findings = list(env_map["findings"])
    if deploy_path_unknown:
        findings.append({"rule": "deploy_path_unknown",
                         "detail": "чем и как производится поставка — из репозитория НЕ следует"
                                   + (f" (найдено: {', '.join(platform_hints)} — это лишь подсказка, "
                                      f"не доказательство)" if platform_hints else "")
                                   + "; значит нельзя ни воспроизвести её, ни откатить"})
    if runnable_paths and not rollback_declared:
        findings.append({"rule": "no_rollback_declared",
                         "detail": "поставка исполнима, но откат не объявлен — поставка, которую нельзя "
                                   "отменить, это риск, а не готовность"})
    if records and not runnable_paths:
        findings.append({"rule": "records_without_path",
                         "detail": "есть записи о поставках, но исполняемого пути в репозитории нет — "
                                   "деплой происходит вне репозитория и не воспроизводим"})

    return {
        "kind": "DeployReadiness",
        "deploy_maturity": maturity,
        "reason": reason,
        "environments_status": env_map["environments_status"],
        "environments": [e["name"] for e in env_map["environments"]],
        "config_markers": markers,
        "deploy_path_unknown": deploy_path_unknown,
        "platform_hints": platform_hints,
        "runnable_paths": sorted(set(runnable_paths)),
        "rollback_declared": rollback_declared,
        "rollback_sources": sorted(set(rollback_sources)),
        "deploy_records": records,
        "secret_names": env_map["secret_names"],
        "findings": findings,
    }


def gate_status(maturity, applicable=True):
    """Маппинг лестницы в вердикт детерминированного гейта. -> (status, note).

    Гейт применяется только при сигнале деплоя; в этом контексте `configured`/`absent` — провал:
    поставка ЗАЯВЛЕНА как изменяемая, а произвести её нечем.
    """
    if not applicable:
        return "not_applicable", "гейт неприменим: нет сигналов деплоя"
    if maturity == "verified":
        return "pass", "поставка исполнима, есть evidence и откат"
    if maturity == "runnable":
        return "pass", "поставка исполнима; нет evidence фактической поставки и/или объявленного отката"
    if maturity == "configured":
        return "fail", "замысел поставки есть, исполняемого пути нет — задеплоить нечем"
    return "fail", "изменение поставки заявлено, а признаков поставки в репозитории нет"


def declaration_template():
    """Шаблон декларации для .ai-ops.yaml. Кит НЕ пишет чужой deploy-скрипт."""
    return (
        "engineering_operating_model:\n"
        "  environments:\n"
        "    - {name: staging, kind: staging, approvers: [team-lead]}\n"
        "    - {name: production, kind: production, approvers: [owner], deploy_ref: .github/workflows/deploy.yml}\n"
        "  deploy:\n"
        "    deploy_command: \"make deploy\"      # чем поставка производится\n"
        "    rollback: \"make rollback\"          # чем ОТМЕНЯЕТСЯ (без этого verified недостижим)\n"
    )


def _fmt(rep):
    lines = [f"поставка (deploy readiness): {rep['deploy_maturity']}",
             f"  {rep['reason']}"]
    if rep["config_markers"]:
        lines.append(f"  признаки: {', '.join(rep['config_markers'][:6])}")
    if rep["runnable_paths"]:
        lines.append(f"  исполняемый путь: {', '.join(rep['runnable_paths'][:4])}")
    lines.append(f"  откат: {'объявлен (' + ', '.join(rep['rollback_sources'][:3]) + ')' if rep['rollback_declared'] else 'НЕ объявлен'}")
    if rep["deploy_records"]:
        lines.append(f"  записи поставок: {len(rep['deploy_records'])}")
    for f in rep["findings"]:
        lines.append(f"  ! {f['rule']}: {f['detail']}")
    if rep["deploy_maturity"] == "absent":
        lines.append("  шаблон декларации — deploy_readiness.py --template")
    return "\n".join(lines)


def summary_line(child_root):
    """Строка для doctor. absent не маскируем: для не-деплоймого продукта это норма."""
    try:
        rep = assess(child_root)
    except Exception as e:  # noqa: BLE001 — зрелость поставки не роняет doctor
        return f"поставка (deploy): недоступно ({e})"
    m = rep["deploy_maturity"]
    tail = ""
    if m == "absent":
        tail = "  — не деплоймый продукт? тогда норма (не маскируем)"
    elif m != "verified":
        tail = "   → `ai-ops engops deploy` для деталей"
    if rep["runnable_paths"] and not rep["rollback_declared"]:
        tail += "; ✗ откат не объявлен"
    return f"поставка (deploy readiness): {m}{tail}"


def main(argv):
    if "--template" in argv:
        print(declaration_template())
        return 0
    root = argv[0] if argv and not argv[0].startswith("--") else "."
    rep = assess(root)
    print(json.dumps(rep, ensure_ascii=False, indent=2) if "--json" in argv else _fmt(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
