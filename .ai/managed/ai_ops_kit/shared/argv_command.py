#!/usr/bin/env python3
"""Команда-строка -> список аргументов БЕЗ оболочки (#1157).

ЗАЧЕМ. Кит запускал команды строкой через оболочку там, где она не нужна:
команда тестов из профиля (`pytest -q`, `npm run test`, `./mvnw -q test`) — это бинарь и
аргументы, ни одного оператора. Оболочка там не давала ничего, кроме поверхности внедрения: всё,
что попало в строку, становилось кодом `/bin/sh`. Сканер безопасности честно ставил на эти места
флаг, и в дочке он уезжал в раздел «адресат — сопровождающий кита», до которого не доходил.

ЧТО ДЕЛАЕТ. `split(cmd)` разбирает строку по правилам POSIX-оболочки для того подмножества, где
оболочка ничего не исполняет сама: слова, одинарные и двойные кавычки, экранирование, ведущие
присваивания `ИМЯ=значение`. Всё, что оболочка ВЫЧИСЛЯЕТ (конвейер, `&&`, `;`, перенаправление,
подстановка `$…`/обратные кавычки, маски файлов, `~`, встроенные команды вроде `cd`), разбором не
подменяется: `split` поднимает `NeedsShell`, и вызывающий решает сам — идти в оболочку осознанно
или отказать. Молча «примерно так же» не исполняется ничего: команда либо идёт ровно как в
оболочке, либо не идёт этим путём вовсе.

`run(argv, …)` — запуск списком. Нет бинаря — код 127, как у оболочки («command not found»):
вызывающие (сбор доказательств, regression evidence) уже различают этот код, и смена исключения
на код сохраняет их поведение.

Модуль без импортов из `ai_ops_kit`: его зовут и ядро (`engine.tool_broker`), и гейты
(`gates.regression_evidence`).
"""
from __future__ import annotations

import errno
import os
import re
import subprocess
from collections.abc import Mapping, Sequence

# Символы, которые оболочка ВЫЧИСЛЯЕТ, когда они стоят вне кавычек. `~` и `#` особые только в
# начале слова — внутри (`a#b`, `x~y`) это обычные буквы, и так же их читает оболочка.
_SHELL_ONLY = set("|&;<>()`$*?[{\n")
_WORD_START_ONLY = set("~#")
# Встроенные команды: отдельного бинаря нет или он ведёт себя иначе (`echo` в dash разворачивает
# `\n`, `/bin/echo` — нет). Запуск списком изменил бы результат, поэтому они — дело оболочки.
SHELL_BUILTINS = frozenset({
    "cd", "export", "source", ".", "exit", "set", "unset", "alias", "unalias", "eval", "exec",
    "trap", "ulimit", "umask", "type", "command", "read", "wait", ":", "shift", "return",
    "readonly", "local", "times", "hash", "getopts", "jobs", "fg", "bg", "echo",
})
# Зарезервированные слова: на месте команды оболочка читает их как синтаксис (`if`, `for`, `!`),
# а запуск списком искал бы бинарь с таким именем. `{` отсекает `_SHELL_ONLY` раньше.
SHELL_RESERVED = frozenset({
    "!", "if", "then", "else", "elif", "fi", "do", "done", "case", "esac", "while", "until",
    "for", "in", "}", "[[", "]]", "function", "select", "time",
})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def is_assignment(word: str) -> bool:
    """Ведущее слово — присваивание окружения (`NAME=value`), а не команда.

    Имя по POSIX: `[A-Za-z_][A-Za-z0-9_]*`. `a/b=c` и `1A=b` — НЕ присваивания: и оболочка, и
    `split` исполняют их как команду. Одно определение на кит: allowlist брокера
    (`tool_broker_fs._allowlist_scan`) судит по нему же — иначе проверялся бы один бинарь,
    а исполнялся другой."""
    return bool(_ASSIGNMENT.match(word))

NOT_FOUND_RC = 127       # соглашение POSIX-оболочки: команда не найдена
NOT_EXECUTABLE_RC = 126  # найдена, но не исполняема


