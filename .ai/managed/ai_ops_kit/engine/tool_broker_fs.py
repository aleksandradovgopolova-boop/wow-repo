#!/usr/bin/env python3
"""Tool Broker — сателлит файловой системы / git-энфорсмента / путей / команд.

Самодостаточный кластер, вынесенный из tool_broker.py (структурный разрез фасад+сателлит для
снятия с потолка module-size; поведение НЕ менялось). Здесь живут функции, которые не зовут
ничего из фасада: они берут `root`/`policy`/`pre` ПАРАМЕТРАМИ. Фасад импортирует эти имена и
ре-экспортирует их, поэтому публичная поверхность `tool_broker.X` не изменилась.

Содержимое:
  * нормализация/разбор команд для allowlist: `_normalize`, `_first_binary`, `_command_binaries`,
    `_allowlist_scan`, `_allowlist_denial`;
  * ALLOWLIST окружения shell-команд: `scrub_env`;
  * пост-фактум git/FS-энфорсмент (снимок дерева до/после, нарушения, откат): `_revision`,
    `_git_q`, `_porcelain`, `_ignored_under`, `_protected_scan_prefixes`, `_fs_snapshot`,
    `_shell_violations`, `_revert_violations`;
  * лексические/физические проверки путей: `_escapes_root`, `_relativize_inside_root`,
    `_within_root`.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from ai_ops_kit.shared import argv_command
from ai_ops_kit.shared.gitio import git

# Ведущие присваивания, которые в allowlist-режиме запрещены: они меняют не аргументы, а то, ЧТО
# исполнится под разрешённым именем — какой файл найдётся (`PATH=. pytest` запускает ./pytest из
# репо), что подгрузится до main (LD_PRELOAD, NODE_OPTIONS, PYTHONPATH…), какой шелл/пейджер/
# редактор/хелпер позовёт сам инструмент (GIT_*, npm_config_*, SHELL, PAGER), откуда он прочтёт
# конфиг, способный всё это задать (HOME, XDG_CONFIG_HOME -> ~/.gitconfig, ~/.npmrc).
# ПОЧЕМУ ЧЁРНЫЙ СПИСОК, А НЕ БЕЛЫЙ: легитимные команды тестов несут произвольные имена
# (`CI=1`, `NODE_ENV=test`, `DJANGO_SETTINGS_MODULE=…`, `FOO=1`), и белый список ломал бы их без
# выигрыша в главном — какой бинарь запустится, allowlist проверяет отдельно. ЧЕСТНО: список —
# известные векторы подмены загрузки, а не исчерпывающий перечень; сам allowlist — сужение
# поверхности входных бинарников, не песочница (см. SANDBOX_SHELL_ALLOWLIST).
_RISKY_ASSIGNMENT_NAMES = frozenset({
    "PATH", "IFS", "ENV", "BASH_ENV", "SHELL", "SHELLOPTS", "BASHOPTS", "CDPATH", "PS4",
    "HOME", "XDG_CONFIG_HOME", "PAGER", "EDITOR", "VISUAL",
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONBREAKPOINT",
    "PYTHONEXECUTABLE", "PYTHONPLATLIBDIR",
    "NODE_OPTIONS", "NODE_PATH", "PERL5LIB", "PERL5OPT", "PERLLIB", "RUBYOPT", "RUBYLIB",
    "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS",
    # тулчейны сборки: компилятор/обёртка/раннер — это исполняемый файл, заданный переменной
    "RUSTC", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER", "RUSTDOC",
    "GOFLAGS", "GOENV", "GOTOOLCHAIN", "CC", "CXX", "LD",   # LD — точное имя, LD_* — префикс ниже
    "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "MAKEFLAGS", "MFLAGS", "MAKEFILES",
})
# Префиксы: у git и npm десятки переменных, которые исполняют команды (GIT_EXTERNAL_DIFF,
# GIT_SSH_COMMAND, GIT_CONFIG_* -> core.pager/alias; npm_config_script_shell/node_options/
# userconfig), перечислять поимённо — значит пропустить следующую. npm читает имена без учёта
# регистра, поэтому его префикс сверяется в нижнем регистре. CARGO_ — по той же причине
# (CARGO_BUILD_RUSTC_WRAPPER, CARGO_TARGET_<триплет>_RUNNER, CARGO_HOME -> config.toml).
# ЕЩЁ РАЗ ЧЕСТНО: даже с этим списком allowlist — сужение, а не песочница. Разрешённые бинарники
# исполняют произвольное сами (`find -exec`, `awk 'BEGIN{system(…)}'`, `git -c core.pager=…`,
# `make`/`npm run`/`pytest` — код репозитория); изоляция — только контейнер.
_RISKY_ASSIGNMENT_PREFIXES = ("LD_", "DYLD_", "GIT_", "CARGO_")
_RISKY_ASSIGNMENT_PREFIXES_CI = ("npm_config_",)


def _normalize(cmd):
    """Снять поверхностную обфускацию перед текстовыми денай-проверками.

    Снимается три формы, все проверены на векторах (R-38):
      * кавычки: `git pu""sh` -> `git push`;
      * продолжение строки: `git \\`↵`push` -> `git push`. Без этого `GIT_PUSH_RE` не срабатывал
        вовсе — его класс `[^\\n;&|]*` не пересекает перевод строки, а shell команду склеивает;
      * одиночный backslash-escape: `cu\\rl` -> `curl`. `/bin/sh` съедает escape перед обычным
        символом и исполняет команду, а денайлист видел другое слово и молчал.

    ЧЕСТНО, что НЕ раскрывается и здесь раскрыто быть не может: переменные и eval
    (`p=push; git $p`), подстановка команд, кодировки. Это по-прежнему поверхностная
    нормализация, а не разбор shell. Жёсткие гарантии живут не тут: недоставка — в окружении
    без push-credentials, изоляция сети и ФС — в контейнере, сужение входных бинарников —
    в allowlist-режиме (он оба вектора выше ловил и до этой правки)."""
    s = (cmd or "").replace('"', "").replace("'", "")
    # Продолжение строки shell УДАЛЯЕТ, не заменяет пробелом: `cur\`↵`l` исполняется как `curl`.
    # Пробел здесь давал бы `cur l` — денайлист снова не видел бы слова (поймано тестом).
    s = re.sub(r"\\\r?\n", "", s)
    return re.sub(r"\\(.)", r"\1", s)  # escape перед обычным символом: shell его съест


def _risky_assignment(name):
    return (name in _RISKY_ASSIGNMENT_NAMES or name.startswith(_RISKY_ASSIGNMENT_PREFIXES)
            or name.lower().startswith(_RISKY_ASSIGNMENT_PREFIXES_CI))


def _allowlist_scan(cmd):
    """Команда -> (бинарь каждой простой команды, имена ведущих присваиваний) — для allowlist.

    Слова берутся из ТОГО ЖЕ разбора, что у запуска (`argv_command.simple_commands`: кавычки,
    escape, разделители слов — только пробел и таб), а не из `str.split()`. Иначе проверялось
    одно, а исполнялось другое: `A='x pytest' rm` давал бинарь `pytest`, `A=1\x0cpytest rm` —
    тоже, `a/b=c pytest` — `pytest`. Присваивание распознаётся правилом `is_assignment` по СЫРОМУ
    слову (`"FOO"=1` — уже команда). Разбор неоднозначен -> `argv_command.NeedsShell`: вызывающий
    обязан отказать (fail-closed). Присваивания из команд без бинаря (`PATH=.; pytest`) тоже
    возвращаются: оболочка оставляет их в силе для следующих команд."""
    bins, names = [], []
    for words in argv_command.simple_commands(cmd or ""):
        k = 0
        while k < len(words) and argv_command.is_assignment(words[k][1]):
            names.append(words[k][1].split("=", 1)[0])
            k += 1
        if k < len(words):
            bins.append(words[k][0])
    return bins, names


def _command_binaries(cmd):
    """Бинарь каждой простой команды (`;` `&&` `||` `|` `&` перевод строки) — v2.85: закрывает
    обход `a && curl`/`a | nc`. Разбор — `_allowlist_scan`; неоднозначная команда -> NeedsShell."""
    return _allowlist_scan(cmd)[0]


def _first_binary(cmd):
    """Бинарь первой простой команды (пропуская ведущие `ИМЯ=значение`); пусто, если его нет."""
    bins = _command_binaries(cmd)
    return bins[0] if bins else ""


def _allowlist_denial(cmd, allowlist):
    """Причина отказа allowlist-режима или None. Отказ: команду нельзя однозначно разобрать на
    слова; бинарь вне allowlist; ведущее присваивание, подменяющее загрузку (`_risky_assignment`)."""
    try:
        bins, names = _allowlist_scan(cmd)
    except argv_command.NeedsShell as e:
        return (f"команду нельзя однозначно разобрать на слова ({e}) — в allowlist-режиме "
                "проверяется только то, что разбирается так же, как исполнится")
    risky = [n for n in names if _risky_assignment(n)]
    if risky:
        return (f"присваивание {risky} перед командой меняет то, что исполнится под разрешённым "
                "именем (поиск бинаря, предзагрузка, конфиг) — в allowlist-режиме запрещено")
    bad = [b for b in bins if b not in allowlist]
    if bad:
        return f"{bad} не в shell_allowlist {sorted(allowlist)}"
    return None


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
    # P0.5: полный SHA (не --short) — надёжный идентификатор ревизии для evidence. gitio: таймаут.
    code, out, _ = git(root, "rev-parse", "HEAD")
    return out if code == 0 else None


def _git_q(root, *args):
    """git в root без интерактива. -> (rc, stdout). Ошибка git не роняет брокер.
    RAW (не gitio.git): нужен env=scrub_env() и stdout ДОСЛОВНО для `_porcelain`; timeout= задан явно."""
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                           timeout=60, env=scrub_env())
        return r.returncode, (r.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def _porcelain(root):
    """Состояние рабочего дерева -> {"paths": set, "renames": [(old, new)]} (None, если не git).

    Переносы нужны отдельно: `git mv security/x public/x` вынес бы содержимое из protected-пути,
    и откат только исходной стороны оставил бы копию снаружи — то есть обход остался бы рабочим."""
    # -uall: новые файлы перечисляются ПОШТУЧНО. Без него git сворачивает untracked-каталог в одну
    # запись «dir/», и откат нарушения промахивался бы: unlink каталога — не файл, тихий no-op.
    rc, out = _git_q(root, "status", "--porcelain", "-uall")
    if rc != 0:
        return None
    paths, renames = set(), []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:].strip()
        if " -> " in entry:                      # "R  old -> new" / "C  old -> new"
            old, new = (s.strip().strip('"') for s in entry.split(" -> ", 1))
            paths.add(old)
            paths.add(new)
            renames.append((old, new))
        else:
            paths.add(entry.strip('"'))
    return {"paths": paths, "renames": renames}


def _ignored_under(root, prefixes):
    """Игнорируемые файлы ПОД защищёнными префиксами -> {relpath: (size, mtime_ns)}.

    R-43: `git status --porcelain -uall` игнорируемые не показывает вовсе (нужен `--ignored`),
    поэтому запись в игнорируемый файл внутри protected-пути (локальные секреты/оверрайды в
    `production/`, ключевой материал в `security/`, пути под `.ai/`, закрытые игнором при доставке)
    сторож не видел. Скан СУЖЕН до защищённых префиксов НАМЕРЕННО: снимать игнор всего дерева
    (`__pycache__`, `node_modules`, артефакты сборки) на каждой из десятков shell-операций петли —
    значит откатывать законную игнорируемую суету; цена ложных откатов легла бы на любую работу.
    (size, mtime_ns) вместо содержимого: детектит и создание, и правку на месте без чтения байт."""
    if not prefixes:
        return {}
    rc, out = _git_q(root, "ls-files", "-z", "--others", "--ignored", "--exclude-standard",
                     "--", *prefixes)
    if rc != 0:
        return {}
    result = {}
    for rel in out.split("\x00"):
        rel = rel.strip()
        if not rel:
            continue
        try:
            stt = (Path(root) / rel).stat()
            result[rel] = (stt.st_size, stt.st_mtime_ns)
        except OSError:
            continue
    return result


def _protected_scan_prefixes(policy):
    """Префиксы для скана игнорируемых: только protected (R-43 — про защищённые пути).

    write_scope сюда НЕ входит: игнорируемое ВНУТРИ scope законно, а «вне scope» префиксом не
    выразить — это отдельная, более широкая забота, не разрыв #786."""
    return [pre for pre, _appr in getattr(policy, "protected", [])]


