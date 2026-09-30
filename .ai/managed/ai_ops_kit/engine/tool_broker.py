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

Использование (программно; интегрируется в ai_ops_kit/providers/orchestrator.py):
  from ai_ops_kit.engine.tool_broker import Policy, execute
  pol = Policy(level="controlled-write", write_scope=["src/"])
  ev = execute({"op": "write", "path": "src/a.ts", "content": "..."}, root, pol)

  tool_broker.py --selftest
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

from ai_ops_kit.shared import argv_command

# ВАЖНО (finding аудита исполнения): shell — НЕ полноценная security boundary. Статически
# проверить произвольную команду нельзя, поэтому НА ВХОДЕ для shell действуют только timeout +
# denylist деструктивных команд + scrub_env.
#
# v3.36 (finding живого прогона: `sed -i` правил .github/workflows там, где эквивалентный write
# отклонялся как protected path): последствия shell ПРОВЕРЯЕМЫ, даже когда сама команда — нет.
# Брокер снимает состояние git-дерева до команды и после; если shell изменил protected-путь
# (а при shell_scope_guard — и путь вне write_scope), правка ОТКАТЫВАЕТСЯ, а операция помечается
# запрещённой. Обход перестал быть необнаружимым и безнаказанным — но это пост-фактум, не запрет.
# Откат может НЕ УДАТЬСЯ (права, каталог) — тогда `revert_complete` false и причина начинается с
# «ОТКАТ НЕ УДАЛСЯ» (R-43/#786: раньше заявляла успех безусловно). Успех перечисляет откаченное —
# сторож видит не всё (см. честный список ниже), и список даёт заметить, чего в нём нет.
#
# R-43 закрыт с обеих сторон: (1) сторож не заявляет успех отката, которого не было (#797);
# (2) игнорируемые файлы ВНУТРИ protected-путей теперь в снимке (`_ignored_under`) — запись в них
# больше не проходит молча: новый игнор-файл удаляется, правка существующего на месте ОБНАРУЖИВАЕТСЯ
# и честно доложена как «откатить не смогли» (прежнего содержимого нет — файл не под git).
# Что этим ЕЩЁ НЕ закрыто, честно:
#   * не-git рабочее дерево — сверять не с чем, сторож молчит (в evidence нет fs_guard);
#   * запись ВНЕ корня репозитория (python -c open('/etc/...','w')), чтение чужих файлов, сеть —
#     сторож смотрит только внутрь git-дерева;
#   * игнорируемые файлы ВНЕ protected-путей не сверяются намеренно: снимать игнор всего дерева на
#     каждой shell-операции значило бы откатывать законную суету (__pycache__, node_modules, сборку);
#   * правку существующего игнорируемого файла под protected сторож ОБНАРУЖИТ, но не восстановит
#     (содержимого нет в git) — это отказ с честным отчётом, не тихий пропуск;
#   * write_scope для shell по умолчанию НЕ enforced (см. shell_scope_guard): тот же брокер
#     исполняет подготовку окружения и проверки движка, а они законно пишут вне scope;
#   * побочные эффекты без файлов (внешние вызовы, БД, отправка данных) не откатываются в принципе.
# Полный jail (writable-only worktree, изолированный HOME, сеть off, лимиты) = контейнер.
# Не давать --engine pipeline с живой моделью доступ к ценному приватному репо без надзора.
SHELL_TIMEOUT_DEFAULT = 300   # сек: shell-команда не висит вечно
# v2.85: хвост вывода shell для evidence. 400 было мало — сводка теста / список упавших node-id
# (для structured-id baseline-diff) часто НЕ попадали в окно -> регрессии терялись (fail-open).
SHELL_OUTPUT_TAIL = 4000
_READ_MAX = 20000   # v3.0-rc18: read отдаёт файл С НАЧАЛА до этого потолка (ревьюер верифицирует полноту)

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
def _scrub_output(text):
    """v3.0.11 (finding аудита P1): редактировать секреты в output_tail (read-контент, stdout/stderr shell)
    ПЕРЕД тем, как он попадёт в evidence/отчёт. scrub_env закрывает окружение дочернего процесса, но
    напечатанный тул-ом токен или прочитанный committed .env иначе легли бы в evidence в открытом виде."""
    if not text:
        return text
    try:
        from ai_ops_kit.security import security_scan as _ss
        for _name, _pat in _ss.SECRET_PATTERNS:
            text = _pat.sub("«***REDACTED-SECRET***»", text)
    # СРЕЗ engine РАТЧЕТА 2026-08-12: здесь стояло `pass` с причиной «в худшем случае — без
    # редактирования». Причина была записана, но НЕВЕРНА: «худший случай» этой функции — это
    # напечатанный тулом токен, уехавший в evidence открытым текстом и МОЛЧА, то есть ровно то,
    # против чего функция и существует. Скраб — не украшение вывода, а его условие.
    # Поэтому fail-closed: не смогли отредактировать — не отдаём содержимое вовсе, и причина
    # видна на месте вывода, а не только в логе. Брокер по-прежнему не падает.
    except Exception as _e:  # noqa: BLE001 — сбой скраба не роняет брокер, но и не пропускает контент
        return (f"«***OUTPUT-WITHHELD: скраб секретов не выполнен "
                f"({type(_e).__name__}: {_e}) — содержимое не показано, чтобы не унести секрет "
                f"в evidence***»")
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


