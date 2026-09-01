#!/usr/bin/env python3
"""branch_policy.py (v3.19.0 Engineering Operating Model, WP2) — BranchContract: на какой ветке
движок вправе работать и доставлять.

Аудит, из которого выросло (реальный случай 2026-08-04): ветка дочки, на которой стоял кит и куда
влили обновление 3.9.1, отстала от `main` на **234 коммита**, и никто этого не заметил. Квалификация
кита на такой ветке была бы замером продукта месячной давности — то есть НЕ замером. Отставание базы
до сих пор не было ничьей ответственностью: гейты смотрели на диф ветки, а не на её актуальность.

Проверяет:
  * прямой коммит в защищённую ветку (main/master/release/*) — движок доставляет ТОЛЬКО через PR;
  * имя ветки прогона (`ai-ops/<slug>`) — иначе доставку не связать с WorkItem;
  * ОТСТАВАНИЕ БАЗЫ: сколько коммитов базы отсутствует в ветке;
  * рассинхрон самой базы с upstream (ветвиться от стухшего локального main — та же болезнь ниже);
  * несколько WorkItem в одной ветке; возраст ветки.

Честность: недоступные измерения (нет upstream, нет git, не репозиторий) отдаются как `None` и
печатаются как `unavailable`, а НЕ как 0 — ноль означал бы «отставания нет», это ложь.

Сила: жёстко блокируются только необратимые вещи (коммит в защищённую ветку, безымянная ветка
доставки). Отставание по умолчанию СОВЕТУЕТ (владелец решает, когда рефрешить); `enforce: block`
поднимает до блока.

CLI:
  branch_policy.py <root> [--base main] [--engine-delivery] [--json]
  branch_policy.py --selftest
"""
from __future__ import annotations

import fnmatch
import json
import re
import sys
import time
from pathlib import Path

import yaml

try:                                    # в ките — tools/gitio.py; в дочке — .ai/managed/tools/gitio.py
    from ai_ops_kit.shared.gitio import git
except ImportError:                     # pragma: no cover — путь подмешивает вызывающий
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ai_ops_kit.shared.gitio import git

DEFAULTS = {
    "enforce": "advise",                # advise | block — сила МЯГКИХ правил
    "branch_prefix": "ai-ops/",         # префикс ветки прогона движка
    "protected_refs": ["main", "master", "release/*"],
    "base_drift_advisory": 20,          # с этого отставания — предупреждать
    "base_drift_stale": 100,            # с этого — считать базу стухшей (замер уже не про текущий продукт)
    "max_branch_age_days": 14,
    "one_workitem_per_branch": True,
}

_WORKITEM_RE = re.compile(r"\b(?:WI|WP)-\d+\b", re.IGNORECASE)
_SECONDS_PER_DAY = 86400


def _merge_policy(policy):
    pol = dict(DEFAULTS)
    pol["protected_refs"] = list(DEFAULTS["protected_refs"])
    for k, v in (policy or {}).items():
        if k in DEFAULTS and v is not None:
            pol[k] = list(v) if k == "protected_refs" else v
    return pol


def is_protected(branch, protected_refs) -> bool:
    """Ветка защищена, если совпадает с записью списка точно или по glob (release/*)."""
    b = str(branch or "")
    return any(b == p or fnmatch.fnmatch(b, p) for p in (protected_refs or ()))


