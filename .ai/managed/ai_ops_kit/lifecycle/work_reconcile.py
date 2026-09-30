#!/usr/bin/env python3
"""Живость держателя и сверка заявок active-work с базой — измерение git.

Сателлит `lifecycle/active_work.py` (вынесен из монолита без изменения поведения). Здесь — та часть
координации, что судит ВНЕШНЮЮ реальность, а не локальный реестр: имя машины и время (личность
заявки), доказательство смерти держателя (процесс/возраст) и сверка ветки с базой (расхождение,
`is-ancestor`, снятие `superseded`). Локальный реестр, команды и классификацию пересечений держит
`active_work`; он же ре-экспортирует эти имена, поэтому `active_work.<имя>` и прежние импорты работают.
"""
from __future__ import annotations

import os
import socket
from datetime import datetime, timezone

from ai_ops_kit.shared import gitio                    # единый вход к git с таймаутом (см. shared/gitio)


def _machine() -> str:
    """Имя машины — часть заявки: «кто держит» без «где» не разобрать при инциденте."""
    try:
        return socket.gethostname() or "unknown"
    except OSError:
        return "unknown"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# session-заявка старше этого (в часах) без finish считается БРОШЕННОЙ. Замер поля 01.09.2026:
# вчерашняя `session:*`-заявка держала доставку сутки — liveness по session-id не проверить, но
# возраст в полсуток не догадка: прогон длиной 12ч нереален, перезапуск сессии/машины — обычен.
_CLAIM_STALE_HOURS = 12

# ЖЁСТКИЙ ПОТОЛОК ВОЗРАСТА (в днях). Замер поля 24.08→18.09.2026 (dogfood, #1048): заявка сессионной
# личности с ЧУЖОЙ машины (`derived-counts`, session:5e296f8f, MacBook-Air-Sasa.local) провисела 24
# дня и навсегда блокировала `next`, хотя работа давно в history — reap чужую session-заявку не
# трогает («вдруг сессия жива»), а возрастной порог `_CLAIM_STALE_HOURS` применяется только к своей
# машине. Потолок ЩЕДРЫЙ (две недели), чтобы не задеть легитимную долгую чужую заявку в пределах
# разумного, но КОНЕЧНЫЙ: ни один прогон/сессия не держит claim две недели — после порога заявка
# снимается ДАЖЕ на чужой машине. Настраивается через env `AI_OPS_CLAIM_HARD_TTL_DAYS`.
_CLAIM_HARD_TTL_DAYS = 14


def _hard_ttl_hours() -> float:
    """Жёсткий потолок возраста заявки в часах. Env `AI_OPS_CLAIM_HARD_TTL_DAYS` переопределяет дни;
    непарсируемое/непозитивное значение -> дефолт (не отключаем защиту по битой настройке)."""
    try:
        days = float(os.environ.get("AI_OPS_CLAIM_HARD_TTL_DAYS", _CLAIM_HARD_TTL_DAYS))
    except (ValueError, TypeError):
        days = _CLAIM_HARD_TTL_DAYS
    if days <= 0:
        days = _CLAIM_HARD_TTL_DAYS
    return days * 24.0


def _claim_age_hours(started_iso) -> float:
    """Возраст заявки в часах по её `started_at` (ISO). Нечитаемое время -> 0.0 (не гасим по догадке)."""
    try:
        t = datetime.fromisoformat(str(started_iso))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return 0.0


