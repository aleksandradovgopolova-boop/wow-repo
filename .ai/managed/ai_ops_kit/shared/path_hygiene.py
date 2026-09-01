#!/usr/bin/env python3
"""path_hygiene.py — окружение не подкладывает киту его собственные пути.

`.pth`-файл в site-packages исполняется интерпретатором при КАЖДОМ старте Python. До v3.33.1
`setup.py` кита писал такой файл (`ai_ops_kit.pth`) в site-packages ПОЛЬЗОВАТЕЛЯ на любом
`pip install`, подкладывая корень репозитория, `tools/` и `validation/` в `sys.path` каждого
процесса на машине. 3.33.1 убрал запись — но НЕ убрал пояса, уже написанные: файл лежит вне
репозитория и вне учёта pip, поэтому ни `git pull`, ни обновление пакета его не касаются.

ЗАЧЕМ ПРОВЕРКА. Пояс не «немного упрощает импорты» — он делает зелёными проверки, которые обязаны
краснеть. Замер на машине владельца: `tests/unit/test_validator_bootstrap.py::
test_missing_bootstrap_is_caught` копирует репозиторий во временный каталог, УДАЛЯЕТ там
`validation/_bootstrap.py` и ждёт `ModuleNotFoundError`. С поясом импорт находит `_bootstrap`
в НАСТОЯЩЕМ репозитории — fail-closed-проверка перестаёт иметь зубы. Тест чистил `PYTHONPATH`,
но пояс переменной среды не задать и не снять: он исполняется до первой строки кода.
Отличить одно от другого можно только запуском без user-site (`python3 -s`).

ЧТО ИМЕННО ЛОВИМ (severity — разная, потому что разное происхождение и разное лечение):

  path_belt (blocking)              пояс кита. Писал его сам кит, пользователь об этом не просил,
                                    и `pip uninstall` его не заберёт: файл не значится в RECORD
                                    установки. Лечится только удалением файла.
  editable_in_user_site (advisory)  кит установлен editable в ПОЛЬЗОВАТЕЛЬСКИЙ site: рабочее дерево
                                    попадает в `sys.path` каждого процесса этого интерпретатора —
                                    тот же класс маскировки. Но это осознанное действие
                                    разработчика, и pip его снимает. Поэтому сообщаем, не блокируем.

Editable-установка в venv НЕ находка: она ограничена своим окружением — ровно то, чем venv и
занимается. Находка — только пользовательский site, у которого нет границ.

ПОЯС ОТ УСТАНОВКИ ОТЛИЧАЕТСЯ ПО ХОЗЯИНУ, А НЕ ПО ФОРМЕ. Содержимое их не различает: editable в
режиме `editable_mode=compat` пишет обычную строку пути, ровно как пояс. Разница в том, кто создал
файл: у пояса хозяина нет вовсе (`setup.py` писал его side effect'ом сборки), а файл, значащийся в
RECORD дистрибутива или названный по схеме setuptools (`__editable__.*`, `easy-install.pth`),
принадлежит установке — его снимает pip, и удалять его нельзя: установка сломается за спиной pip,
а из общего `easy-install.pth` уехали бы вместе с китом и чужие пакеты. Поэтому `is_pip_owned`
проверяется ДО опознания пояса, и второй раз — прямо перед удалением.

ГРАНИЦА ПРОВЕРКИ ОБЪЯВЛЕНА, А НЕ ЗАМОЛЧАНА. Видны: site-каталоги текущего интерпретатора и
пользовательские site-каталоги ДРУГИХ версий Python в домашнем каталоге (иначе проверка ослепла бы
ровно на том случае, который и произошёл: пояс лежит в user-site 3.9, а `doctor` запущен из venv
3.12). Не видны: venv'ы в произвольных местах машины — у них нет ни реестра, ни предсказуемого пути.
`scan_limits` в отчёте говорит это прямо.

Модуль ничего не удаляет сам: `remove_belts()` вызывается только явной командой
`ai-ops doctor --remove-path-belt`. Пакет, молча пишущий файлы за пределами своего окружения, —
это и был исходный дефект; молча их удаляющий — тот же дефект с обратным знаком.

CLI:  path_hygiene.py [--json]
      path_hygiene.py --remove          удалить найденные пояса (печатает, что удалил)
"""
from __future__ import annotations

