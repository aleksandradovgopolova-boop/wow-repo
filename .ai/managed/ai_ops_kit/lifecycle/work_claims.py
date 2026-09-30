#!/usr/bin/env python3
"""Носитель заявок active-work: транспорты и общая карта «кто что держит».

Сателлит `lifecycle/active_work.py` (вынесен из монолита без изменения поведения). Здесь живёт всё,
что переносит заявку ЗА пределы одного рабочего дерева и собирает из этих источников одну карту:

  - публикация в `.ai/claims/` — команда через git (только при явном включении публикации);
  - носитель `.git/ai-ops/claims/` — все рабочие копии одного репозитория на этой машине;
  - `team_view` — сложение трёх источников с одним дедупом по паре (машина, работа).

Локальный реестр, команды и классификацию пересечений держит `active_work`; он же ре-экспортирует
эти имена, поэтому `active_work.<имя>` и прежние импорты продолжают работать.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from ai_ops_kit.shared import gitio                    # единый вход к git с таймаутом (см. shared/gitio)


# ── публикация заявки: файл на работу в git (ep-2026-08-18-published-carrier-file-per-work) ──────
CLAIMS_DIR_REL = Path(".ai") / "claims"

# ТОЛЬКО эти поля уезжают при публикации (условие 4 гибридного решения). Содержимое файлов и
# что-либо сверх списка сюда не попадает — публикация это явная отправка данных, а не «всё, что есть».
PUBLISHED_FIELDS = ("id", "branch", "machine", "owner_session", "started_at", "status")


def _claim_slug(machine: str, wid: str) -> str:
    """Имя файла заявки. Файл на ПАРУ (машина, работа) — потому не пересекается с чужим (в отличие
    от одного общего файла, отклонённого решением). Небезопасные для пути символы заменяются."""
    def safe(s):
        return "".join(c if (c.isalnum() or c in "-_.") else "-" for c in str(s or "unknown"))
    return f"{safe(machine)}__{safe(wid)}.yaml"


def publish_claim(child_root, entry: dict) -> Path | None:
    """Записать опубликованную копию заявки отдельным отслеживаемым файлом. -> путь или None.

    Только объявленные поля (`PUBLISHED_FIELDS`). Идемпотентно: своя пара (машина, работа)
    перезаписывается, чужие не трогаются. Каталог `.ai/claims/` НЕ в .gitignore — он и есть носитель,
    который доезжает к команде через git."""
    if child_root is None:
        return None
    d = Path(child_root) / CLAIMS_DIR_REL
    d.mkdir(parents=True, exist_ok=True)
    payload = {k: entry.get(k) for k in PUBLISHED_FIELDS if entry.get(k) is not None}
    payload["schema_version"] = 1
    payload["kind"] = "published-claim"
    p = d / _claim_slug(entry.get("machine"), entry.get("id"))
    p.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=True), encoding="utf-8")
    return p


def unpublish_claim(child_root, machine: str, wid: str) -> bool:
    """Снять опубликованную заявку (работа закрыта). -> True если файл был и удалён."""
    if child_root is None:
        return False
    p = Path(child_root) / CLAIMS_DIR_REL / _claim_slug(machine, wid)
    if p.is_file():
        p.unlink()
        return True
    return False


def load_published_claims(child_root, exclude_machine: str | None = None) -> list:
    """Прочитать заявки, опубликованные (в т.ч. другими машинами и доехавшие через git). -> список.

    Битый файл заявки ПРОПУСКАЕТСЯ, а не роняет чтение: чужая недокачанная заявка не должна делать
    невидимой всю карту (то же соображение fail-safe, что у локального реестра — но здесь мягче:
    источник внешний). exclude_machine — чтобы не считать свою же опубликованную копию дважды."""
    out = []
    if child_root is None:
        return out
    d = Path(child_root) / CLAIMS_DIR_REL
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.yaml")):
        try:
            rec = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError):
            continue
        if not isinstance(rec, dict) or not rec.get("id"):
            continue
        if exclude_machine and rec.get("machine") == exclude_machine:
            continue
        rec["_published"] = True   # пометка происхождения: это заявка с носителя, не локальная
        out.append(rec)
    return out


# ── вторая досягаемость заявки: рабочие копии ОДНОГО репозитория ─────────────────────────────────
#
# ЗАМЕР 20.08.2026 на двух копиях одного репозитория, ДО правки: копия A регистрирует работу — в
# копии B `next` предлагает ТУ ЖЕ работу, а `register` возвращает 0 без отказа. Реестр
# `.ai/runtime/active-work.yaml` лежит ВНУТРИ рабочего дерева: у каждого worktree свой, и
# `.gitignore` его скрывает. `shared_registry_path` (12.08.2026) написана против этого и не
# вызывалась нигде, кроме тестов — «механизм есть, вызова нет».
#
# РЕЕСТР НЕ ПЕРЕЕЗЖАЕТ: его путь объявлен в манифесте, переезд был бы breaking change по раскладке
# `.ai/` (AGENTS.md). Подключён НОСИТЕЛЬ — тот же формат заявки во второй транспорт; оба сходятся в
# `team_view`, поэтому третьего источника «что идёт» не появляется.
#
# НЕ ГАТИТСЯ `team_coordination.publish`: флаг стоит против ОТПРАВКИ наружу
# (`ep-2026-08-18-claim-medium-hybrid`), а этот носитель лежит внутри `.git/` одной машины, не
# коммитится и в историю не попадает. Гатить его флагом отправки значило бы выключать координацию
# там, где отправки нет. Подробности и протокол — `docs/parallel-sessions.md`.

COPIES_CLAIMS_REL = Path("ai-ops") / "claims"

# На одно поле больше: `worktree` — в какой копии держатель. Без него отказ на одной машине звучит
# «держит сессия X на машине Y», где Y — та же машина. В `PUBLISHED_FIELDS` его нет: абсолютный путь
# другим машинам не уезжает.
COPY_CLAIM_FIELDS = PUBLISHED_FIELDS + ("worktree",)


def _git_common_dir(start=None):
    """Каталог `.git`, ОБЩИЙ для всех рабочих копий репозитория. -> Path или None.

    None — «не измерили» (не git, git не установлен), и вызывающие говорят это, а не «заявок нет»."""
    # Форма с `cwd=`, а не `-C`: единый вход к git с таймаутом через gitio.run (см. shared/gitio).
    cwd = str(start or Path.cwd())
    try:
        rc, out, _ = gitio.run(["rev-parse", "--git-common-dir"], cwd=cwd)
    except OSError:
        return None
    if rc != 0:
        return None
    common = Path(out)
    if not str(common):
        return None
    # `--git-common-dir` из корня репозитория отдаёт ОТНОСИТЕЛЬНЫЙ `.git` — разрешаем от cwd, иначе
    # путь из разных worktree указывал бы в разные места, то есть ровно на тот дефект, против
    # которого носитель и делается.
    if not common.is_absolute():
        common = (Path(cwd) / common).resolve()
    return common


def copies_claims_dir(start=None):
    """Каталог заявок, общий для всех рабочих копий одного репозитория. -> Path или None.

    КОРЕНЬ ОБЯЗАТЕЛЕН, `None` НЕ ЗНАЧИТ «текущий каталог» (найдено своим прогоном 20.08.2026: cwd по
    умолчанию заставлял вызовы без корня координировать тот репозиторий, где стоял процесс, — то
    есть называть держателя не той работы)."""
    if start is None:
        return None
    common = _git_common_dir(start)
    return None if common is None else common / COPIES_CLAIMS_REL


def working_copies(start=None):
    """Сколько рабочих копий у этого репозитория ЗНАЕТ git. -> int или None (не измерено).

    Считается по `<git-common-dir>/worktrees` плюс основная. Это ЗАМЕР git, а не факт о диске:
    удалённую без `git worktree prune` копию git ещё помнит. Корень обязателен — см.
    `copies_claims_dir`."""
    common = _git_common_dir(start) if start is not None else None
    if common is None:
        return None
    d = common / "worktrees"
    try:
        linked = len([x for x in d.iterdir() if x.is_dir()]) if d.is_dir() else 0
    except OSError:
        return None
    return linked + 1


def copies_reach_note(copies) -> str:
    """Строка о досягаемости носителя копий. «Не измерили» — не «соседних заявок нет»."""
    if copies is None:
        return ("Рабочие копии этого репозитория не измерены (git недоступен): заявки соседних копий "
                "здесь не видны, и это «не знаю», а не «их нет».")
    if copies <= 1:
        return "У репозитория одна рабочая копия — соседних заявок здесь быть не может."
    return (f"Видны заявки всех рабочих копий этого репозитория на этой машине (копий: {copies}) — "
            f"носитель лежит в общем каталоге git, коммит и push для этого не нужны.")


def _copies_line(start):
    """Строка о носителе копий человеку — или None, когда она ничего не добавляет.

    Печатаем только при НЕСКОЛЬКИХ копиях: при одной досягаемость совпадает с локальной. «Не
    измерили» молчит не как «ничего нет» — `reach_note` рядом уже говорит про одну машину."""
    n = working_copies(start)
    return copies_reach_note(n) if (n is not None and n > 1) else None


def claim_to_copies(start, entry: dict):
    """Положить заявку на носитель копий. -> путь или None. Идемпотентно по паре (машина, работа),
    как и публикация; чужие файлы не трогаются."""
    d = copies_claims_dir(start)
    if d is None or yaml is None:
        return None
    payload = {k: entry.get(k) for k in COPY_CLAIM_FIELDS if entry.get(k) is not None}
    payload["schema_version"] = 1
    payload["kind"] = "copy-claim"
    try:
        d.mkdir(parents=True, exist_ok=True)
        p = d / _claim_slug(entry.get("machine"), entry.get("id"))
        p.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=True), encoding="utf-8")
    except OSError:
        return None      # носитель не записался: координация между копиями беднее, но регистрация цела
    return p


def withdraw_claim_from_copies(start, machine: str, wid: str) -> bool:
    """Снять свою заявку с носителя копий (работа закрыта). -> True если файл был и удалён."""
    d = copies_claims_dir(start)
    if d is None:
        return False
    p = d / _claim_slug(machine, wid)
    try:
        if p.is_file():
            p.unlink()
            return True
    except OSError:
        return False
    return False


def load_copy_claims(start=None) -> list:
    """Заявки соседних рабочих копий этого репозитория. -> список записей.

    Битый файл ПРОПУСКАЕТСЯ: недописанная заявка соседа не делает невидимой всю карту."""
    out = []
    d = copies_claims_dir(start)
    if d is None or yaml is None or not d.is_dir():
        return out
    for p in sorted(d.glob("*.yaml")):
        try:
            rec = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError):
            continue
        if not isinstance(rec, dict) or not rec.get("id"):
            continue
        # РАБОЧЕЙ КОПИИ БОЛЬШЕ НЕТ — держать работу некому. `git worktree remove` заявку с носителя
        # не снимает, и без этой проверки удалённая копия держала бы работу вечно во всех остальных
        # (#137, «список страшилок»). ГРАНИЦА: это проверка КОПИИ, а не сессии — живость `pid:`
        # смотрит `holder_is_gone`, измеренную личность рантайма не смотрит никто. Поля нет ->
        # запись остаётся: «не знаю, где держат» ≠ «не держат».
        wt = rec.get("worktree")
        if wt and not Path(wt).is_dir():
            continue
        rec["_from_copy"] = True    # пометка происхождения: заявка с носителя копий, не локальная
        out.append(rec)
    return out


def team_view(child_root, local_active: list, published: bool) -> list:
    """Общая карта «кто что держит»: заявки этого дерева + заявки соседних рабочих копий + при
    включённой публикации заявки других машин, которых нет локально. При выключенной публикации
    чужих машин в карте нет — честно, их и неоткуда взять. -> список записей.

    ДЕДУП ПО ПАРЕ (машина, работа), А НЕ ПО ИМЕНИ МАШИНЫ. Замер 18.08.2026 на живом прогоне: два
    клона на ОДНОМ физическом хосте имеют одинаковое имя машины, и дедуп «исключить свою машину»
    прятал заявку соседнего клона целиком. Своя опубликованная копия — это ровно (машина, id) моих
    локальных заявок; её и вычитаем, а чужие работы того же хоста остаются видны.

    ТРИ ИСТОЧНИКА, ОДНА КАРТА (20.08.2026): локальный реестр — одно рабочее дерево; носитель
    `.git/ai-ops/claims/` — весь репозиторий на этой машине (читается ВСЕГДА, он ничего не
    отправляет); `.ai/claims/` — команда через git (только при публикации). Один формат заявки и
    один дедуп по паре, поэтому третьего места, где живёт «что идёт», не появляется."""
    view = list(local_active)
    seen = {(w.get("machine"), w.get("id")) for w in view}
    sources = [load_copy_claims(child_root)]
    if published:   # заявки других МАШИН — только по явному включению публикации
        sources.append(load_published_claims(child_root))
    for src in sources:
        for r in src:
            key = (r.get("machine"), r.get("id"))
            if key in seen:
                continue    # моя же заявка, приехавшая вторым транспортом — не второй держатель
            seen.add(key)
            view.append(r)
    return view
