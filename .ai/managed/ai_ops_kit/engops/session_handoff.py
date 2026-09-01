#!/usr/bin/env python3
"""session_handoff.py — SESSION COMPLETE: состояние, с которым может работать СЛЕДУЮЩАЯ сессия.

ПОВОД — ЗАМЕР НА ЖИВОЙ СЕССИИ (17.08.2026, сессия кита `88c802ae`). `ai-ops session` напечатал:

    Контекст сессии: 414k [measured], ходов: 318
    Расход сессии всего: 80.5M из 20.0M [over_budget]
    Что сохранено: result_achieved, state_saved, handoff_created, decisions_recorded, …
    Рекомендация: NEW_SESSION — … Handoff/решения сохранены в репозитории.

Измерение верное: и контекст, и расход прочитаны из транскрипта. НЕВЕРНО последнее — «handoff
сохранён». Никакого сессионного handoff в ките не существовало: `handoff_created` приходил
параметром `handoff_saved=True` по умолчанию, ни один вызывающий его не передавал, и пункт
означал ровно «автор функции написал True». Это тот класс, который кит ловит у других
(«объявлено — не исполняется»), в его собственном ритуале завершения.

ЧЕМ ЭТО ОТЛИЧАЕТСЯ ОТ `RunHandoff` (`engine/run_handoff.py`) — ВОПРОС ГРАНИЦЫ, А НЕ ИМЕНИ.
`RunHandoff` уже несёт почти те же разделы и уже работает: он про ОДИН прогон движка по ОДНОМУ
WorkItem, лежит в `features/<wid>/run-handoff.yaml` и читается `resume`. Заводить второй объект с
той же ролью было бы дублем сущности, поэтому словарь разделов здесь ВЗЯТ У НЕГО дословно
(`completed`/`decisions`/`changed_files`/`verification`/`open_questions`/`known_risks`/`next_action`).
Разное — предмет:
  * `RunHandoff`  — прогон: «на чём остановился движок по этой работе, откуда продолжать»;
  * `SessionHandoff` — СЕССИЯ: «что произошло в этом рабочем контексте целиком», включая работу,
    которую движок не делал (правки руками, разбор, решения), и включая НЕСКОЛЬКО WorkItem'ов.
Замер, из-за которого одного `RunHandoff` мало: в сессии `88c802ae` 318 ходов, движок не звался ни
разу, `features/` пуст — то есть весь труд сессии не имел носителя вообще.

ЧЕГО ЗДЕСЬ НЕТ ОСОЗНАННО: `goal` НЕ выводится из кода. Цель сессии знает тот, кто её ставил;
вывести её из диффа значило бы пересказать сделанное и назвать это замыслом. Не передали — раздел
честно пуст со словами «не названа», а не заполнен догадкой.

ГДЕ ЛЕЖИТ: `.ai/runtime/sessions/<session_id>/handoff.yaml` — там же, где остальное состояние
рантайма (`process_spend.STATE_FILE`), и по той же причине: это состояние ОДНОЙ машины, а не история
продукта. `.ai/runtime/` в `.gitignore`, поэтому handoff НЕ переезжает на другую машину — предел
объявлен здесь, а не обнаружен потом (решение о коммите — за владельцем, см. `planning/plan.yaml`).
Побочный эффект замерен и нужен: `.ai/` входит в `process_spend._KIT_PREFIXES`, поэтому запись
handoff НЕ читается как «код тронут» и не закрывает процессную фазу чужой работе.

CLI:  session_handoff.py <child_root> [--session SID] [--goal "текст"] [--write] [--json]
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402,F401

# Разделы разбора владельца (Goal / Done / Decisions / Changed / Tests / Open / Next / Risks) в
# ИМЕНАХ КИТА. Слева — то, что просил владелец, справа — поле `RunHandoff`, которое уже это значит.
SECTIONS = ("goal", "completed", "decisions", "changed_files", "verification",
            "open_questions", "next_action", "known_risks")

# Раздел, без которого handoff не является handoff'ом: он ПЕРЕДАЁТ работу, а передать нечего, если
# не назван следующий шаг. Остальные разделы имеют право быть пустыми (в сессии могло не быть
# решений), и пустота отличается от отсутствия — см. `check`.
REQUIRED_NONEMPTY = ("next_action",)

STATE_DIR = Path(".ai") / "runtime" / "sessions"

GOAL_NOT_NAMED = "не названа (сессионный handoff собран без цели от того, кто её ставил)"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git(root, *args):
    """git без падения: нет git/не репозиторий -> None, и это «не знаю», а не «изменений нет»."""
    try:
        r = subprocess.run(["git", "-C", str(root), *args],
                           capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _changed_files(child_root):
    """Что сессия изменила в дереве. -> (список, статус измерения).

    Незакоммиченное считается изменённым: сессия его сделала, и следующей сессии оно важнее
    закоммиченного — именно оно потеряется. Статус называется отдельно, потому что «git не ответил»
    и «изменений нет» — разные факты, и склеивать их в пустой список значило бы соврать.
    """
    out = _git(child_root, "status", "--porcelain")
    if out is None:
        return [], "unavailable"
    files = []
    for line in out.splitlines():
        path = line[3:].strip().strip('"')
        if " -> " in path:                     # переименование: считаем по месту назначения
            path = path.split(" -> ", 1)[1].strip().strip('"')
        if path:
            files.append(path)
    return sorted(files), "measured"


def _branch(child_root):
    out = _git(child_root, "rev-parse", "--abbrev-ref", "HEAD")
    return (out or "").strip() or None


def _head(child_root):
    out = _git(child_root, "rev-parse", "HEAD")
    return (out or "").strip()[:12] or None


def build(child_root, snapshot=None, rec=None, goal=None, decisions=None, open_questions=None,
          verification=None, next_action=None, known_risks=None, completed=None):
    """Собрать SessionHandoff. Детерминированно: одинаковый вход -> одинаковый выход, кроме времени.

    Числа сессии НЕ пересчитываются здесь: их уже измерил `session_telemetry` (единственный
    измеритель расхода сессии в ките). Второй измеритель означал бы две правды об одном числе.
    """
    child_root = Path(child_root)
    snap = snapshot or {}
    files, files_status = _changed_files(child_root)

    # `next_action` — то, ради чего handoff и пишется. Если вызывающий не назвал шаг, берём его из
    # рекомендации по смене сессии: там он уже посчитан и там же лежит точная команда.
    nxt = next_action or (rec or {}).get("command") or (
        "перечитать этот handoff и назвать следующий шаг" if not rec else
        f"исход рекомендации — {(rec or {}).get('outcome')}: {(rec or {}).get('reason') or ''}".strip())

    done = list(completed or [])
    if not done:
        # Пусто не значит «ничего не было»: чаще значит «вызывающий не передал». Говорим ровно это.
        done = [f"изменённых файлов в дереве: {len(files)}"] if files_status == "measured" else [
            "состав работы сессии не передан, а git не ответил — перечислить нечем"]

    return {
        "schema_version": 1, "kind": "SessionHandoff",
        "session_id": snap.get("session_id") or "unlabelled",
        "repository": child_root.resolve().name,
        "branch": _branch(child_root),
        "revision": _head(child_root),
        "created_at": _now(),
        "started_at": snap.get("started_at"),
        # Разделы разбора владельца.
        "goal": goal or GOAL_NOT_NAMED,
        "completed": done,
        "decisions": list(decisions or []),
        "changed_files": files,
        "changed_files_status": files_status,
        "verification": dict(verification or {"passed": [], "failed": []}),
        "open_questions": list(open_questions or []),
        "next_action": nxt,
        "known_risks": list(known_risks or []),
        # Почему сессию пора закрывать — ЧИСЛАМИ, с их статусами. Пересказ без чисел не позволил бы
        # следующей сессии понять, был ли переход обоснован.
        "why_handed_off": {
            "context_current": snap.get("context_current"),
            "context_status": snap.get("context_status") or "unavailable",
            "session_total_tokens": snap.get("session_total_tokens"),
            "session_tokens_status": snap.get("session_tokens_status") or "unavailable",
            "turns": snap.get("turns"),
            "outcome": (rec or {}).get("outcome"),
            "reason": (rec or {}).get("reason"),
        },
        "workitems": list(snap.get("tasks_in_session") or []),
    }


def check(h):
    """Валидация формы и ЧЕСТНОСТИ. Пустой список раздела — норма; отсутствие раздела — нет."""
    e = []
    if not isinstance(h, dict) or h.get("kind") != "SessionHandoff":
        return ["kind должен быть SessionHandoff"]
    for s in SECTIONS:
        if s not in h:
            e.append(f"нет раздела {s}")
    for s in REQUIRED_NONEMPTY:
        if not h.get(s):
            e.append(f"раздел {s} пуст — handoff, который не называет следующий шаг, ничего не передаёт")
    w = h.get("why_handed_off") or {}
    # Тот же инвариант, что в `session_telemetry.check`: unknown НЕ показывается как 0. Handoff
    # переживает сессию, и «расход 0» в нём читался бы следующей сессией как «сессия была дешёвой».
    if w.get("session_tokens_status") == "unavailable" and w.get("session_total_tokens") is not None:
        e.append("расход сессии не измерен, но в handoff записано число (unknown как значение)")
    if w.get("context_status") == "unavailable" and w.get("context_current") is not None:
        e.append("контекст не измерен, но в handoff записано число (unknown как значение)")
    if h.get("changed_files_status") == "unavailable" and h.get("changed_files"):
        e.append("git не ответил, но список изменённых файлов не пуст — откуда он взялся")
    return e


def dir_for(child_root, session_id):
    return Path(child_root) / STATE_DIR / str(session_id or "unlabelled")


def path_for(child_root, session_id):
    return dir_for(child_root, session_id) / "handoff.yaml"


def write(child_root, handoff):
    """Записать handoff. -> путь. Невалидный handoff НЕ пишется: файл, которому нельзя верить,
    хуже отсутствующего — следующая сессия примет его за состояние."""
    errs = check(handoff)
    if errs:
        raise ValueError("SessionHandoff невалиден: " + "; ".join(errs))
    import yaml
    p = path_for(child_root, handoff.get("session_id"))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(handoff, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


def latest(child_root, session_id=None):
    """Есть ли записанный handoff. -> путь или None.

    ЭТО ЕДИНСТВЕННЫЙ ИСТОЧНИК ответа на вопрос «handoff сохранён?». Раньше ответом было значение
    параметра по умолчанию, и он был утвердительным всегда.
    """
    if not child_root:
        return None
    if session_id:
        p = path_for(child_root, session_id)
        return p if p.is_file() else None
    base = Path(child_root) / STATE_DIR
    if not base.is_dir():
        return None
    found = sorted((d / "handoff.yaml" for d in base.iterdir() if d.is_dir()),
                   key=lambda p: p.stat().st_mtime if p.is_file() else 0, reverse=True)
    return next((p for p in found if p.is_file()), None)


def render(h):
    """Блок SESSION COMPLETE — форма из разбора владельца, названия разделов человеческие."""
    w = h.get("why_handed_off") or {}

    def tok(n, status):
        return "н/д" if n is None else (f"{n / 1000000:.1f}M" if n >= 1000000
                                        else f"{n / 1000:.0f}k" if n >= 1000 else str(n)) + f" [{status}]"

    def block(title, items):
        if not items:
            return [f"{title}: —"]
        return [f"{title}:"] + [f"  · {i}" for i in items]

    ver = h.get("verification") or {}
    L = [f"=== SESSION COMPLETE — {h.get('repository')} / сессия {h.get('session_id')} ===",
         f"Цель: {h.get('goal')}",
         f"Ветка/ревизия: {h.get('branch') or '—'} @ {h.get('revision') or '—'}",
         f"Почему передаём: контекст {tok(w.get('context_current'), w.get('context_status'))}, "
         f"прочитано всего {tok(w.get('session_total_tokens'), w.get('session_tokens_status'))}, "
         f"ходов {w.get('turns') if w.get('turns') is not None else 'н/д'}"
         + (f", исход {w['outcome']}" if w.get("outcome") else "")]
    L += block("Сделано", h.get("completed"))
    L += block("Решения", h.get("decisions"))
    changed = h.get("changed_files") or []
    if h.get("changed_files_status") == "unavailable":
        L.append("Изменено: н/д (git не ответил — не пусто, а неизвестно)")
    else:
        L += block(f"Изменено ({len(changed)})", changed[:20]
                   + ([f"…и ещё {len(changed) - 20}"] if len(changed) > 20 else []))
    L += block("Проверки прошли", ver.get("passed"))
    L += block("Проверки упали", ver.get("failed"))
    L += block("Открыто", h.get("open_questions"))
    L += block("Риски", h.get("known_risks"))
    L.append(f"Следующий шаг: {h.get('next_action')}")
    if h.get("workitems"):
        L.append(f"WorkItem'ы сессии: {', '.join(map(str, h['workitems']))}")
    return "\n".join(L)


def main(argv=None):
    argv = list(argv or [])
    sid = goal = None
    args, it = [], iter(argv)
    for a in it:
        if a == "--session":
            sid = next(it, None)
        elif a == "--goal":
            goal = next(it, None)
        elif not a.startswith("--"):
            args.append(a)
    root = args[0] if args else "."
    try:
        from ai_ops_kit.engops import session_telemetry
        snap = session_telemetry.snapshot(root, session_id=sid)
    except Exception:  # noqa: BLE001 — телеметрия недоступна: разделы соберутся, числа будут н/д
        snap = {}
    h = build(root, snap, goal=goal)
    if "--write" in argv:
        h["written_to"] = str(write(root, h))
    print(json.dumps(h, ensure_ascii=False, indent=2) if "--json" in argv else render(h))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