import json
import os
import re
import site
import sys
from pathlib import Path

# Имя, которое писал `setup.py` кита до v3.33.1, и никто больше. Само по себе — достаточная
# идентификация: файл с таким именем в site-packages создавал только кит.
BELT_FILENAME = "ai_ops_kit.pth"

# `__editable__.ai_ops_kit-3.27.7.pth` / `__editable__.ai-ops-kit-*.pth` — след `pip install -e`.
_EDITABLE_RE = re.compile(r"^__editable__[._-]ai[_-]ops[_-]kit[._-]", re.I)

# ОБЩИЕ `.pth`, которые нельзя удалять НИ ПРИ КАКИХ УСЛОВИЯХ: в них пишут все editable-установки
# окружения сразу (старый стиль setuptools — по строке на проект). Удаление такого файла из-за кита
# сломало бы чужие пакеты. Если кит там упомянут — это запись editable-установки, и снимает её pip.
_SHARED_PTH = frozenset({"easy-install.pth"})


def _pip_owned_pth(site_dir: Path) -> frozenset:
    """Имена `.pth` в этом site-каталоге, за которые отвечает pip — из RECORD дистрибутивов.

    Владение решается по RECORD, а не по содержимому: разложить editable-установку setuptools может
    и через finder-импорт, и ОБЫЧНОЙ строкой пути (`editable_mode=compat`) — второй вариант
    текстуально неотличим от пояса. Разница не в форме файла, а в том, кто его создал: у пояса
    хозяина нет вовсе (setup.py писал его side effect'ом), а файл из RECORD снимает `pip uninstall`.
    Удалить такой файл значило бы сломать установку за спиной pip, который о правке не узнает.
    """
    owned = set()
    try:
        records = sorted(site_dir.glob("*.dist-info/RECORD"))
    except OSError:
        return frozenset()
    for record in records:
        try:
            lines = record.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            name = line.split(",", 1)[0].strip()
            if name.endswith(".pth"):
                owned.add(os.path.basename(name))
    return frozenset(owned)

# Родовые каталоги кита, которые пояс подкладывает как top-level имена.
_KIT_DIRS = ("tools", "validation")

# Из пути пользовательского site достаём версию Python — чтобы подсказка называла ТОТ интерпретатор,
# которому файл принадлежит, а не тот, которым запущен doctor.
_PYVER_RE = re.compile(r"(?:Python|python)[/\\]?(3\.\d+)")


def _norm(p) -> str:
    return os.path.normpath(str(p))