def check_branch(branch, base="main", behind_count=None, workitems=(), engine_delivery=True,
                 base_behind_upstream=None, branch_age_days=None, policy=None):
    """Проверить ветку. -> BranchVerdict (dict). behind_count=None означает unavailable, НЕ ноль."""
    pol = _merge_policy(policy)
    hard, soft, unknown = [], [], []

    # --- ЖЁСТКИЕ инварианты -----------------------------------------------------------------------
    if engine_delivery and is_protected(branch, pol["protected_refs"]):
        hard.append({"rule": "direct_commit_to_protected_ref",
                     "detail": f"'{branch}' — защищённая ветка; движок доставляет только через PR "
                               f"(создайте {pol['branch_prefix']}<wid>)"})

    if engine_delivery and not str(branch or "").startswith(pol["branch_prefix"]):
        hard.append({"rule": "branch_naming",
                     "detail": f"ветка доставки должна начинаться с '{pol['branch_prefix']}' — "
                               f"иначе прогон не связать с WorkItem; получено '{branch or '—'}'"})

    # --- ОТСТАВАНИЕ БАЗЫ --------------------------------------------------------------------------
    if behind_count is None:
        unknown.append({"rule": "base_drift", "detail": f"отставание от '{base}' измерить не удалось "
                                                        f"(нет git/базы/upstream) — unavailable, не ноль"})
    elif behind_count >= pol["base_drift_stale"]:
        soft.append({"rule": "base_stale",
                     "detail": f"ветка отстаёт от '{base}' на {behind_count} коммитов (порог "
                               f"{pol['base_drift_stale']}) — прогон измерит не текущий продукт; "
                               f"освежите базу до работы"})
    elif behind_count >= pol["base_drift_advisory"]:
        soft.append({"rule": "base_drift",
                     "detail": f"ветка отстаёт от '{base}' на {behind_count} коммитов "
                               f"(порог {pol['base_drift_advisory']})"})

    if base_behind_upstream is None:
        unknown.append({"rule": "base_sync", "detail": f"рассинхрон '{base}' с upstream не измерен "
                                                       f"(нет upstream?) — unavailable"})
    elif base_behind_upstream > 0:
        soft.append({"rule": "base_not_synced",
                     "detail": f"сама база '{base}' отстаёт от upstream на {base_behind_upstream} "
                               f"коммитов — ветвление от неё воспроизводит отставание; `git fetch` + обновите базу"})

    # --- прочие мягкие --------------------------------------------------------------------------
    wids = sorted({w.upper() for w in (workitems or ())})
    if pol["one_workitem_per_branch"] and len(wids) > 1:
        soft.append({"rule": "multi_workitem",
                     "detail": f"в ветке коммиты нескольких WorkItem ({', '.join(wids)}) — "
                               f"ревью и откат теряют границу задачи"})

    if branch_age_days is not None and branch_age_days > pol["max_branch_age_days"]:
        soft.append({"rule": "stale_branch",
                     "detail": f"ветке {int(branch_age_days)} дней (порог {pol['max_branch_age_days']}) — "
                               f"чем дольше живёт, тем дороже слияние"})

    allowed = not hard and not (pol["enforce"] == "block" and soft)
    return {
        "kind": "BranchVerdict",
        "allowed": allowed,
        "branch": branch,
        "base": base,
        "enforce": pol["enforce"],
        "behind_count": behind_count,
        "base_behind_upstream": base_behind_upstream,
        "workitems": wids,
        "violations": hard,
        "advisories": soft,
        "unavailable": unknown,
    }


def read_state(root, base="main", branch=None):
    """Снять состояние ветки из git через gitio. Недоступные измерения -> None (не 0).

    -> {branch, base, behind_count, base_behind_upstream, workitems, branch_age_days}
    """
    root = str(root)
    st = {"branch": branch, "base": base, "behind_count": None, "base_behind_upstream": None,
          "workitems": [], "branch_age_days": None}

    rc, out, _ = git(root, "rev-parse", "--is-inside-work-tree")
    if rc != 0 or out != "true":
        return st

    if not st["branch"]:
        rc, out, _ = git(root, "rev-parse", "--abbrev-ref", "HEAD")
        st["branch"] = out if rc == 0 and out else None

    # существует ли база вообще (локально или как origin/<base>)
    base_ref = None
    for cand in (base, f"origin/{base}"):
        rc, _, _ = git(root, "rev-parse", "--verify", "--quiet", cand)
        if rc == 0:
            base_ref = cand
            break
    if not base_ref or not st["branch"]:
        return st
    st["base"] = base_ref

    # отставание: коммиты базы, отсутствующие в ветке
    rc, out, _ = git(root, "rev-list", "--count", f"{st['branch']}..{base_ref}")
    if rc == 0 and out.isdigit():
        st["behind_count"] = int(out)

    # рассинхрон самой базы с её upstream (если upstream объявлен)
    rc, up, _ = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", f"{base_ref}@{{upstream}}")
    if rc == 0 and up:
        rc, out, _ = git(root, "rev-list", "--count", f"{base_ref}..{up}")
        if rc == 0 and out.isdigit():
            st["base_behind_upstream"] = int(out)

    # WorkItem'ы в коммитах ветки поверх базы
    rc, out, _ = git(root, "log", "--format=%s", f"{base_ref}..{st['branch']}")
    if rc == 0 and out:
        st["workitems"] = sorted({m.group(0).upper() for line in out.splitlines()
                                  for m in [_WORKITEM_RE.search(line)] if m})

    # возраст ветки = давность её последнего коммита
    rc, out, _ = git(root, "log", "-1", "--format=%ct", st["branch"])
    if rc == 0 and out.isdigit():
        st["branch_age_days"] = max(0.0, (time.time() - int(out)) / _SECONDS_PER_DAY)
    return st


