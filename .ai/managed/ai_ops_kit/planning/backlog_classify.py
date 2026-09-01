#!/usr/bin/env python3
"""Классификация GitHub Issues как операционных единиц backlog (PR-8).

Каждый Issue получает: тип, область, статус, milestone и оценочные атрибуты (impact, urgency,
effort, confidence, strategic alignment, dependencies). Ключевое требование — как у судейского
вердикта: КАЖДЫЙ вывод объясним. Классификатор кладёт `evidence` — какой сигнал (метка, слово в
заголовке, ссылка в теле) дал вывод. Мнение без объяснения непроверяемо.

ЧЕСТНОСТЬ ТРЕТЬЕГО СОСТОЯНИЯ. То, что не выводится из доступных данных, помечается `unknown` с
причиной, а не выдумывается числом. `strategic_alignment` без источника стратегии — `unknown`, а не
«средне». Когда GitHub недоступен, отчёт возвращает `ok=False` с причиной, а не пустой список задач.

CLI:
  python3 -m ai_ops_kit.planning.backlog_classify <owner/repo|путь> [--state all] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: F401

ISSUE_TYPES = ("feature", "bug", "research", "improvement", "tech-debt", "experiment", "infrastructure")
_WORD = re.compile(r"[a-zA-Zа-яА-Я0-9_-]+")

# Метки — сильнейший сигнал типа. Ключ — подстрока имени метки в нижнем регистре.
_LABEL_TYPE = {
    "bug": "bug", "defect": "bug", "regression": "bug",
    "feature": "feature", "enhancement": "feature", "feat": "feature",
    "research": "research", "spike": "research", "investigation": "research", "question": "research",
    "improvement": "improvement", "refactor": "improvement", "polish": "improvement",
    "tech-debt": "tech-debt", "techdebt": "tech-debt", "debt": "tech-debt", "cleanup": "tech-debt",
    "experiment": "experiment", "poc": "experiment", "prototype": "experiment",
    "infra": "infrastructure", "infrastructure": "infrastructure", "ci": "infrastructure",
    "build": "infrastructure", "ops": "infrastructure", "deployment": "infrastructure",
}
# Слабее меток — слова в заголовке. Сопоставляем по СЛОВАМ: stems — совпадение по префиксу
# ("падает" ← "пад"), exact — целиком (короткие/омонимичные "ci", "add"). Порядок = приоритет.
# (stems, exact, тип, человекочитаемый сигнал).
_TITLE_TYPE = [
    # NB: генерик-«починить/fix» намеренно НЕ в bug-стемах — «починить CI» это инфраструктура,
    # а не баг. Bug-сигнал — конкретный сбой (падение, краш, ошибка, регрессия), не любое «чинить».
    (("баг", "bug", "пад", "crash", "ошиб", "error", "fail", "broke", "regress", "слома"),
     (), "bug", "слово-багосигнал в заголовке"),
    (("исследов", "research", "изуч", "investigat", "выясн", "прототип", "hypothes", "гипотез"),
     ("spike",), "research", "слово-исследование в заголовке"),
    (("рефактор", "refactor", "техдолг", "очист", "cleanup", "упрост"),
     ("debt", "tech-debt"), "tech-debt", "слово-техдолг в заголовке"),
    (("деплой", "deploy", "инфра", "infra", "pipeline", "workflow"),
     ("ci", "build", "actions"), "infrastructure", "слово-инфраструктура в заголовке"),
    (("эксперимент", "experiment", "попроб"),
     ("poc",), "experiment", "слово-эксперимент в заголовке"),
    (("улучш", "improve", "доработ", "polish"),
     (), "improvement", "слово-улучшение в заголовке"),
    (("добав", "реализ", "implement", "поддержк", "нов"),
     ("add", "support", "feature", "feat"), "feature", "слово-фича в заголовке"),
]
_PRIORITY_LABEL = re.compile(r"\b(p[0-4]|priority[:\-/ ]?(high|medium|low|critical)|"
                             r"critical|urgent|blocker|high|medium|low)\b", re.I)
_AREA_LABEL = re.compile(r"^(area|component|module|scope)[:/\-](.+)$", re.I)
# Ссылки-зависимости в теле: «depends on #12», «blocked by #7», «см. #3».
_DEP_RE = re.compile(r"\b(depends on|blocked by|requires|зависит от|блокируется|after|нужен[оа]?)\b"
                     r"[^#\n]{0,20}#(\d+)", re.I)
_ANY_REF = re.compile(r"(?<![\w/])#(\d+)\b")


@dataclass
class Classification:
    number: int
    title: str
    type: str
    area: str
    status: str
    priority: str
    milestone: "str | None"
    impact: str
    urgency: str
    effort: str
    confidence: str
    strategic_alignment: str
    dependencies: list = field(default_factory=list)
    references: list = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    url: str = ""


def _labels_lower(issue: dict) -> list:
    return [str(x).lower() for x in (issue.get("labels") or [])]


def infer_type(issue: dict) -> "tuple[str, str]":
    """(тип, объяснение). Метки > заголовок > дефолт `feature` (с честной пометкой дефолта)."""
    labels = _labels_lower(issue)
    for lb in labels:
        for key, typ in _LABEL_TYPE.items():
            if key in lb:
                return typ, f"метка '{lb}' → {typ}"
    words = [w.lower() for w in _WORD.findall(issue.get("title") or "")]
    for stems, exact, typ, why in _TITLE_TYPE:
        for w in words:
            if w in exact or any(w.startswith(s) for s in stems):
                return typ, why
    return "feature", "сигналов типа нет — дефолт feature (низкая уверенность)"


def infer_area(issue: dict) -> "tuple[str, str]":
    for lb in (issue.get("labels") or []):
        m = _AREA_LABEL.match(str(lb))
        if m:
            return m.group(2).strip(), f"метка области '{lb}'"
    # Путь в теле как слабый сигнал области (первый упомянутый каталог).
    body = issue.get("body") or ""
    m = re.search(r"`?([a-z_][a-z0-9_]*/[a-z0-9_./]+)`?", body)
    if m:
        top = m.group(1).split("/")[0]
        return top, f"путь '{m.group(1)}' в теле → область '{top}'"
    return "unknown", "нет метки области и пути в теле"


def infer_priority(issue: dict) -> "tuple[str, str]":
    """Приоритет ИЗ МЕТКИ, если она есть. Полноценный расчёт приоритета — отдельная работа (PR-9),
    здесь только считывание явного сигнала, а не выдумывание."""
    for lb in _labels_lower(issue):
        m = _PRIORITY_LABEL.search(lb)
        if m:
            return _norm_priority(m.group(0)), f"метка приоритета '{lb}'"
    return "unset", "явной метки приоритета нет (расчёт — задача приоритизации, PR-9)"


def _norm_priority(token: str) -> str:
    t = token.lower()
    if any(k in t for k in ("p0", "critical", "blocker", "urgent")):
        return "critical"
    if "p1" in t or "high" in t:
        return "high"
    if "p2" in t or "medium" in t:
        return "medium"
    if "p3" in t or "p4" in t or "low" in t:
        return "low"
    return "unset"


def _level_from_labels(labels, positives, name):
    for lb in labels:
        for key in positives:
            if key in lb:
                return "high", f"метка '{lb}' → {name} высок"
    return "unknown", f"нет сигнала {name} в метках"


def infer_dependencies(issue: dict) -> "tuple[list, list, str]":
    """(зависимости, все ссылки, объяснение). Зависимость — явная формулировка depends/blocked;
    прочие #N — просто ссылки, зависимостью не считаем."""
    body = issue.get("body") or ""
    deps = sorted({int(n) for _, n in _DEP_RE.findall(body)})
    refs = sorted({int(n) for n in _ANY_REF.findall(body)} - set(deps))
    if deps:
        why = f"явные зависимости в теле: {', '.join('#'+str(d) for d in deps)}"
    elif refs:
        why = "ссылки на другие Issue есть, но без формулировки зависимости"
    else:
        why = "ссылок на другие Issue в теле нет"
    return deps, refs, why


