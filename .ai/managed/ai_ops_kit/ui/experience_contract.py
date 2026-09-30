#!/usr/bin/env python3
"""Experience Contract — формат описания опыта + генератор stories.

Experience Contract описывает:
- Задачу пользователя (что хочет достичь)
- Flow (шаги взаимодействия)
- Экраны и состояния
- Роли (кто что видит)
- Тексты (microcopy)
- Responsive breakpoints
- Accessibility требования
- Компоненты и токены
- События аналитики
- Нерешённые вопросы
- Осознанные компромиссы

Из контракта КОДОМ выводится список обязательных stories для Storybook.

Использование:
    experience_contract.py <contract.yaml> [--output stories.json]
    experience_contract.py --selftest
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml


# Схема Experience Contract
# ОБЯЗАТЕЛЬНЫЕ СОСТОЯНИЯ — ОДИН ИСТОЧНИК НА ОБЕ СТОРОНЫ МЕХАНИЗМА (20.08.2026).
#
# ЗАМЕР. Сторона ДОКАЗАТЕЛЬСТВА (`ui/storybook_adapter.py`) требует покрытия четырёх состояний
# (`REQUIRED_STATES`: default/loading/empty/error) и не даёт выдать «нет данных» за «чисто».
# Сторона СОЗДАНИЯ выводила состояния ИЗ КОНТРАКТА — только те, что владелец перечислил. Связи
# между половинами не было: контракт, в котором не описали `error`, порождал набор stories,
# который гейт НЕ МОЖЕТ принять никогда, и узнавалось это на гейте, а не при создании.
#
# Это ровно тот класс, против которого стоит кит: две правды об одном вопросе. Здесь их сводим —
# список берётся у стороны доказательства, а не объявляется второй раз.
from ai_ops_kit.ui.storybook_adapter import REQUIRED_STATES

CONTRACT_SCHEMA = {
    "id": str,              # Уникальный идентификатор
    "title": str,           # Название опыта
    "user_goal": str,       # Что хочет достичь пользователь
    "context": str,         # Контекст использования
    "roles": list,          # Роли: [{name, permissions, views}]
    "flow": list,           # Шаги: [{step, action, screen, state}]
    "screens": list,        # Экраны: [{id, name, components, states}]
    "states": list,         # Состояния: [{name, condition, visual}]
    "microcopy": dict,      # Тексты: {key: text}
    "responsive": list,     # Breakpoints: [{name, min_width, layout}]
    "accessibility": list,  # Требования: [wcag_level, features]
    "components": list,     # Используемые компоненты
    "tokens": dict,         # Дизайн-токены: {color, spacing, typography}
    "analytics": list,      # События: [{event, trigger, data}]
    "open_questions": list, # Нерешённые вопросы
    "tradeoffs": list,      # Осознанные компромиссы
}


def validate_contract(contract: dict) -> list[str]:
    """Проверить контракт: форма + выполнимость на гейте доказательства. -> список ошибок."""
    errors = []
    for field, expected_type in CONTRACT_SCHEMA.items():
        if field not in contract:
            errors.append(f"нет обязательного поля: {field}")
        elif not isinstance(contract[field], expected_type):
            errors.append(f"поле {field} должно быть {expected_type.__name__}")
    return errors


def undeclared_required_states(contract: dict) -> list[str]:
    """Обязательные состояния, которых владелец НЕ описал. -> список имён.

    Это НЕ ошибка контракта: stories для них всё равно будут выведены (см. `generate_stories`),
    и гейт доказательства их примет. Но заглушка без `condition`/`visual` — это вопрос владельцу,
    а не решённая задача, и он обязан быть ВИДЕН, а не растворён в сгенерированном списке.
    Молчаливая заглушка — тот же класс, что «unknown, свёрнутый в not_changed».
    """
    declared = {str(st.get("name")) for st in (contract.get("states") or []) if st.get("name")}
    return [s for s in REQUIRED_STATES if s not in declared]


def generate_stories(contract: dict) -> list[dict]:
    """Generate Storybook stories from Experience Contract.

    Каждая story = один экран + одно состояние + одна роль.
    """
    stories = []
    screens = contract.get("screens", [])
    states = contract.get("states", [])
    roles = contract.get("roles", [])

    # Базовые stories: каждый экран в каждом состоянии
    for screen in screens:
        screen_id = screen.get("id", "unknown")
        screen_name = screen.get("name", screen_id)

        # Default state
        stories.append({
            "id": f"{screen_id}-default",
            "title": f"{screen_name} — Default",
            "screen": screen_id,
            "state": "default",
            "role": None,
            "components": screen.get("components", []),
            "parameters": {
                "design": contract.get("tokens", {}),
            },
        })

        # ОБЯЗАТЕЛЬНЫЕ СОСТОЯНИЯ ВЫВОДЯТСЯ КОДОМ, а не берутся из того, что вспомнил автор.
        # Прежде состояния приходили ТОЛЬКО из контракта, и если владелец не описал `error`,
        # story для него не появлялась — а гейт доказательства его требовал. Работа обещает
        # «обязательные stories выводятся из контракта КОДОМ»; вывод из перечисления — это не
        # вывод, это переписывание списка.
        # Описанное владельцем состояние побеждает: у него есть condition/visual, и он знает про
        # свой продукт больше. Недостающие добавляются заглушкой с пометкой, ОТКУДА они взялись.
        declared = {str(st.get("name")): st for st in states if st.get("name")}
        for req in REQUIRED_STATES:
            declared.setdefault(req, {"name": req, "derived_from": "REQUIRED_STATES",
                                      "condition": None, "visual": None})
        for state_name, state in declared.items():
            stories.append({
                "id": f"{screen_id}-{state_name}",
                "title": f"{screen_name} — {state_name}",
                "screen": screen_id,
                "state": state_name,
                "role": None,
                "components": screen.get("components", []),
                "parameters": {
                    "design": contract.get("tokens", {}),
                    "state": state,
                },
            })

        # Each role
        for role in roles:
            role_name = role.get("name", "unknown")
            stories.append({
                "id": f"{screen_id}-{role_name}",
                "title": f"{screen_name} — {role_name}",
                "screen": screen_id,
                "state": "default",
                "role": role_name,
                "components": screen.get("components", []),
                "parameters": {
                    "design": contract.get("tokens", {}),
                    "role": role,
                },
            })

    # Responsive stories
    responsive = contract.get("responsive", [])
    if responsive and screens:
        first_screen = screens[0]
        for bp in responsive:
            bp_name = bp.get("name", "unknown")
            stories.append({
                "id": f"{first_screen.get('id')}-responsive-{bp_name}",
                "title": f"{first_screen.get('name')} — Responsive {bp_name}",
                "screen": first_screen.get("id"),
                "state": "default",
                "role": None,
                "components": first_screen.get("components", []),
                "parameters": {
                    "viewport": bp,
                    "design": contract.get("tokens", {}),
                },
            })

    # ВЫБОР ОПЫТА — ВХОД ДЛЯ STORIES. Если владелец выбрал ОДИН вариант опыта
    # (`record_experience_choice`), выбор проходит в КАЖДУЮ story: она рисуется под выбранное
    # решение, а не под «какой-то из предложенных». Пока выбор не сделан — ключа нет, и это видно
    # (отсутствие ≠ выбран дефолт). Обязательный НАБОР stories при этом не меняется (экран ×
    # состояние) — детерминированный гейт ux_review остаётся прежним; выбор влияет на СОДЕРЖАНИЕ
    # story, а не на её наличие.
    choice = chosen_experience_option(contract)
    if choice:
        for st in stories:
            st.setdefault("parameters", {})["experience"] = choice["option"]

    return stories


def required_story_specs(contract: dict) -> list[dict]:
    """Обязательные stories, ВЫВЕДЕННЫЕ КОДОМ: каждый экран контракта × каждое обязательное состояние.

    Экраны берутся из контракта (их описывает владелец), а обязательные состояния — из одного
    общего источника `REQUIRED_STATES`, а НЕ из того, что владелец вспомнил перечислить. В этом и
    состоит «выводятся кодом, а не пишутся руками»: набор нельзя недосоставить, забыв `error`.

    Это стабильный контракт обязательного набора, по которому сторона доказательства
    (`storybook_adapter`) сверяет собранный Storybook. Возвращает список
    `{id, screen, screen_name, state}`.
    """
    specs = []
    for screen in contract.get("screens", []) or []:
        sid = str(screen.get("id", "unknown"))
        sname = str(screen.get("name", sid))
        for state in REQUIRED_STATES:
            specs.append({"id": f"{sid}-{state}", "screen": sid,
                          "screen_name": sname, "state": state})
    return specs


def _norm_token(s) -> str:
    """Имя для сравнения: lower + только буквы/цифры. `active-work` и `ActiveWork` -> `activework`.

    Индекс Storybook и id контракта пишут разделители по-разному (kebab в контракте, PascalCase в
    title, слэши в importPath). Сравнение по «сырому» id разошлось бы на первом же дефисе.
    """
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def _index_story_hay(story: dict) -> str:
    """Нормализованная «сенная копна» одной story из индекса Storybook: id+title+name+importPath."""
    parts = (story.get("id", ""), story.get("title", ""),
             story.get("name", ""), story.get("importPath", ""))
    return _norm_token(" ".join(str(p) for p in parts))


def required_stories_coverage(contract: dict, index_stories: list) -> dict:
    """Сверка обязательного набора stories (из контракта, кодом) с СОБРАННЫМ Storybook.

    `index_stories` — распарсенный story-index child-репо (`storybook_adapter._parse_story_index`).
    Обязательная story считается присутствующей, если в индексе есть story, чья копна содержит и
    экран, и состояние (по нормализованному вхождению). Непустой `missing` -> объявленного
    контрактом опыта в Storybook НЕТ, и сторона доказательства обязана краснеть, а не молчать.

    -> {"required": [id...], "present": [id...], "missing": [id...], "complete": bool}
    """
    specs = required_story_specs(contract)
    hays = [_index_story_hay(s) for s in (index_stories or []) if isinstance(s, dict)]
    present, missing = [], []
    for spec in specs:
        sc, st = _norm_token(spec["screen"]), _norm_token(spec["state"])
        found = bool(sc) and bool(st) and any(sc in h and st in h for h in hays)
        (present if found else missing).append(spec["id"])
    return {"required": [s["id"] for s in specs], "present": present,
            "missing": missing, "complete": not missing}


def missing_required_stories(contract: dict, index_stories: list) -> list[str]:
    """Обязательные stories из контракта, которых НЕТ в собранном Storybook. -> список id."""
    return required_stories_coverage(contract, index_stories)["missing"]


def generate_design_options(contract: dict) -> list[dict]:
    """Generate 2-3 design options with trade-offs.

    UI-UX designer предлагает варианты, а не один «правильный» макет.
    """
    # ВАРИАНТ ОБЯЗАН БЫТЬ ПРО ЭТОТ КОНТРАКТ (фаза 0, 19.08.2026). Здесь `user_goal` и `flow`
    # читались и НЕ использовались ни разу — функция возвращала три одинаковых текста на любой
    # вход. Это поймал линтер (F841: «имя обещает логику, которой нет»), и находка настоящая:
    # «предложить варианты для контракта» и «напечатать три заготовки» — разные обещания, а
    # подпись функции и её докстрока обещали первое.
    user_goal = str(contract.get("user_goal") or "").strip()
    flow = contract.get("flow") or []
    goal = user_goal or "цель пользователя в контракте не названа"
    steps = len(flow)
    # Шаги называем словами контракта, а не выдумываем: без них вопрос остаётся общим и это видно.
    step_names = [str(s.get("name") or s.get("step") or s) if isinstance(s, dict) else str(s)
                  for s in flow]

    def _q_hidden():
        if steps <= 1:
            return "Поток в контракте не расписан — какие шаги есть на самом деле?"
        return (f"В потоке {steps} шаг(ов) ({', '.join(step_names[:4])}) — "
                f"какие показывать сразу, какие раскрывать по требованию?")

    options = [
        {
            "id": "minimal",
            "name": "Minimal",
            "description": f"Минимальный интерфейс: на экране только то, что ведёт к «{goal}»",
            "tradeoffs": {
                "pros": ["Быстрое освоение", "Меньше когнитивной нагрузки"],
                "cons": ["Меньше функций видимо", "Может потребовать больше кликов"],
            },
            "questions": [f"Достаточно ли этого для power users, если цель — «{goal}»?"],
        },
        {
            "id": "progressive",
            "name": "Progressive Disclosure",
            "description": f"Сложность раскрывается по мере необходимости; ядро — «{goal}»",
            "tradeoffs": {
                "pros": ["Подходит новичкам и экспертам", "Чистый интерфейс"],
                "cons": ["Сложнее реализовать", "Может скрыть важные функции"],
            },
            "questions": [_q_hidden()],
        },
        {
            "id": "dashboard",
            "name": "Dashboard",
            "description": (f"Всё на одном экране: {steps} шаг(ов) потока видны сразу"
                            if steps else "Всё на одном экране (поток в контракте не расписан)"),
            "tradeoffs": {
                "pros": ["Всё видно сразу", "Для power users"],
                "cons": ["Высокая когнитивная нагрузка", "Сложно для новичков"],
            },
            "questions": [f"Какие метрики критичны, чтобы судить о «{goal}»?"],
        },
    ]
    return options


def option_tradeoff(option: dict) -> str | None:
    """Названный компромисс варианта — непустой `tradeoffs.cons`. -> строка или None.

    Осознанный компромисс — это то, чем ПЛАТЯТ за вариант (`cons`). Плюсы есть у любого макета;
    вариант становится выбором только когда названа его цена. Нет цены -> None, и это ошибка,
    а не мелочь оформления (см. `check_design_options`).
    """
    if not isinstance(option, dict):
        return None
    tr = option.get("tradeoffs")
    if not isinstance(tr, dict):
        return None
    cons = [str(c).strip() for c in (tr.get("cons") or []) if str(c).strip()]
    if not cons:
        return None
    return "; ".join(cons)


def check_design_options(options: list) -> list[str]:
    """Инвариант КОДОМ, а не прозой: КАЖДЫЙ вариант несёт названный компромисс. -> список ошибок.

    Вариант без trade-off — это «вариант без осознанного компромисса»: он выглядит выбором, но не
    даёт человеку, с чем его сравнивать, и молча выдаёт один макет за взвешенное решение. Такой
    набор предлагать нельзя. Пустой список ошибок = набор можно предлагать человеку.
    """
    errors: list[str] = []
    if not options:
        errors.append("нет вариантов опыта — предлагать человеку нечего")
    for i, opt in enumerate(options or []):
        name = (isinstance(opt, dict) and (opt.get("name") or opt.get("id"))) or f"#{i}"
        if option_tradeoff(opt) is None:
            errors.append(f"вариант «{name}» без осознанного компромисса "
                          f"(пустой tradeoffs.cons) — это не выбор, а один макет")
    return errors


def offer_design_options(contract: dict) -> list[dict]:
    """Варианты опыта для контракта, каждый с НАЗВАННЫМ компромиссом. Fail-closed.

    Это точка, через которую варианты попадают в рабочий контур (`engineering_advisor`): она
    предлагает набор ТОЛЬКО если инвариант `check_design_options` выполнен. Вариант без trade-off
    не проскакивает молча — функция бросает `ValueError`, и вызывающий обязан назвать дефект
    человеку, а не показать псевдовыбор.
    """
    options = generate_design_options(contract)
    errors = check_design_options(options)
    if errors:
        raise ValueError("варианты опыта без осознанного компромисса: " + "; ".join(errors))
    return options


def record_experience_choice(contract: dict, option_id: str,
                             rationale: str | None = None) -> dict:
    """Записать ВЫБОР владельца — ОДИН вариант опыта — в Experience Contract. Fail-closed.

    Это ФАЗА ВЫБОРА, вторая половина механизма после `offer_design_options` (фаза предложения).
    Владелец выбирает из набора, который УЖЕ прошёл инвариант trade-offs: `offer_design_options`
    зовётся здесь же и на наборе без названной цены закрывает ворота (`ValueError`) — выбирать из
    псевдовыбора нельзя. Выбор варианта, которого в наборе НЕТ, — тоже ошибка, а не молчаливое
    принятие: иначе в контракт попал бы опыт, которого киту никто не предлагал, и stories вывелись
    бы под несуществующее решение.

    Возвращает НОВЫЙ контракт с полем `experience_choice` (исходный НЕ мутируется — запись выбора
    не переписывает вход задним числом). `experience_choice` = {option, name, tradeoff, rationale}:
    в контракте остаётся не только id, но и НАЗВАННАЯ цена выбранного варианта — чтобы «почему
    выбран этот» было видно рядом с «что выбрано», а не терялось.
    """
    options = offer_design_options(contract)   # fail-closed на наборе без названной цены
    by_id = {str(o.get("id")): o for o in options}
    if str(option_id) not in by_id:
        raise ValueError(
            f"вариант «{option_id}» не из предложенных ({sorted(by_id)}): выбрать можно "
            "только тот опыт, который кит предложил с названной ценой")
    chosen = by_id[str(option_id)]
    choice = {
        "option": str(option_id),
        "name": chosen.get("name"),
        "tradeoff": option_tradeoff(chosen),
        "rationale": (str(rationale).strip() or None) if rationale else None,
    }
    enriched = dict(contract)
    enriched["experience_choice"] = choice
    return enriched


def chosen_experience_option(contract: dict) -> dict | None:
    """Записанный в контракт выбор опыта, если он есть и валиден. -> dict | None.

    Валиден = dict с непустым `option`. Иначе None: «выбор ещё не сделан» не выдаём за сделанный —
    тот же принцип, что «unknown не сворачиваем в default».
    """
    if not isinstance(contract, dict):
        return None
    ch = contract.get("experience_choice")
    if isinstance(ch, dict) and str(ch.get("option") or "").strip():
        return ch
    return None


def process_contract(contract_path: Path) -> dict:
    """Process Experience Contract and generate stories + options."""
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))

    # Validate
    errors = validate_contract(contract)
    if errors:
        return {"error": "Validation failed", "errors": errors}

    # Generate
    stories = generate_stories(contract)
    options = generate_design_options(contract)

    return {
        "schema_version": 1,
        "kind": "experience-contract-output",
        "contract_id": contract.get("id"),
        "contract_title": contract.get("title"),
        "user_goal": contract.get("user_goal"),
        "stories": stories,
        "required_stories": [s["id"] for s in required_story_specs(contract)],
        "design_options": options,
        # Записанный выбор опыта (None, пока владелец не выбрал) — виден в выводе рядом со stories,
        # которые под этот выбор нарисованы.
        "experience_choice": chosen_experience_option(contract),
        "open_questions": contract.get("open_questions", []),
        "tradeoffs": contract.get("tradeoffs", []),
        "summary": {
            "total_stories": len(stories),
            "required_stories": len(required_story_specs(contract)),
            "screens": len(contract.get("screens", [])),
            "states": len(contract.get("states", [])),
            "roles": len(contract.get("roles", [])),
            "design_options": len(options),
            "experience_chosen": chosen_experience_option(contract) is not None,
        },
    }


def format_output(output: dict) -> str:
    """Format output into human-readable report."""
    lines = []

    if "error" in output:
        lines.append(f"# Error: {output['error']}\n")
        for err in output.get("errors", []):
            lines.append(f"- {err}")
        return "\n".join(lines)

    lines.append(f"# Experience Contract: {output.get('contract_title', '?')}\n")
    lines.append(f"**ID:** {output.get('contract_id', '?')}\n")
    lines.append(f"**User Goal:** {output.get('user_goal', '?')}\n")

    summary = output.get("summary", {})
    lines.append("\n## Summary\n")
    lines.append(f"- Stories generated: {summary.get('total_stories', 0)}")
    lines.append(f"- Required stories (derived by code): {summary.get('required_stories', 0)}")
    lines.append(f"- Screens: {summary.get('screens', 0)}")
    lines.append(f"- States: {summary.get('states', 0)}")
    lines.append(f"- Roles: {summary.get('roles', 0)}")
    lines.append(f"- Design options: {summary.get('design_options', 0)}")

    stories = output.get("stories", [])
    if stories:
        lines.append("\n## Generated Stories\n")
        for story in stories[:10]:  # Show first 10
            lines.append(f"- **{story['id']}**: {story['title']}")

    options = output.get("design_options", [])
    if options:
        lines.append("\n## Design Options\n")
        for opt in options:
            lines.append(f"\n### {opt['name']}\n")
            lines.append(f"{opt['description']}\n")
            lines.append(f"**Pros:** {', '.join(opt['tradeoffs']['pros'])}\n")
            lines.append(f"**Cons:** {', '.join(opt['tradeoffs']['cons'])}\n")

    questions = output.get("open_questions", [])
    if questions:
        lines.append("\n## Open Questions\n")
        for q in questions:
            lines.append(f"- {q}")

    return "\n".join(lines)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Experience Contract — формат + генератор stories")
    ap.add_argument("contract", nargs="?", help="Path to contract YAML")
    ap.add_argument("--output", "-o", help="Output file (JSON)")
    ap.add_argument("--selftest", action="store_true", help="Run self-test")
    args = ap.parse_args()

    if args.selftest:
        # ЧЕСТНЫЙ --selftest (фаза 0, 19.08.2026). Здесь печаталась строка о пройденном
        # селфтесте и три строки «... : OK» — без единого вызова проверяемых функций. То есть
        # модуль УТВЕРЖДАЛ проверку, которой не было: ровно класс «объявлено, но не
        # исполняется», против которого стоит весь кит (ср. R-31/R-32 — две фиктивные проверки
        # в валидаторах). Образец честной формы — devtools/mutation_probe.py: модуль объясняет
        # себя и называет, где лежат его настоящие проверки. Правило репозитория (AGENTS.md):
        # тест модуля живёт в tests/, а не в продакшн-модуле, который едет в child-репозиторий.
        print(__doc__)
        print("Проверки модуля — в tests/unit/ (AGENTS.md: selftest не живёт в продакшн-модуле).")
        return 0

    if not args.contract:
        ap.print_help()
        return 1

    contract_path = Path(args.contract)
    if not contract_path.exists():
        print(f"Error: {contract_path} not found", file=sys.stderr)
        return 1

    output = process_contract(contract_path)

    if args.output:
        Path(args.output).write_text(json.dumps(output, indent=2, ensure_ascii=False),
                                     encoding="utf-8")
        print(f"Output written to {args.output}")
    else:
        print(format_output(output))

    return 0


if __name__ == "__main__":
    sys.exit(main())