def policy_from_config(root):
    """`engineering_operating_model.branch` из .ai-ops.yaml. Нет файла/ключа/битый yaml -> {}."""
    for name in (".ai-ops.yaml", ".ai-ops.yml"):
        cfg = Path(root) / name
        if not cfg.is_file():
            continue
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 — битый конфиг не должен ронять проверку ветки
            return {}
        return ((data.get("engineering_operating_model") or {}).get("branch") or {})
    return {}


def assess(root, base="main", engine_delivery=True):
    """Снять состояние из git и вынести вердикт по политике репозитория."""
    pol = policy_from_config(root)
    st = read_state(root, base)
    v = check_branch(st["branch"], st["base"], st["behind_count"], st["workitems"],
                     engine_delivery=engine_delivery,
                     base_behind_upstream=st["base_behind_upstream"],
                     branch_age_days=st["branch_age_days"], policy=pol)
    return v


def _fmt(v):
    behind = "unavailable" if v["behind_count"] is None else str(v["behind_count"])
    lines = [f"branch: {'ДОПУСТИМА' if v['allowed'] else 'ЗАБЛОКИРОВАНА'} — "
             f"'{v['branch'] or '—'}' vs '{v['base']}', отставание {behind} "
             f"(enforce={v['enforce']})"]
    for x in v["violations"]:
        lines.append(f"  ✗ {x['rule']}: {x['detail']}")
    for x in v["advisories"]:
        lines.append(f"  ⚠ {x['rule']}: {x['detail']}")
    for x in v["unavailable"]:
        lines.append(f"  — {x['rule']}: {x['detail']}")
    if not v["violations"] and not v["advisories"]:
        lines.append("  ✓ нарушений и замечаний нет")
    return "\n".join(lines)


def summary_line(root, base="main"):
    """Строка для doctor: актуальность текущей ветки. Не блокирует, только сообщает."""
    try:
        st = read_state(root, base)
    except Exception as e:  # noqa: BLE001 — git недоступен не должен ронять doctor
        return f"актуальность ветки: недоступно ({e})"
    if st["branch"] is None:
        return "актуальность ветки: unavailable (не git-репозиторий?)"
    if st["behind_count"] is None:
        return (f"актуальность ветки: '{st['branch']}' — отставание от '{base}' unavailable "
                f"(базы нет локально? `git fetch`)")
    pol = _merge_policy(policy_from_config(root))
    if st["behind_count"] >= pol["base_drift_stale"]:
        mark = "✗ база стухла"
    elif st["behind_count"] >= pol["base_drift_advisory"]:
        mark = "⚠ отставание"
    else:
        mark = "✓"
    extra = ""
    if st["base_behind_upstream"]:
        extra = f"; сама '{st['base']}' отстаёт от upstream на {st['base_behind_upstream']}"
    return (f"актуальность ветки: {mark} '{st['branch']}' отстаёт от '{st['base']}' на "
            f"{st['behind_count']} коммитов{extra}")


def main(argv):
    root = argv[0] if argv and not argv[0].startswith("--") else "."
    base = "main"
    it = iter(argv)
    for a in it:
        if a == "--base":
            base = next(it, "main") or "main"
    v = assess(root, base, engine_delivery="--engine-delivery" in argv)
    print(json.dumps(v, ensure_ascii=False, indent=2) if "--json" in argv else _fmt(v))
    return 0 if v["allowed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