# Движок, CI и реестры кита — под owner-approval ТОЛЬКО когда прогон идёт над САМИМ китом (self-host /
# догфуд): кит, развивающий себя, всё ещё может править движок с явным одобрением владельца
# (privileged + protected_path_write), но не молча тем же прогоном, который эти правила ослабляет.
# ПОЧЕМУ не в config/protected-paths.yaml: тот дефолт применяется к КАЖДОЙ дочке, а дочка правит свой
# .github/ в рамках write_scope легитимно (её .github/ уже прикрыт human-approval-доменом security-гейта).
# Навязывать эти пути всем — ломать легитимную доставку дочки (откат PR #504). Поэтому они условны от
# self-host, а не универсальны.
SELF_HOST_PROTECTED = (".github/", "ai_ops_kit/", "registry/")


def _is_kit_self_host(root) -> bool:
    """Репозиторий-цель ЕСТЬ исходники кита (а не .ai/managed-слой дочки)?

    Маркеры берём в корне цели и требуем ВСЕ сразу — это исходники кита, а не совпадение имени:
      * пакет `ai_ops_kit/__init__.py` — сам движок лежит в корне (у дочки он под .ai/managed/, не тут);
      * `VERSION` — файл версии пакета в корне (кит его не раздаёт дочке в корень);
      * `manifest/ai-ops-manifest.yaml` — центральный манифест пакета (в дочку едет под .ai/managed/,
        в корне его нет).
    Тройка вместе однозначно отделяет клон кита от произвольной дочки: у дочки в корне нет ни одного из
    трёх. При отсутствии child_root (root=None) — не self-host (карта пакета остаётся дефолтной)."""
    if not root:
        return False
    root = Path(root)
    return ((root / "ai_ops_kit" / "__init__.py").exists()
            and (root / "VERSION").exists()
            and (root / "manifest" / "ai-ops-manifest.yaml").exists())


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
    if _is_kit_self_host(child_root):   # self-host: движок/CI/реестры под owner-approval (SEAM)
        for p in SELF_HOST_PROTECTED:
            add({"path": p, "approval": "owner_required"})
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


def _canon_rel(rel: str) -> str:
    """Каноническое написание относительного пути ДО сравнения с правилом (R-37).

    Было: сравнение строковым префиксом после одного `strip("/")`. Тогда одно и то же место,
    записанное иначе, правило не накрывало — `./p`, `p//q`, `p/./q` проходили мимо
    protected_paths и write_scope, и запись доходила до диска (проба через execute(): 4 из 5
    написаний перезаписали защищённый файл). `normpath` схлопывает эти формы к одной.

    ЧЕСТНО про границы: нормализация ЛЕКСИЧЕСКАЯ. Регистр она не трогает — им заведует
    `_under(ignore_case=...)`, потому что для запрета и для разрешения ответ РАЗНЫЙ (см. ниже).
    Симлинки сюда НЕ входят и `_within_root` их НЕ ловит (R-42): та проверяет лишь ПОБЕГ за корень,
    поэтому симлинк, чья цель остаётся ВНУТРИ корня (`src/out -> migrations/destructive`), проходил
    и переписывал protected-цель. Их ловит отдельный symlink-target-guard в execute(): он судит
    РАЗЫМЕНОВАННУЮ цель записи по deny-стороне (protected + побег), не трогая write_scope."""
    p = (rel or "").strip().strip("/")
    if not p:
        return ""
    return os.path.normpath(p).strip("/")