def _fs_snapshot(root, policy):
    """Состояние до shell-операции: HEAD + грязные пути + игнорируемые под protected. None -> неприм."""
    if not getattr(policy, "shell_path_guard", False):
        return None
    # нечего защищать -> не платим двумя git status за каждую shell-операцию (петля делает их десятки)
    if not policy.protected and not (getattr(policy, "shell_scope_guard", False) and policy.write_scope):
        return None
    st = _porcelain(root)
    if st is None:
        return None      # не git-дерево: пост-фактум сверка невозможна, честно ничего не обещаем
    rc, head = _git_q(root, "rev-parse", "HEAD")
    return {"dirty": st["paths"], "head": head.strip() if rc == 0 else None,
            "ignored": _ignored_under(root, _protected_scan_prefixes(policy))}


def _shell_violations(root, policy, pre):
    """Что shell РЕАЛЬНО изменил в запрещённых путях: рабочее дерево + новые коммиты."""
    check_scope = bool(getattr(policy, "shell_scope_guard", False))
    touched, renames = set(), []
    now = _porcelain(root)
    if now is not None:
        touched |= (now["paths"] - pre["dirty"])   # только delta: чужую грязь до операции не судим
        renames = now["renames"]
    rc, head_now = _git_q(root, "rev-parse", "HEAD")
    head_now = head_now.strip() if rc == 0 else None
    committed = []
    if pre["head"] and head_now and head_now != pre["head"]:
        rc2, names = _git_q(root, "diff", "--name-only", f"{pre['head']}..{head_now}")
        if rc2 == 0:
            committed = [n for n in names.splitlines() if n.strip()]
            touched |= set(committed)
    # R-43: игнорируемые файлы под protected git-статус не показывает — берём их отдельным сканом
    # и судим дельту (появился новый ИЛИ изменился существующий). `pre_ignored` различает новый
    # (можно удалить) от правки существующего (восстановить нельзя — не под git), это идёт в откат.
    pre_ignored = pre.get("ignored") or {}
    post_ignored = _ignored_under(root, _protected_scan_prefixes(policy))
    ignored_pre = {}
    for rel, tup in post_ignored.items():
        if pre_ignored.get(rel) != tup:              # новый или изменённый на месте
            touched.add(rel)
            ignored_pre[rel] = rel in pre_ignored
    # вынос содержимого ИЗ запрещённого пути: целевая сторона переноса тоже подлежит откату,
    # иначе `git mv security/x public/x` оставлял бы копию снаружи и обход работал бы.
    carried = {new: old for old, new in renames
               if policy.path_violation(old, check_scope=check_scope) and new in touched}
    violations = []
    for rel in sorted(touched):
        why = policy.path_violation(rel, check_scope=check_scope)
        if not why and rel in carried:
            why = f"перенос из запрещённого пути '{carried[rel]}'"
        if why:
            v = {"path": rel, "reason": why, "committed": rel in committed}
            if rel in ignored_pre:
                v["ignored"] = True
                v["ignored_preexisting"] = ignored_pre[rel]
            violations.append(v)
    return violations, head_now


