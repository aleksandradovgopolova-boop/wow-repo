#!/usr/bin/env python3
"""context_engine.py (v3.6.7) — единый канонический оркестратор Context Engine v2.

Ревью владельца по v3.6.6: shadow подключал только `build_view()` (full-text role view), а не всю
retrieval-цепочку (Repository Graph, graph-augmentation, semantic-lite, cache в shadow не входили), и
работал на ДЕМО-политике кита без DataClassificationPolicy. Здесь — ОДИН слой, собирающий полную
цепочку перед промоушеном в боевой runtime:

    обязательный контекст v1 (policy/spec/decisions)
    + full-text кандидаты
    + Repository Graph augmentation (соседи по импортам/обратным зависимостям)
    + УСЛОВНЫЙ semantic-lite (только при недостаточном детерминированном recall)
    -> access-filter (AFP роли, DataClassificationPolicy — политики CHILD-репо, НЕ демо)
    -> rerank
    -> budget
    -> role view

Инварианты (fail-closed перед боевым включением):
  - без ТОЧНОГО SHA view НЕ строится (нельзя доказать привязку к ревизии) -> ValueError;
  - обязательный контекст v1 НЕЛЬЗЯ потерять на rerank/budget (только access-filter: secret никогда);
  - graph и semantic ТОЛЬКО ДОБАВЛЯЮТ кандидатов, не удаляют детерминированные;
  - semantic вызывается ТОЛЬКО при недостаточном детерминированном recall;
  - у каждого источника — reason, content-hash и точный SHA;
  - политики берутся у CHILD-репо (load_child_policies), demo-политик в runtime нет.

Только stdlib + pyyaml.  CLI: context_engine.py <child_root> --query ".." --role executor --sha SHA | --selftest
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import yaml

from ai_ops_kit.shared import _bootstrap  # noqa: E402
from ai_ops_kit.context import context_retrieval as cr   # noqa: E402
from ai_ops_kit.context import repo_graph                # noqa: E402
from ai_ops_kit.context import semantic_lite             # noqa: E402

SEMANTIC_RECALL_FLOOR = 3        # < столько детерминированных кандидатов -> recall недостаточен
DEFAULT_BUDGET_TOKENS = 20000


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:12]


def _hidden(rel: str) -> bool:
    return any(part.startswith(".") for part in Path(rel).parts)


def _git(root, *args):
    try:
        p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=20)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.SubprocessError):
        return 1, "", "git unavailable"


def verify_snapshot(root, sha):
    """ДОКАЗАТЬ, что содержимое каталога == commit snapshot: HEAD == sha И рабочее дерево чистое.
    Без этого exact-revision binding декларативен (можно подписать грязный worktree чужим SHA)."""
    if not sha:
        return False, "нет sha -> snapshot не доказуем"
    rc, head, _ = _git(root, "rev-parse", "HEAD")
    if rc != 0:
        return False, "не git-репозиторий / git недоступен -> snapshot не доказан"
    if head != sha:
        return False, f"HEAD ({head[:12]}) != committed_sha ({str(sha)[:12]}) -> читается не тот snapshot"
    rc2, porcelain, _ = _git(root, "status", "--porcelain")
    if rc2 != 0:
        return False, "git status недоступен -> snapshot не доказан"
    if porcelain.strip():
        return False, "рабочее дерево грязное (uncommitted changes) -> snapshot не доказан"
    return True, "HEAD == committed_sha, дерево чистое"


def budget_tokens_from(policy, scope="run", default=DEFAULT_BUDGET_TOKENS):
    """Токен-лимит из настоящего child BudgetContract (по scope run/package/view). Раньше shadow
    отбрасывал budget и жёстко ставил 20000."""
    if not isinstance(policy, dict):
        return default
    for key in ("scopes", "budgets", "limits"):
        for entry in policy.get(key, []) or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("scope") in (scope, None) or key == "limits":
                for tk in ("token_budget", "tokens", "max_tokens", "context_tokens"):
                    if isinstance(entry.get(tk), int):
                        return entry[tk]
    for tk in ("token_budget", "tokens", "max_tokens", "context_tokens"):
        if isinstance(policy.get(tk), int):
            return policy[tk]
    return default


def _gitignore_dirs(root):
    """Простые каталог-паттерны из .gitignore child-репо (имена без wildcard/слэша в середине)."""
    out = set()
    p = Path(root) / ".gitignore"
    if not p.exists():
        return out
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or s.startswith("!"):
                continue
            s = s.rstrip("/").lstrip("/")
            if s and "*" not in s and "/" not in s:
                out.add(s)
    except OSError:
        pass
    return out


def load_child_policies(child_root):
    """Политики CHILD-репо (НЕ демо кита): .ai/policies/{access-filter,data-classification,budget}.yaml.
    Отсутствует -> None (deny-by-default в build_context; никакого demo-fallback в runtime)."""
    base = Path(child_root) / ".ai" / "policies"

    def _load(name):
        p = base / name
        try:
            return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else None
        except OSError:
            return None
    return _load("access-filter.yaml"), _load("data-classification.yaml"), _load("budget.yaml")


def _list_rel_paths(root, exclude):
    """Перечислить кандидат-ПУТИ (без чтения содержимого) для access pre-filter/audit: broad exts,
    без скрытых/vendored/oversize. Только имена — не содержимое."""
    root = Path(root)
    out = []
    for ext in cr.RETRIEVAL_EXTS:
        for f in root.rglob(f"*{ext}"):
            rel = f.relative_to(root)
            parts = rel.parts
            if any(p.startswith(".") for p in parts) or any(p in exclude for p in parts[:-1]):
                continue
            try:
                if f.stat().st_size > cr.MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            out.append(str(rel))
    return out


def _invalid_view(role, query, sha, reasons, snapshot_verified):
    return {"kind": "context-engine-view", "schema": "v2-orchestrated", "role": role, "query": query,
            "repo": None, "sha": sha, "valid": False, "invalid_reasons": list(reasons),
            "snapshot_verified": snapshot_verified,
            "sources_used": {"mandatory_v1": 0, "fulltext": 0, "graph_added": 0,
                             "semantic_used": False, "semantic_reason": "view invalid -> retrieval не выполнен"},
            "included": [], "excluded_access": [], "excluded_budget": [], "mandatory_missing": [],
            "total_tokens": 0, "budget_tokens": 0,
            "cache": {"enabled": False, "reason": "view invalid"}, "cache_key": None}


def build_context(child_root, query, role, *, sha, afp, dcp=None, budget_tokens=DEFAULT_BUDGET_TOKENS,
                  v1_mandatory=None, repo_id=None, semantic_recall_floor=SEMANTIC_RECALL_FLOOR,
                  require_snapshot=False):
    """Собрать полный Context Engine v2 view. sha ОБЯЗАТЕЛЕН (иначе ValueError).

    v3.6.7d: require_snapshot=True ДОКАЗЫВАЕТ, что содержимое каталога == commit snapshot (HEAD==sha,
    дерево чистое) — иначе view невалиден и execution НЕ получает контекст. Обязательный контекст v1,
    потерянный (не найден / запрещён access) -> view невалиден (valid=false), нужен handoff/фикс policy."""
    if not sha:
        raise ValueError("context_engine: без точного SHA view не строится (exact-revision binding)")
    root = Path(child_root)

    # --- ДОКАЗАТЕЛЬСТВО snapshot (перед любым чтением файлов) ---
    snapshot_verified = False
    if require_snapshot:
        oks, sreason = verify_snapshot(root, sha)
        snapshot_verified = oks
        if not oks:
            return _invalid_view(role, query, sha, [f"snapshot не доказан: {sreason}"], False)

    allowed = cr.role_allowed_classes(afp, role) if afp else set()   # deny-by-default
    exclude = set(cr.SCAN_EXCLUDE_DIRS) | _gitignore_dirs(root)      # vendored/build + .gitignore-aware

    # --- ACCESS PRE-FILTER (v3.7.0-fix): классификация ПО ПУТИ (без чтения содержимого) -> роль видит
    # ТОЛЬКО разрешённые пути. Denied-путь НЕ передаётся в full-text/graph/semantic (не читается, имя не
    # раскрывается role-facing). Content-scanner потом может ТОЛЬКО повысить класс. Privileged-audit ведёт
    # список pre_filtered (знать denied-файлы можно только привилегированному слою, не role-facing).
    def _allowed_path(rel):
        pc = cr.classify("", path=rel, policy=dcp)   # path-based класс (content="" -> без scanner/marker)
        return pc != "secret" and (bool(allowed) and pc in allowed)
    pre_filtered_denied = sorted(p for p in _list_rel_paths(root, exclude) if not _allowed_path(p))

    # --- источник 1: обязательный контекст v1 (policy/spec/decisions) — нельзя потерять (НЕ pre-filter'ится;
    #     доступ проверяется при чтении: denied mandatory -> excluded_access -> view invalid) ---
    cand = {}   # rel -> {"file","sources":set,"score","mandatory"}
    for rel in (v1_mandatory or []):
        cand[rel] = {"file": rel, "sources": {"mandatory-v1"}, "score": 10_000, "mandatory": True}

    # --- источник 2: full-text ТОЛЬКО по разрешённым путям (denied не читается) ---
    ft = cr.full_text_search(root, query, subdirs=("",), exclude_dirs=exclude, path_filter=_allowed_path)
    for r in ft:
        c = cand.setdefault(r["file"], {"file": r["file"], "sources": set(), "score": 0, "mandatory": False})
        c["sources"].add("fulltext")
        c["score"] = max(c["score"], r["score"]) if not c["mandatory"] else c["score"]
    deterministic_count = len([1 for r in ft])

    # --- источник 3: Repository Graph augmentation (граф строится ТОЛЬКО по разрешённым .py) ---
    graph_added = 0
    try:
        graph = repo_graph.build_graph(root, subdirs=("",), path_filter=_allowed_path)
    except Exception:
        graph = {"import_edges": {}}
    ft_files = [r["file"] for r in ft]
    for rel in ft_files:
        if not rel.endswith(".py"):
            continue
        neighbors = set(graph.get("import_edges", {}).get(rel, []))   # forward: что импортит rel
        neighbors |= set(repo_graph.impact(graph, rel))               # reverse: кто зависит от rel
        for nb in neighbors:
            if _hidden(nb) or nb in cand or not _allowed_path(nb):     # denied-сосед не добавляется
                continue
            cand[nb] = {"file": nb, "sources": {"graph"}, "score": 0, "mandatory": False}
            graph_added += 1

    # --- источник 4: УСЛОВНЫЙ semantic-lite (индекс ТОЛЬКО по разрешённым путям) ---
    semantic_used, semantic_reason = False, "детерминированный recall достаточен"
    if deterministic_count < semantic_recall_floor:
        semantic_used = True
        semantic_reason = f"детерминированных кандидатов {deterministic_count} < floor {semantic_recall_floor}"
        try:
            idx = semantic_lite.build_index(root, subdirs=("",), path_filter=_allowed_path)
            for r in semantic_lite.search(idx, query, k=5):
                if _hidden(r["file"]) or not _allowed_path(r["file"]):
                    continue
                c = cand.setdefault(r["file"], {"file": r["file"], "sources": set(), "score": 0,
                                                "mandatory": False})
                c["sources"].add("semantic")
        except Exception:
            semantic_reason += " (semantic-индекс не построен: нет подходящих файлов)"

    # --- access-filter (AFP роли + DataClassificationPolicy child) + классификация ---
    included, excl_access, excl_budget, mandatory_missing = [], [], [], []
    resolved = []
    for rel, c in cand.items():
        f = root / rel
        try:
            content = f.read_text(encoding="utf-8")
        except OSError:
            if c["mandatory"]:
                mandatory_missing.append(rel)
            continue
        data_class = cr.classify(content, path=rel, policy=dcp)
        if data_class == "secret" or data_class not in allowed:
            excl_access.append({"file": rel, "data_class": data_class, "mandatory": c["mandatory"],
                                "sources": sorted(c["sources"])})
            continue
        resolved.append({"file": rel, "sources": sorted(c["sources"]), "mandatory": c["mandatory"],
                         "score": c["score"], "data_class": data_class,
                         "tokens": cr._tokens(content), "content_hash": _hash(content), "sha": sha})

    # --- rerank: обязательные первыми (стабильно), затем по score desc, файл для детерминизма ---
    resolved.sort(key=lambda x: (0 if x["mandatory"] else 1, -x["score"], x["file"]))

    # --- budget: обязательные включаются ВСЕГДА (даже сверх бюджета -> флаг); остальное до лимита ---
    total = 0
    for item in resolved:
        item = dict(item)
        item["reason"] = _reason(item)
        if item["mandatory"]:
            total += item["tokens"]
            item["over_budget"] = total > budget_tokens
            included.append(item)
        elif total + item["tokens"] <= budget_tokens:
            total += item["tokens"]
            item["over_budget"] = False
            included.append(item)
        else:
            excl_budget.append({"file": item["file"], "tokens": item["tokens"]})

    afp_id, afp_hash = cr._policy_fingerprint(afp)
    dcp_id, dcp_hash = cr._policy_fingerprint(dcp)
    allowed_h = hashlib.sha256(",".join(sorted(allowed)).encode()).hexdigest()[:8]
    repo = cr._repo_identity(root, repo_id)

    # v3.6.7d: обязательный контекст, потерянный -> view НЕВАЛИДЕН (execution не получает неполный
    # контекст; особенно нельзя продолжать при утере spec/policy — нужен handoff/фикс policy).
    mandatory_excluded_access = sorted(e["file"] for e in excl_access if e.get("mandatory"))
    invalid_reasons = []
    if mandatory_missing:
        invalid_reasons.append(f"обязательный контекст v1 отсутствует в snapshot: {sorted(mandatory_missing)}")
    if mandatory_excluded_access:
        invalid_reasons.append(f"обязательный контекст v1 запрещён access-policy: {mandatory_excluded_access}")
    cache_key = "|".join([f"repo:{repo}", f"sha:{sha}", f"afp:{afp_id}:{afp_hash}",
                          f"dcp:{dcp_id}:{dcp_hash}", f"allowed:{allowed_h}", f"role:{role}",
                          f"q:{query}", f"b:{budget_tokens}", f"idx:{cr.RETRIEVAL_INDEX_VERSION}"])
    return {
        "kind": "context-engine-view", "schema": "v2-orchestrated", "role": role, "query": query,
        "repo": repo, "sha": sha,
        "valid": not invalid_reasons, "invalid_reasons": invalid_reasons,
        "snapshot_verified": snapshot_verified,
        "sources_used": {"mandatory_v1": len(v1_mandatory or []), "fulltext": deterministic_count,
                         "graph_added": graph_added, "semantic_used": semantic_used,
                         "semantic_reason": semantic_reason},
        "included": included, "excluded_access": excl_access, "excluded_budget": excl_budget,
        "mandatory_missing": sorted(mandatory_missing),
        "mandatory_excluded_access": mandatory_excluded_access,
        # privileged-audit: denied-пути, отсеянные ДО чтения (role-facing их не видит; execution не читал)
        "pre_filtered_denied": pre_filtered_denied,
        "read_paths": sorted(i["file"] for i in included) + sorted(e["file"] for e in excl_access),
        "total_tokens": total, "budget_tokens": budget_tokens,
        # v3.6.7d: orchestrated cache честно ВЫКЛЮЧЕН для полного view до live-квалификации (v3.6.8).
        # cache_key считается (для shadow-сравнения/дедупа), но RetrievalCache к orchestrated view пока
        # НЕ подключён — не заявляем возможность, которой нет в рантайме.
        "cache": {"enabled": False, "reason": "orchestrated cache off до v3.6.8 live qualification"},
        "cache_key": cache_key,
    }


def _reason(item):
    src = ",".join(item["sources"])
    if item["mandatory"]:
        return f"обязательный контекст v1 ({src}) — не теряется на rerank/budget"
    return f"кандидат из [{src}] (score={item['score']})"


def compare_v1(view, v1_files):
    """Сравнение полной v2-цепочки с боевым v1: overlap / только v1 / только v2."""
    v2 = {i["file"] for i in view.get("included", [])}
    v1 = set(v1_files or [])
    return {"overlap": sorted(v1 & v2), "v1_only": sorted(v1 - v2), "v2_only": sorted(v2 - v1),
            "v1_count": len(v1), "v2_count": len(v2)}


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1

    def _opt(n, d=None):
        return argv[argv.index(n) + 1] if n in argv else d
    child = args[0]
    afp, dcp, _ = load_child_policies(child)
    view = build_context(child, _opt("--query", ""), _opt("--role", "executor"),
                         sha=_opt("--sha"), afp=afp, dcp=dcp)
    print(json.dumps(view, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