class NeedsShell(ValueError):
    """Команду без оболочки исполнить нельзя: в ней есть то, что оболочка вычисляет сама."""


def _double_quoted(s: str, i: int) -> tuple[str, int]:
    """Содержимое двойных кавычек, начиная с `s[i] == '"'` -> (значение, индекс за кавычкой)."""
    buf: list[str] = []
    j, n = i + 1, len(s)
    while True:
        if j >= n:
            raise NeedsShell('незакрытая кавычка "')
        d = s[j]
        if d == '"':
            return "".join(buf), j + 1
        if d in "$`":                      # подстановка работает и внутри двойных кавычек
            raise NeedsShell(f"подстановка «{d}» внутри кавычек")
        if d == "\\" and j + 1 < n and s[j + 1] in '$`"\\\n':
            if s[j + 1] != "\n":           # экранированный перевод строки — продолжение
                buf.append(s[j + 1])
            j += 2
            continue
        buf.append(d)
        j += 1


def _words(s: str) -> list[tuple[str, str]]:
    """Строка -> [(значение слова, сырой текст слова)]. Сырой текст нужен, чтобы отличить
    присваивание `A=1` от аргумента `"A=1"` в кавычках — оболочка их тоже различает."""
    words: list[tuple[str, str]] = []
    cur: list[str] = []
    raw: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in " \t":
            if raw:
                words.append(("".join(cur), "".join(raw)))
                cur, raw = [], []
            i += 1
        elif c == "\\":
            if i + 1 >= n:
                raise NeedsShell("обратная косая черта в конце команды")
            if s[i + 1] != "\n":           # `\`+перевод строки — продолжение, а не символ
                cur.append(s[i + 1])
                raw.append(s[i:i + 2])
            i += 2
        elif c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise NeedsShell("незакрытая кавычка '")
            cur.append(s[i + 1:j])
            raw.append(s[i:j + 1])
            i = j + 1
        elif c == '"':
            value, j = _double_quoted(s, i)
            cur.append(value)
            raw.append(s[i:j])
            i = j
        elif c in _SHELL_ONLY or (c in _WORD_START_ONLY and not raw):
            raise NeedsShell(f"оболочка вычисляет «{c!s}»" if c != "\n" else "перевод строки")
        else:
            cur.append(c)
            raw.append(c)
            i += 1
    if raw:
        words.append(("".join(cur), "".join(raw)))
    return words


def _value_has_tilde_expansion(raw_value: str) -> bool:
    """`~` вне кавычек в начале значения присваивания или после `:` — оболочка его развернёт
    (`FOO=~/x`, `PATH=a:~/bin`). Экранированный `\\~` и `~` в кавычках — обычная буква."""
    i, n, prev_unquoted = 0, len(raw_value), ":"      # начало значения ведёт себя как после `:`
    while i < n:
        c = raw_value[i]
        if c == "\\":
            i, prev_unquoted = i + 2, ""
        elif c == "'":
            j = raw_value.find("'", i + 1)
            i, prev_unquoted = (n if j < 0 else j + 1), ""
        elif c == '"':
            i, prev_unquoted = _double_quoted(raw_value, i)[1], ""   # с учётом `\"` внутри
        else:
            if c == "~" and prev_unquoted == ":":
                return True
            prev_unquoted = c
            i += 1
    return False


def split(command: str | Sequence[object]) -> tuple[list[str], dict[str, str]]:
    """Команда -> (argv, env из ведущих присваиваний). Список принимается как уже разобранный.

    Поднимает `NeedsShell`, если без оболочки команду исполнить так же нельзя."""
    if isinstance(command, (list, tuple)):
        if not command:
            raise NeedsShell("пустая команда")
        return [str(a) for a in command], {}
    words = _words(str(command))
    env: dict[str, str] = {}
    while words and is_assignment(words[0][1]):
        value, raw = words.pop(0)
        name = raw.split("=", 1)[0]
        if _value_has_tilde_expansion(raw[len(name) + 1:]):
            raise NeedsShell(f"оболочка развернула бы «~» в значении {name}")
        env[name] = value[len(name) + 1:]
    if not words:
        raise NeedsShell("команды нет" if env else "пустая команда")
    argv = [w for w, _ in words]
    if words[0][1] in SHELL_RESERVED:               # сырое: `"if"` в кавычках — просто имя
        raise NeedsShell(f"«{argv[0]}» — зарезервированное слово оболочки")
    if argv[0] in SHELL_BUILTINS:
        raise NeedsShell(f"«{argv[0]}» — встроенная команда оболочки")
    return argv, env


