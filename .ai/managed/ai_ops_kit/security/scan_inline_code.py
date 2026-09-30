"""Inline-код интерпретатору в `.py` и `.sh`: поверхность, когда код собран подстановкой (#1161).

Сателлит `security_scan` (фасад импортирует отсюда `inline_code_lines`). Вторая половина #1161:
первая (`scan_exec_call`) разбирает запуск команды в файле с `child_process` и видит inline-код только
в JS-форме `spawnSync("node", ["-e", …])`. Отдельный файл, а не продолжение `scan_exec_call`: там
вместе с этим правилом модуль вставал на порог монолита в 700 строк, а правило цельное — свой лексер
оболочки, свой разбор Python через `ast` — и зависит от соседа одной строкой (`_имя_бинаря`).

Своей точки входа у сателлита нет осознанно: он обслуживает сканер, а не запускается сам. Импортов
из `ai_ops_kit` нет по той же причине, что у соседей: в CI дочки сканер бежит КАК СКРИПТ и грузит
сателлиты по пути от `__file__`.
"""
from __future__ import annotations

import ast
import re
from collections import Counter


def _имя_бинаря(значение: str) -> str:
    """Последний сегмент пути без каталогов и без регистра: `/usr/bin/Python3` -> `python3`.

    Копия одноимённой функции `scan_exec_call`: импорт соседа сломал бы загрузку по пути.
    """
    return значение.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()


# ─── inline-код интерпретатору в .py и .sh (#1161, вторая половина) ──────────────────────────────
#
# Первая половина (#1162) видела inline-код только в JS-форме `spawnSync("node", ["-e", …])` и только
# в файле с `child_process`. Независимый замер 28.09.2026 на ии-среде нашёл четыре непомеченных места
# в `.sh` и `.py`: `node -e '<код>'`, `python3 -c "…"`, `["node", "-e", f"…"]` в subprocess.
#
# ЧТО ФЛАГУЕТСЯ: интерпретатору (`node`, `python3`, `bash`…) передан КОД, СОБРАННЫЙ ПОДСТАНОВКОЙ:
#   * флагом кода (`-c`, `-e`, `--eval`…) — в Python f-строка, склейка, `.format`, `%` или имя, в
#     оболочке `$VAR`, `${…}`, `$(…)` или обратные кавычки внутри слова кода (в одинарных кавычках
#     подстановки нет);
#   * через stdin — heredoc с НЕЗАКАВЫЧЕННЫМ ограничителем и подстановкой в теле (`python3 - <<EOF`),
#     here-string (`node <<< "$X"`) или конвейер (`echo "$X" | bash`) в интерпретатор без сценария.
# Это ровно внедрение: данные становятся ТЕКСТОМ ПРОГРАММЫ, и кавычка в значении ломает её границу.
#
# ЛИТЕРАЛЬНЫЙ КОД НЕ ФЛАГУЕТСЯ И НЕ СЧИТАЕТСЯ — РЕШЕНИЕ, А НЕ ПРОПУСК. `node -e '<код>'` с данными
# через окружение или stdin — это файл сценария, записанный строкой: исполнится ровно то, что
# написано в файле, как у `node script.mjs`, который сканер не флагует тоже. Отдельная строка отчёта
# «inline-код литералом: N» не дала бы судье ни адреса, ни действия — число без адресата учит
# пролистывать отчёт (#1146). Литералом считается только слово, где подстановки нет ДОКАЗАННО.
#
# ЦЕНА НАЗВАНА. Имя интерпретатора В ПЕРЕМЕННОЙ (`"$PY" -c …`) не распознаётся: правило ищет
# литеральное имя, иначе флагом стал бы и `grep -e "$x"`. Слово с подстановкой на месте сценария
# (`bash "$SCRIPT" -c "$x"`) считается сценарием: всё за ним — его аргументы; поэтому и россыпь опций
# из переменной перед флагом кода (`node $OPTS -e "$x"`) читается как сценарий — это пропуск, и он
# назван, а не спрятан.
_ИНЛАЙН_ИНТЕРПРЕТАТОР = re.compile(
    r"(node|nodejs|bun|deno|python[\d.]*|ruby|perl|php|sh|bash|zsh|dash|ksh)(?:\.exe)?")
