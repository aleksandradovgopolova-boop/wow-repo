#!/usr/bin/env python3
"""Human Communication Layer: между внутренним состоянием и человеком (v3.35.0).

Внутри кит говорит `GateResult`, `write_scope`, `tested_revision`, `preflight_block` — и обязан
продолжать: это точные имена, по которым работает код. Наружу они попадать не должны.

    внутреннее состояние -> UserMessage -> текст под выбранную аудиторию

`UserMessage` — контракт, а не совет по стилю (`registry/communication-policy.yaml`). Слой живёт в
КОДЕ, а не только в скилле, потому что соблюдение правил не может зависеть от того, вспомнила ли
конкретная модель вызвать скилл: иначе при смене runtime поведение теряется, а в половине прогонов
пользователь снова читает лог.

ЧТО СЛОЙ НЕ ДЕЛАЕТ. Не сглаживает. Простой язык — не мягкий: `degraded` остаётся `degraded` на всех
трёх уровнях, недоказанное называется недоказанным. «Готово» вместо «готово, но не проверено»
дороже любого жаргона, и presenter обязан этому мешать, а не помогать.

Использование:
  presenter.py demo [--audience product|technical|debug]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
           Path(__file__).resolve().parents[2])
POLICY = PKG / "registry" / "communication-policy.yaml"

AUDIENCES = ("product", "technical", "debug")
STATUS_LABEL = {
    "ok": "Работа продвинулась",
    "needs_input": "Нужно твоё решение",
    "blocked": "Пока не могу продолжить",
    "done": "Готово",
    "degraded": "Готово, но проверено не всё",
}


def _q(n, one="вопрос", few="вопроса", many="вопросов"):
    """«1 вопрос / 4 вопроса / 6 вопросов». Русская форма — часть простого языка: сообщение,
    спотыкающееся на числительном, читается как машинный перевод, а не как речь."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


class PolicyMissing(Exception):
    """Политика коммуникации не найдена — рендерить «как-нибудь» хуже, чем сказать об этом."""


def load_policy(path=None) -> dict:
    p = Path(path or POLICY)
    if not p.is_file():
        raise PolicyMissing(f"политика коммуникации не найдена: {p}")
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise PolicyMissing(f"политика коммуникации не разбирается ({p}): {e}") from e


def audience_from_config(child_root, policy=None) -> str:
    """Аудитория из `.ai-ops.yaml -> communication.audience`. По умолчанию — `product`.

    Default именно `product`: система по умолчанию разговаривает с владельцем продукта, а не с
    отладчиком. Обратный default — то, как внутренний язык и просачивался наружу.
    """
    policy = policy or load_policy()
    default = policy.get("default_audience", "product")
    cfg = Path(child_root) / ".ai-ops.yaml"
    if not cfg.is_file():
        return default
    try:
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return default
    aud = ((data.get(policy.get("config_key", "communication")) or {}).get("audience"))
    return aud if aud in AUDIENCES else default


def message(status, summary, why_it_matters=None, decision=None, next_steps=None,
            technical=None) -> dict:
    """Собрать UserMessage.

    `technical` не выбрасывается, а откладывается: на уровне `product` он доступен по запросу, на
    `technical`/`debug` печатается. Выбросить его значило бы сделать кит непроверяемым.
    """
    if status not in STATUS_LABEL:
        raise ValueError(f"status '{status}' вне контракта {sorted(STATUS_LABEL)}")
    if not (summary or "").strip():
        raise ValueError("summary обязателен: сообщение без «что произошло» — это лог")
    msg = {"schema_version": 1, "kind": "user-message", "status": status,
           "summary": summary.strip()}
    if why_it_matters:
        msg["why_it_matters"] = why_it_matters.strip()
    if decision:
        # Вопрос без рекомендации — переложенная работа: правило recommend-not-enumerate.
        if not decision.get("question"):
            raise ValueError("decision без question")
        msg["decision"] = {"question": decision["question"],
                           "recommendation": decision.get("recommendation"),
                           "on_approve": decision.get("on_approve"),
                           "on_reject": decision.get("on_reject")}
    if next_steps:
        msg["next"] = list(next_steps) if isinstance(next_steps, (list, tuple)) else [next_steps]
    msg["technical_details"] = {"available": bool(technical), "payload": technical or {}}
    return msg