Word = tuple[str, str]                  # (значение слова, сырой текст) — как у `_words`
_DUP_TARGET = re.compile(r"[0-9]+-?|-")


class _Commands:
    """Накопитель `simple_commands`: слова текущей команды и готовые команды."""

    def __init__(self) -> None:
        self.commands: list[list[Word]] = []
        self.words: list[Word] = []
        self.cur: list[str] = []
        self.raw: list[str] = []
        self.redirect_target = False       # следующее слово — цель перенаправления, не аргумент
        self.dup_target = False            # цель `<&`/`>&`: только номер потока и/или `-`

    def end_word(self) -> None:
        if not self.raw:
            return
        if self.redirect_target:
            # bash режет `>&-marker` на `>&-` и КОМАНДУ `marker`; dash — синтаксическая ошибка.
            # Разборщики расходятся -> только однозначная цель, остальное — отказ.
            if self.dup_target and not _DUP_TARGET.fullmatch("".join(self.raw)):
                raise NeedsShell("цель `<&`/`>&` — не номер потока и не `-`")
            self.redirect_target = self.dup_target = False
        else:
            self.words.append(("".join(self.cur), "".join(self.raw)))
        self.cur, self.raw = [], []

    def end_command(self) -> None:
        self.end_word()
        if self.redirect_target:
            raise NeedsShell("перенаправление без цели")
        if self.words:
            self.commands.append(self.words)
        self.words = []


def _double_quoted_lenient(s: str, i: int) -> tuple[str, int]:
    """Как `_double_quoted`, но `$ИМЯ` внутри кавычек допускает (на бинарь он не влияет);
    отказ — только на подстановку КОМАНДЫ (`$(`, обратная кавычка)."""
    buf: list[str] = []
    j, n = i + 1, len(s)
    while True:
        if j >= n:
            raise NeedsShell('незакрытая кавычка "')
        d = s[j]
        if d == '"':
            return "".join(buf), j + 1
        if d == "`" or (d == "$" and s[j + 1:j + 2] == "("):
            raise NeedsShell("подстановка команды внутри кавычек")
        if d == "\\" and j + 1 < n and s[j + 1] in '$`"\\\n':
            if s[j + 1] != "\n":
                buf.append(s[j + 1])
            j += 2
            continue
        buf.append(d)
        j += 1


def _reject_control_chars(s: str) -> None:
    for c in s:
        if (c < " " and c not in "\t\n") or c == "\x7f":
            raise NeedsShell(f"управляющий символ {ord(c):#04x} — разделяет ли он слова, "
                             "зависит от разборщика")


