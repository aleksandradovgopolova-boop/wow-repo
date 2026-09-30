#!/usr/bin/env python3
"""Реестр активных работ репозитория (v2.22, связи задач — v2.23) — координация
параллельных сессий.

Несколько сессий Claude могут работать в одном репозитории одновременно (новая фича,
фикс интерфейса, аналитика, безопасность). Чтобы они не уничтожали работу друг друга,
каждая регистрирует свою работу здесь: id WorkItem, ветка, затрагиваемые зоны, сессия,
а также ЯВНЫЕ связи — от кого зависит (`depends_on`) и какие общие контракты трогает
(`shared_contracts`). Новая сессия видит карту и получает conflict forecast с типом:

  - area        — две сессии трогают одну зону кода/продукта;
  - contract    — две сессии трогают один общий контракт (схема данных, API, артефакт) →
                  риск расхождения контракта, зафиксируйте общий;
  - dependency  — задача ждёт другую активную задачу (её зависимость ещё не done);
  - cycle       — циклическая зависимость задач (ошибка, не предупреждение).

Реестр НЕ блокирует файлы жёстко, а предупреждает и предлагает решение.

Использование:
  active_work.py register <file> <id> --branch B --areas a,b --session S
                 [--workitem P] [--status in-progress] [--depends x,y] [--contracts p,q] [--at DATE]
  active_work.py list     <file> [--json]
  active_work.py check    <file> --areas a,b [--depends x,y] [--contracts p,q] [--exclude id] [--json]
  active_work.py finish   <file> <id>
  active_work.py --selftest
Возврат: 0 — ок (пересечения area/contract/dependency — предупреждения, не ошибка);
1 — ошибка использования/данных или циклическая зависимость при register.
"""
from __future__ import annotations

import argparse
import os
import contextlib
import json
import sys
from pathlib import Path

import yaml

from ai_ops_kit.shared import lifecycle_store as _ls   # v3.0.12: durable запись + fail-closed чтение общего реестра
from ai_ops_kit.lifecycle import role_handoff as _handoff  # #639: named-переход owner_role роль->роль
# Соседи-сателлиты этого пакета (вынесены из монолита без изменения поведения): носитель заявок
# (публикация/копии/team_view) и измерение git (личность держателя, его живость, сверка с базой).
# active_work остаётся тонким: локальный реестр, команды CLI и классификация пересечений. Имена
# ре-экспортируются, поэтому `active_work.<имя>` и прежние импорты продолжают работать — внешних
# потребителей на новый путь переводить не нужно.
from ai_ops_kit.lifecycle.work_claims import (          # noqa: F401 — ре-экспорт для совместимости
    CLAIMS_DIR_REL, PUBLISHED_FIELDS, COPIES_CLAIMS_REL, COPY_CLAIM_FIELDS,
    _claim_slug, publish_claim, unpublish_claim, load_published_claims,
    _git_common_dir, copies_claims_dir, working_copies, copies_reach_note,
    _copies_line, claim_to_copies, withdraw_claim_from_copies, load_copy_claims,
    team_view,
)
from ai_ops_kit.lifecycle.work_reconcile import (        # noqa: F401 — ре-экспорт для совместимости
    _machine, _now_iso, _CLAIM_STALE_HOURS, _claim_age_hours, _pid_is_dead,
    holder_is_gone, _divergence, _same_ref, reconcile_with_base,
)

STATUS = {"in-progress", "review", "blocked", "done", "superseded"}
# `superseded` (18.08.2026, заявка #137): работа, изменения которой УЖЕ В БАЗЕ. Это не «done»
# (никто её не закрывал) и не «brought» — это запись, которую сняла СВЕРКА, и её причина
# названа замером, а не догадкой (`reconcile_with_base`).

CONFIG_REL = ".ai-ops.yaml"


def publication_enabled(child_root) -> bool:
    """Публикуется ли реестр заявок за пределы ЭТОЙ машины. По умолчанию — НЕТ.

    Решение владельца 18.08.2026 (`ep-2026-08-18-claim-medium-hybrid`): заявка живёт локально, а
    публикация в общий носитель — только по ЯВНОМУ включению `team_coordination.publish: true` в
    `.ai-ops.yaml`. Дефолт False выбран не для удобства, а как самый безопасный: он НИКОГДА не
    выдаёт локальное состояние за координацию команды. Любая неоднозначность (нет файла, битый yaml,
    yaml недоступен) читается как «не опубликовано» — то же самое соображение.
    """
    if yaml is None or child_root is None:
        return False
    p = Path(child_root) / CONFIG_REL
    if not p.is_file():
        return False
    try:
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return False
    tc = cfg.get("team_coordination") or {}
    return bool(tc.get("publish", False))