def render(msg: dict, audience="product", show_technical=False) -> str:
    """UserMessage -> текст. Один контракт, три языка; факты во всех трёх одни и те же."""
    if audience not in AUDIENCES:
        audience = "product"
    L = []
    label = STATUS_LABEL.get(msg.get("status"), msg.get("status", ""))
    L.append(f"{label}. {msg.get('summary', '')}".strip())
    if msg.get("why_it_matters"):
        L.append(msg["why_it_matters"])

    d = msg.get("decision")
    if d:
        L.append("")
        L.append(f"Нужно от тебя: {d['question']}")
        if d.get("recommendation"):
            L.append(f"Рекомендую: {d['recommendation']}")
        if d.get("on_approve"):
            L.append(f"Если согласен — {d['on_approve']}.")
        if d.get("on_reject"):
            L.append(f"Если нет — {d['on_reject']}.")

    if msg.get("next"):
        L.append("")
        L.append("Дальше: " + "; ".join(msg["next"]) + ".")

    tech = (msg.get("technical_details") or {})
    if tech.get("available"):
        # `product` прячет детали за запрос, `technical`/`debug` показывают сразу. Явный
        # `show_technical=True` — ответ на «покажи технические детали» и работает на любом уровне.
        if audience in ("technical", "debug") or show_technical:
            L.append("")
            L.append("Технические детали:")
            for k, v in (tech.get("payload") or {}).items():
                L.append(f"  {k}: {v}")
        elif audience == "product":
            L.append("")
            L.append("Технические детали — по запросу («покажи технические детали»).")
    return "\n".join(L)


# ── Переводчики внутренних отчётов ────────────────────────────────────────────────────────────
# Каждая функция берёт СЫРОЙ внутренний отчёт и возвращает UserMessage. Это и есть шов: внутренние
# имена остаются внутри, наружу выходит смысл.

def from_repository_understanding(rep: dict) -> dict:
    """`repo_audit.run()` -> UserMessage.

    Плохо: «Artifact coverage 8/15. architecture=inferred, data_model=partial, delivery=verified».
    Хорошо: «Осмотрел проект. Техническую картину восстановил сам; не хватает того, что из кода
    честно не узнать». Числа остаются в технических деталях — они не врут, они просто не ответ.
    """
    cls = rep["classification"]["class"]
    aud = rep["audit"]
    ask = rep["ask"]
    n_q = len(ask["questions"])
    known = ", ".join(k.replace("_", " ") for k, v in rep["reconstructed"].items()
                      if v["status"] in ("verified", "inferred") and v.get("value"))
    human_needed = [c["title"] for c in aud["contours"] if c["needs_human"]]

    if cls == "NEW_PRODUCT":
        summary = ("Похоже, это новый продукт: работающей системы и истории разработки я не нашёл. "
                   "Сначала соберём минимальную модель продукта, потом смогу предложить "
                   "архитектуру и план работ.")
        why = None
    elif cls == "UNKNOWN":
        summary = "Не смог осмотреть репозиторий — прочитать его содержимое не получилось."
        why = "Без этого любой мой вывод о проекте был бы выдумкой, поэтому я не начинаю."
    else:
        summary = ("Я разобрался с проектом. Это " + ("уже работающий продукт"
                   if cls == "EXISTING_PRODUCT" else "ранняя стадия продукта") + ".")
        why = ("Техническую картину я восстановил сам" + (f": {known}" if known else "") +
               ". А то, что из кода честно узнать нельзя, спрошу у тебя — "
               "выдумывать это я не буду.") if known else None

    steps = []
    if n_q:
        steps.append(f"задам {n_q} "
                     + ("короткий " if n_q % 10 == 1 and n_q % 100 != 11 else "коротких ")
                     + _q(n_q))
        steps.append("соберу недостающие материалы и покажу их тебе на проверку")
    else:
        steps.append("покажу, какие задачи имеет смысл брать дальше")
    if aud["blocking_gaps"]:
        steps.append("после этого появится план работ")

    return message(
        status="needs_input" if n_q else "ok",
        summary=summary, why_it_matters=why, next_steps=steps,
        decision=({"question": f"ответить на {n_q} {_q(n_q)} о продукте, направлении и границах",
                   "recommendation": "ответить сразу — дальше я работаю без остановок",
                   "on_approve": "соберу базовую модель продукта и предложу первые задачи",
                   "on_reject": "оставлю эти области помеченными как «не подтверждено» и не буду "
                                "их выдумывать"} if n_q else None),
        technical={"classification": cls,
                   "confidence": rep["classification"]["confidence"],
                   "contours_verified": len(aud["ready"]),
                   "contours_total": len(aud["contours"]),
                   "ai_can_build": ", ".join(aud["ai_can_build"]) or "—",
                   "needs_human": ", ".join(human_needed) or "—",
                   "blocking_gaps": ", ".join(aud["blocking_gaps"]) or "—",
                   "questions": n_q})


