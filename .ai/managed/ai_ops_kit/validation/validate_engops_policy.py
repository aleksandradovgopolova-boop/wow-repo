#!/usr/bin/env python3
"""validate_engops_policy.py (v3.19.0 Engineering Operating Model, срез 1) — well-formedness политики
EngOps и ПАРИТЕТ дефолтов «код ↔ правило».

Две проверки, каждая ловит свой класс дрейфа:

1. **Политика репозитория связна.** `engineering_operating_model` в `.ai-ops.yaml` (если объявлен) не
   должен содержать взаимно противоречащих порогов. Пример настоящего противоречия:
   `base_drift_advisory >= base_drift_stale` — тогда предупреждение об отставании не срабатывает
   никогда, а репозиторий уверен, что настроил контроль. Молча «работающая» политика, которая ничего
   не проверяет, хуже отсутствующей.

2. **Правило не расходится с кодом.** `rules/core/EngineeringOperatingModel.md` печатает пороги
   в yaml-блоке; `ai_ops_kit/engops/commit_policy.py` и `ai_ops_kit/engops/branch_policy.py` держат их в `DEFAULTS`. Это два
   места для одного числа — ровно тот шов, на котором кит уже ловил дрейф (release-claims). Валидатор
   сверяет их дословно.

Использование:  validate_engops_policy.py [child_root]   — проверить политику репозитория + паритет
                validate_engops_policy.py --selftest
Возврат 0 — связно и без дрейфа, 1 — есть нарушение.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
RULE_DOC = PKG / "rules" / "core" / "EngineeringOperatingModel.md"

_ENFORCE_VALUES = ("advise", "block")
_COMMIT_KEYS = ("enforce", "max_files", "max_top_level_dirs", "min_message_chars",
                "require_workitem", "require_evidence")
_BRANCH_KEYS = ("enforce", "branch_prefix", "protected_refs", "base_drift_advisory",
                "base_drift_stale", "max_branch_age_days", "one_workitem_per_branch")


def _load_tool_defaults():
    """DEFAULTS из обоих инструментов. -> (commit_defaults, branch_defaults)."""
    # Корень на путь кладём сами: в script-режиме его там нет, а имена нужны пакетные —
    # плоские сюда уже привели бы к тому же ModuleNotFoundError после pip install.
    sys.path.insert(0, str(PKG))
    from ai_ops_kit.engops import branch_policy, commit_policy
    return dict(commit_policy.DEFAULTS), dict(branch_policy.DEFAULTS)


_ENV_KINDS = ("local", "ci", "preview", "staging", "production", "unknown")
_SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Литеральные секреты: в конфиге допустимы только ИМЕНА и ссылки env:/secret:, никогда значения.
_SECRET_VALUE_HINTS = (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
                       re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
                       re.compile(r"\bAKIA[0-9A-Z]{16}\b"))


def _check_environments(envs):
    """v3.20.0 срез 2: объявленные окружения (well-formedness + запрет значений секретов)."""
    if envs is None:
        return []
    if not isinstance(envs, list):
        return ["engineering_operating_model.environments: ожидался список"]
    errs, seen = [], set()
    for i, e in enumerate(envs):
        if isinstance(e, str):
            name, decl = e, {}
        elif isinstance(e, dict):
            name, decl = str(e.get("name") or ""), e
        else:
            errs.append(f"environments[{i}]: ожидалась строка или объект с name")
            continue
        if not name.strip():
            errs.append(f"environments[{i}]: пустое name")
            continue
        if name in seen:
            errs.append(f"environments: окружение '{name}' объявлено дважды")
        seen.add(name)
        bad = set(decl) - {"name", "kind", "approvers", "deploy_ref", "secret_names"}
        if bad:
            errs.append(f"environments['{name}']: неизвестные ключи {sorted(bad)}")
        kind = decl.get("kind")
        if kind is not None and kind not in _ENV_KINDS:
            errs.append(f"environments['{name}'].kind='{kind}' вне {list(_ENV_KINDS)}")
        appr = decl.get("approvers")
        if appr is not None and (not isinstance(appr, list)
                                 or not all(isinstance(a, str) and a.strip() for a in appr)):
            errs.append(f"environments['{name}'].approvers: ожидался список непустых строк")
        secrets = decl.get("secret_names")
        if secrets is not None:
            if not isinstance(secrets, list):
                errs.append(f"environments['{name}'].secret_names: ожидался список ИМЁН")
            else:
                for s in secrets:
                    if not isinstance(s, str) or not _SECRET_NAME_RE.match(s):
                        errs.append(f"environments['{name}'].secret_names: '{s}' не похоже на ИМЯ "
                                    f"переменной — здесь только имена, значения запрещены")
                    elif any(p.search(s) for p in _SECRET_VALUE_HINTS):
                        errs.append(f"environments['{name}'].secret_names: '{s[:12]}…' выглядит как "
                                    f"ЗНАЧЕНИЕ секрета — немедленно отзовите ключ")
    return errs


def _check_deploy(deploy):
    """v3.20.0 срез 2: блок deploy (чем поставка производится и чем отменяется)."""
    if deploy is None:
        return []
    if not isinstance(deploy, dict):
        return ["engineering_operating_model.deploy: ожидался объект"]
    errs = []
    bad = set(deploy) - {"deploy_command", "rollback"}
    if bad:
        errs.append(f"engineering_operating_model.deploy: неизвестные ключи {sorted(bad)}")
    for key in ("deploy_command", "rollback"):
        val = deploy.get(key)
        if val is not None and (not isinstance(val, str) or not val.strip()):
            errs.append(f"engineering_operating_model.deploy.{key}: ожидалась непустая строка")
        if isinstance(val, str) and any(p.search(val) for p in _SECRET_VALUE_HINTS):
            errs.append(f"engineering_operating_model.deploy.{key}: содержит литеральный секрет — "
                        f"передавайте через env:/secret:, ключ отзовите")
    if deploy.get("deploy_command") and not deploy.get("rollback"):
        errs.append("deploy_command объявлен без rollback: поставка, которую нельзя отменить, "
                    "не является готовностью (зрелость verified недостижима)")
    return errs


def _check_economics(eco):
    """v3.21.0 срез 3: экономическая политика (пороги + осознанность require_estimate)."""
    if eco is None:
        return []
    if not isinstance(eco, dict):
        return ["engineering_operating_model.economics: ожидался объект"]
    errs = []
    bad = set(eco) - {"enforce", "require_estimate", "confirm_over_cost_usd", "min_history_tasks"}
    if bad:
        errs.append(f"engineering_operating_model.economics: неизвестные ключи {sorted(bad)}")
    enf = eco.get("enforce")
    if enf is not None and enf not in _ENFORCE_VALUES:
        errs.append(f"economics.enforce='{enf}' вне {list(_ENFORCE_VALUES)}")
    req = eco.get("require_estimate")
    if req is not None and not isinstance(req, bool):
        errs.append("economics.require_estimate: ожидался boolean")
    thr = eco.get("confirm_over_cost_usd")
    if thr is not None and (isinstance(thr, bool) or not isinstance(thr, (int, float)) or thr < 0):
        errs.append(f"economics.confirm_over_cost_usd={thr!r}: ожидалось неотрицательное число или null")
    mh = eco.get("min_history_tasks")
    if mh is not None and (isinstance(mh, bool) or not isinstance(mh, int) or mh < 1):
        errs.append(f"economics.min_history_tasks={mh!r}: ожидалось целое ≥ 1")
    # require_estimate блокирует ЛЮБОЙ первый прогон в репозитории (истории ещё нет). Это законный
    # выбор, но он должен быть осознанным: молча включённым он выглядит как «строгость», а работает
    # как «здесь нельзя запустить ничего».
    if req is True and enf != "block":
        errs.append("economics.require_estimate=true при enforce != block: отсутствие истории будет "
                    "блокировать первый прогон, хотя политика заявлена как советующая — противоречие")
    return errs


def check_policy(policy):
    """Связность объявленной политики. -> список нарушений (строк). policy может быть {} / None."""
    errs = []
    pol = policy or {}
    if not isinstance(pol, dict):
        return ["engineering_operating_model: ожидался объект"]

    unknown = set(pol) - {"commit", "branch", "environments", "deploy", "economics"}
    if unknown:
        errs.append(f"engineering_operating_model: неизвестные ключи {sorted(unknown)} "
                    f"(допустимы commit, branch, environments, deploy, economics)")
    errs += _check_environments(pol.get("environments"))
    errs += _check_deploy(pol.get("deploy"))
    errs += _check_economics(pol.get("economics"))

    commit = pol.get("commit") or {}
    branch = pol.get("branch") or {}
    if not isinstance(commit, dict):
        errs.append("engineering_operating_model.commit: ожидался объект")
        commit = {}
    if not isinstance(branch, dict):
        errs.append("engineering_operating_model.branch: ожидался объект")
        branch = {}

    for name, block, allowed in (("commit", commit, _COMMIT_KEYS), ("branch", branch, _BRANCH_KEYS)):
        bad = set(block) - set(allowed)
        if bad:
            errs.append(f"engineering_operating_model.{name}: неизвестные ключи {sorted(bad)}")
        enf = block.get("enforce")
        if enf is not None and enf not in _ENFORCE_VALUES:
            errs.append(f"engineering_operating_model.{name}.enforce='{enf}' вне {list(_ENFORCE_VALUES)}")
        for key in allowed:
            if key in ("enforce", "branch_prefix", "protected_refs",
                       "require_workitem", "require_evidence", "one_workitem_per_branch"):
                continue
            val = block.get(key)
            if val is not None and (not isinstance(val, int) or isinstance(val, bool) or val < 1):
                errs.append(f"engineering_operating_model.{name}.{key}={val!r}: ожидалось целое ≥ 1")

    adv, stale = branch.get("base_drift_advisory"), branch.get("base_drift_stale")
    if isinstance(adv, int) and isinstance(stale, int) and not isinstance(adv, bool) \
            and not isinstance(stale, bool) and adv >= stale:
        errs.append(f"base_drift_advisory ({adv}) >= base_drift_stale ({stale}): предупреждение об "
                    f"отставании не сработает никогда — политика выглядит настроенной, но не проверяет")

    refs = branch.get("protected_refs")
    if refs is not None:
        if not isinstance(refs, list) or not refs:
            errs.append("protected_refs: ожидался непустой список (пустой = защиты нет)")
        elif not all(isinstance(r, str) and r.strip() for r in refs):
            errs.append("protected_refs: все записи — непустые строки")

    prefix = branch.get("branch_prefix")
    if prefix is not None:
        if not isinstance(prefix, str) or not prefix.strip():
            errs.append("branch_prefix: ожидалась непустая строка")
        elif isinstance(refs, list) and any(isinstance(r, str) and r == prefix.rstrip("/")
                                            for r in refs):
            errs.append(f"branch_prefix '{prefix}' совпадает с защищённой веткой — ветка доставки "
                        f"обязана быть и правильно названной, и незащищённой одновременно")
    return errs


def _rule_doc_defaults(doc_text):
    """Достать yaml-блок конфигурации из правила. -> {'commit': {...}, 'branch': {...}} либо {}."""
    for block in re.findall(r"```yaml\n(.*?)```", doc_text, re.DOTALL):
        if "engineering_operating_model:" not in block:
            continue
        cleaned = re.sub(r"[ \t]+#.*$", "", block, flags=re.MULTILINE)
        try:
            data = yaml.safe_load(cleaned) or {}
        except yaml.YAMLError as e:
            return {"__error__": f"yaml-блок правила не парсится: {e}"}
        return data.get("engineering_operating_model") or {}
    return {}


def check_parity(rule_doc=RULE_DOC):
    """Пороги в правиле == DEFAULTS в коде. -> список нарушений."""
    doc = Path(rule_doc)
    if not doc.is_file():
        return [f"{doc}: правило EngineeringOperatingModel отсутствует"]
    declared = _rule_doc_defaults(doc.read_text(encoding="utf-8"))
    if "__error__" in declared:
        return [declared["__error__"]]
    if not declared:
        return [f"{doc.name}: не найден yaml-блок с engineering_operating_model"]

    commit_defaults, branch_defaults = _load_tool_defaults()
    errs = []
    for name, code in (("commit", commit_defaults), ("branch", branch_defaults)):
        doc_block = declared.get(name) or {}
        missing = set(code) - set(doc_block)
        if missing:
            errs.append(f"{doc.name} → {name}: в правиле не описаны пороги {sorted(missing)} "
                        f"(есть в DEFAULTS кода)")
        for key, doc_val in doc_block.items():
            if key not in code:
                errs.append(f"{doc.name} → {name}.{key}: правило описывает порог, которого нет в коде")
            elif code[key] != doc_val:
                errs.append(f"дрейф {name}.{key}: правило={doc_val!r}, код={code[key]!r}")
    return errs


def check_child(child_root):
    """Политика из .ai-ops.yaml репозитория (если он есть) + паритет правила и кода."""
    errs = list(check_parity())
    for name in (".ai-ops.yaml", ".ai-ops.yml"):
        cfg = Path(child_root) / name
        if not cfg.is_file():
            continue
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            return errs + [f"{name}: не парсится ({e})"]
        if "engineering_operating_model" in data:
            errs += check_policy(data.get("engineering_operating_model"))
        break
    return errs


def main(argv):
    root = argv[0] if argv and not argv[0].startswith("--") else "."
    errs = check_child(root)
    if errs:
        for e in errs:
            print(f"  FAIL {e}")
        print(f"validate_engops_policy: FAIL ({len(errs)})")
        return 1
    print("validate_engops_policy: OK (политика связна, правило не расходится с кодом)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
