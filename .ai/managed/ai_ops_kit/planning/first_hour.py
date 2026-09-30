"""Оркестратор «первого часа» — model→answers→bootstrap→next ОДНИМ нарративом (issue #647).

Первый час с китом сегодня — четыре раздельные команды (`model` → `model --answer` →
`bootstrap --apply` → `next`), а склейку и повествование держит только проза скилла (два
расходящихся файла). Здесь склейка становится first-class В САМОМ ките: понял репозиторий →
вот что знаю/не знаю → (если ответы есть) первое направление и план → следующая работа и почему.

ЭТО НЕ НОВЫЙ ДВИЖОК. Все кирпичи готовы и проверены — `repo_audit.run` (понимание + provenance),
`product_bootstrap.plan/apply`, `next_work.compute`. Здесь только тонкая склейка с честными
состояниями: пока есть открытые БЛОКИРУЮЩИЕ вопросы (или источники противоречат — CONFLICTING,
#634), кит НЕ выдаёт направление за готовое, а честно останавливается на «нужны ответы». Ответы
есть — собирает направление и план и говорит, какую работу взять первой.

Дисциплина записи сохранена: без `apply=True` это сухой предпросмотр (что БУДЕТ создано), запись
делает только явный `apply` — ровно как отдельная команда `bootstrap --apply`.
"""
from __future__ import annotations

NEEDS_ANSWERS = "needs_answers"
READY = "ready"
BLOCKED_UNDERSTANDING = "blocked_understanding"

# Куда ложится ПЕРВЫЙ РЕЗУЛЬТАТ файлом (зона `generated` — сгенерированное китом, не история
# продукта и не правится руками). Конец установки — не строка «установлено», а этот файл: его
# человек может открыть и с ним работать.
RESULT_REL = ".ai/generated/first-result.md"


def run(child_root, *, apply=False, budget_left=None, understanding=None) -> dict:
    """Сшить первый час одним отчётом. -> dict со стадией и честными состояниями каждого шага.

    stage:
      blocked_understanding — дерево не прочиталось (класс UNKNOWN): любой вывод был бы выдумкой;
      needs_answers         — есть блокирующие вопросы или противоречия источников: направление
                              собирать рано, кит останавливается и НЕ выдаёт непроверенное за готовое;
      ready                 — ответов хватает: собрано направление/план (+ предложена работа).
    """
    from ai_ops_kit.planning import repo_audit as _ra
    from ai_ops_kit.planning import product_bootstrap as _boot
    from ai_ops_kit.planning import next_work as _nw

    und = understanding if understanding is not None else _ra.run(child_root)
    ask = und.get("ask") or {}
    questions = list(ask.get("questions") or [])
    blocking = [q for q in questions if q.get("blocks_work")]
    conflicts = list(und.get("conflicts") or [])
    cls = (und.get("classification") or {}).get("class")

    out = {"schema_version": 1, "kind": "first-hour", "classification": cls,
           "understanding": und, "questions": questions, "blocking_questions": blocking,
           "conflicts": conflicts, "bootstrap": None, "bootstrap_applied": False, "next": None}

    # Дерево не прочиталось — останавливаемся раньше всего: bootstrap на выдумке был бы вреден.
    if cls == "UNKNOWN":
        out["stage"] = BLOCKED_UNDERSTANDING
        return out

    # Блокирующие вопросы ИЛИ противоречие источников — направление собирать рано. Это честная
    # остановка, а не провал: кит называет, чего не хватает, и не выдаёт непроверенное за готовое.
    if blocking or conflicts:
        out["stage"] = NEEDS_ANSWERS
        return out

    # Ответов хватает: собираем направление и план. Без apply — предпросмотр (что БУДЕТ создано).
    boot_plan = _boot.plan(child_root, und)
    out["bootstrap"] = _boot.apply(child_root, boot_plan, und) if apply else boot_plan
    out["bootstrap_applied"] = bool(apply)

    # Следующую работу считаем только когда план РЕАЛЬНО есть (после apply): иначе next_work читал бы
    # пустой/заготовочный план и «рекомендация» была бы ни о чём. Сухой предпросмотр честно без next.
    if apply and not (out["bootstrap"] or {}).get("error"):
        out["next"] = _nw.compute(child_root, budget_left=budget_left)

    out["stage"] = READY
    return out


def _join(lines) -> str:
    """Склеить строки Markdown, схлопнув подряд идущие пустые и убрав хвостовые — чтобы порядок
    секций (какие вопросы/конфликты есть) не оставлял двойных отбивок."""
    out = []
    for ln in lines:
        if ln == "" and (not out or out[-1] == ""):
            continue
        out.append(ln)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def _classification(res: dict) -> str:
    cls = res.get("classification")
    if isinstance(cls, dict):
        cls = cls.get("class")
    return cls or "—"


_HEALTH_MARK = {"ok": "✓", "attention": "•", "urgent": "⚠", "not_checked": "?", "info": "·"}


