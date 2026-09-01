#!/usr/bin/env python3
"""Drift между артефактами (PR-22): расхождения документация↔код, roadmap↔backlog,
backlog↔delivery, Passport↔факт.

Оркестратор пар: для каждой пары локализует обе стороны и сравнивает. Инвариант тот же, что у
health (health_common) — «не проверено» ≠ «нет дрейфа»: если хотя бы одну сторону пары прочитать
нельзя, результат пары UNKNOWN с НАЗВАННОЙ причиной (и указанием, какая лента поставит данные),
а НЕ «расхождений нет».

Сегодня сравнивается одна пара — **документация↔код**: реиспользует `repo_graph` как инвентарь
реального кода (dp-001: не свой обход дерева) и находит ссылки в документации на код-модули,
которых в коде нет. Планка — precision > recall: ссылка «висячая», только если её basename не
совпадает ни с одним файлом кода (документация сплошь ссылается на модули по имени, а не по
полному пути, — полнопутевая проверка давала бы стену ложных срабатываний). Ссылку с верным
именем, но неверным путём этот шов НЕ ловит — это следующее уточнение, не сегодняшнее.
Остальные три пары опираются на артефакты, которые строят соседние ленты
(Passport/roadmap — лента 2, backlog — лента 3, delivery-план — лента 4); до их появления пара
честно UNKNOWN. Когда сторона появится — сюда добавляется её компаратор, форма отчёта не меняется.

Использование:  python3 -m ai_ops_kit.intelligence.drift_artifacts <repo_root> [-o report.json]
Возврат 0 — успех (наличие дрейфа — это данные, а не ошибка запуска).
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ai_ops_kit.context import repo_graph

KIND = "artifact-drift-report"

DRIFT = "drift"
CLEAN = "clean"
UNKNOWN = "unknown"

# документы, которые сканируем на ссылки в код (существующие из списка)
DOC_CANDIDATES = ("README.md", "AGENTS.md", "CLAUDE.md")
DOC_DIRS = ("docs",)
# drift сверяет ТЕКУЩИЕ документы с кодом; исторические записи (changelog, change-brief) законно
# ссылаются на старые и примерные пути — их из сверки исключаем, иначе это стена ложного дрейфа.
DOC_SKIP_SUBSTRINGS = ("changelog", "change-brief")
# ссылка на код-путь внутри `обратных кавычек`, оканчивающаяся расширением кода
_CODE_REF = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*\.(?:py|js|ts|tsx|jsx))`")

# где лежат стороны пар, которые поставят соседние ленты
PASSPORT_REL = ".ai-ops/PRODUCT_PASSPORT.md"
ROADMAP_REL = ".ai-ops/ROADMAP.md"


@dataclass
class DriftResult:
    pair: str
    status: str
    reason: str
    findings: list = field(default_factory=list)

    def __post_init__(self):
        if self.status not in {DRIFT, CLEAN, UNKNOWN}:
            raise ValueError(f"неизвестный статус дрейфа {self.status!r}")
        if not self.reason or not self.reason.strip():
            raise ValueError(f"пара {self.pair!r} без причины — расхождение без объяснения бесполезно")

    def as_dict(self) -> dict:
        out = {"pair": self.pair, "status": self.status, "reason": self.reason}
        if self.findings:
            out["findings"] = self.findings
        return out


def _doc_files(root: Path) -> list:
    docs = [root / name for name in DOC_CANDIDATES if (root / name).is_file()]
    for d in DOC_DIRS:
        base = root / d
        if base.is_dir():
            docs.extend(sorted(base.rglob("*.md")))
    return [p for p in docs
            if not any(skip in str(p.relative_to(root)).lower() for skip in DOC_SKIP_SUBSTRINGS)]


def _code_inventory(root: Path):
    """Реальный код репозитория через repo_graph (не свой обход). -> (пути, basename-ы)."""
    graph = repo_graph.build_graph(root, subdirs=None)
    paths = set(graph.get("files") or {})
    basenames = {Path(p).name for p in paths}
    return paths, basenames


def docs_vs_code(root: Path) -> DriftResult:
    docs = _doc_files(root)
    if not docs:
        return DriftResult("документация↔код", UNKNOWN,
                           "документация (README/AGENTS/CLAUDE/docs) не найдена — сверить с кодом нечего")
    paths, basenames = _code_inventory(root)
    dangling = []
    seen = set()
    for doc in docs:
        try:
            text = doc.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel_doc = str(doc.relative_to(root))
        for m in _CODE_REF.finditer(text):
            ref = m.group(1)
            # «висячая» = ни полный путь (инвентарь/диск), ни basename не совпали ни с чем в коде
            if (ref in paths or (root / ref).exists()
                    or Path(ref).name in basenames):
                continue
            key = (rel_doc, ref)
            if key in seen:
                continue
            seen.add(key)
            dangling.append(f"{rel_doc}: ссылается на `{ref}`, которого нет в коде")
    if dangling:
        return DriftResult("документация↔код", DRIFT,
                           f"документация ссылается на {len(dangling)} код-путей, которых нет",
                           findings=dangling)
    return DriftResult("документация↔код", CLEAN,
                       f"проверено {len(docs)} документов — висячих ссылок на код нет")


def _pending_pair(pair: str, sides: str) -> DriftResult:
    return DriftResult(pair, UNKNOWN,
                       f"стороны пары ещё не поставляются ({sides}) — расхождение не определить, "
                       "это не «расхождений нет»")


def roadmap_vs_backlog(root: Path) -> DriftResult:
    if not (root / ROADMAP_REL).is_file():
        return _pending_pair("roadmap↔backlog", "roadmap — лента 2, backlog — лента 3")
    # roadmap появился (лента 2), но компаратор с backlog (лента 3) ещё не построен — честно unknown
    return DriftResult("roadmap↔backlog", UNKNOWN,
                       "roadmap есть, но сверка с backlog (лента 3) ещё не реализована — не проверено")


def backlog_vs_delivery(root: Path) -> DriftResult:
    return _pending_pair("backlog↔delivery", "backlog — лента 3, delivery-план — лента 4")


def passport_vs_reality(root: Path) -> DriftResult:
    if not (root / PASSPORT_REL).is_file():
        return _pending_pair("Passport↔факт", "Product Passport — лента 2")
    return DriftResult("Passport↔факт", UNKNOWN,
                       "Passport есть, но сверка с фактом (версия/milestone/health) ещё не реализована — не проверено")


def collect_pairs(root: Path) -> list:
    return [docs_vs_code(root), roadmap_vs_backlog(root),
            backlog_vs_delivery(root), passport_vs_reality(root)]


def drift_report(root: Path) -> dict:
    pairs = collect_pairs(root)
    unverified = [p.pair for p in pairs if p.status == UNKNOWN]
    return {
        "schema_version": 1,
        "kind": KIND,
        "root": str(root),
        "has_drift": any(p.status == DRIFT for p in pairs),
        "complete": len(unverified) == 0,
        "unverified": unverified,
        "pairs": [p.as_dict() for p in pairs],
    }


def main(argv) -> int:
    if not argv:
        print(__doc__)
        return 1
    root = Path(argv[0])
    if not root.is_dir():
        print(f"не каталог: {root}", file=sys.stderr)
        return 2
    report = drift_report(root)
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if "-o" in argv:
        out = Path(argv[argv.index("-o") + 1])
        out.write_text(text + "\n", encoding="utf-8")
        state = "есть дрейф" if report["has_drift"] else "дрейфа нет"
        tail = "" if report["complete"] else f", не проверено: {report['unverified']}"
        print(f"отчёт: {out} ({state}{tail})")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