def _user_site_dirs():
    """Пользовательские site-каталоги: текущего интерпретатора + других версий Python рядом.

    Второе — не запас прочности, а условие работоспособности: пояс живёт в user-site той версии,
    которой делали `pip install`, а `doctor` может быть запущен любой другой.
    """
    found = []
    try:
        current = site.getusersitepackages()
    except Exception:  # noqa: BLE001 — отсутствие user-site (например -s) не ломает проверку
        current = None
    if isinstance(current, str):
        found.append(current)
    elif isinstance(current, (list, tuple)):
        found.extend(str(x) for x in current)

    home = Path.home()
    patterns = (
        "Library/Python/*/lib/python/site-packages",   # macOS framework-сборки
        ".local/lib/python*/site-packages",            # Linux и macOS из python.org/brew
    )
    for pattern in patterns:
        found.extend(str(d) for d in sorted(home.glob(pattern)) if d.is_dir())
    appdata = os.environ.get("APPDATA")
    if appdata:
        found.extend(str(d) for d in sorted(Path(appdata).glob("Python/Python*/site-packages"))
                     if d.is_dir())

    seen, out = set(), []
    for d in found:
        n = _norm(d)
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _env_site_dirs():
    """site-каталоги ТЕКУЩЕГО окружения (venv или система). Не user-site."""
    found = []
    getter = getattr(site, "getsitepackages", None)   # отсутствует в некоторых venv старого virtualenv
    if getter is not None:
        try:
            found.extend(str(x) for x in getter())
        # Причина подавления (срез ратчета, 2026-08-12): `getsitepackages()` отсутствует или
        # падает в нестандартных venv — ровно для этого ниже стоит добор из `sys.path`, который
        # покрывает тот же материал другим путём. Пропуск здесь не теряет данных: он их не
        # получает раньше срока.
        except Exception:  # noqa: BLE001,S110 — есть равноценный путь ниже (добор из sys.path)
            pass
    # Добор из sys.path: покрывает нестандартные схемы раскладки, где getsitepackages() молчит.
    for entry in sys.path:
        if entry and os.path.basename(_norm(entry)) in ("site-packages", "dist-packages"):
            found.append(entry)

    seen, out = set(), []
    for d in found:
        n = _norm(d)
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _looks_like_kit_tree(d: Path) -> bool:
    """Рабочее дерево кита — по маркерам, а не по имени каталога (его пользователь вправе назвать
    как угодно). Те же маркеры использует `_bootstrap` для поиска корня."""
    try:
        return (d / "VERSION").is_file() and (d / "ai_ops_kit").is_dir()
    except OSError:
        return False


def _injected_kit_paths(text: str, site_dir: Path):
    """Пути кита, которые файл подкладывает в sys.path. Разбор текстовый: пояс может ссылаться на
    каталог, которого уже нет (установка из sdist оставляла путь во временный каталог).

    `site_dir` обязателен: по семантике `.pth` относительная запись отсчитывается от site-каталога,
    а НЕ от cwd процесса. Без этого чужой `.pth` со строкой `.` объявлялся бы поясом кита всякий
    раз, когда проверку запускают из корня кита, — а настоящий пояс с относительной записью
    не находился бы вовсе.
    """
    hits = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("import ") or line.startswith("import\t"):
            # Исполняемая строка. Опасна, если правит sys.path и называет родовые каталоги кита.
            if "sys.path" not in line:
                continue
            quoted = re.findall(r"""['"]([^'"]+)['"]""", line)
            roots = [q for q in quoted if os.path.isabs(q)]
            names = [q for q in quoted if q in _KIT_DIRS]
            if not names:
                continue
            # Пояс подкладывает корень И названные подкаталоги (`os.path.join(root, d)`).
            # Разворачиваем ровно это, чтобы отчёт показывал все три пути, а не только корень.
            for root in roots:
                hits.append(root)
                hits.extend(os.path.join(root, n) for n in names)
            continue
        # Обычная строка .pth — это готовая запись sys.path, относительная к site-каталогу.
        p = Path(line) if os.path.isabs(line) else site_dir / line
        if _looks_like_kit_tree(p) or (p.name in _KIT_DIRS and _looks_like_kit_tree(p.parent)):
            hits.append(_norm(p))
    seen, out = set(), []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def _same_dir(a, b) -> bool:
    try:
        return os.path.realpath(str(a)) == os.path.realpath(str(b))
    except OSError:
        return _norm(a) == _norm(b)


