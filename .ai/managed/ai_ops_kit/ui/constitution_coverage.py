#!/usr/bin/env python3
"""constitution_coverage.py — покрытие UI/UX-Конституции проверками кита (машинно из rules.yaml).

Углубляет UI-health и ревьюеров ИЗ Конституции, а не рядом с ней. Раньше UI-health проверял
только НАЛИЧИЕ Storybook, а правила Конституции жили отдельно от того, что реально проверяет
ревьюер/health. Здесь связь становится машинной и проверяемой:

- ИСТОЧНИК — `standards/uiux/rules.yaml` (машинный реестр правил, генерится из `UI_CONSTITUTION.md`);
  семантику правил модуль НЕ переписывает, только читает.
- Что Конституция САМА объявляет автоматизируемым — поле `validation.automated: true` у правила.
  Это не выдуманная «глубина»: список машинно-проверяемых правил берётся из реестра, а не из головы.
- Покрытие ревьюерами выводится из `rules/design/*.yaml`: пункты чек-листов несут `constitution_id`
  (механизм работы `design-gates-cite-constitution-ids`, #518). Цитата ревьюера резолвится в правило.
- Каждая health-проверка/находка ссылается на стабильный `constitution_id`, резолвящийся в реестр;
  висячих (выдуманных) ID быть не может — `check()` fail-closed.

ЧЕСТНАЯ ГРАНИЦА (не фабрикуем глубину): правило, которое Конституция объявляет автоматизируемым,
но за которым ПОКА нет проверки-двойника в ките, попадает в `automated_uncovered` и называется
ПОИМЁННО. КАК именно его проверять — дизайн-решение владельца; модуль не выдумывает семантику,
он лишь честно показывает разрыв между «Конституция считает это автоматизируемым» и «в ките есть
проверка». Расхождения зафиксированы прозой в `standards/uiux/gate-reconciliation.md`.
"""
from __future__ import annotations

from pathlib import Path

import yaml

SENTINEL_NONE = "none"
_RULES_REL = ("standards", "uiux", "rules.yaml")
_DESIGN_REL = ("rules", "design")


def _find_up(*rel_parts: str, start: Path | None = None) -> Path | None:
    """Найти путь `*rel_parts` вверх по дереву от `start` (по умолчанию — каталог этого модуля).

    Работает и в репозитории кита, и в дочке под `.ai/managed`: ищем ровно тот же относительный
    путь, что уезжает в поставку."""
    here = (start or Path(__file__).resolve()).resolve()
    for base in (here, *here.parents):
        cand = base.joinpath(*rel_parts)
        if cand.exists():
            return cand
    return None


def rules_path(start: Path | None = None) -> Path | None:
    """Путь к доставляемому реестру Конституции или None (тогда health честно скажет «не проверено»)."""
    return _find_up(*_RULES_REL, start=start)


def design_dir(start: Path | None = None) -> Path | None:
    """Каталог машиночитаемых чек-листов дизайн-гейтов (`rules/design/`) или None."""
    return _find_up(*_DESIGN_REL, start=start)


