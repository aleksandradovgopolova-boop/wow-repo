#!/usr/bin/env python3
"""AI-приоритизация backlog с ОБЪЯСНЕНИЕМ вердикта и учётом override человека (PR-9, PR-20).

Приоритет считается из атрибутов классификации: impact, urgency, effort (cost), strategic alignment,
confidence и зависимостей (блокирует ли задача другие; заблокирована ли сама). Как судейский вердикт,
рекомендация БЕЗ ОБЪЯСНЕНИЯ непроверяема — поэтому у каждой задачи есть `factors` (вклад каждого
сигнала) и `explanation` (человекочитаемое «почему такой приоритет»).

ЧЕСТНОСТЬ. `unknown`-атрибут не выдаёт себя за средний молча: он входит нейтральным весом, но
понижает `confidence` рекомендации, и это НАЗВАНО. Задача из сплошных `unknown` не получает высокий
приоритет с видом уверенности.

OVERRIDE ЧЕЛОВЕКА (PR-20). Человек может переопределить приоритет; это не ошибка, а сигнал. Override
хранится в реестре (`.ai/backlog-overrides.yaml` по умолчанию) и УЧИТЫВАЕТСЯ ВПРЕДЬ: следующий прогон
его читает и не пересчитывает поверх решения человека, а при расхождении с расчётом — показывает и
то, и другое, чтобы правило человека можно было увидеть и закодировать.

CLI:
  python3 -m ai_ops_kit.planning.backlog_prioritize <owner/repo|путь> [--state all]
      [--overrides PATH] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: F401

# Уровень атрибута → вес. `unknown` — нейтральный, НЕ равен medium: он же понижает confidence.
_LEVEL = {"high": 1.0, "medium": 0.6, "low": 0.2, "unknown": 0.4, "unset": 0.4}
# Вклад факторов в пользу (benefit). Стоимость (effort) и зависимости учитываются отдельно.
_W_IMPACT, _W_URGENCY, _W_STRATEGIC = 0.45, 0.35, 0.20
_CONF_FACTOR = {"high": 1.0, "medium": 0.8, "low": 0.6, "unknown": 0.6, "unset": 0.6}
DEFAULT_OVERRIDES_REL = ".ai/backlog-overrides.yaml"


@dataclass
class Priority:
    number: int
    title: str
    score: float
    priority: str
    computed_priority: str
    confidence: str
    overridden: bool = False
    override_reason: str = ""
    factors: dict = field(default_factory=dict)
    explanation: str = ""


def _bucket(score: float) -> str:
    if score >= 0.80:
        return "critical"
    if score >= 0.58:
        return "high"
    if score >= 0.38:
        return "medium"
    return "low"


def score_issue(item, dependents: int = 0, blocked_by_open: int = 0) -> Priority:
    """Посчитать приоритет одной классифицированной задачи. `dependents` — сколько задач зависят от
    неё (блокирует), `blocked_by_open` — сколькими открытыми задачами заблокирована она сама."""
    get = (lambda k: getattr(item, k, "unknown")) if hasattr(item, "number") else item.get
    number = get("number")
    title = get("title") or ""
    impact, urgency = _LEVEL.get(get("impact"), 0.4), _LEVEL.get(get("urgency"), 0.4)
    strategic = _LEVEL.get(get("strategic_alignment"), 0.4)
    effort_lvl = get("effort")
    conf_attr = get("confidence")

    benefit = _W_IMPACT * impact + _W_URGENCY * urgency + _W_STRATEGIC * strategic
    conf = _CONF_FACTOR.get(conf_attr, 0.6)
    # Стоимость: больше усилий — ниже приоритет за единицу пользы (unknown — лёгкий штраф).
    cost_penalty = {"high": 0.18, "medium": 0.08, "low": 0.0, "unknown": 0.05, "unset": 0.05}.get(effort_lvl, 0.05)
    block_boost = min(0.30, 0.12 * dependents)         # блокирует многих — двигать раньше
    blocked_penalty = 0.15 if blocked_by_open else 0.0  # сама заблокирована — начать нельзя

    raw = benefit * conf - cost_penalty + block_boost - blocked_penalty
    score = round(max(0.0, min(1.0, raw)), 3)

    # confidence рекомендации: сколько ключевых атрибутов неизвестны.
    unknown_attrs = [k for k in ("impact", "urgency", "strategic_alignment")
                     if get(k) in ("unknown", "unset")]
    if len(unknown_attrs) >= 2:
        rec_conf = "low"
    elif unknown_attrs:
        rec_conf = "medium"
    else:
        rec_conf = "high"

    factors = {
        "impact": round(_W_IMPACT * impact, 3),
        "urgency": round(_W_URGENCY * urgency, 3),
        "strategic": round(_W_STRATEGIC * strategic, 3),
        "confidence_mult": conf,
        "cost_penalty": -cost_penalty,
        "blocking_boost": round(block_boost, 3),
        "blocked_penalty": -blocked_penalty,
    }
    parts = [
        f"польза {round(benefit, 2)} (impact/urgency/strategic) ×conf {conf}",
        f"−стоимость {cost_penalty}",
    ]
    if dependents:
        parts.append(f"+блокирует {dependents} задач ({round(block_boost, 2)})")
    if blocked_by_open:
        parts.append(f"−сама заблокирована {blocked_by_open} открытыми (нельзя начать)")
    if unknown_attrs:
        parts.append(f"неизвестны {', '.join(unknown_attrs)} → уверенность {rec_conf}")
    explanation = f"score {score} → {_bucket(score)}: " + "; ".join(parts)

    return Priority(number=number, title=title, score=score, priority=_bucket(score),
                    computed_priority=_bucket(score), confidence=rec_conf,
                    factors=factors, explanation=explanation)


# ── overrides (PR-20) ─────────────────────────────────────────────────────────────────────────

def _overrides_path(repo_or_root: str, overrides: str) -> Path:
    if overrides:
        return Path(overrides)
    root = Path(repo_or_root) if Path(repo_or_root).exists() else Path(".")
    return root / DEFAULT_OVERRIDES_REL


def load_overrides(path: Path) -> dict:
    """Читает {number: {priority, reason, by}} из YAML. Нет файла/битый — пустой словарь (не падать)."""
    if not path or not Path(path).is_file():
        return {}
    try:
        import yaml
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception:                                  # noqa: BLE001 — реестр не обязан существовать/быть валидным
        return {}
    out = {}
    for row in (data.get("overrides") or []):
        num = row.get("number")
        if num is not None:
            out[int(num)] = {"priority": row.get("priority"), "reason": row.get("reason", ""),
                             "by": row.get("by", "")}
    return out


def save_override(path: Path, number: int, priority: str, reason: str = "", by: str = "") -> None:
    """Записать решение человека в реестр — чтобы СЛЕДУЮЩИЙ прогон его учёл (PR-20: override впредь)."""
    import yaml
    path = Path(path)
    data = {}
    if path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = [r for r in (data.get("overrides") or []) if r.get("number") != number]
    rows.append({"number": int(number), "priority": priority, "reason": reason, "by": by})
    data["overrides"] = sorted(rows, key=lambda r: r["number"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _apply_learning(root: Path, p: Priority) -> Priority:
    """Мягкий сдвиг РАСЧЁТНОГО приоритета из прошлых human-override'ов (learning-from-human-overrides).

    В отличие от `_apply_override` (жёсткое решение человека по ЭТОЙ задаче), здесь прошлые
    governance-override'ы (`decisions/registry.yaml`) сдвигают score похожих задач в сторону
    прежних решений человека. Сдвиг ограничен (`override_learning.MAX_SHIFT`) и ВСЕГДА назван в
    объяснении; нет override'ов — score не тронут (честный unknown). Явный override по задаче сюда
    не попадает (ветка `if c.number in ov_map` выше) и остаётся сильнее обучения.
    """
    from ai_ops_kit.governance import override_learning
    adj = override_learning.adjust_priority(root, work_id=str(p.number), base_score=p.score)
    if adj.get("override_count", 0) and adj.get("override_shift"):
        p.score = adj["adjusted_score"]
        p.priority = _bucket(p.score)
        p.computed_priority = p.priority
        p.explanation = f"ОБУЧЕНИЕ: {adj['explanation']}. {p.explanation}"
    return p


def _apply_override(p: Priority, ov: dict) -> Priority:
    p.overridden = True
    p.override_reason = ov.get("reason", "")
    human = ov.get("priority")
    if human and human != p.computed_priority:
        p.explanation = (f"ЧЕЛОВЕК: {human} (было расчётно {p.computed_priority}). "
                         f"Причина: {p.override_reason or '—'}. Расчёт: {p.explanation}")
    else:
        p.explanation = f"ЧЕЛОВЕК подтвердил {human or p.computed_priority}. {p.explanation}"
    p.priority = human or p.computed_priority
    return p


@dataclass
class PriorityReport:
    ok: bool
    repo: str
    reason: str
    items: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["items"] = [asdict(x) if isinstance(x, Priority) else x for x in self.items]
        return d


def prioritize_backlog(repo_or_root: str = ".", state: str = "open", overrides: str = "",
                       client=None) -> PriorityReport:
    from ai_ops_kit.planning import backlog_classify as bc
    from ai_ops_kit.planning import backlog_depgraph as dg
    rep = bc.classify_backlog(repo_or_root, state=state, client=client)
    if not rep.ok:
        return PriorityReport(False, rep.repo, rep.reason, [])
    graph = dg.build(rep.items)
    dependents = {b["number"]: b["dependents"] for b in graph.blocking}
    open_set = {c.number for c in rep.items}
    blocked = {c.number: sum(1 for d in (c.dependencies or []) if d in open_set) for c in rep.items}

    root = Path(repo_or_root) if Path(repo_or_root).exists() else Path(".")
    ov_map = load_overrides(_overrides_path(repo_or_root, overrides))
    scored = []
    for c in rep.items:
        p = score_issue(c, dependents=dependents.get(c.number, 0),
                        blocked_by_open=blocked.get(c.number, 0))
        if c.number in ov_map:
            p = _apply_override(p, ov_map[c.number])   # явный человек-override — авторитетнее обучения
        else:
            p = _apply_learning(root, p)               # мягкий сдвиг из прошлых governance-override'ов
        scored.append(p)
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    scored.sort(key=lambda x: (order.get(x.priority, 9), -x.score))
    return PriorityReport(True, rep.repo, "", scored)


# ── CLI ───────────────────────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="backlog_prioritize.py")
    ap.add_argument("target", nargs="?", default=".")
    ap.add_argument("--state", default="open", choices=("open", "closed", "all"))
    ap.add_argument("--overrides", default="", help="путь к реестру override (по умолчанию .ai/backlog-overrides.yaml)")
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])
    rep = prioritize_backlog(ns.target, state=ns.state, overrides=ns.overrides)
    if ns.json:
        print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))
        return 0 if rep.ok else 2
    if not rep.ok:
        print(f"НЕ проверено: {rep.reason}")
        return 2
    print(f"Приоритизация {rep.repo}: {len(rep.items)} задач")
    for p in rep.items:
        mark = " [override]" if p.overridden else ""
        print(f"  #{p.number} {p.priority}{mark} (score {p.score}, conf {p.confidence})")
        print(f"      {p.explanation}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