def pip_command(site_dir) -> str:
    """Команда, которой снимается установка ИМЕННО из этого site-каталога.

    Версию интерпретатора видно из пути (`…/Python/3.9/…`), но БИНАРНИКА с таким именем на машине
    может не быть: на macOS пользовательский site 3.9 обслуживает `python3`, а `python3.9` в PATH
    отсутствует — и напечатанная команда не запускается. Подсказка, которую нельзя выполнить, хуже
    отсутствующей: пользователь верит, что сделал, а ничего не произошло.

    Поэтому: если это пользовательский site ТЕКУЩЕГО интерпретатора — называем его буквально
    (`sys.executable` заведомо существует). Иначе версию называем, но говорим прямо, что имя
    бинарника надо подставить своё: угадать его нельзя, а молча выдать `python3` значило бы
    предложить снять установку У ДРУГОГО интерпретатора.
    """
    tail = " -m pip uninstall -y ai-ops-kit"
    try:
        current = site.getusersitepackages()
    except Exception:  # noqa: BLE001 — отсутствие user-site не мешает дать подсказку по версии
        current = None
    if current and _same_dir(current, site_dir):
        exe = str(sys.executable)
        return (f'"{exe}"' if " " in exe else exe) + tail
    m = _PYVER_RE.search(str(site_dir))
    if m:
        return (f"python{m.group(1)}{tail}   # интерпретатор {m.group(1)}; бинарник на этой машине "
                f"может называться иначе — подставьте свой")
    return f"<интерпретатор, которому принадлежит {site_dir}>{tail}"


def is_pip_owned(pth: Path, pip_owned=frozenset()) -> bool:
    """Файл принадлежит установке, а не поясу: его снимает pip, и удалять его нельзя.

    Три независимых признака — форма имени setuptools, общий файл окружения и запись в RECORD:
    любой из них достаточен. Проверка ДО опознания пояса, а не после: editable-установка в режиме
    `editable_mode=compat` пишет обычную строку пути, текстуально неотличимую от пояса.
    """
    return (pth.name in _SHARED_PTH
            or bool(_EDITABLE_RE.match(pth.name))
            or pth.name in pip_owned)


def classify(pth: Path, in_user_site: bool, pip_owned=frozenset()):
    """Находка по одному .pth или None. Отдельная функция — чтобы тест мог подать файл напрямую."""
    try:
        text = pth.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"rule": "unreadable", "severity": "advisory", "path": str(pth),
                "detail": f"{pth} не читается ({e}) — что он подкладывает, неизвестно",
                "fix": f'посмотрите файл вручную: cat "{pth}"'}

    paths = _injected_kit_paths(text, pth.parent)

    if is_pip_owned(pth, pip_owned):
        # Хозяин у файла есть — pip. Значит это не пояс, а запись установки, и лечение только через
        # pip: `rm` сломал бы установку за его спиной (а у общего easy-install.pth — ещё и чужие
        # пакеты вместе с китом). В venv это вообще не находка: установка ограничена окружением,
        # ровно то, ради чего venv существует. Находка — только пользовательский site, у которого
        # границ нет: рабочее дерево кита попадает в sys.path каждого процесса интерпретатора.
        if not paths and not _EDITABLE_RE.match(pth.name):
            return None
        if not in_user_site:
            return None
        shared = pth.name in _SHARED_PTH
        return {
            "rule": "editable_in_user_site", "severity": "advisory", "path": str(pth),
            "in_user_site": True, "injected_paths": paths,
            "detail": ("кит установлен editable в site ПОЛЬЗОВАТЕЛЯ"
                       + (f" (запись в общем {pth.name})" if shared else "")
                       + ": рабочее дерево попадает в sys.path каждого процесса этого "
                         "интерпретатора — тот же класс маскировки, что и пояс. В venv это было бы "
                         "нормой, здесь границ нет"
                       + (". Файл ОБЩИЙ для всех editable-установок окружения: удалять нельзя"
                          if shared else "")),
            "fix": pip_command(pth.parent)
                   + (f" (строку из {pth.name} убирает pip, не rm)" if shared else ""),
        }

    if pth.name == BELT_FILENAME or paths:
        where = (f"подкладывает в sys.path: {', '.join(paths)}" if paths
                 else "имя файла писал только setup.py кита до v3.33.1")
        return {
            "rule": "path_belt", "severity": "blocking", "path": str(pth),
            "injected_paths": paths, "in_user_site": in_user_site,
            "detail": ("остаточный пояс кита: исполняется при каждом старте Python"
                       + (" (site пользователя — значит в КАЖДОМ процессе на машине)"
                          if in_user_site else "") + f"; {where}"),
            "fix": (f'rm -f "{pth}"   # pip uninstall его НЕ заберёт: файл не значится '
                    f"в RECORD установки"),
        }
    return None


