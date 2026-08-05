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

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import data_classification as _dc   # noqa: E402  (v3.6.4 авторитетная классификация)
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


def selftest():
    import tempfile
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "tools").mkdir()
        (root / "tools" / "a.py").write_text("# keyword foo appears foo twice\ndef alpha():\n    return 'foo'\n", encoding="utf-8")
        (root / "tools" / "b.py").write_text("# data-class: confidential\n# foo here once\n", encoding="utf-8")
        (root / "tools" / "c.py").write_text("# foo but has a secret sk-ant-api03xxxxxxxx\n", encoding="utf-8")
        (root / "tools" / "d.py").write_text("# nothing relevant here\n", encoding="utf-8")

        res = full_text_search(root, "foo", ("tools",))
        expect("full-text: a.py ранжирован выше (больше hits), d.py не найден",
               res[0]["file"] == "tools/a.py" and all(r["file"] != "tools/d.py" for r in res))

        # v3.6.7: full-text больше не Python-only — TS/React/docs/JSON тоже сканируются
        (root / "ui").mkdir()
        (root / "ui" / "Widget.tsx").write_text("// foo component\nexport const Widget = () => 'foo';\n", encoding="utf-8")
        (root / "ui" / "notes.md").write_text("# foo doc\n", encoding="utf-8")
        rts = full_text_search(root, "foo", ("ui",))
        expect("full-text охватывает .tsx и .md (не только .py)",
               {"ui/Widget.tsx", "ui/notes.md"} <= {r["file"] for r in rts})

        # v3.6.7d: vendored/generated каталоги исключены (node_modules и т.п.)
        (root / "node_modules" / "pkg").mkdir(parents=True)
        (root / "node_modules" / "pkg" / "index.js").write_text("// foo vendored\n", encoding="utf-8")
        rex = full_text_search(root, "foo", ("",))
        expect("full-text НЕ заходит в node_modules (hard-exclude)",
               all("node_modules" not in r["file"] for r in rex))

        # planner: allowed {public, internal}
        v = build_view(root, "foo", "planner", {"public", "internal"}, budget_tokens=10000)
        inc = {i["file"] for i in v["included"]}
        exa = {e["file"] for e in v["excluded_access"]}
        expect("planner видит internal a.py", "tools/a.py" in inc)
        expect("planner НЕ видит confidential b.py (access-filter)", "tools/b.py" in exa)
        expect("секрет c.py исключён ВСЕГДА (никогда в контекст)", "tools/c.py" in exa)

        # executor: allowed adds confidential -> b.py включён, но c.py (secret) всё равно нет
        v2 = build_view(root, "foo", "executor", {"public", "internal", "confidential"}, 10000)
        inc2 = {i["file"] for i in v2["included"]}
        exa2 = {e["file"] for e in v2["excluded_access"]}
        expect("executor видит confidential b.py", "tools/b.py" in inc2)
        expect("секрет c.py исключён и для executor (secret никогда)", "tools/c.py" in exa2)

        # budget: крошечный бюджет -> часть уходит в excluded_budget
        vb = build_view(root, "foo", "executor", {"public", "internal", "confidential"}, budget_tokens=1)
        expect("бюджет=1 -> включён максимум один файл, остальное excluded_budget",
               len(vb["included"]) <= 1 and (len(vb["excluded_budget"]) >= 1 or len(vb["included"]) == 0)
               and vb["total_tokens"] <= 1)

        # deny-by-default: пустой allowed -> ничего не включается
        vd = build_view(root, "foo", "nobody", set(), 10000)
        expect("deny-by-default: allowed пуст -> included пуст", vd["included"] == [])

        expect("cache_key содержит repo+sha+role+view",
               all(x in v["cache_key"] for x in ("repo:", "sha:", "role:", "view:")))

        # --- v3.6.3 cache trust-fixes ---
        def _afp(rules):
            return {"id": "AFP-T", "kind": "AccessFilterPolicy", "rules": rules}
        afp_wide = _afp([{"role": "executor", "allowed_classes": ["public", "internal", "confidential"]}])
        afp_narrow = _afp([{"role": "executor", "allowed_classes": ["public", "internal"]}])

        cache = RetrievalCache()
        vw, hw = cache.get_or_build(root, "foo", "executor", afp_wide, 10000, sha="s1")
        _, hw2 = cache.get_or_build(root, "foo", "executor", afp_wide, 10000, sha="s1")
        expect("cache: повтор (repo+sha+policy) -> hit, retrieval не пере-строен",
               hw is False and hw2 is True and cache.builds == 1)
        expect("wide-policy: confidential b.py включён в view", "tools/b.py" in {i["file"] for i in vw["included"]})

        # P0: ужесточение policy при тех же sha/query/budget -> НЕ старый permissive view
        vn, hn = cache.get_or_build(root, "foo", "executor", afp_narrow, 10000, sha="s1")
        expect("P0 access-leak: ужесточение policy -> cache MISS (не отдаёт старый view)", hn is False)
        expect("P0: после ужесточения confidential b.py НЕ в view (нет утечки)",
               "tools/b.py" not in {i["file"] for i in vn["included"]})

        # lookup ДО retrieval: hit не пере-строит
        builds_before = cache.builds
        cache.get_or_build(root, "foo", "executor", afp_narrow, 10000, sha="s1")
        expect("cache: hit НЕ пере-строит retrieval (builds не растёт)", cache.builds == builds_before)

        # exact-revision binding: без sha не кэшируем
        c2 = RetrievalCache()
        c2.get_or_build(root, "foo", "executor", afp_wide, 10000, sha=None)
        c2.get_or_build(root, "foo", "executor", afp_wide, 10000, sha=None)
        expect("exact-revision: без sha НЕ кэшируем (2 miss/2 build, 0 hit)",
               c2.misses == 2 and c2.builds == 2 and c2.hits == 0)

        # repo identity: одинаковое имя каталога, разные пути -> разные ключи
        expect("repo identity: одинаковое имя, разные пути -> разные ключи",
               cache.cache_key("/x/proj", "foo", "executor", afp_wide, 10000, "s1")
               != cache.cache_key("/y/proj", "foo", "executor", afp_wide, 10000, "s1"))
        # смена sha -> другой ключ
        expect("exact-revision: смена sha -> другой ключ",
               cache.cache_key(root, "foo", "executor", afp_wide, 10000, "s1")
               != cache.cache_key(root, "foo", "executor", afp_wide, 10000, "s2"))

    # интеграция с реальным AFP-001 (v3.4.2): роли резолвятся
    afp_p = PKG / "examples" / "access-filter-demo" / "AFP-001.yaml"
    if afp_p.exists():
        afp = yaml.safe_load(afp_p.read_text(encoding="utf-8"))
        expect("AFP-001: planner -> {public, internal}",
               role_allowed_classes(afp, "planner") == {"public", "internal"})
        expect("AFP-001: security_reviewer включает confidential",
               "confidential" in role_allowed_classes(afp, "security_reviewer"))

    print("context_retrieval selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
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