def from_next_work(rep: dict) -> dict:
    """`next_work.compute()` -> UserMessage. «Что делать дальше» человеческими словами."""
    if not rep.get("plan_present"):
        return message(status="blocked",
                       summary="Плана работ в проекте пока нет, поэтому предложить следующую "
                               "задачу мне нечем.",
                       why_it_matters="Без объявленных целей и работ любой мой выбор был бы "
                                      "просто первой строкой списка.",
                       next_steps=["создам черновик плана из шаблона, если скажешь"],
                       technical={"gap": rep.get("gap")})

    # ПЕРЕВОД НЕ ПРЯЧЕТ ДЕФЕКТ. Если сам план недостоверен (цикл зависимостей, поле исполнителя,
    # отсутствующее направление), ответ «что взять следующим» построен на неверных данных, и
    # сообщить об этом обязательно — иначе слой простого языка становится способом скрыть ошибку,
    # а не объяснить её. Проверка стоит ПЕРВОЙ: она сильнее любого другого исхода.
    plan_errors = list(rep.get("plan_errors") or [])
    rm_errors = list((rep.get("roadmap") or {}).get("errors") or [])
    if plan_errors or rm_errors:
        n = len(plan_errors) + len(rm_errors)
        return message(
            status="blocked",
            summary="Не могу предложить следующую работу: описание плана и направления содержит "
                    f"{n} {_q(n, 'ошибку', 'ошибки', 'ошибок')}.",
            why_it_matters="Пока они не исправлены, любой мой ответ про «что дальше» опирался бы на "
                           "неверные данные — я предпочитаю сказать это прямо.",
            next_steps=["перечислю, что именно неверно, и предложу исправления"],
            technical={f"ошибка {i + 1}": x for i, x in enumerate(plan_errors + rm_errors)})

    nb = rep.get("next_best")
    active = rep.get("in_progress") or []
    blocked = rep.get("blocked") or []
    if not nb:
        # Ведро `not_ready` — работа, ГОТОВАЯ по графу, но не прошедшая допуск (бюджет, capability,
        # конфликт записи). Прежде оно терялось, и продакту сообщался ложный факт «работа не
        # объявлена», хотя работа объявлена и всего лишь не допущена. Перевод менял не язык, а
        # факты — то, что политика запрещает прямо.
        not_ready = rep.get("not_ready") or []
        _ADMISSION_RU = {
            "within_budget": "не укладывается в остаток бюджета",
            "capabilities_ready": "требует возможностей, которых нет",
            "no_write_conflict": "трогает файлы, которые уже правит другая работа",
            "no_human_decision": "ждёт решения человека",
            "deps_done": "ждёт незакрытые зависимости",
        }
        if not_ready:
            causes = sorted({_ADMISSION_RU.get(c, c)
                             for r in not_ready for c in (r.get("blocked_by_admission") or [])})
            why = ("Работа объявлена, но взять её сейчас нельзя: "
                   + "; ".join(causes) + ".") if causes else \
                  "Работа объявлена, но не прошла проверку готовности."
        elif blocked:
            why = f"Это не значит, что всё сделано: {len(blocked)} задач ждут снятия блокировки."
        else:
            why = "Это не значит, что всё сделано: работа пока не объявлена."
        return message(
            status="blocked",
            summary="Готовой к работе задачи сейчас нет.",
            why_it_matters=why,
            next_steps=["покажу, что именно мешает, если нужно"],
            technical={"blocked": ", ".join(b["id"] for b in blocked) or "—",
                       "in_progress": ", ".join(a["id"] for a in active) or "—",
                       "not_admitted": ", ".join(
                           f"{r.get('id')}: {', '.join(r.get('blocked_by_admission') or [])}"
                           for r in not_ready) or "—"})

    par = rep.get("parallel_with") or []
    steps = [f"возьмусь за «{nb['title']}»"]
    if par:
        steps.append("параллельно можно вести " +
                     " и ".join(f"«{p['title']}»" for p in par) +
                     " — эти работы не пересекаются по изменяемым файлам")
    return message(
        status="ok",
        summary=f"Следующей имеет смысл взять «{nb['title']}».",
        why_it_matters="Потому что " + "; ".join(nb["why"]) + ".",
        next_steps=steps,
        technical={"id": nb["id"], "owner_role": nb["owner_role"], "score": nb["score"],
                   "unblocks": nb["unblocks"],
                   "parallel_with": ", ".join(p["id"] for p in par) or "—",
                   "blocked_count": len(blocked)})