def assess(user_site_dirs=None, env_site_dirs=None):
    """Read-only снимок: подкладывает ли окружение пути кита. Ничего не меняет.

    Каталоги можно передать явно — так тест проверяет РАЗБОР, не машину, на которой запущен."""
    user_dirs = [_norm(d) for d in (user_site_dirs if user_site_dirs is not None
                                    else _user_site_dirs())]
    env_dirs = [_norm(d) for d in (env_site_dirs if env_site_dirs is not None
                                   else _env_site_dirs())]
    user_set = set(user_dirs)
    scanned, findings, seen_real = [], [], set()
    for d in user_dirs + [e for e in env_dirs if e not in user_set]:
        path = Path(d)
        if not path.is_dir():
            continue
        # Дедупликация по РЕАЛЬНОМУ пути: один и тот же каталог виден и через симлинк (на macOS
        # /tmp -> /private/tmp, симлинк ~/.local, venv по ссылке). Иначе файл попадал в findings
        # дважды, второе удаление падало с ENOENT — и doctor печатал «пояс НЕ удалён» про файл,
        # которого он же только что не стало.
        try:
            real = os.path.realpath(d)
        except OSError:
            real = _norm(d)
        if real in seen_real:
            continue
        seen_real.add(real)
        scanned.append(d)
        pip_owned = _pip_owned_pth(path)
        for pth in sorted(path.glob("*.pth")):
            found = classify(pth, in_user_site=d in user_set, pip_owned=pip_owned)
            if found is not None:
                findings.append(found)

    blocking = [f for f in findings if f["severity"] == "blocking"]
    # «Ни одного каталога не просмотрено» — это НЕ «чисто». Отдельный статус: проверка, выдающая
    # «не знаю» за «в порядке», хуже отсутствующей (окружения старого virtualenv, где
    # getusersitepackages() бросает, getsitepackages() нет, а в sys.path нет site-packages).
    if not scanned:
        status = "unknown"
    elif blocking:
        status = "belt_found"
    else:
        status = "advisory" if findings else "clean"
    return {
        "kind": "PathHygiene",
        "status": status,
        "scanned_dirs": scanned,
        "user_site_dirs": [d for d in user_dirs if Path(d).is_dir()],
        "findings": findings,
        "counts": {"scanned_dirs": len(scanned), "findings": len(findings),
                   "blocking": len(blocking)},
        # Честная граница: «clean» означает «в просмотренных каталогах чисто», не «на машине чисто».
        "scan_limits": ("просмотрены site-каталоги текущего интерпретатора и пользовательские "
                        "site-каталоги других версий Python в домашнем каталоге; venv'ы в "
                        "произвольных местах машины этой проверке не видны"),
    }


def remove_belts(report=None, dry_run=False):
    """Удалить найденные пояса. Только rule=path_belt: editable-установку снимает pip, не мы.

    Вызывается ЯВНОЙ командой пользователя. Возвращает список {path, removed, error}."""
    rep = report if report is not None else assess()
    results = []
    for f in rep["findings"]:
        if f["rule"] != "path_belt":
            continue
        p = Path(f["path"])
        # Второй замок на разрушительном исходе. Классификация файл с хозяином сюда не пускает, но
        # цена ошибки — сломанная чужая установка, а стоимость проверки — одна строка.
        if is_pip_owned(p, _pip_owned_pth(p.parent)):
            results.append({"path": str(p), "removed": False,
                            "error": "файл принадлежит установке (RECORD/editable) — снимает pip"})
            continue
        if dry_run:
            results.append({"path": str(p), "removed": False, "error": None, "dry_run": True})
            continue
        try:
            p.unlink()
            results.append({"path": str(p), "removed": True, "error": None})
        except FileNotFoundError:
            # Файла уже нет — цель достигнута, а не отказ.
            results.append({"path": str(p), "removed": True, "error": None})
        except OSError as e:
            results.append({"path": str(p), "removed": False, "error": str(e)})
    return results