def classify_issue(issue: dict) -> Classification:
    labels = _labels_lower(issue)
    typ, typ_why = infer_type(issue)
    area, area_why = infer_area(issue)
    prio, prio_why = infer_priority(issue)
    deps, refs, dep_why = infer_dependencies(issue)
    impact, impact_why = _level_from_labels(labels, ("impact", "critical", "major"), "impact")
    urgency, urg_why = _level_from_labels(labels, ("urgent", "blocker", "critical"), "urgency")
    effort, eff_why = _level_from_labels(labels, ("epic", "large", "xl"), "effort")
    # confidence — уверенность классификатора в ТИПЕ: метка = высокая, заголовок = средняя, дефолт = низкая.
    if "метка" in typ_why:
        confidence = "high"
    elif "дефолт" in typ_why:
        confidence = "low"
    else:
        confidence = "medium"
    return Classification(
        number=issue.get("number"),
        title=issue.get("title") or "",
        type=typ,
        area=area,
        status=issue.get("state") or "",
        priority=prio,
        milestone=issue.get("milestone"),
        impact=impact,
        urgency=urgency,
        effort=effort,
        confidence=confidence,
        strategic_alignment="unknown",
        dependencies=deps,
        references=refs,
        url=issue.get("url") or "",
        evidence={
            "type": typ_why, "area": area_why, "priority": prio_why,
            "impact": impact_why, "urgency": urg_why, "effort": eff_why,
            "dependencies": dep_why,
            "strategic_alignment": "источник стратегии (roadmap/passport) не задан — не оцениваем",
        },
    )