def _under(path: str, prefix: str, *, ignore_case: bool = False) -> bool:
    """Путь лежит под префиксом? `ignore_case` НЕ симметричен по умолчанию — и это суть.

    Регистр (второй под-вектор R-37): на регистронезависимой ФС (macOS) `Migrations/x` и
    `migrations/x` — один файл, на Linux — два разных. Соблазн «сравнивать без учёта регистра
    везде» неверен, потому что `_under` обслуживает ДВА правила с противоположной полярностью:

      * protected_paths — ЗАПРЕТ. Пропустить запрещённое хуже, чем лишний раз спросить
        одобрение, поэтому здесь сравнение ШИРОКОЕ: ignore_case=True. Цена — на Linux
        `Migrations/x` попросит approval, хотя это другой файл. Отказ виден и обратим.
      * write_scope — РАЗРЕШЕНИЕ. Здесь широкое сравнение работало бы в обратную сторону:
        `SRC/x` засчитался бы как «в пределах зоны», то есть fail-OPEN. Поэтому строгое.

    Второй довод за константу вместо определения регистрозависимости ФС: вердикт политики
    обязан быть одинаковым на машине владельца и в CI. Судья, который щупает ФС, даёт разные
    вердикты на разных машинах — а на них ссылается evidence, привязанный к SHA."""
    p = _canon_rel(path)
    pre = _canon_rel(prefix)
    if not p or not pre:
        return False
    if ignore_case:
        p, pre = p.casefold(), pre.casefold()
    return p == pre or p.startswith(pre + "/")


# v2.81 Containment: сетевые команды (exfil/доставка в обход движка). Денайлист best-effort —
# НЕ настоящий сетевой jail (это контейнер), но закрывает частые векторы, когда allow_network=False.
NETWORK_RE = re.compile(r"\b(curl|wget|nc|ncat|netcat|ssh|scp|sftp|telnet|rsync|ftp|"
                        r"nmap|dig|nslookup|http|https)\b", re.I)
# git push из tool-loop: доставка ветки/PR — только доверенным кодом движка (pr_open), не моделью
# (finding аудита v2.79 P0.2). ЧЕСТНО (v2.85, уточнено R-38): это best-effort текстовый денай —
# ВТОРОЙ рубеж (defense-in-depth), НЕ гарантия. _normalize снимает кавычки, продолжение строки и
# backslash-escape; ПЕРЕМЕННЫЕ/eval (`p=push; git $p`) статически не ловятся — перечень обходов
# держать в _normalize актуальным, иначе комментарий обещает больше, чем код (тот же класс, что R-33).
# ПЕРВЫЙ рубеж — СРЕДА: песочница credential-less для push (containers/run-sandboxed.sh + Dockerfile:
# credential.helper="", GIT_ASKPASS=/bin/false, GIT_TERMINAL_PROMPT=0; .git-credentials/SSH-agent
# внутрь не проброшены). Это закрывает АВТОМАТИЧЕСКИЕ каналы, которыми git сам добывает креду —
# push через них падёт независимо от regex. ЧЕСТНО, НЕ полная гарантия: GITHUB_TOKEN всё же в
# песочнице (для чтения GitHub) и push-способен — явную доставку им (токен в URL / API из скрипта)
# ловят этот regex + медиатор shell, а не среда. Полная гарантия средой = read-only токен без
# push-scope или host-side чтение GitHub (решение владельца, см. run-sandboxed.sh ОГРАНИЧЕНИЕ).
GIT_PUSH_RE = re.compile(r"\bgit\b[^\n;&|]*\bpush\b", re.I)

# подстановка команд / process substitution — статически не проверить -> в allowlist-режиме денай.
_SUBST_RE = re.compile(r"\$\(|`|<\(|>\(")
# `\r` перед переводом строки или в конце команды — CRLF, а не часть слова (см. execute).
_CR_AT_EOL_RE = re.compile(r"\r+(?=\n)|\r+\Z")


