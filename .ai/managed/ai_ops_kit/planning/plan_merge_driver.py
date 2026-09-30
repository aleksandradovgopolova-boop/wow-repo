"""Кастомный git merge-driver для planning/plan.yaml.

ЗАЧЕМ. `planning/plan.yaml` — самый правимый файл кита (замер #148: ~25 коммитов/неделю), его
трогает каждый срез работы. Параллельные ветки регулярно дают РУЧНОЙ конфликт слияния, хотя правки
почти всегда НЕПЕРЕСЕКАЮЩИЕСЯ: две работы добавили разные элементы в список `work:` или изменили
разные цели. `merge=union` (как для JSONL-журналов) здесь применять НЕЛЬЗЯ — он склеивает строки и
на структурном YAML даёт синтаксически битый документ. Разбивку по работам (файл-на-работу, PR #290)
владелец откатил. Остаётся третий путь — понимающий структуру merge-driver.

ЧТО ДЕЛАЕТ. Получает три версии файла (base/ours/theirs) и сводит их на уровне ЭЛЕМЕНТОВ списков
`goals:` и `work:`, ключуемых по `id`, плюс сырых (не-списочных) участков документа:
  * разные работы добавили РАЗНЫЕ id / изменили РАЗНЫЕ элементы  → авто-слить обе стороны;
  * обе стороны тронули ОДИН И ТОТ ЖЕ id (или не-списочный участок) по-разному → отдать обычный
    конфликт человеку.

БЕЗОПАСНОСТЬ — ПЕРВЕЕ ВСЕГО (fail-closed). На ЛЮБОЙ неоднозначности драйвер НЕ угадывает, а падает в
конфликт (вызывающий получает `conflict=True` и обычные конфликт-маркеры от `git merge-file`):
  * обе стороны изменили один id по-разному, или удалили/изменили встречно — конфликт;
  * структура не распозналась (нет ожидаемых ключей, дубли id, маркер списка без `id`) — конфликт;
  * КОНТРОЛЬ РЕЗУЛЬТАТА: даже при чистом слиянии итог заново парсится как YAML и проверяется на
    дубли id и совпадение множеств id с ожидаемым — если что-то не так, отдаётся конфликт.
Драйвер НИКОГДА не производит молча неверный/битый YAML: это было бы хуже конфликта.

Слияние работает на ТЕКСТЕ блоков (не пере-сериализует YAML), поэтому комментарии и форматирование
сохраняются дословно; порядок элементов берётся из нашей (ours) стороны, новые чужие (theirs)
элементы дописываются в конец их относительного порядка.

Функция `merge_plan_yaml` детерминирована и идемпотентна и проверяется тестами напрямую, без git.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass

# Маркер элемента списка верхнего уровня: два пробела, дефис, пробел.
_ITEM_MARKER = re.compile(r"^  - ")
# Ключ id внутри элемента: `  - id: <slug>`.
_ITEM_ID = re.compile(r"^  - id:\s*(.+?)\s*$")


@dataclass
class _Parsed:
    head: str          # всё до строки `goals:` включительно
    goals_pre: str     # комментарии/пустые строки перед первым элементом goals
    goals_items: list  # [(id, block_text), ...]
    mid: str           # строка `work:` (и всё между списком goals и ней)
    work_pre: str
    work_items: list
    tail: str          # хвост после списка work (обычно пусто)


def _find_key(lines, name):
    pat = re.compile(r"^" + re.escape(name) + r":\s*$")
    for i, line in enumerate(lines):
        if pat.match(line):
            return i
    return None


def _split_items(body_lines):
    """body_lines -> (preamble_text, [(id, block_text), ...]) либо None при нераспознанном элементе.

    Комментарии/пустые строки НЕПОСРЕДСТВЕННО перед `- id:` прикрепляются к СЛЕДУЮЩЕМУ элементу.
    """
    markers = [i for i, line in enumerate(body_lines) if _ITEM_MARKER.match(line)]
    if not markers:
        return "".join(body_lines), []
    leadstarts = []
    for j, m in enumerate(markers):
        lo = markers[j - 1] + 1 if j > 0 else 0
        k = m
        while k - 1 >= lo and (body_lines[k - 1].strip() == ""
                               or body_lines[k - 1].lstrip().startswith("#")):
            k -= 1
        leadstarts.append(k)
    preamble = "".join(body_lines[: leadstarts[0]])
    items = []
    for j, m in enumerate(markers):
        start = leadstarts[j]
        end = leadstarts[j + 1] if j + 1 < len(markers) else len(body_lines)
        match = _ITEM_ID.match(body_lines[m])
        if not match:
            return None  # маркер списка без `id` — не ключуется, fail-closed
        items.append((match.group(1), "".join(body_lines[start:end])))
    return preamble, items


def _parse(text):
    """Разобрать plan.yaml в _Parsed либо вернуть None (структура не распознана → конфликт)."""
    lines = text.splitlines(keepends=True)
    g = _find_key(lines, "goals")
    w = _find_key(lines, "work")
    if g is None or w is None or not (g < w):
        return None
    head = "".join(lines[: g + 1])
    goals_body = lines[g + 1 : w]
    mid = "".join(lines[w : w + 1])
    work_body = lines[w + 1 :]
    gs = _split_items(goals_body)
    ws = _split_items(work_body)
    if gs is None or ws is None:
        return None
    return _Parsed(head, gs[0], gs[1], mid, ws[0], ws[1], "")


def _merge_raw(base, ours, theirs):
    """3-way слияние сырого текстового участка. -> (text, conflict)."""
    if ours == theirs:
        return ours, False
    if ours == base:
        return theirs, False
    if theirs == base:
        return ours, False
    return None, True


def _merge_items(b_items, o_items, t_items):
    """3-way слияние упорядоченного списка элементов, ключуемых по id.

    -> (merged_text, conflict, kept_ids). При любой неоднозначности conflict=True.
    """
    for items in (b_items, o_items, t_items):
        ids = [i for i, _ in items]
        if len(ids) != len(set(ids)):
            return None, True, None  # дубли id внутри одной стороны — не ключуется
    bmap, omap, tmap = dict(b_items), dict(o_items), dict(t_items)
    resolved = {}  # id -> block_text | None(удалить)
    for iid in set(bmap) | set(omap) | set(tmap):
        b, o, t = bmap.get(iid), omap.get(iid), tmap.get(iid)
        if iid in bmap:
            if o is None and t is None:
                resolved[iid] = None
            elif o is None:                     # удалён у нас
                if t == b:
                    resolved[iid] = None
                else:
                    return None, True, None      # удалён у нас, изменён у них
            elif t is None:                     # удалён у них
                if o == b:
                    resolved[iid] = None
                else:
                    return None, True, None      # удалён у них, изменён у нас
            elif o == t or t == b:
                resolved[iid] = o                # совпали / изменили только мы
            elif o == b:
                resolved[iid] = t                # изменили только они
            else:
                return None, True, None          # изменён обеими по-разному
        else:                                   # добавлен
            if o is not None and t is not None:
                if o == t:
                    resolved[iid] = o
                else:
                    return None, True, None      # обе добавили один id по-разному
            else:
                resolved[iid] = o if o is not None else t
    order, seen = [], set()
    for iid, _ in o_items:
        if resolved.get(iid) is not None:
            order.append(iid)
            seen.add(iid)
    for iid, _ in t_items:
        if iid not in seen and resolved.get(iid) is not None:
            order.append(iid)
            seen.add(iid)
    return "".join(resolved[i] for i in order), False, set(order)


def _validate(text, expected_goal_ids, expected_work_ids):
    """Контроль результата: валидный YAML, нет дублей id, множества id совпадают с ожидаемыми."""
    try:
        import yaml
    except ImportError:
        return False  # без парсера не можем гарантировать корректность → fail-closed
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    if not isinstance(data, dict):
        return False
    for key, expected in (("goals", expected_goal_ids), ("work", expected_work_ids)):
        seq = data.get(key)
        if not isinstance(seq, list):
            return False
        ids = []
        for item in seq:
            if not isinstance(item, dict) or "id" not in item:
                return False
            ids.append(item["id"])
        if len(ids) != len(set(ids)):
            return False           # дубль id проскочил в итог
        if set(ids) != expected:
            return False           # состав элементов не тот, что мы намеревались собрать
    return True


def merge_plan_yaml(base, ours, theirs):
    """Свести три версии plan.yaml. -> (merged_text, conflict).

    conflict=True означает: авто-слияние невозможно/небезопасно, merged_text не использовать.
    """
    if ours == theirs:
        return ours, False
    if ours == base:
        return theirs, False
    if theirs == base:
        return ours, False
    pb, po, pt = _parse(base), _parse(ours), _parse(theirs)
    if pb is None or po is None or pt is None:
        return None, True
    head, c1 = _merge_raw(pb.head, po.head, pt.head)
    gpre, c2 = _merge_raw(pb.goals_pre, po.goals_pre, pt.goals_pre)
    gitems, c3, gids = _merge_items(pb.goals_items, po.goals_items, pt.goals_items)
    mid, c4 = _merge_raw(pb.mid, po.mid, pt.mid)
    wpre, c5 = _merge_raw(pb.work_pre, po.work_pre, pt.work_pre)
    witems, c6, wids = _merge_items(pb.work_items, po.work_items, pt.work_items)
    tail, c7 = _merge_raw(pb.tail, po.tail, pt.tail)
    if any((c1, c2, c3, c4, c5, c6, c7)):
        return None, True
    merged = head + gpre + gitems + mid + wpre + witems + tail
    if not _validate(merged, gids, wids):
        return None, True
    return merged, False


def _run_driver(ancestor, current, other):
    """Git merge-driver: argv = %O %A %B. Пишет результат в `current`, возвращает код возврата."""
    with open(ancestor, encoding="utf-8") as f:
        base = f.read()
    with open(current, encoding="utf-8") as f:
        ours = f.read()
    with open(other, encoding="utf-8") as f:
        theirs = f.read()
    merged, conflict = merge_plan_yaml(base, ours, theirs)
    if not conflict:
        with open(current, "w", encoding="utf-8") as f:
            f.write(merged)
        return 0
    # Fail-closed: отдать человеку ОБЫЧНЫЙ конфликт с маркерами (git merge-file правит `current`).
    subprocess.run(
        ["git", "merge-file", "-L", "ours", "-L", "base", "-L", "theirs",
         current, ancestor, other],
        check=False, timeout=60,
    )
    return 1


_DRIVER_NAME = "ai-ops-plan"
_PLAN_PATH = "planning/plan.yaml"
_ATTR_RULE = f"{_PLAN_PATH} merge={_DRIVER_NAME}"


def install_for_repo(root=".", script_rel="ai_ops_kit/planning/plan_merge_driver.py"):
    """Зарегистрировать драйвер В САМОМ КИТЕ (или любом репозитории кита) для ЭТИХ сессий.

    Ставит всё ЛОКАЛЬНО и НЕ трогает отслеживаемые файлы:
      * правило `planning/plan.yaml merge=ai-ops-plan` пишется в `.git/info/attributes` (локальный,
        некоммитируемый файл атрибутов — его git читает наравне с `.gitattributes`);
      * `merge.ai-ops-plan.driver` пишется в git config, указывая на скрипт В репозитории.
    Так worktree'ы одной сессии и параллельные ветки кита получают структурное слияние плана без
    коммита в историю. Идемпотентно. -> 0 при успехе, !=0 если git недоступен.
    """
    import os

    def _git(*args):
        return subprocess.run(["git", "-C", str(root), *args],
                              check=True, capture_output=True, text=True, timeout=30)
    try:
        attr_path = _git("rev-parse", "--git-path", "info/attributes").stdout.strip()
        attr_abs = os.path.join(str(root), attr_path) if not os.path.isabs(attr_path) else attr_path
        os.makedirs(os.path.dirname(attr_abs), exist_ok=True)
        existing = ""
        if os.path.exists(attr_abs):
            with open(attr_abs, encoding="utf-8") as f:
                existing = f.read()
        if _ATTR_RULE not in existing:
            sep = "" if (existing == "" or existing.endswith("\n")) else "\n"
            with open(attr_abs, "a", encoding="utf-8") as f:
                f.write(sep + _ATTR_RULE + "\n")
        _git("config", f"merge.{_DRIVER_NAME}.name", "AI Ops: структурное слияние planning/plan.yaml")
        _git("config", f"merge.{_DRIVER_NAME}.driver", f"python3 {script_rel} %O %A %B")
    except (subprocess.CalledProcessError, OSError) as exc:
        sys.stderr.write(f"plan_merge_driver --install: git недоступен ({exc})\n")
        return 1
    sys.stderr.write(f"plan_merge_driver: зарегистрирован для {root} (merge={_DRIVER_NAME})\n")
    return 0


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if len(argv) >= 2 and argv[1] == "--install":
        return install_for_repo(argv[2] if len(argv) >= 3 else ".")
    if len(argv) < 4:
        sys.stderr.write("usage: plan_merge_driver.py <base> <current> <other>\n"
                         "       plan_merge_driver.py --install [repo_root]\n")
        return 2
    return _run_driver(argv[1], argv[2], argv[3])


if __name__ == "__main__":
    sys.exit(main())