# Короткие флаги кода ПО СЕМЕЙСТВАМ: общий набор принял бы `node -r mod` (preload) и `sh -e`
# (errexit) за код. Буква ищется в склейке (`-Sc`, `-pe`, `-lne`, `-euc`): код идёт следующим словом.
_ИНЛАЙН_БУКВЫ = {"node": "ep", "nodejs": "ep", "bun": "ep", "deno": "", "ruby": "e",
                 "perl": "eE", "php": "r", "python": "c"}
# Короткие опции СО ЗНАЧЕНИЕМ. Нужны с двух сторон: значение (`-W ignore`, `-o pipefail`) — не
# сценарий, а буква значения в склейке (`-Mfeature`, `-rset`) — не флаг кода, хотя в ней есть `e`.
_ИНЛАЙН_ЗНАЧЕНИЯ = {"node": "rC", "nodejs": "rC", "bun": "r", "deno": "", "ruby": "ICEFr",
                    "perl": "IM", "php": "dcz", "python": "WXQ"}
_ИНЛАЙН_ДЛИННЫЕ = ("--eval", "--print", "--command")
_ИНЛАЙН_ДЛИННЫЕ_ЗНАЧЕНИЯ = frozenset({"--require", "--import", "--loader", "--experimental-loader",
                                      "--conditions", "--input-type", "--env-file"})
# Слово-сценарий: дальше идут аргументы ЕМУ, а не флаги интерпретатору (`python3 x.py -c "$v"`).
_ИНЛАЙН_СЦЕНАРИЙ = re.compile(r"\.(?:py|m?js|cjs|ts|rb|pl|php|sh|bash)$")


def _инлайн_семейство(слово: str):
    """-> (буквы флага кода, это python, буквы опций со значением); None — не интерпретатор."""
    m = _ИНЛАЙН_ИНТЕРПРЕТАТОР.fullmatch(_имя_бинаря(слово))
    if not m:
        return None
    имя = "python" if m.group(1).startswith("python") else m.group(1)
    return _ИНЛАЙН_БУКВЫ.get(имя, "c"), имя == "python", _ИНЛАЙН_ЗНАЧЕНИЯ.get(имя, "oO")


def _вид_флага(текст: str, подст: bool, семейство) -> str:
    """-> `next` (код — следующее слово), `attached` (в этом же слове), `stop`, `stdin` или ``."""
    буквы, python, значения = семейство
    if any(текст.startswith(f + "=") for f in _ИНЛАЙН_ДЛИННЫЕ):
        return "attached"                               # `--eval=…`: подстановка — в этом слове
    if подст:
        return ""
    if текст in _ИНЛАЙН_ДЛИННЫЕ or (текст == "eval" and not буквы):   # `deno eval <код>`
        return "next"
    if текст == "--" or (python and текст == "-m") or _ИНЛАЙН_СЦЕНАРИЙ.search(текст):
        return "stop"
    if re.fullmatch(r"-[A-Za-z]+", текст):
        for б in текст[1:]:
            if б in буквы:
                return "next"
            if б == "s" and буквы == "c" and not python:
                return "stdin"                          # `bash -s a b`: программа — со входа
            if б in значения:
                return ""                               # дальше в склейке — значение опции
    return ""


def _берёт_значение(предыдущее, семейство) -> bool:
    """Предыдущее слово — опция, чьё значение стоит отдельным словом (`-W ignore`, `-o pipefail`)."""
    if предыдущее is None or предыдущее[1]:
        return False
    текст = предыдущее[0]
    if текст in _ИНЛАЙН_ДЛИННЫЕ_ЗНАЧЕНИЯ:
        return True
    return bool(re.fullmatch(r"-[A-Za-z]+", текст)) and текст[-1] in семейство[2]


def verdict(слова, семейство) -> str:
    """`слова` — [(текст, подставлено)] после имени интерпретатора. -> что получает интерпретатор.

    `подстановка` — флаг кода и код собран; `литерал` — флаг кода и код написан; `сценарий` —
    дальше аргументы файлу-сценарию; `stdin` — ни кода, ни сценария: программа придёт на вход.
    Разбор останавливается на первом флаге кода: всё за словом кода — аргументы программе
    (`sh -c 'echo "$1"' _ "$x"` безопасен: данные идут отдельным словом). Флаг кода последним словом
    значит, что код дописывают позже (`cmd.append(code)`) — собирают, а не пишут: подстановка.
    """
    предыдущее = None
    for k, (текст, подст) in enumerate(слова):
        вид = _вид_флага(текст, подст, семейство)
        if вид == "attached":
            return "подстановка" if подст else "литерал"
        if вид == "next":
            return "подстановка" if k + 1 >= len(слова) or слова[k + 1][1] else "литерал"
        if вид in ("stop", "stdin"):
            # `-s` у оболочки: программа приходит на вход, а слова за ним — ЕЁ позиционные
            # аргументы (`curl … | bash -s -- --yes`), а не сценарий.
            return "сценарий" if вид == "stop" else "stdin"
        # Позиционное слово не после опции со значением — сценарий, литерал он или подстановка.
        if not текст.startswith("-") and not _берёт_значение(предыдущее, семейство):
            return "сценарий"
        предыдущее = (текст, подст)
    return "stdin"


