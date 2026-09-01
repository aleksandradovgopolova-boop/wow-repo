#!/usr/bin/env python3
"""Поиск дубликатов и устаревших Issue в backlog (PR-8).

Дедуп ПРЕДЛАГАЕТ, а не сливает молча (PR-19/20: Suggest → Prepare → Execute → Require approval —
объединение задач требует одобрения человека). Результат — список пар-кандидатов с оценкой похожести
и ОБЪЯСНЕНИЕМ, почему они похожи (общие слова заголовка, общие метки, близость тела). Как и вердикт
судьи, подозрение без основания непроверяемо.

Похожесть считается по трём сигналам: заголовок (Jaccard по словам), метки (Jaccard), тело (Jaccard
по словам). Итоговый балл — взвешенная сумма; пара попадает в кандидаты, если заголовки очень близки
ИЛИ общий балл выше порога.

Устаревание: Issue, не обновлявшийся дольше `stale_days` относительно опорной даты (по умолчанию —
самый свежий `updated_at` в backlog, чтобы результат был детерминирован без обращения к часам), —
кандидат на закрытие/ревизию. «Устарел» — это предложение посмотреть, а не приговор.

CLI:
  python3 -m ai_ops_kit.planning.backlog_dedup <owner/repo|путь> [--state all] [--threshold 0.6]
      [--stale-days 120] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: F401

# Стоп-слова (RU+EN), которые не несут смысла для сравнения заголовков backlog.
_STOP = {
    "the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "is", "are", "be", "not", "no",
    "при", "не", "и", "в", "на", "по", "для", "с", "к", "от", "до", "из", "что", "как", "это",
    "когда", "если", "но", "а", "же", "бы", "ли", "за", "во", "об", "про",
}
_WORD = re.compile(r"[a-zA-Zа-яА-Я0-9_]+")
_W_TITLE, _W_LABELS, _W_BODY = 0.6, 0.2, 0.2       # веса сигналов в общем балле


def _tokens(text: str, keep_stop: bool = False) -> set:
    words = [w.lower() for w in _WORD.findall(text or "")]
    return {w for w in words if keep_stop or (w not in _STOP and len(w) > 1)}


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    inter = a & b
    union = a | b
    return len(inter) / len(union) if union else 0.0


@dataclass
class DuplicatePair:
    a: int
    b: int
    score: float
    title_sim: float
    label_sim: float
    body_sim: float
    shared_title_words: list = field(default_factory=list)
    shared_labels: list = field(default_factory=list)
    suggestion: str = ""
    action: str = "suggest_merge"                  # PR-19: только предложение, не слияние
    evidence: str = ""


@dataclass
class StaleIssue:
    number: int
    title: str
    updated_at: str
    days_idle: int
    reason: str


def _pair_similarity(x: dict, y: dict) -> DuplicatePair:
    tx, ty = _tokens(x.get("title")), _tokens(y.get("title"))
    lx = {str(s).lower() for s in (x.get("labels") or [])}
    ly = {str(s).lower() for s in (y.get("labels") or [])}
    bx, by = _tokens(x.get("body")), _tokens(y.get("body"))
    ts, ls, bs = _jaccard(tx, ty), _jaccard(lx, ly), _jaccard(bx, by)
    score = round(_W_TITLE * ts + _W_LABELS * ls + _W_BODY * bs, 3)
    shared_words = sorted(tx & ty)
    shared_labels = sorted(lx & ly)
    return DuplicatePair(
        a=x.get("number"), b=y.get("number"), score=score,
        title_sim=round(ts, 3), label_sim=round(ls, 3), body_sim=round(bs, 3),
        shared_title_words=shared_words, shared_labels=shared_labels,
    )


def find_duplicates(items: list, threshold: float = 0.6, title_floor: float = 0.7) -> list:
    """Пары-кандидаты в дубликаты. Кандидат, если общий балл ≥ threshold ИЛИ заголовки почти
    совпадают (title_sim ≥ title_floor). Отсортированы по убыванию балла."""
    pairs = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            p = _pair_similarity(items[i], items[j])
            if p.score >= threshold or p.title_sim >= title_floor:
                shared = ", ".join(p.shared_title_words[:6]) or "—"
                lbls = ", ".join(p.shared_labels) or "нет общих меток"
                p.evidence = (f"общие слова заголовка: {shared}; метки: {lbls}; "
                              f"title={p.title_sim} body={p.body_sim} labels={p.label_sim}")
                p.suggestion = (f"похоже на дубликаты (#{p.a} ↔ #{p.b}) — ПРЕДЛОЖИТЬ объединение; "
                                f"слияние требует одобрения человека")
                pairs.append(p)
    pairs.sort(key=lambda p: p.score, reverse=True)
    return pairs


def _parse_ts(s: str) -> "datetime | None":
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def find_stale(items: list, stale_days: int = 120, now_iso: str = "") -> list:
    """Issue без обновления дольше `stale_days`. Опорная дата: `now_iso`, иначе самый свежий
    `updated_at` в backlog (детерминизм без обращения к часам; в CLI подставляется реальное сейчас)."""
    stamps = [_parse_ts(i.get("updated_at")) for i in items]
    stamps = [t for t in stamps if t]
    ref = _parse_ts(now_iso) or (max(stamps) if stamps else None)
    if ref is None:
        return []
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    stale = []
    for i in items:
        t = _parse_ts(i.get("updated_at"))
        if not t:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        days = (ref - t).days
        if days >= stale_days:
            stale.append(StaleIssue(
                number=i.get("number"), title=i.get("title") or "",
                updated_at=i.get("updated_at") or "", days_idle=days,
                reason=f"не обновлялся {days} дн. (порог {stale_days}) — предложить закрыть/ревизовать",
            ))
    stale.sort(key=lambda s: s.days_idle, reverse=True)
    return stale


@dataclass
class DedupReport:
    ok: bool
    repo: str
    source: str
    reason: str
    total: int
    duplicate_pairs: list
    stale: list

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duplicate_pairs"] = [asdict(p) for p in self.duplicate_pairs]
        d["stale"] = [asdict(s) for s in self.stale]
        return d


def dedup_backlog(repo_or_root: str = ".", state: str = "open", threshold: float = 0.6,
                  stale_days: int = 120, client=None, now_iso: str = "") -> DedupReport:
    from ai_ops_kit.integrations import github as gh
    client = client or gh.make_client(repo_or_root)
    res = client.issues(state=state)
    if not res.ok:
        return DedupReport(False, getattr(client, "repo", ""), "", res.reason, 0, [], [])
    dups = find_duplicates(res.items, threshold=threshold)
    stale = find_stale(res.items, stale_days=stale_days, now_iso=now_iso)
    return DedupReport(True, getattr(client, "repo", ""), res.source, "",
                       len(res.items), dups, stale)


# ── СЛИЯНИЕ ДУБЛЕЙ: approval-gated execute (PR-19/20 «Execute → Require approval») ───────────────
#
# Детектор ПРЕДЛАГАЕТ (find_duplicates, action=suggest_merge). Здесь — вторая половина исхода
# `duplicates_detected_and_merged`, ПОСТРОЕННАЯ ПО РЕШЕНИЮ ВЛАДЕЛЬЦА (2026-08-31, вариант «одобряемое
# слияние»): кит выполняет слияние ТОЛЬКО по явно одобренным человеком парам, и никогда сам.
#
# GitHub не умеет «слить» два Issue — конвенция: закрыть ДУБЛЬ с кросс-ссылкой на канонический,
# канонический оставить открытым. Операция ОБРАТИМА (закрытый issue переоткрывается).
#
# ГРАНИЦЫ (механизмами, не на словах):
#   · approved — ЯВНЫЙ вход человека; из детектора он НЕ выводится (принцип «предлагать, не сливать»);
#   · dry_run по умолчанию True — что закроется, человек видит ДО того, как это произошло;
#   · канонический НИКОГДА не трогается (закрывается только дубль);
#   · пустой approved / duplicate==canonical / не-int — отказ с причиной, не «слил на всякий случай»;
#   · при провале комментария дубль НЕ закрывается (пара пропущена с причиной) — нет «тихого» закрытия.

@dataclass
class MergeExecution:
    ok: bool
    dry_run: bool
    by: str
    executed: list = field(default_factory=list)   # [{duplicate, canonical, comment_ok, close_ok}]
    skipped: list = field(default_factory=list)     # [{duplicate, canonical, reason}]
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _validate_approved(approved) -> "tuple[list, list]":
    """Разобрать одобренные пары -> (валидные, отклонённые с причиной). Инварианты границы."""
    valid, bad = [], []
    for row in (approved or []):
        dup = row.get("duplicate")
        can = row.get("canonical")
        if not isinstance(dup, int) or not isinstance(can, int) or dup <= 0 or can <= 0:
            bad.append({"duplicate": dup, "canonical": can,
                        "reason": "duplicate/canonical должны быть положительными int (номера Issue)"})
            continue
        if dup == can:
            bad.append({"duplicate": dup, "canonical": can,
                        "reason": "duplicate == canonical — это не пара дублей"})
            continue
        valid.append({"duplicate": dup, "canonical": can})
    return valid, bad


def execute_merge(repo_or_root: str = ".", approved=None, *, dry_run: bool = True,
                  by: str = "owner", note: str = "", client=None) -> MergeExecution:
    """Слить ОДОБРЕННЫЕ пары дублей: закрыть дубль с кросс-ссылкой на канонический.

    `approved`: список {"duplicate": N, "canonical": M} — ЯВНЫЙ выбор человека (не из детектора).
    `dry_run=True` (по умолчанию): ничего не пишет, возвращает план. Никогда не трогает канонический.
    """
    valid, bad = _validate_approved(approved)
    if not valid:
        return MergeExecution(False, dry_run, by, executed=[], skipped=bad,
                              reason="нет валидных одобренных пар — слияние не выполняется "
                                     "(approved задаёт ЧЕЛОВЕК, из детектора он не берётся)")
    from ai_ops_kit.integrations import github as gh
    client = client or gh.make_client(repo_or_root)
    av = client.availability()
    if not av.ok:
        return MergeExecution(False, dry_run, by, executed=[], skipped=bad, reason=av.reason)

    executed, skipped = [], list(bad)
    for pair in valid:
        dup, can = pair["duplicate"], pair["canonical"]
        body = (f"Дубликат #{can}. Объединено с одобрения: {by}."
                + (f" {note}" if note else "")
                + "\n\n(закрыто автоматизацией AI Ops по одобренной паре; обратимо переоткрытием)")
        if dry_run:
            executed.append({"duplicate": dup, "canonical": can, "dry_run": True,
                             "would": f"комментарий на #{dup} '{body[:40]}...' + закрыть #{dup} "
                                      f"(канонический #{can} остаётся открыт)"})
            continue
        cres = client.comment_issue(dup, body)
        if not cres.ok:
            skipped.append({"duplicate": dup, "canonical": can,
                            "reason": f"комментарий не оставлен ({cres.reason}) — дубль НЕ закрыт"})
            continue
        clres = client.close_issue(dup, reason="not_planned")
        executed.append({"duplicate": dup, "canonical": can,
                         "comment_ok": True, "close_ok": clres.ok,
                         "close_reason": "" if clres.ok else clres.reason})
    ok = bool(executed) and all(e.get("close_ok", True) for e in executed)
    return MergeExecution(ok, dry_run, by, executed=executed, skipped=skipped,
                          reason="" if ok else "часть пар не слита — см. skipped/close_reason")


# ── CLI ───────────────────────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="backlog_dedup.py")
    ap.add_argument("target", nargs="?", default=".")
    ap.add_argument("--state", default="open", choices=("open", "closed", "all"))
    ap.add_argument("--threshold", type=float, default=0.6)
    ap.add_argument("--stale-days", type=int, default=120)
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])
    now_iso = datetime.now(timezone.utc).isoformat()
    rep = dedup_backlog(ns.target, state=ns.state, threshold=ns.threshold,
                        stale_days=ns.stale_days, now_iso=now_iso)
    if ns.json:
        print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))
        return 0 if rep.ok else 2
    if not rep.ok:
        print(f"НЕ проверено: {rep.reason}")
        return 2
    print(f"Backlog {rep.repo} ({rep.source}): {rep.total} Issues")
    print(f"— кандидаты в дубликаты: {len(rep.duplicate_pairs)} (ПРЕДЛОЖЕНИЕ, слияние — с одобрения)")
    for p in rep.duplicate_pairs:
        print(f"  #{p.a} ↔ #{p.b}  score={p.score}  {p.evidence}")
    print(f"— устаревшие: {len(rep.stale)}")
    for s in rep.stale:
        print(f"  #{s.number} idle={s.days_idle}д  {s.title[:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