def from_contour_consistency(rep: dict) -> dict:
    """`contours.reconcile()` -> UserMessage. Ровно тот случай, ради которого модель нужна."""
    major = [f for f in rep["findings"] if f["severity"] == "major"]
    if not rep["comparable"]:
        return message(status="ok", summary="Сверять пока нечего: изменений не предъявлено.",
                       next_steps=["проверю связность, когда появится изменение"])
    if not major:
        return message(status="ok",
                       summary="Изменение согласовано с описанием продукта.",
                       next_steps=["продолжаю"],
                       technical={"findings": len(rep["findings"])})
    undeclared = [f["contour"] for f in major if f["id"] == "undeclared_change"]
    not_updated = [f["contour"] for f in major if f["id"] == "declared_not_updated"]
    parts = []
    if not_updated:
        parts.append("изменилось то, что описано в проекте, но само описание не обновлено")
    if undeclared:
        parts.append("изменение затронуло области, о которых задача не предупреждала")
    return message(
        status="degraded",
        summary="Изменение готово, но описание продукта за ним не поспело: " + "; ".join(parts) + ".",
        why_it_matters="Следующая сессия — и человек, и агент — прочитает устаревшее описание как "
                       "правду. Именно так расходятся код и представление о нём.",
        next_steps=["обновлю затронутые описания и покажу изменения",
                    "либо скажи, что менять их не нужно, и я запишу это как решение"],
        technical={f["contour"]: f["detail"] for f in major})


def demo(audience="product"):
    """Один и тот же внутренний отчёт на трёх языках — то, что проверяют evals."""
    msg = message(
        status="needs_input",
        summary="Пока не начинаю разработку.",
        why_it_matters="Задача затрагивает защищённую часть проекта, поэтому мне нужно твоё "
                       "подтверждение. Остальное к работе готово.",
        decision={"question": "разрешить изменение модуля авторизации",
                  "recommendation": "разрешить только чтение агрегированных данных",
                  "on_approve": "начну реализацию и принесу результат на проверку",
                  "on_reject": "предложу вариант, который этот модуль не трогает"},
        next_steps=["после подтверждения — реализация и независимая проверка"],
        technical={"gate": "specification", "protected_paths": "auth/*",
                   "context": "128k / 150k", "approval_record": "missing"})
    return render(msg, audience=audience)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="presenter.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo")
    d.add_argument("--audience", choices=list(AUDIENCES), default="product")
    d.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])
    if ns.cmd == "demo":
        if ns.json:
            print(json.dumps(load_policy().get("message_contract"), ensure_ascii=False, indent=2))
        else:
            print(demo(ns.audience))
    return 0


if __name__ == "__main__":
    sys.exit(main())