def classify_items(items: list) -> list:
    return [classify_issue(i) for i in items]


@dataclass
class BacklogReport:
    ok: bool
    repo: str
    source: str
    reason: str
    total: int
    by_type: dict
    items: list

    def to_dict(self) -> dict:
        d = asdict(self)
        d["items"] = [asdict(c) if isinstance(c, Classification) else c for c in self.items]
        return d


def classify_backlog(repo_or_root: str = ".", state: str = "open", client=None) -> BacklogReport:
    """Собрать Issues и классифицировать. При недоступном GitHub — ok=False с причиной, НЕ пустой."""
    from ai_ops_kit.integrations import github as gh
    client = client or gh.make_client(repo_or_root)
    res = client.issues(state=state)
    if not res.ok:
        return BacklogReport(False, getattr(client, "repo", ""), "", res.reason, 0, {}, [])
    classes = classify_items(res.items)
    by_type = {}
    for c in classes:
        by_type[c.type] = by_type.get(c.type, 0) + 1
    return BacklogReport(True, getattr(client, "repo", ""), res.source, "",
                         len(classes), by_type, classes)


# ── CLI ───────────────────────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="backlog_classify.py")
    ap.add_argument("target", nargs="?", default=".", help="owner/repo или путь к репозиторию")
    ap.add_argument("--state", default="open", choices=("open", "closed", "all"))
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])
    rep = classify_backlog(ns.target, state=ns.state)
    if ns.json:
        print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))
        return 0 if rep.ok else 2
    if not rep.ok:
        print(f"НЕ проверено: {rep.reason}")
        return 2
    print(f"Backlog {rep.repo} ({rep.source}): {rep.total} Issues")
    print("  по типам: " + ", ".join(f"{k}={v}" for k, v in sorted(rep.by_type.items())))
    for c in rep.items:
        dep = f" deps={c.dependencies}" if c.dependencies else ""
        print(f"  #{c.number} {c.type}/{c.priority} area={c.area} conf={c.confidence}{dep}")
        print(f"      почему тип: {c.evidence['type']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
