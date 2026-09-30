"""Состояние проекта в первом результате: «в порядке, срочно править нечего» или «вот что поправить».

ПОВОД (#1200, репетиция первого часа на «Нитях» 29.09). После установки владелец видел план работ,
но не главное: в каком состоянии проект СЕЙЧАС. Сверка с конституцией на «Нитях» прошла чисто, но
вывод «расхождений нет» лежал в отдельном отчёте и одной строке онбординга; сканер безопасности в
первый час не входил вовсе.

ЭТО НЕ НОВАЯ ПРОВЕРКА. Здесь связаны уже существующие: `checks.constitution_conformance` (устройство
кода), `security.security_scan.scan_repo` (секреты и опасные места в коде продукта) и
`shared.project_detector` (какими командами проект проверяется). Связка живёт на слое команд:
первый час (`planning`) остаётся без зависимости на сканер, он только печатает готовый итог.

ГРАНИЦА ЧЕСТНОСТИ. Итог говорит о коде и безопасности — о том, что РЕАЛЬНО проверено при установке.
Тесты и сборку проекта установка не запускает, поэтому про них сказано «кит знает, чем проверять», а
не «проходят». Проверка, которая не смогла пройти или которой кит не видит, называется непроверенной
и снимает с итога слово «всё» — «в порядке, но проверено не всё».
"""
from __future__ import annotations

OK, ATTENTION, URGENT, NOT_CHECKED, INFO = "ok", "attention", "urgent", "not_checked", "info"

_CONFORMANCE_REPORT = ".ai/project/constitution-conformance.md"
_COMMAND_NAMES = {"build": "сборка", "lint": "стиль кода", "typecheck": "проверка типов",
                  "test": "тесты"}


def _code_item(child_root) -> dict:
    """Устройство кода — сверка с автоматизируемыми статьями конституции."""
    try:
        from ai_ops_kit.checks import constitution_conformance as _cc
        findings = _cc.conform(child_root)
    except (ImportError, OSError, ValueError) as exc:
        return {"area": "Устройство кода", "status": NOT_CHECKED,
                "text": f"проверить не удалось ({type(exc).__name__})"}
    if not findings:
        return {"area": "Устройство кода", "status": OK,
                "text": "расхождений с правилами устройства кода нет"}
    n = sum(f.get("count", 1) for f in findings)
    return {"area": "Устройство кода", "status": ATTENTION,
            "text": f"{n} рекомендаций к рассмотрению по {len(findings)} правилам — это не блок "
                    f"(подробно: {_CONFORMANCE_REPORT})"}


def _security_item(child_root) -> dict:
    """Безопасность — сканер кита по всему дереву; в итог идёт только код продукта, не обвязка."""
    try:
        from ai_ops_kit.security import security_scan as _ss
        rep = _ss.scan_repo(child_root)
    except (ImportError, OSError, ValueError) as exc:
        return {"area": "Безопасность", "status": NOT_CHECKED,
                "text": f"проверить не удалось ({type(exc).__name__})"}
    secrets = rep.get("secrets") or []
    product = [f for f in rep.get("injection_flags") or [] if f.get("area") != "harness"]
    if secrets:
        return {"area": "Безопасность", "status": URGENT,
                "text": f"похоже на секрет в репозитории: {len(secrets)} — проверьте и уберите "
                        f"ключи из кода"}
    if product:
        where = ", ".join(f"{f.get('path')}:{f.get('line')}" for f in product[:3])
        more = f" и ещё {len(product) - 3}" if len(product) > 3 else ""
        return {"area": "Безопасность", "status": ATTENTION,
                "text": f"секретов нет; {len(product)} мест в коде продукта стоит перечитать "
                        f"(запуск команд, вставка HTML): {where}{more}"}
    return {"area": "Безопасность", "status": OK,
            "text": "секретов и опасных мест в коде продукта не найдено"}


def _checks_item(child_root) -> dict:
    """Какими командами проект проверяется. Установка их НЕ запускает — так и сказано."""
    try:
        from ai_ops_kit.shared import project_detector
        prof = project_detector.detect(child_root)
    except (ImportError, OSError, ValueError) as exc:
        return {"area": "Проверки проекта", "status": NOT_CHECKED,
                "text": f"определить не удалось ({type(exc).__name__})"}
    known, missing = [], []
    for stack in prof.get("stacks") or []:
        for key, name in _COMMAND_NAMES.items():
            cmd = (stack.get("commands") or {}).get(key)
            (known if cmd else missing).append(name)
    known = list(dict.fromkeys(known))
    missing = [m for m in dict.fromkeys(missing) if m not in known]
    if not known:
        return {"area": "Проверки проекта", "status": NOT_CHECKED,
                "text": "команд проверки кит не нашёл — проверять изменения ему будет нечем"}
    text = (f"кит знает, чем проверять: {', '.join(known)} — прогонит их в каждой работе "
            f"(при установке не запускались)")
    if missing:
        return {"area": "Проверки проекта", "status": NOT_CHECKED,
                "text": f"{text}; не нашёл: {', '.join(missing)} — эту часть проводить не сможет"}
    return {"area": "Проверки проекта", "status": INFO, "text": text}


def verdict(items: list[dict]) -> tuple[str, bool]:
    """Итог одной фразой + проверено ли всё. Сильнейший статус решает, непроверенное — оговаривается."""
    urgent = [i for i in items if i["status"] == URGENT]
    attention = [i for i in items if i["status"] == ATTENTION]
    unchecked = [i for i in items if i["status"] == NOT_CHECKED]
    if urgent:
        head = "Стоит поправить: " + "; ".join(f"{i['area'].lower()} — {i['text']}" for i in urgent) + "."
    elif attention:
        head = ("Срочно править нечего; посмотреть стоит: "
                + ", ".join(i["area"].lower() for i in attention) + ".")
    else:
        head = "По коду и безопасности проект в порядке — срочно править нечего."
    if unchecked:
        head += " Проверено не всё: " + ", ".join(i["area"].lower() for i in unchecked) + "."
    return head, not unchecked


def assess(child_root) -> dict:
    """-> {"verdict": str, "complete": bool, "items": [{area, status, text}]} — только чтение."""
    items = [_code_item(child_root), _security_item(child_root), _checks_item(child_root)]
    head, complete = verdict(items)
    return {"verdict": head, "complete": complete, "items": items}