class Policy:
    def __init__(self, level="controlled-write", write_scope=None, confidentiality="internal",
                 approvals=None, child_root=None, shell_mode="unrestricted",
                 shell_allowlist=None, allow_network=True, block_push=False,
                 shell_path_guard=True, shell_scope_guard=False):
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
        # v3.36 (закрытие известного разрыва, см. комментарий в начале модуля): shell статически
        # не проверить, но ПОСЛЕДСТВИЯ shell проверяемы. shell_path_guard=True -> после каждой
        # shell/git-операции брокер сверяет ФАКТИЧЕСКИ изменённые пути с protected_paths и
        # откатывает нарушения (пост-фактум enforcement вместо необнаружимого обхода).
        #   shell_scope_guard: то же для write_scope. По умолчанию ВЫКЛЮЧЕН — тот же брокер
        #   исполняет подготовку окружения и проверки движка (npm ci, сборка), а они законно
        #   пишут lock-файлы и артефакты вне write_scope; включать для tool-loop модели.
        self.shell_path_guard = shell_path_guard
        self.shell_scope_guard = shell_scope_guard

    def _level_ok(self, required):
        return LEVEL_ORDER.index(self.level) >= LEVEL_ORDER.index(required)

    def protected_match(self, rel: str):
        """(prefix, approval) первого protected-правила, накрывающего путь, иначе None.

        ignore_case=True: правило-ЗАПРЕТ сравнивается широко (обоснование — в `_under`)."""
        path = (rel or "").strip("/")
        for pre, appr in self.protected:
            if path and _under(path, pre, ignore_case=True):
                return pre, appr
        return None

    def path_violation(self, rel: str, *, check_scope=True):
        """Единственный судья по пути: protected_paths (+ опционально write_scope).

        Возвращает причину-строку, если путь писать НЕЛЬЗЯ, иначе None. Используется и ветвью
        write в decide(), и пост-фактум сторожем shell — чтобы канал записи не менял вердикт
        (finding живого прогона: `sed -i` правил .github/workflows там, где write отклонялся).

        Полярность сравнения РАЗНАЯ и намеренно (R-37, обоснование в `_under`): protected —
        широко (ignore_case), write_scope — строго. Иначе `SRC/x` засчитался бы «в зоне»."""
        path = (rel or "").strip("/")
        if not path:
            return None
        for pre, appr in self.protected:
            if _under(path, pre, ignore_case=True):
                if self._level_ok("privileged") and "protected_path_write" in self.approvals:
                    return None
                return f"protected path '{pre}' ({appr}) — нужен privileged + approval"
        if check_scope and self.write_scope and not any(_under(path, s) for s in self.write_scope):
            return f"'{path}' вне write_scope {self.write_scope}"
        return None

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
            # protected path -> нужен privileged + approval; вне write_scope -> запрет.
            # Судит path_violation() — тот же код, что и пост-фактум сторож shell.
            why = self.path_violation(path)
            if why:
                return {"allow": False, "reason": why}
            hit = self.protected_match(path)
            if hit:   # накрыт protected-правилом, но есть privileged + approval
                return {"allow": True, "reason": f"protected '{hit[0]}' + approval"}
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
                # иначе `pytest && curl` обходил проверку по первому токену). Слова — по СЫРОЙ
                # команде тем же разбором, что у запуска (кавычки, escape, только пробел/таб):
                # проверяется ровно то, что исполнится; неоднозначное — отказ.
                why = _allowlist_denial(cmd, self.shell_allowlist)
                if why:
                    return {"allow": False, "reason": why}
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


def sandbox_policy(child_root=None, write_scope=None, allow_network=True):
    """v2.81 Containment: усиленная политика МОДЕЛЬНОЙ ПЕТЛИ — shell по allowlist, git push заблокирован,
    allow_network=True по умолчанию (install нуждается в сети). P0 (аудит 04.09): shell_scope_guard=True —
    write_scope enforce-ится и на shell (иначе `echo … > out_of_scope/x` писал мимо scope, а прямой write —
    нет); фаза install берёт install-политику со снятым guard (см. _install_dependencies). Полн. изоляция — контейнер."""
    return Policy(level="execution", child_root=child_root, write_scope=write_scope,
                  shell_mode="allowlist", shell_allowlist=SANDBOX_SHELL_ALLOWLIST,
                  allow_network=allow_network, block_push=True, shell_scope_guard=True)