def reach_note(published: bool) -> str:
    """Одна честная строка о ДОСЯГАЕМОСТИ реестра (ep-2026-08-18-claim-medium-hybrid, условие 3).

    Смысл: локальное состояние не должно читаться как координация команды. Пока публикация выключена,
    кит обязан сказать, что видит только свою машину, — а не подавать пересечения так, будто видит
    заявки других участников. Это ровно тот ложный green, против которого стоит весь контур.
    """
    if published:
        return ("Реестр публикуется: кит видит заявки других машин команды. При публикации уезжают "
                "id работы, ветка, машина, время, сессия — НЕ содержимое файлов.")
    return ("Это заявки ТОЛЬКО этой машины: работу других участников кит здесь не видит — публикация "
            "выключена (team_coordination.publish в .ai-ops.yaml). Пересечения, если они ниже есть, — "
            "про параллельные сессии на этой машине, а не про команду.")


def shared_registry_path(start=None):
    """Путь к реестру, ОБЩЕМУ для всех worktree одного репозитория. -> Path.

    ЗАЧЕМ ЭТО ПОЯВИЛОСЬ (замер 12.08.2026). Протокол параллельной работы
    (`docs/parallel-sessions.md`) требует двух вещей одновременно: сессия работает в своём
    `git worktree` (иначе чужой `checkout` уводит незакоммиченные правки) И заявляет область записи
    в общем реестре (иначе две сессии берут одну работу). В таком виде правила ПРОТИВОРЕЧИЛИ друг
    другу: `.ai/runtime/active-work.yaml` лежит внутри рабочего дерева, то есть у каждого worktree
    свой — проверено, файл, созданный в одном, из другого не виден вовсе. Реестр, невидимый другой
    сессии, — это не координация, а её видимость.

    Поэтому путь берётся из `git rev-parse --git-common-dir`: этот каталог ОДИН на репозиторий и
    все его worktree (у worktree свой `--git-dir`, но общий `--git-common-dir`). Реестр там же не
    попадает в историю — он состояние машины, а не факт о продукте.

    ЧТО НЕ МЕНЯЕТСЯ: путь дочки `.ai/runtime/active-work.yaml` объявлен в манифесте
    (`ai-ops-manifest.yaml`) и остаётся контрактом — в дочке сессии обычно делят один checkout, и
    там он работает. Эта функция — для случая «несколько worktree одного репозитория».
    """
    common = _git_common_dir(start)
    if common is None:
        raise ActiveWorkCorrupt(
            f"не git-репозиторий или git недоступен ({start or Path.cwd()}): "
            f"общий реестр сессий разместить негде")
    return common / "ai-ops" / "active-work.yaml"


class ActiveWorkCorrupt(Exception):
    """Реестр active-work недостоверен (повреждён/не сохранён) — координация сессий небезопасна."""


def load(path: Path):
    """v3.0.12 (finding аудита блок B): FAIL-CLOSED. Прежде safe_load(...) or {} на битом/пустом реестре
    возвращал ПУСТУЮ карту -> concurrency forecast «пересечений нет» на потерянных записях (две сессии
    сталкивались). Теперь: отсутствует -> fresh; повреждён -> raise (не тихая пустая карта)."""
    g = _ls.load_guarded(Path(path), kind="active-work")
    if g["state"] == "absent":
        return {"schema_version": 1, "kind": "active-work", "active": []}
    if g["state"] == "corrupt":
        raise ActiveWorkCorrupt(f"active-work реестр повреждён ({g['reason']}) — координация "
                                "параллельных сессий недостоверна; нужна явная recovery")
    data = g["data"]
    data.setdefault("schema_version", 1)
    data.setdefault("kind", "active-work")
    data.setdefault("active", [])
    return data


def save(path: Path, data: dict):
    """v3.0.12: АТОМАРНАЯ durable-запись общего реестра (tmp+fsync+rename+fsync-dir+перечитывание).
    Сбой -> raise (registration потеряна — не молчим)."""
    r = _ls.durable_write(Path(path), data, require_keys=("kind", "active"))
    if not r.get("ok"):
        raise ActiveWorkCorrupt(f"не удалось надёжно сохранить active-work: {r.get('error')}")