def summary_line(report=None):
    """Строка для doctor. Про ОКРУЖЕНИЕ, не про репозиторий — поэтому без параметра root.

    Готовый отчёт можно передать: doctor уже сделал `assess()`, чтобы решить про блокировку, —
    второй скан дал бы те же цифры за ту же цену."""
    try:
        rep = report if report is not None else assess()
    except Exception as e:  # noqa: BLE001 — гигиена путей не роняет doctor
        return f"пути окружения: недоступно ({e})"
    c = rep["counts"]
    if rep["status"] == "unknown":
        return ("пути окружения: НЕ ПРОВЕРЕНО — ни один site-каталог не просмотрен "
                "(нестандартное окружение); это не «чисто»")
    if rep["status"] == "clean":
        return (f"пути окружения: ✓ пояса нет (просмотрено site-каталогов: {c['scanned_dirs']}; "
                f"venv'ы в произвольных местах не видны)")
    lines = []
    for f in rep["findings"]:
        mark = "✗" if f["severity"] == "blocking" else "⚠"
        lines.append(f"  {mark} {f['rule']}: {f['path']}")
        lines.append(f"      {f['detail']}")
        lines.append(f"      удалить: {f['fix']}")
    head = (f"пути окружения: ✗ остаточный пояс кита ({c['blocking']}) — проверки могут быть "
            f"зелёными на сломанном коде" if c["blocking"]
            else f"пути окружения: ⚠ замечания ({c['findings']})")
    if c["blocking"]:
        lines.append("      после удаления перезапустите проверки: пояс исполняется при старте "
                     "Python, снять его переменной среды нельзя")
    return "\n".join([head] + lines)


def _fmt(rep):
    lines = [f"гигиена путей: {rep['status']}",
             f"просмотрено каталогов: {rep['counts']['scanned_dirs']}"]
    for d in rep["scanned_dirs"]:
        lines.append(f"  · {d}")
    for f in rep["findings"]:
        mark = "✗" if f["severity"] == "blocking" else "⚠"
        lines.append(f"{mark} {f['rule']}: {f['path']}")
        lines.append(f"    {f['detail']}")
        lines.append(f"    удалить: {f['fix']}")
    if rep["status"] == "unknown":
        lines.append("НЕ ПРОВЕРЕНО: ни один site-каталог не просмотрен — это не «чисто»")
    elif not rep["findings"]:
        lines.append("✓ ни один .pth не подкладывает пути кита")
    lines.append(f"граница проверки: {rep['scan_limits']}")
    return "\n".join(lines)


def main(argv):
    if "--help" in argv or "-h" in argv:
        # У этой команды есть флаг, который УДАЛЯЕТ файлы. Справка, вместо которой выполняется скан
        # машины, — плохая справка: пользователь просил объяснить, а не сделать.
        print(__doc__.strip())
        return 0
    rep = assess()
    if "--remove" in argv:
        results = remove_belts(rep)
        if not results:
            print("удалять нечего: пояса не найдены")
            return 0
        for r in results:
            print(f"{'удалён' if r['removed'] else 'НЕ удалён'}: {r['path']}"
                  + (f" ({r['error']})" if r["error"] else ""))
        return 0 if all(r["removed"] for r in results) else 1
    print(json.dumps(rep, ensure_ascii=False, indent=2) if "--json" in argv else _fmt(rep))
    return 1 if rep["counts"]["blocking"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