def execute(action: dict, root, policy: Policy) -> dict:
    """Единственная точка исполнения. ВСЕГДА проверяет policy.decide() первым."""
    root = Path(root)
    # F-016: абсолютный путь ВНУТРИ рабочего корня приводим к относительному до решения политики.
    # Здесь — единственное место, где корень известен по-настоящему (для worktree он не равен
    # child_root), поэтому нормализация живёт тут, а не в decide(). Всё, что не разрешается внутрь
    # корня, остаётся traversal'ом: fail-closed не ослаблен.
    _normalized_from = None
    if action.get("op") in ("read", "write"):
        _relp = _relativize_inside_root(root, action.get("path") or "")
        if _relp:
            _normalized_from = action.get("path")
            action = dict(action, path=_relp)
    # CRLF (команда из файла с Windows-переводами строк): `\r` в конце строки срезаем ДО решения
    # политики — и решение, и запуск видят одну и ту же команду. Срезать только в проверке было
    # бы обходом: `./gradlew\r` оболочка исполняет как другой файл. `\r` в середине строки
    # остаётся и allowlist-режимом отклоняется.
    if action.get("op") in ("shell", "git") and "\r" in (action.get("command") or ""):
        action = dict(action, command=_CR_AT_EOL_RE.sub("", action["command"]))
    d = policy.decide(action)
    ev = {"op": action.get("op"), "target": action.get("path") or action.get("command"),
          "allowed": d["allow"], "reason": d["reason"], "revision": _revision(root)}
    if _normalized_from:
        ev["path_normalized_from"] = _normalized_from
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
        # R-42 (novelty-Watch): decide() судит НАПИСАННЫЙ путь, но write_text РАЗЫМЕНОВЫВАЕТ симлинк.
        # Симлинк, чья цель не покидает корень (`src/out -> migrations/destructive`), проходил
        # _within_root (тот стережёт только ПОБЕГ за корень) и переписывал protected-цель мимо
        # protected_paths/write_scope. Судим ЦЕЛЬ по DENY-стороне (protected + побег через симлинк);
        # write_scope остаётся на НАПИСАННОМ пути — симметрия R-37: судить цель на allow-стороне
        # сделало бы write_scope fail-open (`вне-зоны -> src` прошёл бы как «в зоне»).
        if op == "write":
            _resolved = (root / action["path"]).resolve()
            try:
                _tgt_rel = _resolved.relative_to(root.resolve()).as_posix()
            except ValueError:
                ev.update({"ok": False, "error": "цель симлинка вне корня (containment)",
                           "allowed": False, "reason": "symlink-target-guard: цель вне корня"})
                return ev
            if _tgt_rel != action["path"]:
                _why = policy.path_violation(_tgt_rel, check_scope=False)
                if _why:
                    ev.update({"ok": False, "error": f"симлинк ведёт в '{_tgt_rel}': {_why}",
                               "allowed": False, "reason": f"symlink-target-guard: {_why}"})
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
            # БЕЗ ОБОЛОЧКИ, КОГДА ОНА НЕ НУЖНА (#1157). Команды движка — установка и проверки из
            # профиля (`npm ci`, `pytest -q`, `git status`) — это бинарь и аргументы; оболочка там
            # давала лишь поверхность внедрения. Такие команды идут СПИСКОМ: исполняется ровно
            # argv[0], и денай-проверки выше судят то, что будет исполнено, а не текст до `sh`.
            #
            # ОБОЛОЧКА ОСТАЁТСЯ ОСОЗНАННО — ДЛЯ КОМАНД С ЕЁ СИНТАКСИСОМ. Модель tool-loop пишет
            # конвейеры, `&&`, перенаправления (`echo … > f`), маски — это объявленная возможность
            # op `shell`, и на её ПОСЛЕДСТВИЯХ построен пост-фактум сторож путей ниже. Исполнить
            # такую строку без оболочки значило бы написать свою оболочку («свой tool-loop не
            # наращиваем», AGENTS.md). Контур этой ветки: Policy.decide (уровень, allowlist по
            # КАЖДОМУ сегменту, денай подстановок/сети/push), scrub_env (секретов в окружении нет),
            # timeout, скраб вывода, откат правок protected/вне scope; полная изоляция — контейнер.
            # Флаг сканера на `shell=True` здесь — правда, а не шум: это настоящая поверхность, и
            # прятать её от сканера (`["sh", "-c", …]`) было бы подгонкой замера.
            timeout = action.get("timeout", SHELL_TIMEOUT_DEFAULT)
            # v3.36: снимок ДО команды — основа пост-фактум сторожа путей (см. _fs_snapshot)
            _pre = _fs_snapshot(root, policy)
            try:
                _env = scrub_env()
                try:
                    _argv, _env_extra = argv_command.split(action["command"])
                except argv_command.NeedsShell:
                    _argv = None
                if _argv is None:
                    r = subprocess.run(action["command"], shell=True, cwd=str(root),
                                       capture_output=True, text=True, env=_env,
                                       timeout=timeout)
                else:
                    r = argv_command.run(_argv, cwd=str(root), env={**_env, **_env_extra},
                                         timeout=timeout)
                ev["via_shell"] = _argv is None
                ev.update({"ok": r.returncode == 0, "exit_code": r.returncode,
                           "command": action["command"],
                           "output_tail": _scrub_output((r.stdout + r.stderr)[-SHELL_OUTPUT_TAIL:])})
            except subprocess.TimeoutExpired:
                # finding аудита: без timeout shell мог висеть вечно
                ev.update({"ok": False, "exit_code": None, "command": action["command"],
                           "timed_out": True, "output_tail": f"timeout {timeout}s"})
            # v3.36 Containment: статически shell не проверить, но его ПОСЛЕДСТВИЯ проверяемы.
            # Если команда изменила protected-путь (или, при shell_scope_guard, путь вне
            # write_scope) — правка откатывается, а операция помечается ЗАПРЕЩЁННОЙ. Иначе
            # `sed -i .github/workflows/...` проходил там, где эквивалентный write отклонялся.
            if _pre is not None:
                _viol, _head_now = _shell_violations(root, policy, _pre)
                if _viol:
                    _undone = _revert_violations(root, _pre, _head_now, _viol)
                    ev["allowed"] = False
                    ev["ok"] = False
                    # R-43 (#786): причина заявляла «правка откачена» БЕЗУСЛОВНО — в том числе
                    # когда откат провалился и правка ОСТАЛАСЬ на диске (замер: каталог r-x ->
                    # EACCES на unlink, файл цел). Отчёт, называющий дыру закрытой, опаснее самой
                    # дыры: читающий «откачено» на диск не пойдёт. Разбор — docs/audit-report R-43.
                    _names = "; ".join(f"{v['path']}: {v['reason']}" for v in _viol[:5])
                    _done = _undone["restored"] + _undone["removed"]
                    _complete = not _undone["failed"]
                    ev["fs_guard"] = {"violations": _viol, "reverted": _undone,
                                      "revert_complete": _complete}
                    if not _complete:
                        _what = ("ОТКАТ НЕ УДАЛСЯ — правка ОСТАЛАСЬ на диске: "
                                 + "; ".join(str(x) for x in _undone["failed"][:5]))
                    elif _done:                      # перечень: сторож видит не всё (R-43)
                        _what = "откачено (" + ", ".join(_done[:5]) + ")"
                    else:
                        _what = "откатывать было нечего (пути уже отсутствуют)"
                    ev["reason"] = f"shell изменил запрещённые пути; {_what} | {_names}"
                elif _pre["dirty"] is not None:
                    ev["fs_guard"] = {"violations": []}
    except (OSError, KeyError) as e:
        ev.update({"ok": False, "error": str(e)})
    return ev


# Ре-экспорт кластера файловой системы / git-энфорсмента / путей / команд (сателлит
# tool_broker_fs). Разрез фасад+сателлит снят с потолка module-size; поведение НЕ менялось.
# Публичная поверхность `tool_broker.X` не изменилась: внешний код и тесты по-прежнему зовут
# эти имена как атрибуты tool_broker. `execute`/`Policy`/`sandbox_policy` выше используют их как
# модульные глобали (импортированы здесь на уровне модуля). Сателлит фасад НЕ импортирует — ни
# одного обратного ребра (все вынесенные функции берут root/policy/pre параметрами).
from ai_ops_kit.engine.tool_broker_fs import (  # noqa: E402  — ре-экспорт в конце модуля
    _allowlist_denial,
    _command_binaries,
    _escapes_root,
    _first_binary,
    _fs_snapshot,
    _git_q,
    _ignored_under,
    _normalize,
    _porcelain,
    _protected_scan_prefixes,
    _relativize_inside_root,
    _revert_violations,
    _revision,
    _shell_violations,
    _within_root,
    scrub_env,
)


def main(argv):
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