def code_is_interpolated(слова, семейство) -> bool:
    """Интерпретатору передан флагом кода код, собранный подстановкой?"""
    return verdict(слова, семейство) == "подстановка"


# ─── Python: разбор через ast ────────────────────────────────────────────────────────────────────
#
# КОНСТАНТА ФАЙЛА — ЛИТЕРАЛ (судья по 86b11bd4). Код, собранный ТОЛЬКО из литералов и констант этого
# же файла, исполняет ровно то, что написано в файле, — внедрять туда нечего, как и в литерал. Это
# точность, а не подгонка замера: единственный новый адрес ии-среды (`proba_pamyati_polnaya.py:90`)
# подставлял в код `SREDA = "/…"`, и судья признал его шумом.
# Константой считается имя, которому на уровне модуля БЕЗУСЛОВНО и ОДИН раз присвоено литеральное
# значение, и которое больше НИГДЕ в файле не связывается: ни присвоением в функции, ни параметром,
# ни `for`/`with`/`except`/импортом, ни `global`/`nonlocal`. Проверка по всему файлу, а не по
# области видимости, — строже нужного: одноимённая локальная где угодно лишает имя статуса константы
# (в сторону лишнего флага). Чистые обёртки литерала (`textwrap.dedent`, `inspect.cleandoc`, `str`,
# `.strip()`, `.format(…)` и `.join(…)` из литералов) литерал не портят.
#
# FAIL-CLOSED (судья, второй круг). Пространство имён модуля можно переписать мимо присвоения:
# `globals()[…] = …`, `setattr(mod, …)`, `vars()`, `__dict__`, `from x import *`. Если в файле есть
# хоть одно из этого — констант в файле НЕТ. Затенённая присвоением обёртка (`str = …`, `dedent = …`)
# перестаёт быть чистой. ГРАНИЦА НАЗВАНА: другой модуль, пишущий `mod.CODE = …`, сканер ОДНОГО
# файла не видит — константа здесь означает «в этом файле значение не меняется», а не больше.
_ЧИСТЫЕ_МЕТОДЫ = frozenset({"strip", "lstrip", "rstrip", "lower", "upper", "format", "join",
                            "replace"})
_ЧИСТЫЕ_ФУНКЦИИ: dict = {"dedent": ("textwrap",), "cleandoc": ("inspect",), "str": ()}
_ИМЕНА_ОБЁРТОК = frozenset({"dedent", "cleandoc", "str", "textwrap", "inspect"})
# Ключ в словаре констант, который не может быть именем Python: обёртки в этом файле затенены.
_ОБЁРТКИ_ЗАТЕНЕНЫ = "<обёртки затенены>"
_ПЕРЕПИСЬ_ИМЁН = frozenset({"globals", "setattr", "vars"})


def _чистая_функция(f, конст: dict) -> bool:
    if _ОБЁРТКИ_ЗАТЕНЕНЫ in конст:
        return False
    if isinstance(f, ast.Name):
        return f.id in _ЧИСТЫЕ_ФУНКЦИИ
    return (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
            and f.value.id in _ЧИСТЫЕ_ФУНКЦИИ.get(f.attr, ()))


