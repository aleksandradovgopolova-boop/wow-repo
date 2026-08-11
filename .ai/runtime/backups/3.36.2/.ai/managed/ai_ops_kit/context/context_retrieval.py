#!/usr/bin/env python3
"""context_retrieval.py (v3.6.1) — full-text retrieval + budgeted role context view.

Следующая ступень цепочки ContextArchitectureDecision (после Repository Graph Lite, v3.6.0):
детерминированный full-text поиск + сборка context view ПОД РОЛЬ с реальным применением
AccessFilterPolicy (v3.4.2) и token-бюджета (BudgetContract, v3.4.0). Всё ещё БЕЗ vector-DB
(семантика — позже в цепочке).

Инварианты цепочки (реализованы здесь):
  - access-filter ДО включения в view: файл класса, не разрешённого роли, НЕ попадает в контекст;
    секреты не попадают НИКОГДА (data_class=secret исключается всегда);
  - budget: включаем ранжированные файлы, пока не исчерпан token-бюджет (оценка chars/4);
  - provenance + cache_key = repository + sha + policy(role) + view (как в CAD).

CLI:  context_retrieval.py <root> --query "kw1 kw2" --role planner [--budget N] [--afp <file>] [--json]
      context_retrieval.py --selftest
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
from ai_ops_kit.shared import _bootstrap  # noqa: E402
from ai_ops_kit.security import data_classification as _dc   # noqa: E402  (v3.6.4 авторитетная классификация)
RETRIEVAL_INDEX_VERSION = "1"


def _repo_identity(root, repo_id=None) -> str:
    """Устойчивая идентичность репо: явный repo_id (remote) ИЛИ нормализованный абсолютный путь —
    НЕ голое имя каталога (два разных репо с одинаковым именем не должны делить cache-ключ)."""
    return repo_id or str(Path(root).resolve())


def _policy_fingerprint(afp: dict):
    """(policy_id, content_hash) AccessFilterPolicy — смена ЛЮБОГО правила меняет hash -> инвалидация."""
    pid = (afp or {}).get("id", "none")
    canon = json.dumps(afp or {}, sort_keys=True, ensure_ascii=False)
    return pid, hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
_SECRET = re.compile(r"sk-ant-api03|sk-[A-Za-z0-9]{16,}|gho_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY")
_DC_MARKER = re.compile(r"data-class:\s*(public|internal|confidential|secret)")


def classify(content: str, path: str = None, policy: dict = None, strict: bool = False) -> str:
    """v3.6.4: делегирует авторитетной классификации (policy -> scanner -> marker advisory raise-only).
    Недоверенный inline-marker НЕ понижает класс; секрет всегда secret."""
    return _dc.classify(content, path=path, policy=policy, strict=strict)


def _tokens(content: str) -> int:
    return max(1, len(content) // 4)   # грубая оценка токенов


# v3.6.7: full-text и role-view больше НЕ Python-only. Базовый охват под TS/React/docs child-репо
# (граф для TS — отдельный адаптер позже; здесь именно full-text + role-view поддержка).
RETRIEVAL_EXTS = (".py", ".ts", ".tsx", ".js", ".jsx", ".md", ".yaml", ".yml", ".json")
# v3.6.7d: hard-excludes — vendored/generated/build каталоги (в TS-репо иначе full-text зайдёт в
# node_modules/dist/… -> стоимость и мусор). Плюс лимиты размера/числа файлов (надёжность/стоимость).
SCAN_EXCLUDE_DIRS = frozenset({
    "node_modules", "dist", "build", "out", ".next", "coverage", "vendor", "target",
    "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".turbo", "bower_components"})
MAX_FILE_BYTES = 512 * 1024          # один файл > 512KB не читаем (сгенерированные бандлы)
MAX_SCAN_FILES = 5000                # верхняя граница числа читаемых файлов


def full_text_search(root, query: str, subdirs=("tools", "validation"), exts=RETRIEVAL_EXTS,
                     exclude_dirs=SCAN_EXCLUDE_DIRS, max_file_bytes=MAX_FILE_BYTES,
                     max_files=MAX_SCAN_FILES, path_filter=None):
    root = Path(root)
    exclude = set(exclude_dirs or ())
    kws = [k.lower() for k in query.split() if k.strip()]
    out = []
    scanned = 0
    for sd in subdirs:
        d = root / sd if sd else root   # sd="" -> сканировать весь root
        if not d.is_dir():
            continue
        files = []
        for ext in exts:
            files.extend(d.rglob(f"*{ext}"))
        for f in sorted(set(files)):
            rel = f.relative_to(root)
            parts = rel.parts
            if any(p.startswith(".") for p in parts):       # скрытые (.ai/.git/…)
                continue
            if any(p in exclude for p in parts[:-1]):        # vendored/generated/build каталоги
                continue
            # v3.7.0: access pre-filter — denied-путь НЕ читается (проверка ДО read_text)
            if path_filter is not None and not path_filter(str(rel)):
                continue
            try:
                if f.stat().st_size > max_file_bytes:        # слишком большой файл -> пропуск
                    continue
            except OSError:
                continue
            if scanned >= max_files:                         # верхняя граница числа файлов
                break
            scanned += 1
            try:
                content = f.read_text(encoding="utf-8")
            except OSError:
                continue
            low = content.lower()
            hits = {k: low.count(k) for k in kws}
            score = sum(hits.values())
            if score > 0:
                out.append({"file": str(rel), "score": score, "hits": hits})
    out.sort(key=lambda r: (-r["score"], r["file"]))   # детерминированно
    return out


def role_allowed_classes(afp: dict, role: str):
    for r in afp.get("rules", []) or []:
        if r.get("role") == role:
            return set(r.get("allowed_classes") or [])
    return set()   # deny-by-default


def build_view(root, query: str, role: str, allowed_classes, budget_tokens: int, sha=None,
               repo_id=None, policy=None, strict=False, subdirs=("tools", "validation"),
               exts=RETRIEVAL_EXTS):
    root = Path(root)
    allowed = set(allowed_classes)
    included, excl_access, excl_budget = [], [], []
    total = 0
    for r in full_text_search(root, query, subdirs, exts):
        f = root / r["file"]
        try:
            content = f.read_text(encoding="utf-8")
        except OSError:
            continue
        dc = classify(content, path=r["file"], policy=policy, strict=strict)
        # access-filter ДО включения: секрет — никогда; класс вне allowed — исключить
        if dc == "secret" or dc not in allowed:
            excl_access.append({"file": r["file"], "data_class": dc})
            continue
        tk = _tokens(content)
        if total + tk <= budget_tokens:
            total += tk
            included.append({"file": r["file"], "data_class": dc, "tokens": tk, "score": r["score"]})
        else:
            excl_budget.append({"file": r["file"], "tokens": tk})
    allowed_h = hashlib.sha256(",".join(sorted(allowed)).encode("utf-8")).hexdigest()[:8]
    return {"kind": "context-view", "role": role, "query": query,
            "repo": _repo_identity(root, repo_id), "sha": sha,
            "cache_key": (f"repo:{_repo_identity(root, repo_id)}|sha:{sha or 'DIRTY'}"
                          f"|role:{role}|allowed:{allowed_h}|view:{role}"),
            "included": included, "excluded_access": excl_access, "excluded_budget": excl_budget,
            "total_tokens": total, "budget_tokens": budget_tokens}


class RetrievalCache:
    """Детерминированный кэш context-view (v3.6.3 trust-фикс).

    ИНВАРИАНТЫ (P0 перед runtime-wiring):
      - ключ включает ИДЕНТИЧНОСТЬ ACCESS-POLICY (id + content-hash + allowed_classes роли): смена
        policy / ужесточение классов -> ДРУГОЙ ключ -> miss -> пере-retrieval (никогда не возвращаем
        старый permissive view);
      - lookup выполняется ДО retrieval: ключ считается из входов, build_view вызывается только на miss
        (реальная экономия I/O);
      - exact-revision binding: sha ОБЯЗАТЕЛЕН; без sha (dirty/unknown snapshot) — НЕ кэшируем;
      - идентичность репо — нормализованный путь/repo_id, не голое имя.
    """

    def __init__(self):
        self._store = {}
        self.hits = 0
        self.misses = 0
        self.builds = 0

    def cache_key(self, root, query, role, afp, budget_tokens, sha, repo_id=None):
        pid, phash = _policy_fingerprint(afp)
        allowed = ",".join(sorted(role_allowed_classes(afp, role)))
        return "|".join([f"repo:{_repo_identity(root, repo_id)}", f"sha:{sha}", f"policy:{pid}",
                         f"phash:{phash}", f"allowed:{allowed}", f"role:{role}", f"q:{query}",
                         f"b:{budget_tokens}", f"idx:{RETRIEVAL_INDEX_VERSION}"])

    def get_or_build(self, root, query, role, afp, budget_tokens, sha=None, repo_id=None):
        allowed = role_allowed_classes(afp, role)
        if not sha:   # exact-revision binding обязателен: dirty/unknown snapshot НЕ кэшируем
            self.misses += 1
            self.builds += 1
            return build_view(root, query, role, allowed, budget_tokens, sha=None, repo_id=repo_id), False
        key = self.cache_key(root, query, role, afp, budget_tokens, sha, repo_id)
        if key in self._store:              # lookup ДО retrieval
            self.hits += 1
            return self._store[key], True
        self.misses += 1
        self.builds += 1
        view = build_view(root, query, role, allowed, budget_tokens, sha=sha, repo_id=repo_id)
        self._store[key] = view
        return view, False


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    root = args[0]

    def _opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default
    query = _opt("--query", "")
    role = _opt("--role", "planner")
    budget = int(_opt("--budget", "20000"))
    afp_path = _opt("--afp", str(PKG / "examples" / "access-filter-demo" / "AFP-001.yaml"))
    afp = yaml.safe_load(Path(afp_path).read_text(encoding="utf-8")) if Path(afp_path).exists() else {}
    allowed = role_allowed_classes(afp, role)
    view = build_view(root, query, role, allowed, budget)
    if "--json" in argv:
        print(json.dumps(view, ensure_ascii=False, indent=2))
    else:
        print(f"CONTEXT-VIEW [{role}] query='{query}' budget={budget}t used={view['total_tokens']}t")
        print(f"  included ({len(view['included'])}): {[i['file'] for i in view['included']]}")
        print(f"  excluded_access ({len(view['excluded_access'])}): {[e['file'] for e in view['excluded_access']]}")
        print(f"  excluded_budget ({len(view['excluded_budget'])})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