def _pid_is_dead(pid) -> bool:
    """Доказано ли, что процесс `pid` на ЭТОЙ машине уже не существует. Неизвестность -> False.

    Смертью считается ТОЛЬКО `ProcessLookupError` (процесса нет). `PermissionError` — «процесс есть,
    просто чужой», любая иная ошибка ОС или непарсируемый/невалидный pid — «не знаю», и заявку по
    догадке не снимаем. Переиспользованный ОС pid даёт «жив» — это безопасная сторона (не снимем
    рано), а не «мёртв»."""
    if pid in (None, ""):
        return False
    try:
        pid_i = int(pid)
    except (ValueError, TypeError):
        return False
    if pid_i <= 0:
        return False
    try:
        os.kill(pid_i, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False               # процесс есть, просто чужой
    except OSError:
        return False
    return False


def holder_is_gone(entry, machine=None) -> bool:
    """Держатель заявки уже не существует? -> True только когда это ДОКАЗАНО (процессом или возрастом).

    Личность держателя бывает двух видов. `pid:1234` — конкретный процесс на конкретной машине: нет
    процесса — заявку держать некому. `session:ab12cd34` — идентификатор рантайма, он живёт дольше
    процесса, и по САМОМУ id liveness не проверить. Два доказательства смерти session-заявки:
      1. `owner_pid` — pid ПРОЦЕССА прогона, который держал заявку (пишется при register). Прогон
         живёт внутри процесса: если этот pid на нашей машине мёртв, а работа не снята через finish,
         прогон умер, не убрав за собой. Это доказуемо сразу, не дожидаясь порога возраста — ровно
         тот случай из обратной связи, где заявка мёртвой сессии висела предупреждением весь заход.
      2. ВОЗРАСТ — заявка старше `_CLAIM_STALE_HOURS` без finish держать некому (перезапуск
         сессии/машины реальнее прогона длиной в полсуток). Остаётся как страховка, когда pid не
         записан (старые заявки) или процесс формально ещё жив.
    Без этого честный отказ второй сессии становился помехой: одиночный повторный прогон получал
    «её держит другой» от процесса, которого нет, или от сессии, которой уже нет.

    ЖЁСТКИЙ ПОТОЛОК (#1048). Сверх двух доказательств выше есть безусловный: заявка старше
    `_hard_ttl_hours()` (по умолчанию 14 дней) держать некому НИ НА КАКОЙ машине. До него чужая
    session-заявка не снималась вовсе — и мёртвая висела вечно, блокируя `next`. СВЕЖИЕ чужие заявки
    (моложе потолка) по-прежнему уважаются как авторитет координации; меняется судьба только ОЧЕНЬ
    старых.
    """
    started = entry.get("started_at") or entry.get("since")
    if started and _claim_age_hours(started) >= _hard_ttl_hours():
        return True                # жёсткий потолок: очень старую заявку снимаем даже с чужой машины
    holder = str(entry.get("owner_session") or "")
    if holder.startswith("pid:"):
        if (entry.get("machine") or "") != (machine or _machine()):
            return False           # чужая машина: её процессы отсюда не видны, значит не знаем
        try:
            pid = int(holder.split(":", 1)[1])
        except (ValueError, IndexError):
            return False
        return _pid_is_dead(pid)
    # session-личность: судим ТОЛЬКО на своей машине. Чужую машину не трогаем — её сессия может быть
    # жива, а её ОПУБЛИКОВАННАЯ заявка есть авторитет координации (как и в pid-пути выше: «чужая
    # машина — не знаю»). Заявка без поля machine считается локальной (register всегда пишет machine
    # своей машины; пустое — тестовый/битый артефакт, судим как своё).
    claim_machine = entry.get("machine") or ""
    if claim_machine and claim_machine != (machine or _machine()):
        return False
    # Признак смерти сессии, не дожидаясь возрастного порога: pid процесса прогона на нашей машине
    # уже мёртв. Поля нет (старая заявка) -> `_pid_is_dead` вернёт False, и решает возраст.
    if _pid_is_dead(entry.get("owner_pid")):
        return True
    started = entry.get("started_at") or entry.get("since")
    return bool(started) and _claim_age_hours(started) >= _CLAIM_STALE_HOURS


def _divergence(child_root, branch, base):
    """Расхождение ветки и базы В ОБЕ СТОРОНЫ. -> (ahead, behind) или (None, None), если не измерено.

    ПОЛЕ 17.08.2026: в дочке нашлась ветка ВПЕРЕДИ base на 1 коммит и ПОЗАДИ на 241 — проверка
    «содержится в base» по ОДНОМУ направлению давала «не влито» на давно закрытой задаче. Поэтому
    оба числа считаются и оба показываются: одно из них без другого вводит в заблуждение."""
    # Единый вход к git с таймаутом (см. shared/gitio).
    rc, out, _ = gitio.git(child_root, "rev-list", "--left-right", "--count", f"{base}...{branch}")
    if rc != 0:
        return None, None
    parts = out.split()
    if len(parts) != 2:
        return None, None
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return None, None
    return ahead, behind


def _same_ref(child_root, a, b) -> bool:
    """Указывают ли два имени на ОДНУ И ТУ ЖЕ ветку. -> bool (не разобрали — False, не угадываем)."""
    # Единый вход к git с таймаутом (см. shared/gitio).
    if not a or not b:
        return False
    if str(a) == str(b):
        return True
    def full(name):
        rc, out, _ = gitio.git(child_root, "rev-parse", "--symbolic-full-name", str(name))
        return out if rc == 0 else None
    fa, fb = full(a), full(b)
    return bool(fa) and fa == fb


def reconcile_with_base(entries, child_root, base=None):
    """Сверить записи реестра с базой. -> новый список записей (исходные НЕ мутируются).

    ЗАЯВКА #137, поле 17.08.2026 (дочка ИИ-Среда): реестр держал четыре записи незакрытыми, и ТРИ ИЗ
    ЧЕТЫРЁХ относились к работе, давно влитой в main. Настоящий хвост был один. Подтверждено замером
    на 3.36.12: ветка работы влита обычным merge, запись оставлена `blocked`, и `ai-ops status`
    отвечает «Работа идёт» и советует не трогать те же файлы. Сверки с базой не было НИКАКОЙ: ни
    `merged`, ни `is-ancestor`, ни `superseded`.

    ЦЕНА, НАЗВАННАЯ ПОЛЕМ: реестр превращается в список страшилок — либо переделываешь готовое (в
    дочке почти начали доделывать задачу, закрытую месяц назад), либо перестаёшь ему верить, и тогда
    он не нужен.

    ЧТО ЗДЕСЬ. Для каждой записи с веткой: база берётся тем же резолвером, что у `run`/`review`
    (`pipeline_git._resolve_base` — автоподбор, а не хардкод `main`); считаются ОБА числа
    расхождения; если коммиты ветки уже содержатся в базе (`merge-base --is-ancestor`), запись
    помечается `superseded` с названной причиной и ДАТОЙ замера. Не измерили — говорим `None` и
    называем почему; отсутствие сверки не выдаётся за «не влито»."""
    # Единый вход к git с таймаутом (см. shared/gitio).
    out = []
    src = list(entries or [])
    if not src:
        return out
    from ai_ops_kit.shared import gitio as _gitio   # foundation (K5): резолвер базы — чистый git-запрос
    resolved = _gitio.resolve_base(child_root, base)
    base_ref = resolved.get("base_ref") if resolved.get("resolved") else None
    note = None if base_ref else (resolved.get("reason") or "база не определена")
    at = _now_iso()
    for w in src:
        e = dict(w)
        branch = e.get("branch")
        if not branch or e.get("status") == "done":
            out.append(e)
            continue
        if not base_ref:
            e["reconcile_note"] = f"сверка с базой не выполнена: {note}"
            e["merged_into_base"] = None
            out.append(e)
            continue
        e["base_ref"] = base_ref
        # БАЗА, СОВПАДАЮЩАЯ С САМОЙ ВЕТКОЙ, НИЧЕГО НЕ ДОКАЗЫВАЕТ (замер 20.08.2026). В рабочей копии
        # прогона HEAD — это и есть заявленная ветка, и `_resolve_base` отдаёт её же: сверка
        # получалась `ai-ops/w` против `ai-ops/w` («впереди 0, позади 0»), любая заявка объявлялась
        # влитой, отказ второй сессии не срабатывал, а `status` говорил, что работа не идёт. Кит сам
        # ставит дочку в такую копию (`worktree.add` -> `.ai/worktrees/<работа>`), так что место
        # штатное. Третье состояние: это «не измерили», а не «не влито» и не «влито».
        if _same_ref(child_root, branch, base_ref):
            e["merged_into_base"] = None
            e["reconcile_note"] = (f"база совпадает с самой веткой '{branch}' (рабочая копия этой "
                                   f"работы) — сверка невозможна, заявка остаётся как есть")
            out.append(e)
            continue
        if gitio.git(child_root, "rev-parse", "--verify", "--quiet", branch)[0] != 0:
            # ветки нет локально: сказать это, а не молча считать работу идущей
            e["merged_into_base"] = None
            e["reconcile_note"] = f"ветки '{branch}' нет в этом репозитории — сверка невозможна"
            out.append(e)
            continue
        ahead, behind = _divergence(child_root, branch, base_ref)
        e["ahead"], e["behind"] = ahead, behind
        merged = gitio.git(child_root, "merge-base", "--is-ancestor", branch, base_ref)[0] == 0
        e["merged_into_base"] = merged
        if merged:
            e["status"] = "superseded"
            e["status_reason"] = (f"изменения ветки '{branch}' уже в базе '{base_ref}' "
                                  f"(впереди {ahead}, позади {behind}) — запись сняла сверка")
            e["status_reason_at"] = at
        out.append(e)
    return out