def simple_commands(command: str) -> list[list[Word]]:
    """Строка оболочки -> простые команды, каждая — список слов `(значение, сырой текст)`.

    Для allowlist брокера: он обязан сверять ТЕ ЖЕ слова, что исполнит оболочка (и `split`).
    Делит как оболочка: слова — только по пробелу и табу, кавычки и escape — по правилам POSIX
    (как `_words`), команды — по `;`, `&`, `|` (`&&`, `||` — те же символы) и переводу строки
    вне кавычек; комментарий `#` в начале слова — до конца строки; перенаправление вместе с его
    целью (`> out`, `2>&1`) словом команды не считается.

    Всё, что нельзя разобрать однозначно, — `NeedsShell`, а не «примерно»: незакрытая кавычка,
    подстановка команды (`$(`, обратная кавычка, `<(`), скобки подоболочки, here-document,
    управляющие символы (`\\x00`–`\\x1f`, кроме таба и перевода строки, и `\\x7f`) где угодно и
    не-ASCII пробелы вне кавычек: `str.split()` делит по ним, а оболочка — нет."""
    _reject_control_chars(command)
    acc = _Commands()
    s, i = command, 0
    n = len(s)
    while i < n:
        c = s[i]
        if c in " \t":
            acc.end_word()
            i += 1
        elif c in ";&|\n":
            acc.end_command()
            i += 1
        elif c == "#" and not acc.raw:
            j = s.find("\n", i)
            i = n if j < 0 else j
        elif c == "\\":
            if i + 1 >= n:
                raise NeedsShell("обратная косая черта в конце команды")
            if s[i + 1] != "\n":           # `\`+перевод строки — продолжение, а не символ
                acc.cur.append(s[i + 1])
                acc.raw.append(s[i:i + 2])
            i += 2
        elif c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise NeedsShell("незакрытая кавычка '")
            acc.cur.append(s[i + 1:j])
            acc.raw.append(s[i:j + 1])
            i = j + 1
        elif c == '"':
            value, j = _double_quoted_lenient(s, i)
            acc.cur.append(value)
            acc.raw.append(s[i:j])
            i = j
        elif c == "`" or (c == "$" and s[i + 1:i + 2] == "("):
            raise NeedsShell("подстановка команды")
        elif c in "()":
            raise NeedsShell("скобки подоболочки")
        elif c in "<>":
            if acc.raw and "".join(acc.raw).isdigit():
                acc.cur, acc.raw = [], []  # `2>`: цифры перед оператором — номер потока
            else:
                acc.end_word()
            if acc.redirect_target:
                raise NeedsShell("перенаправление без цели")
            # Оператор — только POSIX-токен: `<` `>` `>>` `<&` `>&` `<>` `>|` (`<<`/`<<-` — отказ).
            # Дальше — слово-цель (для `<&`/`>&` это цифра или `-`, но разбирается как любое
            # слово). `|`, `&`, `;` сразу за оператором — НЕ часть оператора: это разделитель,
            # и до цели он даёт отказ «перенаправление без цели». Прежний жадный цикл вбирал
            # `|`/`&`/`-` в оператор, и `pytest >-|marker` прятал команду marker в «цель».
            nxt = s[i + 1:i + 2]
            if c == "<" and nxt == "<":
                raise NeedsShell("here-document")
            two = (c == ">" and nxt in (">", "&", "|")) or (c == "<" and nxt in ("&", ">"))
            acc.redirect_target = True
            acc.dup_target = two and nxt == "&"
            i += 2 if two else 1
        elif c.isspace():
            raise NeedsShell(f"нестандартный пробел U+{ord(c):04X} вне кавычек")
        else:
            acc.cur.append(c)
            acc.raw.append(c)
            i += 1
    acc.end_command()
    return acc.commands


def run(argv: Sequence[str], *, cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None, timeout: float | None = None,
        text: bool = True) -> subprocess.CompletedProcess:
    """Запуск списком, без оболочки. Нет бинаря -> код 127, не исполняем (в т.ч. файл без `#!`) -> 126.

    `subprocess.TimeoutExpired` пробрасывается: у каждого вызывающего своя реакция на таймаут."""
    try:
        return subprocess.run(list(argv), cwd=cwd, env=env, timeout=timeout,
                              capture_output=True, text=text)
    except FileNotFoundError:
        if cwd is not None and not os.path.isdir(cwd):
            raise                          # нет каталога, а не бинаря — это не «команда не найдена»
        rc, why = NOT_FOUND_RC, "команда не найдена"
    except PermissionError:
        rc, why = NOT_EXECUTABLE_RC, "нет права на исполнение"
    except OSError as e:
        if e.errno != errno.ENOEXEC:
            raise
        # Файл исполняемый, но без строки `#!`: оболочка запустила бы его сама через `sh`, а
        # запуск списком — нет. Молча подменять интерпретатор не будем: код 126 и причина.
        rc, why = NOT_EXECUTABLE_RC, "не программа и без строки #! — без оболочки не запускается"
    msg = f"{argv[0]}: {why}\n"
    return subprocess.CompletedProcess(list(argv), rc, "" if text else b"",
                                       msg if text else msg.encode("utf-8"))
