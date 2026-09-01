#!/usr/bin/env python3
"""Documentation Evidence — гейт `documentation_updated` стал машинным (C3, v3.37).

ЧТО БЫЛО. Гейт закрывался `writer`'ом: стадия, которая работу и сделала, объявляла её
задокументированной. Самозаявление — слабейшая из четырёх форм закрытия
(`rules/quality/gate-closure-map.md`), и здесь оно было лишним: оба объявленных доказательства —
ФАКТЫ О ДИФЕ, а не суждение.

  * `docs_updated_or_explicit_none` — изменение затронуло документацию, либо её ненужность
    объявлена явно и с причиной;
  * `release_notes_entry` — запись для CHANGELOG добавлена в этой ветке.

Ни одно из двух не требует мнения: они либо есть в дереве, либо их нет. Проверка ниже спрашивает
дерево.

ТРИ СОСТОЯНИЯ, И ТРЕТЬЕ НЕ РАВНО ВТОРОМУ. База сравнения не определяется (первый коммит, нет git,
detached-состояние) -> `unverifiable`, а НЕ «документация не тронута». Каталог фрагментов
CHANGELOG в этом репозитории не объявлен -> `unknown` по этому доказательству, а не «записи нет»:
дочка вправе вести историю иначе, и обвинять её в нарушении правила, которого она не принимала,
значит выдавать незнание за находку.

ОСВОБОЖДЕНИЕ ГРОМКОЕ, А НЕ ТИХОЕ. «Документация не нужна» закрывает первое доказательство, но
только явным объявлением с причиной, и причина уходит в warnings и в отчёт — как
`behavior_unchanged` у `regression_evidence`. Молчаливого обхода нет.

ЧЕГО ЭТА ПРОВЕРКА НЕ ДЕЛАЕТ. Она не судит, ХОРОШО ли написана документация и то ли в ней написано.
Это другой вопрос и другой гейт (`documentation_drift`, и он остаётся не машинным). Смешать их
значило бы объявить машинным суждение — ровно та ошибка, против которой заведён
`quality/gate-machinability.yaml`.

Использование:  documentation_evidence.py assess <root> [--base REF] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from ai_ops_kit.gates.regression_evidence import is_doc_path
from ai_ops_kit.shared import _bootstrap  # noqa: E402,F401

# Фрагмент — файл вида <что-это>.<тип>.md. Типы читаем из того же pyproject, если он есть;
# запасной набор — типы towncrier по умолчанию в этом репозитории.
_FALLBACK_TYPES = ("feat", "fix", "quality", "chore")


def _git(root, *args):
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def resolve_base(root, base=None):
    """База сравнения: явная -> AI_OPS_DIFF_BASE -> HEAD~1 -> None.

    None означает «сравнить не с чем», и это ТРЕТЬЕ состояние: вызывающий обязан сказать
    `unverifiable`, а не сделать вид, что диф пуст."""
    if base:
        return base
    if os.environ.get("AI_OPS_DIFF_BASE"):
        return os.environ["AI_OPS_DIFF_BASE"]
    rc, _, _ = _git(root, "rev-parse", "--verify", "HEAD~1")
    return "HEAD~1" if rc == 0 else None


def changed_files(root, base):
    """Файлы, изменённые между базой и рабочим деревом. None — база недоступна."""
    rc, out, _ = _git(root, "diff", "--name-only", base)
    if rc != 0:
        return None
    rc2, out2, _ = _git(root, "diff", "--name-only", "--cached")
    files = [f for f in (out.splitlines() + out2.splitlines()) if f]
    return sorted(set(files))


def _fragments_dir(root):
    """Объявленный каталог фрагментов, либо None — репозиторий его не объявлял.

    БЕЗ `tomllib` намеренно: он появился в stdlib только с 3.11, а объявленный пол репозитория —
    3.9 (`validate_python_compat` это ловит, и джоба python39-compat роняла релиз ровно на таком
    импорте, v3.33.2). Ключ один и формат простой — читаем одну строку регуляркой, как это уже
    делают `security_scan` и `test_release_gates`. Это не разбор TOML и на него не претендует."""
    py = Path(root) / "pyproject.toml"
    if not py.is_file():
        return None
    import re
    m = re.search(r'(?m)^\s*changelog_fragments_dir\s*=\s*["\']([^"\']+)["\']',
                  py.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def _is_fragment(path, frag_dir, types=_FALLBACK_TYPES):
    p = str(path).replace("\\", "/")
    if not p.startswith(frag_dir.rstrip("/") + "/"):
        return False
    name = p.rsplit("/", 1)[-1]
    if name.lower() == "readme.md" or not name.endswith(".md"):
        return False
    parts = name[:-3].rsplit(".", 1)
    return len(parts) == 2 and parts[1] in types


def assess(root, base=None, files=None) -> dict:
    """-> DocumentationEvidence: тронута ли документация и есть ли запись для CHANGELOG."""
    root = Path(root)
    resolved = resolve_base(root, base)
    if files is None:
        files = changed_files(root, resolved) if resolved else None
    res = {"kind": "DocumentationEvidence", "base": resolved,
           "docs_changed": [], "fragments": [], "checks": []}

    if files is None:
        res["status"] = "unverifiable"
        res["reason"] = ("базу сравнения определить нечем (нет истории git или база недоступна) — "
                         "«документация не тронута» отсюда НЕ следует")
        res["checks"] = [{"id": "diff_base_available", "status": "warn"}]
        return res

    frag_dir = _fragments_dir(root)
    res["fragments_dir"] = frag_dir
    res["docs_changed"] = [f for f in files if is_doc_path(f)
                           and not (frag_dir and _is_fragment(f, frag_dir))]
    res["fragments"] = [f for f in files if frag_dir and _is_fragment(f, frag_dir)]

    docs_ok = bool(res["docs_changed"])
    res["checks"].append({"id": "docs_touched", "status": "pass" if docs_ok else "fail"})
    if frag_dir is None:
        # Репозиторий не объявлял, где держит историю. Это НЕ нарушение — это незнание.
        res["checks"].append({"id": "release_notes_dir_not_declared", "status": "warn"})
        notes = "unknown"
    else:
        notes = "pass" if res["fragments"] else "fail"
        res["checks"].append({"id": "release_notes_entry", "status": notes})

    if docs_ok and notes == "pass":
        res["status"] = "documented"
    elif notes == "unknown" and docs_ok:
        res["status"] = "partly"
        res["reason"] = ("документация тронута; каталог фрагментов CHANGELOG в этом репозитории "
                         "не объявлен — запись для истории не проверяется")
    else:
        res["status"] = "not_documented"
        missing = []
        if not docs_ok:
            missing.append("изменение не трогает документацию")
        if notes == "fail":
            missing.append(f"нет записи для CHANGELOG в {frag_dir}/")
        res["reason"] = "; ".join(missing)
    return res


def gate_evidence(assessment, docs_not_needed=None):
    """DocumentationEvidence -> evidence гейта `documentation_updated`.

    `docs_not_needed` — объявление writer'а с причиной. Закрывает доказательство про документацию,
    но ГРОМКО: причина уходит в warnings и в отчёт."""
    a = assessment or {}
    st = a.get("status")
    checks = a.get("checks") or []
    if st == "documented":
        return {"status": "pass",
                "provided": ["docs_updated_or_explicit_none", "release_notes_entry"],
                "checks": checks,
                "evidence": [f"документация: {', '.join(a['docs_changed'][:3])}",
                             f"запись истории: {', '.join(a['fragments'][:3])}"]}
    if st == "unverifiable":
        # Гейт advisory: непроверяемое даёт warn с причиной, а не pass и не блок.
        return {"status": "warn", "checks": checks,
                "warnings": [a.get("reason") or "проверить нечем"]}
    if st == "partly":
        return {"status": "warn", "checks": checks,
                "provided": ["docs_updated_or_explicit_none"],
                "warnings": [a.get("reason") or "проверена только часть"]}
    if docs_not_needed:
        return {"status": "warn", "checks": checks,
                "provided": ["docs_updated_or_explicit_none"],
                "warnings": [f"документация объявлена ненужной: {docs_not_needed}"],
                "evidence": ["объявление writer'а вместо изменения документации"]}
    return {"status": "warn", "checks": checks,
            "warnings": [a.get("reason") or "документация не обновлена",
                         'если она не нужна — объявите причину: {"docs_not_needed": "<причина>"}']}


def main(argv):
    ap = argparse.ArgumentParser(description="Documentation Evidence (гейт documentation_updated)")
    ap.add_argument("cmd", nargs="?", choices=["assess"], default=None)
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--base")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd != "assess":
        ap.print_help()
        return 1
    res = assess(a.root, a.base)
    print(json.dumps(res, ensure_ascii=False, indent=2) if a.json
          else f"{res['status']}: {res.get('reason', '')}".rstrip(": "))
    return 0 if res["status"] in ("documented", "partly", "unverifiable") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
