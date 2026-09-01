#!/usr/bin/env python3
"""process_spend.py — потолок траты на ПРОЦЕССНЫЕ шаги до первой правки кода (решение владельца).

ПОВОД — ЗАМЕР ПОЛЯ (ИИ-Среда, 15.08.2026): две сессии из пяти сожгли по 200+ тысяч токенов на
`onboard -> specify -> plan` и встали без единой строки кода. Ни один существующий потолок этого не
видел, и это не случайность — они меряют другое:
  * `engine/budget.py` ограничивает ОДИН прогон движка (вызовы модели, стоимость);
  * `gates/economic_preflight` и `shared/usage_ledger` — вызовы моделей САМОГО кита;
  * `engops/session_guardrails.session_token_budget` — расход сессии ЦЕЛИКОМ (20M токенов).
Залипание на описании умещается во все три: 200 тысяч токенов — это норма для сессии и ноль для
ledger'а, потому что модель звал не кит, а разговор вокруг кита.

ЧТО МЕРИТСЯ ЗДЕСЬ: расход ТЕКУЩЕЙ живой сессии рантайма МЕЖДУ первым процессным шагом по этой работе
В ЭТОЙ ЖЕ СЕССИИ и текущим моментом, при условии что код ещё не тронут. Порог — 50 000 токенов
(владелец, 2026-08-17; замер поля показывал 200 000+, порог назван вчетверо меньшим сознательно).
Настраивается ключом `session_economy.process_spend_ceiling_before_code` в `.ai-ops.yaml`.

СЧЁТ ПРИВЯЗАН К СЕССИИ, А НЕ К ИСТОРИИ (полевой дефект 487d952b). `session_total` — накопленный расход
транскрипта ТЕКУЩЕЙ сессии; отсчёт, снятый в ДРУГОЙ сессии, из него не вычитается — иначе на первом же
шаге свежей работы кит показывал бы трату прошлых сессий (в поле: ~559k против 50k ещё до того, как в
задаче что-либо описали). Смена сессии рантайма (id из ENV `CLAUDE_CODE_SESSION_ID`, иначе из
транскрипта) переносит отсчёт на текущую сессию: новая сессия — новый бюджет разбора.

«КОД ТРОНУТ» — ВЫВОД, А НЕ ОБЪЯВЛЕНИЕ. Спрашиваем git: есть ли изменения (в индексе или в дереве) по
путям, которые киту НЕ принадлежат (список получен замером свежей установки, см. `_KIT_PREFIXES`).
Объявлять это флагом было бы тем же классом, что `status: done` без результата: кто объявляет, тот и
ошибается в свою пользу.

ЧЕСТНОСТЬ ПЕРЕД БЛОКИРОВКОЙ. Расход сессии измерим только при доступном транскрипте рантайма. Нет
числа -> состояние `unknown`, и оно НЕ блокирует: остановить работу на основании неизвестного числа
означало бы выдать «не знаю» за «слишком дорого». Про `unknown` кит говорит вслух — молчаливое
«норма» здесь и было бы ложью.

CLI:  process_spend.py <child_root> --workitem WID [--intent specify] [--session-total N] [--json]
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402,F401

# Потолок владельца (2026-08-17). Живёт в session_economy, потому что это политика экономии сессии,
# а не параметр прогона: у прогона свой бюджет и другой предмет измерения.
CEILING_KEY = "process_spend_ceiling_before_code"
CEILING_DEFAULT = 50000

# Процессные шаги — те, что ОПИСЫВАЮТ КОНКРЕТНУЮ РАБОТУ. `run`/`do` здесь нет: они её делают, и с
# их началом процессная фаза кончается. `status`/`next` — чтение, оно ничего не описывает.
#
# `onboard`/`model` тоже стоят денег, но их здесь НЕТ, и это осознанно: они про репозиторий, а не про
# работу, у них нет workitem_id, и точку отсчёта расхода вешать не на что. Считать их «шагом работы»
# значило бы приписать первой же задаче стоимость знакомства с проектом — то есть соврать в числе.
PROCESS_INTENTS = ("discuss", "specify", "plan")

STATES = ("normal", "attention", "over_ceiling", "code_started", "unknown")

# ЧТО НЕ СЧИТАЕТСЯ ПРАВКОЙ КОДА — СПИСОК ПО ЗАМЕРУ, А НЕ ПО ПАМЯТИ. Получен так: кит поставлен в
# пустой git-репозиторий, и `git status` показал ровно эти пути (проба шва 2026-08-17). Пока список
# состоял из трёх каталогов, свежеустановленная дочка читалась как «код уже правится» — то есть
# потолок не применялся НИКОГДА и ни один тест этого не видел, потому что все они мерили кит, а не
# установленную копию. Это тот самый класс F-030/F-032, и он поймался только пробой на дочке.
#
# `planning/`, `ROADMAP.md`, `CLAUDE.md`, `.github/` в списке сознательно: их правка — это описание
# работы или настройка, а мерится здесь «начали ли ДЕЛАТЬ». Правка плана — не начало работы.
# `.ai-ops/` добавлен 20.08.2026 вместе с bootstrap продуктового слоя (`_seed_product_layer`): кит
# сам создаёт `.ai-ops/{PRODUCT_PASSPORT,ROADMAP,DELIVERY}.md`, `POLICY.yaml`, `templates/` при
# `ai-ops init`/`update`, значит свежая поставка НЕ должна читаться как правка кода — тот же шов и
# тот же случай, что с `.gitignore`/`.gitattributes` (проба `test_fresh_install_is_not_a_code_change`).
# Правка этих артефактов — описание продукта, а не «начали делать»: та же категория, что `ROADMAP.md`
# и `planning/`. ГРАНИЦА `engops/` (лента B) ПЕРЕСЕЧЕНА ОСОЗНАННО, правка на одну строку; сказано в PR.
_KIT_PREFIXES = (".ai/", ".ai-ops/", ".claude/", ".github/", "features/", "planning/", "history/")
_KIT_FILES = (".ai-ops.yaml", "ai-ops", "AI-OPS-ONBOARDING.md", "CLAUDE.md", "ROADMAP.md",
              # `.gitattributes` добавлен 18.08.2026 вместе с его установкой: `ensure_gitattributes`
              # пишет его в дочку, значит свежая поставка НЕ должна читаться как правка кода — ровно
              # тот же случай, что был с `.gitignore` (проба шва test_fresh_install_is_not_a_code_change).
              ".gitignore", ".gitattributes")


def _is_kit_path(path):
    """Путь принадлежит киту (его поставке или его артефактам), а не коду продукта."""
    return path in _KIT_FILES or any(path.startswith(p) for p in _KIT_PREFIXES)


STATE_FILE = Path(".ai") / "runtime" / "process-spend.yaml"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ceiling(child_root, policy=None):
    """Потолок для этого репозитория. -> int или None (потолок выключен значением 0/null)."""
    if policy is None:
        from ai_ops_kit.engops import session_guardrails
        policy = session_guardrails.load_policy(child_root)
    val = policy.get(CEILING_KEY, CEILING_DEFAULT)
    try:
        val = int(val)
    except (TypeError, ValueError):
        return CEILING_DEFAULT
    return val if val > 0 else None


def state_path(child_root):
    return Path(child_root) / STATE_FILE


def load_state(child_root):
    """Журнал процессных шагов. Битый файл -> пустой журнал: он не источник истины о работе."""
    p = state_path(child_root)
    if not p.is_file():
        return {"schema_version": 1, "kind": "ProcessSpendLog", "works": {}}
    try:
        import yaml
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — журнал восстановим; ронять команду из-за него нельзя
        return {"schema_version": 1, "kind": "ProcessSpendLog", "works": {}}
    if not isinstance(doc.get("works"), dict):
        doc = {"schema_version": 1, "kind": "ProcessSpendLog", "works": {}}
    return doc


def save_state(child_root, doc):
    import yaml
    p = state_path(child_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


def code_changed(child_root):
    """Тронут ли код продукта. -> True/False/None (None — git не ответил, это «не знаю»).

    Смотрим и индекс, и рабочее дерево, и неотслеживаемые файлы: правка, которую ещё не закоммитили,
    — всё равно правка. Пути самого кита исключены (см. `_is_kit_path`).
    """
    try:
        r = subprocess.run(["git", "-C", str(child_root), "status", "--porcelain"],
                           capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    for line in (r.stdout or "").splitlines():
        path = line[3:].strip().strip('"')
        if " -> " in path:                     # переименование: считаем по месту назначения
            path = path.split(" -> ", 1)[1].strip().strip('"')
        if not path:
            continue
        if _is_kit_path(path):
            continue
        return True
    return False


def record_step(child_root, wid, intent, session_total, session_id=None):
    """Отметить процессный шаг. -> запись работы. Первый шаг задаёт точку отсчёта расхода.

    Точка отсчёта берётся ОДИН раз В ПРЕДЕЛАХ ОДНОЙ СЕССИИ и внутри неё не переписывается: иначе
    каждый следующий шаг обнулял бы счёт, и потолок не срабатывал бы никогда — ровно та ошибка, из-за
    которой залипание и не ловилось. НО смена сессии рантайма (id, отличный от записанного) переносит
    отсчёт на текущую сессию — новый бюджет разбора (см. счёт-привязан-к-сессии в docstring модуля).
    При НЕизвестном id ничего не трогаем: «не знаю» не повод сбросить.
    """
    doc = load_state(child_root)
    wid = str(wid)
    entry = doc["works"].get(wid)
    new = not isinstance(entry, dict)
    if new:
        entry = {"first_step": intent, "first_step_at": _now(),
                 "first_step_session_tokens": session_total,
                 "first_step_session_id": session_id, "steps": []}
    base_sid = entry.get("first_step_session_id")
    session_changed = (not new) and session_id is not None and session_id != base_sid
    first_measurable = entry.get("first_step_session_tokens") is None and session_total is not None
    if session_changed or first_measurable:
        # session_changed — новая сессия рантайма. first_measurable — первый шаг был при недоступном
        # транскрипте, отсчёт берём с первого измеримого (иначе ложное «пробит» на первом же числе).
        entry["first_step_session_tokens"] = session_total
        entry["first_step_session_id"] = session_id
        entry["baseline_from"] = intent
        if session_changed:
            entry["baseline_session_changed_at"] = _now()
    entry["steps"] = (entry.get("steps") or []) + [
        {"intent": intent, "at": _now(), "session_total_tokens": session_total}]
    entry["last_step"] = intent
    entry["last_step_at"] = entry["steps"][-1]["at"]
    doc["works"][wid] = entry
    save_state(child_root, doc)
    return entry


def classify(spent_on_process, limit):
    """Состояние по расходу процессной фазы. None -> unknown (не «норма»)."""
    if spent_on_process is None or not limit:
        return "unknown"
    if spent_on_process >= limit:
        return "over_ceiling"
    if spent_on_process >= limit * 0.7:
        return "attention"
    return "normal"


# «Число не передано» и «числа нет» — РАЗНЫЕ входы, и различать их обязательно: без сентинела
# явный `session_total=None` уходил в телеметрию и возвращался измеренным, то есть проверить
# поведение на неизвестном расходе было нечем (нашлось тестом сразу же).
_UNSET = object()


def assess(child_root, wid, intent, session_total=_UNSET, session_id=None, policy=None, record=True):
    """Оценить процессную фазу этой работы. -> ProcessSpendCheck.

    `blocks=True` означает: шаг делать НЕ надо, надо спросить владельца. Блокирует только
    `over_ceiling` при нетронутом коде — то есть измеренная трата на описание без единой правки.
    """
    child_root = Path(child_root)
    limit = ceiling(child_root, policy)
    if session_total is _UNSET:
        session_total = _session_total(child_root, wid)
        # Личность живой сессии доезжает до РЕШЕНИЯ: без неё трата этой работы неотличима от прошлых
        # сессий (дефект 487d952b). Тот же живой рантайм, что и расход.
        session_id = _session_id(child_root, wid)
    touched = code_changed(child_root)
    entry = record_step(child_root, wid, intent, session_total, session_id) if record \
        else (load_state(child_root)["works"].get(str(wid)) or {})

    base = entry.get("first_step_session_tokens")
    spent = _work_scoped_spend(entry, session_total, session_id)

    if touched is True:
        state, reason = "code_started", "код уже правится — процессная фаза закрыта, потолок не применяется"
    else:
        state = classify(spent, limit)
        if state == "unknown" and session_total is not None and base is not None:
            reason = ("точка отсчёта снята в другой сессии — трату разбора ЭТОЙ работы в текущей "
                      "сессии посчитать нечем; потолок не применяю и не выдаю это за норму")
        elif state == "unknown":
            reason = ("расход сессии не измерим (нет транскрипта рантайма) — потолок процессных "
                      "шагов не применяю и не выдаю это за норму")
        elif state == "over_ceiling":
            reason = (f"на описание работы ушло {_tok(spent)} токенов, кода ещё нет; "
                      f"потолок владельца — {_tok(limit)}")
        elif state == "attention":
            reason = (f"на описание ушло {_tok(spent)} из {_tok(limit)} токенов, кода ещё нет — "
                      "стоит довести объявленный шаг или назвать, чего не хватает, а не углубляться в разбор")
        else:
            reason = f"на описание ушло {_tok(spent)} из {_tok(limit)} токенов"

    return {
        "schema_version": 1, "kind": "ProcessSpendCheck",
        "workitem_id": str(wid), "intent": intent,
        "state": state, "blocks": state == "over_ceiling",
        "ceiling": limit,
        "spent_on_process": spent,
        "session_total_tokens": session_total,
        "baseline_session_tokens": base,
        "session_id": session_id,
        "baseline_session_id": entry.get("first_step_session_id"),
        "code_changed": touched,
        "process_steps": [s.get("intent") for s in (entry.get("steps") or [])],
        "measurement": "measured" if session_total is not None else "unavailable",
        "decision_ref": "потолок владельца 2026-08-17: 50 000 токенов на описание до первой правки кода",
        "reason": reason,
    }


def _work_scoped_spend(entry, session_total, session_id):
    """Трата на разбор ТЕКУЩЕЙ работы в ТЕКУЩЕЙ сессии. -> int или None (не измеримо -> unknown).

    None, а НЕ расход сессии целиком: точка отсчёта и текущий замер ТОЧНО из разных сессий (обе
    известны и различны) -> вычесть значило бы посчитать трату прошлых сессий (дефект 487d952b), и
    «не знаю» тут честнее. Обычный путь (record=True) сюда не доходит — record_step переносит отсчёт
    на текущую сессию; ветка держит честность на пути record=False (preview/CLI).
    """
    base = entry.get("first_step_session_tokens")
    base_sid = entry.get("first_step_session_id")
    if base is None or session_total is None:
        return None
    # База из ИЗВЕСТНОЙ сессии, а личность текущей неизвестна (session_id=None, напр. рантайм без
    # CLAUDE_CODE_SESSION_ID): same-session НЕ подтвердить -> unknown, а не вычитать. Иначе, если
    # база из прошлой сессии, вернулся бы расход прошлых сессий (дефект 487d952b) под видом текущего.
    if base_sid is not None and session_id is None:
        return None
    if session_id is not None and base_sid is not None and session_id != base_sid:
        return None
    return max(0, session_total - base)


def _session_total(child_root, wid):
    try:
        from ai_ops_kit.engops import session_telemetry
        snap = session_telemetry.snapshot(child_root, workitem_id=wid)
        return snap.get("session_total_tokens")
    except Exception:  # noqa: BLE001 — телеметрия недоступна -> unknown, а не 0
        return None


def _session_id(child_root, wid):
    """Личность живой сессии рантайма: ENV -> транскрипт. None, если сессии нет.

    ENV первичен: он есть и когда транскрипт ещё не найден, и стабилен весь срок сессии.
    """
    try:
        from ai_ops_kit.engops import session_telemetry_provider as _p
        env_sid = _p._env(_p.ENV_SESSION_ID_KEYS)
        if env_sid:
            return env_sid
        from ai_ops_kit.engops import session_telemetry
        sid = session_telemetry.snapshot(child_root, workitem_id=wid).get("session_id")
        return sid if sid and sid != "unlabelled" else None
    except Exception:  # noqa: BLE001 — сессии нет -> личности нет, это «не знаю», а не подстановка
        return None


def _tok(n):
    if n is None:
        return "н/д"
    return f"{n / 1000:.0f}k" if n >= 1000 else str(n)


def render(c):
    L = [f"PROCESS-SPEND {c['workitem_id']} ({c['intent']}): {c['state']} — {c['reason']}",
         f"  шагов описания: {', '.join(c['process_steps']) or '—'} · "
         f"код тронут: {'да' if c['code_changed'] else ('н/д' if c['code_changed'] is None else 'нет')}"]
    if c["blocks"]:
        L.append("  -> шаг не делаю: нужно решение владельца")
    return "\n".join(L)


def main(argv=None):
    argv = list(argv or [])
    wid = intent = None
    total = None
    args = []
    it = iter(argv)
    for a in it:
        if a == "--workitem":
            wid = next(it, None)
        elif a == "--intent":
            intent = next(it, None)
        elif a == "--session-total":
            v = next(it, None)
            total = int(v) if v and v.isdigit() else None
        elif not a.startswith("--"):
            args.append(a)
    root = args[0] if args else "."
    c = assess(root, wid or "—", intent or "plan",
               session_total=total if total is not None else _UNSET, record=False)
    print(json.dumps(c, ensure_ascii=False, indent=2) if "--json" in argv else render(c))
    return 2 if c["blocks"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
