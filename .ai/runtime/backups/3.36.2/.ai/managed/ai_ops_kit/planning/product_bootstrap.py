#!/usr/bin/env python3
"""BOOTSTRAP: онбординг заканчивается РАБОТОЙ, а не документацией (v3.35.2).

ЧТО БЫЛО НЕ ТАК. Первый сценарий объявлен как
`INSTALL -> DISCOVER -> CLASSIFY -> RECONSTRUCT -> AUDIT -> ASK -> BOOTSTRAP -> PLAN -> RECOMMEND`,
и семь стадий из девяти работали. BOOTSTRAP существовал СТРОКОЙ в реестре: кит не создавал ни
`ROADMAP.md`, ни `planning/plan.yaml`, ни одного документа контекста. Владелец, ответив на вопросы,
оставался там же, где был, — с пониманием и без плана. Обещание «онбординг заканчивается работой»
не выполнялось ничем (тир 4 разбора перед квалификацией).

ЧЕГО КИТ НЕ ДЕЛАЕТ. Не выдумывает продуктовые факты. Цели продукта, боли пользователей и
следующий результат из кода не выводятся — и на их месте кит НЕ пишет правдоподобный текст, он
пишет пометку «нужно ваше слово». Единственная работа, которую кит вправе объявить сам, — работа
по закрытию СВОИХ ЖЕ пробелов: она выведена из аудита, а не сочинена. Отсюда и содержание плана:
«описать модель данных», «объявить окружения» — с ролью, областью записи и затронутым контуром.

ЧЕГО КИТ НЕ ПЕРЕЗАПИСЫВАЕТ. Ничего. Существующий файл — факт о продукте, и он сильнее любого
шаблона: bootstrap создаёт только отсутствующее и говорит, что пропустил и почему.

Использование:
  product_bootstrap.py plan  <repo> [--json]     # что будет создано (ничего не пишет)
  product_bootstrap.py apply <repo> [--json]     # создать отсутствующее
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from ai_ops_kit.planning import contours as _contours
from ai_ops_kit.planning import delivery_plan as _plan
from ai_ops_kit.planning import repo_audit as _audit
from ai_ops_kit.planning import roadmap as _roadmap

# Цель, которую кит вправе объявить за владельца: она про САМ КИТ, а не про продукт. «Продукт
# описан настолько, чтобы работу можно было планировать и проверять» — проверяемый результат, и он
# не притворяется продуктовой целью.
BASELINE_GOAL = "model-baseline"

# Контур -> тип работы по его закрытию. Берём из `work_types` модели (contour -> type), чтобы
# словарь не разъехался с реестром: обратное отображение строится, а не переписывается.
_FALLBACK_TYPE = "docs"


def _type_for_contour(model: dict, cid: str) -> str:
    for wt, spec in (model.get("work_types") or {}).items():
        if (spec or {}).get("contour") == cid:
            return wt
    return _FALLBACK_TYPE


def _write_scope_for(model: dict, cid: str, child_root) -> list:
    """Область записи работы = места, где живёт правда контура. Без неё параллельность недоказуема."""
    scope = []
    for s in _contours.sot_for(model, cid, child_root):
        rel = (s.get("path") or "").strip()
        if not rel:
            continue
        # Внутренности кита в область записи человека не попадают: `.ai/runtime/active-work.yaml`
        # ведёт сам кит, и предлагать владельцу «описать» его — приглашение править не своё.
        if rel.startswith(".ai/"):
            continue
        # Каталог, а не файл: работа по описанию контура тронет и соседние документы.
        scope.append(rel if rel.endswith("/") else str(Path(rel).parent) + "/")
    return sorted({s for s in scope if s not in ("./", "/")}) or ["context/"]


def work_items(understanding: dict, model: dict | None = None, child_root=".") -> list:
    """Работы по закрытию пробелов модели. -> список элементов плана в порядке срочности.

    ЭТО НЕ ВЫДУМКА, А ВЫВОД: каждый элемент соответствует контуру, чей источник истины
    отсутствует или устарел, и ярус пробела задаёт порядок. Контуры, которые кит закрывает сам,
    в план не попадают — иначе владелец получил бы список работы, которую делать не нужно.
    """
    model = model or _contours.load_model()
    tiers = [t.get("id") for t in (model.get("gap_tiers") or [])]
    rows = [r for r in understanding["audit"]["contours"] if r["state"] != _audit.VERIFIED]
    rows.sort(key=lambda r: (tiers.index(r["gap_tier"]) if r["gap_tier"] in tiers else 99,
                             r["contour"]))
    out, prev = [], None
    for r in rows:
        cid = r["contour"]
        wid = f"describe-{cid.replace('_', '-')}"[:64]
        item = {
            "id": wid,
            "title": f"Описать: {r['title']}",
            "type": _type_for_contour(model, cid),
            "goal": BASELINE_GOAL,
            "status": "todo",
            "owner_role": r.get("owner_role") or "product-owner",
            "write_scope": _write_scope_for(model, cid, child_root),
            "value": "high" if r["gap_tier"] == "required_now" else "medium",
            "affects": {cid: True},
            # ЧЕГО НЕ ХВАТАЕТ — В САМОМ ЭЛЕМЕНТЕ, а не в отдельном отчёте: работа без причины
            # выглядит как бюрократия, и её справедливо не делают.
            "why": (f"{r['question']} — сейчас ответа в репозитории нет "
                    f"({r['state']}); отсутствует: "
                    + (", ".join(r["missing_required"][:3]) or "описание контура")),
        }
        if r["needs_human"]:
            # ЖДЁТ ЧЕЛОВЕКА — ЗНАЧИТ НЕ ЖДЁТ ДРУГИХ РАБОТ. Сначала я выстроил все работы в одну
            # цепочку, и первая же из них, требующая ответа владельца, блокировала ВСЕ остальные:
            # сразу после bootstrap кит отвечал «готовой к работе задачи сейчас нет» — то есть
            # онбординг снова заканчивался не работой. Такие работы стоят отдельно.
            item["depends_on"] = []
            item["human_decision"] = ("нужен ответ владельца: из кода это не выводится "
                                      "(см. .ai/project/onboarding-answers.yaml). Ответив, "
                                      "удалите это поле — тогда работа станет готовой")
        else:
            # Цепочка, а не веер: то, что кит может закрыть сам, он закрывает по одному — иначе
            # `next` предложит восемь работ сразу, и владелец получит тот самый список из
            # четырнадцати пунктов, против которого вся модель.
            item["depends_on"] = [prev] if prev else []
            prev = wid
        out.append(item)
    return out


def _roadmap_text(understanding: dict) -> str:
    """ROADMAP из ФАКТОВ, с честными пробелами там, где фактов нет."""
    rec = understanding.get("reconstructed") or {}

    def fact(key):
        v = (rec.get(key) or {})
        if v.get("value") and v.get("status") in (_audit.VERIFIED, _audit.INFERRED,
                                                  _audit.USER_CONFIRMED):
            return str(v["value"]), v["status"]
        return None, None

    goal_now, goal_src = fact("main_goal_now")
    user, user_src = fact("primary_user")
    cls = (understanding.get("classification") or {}).get("class", "UNKNOWN")

    L = ["---",
         "read_tier: 1            # ярус 1: направление читается при старте сессии",
         "stability: evolving",
         "owner: product-team",
         "generated_by: ai-ops bootstrap",
         "---",
         "",
         "# ROADMAP — куда идёт продукт",
         "",
         "> Первую версию этого файла собрал AI Ops при онбординге: **из фактов репозитория**, а",
         "> не из догадок. Там, где факта не было, стоит пометка «нужно ваше слово» — это не",
         "> заготовка на будущее, а именно то, чего кит знать не может.",
         ""]
    if user:
        L += [f"Основной пользователь: {user} _(источник: {user_src})_.", ""]
    L += ["## Сейчас", "",
          f"- `{BASELINE_GOAL}` — продукт описан настолько, чтобы работу можно было планировать и",
          "  проверять: у каждого контура есть источник истины, и кит перестаёт отвечать «не знаю»."]
    if goal_now:
        L += [f"- _нужно ваше слово:_ главная цель продукта сейчас — по репозиторию похоже на",
              f"  «{goal_now}» _(источник: {goal_src})_. Подтвердите или замените, и дайте цели id."]
    else:
        L += ["- _нужно ваше слово:_ какая проблема продукта главная сейчас. Из кода это не",
              "  выводится, и выдумывать я это не буду."]
    L += ["",
          "## Следующий результат", "",
          "Что изменится **для пользователя** после ближайшей работы.", "",
          "- _нужно ваше слово:_ один проверяемый результат глазами пользователя.",
          "",
          "## Дальше", "",
          "- _нужно ваше слово:_ крупная возможность, к которой идёте после ближайшего результата.",
          "",
          "## Later", "",
          "Что осознанно НЕ берём сейчас — записанное решение тоже артефакт.", "",
          "- _нужно ваше слово:_ идея, которую вы сознательно отложили.",
          "",
          f"<!-- класс репозитория на момент онбординга: {cls} -->",
          ""]
    return "\n".join(L)


def _plan_text(items: list) -> str:
    """Delivery plan из работ по закрытию пробелов. Без `template: true`: это НЕ пример."""
    head = ("# planning/plan.yaml — delivery plan репозитория.\n"
            "#\n"
            "# Первую версию собрал `ai-ops bootstrap` из аудита репозитория: каждая работа ниже\n"
            "# соответствует контуру, чей источник истины отсутствует или устарел. Это не пример и\n"
            "# не шаблон — поэтому строки `template: true` здесь нет, и `ai-ops next` советует по\n"
            "# этому файлу как по настоящему плану.\n"
            "#\n"
            "# Дальше он ваш: добавляйте продуктовую работу, меняйте порядок, снимайте лишнее.\n"
            "# Статусы `ready`/`blocked`/`waiting` НЕ объявляйте — кит считает их из зависимостей,\n"
            "# гейтов и активной работы.\n")
    doc = {"schema_version": 1, "kind": "delivery-plan",
           "goals": [{"id": BASELINE_GOAL, "status": "active",
                      "outcome": {"every_contour_has_a_source_of_truth": False}}],
           "work": items}
    return head + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100)


def plan(child_root, understanding: dict | None = None, model: dict | None = None) -> dict:
    """Что bootstrap СОБИРАЕТСЯ создать. Ничего не пишет.

    Сухой прогон — не вежливость, а требование: запись артефактов в чужой репозиторий владелец
    должен увидеть до того, как она произошла.
    """
    root = Path(child_root)
    model = model or _contours.load_model()
    understanding = understanding or _audit.run(root)
    items = work_items(understanding, model, root)

    rm_rel = _roadmap.roadmap_rel(root)
    pl_rel = _plan.plan_rel(root)
    existing_plan = None
    try:
        existing_plan = _plan.load(root)
    except _plan.PlanCorrupt:
        existing_plan = "corrupt"

    actions = []
    rm_exists = (root / rm_rel).is_file()
    actions.append({
        "path": rm_rel, "what": "направление продукта (четыре горизонта)",
        "exists": rm_exists, "will_write": not rm_exists,
        "why": ("уже есть — не трогаю: существующий файл сильнее любого шаблона" if rm_exists
                else "направление не является артефактом, поэтому «что важнее сейчас» "
                     "не на чём считать"),
        "provenance": "факты репозитория + пометки «нужно ваше слово» там, где фактов нет"})

    # Заготовку кита ЗАМЕНЯЕМ: она не содержит фактов о продукте и прямо помечена как пример.
    # Настоящий план — нет: он мог быть написан руками, и перезапись уничтожила бы работу человека.
    plan_is_template = bool(existing_plan) and existing_plan != "corrupt" \
        and _plan.is_template(existing_plan)
    pl_exists = (root / pl_rel).is_file()
    actions.append({
        "path": pl_rel, "what": f"план работ: {len(items)} " + _q(len(items)),
        "exists": pl_exists,
        "will_write": (not pl_exists) or plan_is_template,
        "replaces_template": plan_is_template,
        "why": ("в файле лежит заготовка кита (пример работы) — заменю её планом из фактов"
                if plan_is_template else
                "уже есть настоящий план — не трогаю" if pl_exists else
                "работа не объявлена, поэтому «что взять следующим» не из чего выбирать"),
        "provenance": "аудит контуров: каждая работа — незакрытый источник истины"})

    open_questions = [q for q in understanding["ask"]["questions"] if q.get("blocks_work")]
    # Сколько работ НАЧИНАЮТСЯ с ответа владельца — отдельное число: если все, то обещать «спроси
    # что дальше» нельзя, там будет «ждёт решения человека», и сказать об этом надо сразу.
    awaiting = [w["id"] for w in items if w.get("human_decision")]
    return {"schema_version": 1, "kind": "bootstrap-plan",
            "actions": actions, "work_items": items,
            "awaiting_human": awaiting,
            "blocking_questions": open_questions,
            "plan_corrupt": existing_plan == "corrupt",
            "will_write": [a["path"] for a in actions if a["will_write"]],
            "skipped": [a["path"] for a in actions if not a["will_write"]]}


def _q(n):
    if n % 10 == 1 and n % 100 != 11:
        return "работа"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "работы"
    return "работ"


def apply(child_root, boot: dict | None = None, understanding: dict | None = None) -> dict:
    """Создать то, что перечислено в `plan()`. -> отчёт о записанном.

    FAIL-CLOSED на битом плане: перезаписать неразобранный файл значило бы уничтожить работу,
    которую кит не смог прочитать.
    """
    root = Path(child_root)
    # Аудит считается ОДИН раз на команду: он обходит дерево, и повторный проход ради тех же
    # фактов — та же расточительность, что обход на каждый контур (тир 3).
    understanding = understanding or _audit.run(root)
    boot = boot or plan(root, understanding)
    if boot["plan_corrupt"]:
        return {"schema_version": 1, "kind": "bootstrap-result", "written": [], "skipped": [],
                "error": "план в репозитории не разбирается — не перезаписываю: сначала починим файл"}
    written, skipped = [], []
    for a in boot["actions"]:
        target = root / a["path"]
        if not a["will_write"]:
            skipped.append({"path": a["path"], "why": a["why"]})
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if a["path"] == _roadmap.roadmap_rel(root):
            target.write_text(_roadmap_text(understanding), encoding="utf-8")
        else:
            target.write_text(_plan_text(boot["work_items"]), encoding="utf-8")
        written.append({"path": a["path"], "what": a["what"]})
    return {"schema_version": 1, "kind": "bootstrap-result",
            "written": written, "skipped": skipped, "error": None,
            "work_items": len(boot["work_items"]),
            "awaiting_human": len(boot["awaiting_human"]),
            "ready_without_human": len(boot["work_items"]) - len(boot["awaiting_human"]),
            "blocking_questions": len(boot["blocking_questions"])}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="product_bootstrap.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "apply"):
        s = sub.add_parser(name)
        s.add_argument("repo")
        s.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])
    root = Path(ns.repo)
    try:
        und = _audit.run(root)
        boot = plan(root, und)
    except (_contours.ModelCorrupt, _plan.PlanCorrupt) as e:
        print(f"ОШИБКА: {e}")
        return 1
    rep = boot if ns.cmd == "plan" else apply(root, boot, und)
    if ns.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if not rep.get("error") else 1
    if ns.cmd == "plan":
        print(f"BOOTSTRAP (сухой прогон): создам {len(rep['will_write'])}, пропущу "
              f"{len(rep['skipped'])}")
        for a in rep["actions"]:
            mark = "+" if a["will_write"] else "·"
            print(f"  {mark} {a['path']} — {a['what']}: {a['why']}")
        for w in rep["work_items"][:5]:
            print(f"      работа: {w['id']} ({w['owner_role']}) — {w['title']}")
        return 0
    if rep.get("error"):
        print(f"ОШИБКА: {rep['error']}")
        return 1
    for w in rep["written"]:
        print(f"  + {w['path']} — {w['what']}")
    for s in rep["skipped"]:
        print(f"  · {s['path']} — {s['why']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