def _revert_violations(root, pre, head_now, violations):
    """Откатить ровно нарушения. Коммиты операции снимаются (движок коммитит сам, не модель)."""
    undone = {"reset_from": None, "restored": [], "removed": [], "failed": []}
    if head_now and pre["head"] and head_now != pre["head"] and any(v["committed"] for v in violations):
        rc, _ = _git_q(root, "reset", "--mixed", pre["head"])
        if rc == 0:
            undone["reset_from"] = head_now
        else:
            undone["failed"].append(f"reset к {pre['head'][:12]} не удался")
    for v in violations:
        rel = v["path"]
        rc, _ = _git_q(root, "cat-file", "-e", f"HEAD:{rel}")
        if rc == 0:
            rc2, _ = _git_q(root, "checkout", "HEAD", "--", rel)
            (undone["restored"] if rc2 == 0 else undone["failed"]).append(rel)
        elif v.get("ignored_preexisting"):
            # R-43: игнорируемый файл СУЩЕСТВОВАЛ до операции и изменён на месте. Его нет в git,
            # прежнего содержимого у нас нет — восстановить нельзя. Честно: обнаружено, откатить не
            # смогли (не молчим и НЕ удаляем чужой файл, выдав удаление за откат).
            undone["failed"].append(
                f"{rel}: изменён игнорируемый файл под защитой — восстановить нельзя (не под git)")
        else:
            fp = Path(root) / rel
            try:
                if fp.is_file() or fp.is_symlink():
                    fp.unlink()
                    undone["removed"].append(rel)
                elif fp.is_dir():
                    # с -uall сюда попасть не должно; если попали — не молчим (тихий no-op в
                    # security-откате хуже отказа: он выглядел бы как успешный откат)
                    undone["failed"].append(f"{rel}: каталог, автоматический откат не выполнен")
                # путь уже отсутствует -> откатывать нечего, это не ошибка
            except OSError as e:
                undone["failed"].append(f"{rel}: {e}")
    return undone


