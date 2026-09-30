#!/usr/bin/env python3
"""Модель delivery-plan: словарь схемы, аксессоры и политика целей/заморозки.

Вынесено из `delivery_plan.py` (чистый рефакторинг, поведение байт-в-байт). Здесь то, что
ОПИСЫВАЕТ план и читает его: словарь допустимых значений, доступ к работам и целям, живость цели
и политика заморозки умений. Модуль-лист — он НЕ импортирует `delivery_plan`, поэтому и
`delivery_plan`, и `plan_validate` берут имена отсюда без риска цикла импорта.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml


PLAN_REL = "planning/plan.yaml"
KIND = "delivery-plan"
HISTORY_REL = "history/plan-history.yaml"
HISTORY_KIND = "delivery-plan-history"

DECLARABLE = ("todo", "in_progress", "waiting_on_owner", "done", "dropped")
DERIVED = ("ready", "blocked", "waiting")
VALUE = ("high", "medium", "low")
# «Механизм готов — ждём НАЗВАННОЕ действие владельца». Замер 04.09.2026: две работы простояли
# `in_progress` ~9 дней, хотя код давно слит и они ждут одного шага в настройке GitHub («Require
# merge queue»). `in_progress` лгал дважды: `status`/`next` считали их идущими, а сверка «идёт по
# плану, но нет прогона» — брошенными. Ни то, ни другое: работа не движется и не брошена, она ждёт
# человека. Статус ОБЪЯВЛЯЕМЫЙ (факт, который код вывести не может — что именно у владельца в
# очереди), живёт в активном плане и ОБЯЗАН назвать ожидаемое действие в поле `waiting_on` — иначе
# это фантомное состояние: план выглядит осмысленным, а чего он ждёт — нигде.
OWNER_WAIT_STATUS = "waiting_on_owner"
OWNER_WAIT_KEY = "waiting_on"
# АКТИВНЫЙ план содержит только незакрытую работу (`plan-as-control-plane`, 2026-08-14). Закрытая
# уезжает в `history/plan-history.yaml`. Повод — замер: план кита стал одновременно планом, бэклогом,
# журналом расследований и отчётом квалификации, 20 из 25 работ были `done`, и чтобы ответить «что
# идёт сейчас», приходилось читать разбор давно закрытых дефектов. Управляющий файл, в котором
# управление занимает пятую часть, управляющим быть перестаёт.
ACTIVE_DECLARABLE = ("todo", "in_progress", "waiting_on_owner")
CLOSED_DECLARABLE = ("done", "dropped")
# ИМЕНОВАННЫЙ ТЕКУЩИЙ MILESTONE — первоклассная запись плана (2026-09-14). Уровень МЕЖДУ
# направлением (goals/ROADMAP) и работой: какой названный результат кит доводит ПРЯМО СЕЙЧАС.
# Прежде «текущий milestone» существовал только как ПРОКСИ — первый пункт горизонта «Сейчас» из
# ROADMAP.md, который читал паспорт. Теперь milestone объявляется здесь; если ключа нет — паспорт
# честно откатывается к прежнему прокси. Статус берём из того же объявляемого набора, что и работа
# (`DECLARABLE`): вторую правду о состоянии заводить не нужно.
MILESTONE_KEY = "current_milestone"
MILESTONE_STATUSES = DECLARABLE
# Поля-связки: чем работа привязана к реальности. Не обязательны, но их СМЫСЛ проверяется ниже.
LINK_KEYS = ("pr", "branch", "commit", "evidence", "decision", "finding")

# Поля, называющие КОНКРЕТНОГО исполнителя. Запрещены не слова, а ПОЛЯ: «OpenAI» в заголовке
# работы — законная часть продукта, а `runtime: claude-code` в плане — привязка плана к вендору.
FORBIDDEN_ITEM_KEYS = ("runtime", "model", "provider", "executor", "assignee", "agent")


# ИД-ЗАГЛУШКИ ЦЕЛЕЙ ИЗ BOOTSTRAP-ЧЕРНОВИКА. Установщик рисует черновой план с целями `goal-id-1`/
# `goal-id-2` — это внутренние плейсхолдеры, а НЕ имя направления. Пока человек не задал имя
# (`ai-ops model`), такой id нельзя echo'ить владельцу: «начать двигать направление «goal-id-1»» —
# утечка трубопровода в лицо человека (репетиция P0 №3, 2026-09-16). Распознаём и НАБОР заглушек
# (is_template), и одиночный id (для отображения через `goal_display_name`).
PLACEHOLDER_GOAL_IDS = frozenset({"goal-id-1", "goal-id-2"})
_PLACEHOLDER_GOAL_RE = re.compile(r"^goal-id-\d+$")
# Как назвать безымянное направление ВЛАДЕЛЬЦУ вместо сырого id. Без команды в тексте (#1204):
# человеку — что сделать словами, команду зовёт помощник, а не владелец.
UNNAMED_GOAL_LABEL = "безымянное направление (у него пока нет имени — назови его своими словами)"


def is_placeholder_goal(gid) -> bool:
    """id цели — это заглушка bootstrap-черновика (`goal-id-N`), а не заданное человеком имя?"""
    return bool(gid) and bool(_PLACEHOLDER_GOAL_RE.match(str(gid).strip()))


def goal_display_name(gid, title=None) -> str:
    """Как назвать цель ВЛАДЕЛЬЦУ. Пока у неё нет человеческого имени (id-заглушка `goal-id-N` или
    пустой title), не показываем сырой id — называем «безымянным направлением» и подсказываем, чем
    это чинится. Иначе — человеческий заголовок, а при его отсутствии осмысленный id как есть.
    """
    t = (title or "").strip()
    if t and not is_placeholder_goal(t):
        return t
    g = str(gid).strip() if gid is not None else ""
    if not g or is_placeholder_goal(g):
        return UNNAMED_GOAL_LABEL
    return g


def is_template(plan) -> bool:
    """Это ещё заготовка кита, а не план продукта?

    Установщик кладёт шаблон в репозиторий, и без маркера кит уверенно советовал работу из своего
    примера («Спроектировать pipeline»), рапортовал «работа 1/5» и получал `✓` в doctor — выдавал
    догадку за факт на чужом продукте. Маркер `template: true` снимает человек, когда впишет своё.
    Заодно распознаём незаполненные id-заглушки: файл могли скопировать руками, потеряв маркер.
    """
    if not plan:
        return False
    if plan.get("template") is True:
        return True
    gids = {g.get("id") for g in (plan.get("goals") or []) if isinstance(g, dict)}
    return bool(gids) and gids <= PLACEHOLDER_GOAL_IDS


def items(plan) -> list:
    return [w for w in (plan or {}).get("work") or [] if isinstance(w, dict)]


def goals(plan) -> list:
    """Цели в объявленном порядке. Порядок — это приоритет (ranking его читает)."""
    gs = (plan or {}).get("goals") or []
    return [g for g in gs if isinstance(g, dict) and g.get("id")]


def current_milestone(plan) -> dict | None:
    """Именованный текущий milestone плана (первоклассная запись). None, если он не объявлен.

    Один ключ `current_milestone`, а не список `milestones` с указателем «который текущий»:
    указатель — вторая правда о том же вопросе (какой из них сейчас), а текущий milestone у
    репозитория ровно один. Отсутствие ключа — законное состояние: паспорт откатится к прокси из
    ROADMAP «Сейчас».
    """
    m = (plan or {}).get(MILESTONE_KEY)
    return m if isinstance(m, dict) else None


# Живость цели -> насколько её работы вправе идти первыми. Порядок в файле остаётся приоритетом,
# но ТОЛЬКО СРЕДИ ЦЕЛЕЙ ОДНОЙ ЖИВОСТИ.
#
# ЗАМЕР 19.08.2026. Первой целью плана стоит `real-project-qualification` со `status: achieved`, и
# `next` три раза подряд советовал её работу — обходя P1-находки живого прогона, заведённые в тот
# же день. Причина: приоритет считался ИНДЕКСОМ цели в файле, а `status` читался только для показа
# (`where_are_we`). Достигнутая цель — это не «самое важное направление», это направление, которое
# больше никуда не ведёт.
#
# РАБОТА ПОД ДОСТИГНУТОЙ ЦЕЛЬЮ НЕ ИСЧЕЗАЕТ, а опускается: исчезновение было бы тем же молчанием,
# которое запрещено заморозке умений («замороженное не исчезает молча»). Она остаётся кандидатом и
# получает вслух названную причину низкого приоритета.
GOAL_STATUSES = ("active", "paused", "achieved")
_GOAL_LIVENESS = {"active": 0, "paused": 1, "achieved": 2}


def goal_priority(plan) -> dict:
    """Приоритет целей: живые в объявленном порядке, потом приостановленные, потом достигнутые.

    Неизвестный статус считается живым НАМЕРЕННО: молча опустить цель из-за опечатки значило бы
    переставить весь план и никому об этом не сказать. Опечатку ловит `validate` ошибкой — там она
    видна, здесь была бы невидима.
    """
    gs = list(goals(plan))
    order = sorted(range(len(gs)),
                   key=lambda i: (_GOAL_LIVENESS.get(gs[i].get("status") or "active", 0), i))
    return {gs[i]["id"]: rank for rank, i in enumerate(order)}


def goal_is_live(plan, gid) -> bool:
    """Ведёт ли цель куда-то ещё. Достигнутая и приостановленная — нет."""
    for g in goals(plan):
        if g.get("id") == gid:
            return _GOAL_LIVENESS.get(g.get("status") or "active", 0) == 0
    return True                      # цели нет в плане — это ловит validate, не молчим здесь


FREEZE_DECISION = "ep-2026-08-17-capability-freeze-until-second-brownfield"
FREEZE_GOAL = "second-real-brownfield"
FREEZE_OUTCOME = "owner_reaches_verified_pr_without_patching_the_kit"
FREEZE_RELATIONS = ("run_condition", "extension")
# СНЯТИЕ РЕШЕНИЕМ — ОТДЕЛЬНОЕ ПОЛЕ, А НЕ ИСХОД (19.08.2026, разбор плана после аудита).
#
# Заморозка снималась единственным способом — исходом, ставшим верным; так и задумано, чтобы
# «разморозить вручную технически нечем». 19.08 владелец решил снять её ДО достижения условия
# (`ep-2026-08-19-freeze-lifted`), и снятие записали единственным доступным способом: поставили
# `owner_reaches_verified_pr_without_patching_the_kit: true`. Комментарий рядом честно говорил,
# что условие не достигнуто, — но ЧИТАЕТ-ТО КОД ЗНАЧЕНИЕ, а не комментарий.
#
# ЦЕНА БЫЛА НЕ В ЗАМОРОЗКЕ. Этот же исход стережёт канал `stable`: пока он `true`, и `next`, и
# любая проверка считают второй brownfield пройденным, а `verified PR = 0` и `field_evidence`
# пуст. То есть решение о процессе молча переписало ФАКТ о продукте.
#
# Развязка: снятие объявляется своим полем на цели и обязано ссылаться на существующее решение
# (проверяет `validate`), а исход остаётся тем, что он есть. Оба намерения сохранены и видны
# порознь: заморозка снята явно, гейт stable честен.
FREEZE_LIFT_FIELD = "freeze_lifted_by"


def freeze_state(plan) -> dict:
    """Держится ли заморозка новых умений. -> {"frozen": bool, "reason": str, ...}.

    Решение владельца `ep-2026-08-17-capability-freeze-until-second-brownfield`: новое умение не
    принимается, пока исход `owner_reaches_verified_pr_without_patching_the_kit` не стал верным.

    СНЯТИЕ — НЕ ДАТА И НЕ ФЛАГ, а тот самый исход: он читается из цели, поэтому «разморозить
    вручную» технически нечем. Если цели или исхода в плане нет — это НЕ «заморозки нет»: считаем
    заморозку держащейся и говорим, что состояние не прочиталось (fail-closed: иначе достаточно
    удалить строку, чтобы правило исчезло).
    """
    g = next((x for x in goals(plan) if x.get("id") == FREEZE_GOAL), None)
    # ПРАВИЛО ОТНОСИТСЯ К РЕПОЗИТОРИЮ КИТА, А НЕ К ПРОДУКТАМ ДОЧЕК. Заморозка — внутреннее решение о
    # развитии кита; требовать её классификацию от плана чужого продукта значило бы экспортировать
    # свою политику в чужой CI. Поймано первым же полным прогоном: обязательный признак уронил 12
    # проверок, включая планы, которые кит СОЗДАЁТ дочке (`bootstrap`) и шаблон плана.
    # Признак применимости — наличие САМОЙ ЦЕЛИ заморозки в плане: она есть только там, где решение
    # принято.
    if not g:
        return {"frozen": False, "applies": False, "readable": True, "decision": FREEZE_DECISION,
                "reason": f"в этом плане нет цели '{FREEZE_GOAL}' — заморозка умений относится к "
                          f"репозиторию кита, а не к плану продукта"}
    lifted = str(g.get(FREEZE_LIFT_FIELD) or "").strip()
    if lifted:
        # Снято решением человека, а не достижением исхода. Исход при этом НЕ трогаем и
        # возвращаем как есть: «правило больше не держит» и «условие выполнено» — разные факты,
        # и путать их значит потерять второй.
        reached = bool((g.get("outcome") or {}).get(FREEZE_OUTCOME))
        return {"frozen": False, "applies": True, "readable": True, "decision": FREEZE_DECISION,
                "lifted_by": lifted, "outcome_reached": reached,
                "reason": (f"заморозка снята решением {lifted}"
                           + ("" if reached else
                              f"; исход {FREEZE_OUTCOME} при этом ещё НЕ достигнут — "
                              f"снято решением, а не результатом"))}
    if not isinstance(g.get("outcome"), dict) or FREEZE_OUTCOME not in g["outcome"]:
        # ЦЕЛЬ ЕСТЬ, А ИСХОДА НЕТ — это НЕ «заморозки нет»: иначе правило снималось бы удалением
        # одной строки в том же файле, который оно охраняет.
        return {"frozen": True, "applies": True, "readable": False, "decision": FREEZE_DECISION,
                "reason": f"исход {FREEZE_GOAL}.{FREEZE_OUTCOME} не найден в плане — "
                          f"состояние заморозки не прочитано, поэтому считается держащейся"}
    reached = bool(g["outcome"][FREEZE_OUTCOME])
    return {"frozen": not reached, "applies": True, "readable": True, "decision": FREEZE_DECISION,
            "lifted_by": None, "outcome_reached": reached,
            "reason": (f"исход {FREEZE_OUTCOME} верен — заморозка снята" if reached else
                       f"исход {FREEZE_OUTCOME} ещё не верен — новые умения не принимаются")}


def _field_evidence_present(root) -> bool:
    """Есть ли полевое доказательство исхода снятия заморозки — `registry/release-claims.yaml`,
    ключ `field_evidence`. Здесь же его требует и `test_the_real_plan_does_not_claim_an_outcome...`.

    Fail-closed: файла нет или он не разобран — доказательства нет. «Не смог прочитать» не должно
    читаться как «доказано», иначе снятие заморозки снова держалось бы на самодекларации.
    """
    p = Path(root) / "registry" / "release-claims.yaml"
    if not p.is_file():
        return False
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False
    return bool(doc.get("field_evidence"))


def _freeze_lift_errors(fz, root) -> list:
    """Ошибки контракта СНЯТИЯ заморозки. Заморозку снимают двумя способами, и каждый обязан
    опираться на доказательство, а не на слово в плане:

    1. решением человека (`freeze_lifted_by`) — решение обязано существовать в
       `decisions/registry.yaml`, иначе строка `freeze_lifted_by: почему-то` снимала бы правило без
       следа (это ровно «разморозить вручную», от чего механизм и защищал);
    2. достигнутым исходом (`FREEZE_OUTCOME: true`) — исход, ставший верным, снимает заморозку САМ
       ПО СЕБЕ (fail-closed `freeze_state`) и вдобавок открывает канал `stable`, поэтому `true`
       обязан нести полевое доказательство (`registry/release-claims.yaml -> field_evidence`).
       `true` без доказательства — самодекларация класса F-002/F-005: заморозку сняли, не доказав
       исход (аудит 04.09.2026). `validate` прежде сверял только структуру, а не истинность исхода.

    Проверка молчит без `root` (доказательство негде резолвить — как и остальные ссылочные проверки)
    и на планах без цели заморозки (`applies=False`): правило внутреннее для репозитория кита, а не
    для планов дочек.
    """
    errors = []
    fz = fz or {}
    if root is None:
        return errors
    lift = str(fz.get("lifted_by") or "").strip()
    if lift:
        reg = Path(root) / "decisions" / "registry.yaml"
        if not reg.is_file():
            errors.append(f"заморозка снята решением '{lift}', но decisions/registry.yaml нет — "
                          f"сослаться не на что")
        else:
            doc = None
            try:
                doc = yaml.safe_load(reg.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError) as e:
                errors.append(f"заморозка снята решением '{lift}', но decisions/registry.yaml "
                              f"не разобран ({type(e).__name__}) — сослаться не на что")
            if doc is not None:
                reg_items = doc.get("decisions") or doc.get("episodes") or doc.get("registry") or []
                if isinstance(reg_items, dict):
                    known = lift in reg_items
                elif isinstance(reg_items, list):
                    known = any(str((d or {}).get("id")) == lift
                                for d in reg_items if isinstance(d, dict))
                else:
                    known = False
                if not known:
                    errors.append(
                        f"заморозка снята решением '{lift}', которого нет в decisions/registry.yaml — "
                        f"снятие без записанного решения это тихий обход правила, а не решение")
    # ГЛАВНОЕ ЗАКРЫТИЕ (P0 04.09.2026): исход, ставший `true`, снимает заморозку — значит именно от
    # него и требуется доказательство, независимо от того, названо ли ещё и решение.
    if fz.get("applies") and fz.get("outcome_reached") and not _field_evidence_present(root):
        errors.append(
            f"заморозка {FREEZE_DECISION} снята исходом {FREEZE_OUTCOME}=true, но доказательства "
            f"нет: registry/release-claims.yaml -> field_evidence пуст. `true` снимает заморозку и "
            f"открывает канал stable, поэтому обязан нести полевое доказательство — иначе это "
            f"самодекларация, а не достигнутый исход")
    return errors


def goal_freeze_relation(plan) -> dict:
    """{id цели: run_condition|extension|None}. Отношение объявлено НА ЦЕЛИ намеренно: оговорка
    решения сказана про назначение работы, а проверять её по формулировке заявки — открыть лазейку
    через слова (это названо в самом решении)."""
    return {g["id"]: g.get("freeze_relation") for g in goals(plan)}


def frozen_work(plan, single_goal=None) -> dict:
    """{id работы: причина} для работ, которые заморозка не пускает в дело.

    Заморожена работа цели `extension`, пока держится заморозка, — КРОМЕ работы с явным
    `freeze_exception: <причина>`: исключение существует, но только словами и с причиной, потому что
    молчаливый обход правила и есть то, от чего правило не работает.
    """
    st = freeze_state(plan)
    if not st["frozen"]:
        return {}
    rel = goal_freeze_relation(plan)
    out = {}
    for w in items(plan):
        goal = w.get("goal") or single_goal
        if rel.get(goal) != "extension":
            continue
        if str(w.get("freeze_exception") or "").strip():
            continue
        out[w.get("id")] = (f"цель '{goal}' помечена как расширение умений, а заморозка держится "
                            f"({st['reason']}; решение {st['decision']})")
    return out