def _литерал(узел, конст: dict):
    """Значение выражения, если оно собрано только из литералов и констант файла, иначе None."""
    if isinstance(узел, ast.Constant) and isinstance(узел.value, (str, int, float)):
        return str(узел.value)
    if isinstance(узел, ast.Name):
        return конст.get(узел.id)
    if isinstance(узел, ast.FormattedValue):
        return _литерал(узел.value, конст)
    if isinstance(узел, (ast.JoinedStr, ast.List, ast.Tuple)):
        части = [_литерал(ч, конст) for ч in (узел.values if isinstance(узел, ast.JoinedStr)
                                              else узел.elts)]
        return None if None in части else "".join(ч for ч in части if ч is not None)
    if isinstance(узел, ast.BinOp) and isinstance(узел.op, (ast.Add, ast.Mod)):
        лево, право = _литерал(узел.left, конст), _литерал(узел.right, конст)
        if лево is None or право is None:
            return None
        return лево + право if isinstance(узел.op, ast.Add) else лево
    if isinstance(узел, ast.Call) and not any(isinstance(a, ast.Starred) for a in узел.args):
        f, получатель = узел.func, ""
        if isinstance(f, ast.Attribute) and f.attr in _ЧИСТЫЕ_МЕТОДЫ:
            получатель = _литерал(f.value, конст)
        elif not _чистая_функция(f, конст):
            return None
        аргументы = [_литерал(a, конст) for a in [*узел.args, *(к.value for к in узел.keywords)]]
        if получатель is None or None in аргументы:
            return None
        return получатель + "".join(a for a in аргументы if a is not None)
    return None


