#!/usr/bin/env python3
"""commit_policy.py (v3.19.0 Engineering Operating Model, WP1) — CommitContract: что вправе стать
ОДНИМ коммитом.

Аудит, из которого выросло: коммит-дисциплина в ките держалась на прозе (AGENTS.md «перед коммитом —
обязательно») и на аккуратности человека. Машиночитаемого контракта не было, поэтому воспроизводились
три класса ошибок:
  * обновление managed-слоя кита ехало ОДНИМ коммитом с продуктовыми правками — такой коммит нельзя
    откатить по отдельности, а именно откат обновления кита нужен чаще всего;
  * в коммит попадали файлы, которых там быть не должно (`.env`, приватные ключи, worktree прогонов);
  * сообщение вида «fix»/«обновление» не давало ни причины, ни evidence — история переставала быть
    источником истины через неделю.

Здесь — ДЕТЕРМИНИРОВАННАЯ проверка предполагаемого коммита по списку путей и сообщению. Модуль НЕ
запускает git, НЕ читает содержимое файлов и НЕ ходит в сеть: на вход — данные, на выход — вердикт.
Это делает его тестируемым без инфраструктуры и пригодным и для движка, и для человека, и для хука.

Разделение силы (owner-правило):
  * ЖЁСТКИЕ инварианты блокируют всегда (смешение зон, запрещённые файлы, секрет в сообщении,
    protected_paths без ApprovalRecord) — это не вкусовщина, а необратимый вред;
  * МЯГКИЕ правила (размер коммита, ссылка на WorkItem/evidence, широкий scope) по умолчанию
    СОВЕТУЮТ; репозиторий может поднять их до блока через `engineering_operating_model.commit.enforce`.

Секреты: модуль не сканирует содержимое (это делает tools/security_scan.py — не дублируем). Здесь —
дополняющая проверка: ИМЕНА файлов, которые не должны попадать в историю, и литеральные секреты
в тексте СООБЩЕНИЯ.

CLI:
  commit_policy.py <child_root> --files a.py b.py --message "feat: ..." [--workitem WI-1]
                   [--task-type ENGINEERING] [--approval AR-1] [--json]
  commit_policy.py --selftest
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

# --- зоны репозитория: смешивать в одном коммите нельзя -----------------------------------------
# kit_managed  — слой, которым владеет `ai-ops update` (перезаписывается целиком);
# kit_config   — конфиг связи с китом (версия/политики);
# product      — код и тесты продукта;
# runtime      — артефакты прогонов (в историю попадать не должны вовсе).
ZONE_KIT_MANAGED = "kit_managed"
ZONE_KIT_CONFIG = "kit_config"
ZONE_PRODUCT = "product"
ZONE_RUNTIME = "runtime"

# Пары зон, которые нельзя мешать в одном коммите (откат должен быть независимым).
_UNMIXABLE = frozenset({frozenset({ZONE_KIT_MANAGED, ZONE_PRODUCT}),
                        frozenset({ZONE_KIT_CONFIG, ZONE_PRODUCT})})

# Файлы, которых не должно быть в истории ни при каких условиях (проверка по ИМЕНИ, не по содержимому).
_FORBIDDEN_NAMES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".npmrc", ".netrc", ".pypirc")
_FORBIDDEN_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks", ".ppk")
_ENV_ALLOWED = (".env.example", ".env.sample", ".env.template", ".env.dist")

# Литеральные секреты в ТЕКСТЕ сообщения (в конфигах допустимы только ссылки env:/secret:).
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),                    # OpenAI-совместимые ключи
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),  # GitHub-токены
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                        # AWS access key id
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),             # Slack
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),          # приватный ключ целиком
)

# Сообщения-заглушки: не несут ни причины, ни evidence.
_PLACEHOLDER_MESSAGES = ("wip", "fix", "fixes", "фикс", "update", "updates", "обновление", "правка",
                         "tmp", "temp", "test", "тест", "asdf", ".", "..", "минор", "мелочь")

# Мягкие пороги по умолчанию (калибруются на реальных прогонах).
DEFAULTS = {
    "enforce": "advise",            # advise | block — сила МЯГКИХ правил
    "max_files": 40,                # больше — коммит перестаёт быть обозримым в ревью
    "max_top_level_dirs": 4,        # шире — признак нескольких задач в одном коммите
    "min_message_chars": 15,
    "require_workitem": True,       # ссылка на WorkItem в сообщении
    "require_evidence": True,       # ссылка на доказательство (PR/SHA/отчёт/selftest)
}

# Типы задач, для которых мягкие правила про WorkItem/evidence вообще применяются.
_TRACKED_TASK_TYPES = ("ENGINEERING", "AI_FEATURE", "PRODUCT", "CRITICAL")

_WORKITEM_RE = re.compile(r"\b(?:WI|WP|RR|DP|EV)-\d+\b", re.IGNORECASE)
_EVIDENCE_RE = re.compile(r"(selftest|self-test|evidence|доказательств|PR #\d+|#\d+|"
                          r"\b[0-9a-f]{7,40}\b|validate_|doctor|CI)", re.IGNORECASE)


def _norm(path) -> str:
    """Нормализовать путь: слэши вперёд, снять ведущие './'. НЕ использовать lstrip('./') —
    он срезает и ведущую точку тоже, превращая '.ai/managed/x' в 'ai/managed/x'."""
    p = str(path).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def zone_of(path: str) -> str:
    """Зона репозитория для пути. Детерминированно, без обращения к файловой системе."""
    p = _norm(path)
    if p.startswith(".ai/managed/") or p == ".ai/managed":
        return ZONE_KIT_MANAGED
    if p in (".ai-ops.yaml", ".ai-ops.yml") or p.startswith(".ai/generated/"):
        return ZONE_KIT_CONFIG
    if p.startswith(".ai/runtime/") or p.startswith(".ai/worktrees/"):
        return ZONE_RUNTIME
    return ZONE_PRODUCT


def classify_paths(files):
    """-> {zone: [paths]} (только непустые зоны)."""
    out = {}
    for f in files or ():
        out.setdefault(zone_of(f), []).append(f)
    return out


def _forbidden_file(path: str):
    """-> причина, если файла не должно быть в истории; иначе None."""
    p = _norm(path)
    name = p.rsplit("/", 1)[-1]
    if name.startswith(".env") and name not in _ENV_ALLOWED:
        return f"{p}: файл окружения не коммитим (только .env.example)"
    if name in _FORBIDDEN_NAMES:
        return f"{p}: файл с учётными данными не коммитим"
    if any(name.endswith(s) for s in _FORBIDDEN_SUFFIXES):
        return f"{p}: приватный ключ/сертификат не коммитим"
    return None


def _match_protected(path: str, protected):
    """Совпадение пути с protected_paths (префикс каталога или точный файл)."""
    p = _norm(path)
    for pat in protected or ():
        q = _norm(pat)
        if not q:
            continue
        if q.endswith("/") and p.startswith(q):
            return q
        if p == q or p.startswith(q.rstrip("/") + "/"):
            return q
    return None


def check_commit(files, message="", workitem=None, task_type="ENGINEERING",
                 protected_paths=(), approvals=(), policy=None):
    """Проверить предполагаемый коммит. -> CommitVerdict (dict).

    allowed=False только по ЖЁСТКИМ инвариантам либо по мягким при enforce=block.
    """
    pol = dict(DEFAULTS)
    pol.update({k: v for k, v in (policy or {}).items() if k in DEFAULTS})
    files = [str(f) for f in (files or ())]
    msg = (message or "").strip()
    hard, soft = [], []

    # --- ЖЁСТКИЕ инварианты -----------------------------------------------------------------------
    if not files:
        hard.append({"rule": "empty_commit", "detail": "нет файлов — коммит не создаётся"})

    zones = classify_paths(files)
    present = set(zones)
    for pair in _UNMIXABLE:
        if pair <= present:
            a, b = sorted(pair)
            hard.append({"rule": "zone_mixing",
                         "detail": f"в одном коммите зоны {a} и {b} — откат обновления кита станет "
                                   f"невозможен отдельно от продуктовых правок; разделите коммиты",
                         "paths": sorted(zones[a])[:5] + sorted(zones[b])[:5]})

    if ZONE_RUNTIME in present:
        hard.append({"rule": "runtime_artifacts",
                     "detail": "артефакты прогонов (.ai/runtime, .ai/worktrees) не коммитим — "
                               "добавьте в .gitignore",
                     "paths": sorted(zones[ZONE_RUNTIME])[:5]})

    for f in files:
        why = _forbidden_file(f)
        if why:
            hard.append({"rule": "forbidden_file", "detail": why})

    for pat in _SECRET_PATTERNS:
        if pat.search(msg):
            hard.append({"rule": "secret_in_message",
                         "detail": "в сообщении коммита литеральный секрет — история необратима, "
                                   "отзовите ключ и перепишите сообщение"})
            break

    touched_protected = sorted({m for f in files if (m := _match_protected(f, protected_paths))})
    if touched_protected and not [a for a in (approvals or ()) if a]:
        hard.append({"rule": "protected_without_approval",
                     "detail": f"затронуты protected_paths ({', '.join(touched_protected)}) без "
                               f"ApprovalRecord — нужно одобрение человека",
                     "paths": touched_protected})

    tracked = str(task_type).upper() in _TRACKED_TASK_TYPES
    if tracked and (not msg or msg.lower().strip(" .:-") in _PLACEHOLDER_MESSAGES):
        hard.append({"rule": "placeholder_message",
                     "detail": f"сообщение '{msg or '—'}' не объясняет ни что, ни зачем"})

    # --- МЯГКИЕ правила ---------------------------------------------------------------------------
    if len(files) > pol["max_files"]:
        soft.append({"rule": "large_commit",
                     "detail": f"{len(files)} файлов > порога {pol['max_files']} — ревью такого "
                               f"коммита вырождается в формальность"})

    # Считаем именно КАТАЛОГИ: файлы в корне (VERSION, CHANGELOG.md, README.md…) — это один бакет,
    # иначе релизный коммит, честно правящий корневые доки, выглядел бы «размазанным по 14 каталогам».
    top = {seg[0] for f in files if len(seg := _norm(f).split("/")) > 1}
    root_files = any(len(_norm(f).split("/")) == 1 for f in files)
    if len(top) > pol["max_top_level_dirs"]:
        shown = ", ".join(sorted(top)[:6]) + (", + файлы в корне" if root_files else "")
        soft.append({"rule": "broad_scope",
                     "detail": f"затронуто {len(top)} верхнеуровневых каталогов ({shown}) — "
                               f"похоже на несколько задач в одном коммите"})

    if tracked and len(msg) < pol["min_message_chars"]:
        soft.append({"rule": "short_message",
                     "detail": f"сообщение короче {pol['min_message_chars']} символов — "
                               f"причина изменения не зафиксирована"})

    if tracked and pol["require_workitem"] and not (workitem or _WORKITEM_RE.search(msg)):
        soft.append({"rule": "no_workitem",
                     "detail": "нет ссылки на WorkItem — коммит не связывается с задачей и её evidence"})

    if tracked and pol["require_evidence"] and not _EVIDENCE_RE.search(msg):
        soft.append({"rule": "no_evidence",
                     "detail": "в сообщении нет ссылки на доказательство (selftest/CI/SHA/PR) — "
                               "заявление о работоспособности непроверяемо"})

    blocking_soft = pol["enforce"] == "block"
    allowed = not hard and not (blocking_soft and soft)
    return {
        "kind": "CommitVerdict",
        "allowed": allowed,
        "enforce": pol["enforce"],
        "violations": hard,
        "advisories": soft,
        "zones": {z: len(v) for z, v in sorted(zones.items())},
        "files_count": len(files),
        "task_type": str(task_type).upper(),
    }


def policy_from_config(root):
    """Прочитать `engineering_operating_model.commit` из .ai-ops.yaml. Нет файла/ключа -> {}."""
    for name in (".ai-ops.yaml", ".ai-ops.yml"):
        cfg = Path(root) / name
        if not cfg.is_file():
            continue
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 — битый конфиг не должен ронять проверку коммита
            return {}
        return ((data.get("engineering_operating_model") or {}).get("commit") or {})
    return {}


def protected_from_config(root):
    """protected_paths из .ai-ops.yaml (список). Нет — пустой список."""
    for name in (".ai-ops.yaml", ".ai-ops.yml"):
        cfg = Path(root) / name
        if not cfg.is_file():
            continue
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            return []
        return list(data.get("protected_paths") or [])
    return []


def _fmt(v):
    lines = [f"commit: {'ДОПУСТИМ' if v['allowed'] else 'ЗАБЛОКИРОВАН'} "
             f"({v['files_count']} файлов, зоны: {', '.join(v['zones']) or '—'}; enforce={v['enforce']})"]
    for x in v["violations"]:
        lines.append(f"  ✗ {x['rule']}: {x['detail']}")
    for x in v["advisories"]:
        lines.append(f"  ⚠ {x['rule']}: {x['detail']}")
    if not v["violations"] and not v["advisories"]:
        lines.append("  ✓ нарушений и замечаний нет")
    return "\n".join(lines)


def summary_line(root):
    """Строка для doctor: объявленная политика коммитов (без обращения к git)."""
    pol = dict(DEFAULTS)
    pol.update({k: v for k, v in policy_from_config(root).items() if k in DEFAULTS})
    src = "из .ai-ops.yaml" if policy_from_config(root) else "по умолчанию"
    return (f"политика коммитов ({src}): enforce={pol['enforce']}, "
            f"max_files={pol['max_files']}, workitem={'да' if pol['require_workitem'] else 'нет'}, "
            f"evidence={'да' if pol['require_evidence'] else 'нет'}")


def selftest():
    import tempfile
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    def rules(v, key="violations"):
        return {x["rule"] for x in v[key]}

    good = check_commit(["src/app.py", "tests/test_app.py"],
                        "feat(app): считать ретраи по WI-12; доказано selftest", workitem="WI-12")
    expect("нормальный продуктовый коммит допустим", good["allowed"] and not good["violations"])

    mixed = check_commit([".ai/managed/tools/x.py", "src/app.py"], "chore: WI-1 обновление + правка, selftest")
    expect("managed + продукт -> zone_mixing блокирует",
           not mixed["allowed"] and "zone_mixing" in rules(mixed))

    cfg_mixed = check_commit([".ai-ops.yaml", "src/app.py"], "chore: WI-1 конфиг и код, selftest")
    expect("kit_config + продукт -> zone_mixing блокирует", "zone_mixing" in rules(cfg_mixed))

    kit_only = check_commit([".ai/managed/tools/x.py", ".ai-ops.yaml"],
                            "chore(ai-ops): update kit 3.9.1 -> 3.18.0 по WI-9, doctor OK")
    expect("managed + kit_config вместе — допустимая пара (это один откатываемый шаг)",
           kit_only["allowed"])

    rt = check_commit([".ai/runtime/last-update-report.json"], "chore: WI-2 отчёт прогона, selftest")
    expect("артефакт прогона -> блок", "runtime_artifacts" in rules(rt))

    wt = check_commit([".ai/worktrees/task-x/file.py"], "chore: WI-2 worktree, selftest")
    expect("worktree прогона -> блок", "runtime_artifacts" in rules(wt))

    env = check_commit([".env"], "chore: WI-3 окружение, selftest")
    expect(".env -> forbidden_file", "forbidden_file" in rules(env))
    expect(".env.example -> допустим",
           "forbidden_file" not in rules(check_commit([".env.example"], "docs: WI-3 пример env, selftest")))
    expect("приватный ключ -> forbidden_file",
           "forbidden_file" in rules(check_commit(["deploy/server.pem"], "chore: WI-3 ключ, selftest")))
    expect("id_rsa -> forbidden_file",
           "forbidden_file" in rules(check_commit([".ssh/id_rsa"], "chore: WI-3 ключ, selftest")))

    sec = check_commit(["src/a.py"], "fix(auth): WI-4 ключ sk-abcdefghijklmnopqrstuvwx попал в конфиг, selftest")
    expect("литеральный секрет в сообщении -> блок", "secret_in_message" in rules(sec))
    expect("ghp_-токен в сообщении -> блок",
           "secret_in_message" in rules(check_commit(
               ["a.py"], "chore: WI-4 отозвать ghp_abcdefghijklmnopqrstuvwxyz012345, selftest")))
    expect("ссылка env:KEY секретом НЕ считается",
           "secret_in_message" not in rules(check_commit(
               ["a.py"], "chore(cfg): WI-4 брать ключ через env:ANTHROPIC_API_KEY, selftest")))

    prot = check_commit([".github/workflows/ci.yml"], "ci: WI-5 поправить матрицу, selftest",
                        protected_paths=[".github/workflows/"])
    expect("protected_paths без approval -> блок", "protected_without_approval" in rules(prot))
    expect("protected_paths с ApprovalRecord -> допустим",
           check_commit([".github/workflows/ci.yml"], "ci: WI-5 поправить матрицу, selftest",
                        protected_paths=[".github/workflows/"], approvals=["AR-1"])["allowed"])

    ph = check_commit(["src/a.py"], "wip")
    expect("сообщение-заглушка -> блок", "placeholder_message" in rules(ph))
    expect("QUICK не требует WorkItem/evidence (мягкие правила не применяются)",
           check_commit(["src/a.py"], "почистить лог", task_type="QUICK")["advisories"] == [])

    soft = check_commit(["src/a.py"], "рефакторинг вынес хелпер наружу")
    expect("нет WorkItem/evidence -> СОВЕТЫ, но коммит допустим (enforce=advise)",
           soft["allowed"] and {"no_workitem", "no_evidence"} <= rules(soft, "advisories"))
    expect("enforce=block поднимает советы до блока",
           not check_commit(["src/a.py"], "рефакторинг вынес хелпер наружу",
                            policy={"enforce": "block"})["allowed"])

    big = check_commit([f"src/m{i}.py" for i in range(50)], "feat: WI-6 большая правка, selftest")
    expect("много файлов -> large_commit (совет)", "large_commit" in rules(big, "advisories"))
    broad = check_commit(["a/x", "b/x", "c/x", "d/x", "e/x"], "feat: WI-7 широкая правка, selftest")
    expect("много верхних каталогов -> broad_scope (совет)", "broad_scope" in rules(broad, "advisories"))
    expect("корневые файлы НЕ считаются каталогами (релизный коммит не «размазан»)",
           "broad_scope" not in rules(check_commit(
               ["VERSION", "CHANGELOG.md", "README.md", "ROADMAP.md", "AGENTS.md", "FILE_INDEX.md",
                "tools/x.py", "rules/core/y.md"],
               "release(3.19.0): WI-7 срез 1, selftest"), "advisories"))
    expect("каталоги считаются и при наличии корневых файлов",
           "broad_scope" in rules(check_commit(
               ["VERSION", "a/x", "b/x", "c/x", "d/x", "e/x"], "feat: WI-7 правка, selftest"), "advisories"))

    expect("пустой список файлов -> блок", "empty_commit" in rules(check_commit([], "feat: WI-8, selftest")))

    expect("зоны определяются детерминированно",
           zone_of(".ai/managed/tools/x.py") == ZONE_KIT_MANAGED
           and zone_of(".ai-ops.yaml") == ZONE_KIT_CONFIG
           and zone_of(".ai/runtime/x.json") == ZONE_RUNTIME
           and zone_of("src/app.ts") == ZONE_PRODUCT)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        expect("нет .ai-ops.yaml -> политика по умолчанию", policy_from_config(root) == {})
        (root / ".ai-ops.yaml").write_text(
            "engineering_operating_model:\n  commit:\n    enforce: block\n    max_files: 5\n"
            "protected_paths: [.github/workflows/]\n", encoding="utf-8")
        p = policy_from_config(root)
        expect("политика читается из .ai-ops.yaml", p.get("enforce") == "block" and p.get("max_files") == 5)
        expect("protected_paths читаются из конфига", protected_from_config(root) == [".github/workflows/"])
        expect("summary_line отражает конфиг", "enforce=block" in summary_line(root))
        (root / ".ai-ops.yaml").write_text("{{ битый yaml", encoding="utf-8")
        expect("битый конфиг не роняет проверку", policy_from_config(root) == {})

    print("commit_policy selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    root = argv[0] if argv and not argv[0].startswith("--") else "."
    files, message, workitem, task_type, approvals = [], "", None, "ENGINEERING", []
    it = iter(argv)
    for a in it:
        if a == "--files":
            for nxt in it:
                if str(nxt).startswith("--"):
                    a = nxt
                    break
                files.append(nxt)
            else:
                a = None
        if a == "--message":
            message = next(it, "") or ""
        elif a == "--workitem":
            workitem = next(it, None)
        elif a == "--task-type":
            task_type = next(it, "ENGINEERING") or "ENGINEERING"
        elif a == "--approval":
            approvals.append(next(it, None))
    v = check_commit(files, message, workitem, task_type,
                     protected_paths=protected_from_config(root),
                     approvals=approvals, policy=policy_from_config(root))
    print(json.dumps(v, ensure_ascii=False, indent=2) if "--json" in argv else _fmt(v))
    return 0 if v["allowed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