def load_registry(path: Path) -> dict[str, dict]:
    """{id: правило} из реестра Конституции. Пустой реестр -> {} (резолв станет fail-closed, не зелёным)."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {r["id"]: r for r in doc.get("rules", []) or [] if isinstance(r, dict) and r.get("id")}


def automated_rule_ids(registry: dict[str, dict]) -> set[str]:
    """ID правил, которые Конституция САМА объявляет автоматизируемыми (`validation.automated: true`)."""
    out: set[str] = set()
    for rid, rule in registry.items():
        if bool((rule.get("validation") or {}).get("automated")):
            out.add(rid)
    return out


def reviewer_cited_ids(design: Path) -> set[str]:
    """`constitution_id`, которые ревьюеры МОГУТ процитировать: из пунктов всех `rules/design/*.yaml`.

    `none` не считается покрытием (у пункта нет правила-двойника — это осознанное расхождение)."""
    cited: set[str] = set()
    for p in sorted(design.glob("*.yaml")):
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for item in doc.get("items", []) or []:
            cid = item.get("constitution_id") if isinstance(item, dict) else None
            if cid and cid != SENTINEL_NONE:
                cited.add(cid)
    return cited


def resolves(constitution_id: str, valid_ids: set[str]) -> bool:
    """Страж привязки (fail-closed): валиден только явный `none` ИЛИ реальный ID реестра.

    Ровно эту функцию используют и позитивные проверки, и fail-closed тест — иначе fail-closed
    доказывал бы тавтологию «строка in множество», а не поведение самого стража."""
    if constitution_id == SENTINEL_NONE:
        return True
    return constitution_id in valid_ids


def coverage(rules_p: Path | None = None, design_d: Path | None = None) -> dict:
    """Машинное покрытие Конституции проверками кита. Всё выведено из файлов, ничего не захардкожено.

    Возвращает отчёт, где каждая health-проверка (`checks[*]`) привязана к `constitution_id`,
    резолвящемуся в реестр; `automated_uncovered` называет правила без проверки-двойника (дизайн-
    решение владельца), `dangling` держит fail-closed (цитата, не резолвящаяся в реестр)."""
    rp = rules_p or rules_path()
    dd = design_d or design_dir()
    if rp is None or dd is None:
        raise FileNotFoundError("реестр Конституции или каталог дизайн-чек-листов не найдены")

    registry = load_registry(rp)
    valid = set(registry)
    automated = automated_rule_ids(registry)
    cited = reviewer_cited_ids(dd)

    # Висячие: то, что ревьюер цитирует, но реестр не раскрывает. В норме пусто; непусто -> дефект связи.
    dangling = sorted(cid for cid in cited if not resolves(cid, valid))
    covered = sorted(cid for cid in cited if cid in valid)
    automated_covered = sorted(automated & cited)
    automated_uncovered = sorted(automated - cited)

    checks = []
    for rid in sorted(automated):
        rule = registry[rid]
        wired = rid in cited
        checks.append({
            "constitution_id": rid,
            "title": rule.get("title", ""),
            "level": rule.get("level"),
            "category": rule.get("category"),
            "automated": True,
            "reviewer_wired": wired,
            # Статус честен: либо проверка-двойник у ревьюера есть, либо КАК проверять — дизайн-решение.
            "status": "reviewer_wired" if wired else "design_decision_pending",
        })

    return {
        "kind": "ConstitutionCoverage",
        "source": "/".join(_RULES_REL),
        "rules_total": len(registry),
        "automated_total": len(automated),
        "reviewer_covered": covered,
        "automated_covered": automated_covered,
        "automated_uncovered": automated_uncovered,
        "dangling": dangling,
        "checks": checks,
    }


def check(report: dict) -> list[str]:
    """Валидация отчёта покрытия + честность (fail-closed: нет висячих; каждая проверка резолвится)."""
    e: list[str] = []
    if not isinstance(report, dict) or report.get("kind") != "ConstitutionCoverage":
        return ["kind должен быть ConstitutionCoverage"]
    if report.get("dangling"):
        e.append(f"висячие constitution_id (не резолвятся в реестр): {report['dangling']}")
    # Каждая health-проверка обязана нести constitution_id (привязка к Конституции, не текст).
    for c in report.get("checks", []) or []:
        if not c.get("constitution_id"):
            e.append(f"health-проверка без constitution_id: {c!r}")
        if c.get("status") not in ("reviewer_wired", "design_decision_pending"):
            e.append(f"недопустимый status у {c.get('constitution_id')!r}: {c.get('status')!r}")
    return e


def summary(report: dict) -> dict:
    """Компактный срез покрытия для UI-health (ui_readiness). Каждый ID резолвится в реестр."""
    return {
        "available": True,
        "source": report["source"],
        "rules_total": report["rules_total"],
        "automated_total": report["automated_total"],
        "reviewer_covered_count": len(report["reviewer_covered"]),
        "automated_covered": report["automated_covered"],
        "automated_uncovered": report["automated_uncovered"],
        "note": ("health выведен из Конституции: покрытые правила проверяются ревьюером, "
                 "непокрытые автоматизируемые названы поимённо (как проверять — дизайн-решение владельца)"),
    }


def unavailable(reason: str) -> dict:
    """Честный «не проверено», когда реестр недоступен — absent не маскируется под ok."""
    return {"available": False, "reason": reason}