def _связывания(дерево) -> Counter:
    """Сколько раз каждое имя связывается где угодно в файле (присвоение, параметр, импорт…)."""
    счёт: Counter = Counter()
    for у in ast.walk(дерево):
        if isinstance(у, ast.Name) and not isinstance(у.ctx, ast.Load):
            счёт[у.id] += 1
        elif isinstance(у, ast.arg):
            счёт[у.arg] += 1
        elif isinstance(у, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            счёт[у.name] += 1
        elif isinstance(у, ast.alias):
            счёт[(у.asname or у.name).split(".")[0]] += 1
        elif isinstance(у, (ast.Global, ast.Nonlocal)):
            счёт.update(у.names)
        elif isinstance(у, ast.ExceptHandler) and у.name:
            счёт[у.name] += 1
        elif isinstance(у, (ast.MatchAs, ast.MatchStar)) and у.name:
            счёт[у.name] += 1
        elif isinstance(у, ast.MatchMapping) and у.rest:
            счёт[у.rest] += 1
    return счёт


def _пространство_переписывается(дерево) -> tuple:
    """-> (имена модуля переписываются мимо присвоения, обёртки затенены присвоением)."""
    переписывается, затенены = False, False
    for у in ast.walk(дерево):
        if (isinstance(у, ast.Call) and isinstance(у.func, ast.Name) and у.func.id in _ПЕРЕПИСЬ_ИМЁН
                or isinstance(у, ast.Attribute) and у.attr == "__dict__"
                or isinstance(у, ast.ImportFrom) and any(a.name == "*" for a in у.names)):
            переписывается = True
        имя = (у.id if isinstance(у, ast.Name) and not isinstance(у.ctx, ast.Load)
               else у.arg if isinstance(у, ast.arg)
               else у.name if isinstance(у, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
               else None)
        затенены = затенены or имя in _ИМЕНА_ОБЁРТОК
    return переписывается, затенены


def _константы(дерево) -> dict:
    """Имена-константы файла -> значение. По порядку: константа из констант выше — тоже константа."""
    счёт = _связывания(дерево)
    переписывается, затенены = _пространство_переписывается(дерево)
    конст: dict = {_ОБЁРТКИ_ЗАТЕНЕНЫ: ""} if затенены else {}
    if переписывается:
        return конст
    for st in дерево.body:
        if isinstance(st, ast.Assign) and len(st.targets) == 1:
            цель, значение = st.targets[0], st.value
        elif isinstance(st, ast.AnnAssign) and st.value is not None:
            цель, значение = st.target, st.value
        else:
            continue
        if isinstance(цель, ast.Name) and счёт[цель.id] == 1:
            v = _литерал(значение, конст)
            if v is not None:
                конст[цель.id] = v
    return конст


def _py_слово(узел, конст: dict) -> tuple:
    """Элемент списка аргументов -> (известный текст, подставлено)."""
    v = _литерал(узел, конст)
    if v is not None:
        return v, False
    if isinstance(узел, ast.JoinedStr):                 # известен только литеральный префикс
        префикс = []
        for ч in узел.values:
            if not isinstance(ч, ast.Constant):
                break
            префикс.append(str(ч.value))
        return "".join(префикс), True
    if isinstance(узел, ast.BinOp):
        return _литерал(узел.left, конст) or "", True  # `"--eval=" + code`
    return "", True


def _py_семейство(узел, конст: dict):
    v = None if isinstance(узел, (ast.List, ast.Tuple)) else _литерал(узел, конст)
    if v is not None:
        return _инлайн_семейство(v)
    if (isinstance(узел, ast.Attribute) and узел.attr == "executable"
            and isinstance(узел.value, ast.Name) and узел.value.id == "sys"):
        return _инлайн_семейство("python")              # `sys.executable` — тот же python
    return None


def _развернуть(элементы) -> list:
    """`[*["-c", code]]` — звёздочка по литеральному списку: его элементы стоят на её месте."""
    out: list = []
    for э in элементы:
        if isinstance(э, ast.Starred) and isinstance(э.value, (ast.List, ast.Tuple)):
            out += _развернуть(э.value.elts)
        else:
            out.append(э)
    return out


# ПОЗИЦИОННЫЕ ФОРМЫ: аргументы команды — аргументы вызова, а не список. Значение — (индекс
# программы, идёт ли за ней `argv[0]`): `os.execlp("node", "node", "-e", code)` повторяет имя, и
# повтор иначе читался бы сценарием.
_ПОЗИЦИОННЫЕ = {"create_subprocess_exec": (0, False),
                "execl": (0, True), "execle": (0, True), "execlp": (0, True), "execlpe": (0, True),
                "spawnl": (1, True), "spawnle": (1, True), "spawnlp": (1, True),
                "spawnlpe": (1, True)}


def _последовательности(дерево):
    """Все последовательности аргументов команды: списки/кортежи и позиционные вызовы."""
    for у in ast.walk(дерево):
        if isinstance(у, (ast.List, ast.Tuple)):
            yield _развернуть(у.elts)
        elif isinstance(у, ast.Call):
            имя = у.func.attr if isinstance(у.func, ast.Attribute) else getattr(у.func, "id", "")
            if имя in _ПОЗИЦИОННЫЕ:
                начало, argv0 = _ПОЗИЦИОННЫЕ[имя]
                args = _развернуть(у.args)
                yield args[начало:начало + 1] + args[начало + 1 + argv0:]


# Запасной разбор файла, который `ast` не принял (Python 2, обрывок): построчно и в сторону лишнего
# флага — литерал интерпретатора, литерал флага кода и за ним НЕ простая строка.
_PY_ЗАПАСНОЙ = re.compile(
    r"""(?:["'](?:[^"'\n]*/)?(?:node|nodejs|bun|deno|python[\d.]*|ruby|perl|php|sh|bash|zsh)["']"""
    r"""|\bsys\.executable)\s*,.*?["'](?:-[A-Za-z]*[ceEpr]|--(?:eval|print|command))["']"""
    r"""\s*(?:,\s*(?![rRuU]?["'])|[\])])""")


def _py_строки(text: str) -> list:
    """Строки .py, где интерпретатору передан код, собранный подстановкой."""
    try:
        дерево = ast.parse(text)
    except (SyntaxError, ValueError):
        return [n for n, s in enumerate(text.splitlines(), 1) if _PY_ЗАПАСНОЙ.search(s)]
    конст, out = _константы(дерево), set()
    for элементы in _последовательности(дерево):
        # Интерпретатор может стоять не первым: `["ssh", host, "bash", "-c", f"…"]`.
        for k, элемент in enumerate(элементы):
            семейство = _py_семейство(элемент, конст)
            if семейство and code_is_interpolated(
                    [_py_слово(э, конст) for э in элементы[k + 1:]], семейство):
                out.add(элемент.lineno)
                break
    return sorted(out)


# ─── оболочка: лексер ────────────────────────────────────────────────────────────────────────────
#
# ОБОЛОЧКА ЧИТАЕТСЯ ЛЕКСЕРОМ, А НЕ РЕГУЛЯРКОЙ ПО СТРОКЕ. Код в `node -e '…'` занимает десятки строк
# (ии-среда, `health-monitor.sh`), а подстановка бывает вложенной: `"$(python3 -c "print($X)")"` —
# команда ВНУТРИ `$(…)` тоже команда, и её код тоже собран. Лексер знает ровно то, что нужно правилу:
# границы слов и простых команд, три вида кавычек, `$…`/`` `…` ``, комментарии, конвейеры и тела
# heredoc (их текст — данные, а не команды: апостроф в теле иначе сбил бы разбор до конца файла).
# Heredoc и here-string остаются в команде СЛОВОМ-МЕТКОЙ (`<<0`, `<<<`): по ней видно, что
# интерпретатор получает программу на вход и собрана ли она подстановкой.
_SH_СУФФИКСЫ = (".sh", ".bash", ".zsh", ".ksh")
_SH_ШЕБАНГ = re.compile(r"#!\s*(?:\S*/)?(?:env\s+(?:-\S+\s+)*)?(?:ba|z|k|da)?sh\b")
_SH_ИМЯ = re.compile(r"[A-Za-z_]\w*|[0-9@*#?$!-]")
_SH_ОГРАНИЧИТЕЛЬ = re.compile(r"[^\s;&|<>()]*")
# Подстановка в теле heredoc без кавычек у ограничителя: `$X`, `${…}`, `$(…)`, обратная кавычка.
_SH_ПОДСТАНОВКА_В_ТЕЛЕ = re.compile(r"(?<!\\)(?:\$[A-Za-z_{(0-9@*#?!$-]|`)")
_SH_МЕТКА_HEREDOC = re.compile(r"<<(\d+)")


class _Оболочка:
    """Лексер оболочки: простые команды как [(текст слова, есть подстановка, строка)]."""

    def __init__(self, text: str) -> None:
        self.t, self.i, self.строка = text, 0, 1
        self.команды: list = []
        self.heredoc: list = []
        self.тела: dict = {}            # номер heredoc -> в теле есть подстановка
        self.входы: dict = {}           # номер команды -> номер команды, чей вывод идёт ей на вход
        self._труба_от: int | None = None
        self._цель = False              # следующее слово — цель перенаправления (`> out`), не аргумент

    def _добавить(self, команда: list) -> None:
        if self._труба_от is not None:
            self.входы[len(self.команды)] = self._труба_от
            self._труба_от = None
        self.команды.append(list(команда))

    def разобрать(self, до_скобки: bool = False) -> None:
        t, команда, глубина = self.t, [], 0
        слово: list = []                                 # [части, подстановка, строка] или пусто

        def начать() -> list:
            if not слово:
                слово.extend([[], False, self.строка])
            return слово[0]

        def конец(команду: bool = False) -> None:
            if слово and self._цель:
                self._цель = False                       # `> /dev/null`: имя файла — не аргумент
            elif слово:
                команда.append(("".join(слово[0]), слово[1], слово[2]))
            слово.clear()
            if команду and команда:
                self._добавить(команда)
                команда.clear()

        while self.i < len(t):
            c = t[self.i]
            if c == "\n":
                конец(True)
                self.строка, self.i = self.строка + 1, self.i + 1
                self._пропустить_heredoc()
            elif c in " \t":
                конец()
                self.i += 1
            elif c == "\\":
                if t[self.i + 1:self.i + 2] == "\n":
                    self.строка += 1
                else:
                    начать().append(t[self.i + 1:self.i + 2])
                self.i += 2
            elif c == "#" and not слово:
                конец_строки = t.find("\n", self.i)
                self.i = len(t) if конец_строки < 0 else конец_строки
            elif c == "'" or t.startswith("$'", self.i):
                части = начать()
                self.i += 1 if c == "'" else 2
                части.append(self._до_кавычки("'", экранирование=c == "$"))
            elif c in '"$`':
                части = начать()
                self.i += c == '"'
                if self._двойные(части) if c == '"' else self._подстановка_или_доллар(части):
                    слово[1] = True
            elif c in "<>":
                if слово and not слово[1] and "".join(слово[0]).isdigit():
                    слово.clear()                         # `2>…`: номер дескриптора — не аргумент
                конец()
                метка, self._цель = self._перенаправление()
                if метка:
                    команда.append((метка, False, self.строка))
            elif c == "|" and t[self.i + 1:self.i + 2] != "|":
                конец(True)
                self._труба_от = len(self.команды) - 1 if self.команды else None
                self.i += 1
            elif c in ";&|(" or (c in "{}" and not слово and t[self.i + 1:self.i + 2] in " \t\n;"):
                конец(True)
                глубина += c == "("
                self.i += 1 + (t[self.i:self.i + 2] == "||")
            elif c == ")":
                конец(True)
                self.i += 1
                if до_скобки and глубина == 0:
                    return
                глубина -= 1
            else:
                начать().append(c)
                self.i += 1
        конец(True)

    def _перенаправление(self) -> tuple:
        """Курсор на `<`/`>`. -> (слово-метка heredoc/here-string или пусто, ждать ли имя файла)."""
        t = self.t
        if t.startswith("<<<", self.i):
            self.i += 3
            return "<<<", False
        if t.startswith("<<", self.i):
            return self._heredoc_заголовок(), False
        m = re.compile(r"[<>]+(?:&[0-9-]*)?\|?").match(t, self.i)
        self.i = m.end() if m else self.i + 1
        return "", not (m and "&" in m.group(0))           # `2>&1` цели не ждёт

    def _до_кавычки(self, кавычка: str, экранирование: bool) -> str:
        t, начало = self.t, self.i
        while self.i < len(t) and t[self.i] != кавычка:
            self.i += 2 if экранирование and t[self.i] == "\\" else 1
        текст = t[начало:self.i]
        self.строка += текст.count("\n")
        self.i += 1
        return текст

    def _подстановка_или_доллар(self, части: list) -> bool:
        if self._подстановка():
            return True
        части.append("$")
        return False

    def _двойные(self, части: list) -> bool:
        t, подст = self.t, False
        while self.i < len(t):
            c = t[self.i]
            if c == '"':
                self.i += 1
                return подст
            if c == "\\" and t[self.i + 1:self.i + 2] in ('$', '`', '"', "\\", "\n"):
                части.append(t[self.i + 1])
                self.строка += t[self.i + 1] == "\n"
                self.i += 2
            elif c in "$`":
                подст = self._подстановка_или_доллар(части) or подст
            else:
                self.строка += c == "\n"
                части.append(c)
                self.i += 1
        return подст

    def _подстановка(self) -> bool:
        """Курсор на `$` или обратной кавычке. -> была ли подстановка (курсор — за ней)."""
        t = self.t
        if t[self.i] == "`":
            self.i += 1
            внутри = _Оболочка(self._до_кавычки("`", экранирование=True))
            внутри.строка = self.строка - внутри.t.count("\n")
            внутри.разобрать()
            self.команды += внутри.команды
            return True
        следующий = t[self.i + 1:self.i + 2]
        if следующий == "(":
            self.i += 2
            self.разобрать(до_скобки=True)                # команда внутри `$(…)` — тоже команда
            return True
        if следующий == "{":
            глубина = 0
            while self.i < len(t):
                глубина += {"{": 1, "}": -1}.get(t[self.i], 0)
                self.строка += t[self.i] == "\n"
                self.i += 1
                if глубина == 0 and t[self.i - 1] == "}":
                    break
            return True
        m = _SH_ИМЯ.match(t, self.i + 1)
        self.i = m.end() if m else self.i + 1
        return bool(m)

    def _heredoc_заголовок(self) -> str:
        """Курсор на `<<`. -> метка `<<N` (тело — позже, после конца строки) или пусто."""
        t = self.t
        self.i += 2
        срезать = t[self.i:self.i + 1] == "-"
        self.i += срезать
        while self.i < len(t) and t[self.i] in " \t":
            self.i += 1
        m = _SH_ОГРАНИЧИТЕЛЬ.match(t, self.i)
        сырой = m.group(0) if m else ""
        self.i += len(сырой)
        ограничитель = re.sub(r"[\"'\\]", "", сырой)
        if not re.fullmatch(r"[A-Za-z_]\w*", ограничитель):   # `$((1<<2))` — сдвиг, не heredoc
            return ""
        # Ограничитель В КАВЫЧКАХ (`'EOF'`, `"EOF"`, `\EOF`) гасит подстановку во всём теле.
        закавычен = сырой != ограничитель
        номер = len(self.тела)
        self.тела[номер] = False
        self.heredoc.append((ограничитель, срезать, закавычен, номер))
        return f"<<{номер}"

    def _пропустить_heredoc(self) -> None:
        t = self.t
        for ограничитель, срезать, закавычен, номер in self.heredoc:
            while self.i < len(t):
                конец_строки = t.find("\n", self.i)
                конец_строки = len(t) if конец_строки < 0 else конец_строки
                строка = t[self.i:конец_строки]
                self.i, self.строка = конец_строки + 1, self.строка + 1
                if (строка.lstrip("\t") if срезать else строка) == ограничитель:
                    break
                if not закавычен and _SH_ПОДСТАНОВКА_В_ТЕЛЕ.search(строка):
                    self.тела[номер] = True
        self.heredoc.clear()


# ─── оболочка: разбор команд ─────────────────────────────────────────────────────────────────────
#
# Программа НА ВХОД считается, только когда интерпретатор стоит на месте команды — первым словом,
# после присваиваний или за обёрткой. Иначе `echo "$X" | grep python` читался бы как конвейер в
# интерпретатор: там `python` — аргумент grep, а не команда.
_SH_ОБЁРТКИ = frozenset({"sudo", "doas", "env", "exec", "command", "builtin", "nohup", "time",
                         "nice", "timeout", "ssh", "docker", "podman", "kubectl", "xargs"})
_SH_ПРИСВАИВАНИЕ = re.compile(r"[A-Za-z_]\w*\+?=")
# Код, скачанный из сети и отданный интерпретатору (`curl … | bash`), — тот же класс: текст программы
# приходит снаружи.
_SH_ЗАГРУЗЧИКИ = frozenset({"curl", "wget"})


def _на_месте_команды(команда: list, k: int) -> bool:
    if k == 0:
        return True
    первое = команда[0][0]
    return (not команда[0][1] and (_имя_бинаря(первое) in _SH_ОБЁРТКИ
                                   or bool(_SH_ПРИСВАИВАНИЕ.match(первое))))


def _отделить_вход(слова: list, разбор: _Оболочка) -> tuple:
    """-> (слова без меток перенаправления, программа на входе собрана подстановкой)."""
    out, собрано, k = [], False, 0
    while k < len(слова):
        текст, подст = слова[k][0], слова[k][1]
        метка = None if подст else _SH_МЕТКА_HEREDOC.fullmatch(текст)
        if метка:
            собрано = собрано or разбор.тела.get(int(метка.group(1)), False)
        elif текст == "<<<" and not подст:
            собрано = собрано or (k + 1 < len(слова) and слова[k + 1][1])
            k += 1                                      # слово here-string — данные, не аргумент
        else:
            out.append((текст, подст))
        k += 1
    return out, собрано


def _вход_из_трубы(разбор: _Оболочка, номер: int) -> bool:
    """Какая-то ступень конвейера левее даёт текст с подстановкой или скачанный из сети.

    Ступени проходятся ВСЕ: `echo "$X" | tr a b | bash` — `tr` данные не очищает от кода.
    """
    слева, пройдено = разбор.входы.get(номер), set()
    while слева is not None and слева not in пройдено:
        пройдено.add(слева)
        команда = разбор.команды[слева]
        if (_имя_бинаря(команда[0][0]) in _SH_ЗАГРУЗЧИКИ
                or any(п for _, п, _ in команда) or _отделить_вход(команда, разбор)[1]):
            return True
        слева = разбор.входы.get(слева)
    return False


def _sh_строки(text: str) -> list:
    """Строки скрипта оболочки, где интерпретатору передан код, собранный подстановкой."""
    разбор = _Оболочка(text)
    разбор.разобрать()
    out = set()
    for номер, команда in enumerate(разбор.команды):
        # Интерпретатор с флагом кода ищется в любом месте команды: обёртки (`sudo -u x`,
        # `timeout 5`, `docker exec c`, `xargs`) ставят его не первым словом.
        for k, (текст, подст, строка) in enumerate(команда):
            семейство = None if подст else _инлайн_семейство(текст)
            if not семейство:
                continue
            слова, вход_собран = _отделить_вход(команда[k + 1:], разбор)
            вывод = verdict(слова, семейство)
            if вывод == "подстановка" or (
                    вывод == "stdin" and _на_месте_команды(команда, k)
                    and (вход_собран or _вход_из_трубы(разбор, номер))):
                out.add(строка)
                break
    return sorted(out)


def inline_code_lines(text: str, path: str) -> list:
    """Строки, где интерпретатору передан inline-код, собранный подстановкой (#1161).

    Язык — по суффиксу (`.py`, `.sh`…), у файла без суффикса — по `#!`. Прочие файлы правило не
    читает: JS-форма разбирается запуском команды (`launch_is_a_surface`) в файле с `child_process`.
    """
    низ, первая = path.lower(), text.split("\n", 1)[0]
    без_суффикса = "." not in низ.replace("\\", "/").rsplit("/", 1)[-1]
    if низ.endswith((".py", ".pyw")) or (без_суффикса and первая.startswith("#!")
                                         and "python" in первая):
        return _py_строки(text)
    if низ.endswith(_SH_СУФФИКСЫ) or (без_суффикса and _SH_ШЕБАНГ.match(первая)):
        return _sh_строки(text)
    return []