@contextlib.contextmanager
def _locked(path: Path):
    """v3.0.12 (finding аудита блок B): межпроцессная блокировка вокруг read-modify-write общего реестра,
    чтобы конкурентные register/finish не теряли записи друг друга (last-writer-wins TOCTOU). best-effort:
    на платформах без fcntl (Windows) деградирует до no-op — не хуже прежнего поведения."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        import fcntl
    except ImportError:
        yield
        return
    f = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()


_CLOSED_STATUSES = ("done", "superseded")   # #137: снятое сверкой — не идущая работа


def _active_others(active, exclude_id):
    return [w for w in active if w.get("status") not in _CLOSED_STATUSES and w.get("id") != exclude_id]


def persist_reconciliation(path, reconciled):
    """Записать снятые сверкой статусы в реестр. -> число снятых записей.

    ПОЧЕМУ ЗАПИСЫВАЕМ, А НЕ ТОЛЬКО ПОКАЗЫВАЕМ: сверка на чтении исправляет ОТВЕТ, но не реестр — и
    та же ложь возвращается при следующем чтении, а `register` продолжает считать влитую работу
    активной и предупреждать о ней. Пишем ТОЛЬКО переход в `superseded` и только для своей машины:
    чужая опубликованная заявка — не наша запись, снимать её за другого нельзя."""
    changed = {w["id"]: w for w in (reconciled or [])
               if w.get("status") == "superseded" and w.get("id")}
    if not changed:
        return 0
    mine = _machine()
    n = 0
    with _locked(path):
        data = load(path)
        for w in data.get("active", []):
            upd = changed.get(w.get("id"))
            if not upd or w.get("status") in _CLOSED_STATUSES:
                continue
            if (w.get("machine") or mine) != mine:
                continue
            w["status"] = "superseded"
            w["status_reason"] = upd.get("status_reason")
            w["status_reason_at"] = upd.get("status_reason_at")
            w["base_ref"] = upd.get("base_ref")
            w["ahead"], w["behind"] = upd.get("ahead"), upd.get("behind")
            n += 1
        if n:
            save(path, data)
    return n


def classify(active, entry):
    """Классифицировать пересечения новой/проверяемой работы с активными.
    entry: dict с id, affected_areas, depends_on, shared_contracts, branch, machine, owner_session.
    Возвращает список находок с полем kind ∈ {area, contract, dependency, branch, same-work}.

    branch и same-work добавлены 18.08.2026 — это ровно два случая из заявки #150, которые ломали
    команду: двойная работа на ОДНОЙ ветке и двойная работа над ОДНОЙ работой из разных сессий. Они
    видны и МЕЖДУ машинами, потому что опубликованная заявка несёт branch и id (team_view их подаёт)."""
    from ai_ops_kit.shared import work_areas as _work_areas   # foundation (K5): зоны — чистая утилита
    wid = entry.get("id")
    areas = list(entry.get("affected_areas") or [])
    deps = set(entry.get("depends_on") or [])
    contracts = set(entry.get("shared_contracts") or [])
    my_branch = entry.get("branch")
    my_holder = (entry.get("machine"), entry.get("owner_session"))
    # Ветку и дубль работы флагуем ТОЛЬКО когда у пробы есть личность (машина или сессия): проба без
    # личности — это самопроверка/форкаст без актора, и объявлять её «другим держателем» нельзя
    # (иначе classify считал бы работу саму против себя — селфтест это и стережёт).
    probe_has_identity = bool(entry.get("machine") or entry.get("owner_session"))
    out = []
    if probe_has_identity:
        # «Та же работа у другого держателя» — считаем ДО фильтра по id (иначе своя id спрячет чужую).
        for w in active:
            if w.get("status") in _CLOSED_STATUSES:
                continue
            if w.get("id") == wid and (w.get("machine"), w.get("owner_session")) != my_holder:
                out.append({"kind": "same-work", "id": w.get("id"), "branch": w.get("branch"),
                            "owner_session": w.get("owner_session"), "machine": w.get("machine"),
                            # «с какого момента» решает «ждать или перенимать»; `worktree` — «где
                            # именно», потому что имя хоста у всех копий одной машины общее.
                            "since": w.get("started_at"), "worktree": w.get("worktree"),
                            "detail": wid})
    others = _active_others(active, wid)
    for w in others:
        if probe_has_identity and my_branch and w.get("branch") == my_branch and \
                (w.get("machine"), w.get("owner_session")) != my_holder:
            out.append({"kind": "branch", "id": w.get("id"), "branch": w.get("branch"),
                        "owner_session": w.get("owner_session"), "machine": w.get("machine"),
                        "since": w.get("started_at"), "worktree": w.get("worktree"),
                        "detail": my_branch})
        # ЗАЯВКА #138: было `areas & set(...)` — то есть `unspecified` совпадал с `unspecified`, и две
        # работы без зон «пересекались» друг с другом. Неизвестность не является пересечением; заодно
        # считается ВЛОЖЕННОСТЬ каталогов (работа, объявившая пакет целиком, держит и его подсистемы).
        shared_areas = _work_areas.areas_overlap(areas, w.get("affected_areas"))
        if shared_areas:
            out.append({"kind": "area", "id": w.get("id"), "branch": w.get("branch"),
                        "owner_session": w.get("owner_session"), "detail": shared_areas})
        shared_contracts = sorted(contracts & set(w.get("shared_contracts") or []))
        if shared_contracts:
            out.append({"kind": "contract", "id": w.get("id"), "branch": w.get("branch"),
                        "owner_session": w.get("owner_session"), "detail": shared_contracts})
        if w.get("id") in deps:
            out.append({"kind": "dependency", "id": w.get("id"), "branch": w.get("branch"),
                        "owner_session": w.get("owner_session"), "detail": w.get("status")})
    return out


def find_cycle(active, entry):
    """Есть ли цикл в графе depends_on после добавления entry? Возвращает путь цикла или []."""
    graph = {w.get("id"): list(w.get("depends_on") or []) for w in active}
    graph[entry.get("id")] = list(entry.get("depends_on") or [])
    start = entry.get("id")
    # `stack`/`seen_paths` убраны ревизией 2026-08-11: остались от итеративной версии обхода,
    # текущий DFS ниже рекурсивный и ими не пользуется.
    # DFS с поиском возврата к уже посещённому в текущем пути
    def dfs(node, path):
        for nxt in graph.get(node, []):
            if nxt == start and len(path) >= 1:
                return path + [nxt]
            if nxt in path:
                return path[path.index(nxt):] + [nxt]
            if nxt in graph:
                r = dfs(nxt, path + [nxt])
                if r:
                    return r
        return None
    return dfs(start, [start]) or []


def _holder(c):
    """Кто держит: машина + сессия, если машина известна (опубликованная заявка её несёт)."""
    m = c.get("machine")
    return f"машина {m}, сессия {c.get('owner_session')}" if m else f"сессия {c.get('owner_session')}"


def _forecast_lines(confs):
    lines = []
    label = {"area": "зона", "contract": "контракт", "dependency": "зависимость"}
    for c in confs:
        k = c["kind"]
        if k == "dependency":
            lines.append(f"  ⚠ зависимость: '{c['id']}' ещё в работе (статус {c['detail']}, "
                         f"ветка {c['branch']}, {_holder(c)})")
        elif k == "same-work":
            lines.append(f"  ⚠ та же работа: '{c['id']}' уже держит другой ({_holder(c)}, "
                         f"ветка {c['branch']}) — это ДУБЛЬ, не начинайте второй раз")
        elif k == "branch":
            lines.append(f"  ⚠ ветка: '{c['detail']}' уже занята работой '{c['id']}' ({_holder(c)}) "
                         f"— два PR на одну ветку затирают друг друга")
        else:
            what = "зоны" if k == "area" else "контракты"
            lines.append(f"  ⚠ {label[k]}: пересечение с '{c['id']}' (ветка {c['branch']}, "
                         f"{_holder(c)}): общие {what} {', '.join(c['detail'])}")
    if confs:
        lines.append("  Варианты: дождаться · перенести зависимость · объединить задачи · "
                     "зафиксировать общий контракт · работать в разных слоях.")
    return lines


def register(path, wid, branch, areas, session, workitem=None, status="in-progress",
             depends=None, contracts=None, at=None, published=False, child_root=None,
             takeover=False, takeover_reason=None, owner_role=None, audience="technical"):
    if branch in (None, "", "main", "master"):
        print("ОШИБКА: работа не должна вестись в main/master — задайте ветку/worktree.")
        return 1
    if status not in STATUS:
        print(f"ОШИБКА: status '{status}' не в {sorted(STATUS)}")
        return 1
    if not areas:
        print("ОШИБКА: нужны affected_areas (основа conflict forecast).")
        return 1
    # #639: начальная роль-владелец, если задана, обязана быть из словаря модели (роль, не исполнитель).
    if owner_role is not None:
        ok, err = _handoff.validate_role(owner_role)
        if not ok:
            print(f"ОШИБКА: {err}")
            return 1
    # v3.0.12: весь read-modify-write под межпроцессной блокировкой (иначе конкурентная сессия могла
    # перезаписать нашу регистрацию — last-writer-wins — и concurrency-forecast увидел бы неполную карту).
    with _locked(path):
        data = load(path)
        # Заявка = кто (сессия) + где (машина) + когда (время) + что (ветка/зоны). Машина и время
        # добавлены 18.08.2026: без «где» и «когда» инцидент параллельных сессий не разобрать
        # (заявка #150: атрибуция была невозможна). Поля аддитивны — прежние записи без них валидны.
        # `owner_pid` — pid ЭТОГО процесса прогона, держащего заявку. По нему `holder_is_gone`
        # доказывает смерть session-заявки сразу (процесс мёртв), не дожидаясь возрастного порога.
        # Наружу поле НЕ уезжает (нет в PUBLISHED_FIELDS/COPY_CLAIM_FIELDS): pid значим только на
        # своей машине, где liveness и проверяется.
        entry = {"id": wid, "branch": branch, "status": status,
                 "affected_areas": list(areas), "owner_session": session,
                 "machine": _machine(), "started_at": at or _now_iso(),
                 "owner_pid": os.getpid()}
        # В КАКОЙ копии сидит держатель: без этого отказ называл бы «машину», то есть саму себя.
        # Наружу поле НЕ уезжает — `PUBLISHED_FIELDS` его не содержит.
        if child_root is not None:
            entry["worktree"] = str(Path(child_root).resolve())
        # #639: роль-владелец на записи Work (закрывает дрейф модель↔код — domain_model заявляет,
        # что Work несёт owner_role, а active_work его провайдит). Меняется потом через handoff_cmd.
        if owner_role is not None:
            entry["owner_role"] = owner_role
        if workitem:
            entry["workitem"] = workitem
        if depends:
            entry["depends_on"] = list(depends)
        if contracts:
            entry["shared_contracts"] = list(contracts)
        # цикл зависимостей — это ошибка, а не предупреждение
        cycle = find_cycle(data["active"], entry)
        if cycle:
            print(f"ОШИБКА: циклическая зависимость задач: {' -> '.join(cycle)}. "
                  f"Разорвите цикл (одна задача не может транзитивно зависеть от себя).")
            return 1
        # Пересечения ищем по ОБЩЕЙ карте: локальные + опубликованные заявки других машин. При
        # выключенной публикации team_view вернёт только локальные — чужих неоткуда взять, и честно.
        against = team_view(child_root, data["active"], published)
        # #137: сверяем карту с базой ДО прогноза — иначе влитая работа предупреждает о себе, и
        # человек либо переделывает готовое, либо перестаёт верить реестру (и тогда он не нужен).
        if child_root:
            against = reconcile_with_base(against, child_root)
        confs = classify(against, entry)
        # ОТКАЗ, А НЕ ПРЕДУПРЕЖДЕНИЕ (замер 18.08.2026, заявка потребителя #150). Прежде на «ту же
        # работу» и «ту же ветку» кит печатал `⚠ это ДУБЛЬ, не начинайте второй раз`, ВОЗВРАЩАЛ 0 и
        # ЗАТИРАЛ чужую заявку своей: держателя больше нет ни в реестре, ни в предупреждении, и
        # разобрать инцидент нечем. В поле это дало два PR на одну ветку, закрытый пустой дубль и
        # выброшенную половину работы по описанию.
        # Перенять заявку можно — но ЯВНО и с причиной: тогда прежний держатель остаётся записан в
        # `taken_over_from`, то есть атрибуция не теряется (заявка #150, следствие 3).
        blocking = [c for c in confs if c.get("kind") in ("same-work", "branch")]
        # Мёртвый процесс заявку не держит. Отпускаем НАЗЫВАЯ это: тихое снятие чужой заявки — то же
        # затирание, от которого работа и заводилась.
        _by_id = {w.get("id"): w for w in data["active"]}
        released = []
        for c in list(blocking):
            w = _by_id.get(c.get("id")) or {}
            if holder_is_gone(w, entry.get("machine")):
                blocking.remove(c)
                released.append(c)
        for c in released:
            print(f"ЗАЯВКА ОСВОБОЖДЕНА: держатель {c.get('owner_session')} на этой машине уже не "
                  f"существует (работа {c.get('id')}, ветка {c.get('branch')}) — заявка не держит.")
        if blocking and not takeover:
            for c in blocking:
                what = ("ту же работу" if c["kind"] == "same-work" else f"ту же ветку {c.get('detail')}")
                # «С какого момента» — часть отказа: по нему решают, ждать или перенимать.
                since = c.get("since") or "время начала не записано"
                where = f", рабочая копия {c['worktree']}" if c.get("worktree") else ""
                print(f"ОТКАЗ: не начинаю — {what} уже держит другой: сессия "
                      f"{c.get('owner_session') or '?'} на машине {c.get('machine') or '?'}{where} "
                      f"с {since} (работа {c.get('id')}, ветка {c.get('branch')}).")
            print("  " + reach_note(published))
            _cl = _copies_line(child_root)
            if _cl:
                print("  " + _cl)
            print("  что можно сделать: дождаться держателя; взять другую работу "
                  "(`ai-ops next` предложит незанятую); или ПЕРЕНЯТЬ заявку осознанно — "
                  "`active_work.py register … --takeover --takeover-reason \"почему\"`, "
                  "прежний держатель останется записан.")
            return 1
        if takeover and blocking:
            prev = blocking[0]
            entry["taken_over_from"] = {"owner_session": prev.get("owner_session"),
                                        "machine": prev.get("machine"),
                                        "at": entry["started_at"],
                                        "reason": (takeover_reason or "причина не названа")}
        data["active"] = [w for w in data["active"] if w.get("id") != wid] + [entry]
        save(path, data)
        # Опубликованная копия — отдельным файлом на работу, только при включённой публикации.
        if published:
            publish_claim(child_root, entry)
        # Носитель копий — ВСЕГДА, когда его есть где разместить: он не отправляет данные с машины,
        # поэтому флагом публикации не гатится (замер 20.08.2026).
        claim_to_copies(child_root, entry)
    # #708: на product-аудитории человеку не нужны wi-…/сессия/машина/ветка — это внутренние
    # идентификаторы координации. Суть одна: работа началась в отдельной копии, main не тронут.
    # Заметку о досягаемости в соло-случае опускаем как шум про выключенную фичу. Прогноз-пересечения
    # (реальный сигнал) и публикацию (данные УХОДЯТ с машины) — сохраняем на ЛЮБОЙ аудитории:
    # `if published:` намеренно ОДИН на оба пути (это же место стережёт mutation-проба
    # publish-off-writes-nothing — дубль анкера сделал бы её неоднозначной).
    if audience == "product":
        print("  Начал работу в отдельной копии проекта — main не трогаю.")
    else:
        print(f"ACTIVE-WORK: зарегистрирована работа '{wid}' "
              f"(ветка {branch}, сессия {session}, машина {entry['machine']}).")
    for line in _forecast_lines(confs):
        print(line)
    if audience != "product":
        # Честная фраза о досягаемости — ВСЕГДА, а не только при пересечениях: иначе «пересечений нет»
        # на локальном реестре читается как «команда свободна», хотя других машин кит не видит.
        print("  " + reach_note(published))
        _cl = _copies_line(child_root)
        if _cl:
            print("  " + _cl)
    if published:
        # Условие 4 гибридного решения: включённая публикация ОТПРАВЛЯЕТ данные — назвать какие,
        # в момент отправки, а не только в общем пояснении.
        print("  опубликовано в .ai/claims/: " + ", ".join(PUBLISHED_FIELDS)
              + " — содержимое файлов не уходит.")
    return 0


def list_cmd(path, as_json=False, published=False, child_root=None):
    data = load(path)
    team = team_view(child_root, data["active"], published)
    if as_json:
        data = dict(data, published=published, team=team)  # досягаемость и общая карта — и в JSON
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    act = [w for w in team if w.get("status") != "done"]
    if not act:
        print("ACTIVE-WORK: активных работ нет.")
        print("  " + reach_note(published))
        _cl = _copies_line(child_root)
        if _cl:
            print("  " + _cl)
        return 0
    print(f"ACTIVE-WORK: {len(act)} активных работ:")
    for w in act:
        extra = ""
        if w.get("depends_on"):
            extra += f" зависит от: {', '.join(w['depends_on'])};"
        if w.get("shared_contracts"):
            extra += f" контракты: {', '.join(w['shared_contracts'])};"
        print(f"  - {w.get('id')} [{w.get('status')}] ветка {w.get('branch')} "
              f"зоны: {', '.join(w.get('affected_areas') or [])} (сессия {w.get('owner_session')}){extra}")
    print("  " + reach_note(published))
    _cl = _copies_line(child_root)
    if _cl:
        print("  " + _cl)
    return 0


def check_cmd(path, areas, depends=None, contracts=None, exclude_id=None, as_json=False,
              branch=None, child_root=None, published=False):
    data = load(path)
    probe = {"id": exclude_id, "affected_areas": list(areas), "branch": branch,
             "machine": _machine(), "owner_session": None,
             "depends_on": list(depends or []), "shared_contracts": list(contracts or [])}
    against = team_view(child_root, data["active"], published)
    if child_root:   # #137: прогноз считается по СВЕРЕННОЙ карте и здесь — это третий путь ответа
        against = reconcile_with_base(against, child_root)
    confs = classify(against, probe)
    if as_json:
        print(json.dumps({"schema_version": 1, "kind": "conflict-forecast",
                          "areas": list(areas), "conflicts": confs, "published": published},
                         ensure_ascii=False, indent=2))
        return 0
    _cl = _copies_line(child_root)
    if not confs:
        print(f"CONFLICT-FORECAST: пересечений по зонам {', '.join(areas)} нет — можно стартовать.")
        print("  " + reach_note(published))
        if _cl:
            print("  " + _cl)
        return 0
    print("CONFLICT-FORECAST: возможны пересечения:")
    for line in _forecast_lines(confs):
        print(line)
    print("  " + reach_note(published))
    if _cl:
        print("  " + _cl)
    return 0


def finish_cmd(path, wid, status="done", reason=None, child_root=None, published=False,
               audience="technical"):
    """Снять работу с учёта. status — из STATUS; 'done' ТОЛЬКО когда работа действительно закончена.

    v3.28.x (F-012, находка живой квалификации на niti): прогон помечал работу `done` независимо
    от исхода — при NOT_READY, при исключении провайдера и даже при Ctrl-C. `ai-ops status` после
    этого показывал пустоту, хотя работа не сделана: реестр активной работы врал ровно там, где
    он единственный источник правды о незавершённом."""
    if status not in STATUS:
        print(f"ОШИБКА: status '{status}' не в {sorted(STATUS)}")
        return 1
    # v3.0.12: read-modify-write под блокировкой (симметрично register — без гонки на общем реестре)
    with _locked(path):
        data = load(path)
        found = False
        for w in data["active"]:
            if w.get("id") == wid:
                w["status"] = status
                if reason:
                    # #137: причина ДАТИРУЕТСЯ. Без даты «код не написан — правок 0» читается как
                    # утверждение о настоящем, хотя относится к одному давнему прогону.
                    w["status_reason"] = reason
                    w["status_reason_at"] = _now_iso()
                found = True
        if not found:
            print(f"ACTIVE-WORK: работа '{wid}' не найдена.")
            return 1
        entry = next((w for w in data["active"] if w.get("id") == wid), None)
        save(path, data)
        if entry is not None:
            _m = entry.get("machine") or _machine()
            # #695: носитель копий координирует ТОЛЬКО идущий прогон — снимаем на ВЫХОДЕ любым исходом
            # (done/blocked/прерван). Заявка там без `owner_pid` → `holder_is_gone` судила её лишь по
            # возрасту (12ч) и блокировала следующую команду (замер 01.09); локальный реестр сохраняется.
            withdraw_claim_from_copies(child_root, _m, wid)
            if status == "done":                      # опубликованную (наружу) — только на завершении
                unpublish_claim(child_root, _m, wid)
    if audience == "product":
        # #708: на product человеку не нужны wi-…/жаргон «ACTIVE-WORK». Итог прогона он уже видит в
        # отчёте; здесь важна лишь ПРИЧИНА остановки, если она есть (реальный сигнал). Успех без
        # причины — молчим, чтобы не дублировать «готово» отчёта.
        _human = {"blocked": "приостановлена", "done": "завершена"}.get(status, status)
        if reason:
            print(f"  Работа {_human}: {reason}.")
        return 0
    print(f"ACTIVE-WORK: работа '{wid}' помечена {status}"
          f"{' — ' + reason if reason else ''}.")
    return 0


def handoff_cmd(path, wid, to_role, reason, session, at=None):
    """Передать работу другой роли-владельцу — named-переход owner_role (#639). Роль→роль.

    Тонкая проводка: locked read-modify-write реестра, сам переход считает `role_handoff.apply_handoff`
    (валидация роли по словарю модели, запись перехода с брифом, атрибуция прежнего владельца). Логика
    вынесена в донор `lifecycle/role_handoff.py`, здесь — только чтение/запись общего реестра.
    """
    # v3.0.12: под блокировкой (симметрично register/finish — общий реестр, чужая сессия параллельна).
    with _locked(path):
        data = load(path)
        entry = next((w for w in data["active"] if w.get("id") == wid), None)
        if entry is None:
            print(f"ACTIVE-WORK: работа '{wid}' не найдена.")
            return 1
        new_entry, err = _handoff.apply_handoff(entry, to_role, reason, session, at=at)
        if err:
            print(f"ОШИБКА: {err}")
            return 1
        entry.update(new_entry)                      # тот же объект в data["active"] — сохранится ниже
        save(path, data)
    frm = new_entry["handoffs"][-1]["from"]
    print(f"ACTIVE-WORK: работа '{wid}' передана {frm or '—'} → {to_role} ({reason}).")
    return 0


def _split(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def main(argv):
    ap = argparse.ArgumentParser(prog="active_work.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("register")
    r.add_argument("file"); r.add_argument("id")
    r.add_argument("--branch", required=True)
    r.add_argument("--areas", required=True, help="через запятую")
    r.add_argument("--session", required=True)
    r.add_argument("--workitem")
    r.add_argument("--status", default="in-progress")
    r.add_argument("--depends", help="id задач-зависимостей через запятую")
    r.add_argument("--contracts", help="пути общих контрактов через запятую")
    r.add_argument("--at")
    r.add_argument("--repo", help="корень репозитория для чтения team_coordination (по умолчанию cwd)")
    # Перенять чужую заявку можно только СЛОВАМИ, а не молчанием: флаг + причина. Прежний держатель
    # записывается в заявку, иначе перенос выглядел бы как «работу никто не держал».
    r.add_argument("--owner-role", help="начальная роль-владелец из словаря модели (#639)")
    r.add_argument("--takeover", action="store_true",
                   help="перенять заявку, которую держит другая сессия (осознанно)")
    r.add_argument("--takeover-reason", help="почему заявка перенимается (уходит в запись)")

    l = sub.add_parser("list")
    l.add_argument("file"); l.add_argument("--json", action="store_true")
    l.add_argument("--repo", help="корень репозитория для чтения team_coordination (по умолчанию cwd)")

    c = sub.add_parser("check")
    c.add_argument("file"); c.add_argument("--areas", required=True)
    c.add_argument("--depends"); c.add_argument("--contracts")
    c.add_argument("--exclude"); c.add_argument("--json", action="store_true")
    c.add_argument("--branch"); c.add_argument("--repo")

    f = sub.add_parser("finish")
    f.add_argument("file"); f.add_argument("id")
    f.add_argument("--status", default="done"); f.add_argument("--repo")

    h = sub.add_parser("handoff", help="передать работу другой роли-владельцу (#639)")
    h.add_argument("file"); h.add_argument("id")
    h.add_argument("--to-role", required=True, help="роль-владелец, которой передаётся работа")
    h.add_argument("--reason", required=True, help="бриф для следующего владельца: что передаётся")
    h.add_argument("--session", required=True)
    h.add_argument("--at")

    a = ap.parse_args(argv)
    if a.cmd == "register":
        repo = getattr(a, "repo", None) or Path.cwd()
        pub = publication_enabled(repo)
        return register(Path(a.file), a.id, a.branch, _split(a.areas), a.session,
                        a.workitem, a.status, _split(a.depends), _split(a.contracts), a.at,
                        published=pub, child_root=repo,
                        takeover=getattr(a, "takeover", False),
                        takeover_reason=getattr(a, "takeover_reason", None),
                        owner_role=getattr(a, "owner_role", None))
    if a.cmd == "list":
        repo = getattr(a, "repo", None) or Path.cwd()
        return list_cmd(Path(a.file), a.json, published=publication_enabled(repo), child_root=repo)
    if a.cmd == "check":
        repo = getattr(a, "repo", None) or Path.cwd()
        return check_cmd(Path(a.file), _split(a.areas), _split(a.depends),
                         _split(a.contracts), a.exclude, a.json,
                         branch=getattr(a, "branch", None), child_root=repo,
                         published=publication_enabled(repo))
    if a.cmd == "finish":
        repo = getattr(a, "repo", None) or Path.cwd()
        return finish_cmd(Path(a.file), a.id, status=getattr(a, "status", "done"),
                          child_root=repo, published=publication_enabled(repo))
    if a.cmd == "handoff":
        return handoff_cmd(Path(a.file), a.id, a.to_role, a.reason, a.session,
                           at=getattr(a, "at", None))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