def _health_lines(res: dict) -> list:
    """Раздел «Состояние проекта сейчас» (#1200): итог одной фразой + по строке на проверку.

    Итог считает слой команд (`cli/ai_ops_cli_first_health`) из уже существующих проверок; здесь его
    только печатаем. Нет итога (старый вызов / не посчитан) — раздела нет, а не «всё в порядке»."""
    health = res.get("health")
    if not isinstance(health, dict) or not health.get("verdict"):
        return []
    lines = ["## Состояние проекта сейчас", "", f"**{health['verdict']}**", ""]
    for it in health.get("items") or []:
        mark = _HEALTH_MARK.get(it.get("status"), "·")
        lines.append(f"- {mark} **{it.get('area')}**: {it.get('text')}")
    return lines + [""]


def _render_needs_answers(res: dict, cls: str) -> str:
    """Первый результат стадии needs_answers: что понято + какие ответы нужны (без правки YAML)."""
    lines = ["# Первый результат AI Ops", "",
             "Кит разобрался в репозитории. Это первый результат: не «кит установлен», а понимание",
             "проекта. Собрать направление и план пока не из чего — часть фактов знает только человек,",
             "из кода их не вывести. Недоказанное кит называет недоказанным, а не выдаёт за готовое.",
             "", f"**Что понято.** Класс репозитория: `{cls}`.", ""] + _health_lines(res)
    for c in res.get("conflicts") or []:
        if len(lines) and lines[-1] != "":
            lines.append("")
        cat = c.get("category") or "?"
        summ = (c.get("summary") or "").strip()
        lines.append(f"**Источники истины расходятся** ({cat})"
                     + (f": {summ}" if summ else "") + " — сторону кит сам не выбирает.")
    blocking = res.get("blocking_questions") or []
    if blocking:
        lines += ["", "**Нужны ответы:**", ""]
        for q in blocking:
            qid = q.get("id") or "?"
            ask = (q.get("ask") or "").strip()
            lines.append(f"- `{qid}` — {ask}" if ask else f"- `{qid}`")
    lines += ["", "## Как продолжить (YAML править руками не нужно)", "",
              'Ответь на вопросы командой:', "",
              '    ai-ops model --answer <вопрос> "<ответ>"', "",
              "Потом собери направление, план и первую работу:", "",
              "    ai-ops model --flow --apply", ""]
    return _join(lines)


def _render_ready(res: dict, cls: str) -> str:
    """Первый результат стадии ready: собранные направление/план + первая работа."""
    boot = res.get("bootstrap") or {}
    n_work = boot.get("work_items")
    if isinstance(n_work, list):
        n_work = len(n_work)
    n_work = n_work or 0
    lines = ["# Первый результат AI Ops", "",
             "Кит понял репозиторий и собрал первое направление и план продукта — из фактов, а не из",
             "догадок. Это и есть первый результат: не «кит установлен», а материал, с которым можно",
             "работать.", "",
             f"**Что понято.** Класс репозитория: `{cls}`.",
             f"**Собрано.** Работ в плане: {n_work}.", ""] + _health_lines(res)
    written = boot.get("written") or []
    if written:
        lines += ["**Записанные артефакты:**", ""]
        for w in written:
            path = w.get("path") or "?"
            what = (w.get("what") or "").strip()
            lines.append(f"- `{path}`" + (f" — {what}" if what else ""))
        lines.append("")
    nxt = res.get("next") or {}
    first = nxt.get("next_best") if isinstance(nxt, dict) else None
    if isinstance(first, dict):
        title = first.get("title") or first.get("id") or "?"
        lines += [f"## Первой имеет смысл взять: {title}", ""]
        why = first.get("why") or []
        if why:
            lines += ["Почему:", ""] + [f"- {w}" for w in why] + [""]
    lines += ["## Как продолжить", "",
              "Очередь работ и рекомендация:", "", "    ai-ops next", "",
              "Или попроси кит начать первую работу.", ""]
    return _join(lines)


def render_result_markdown(res: dict):
    """Первый час -> человекочитаемый ПЕРВЫЙ РЕЗУЛЬТАТ (Markdown). -> str | None.

    None на blocked_understanding: результата нет, а писать «результат», которого не было, значило бы
    выдать «установлено» за «сделано». На ready — собранные направление/план и первая работа; на
    needs_answers — что понято и какие ответы нужны (и как их дать без ручной правки YAML)."""
    stage = res.get("stage")
    if stage == BLOCKED_UNDERSTANDING:
        return None
    cls = _classification(res)
    if stage == NEEDS_ANSWERS:
        return _render_needs_answers(res, cls)
    return _render_ready(res, cls)


def write_result(child_root, res: dict):
    """Записать первый результат файлом в дочку (`RESULT_REL`). -> относительный путь (str) | None.

    None — когда результата нет (blocked_understanding): файла-обманки не создаём. Пишем в зону
    `generated`, создавая каталог при необходимости. Вызывать ТОЛЬКО при apply — как и bootstrap,
    сухой прогон в чужой репозиторий ничего не пишет."""
    from pathlib import Path
    text = render_result_markdown(res)
    if text is None:
        return None
    target = Path(child_root) / RESULT_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return RESULT_REL