def _escapes_root(rel):
    """Лексически: путь выходит за корень рабочего дерева? (абсолютный или ../ после нормализации).
    Не требует реального root — защищает decide() до любого доступа к ФС."""
    if not rel:
        return False
    if os.path.isabs(rel):
        return True
    norm = os.path.normpath(rel)
    return norm == ".." or norm.startswith(".." + os.sep) or norm.startswith("../")


def _relativize_inside_root(root, rel):
    """Абсолютный путь, физически лежащий ВНУТРИ root -> путь относительно root. Иначе None.

    F-016 (находка живой квалификации, раунд C, задача T2): writer предлагал абсолютный путь
    внутри собственного worktree, брокер отклонял его как traversal (`_escapes_root` судит
    лексически, без знания корня), writer повторял попытку относительным путём. Формально
    безопасно, фактически — ложный отказ и лишний шаг цикла на ровном месте.

    Проверка физическая (resolve), поэтому симлинк наружу сюда не пролезет: путь обязан
    разрешиться внутрь настоящего корня."""
    if not rel or not os.path.isabs(str(rel)):
        return None
    try:
        target = Path(rel).resolve()
        base = Path(root).resolve()
    except OSError:
        return None
    try:
        return target.relative_to(base).as_posix()
    except ValueError:
        return None


def _within_root(root, rel):
    """Belt-and-suspenders: итоговый путь физически внутри root (resolve, без симлинк-побега)."""
    try:
        (Path(root).resolve() / rel).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False
