#!/usr/bin/env python3
"""FoundationFreshness — advisory-сигнал «фундамент продукта устарел» и ГЛАВНЫЙ кандидат на пересмотр.

ПОВОД (кейс владельца, дочка ии-среда). Нижние документы (сценарии, MVP-scope) и поток выпущенных
фич живут и обновляются, а фундамент — «зачем / для кого / какая ценность» (Vision, JTBD, Canvas) —
правят редко. За месяц-два в продукт уезжает много изменений, и фундамент тихо ОТСТАЁТ: Vision уже
не отражает того, чем продукт стал. Владелец не хочет ловить это глазами.

ПОЧЕМУ СУЩЕСТВУЮЩИЕ ПРОВЕРКИ ЭТО НЕ ЛОВЯТ. `contour_consistency` смотрит на diff — расхождение,
СОЗДАННОЕ изменением; `product_contract` меряет НАЛИЧИЕ и СВЯЗНОСТЬ артефактов; `staleness.freshness`
спит без явной даты в документе. Ни одна не сравнивает СВЕЖЕСТЬ фундамента со свежестью нижнего слоя
и с потоком фич. Это и строит этот модуль.

ЧТО ЭТО НЕ. Не второй реестр документов и не второй путь рассуждения о фундаменте. Кто такой
«фундамент», модуль берёт из СУЩЕСТВУЮЩЕЙ модели контуров (`registry/product-operating-model.yaml`,
секция `foundation`) и из объявления самого репозитория; свежесть — из git-даты последнего изменения
файла. Всё это ОРКЕСТРАЦИЯ существующих сущностей, а результат встраивается в брифинг
`cli.foundation_proposal` (раздел «Ревью фундамента»), а не выводится отдельной командой.

ГРАНИЦЫ ЧЕСТНОСТИ (не сглаживаем):
  * нет git-даты у документа фундамента (не под контролем версий или без истории) -> состояние
    «не знаю», НЕ «свежо». «Не знаю» и «свежо» — разные состояния, и подменять первое вторым значит
    зеленить непроверенное;
  * кандидат называется ТОЛЬКО при измеримом основании: нижние документы и/или выпущенные фичи
    ДЕМОНСТРИРУЕМО новее фундамента на назначенный порог. Нет мерки (ни нижних документов с датой,
    ни фич) -> модуль МОЛЧИТ, устаревание не выдумывает;
  * сигнал ADVISORY — он СОВЕТУЕТ пересмотреть, а не валит сборку.

СЛОЙ. Модуль живёт в `planning` (capabilities): читает модель контуров (сосед по слою) и git
(`shared.gitio`, ниже). Здоровье/риски/интеллект сюда не импортируются — тут только даты файлов.
Библиотечный слой: возвращает данные, наружу человеку их подаёт `cli.foundation_proposal`.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ai_ops_kit.planning import contours
from ai_ops_kit.shared import gitio

DAY = 86400

#: Дефолт, если модель не объявила секцию `foundation`: верх цикла — контур стратегии.
_DEFAULT_FOUNDATION_CONTOURS = ["product_strategy"]
_DEFAULT_MIN_LAG_DAYS = 21
_DEFAULT_MIN_NEWER_FEATURES = 1

#: Артефакты фичи в дочке (`features/<id>/`) — конвенция кита. Дата последнего изменения любого из
#: них = когда фича двигалась в продукте; это и есть «поток фич» как мерка свежести фундамента.
_FEATURE_FILES = ("blueprint.yaml", "outcome-readout.yaml", "outcome-contract.yaml")


def _child_cfg(child_root) -> dict:
    """Разобранный `.ai-ops.yaml` репозитория. -> dict (пустой при отсутствии/порче: advisory-путь
    не роняет прогон из-за конфига, его достоверность стерегут doctor/validate_ai_ops_child)."""
    p = Path(child_root) / ".ai-ops.yaml"
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _pom_section(model: dict) -> dict:
    return (model or {}).get("foundation") or {}


def foundation_contours(model: dict) -> list:
    """Контуры фундамента: из `foundation.contours` модели, иначе дефолт (стратегия)."""
    declared = _pom_section(model).get("contours")
    if isinstance(declared, list) and declared:
        return [str(c) for c in declared]
    return list(_DEFAULT_FOUNDATION_CONTOURS)


def freshness_cfg(model: dict) -> dict:
    """Пороги сигнала: `foundation.freshness.{min_lag_days,min_newer_features}` (с дефолтами)."""
    fr = _pom_section(model).get("freshness") or {}
    try:
        lag = int(fr.get("min_lag_days", _DEFAULT_MIN_LAG_DAYS))
    except (TypeError, ValueError):
        lag = _DEFAULT_MIN_LAG_DAYS
    try:
        feats = int(fr.get("min_newer_features", _DEFAULT_MIN_NEWER_FEATURES))
    except (TypeError, ValueError):
        feats = _DEFAULT_MIN_NEWER_FEATURES
    return {"min_lag_days": max(0, lag), "min_newer_features": max(1, feats)}


def _declared_foundation_docs(child_root) -> list:
    """Документы фундамента, ЯВНО объявленные репозиторием (`product_operating_model.foundation.
    documents`). Тоньше контуров: дочка вправе назвать Vision/JTBD/Canvas фундаментом, а нижние
    документы (сценарии/scope) — нет, даже если кит держит их в одном контуре стратегии."""
    fnd = ((_child_cfg(child_root).get("product_operating_model") or {}).get("foundation") or {})
    docs = fnd.get("documents")
    return [str(p).strip() for p in docs if str(p).strip()] if isinstance(docs, list) else []


def _all_sot_paths(model: dict, child_root) -> dict:
    """Источники истины по контурам (кит + доопределение репозитория). -> {cid: [rel, ...]}."""
    out = {}
    for cid in contours.contour_ids(model):
        out[cid] = [s.get("path") for s in contours.sot_for(model, cid, child_root) if s.get("path")]
    return out


def foundation_doc_paths(model: dict, child_root) -> list:
    """Документы фундамента: явное объявление репозитория сильнее; иначе — источники истины
    контуров фундамента из модели. -> список относительных путей (dedup, порядок сохранён)."""
    declared = _declared_foundation_docs(child_root)
    if declared:
        base = declared
    else:
        sot = _all_sot_paths(model, child_root)
        base = [p for cid in foundation_contours(model) for p in sot.get(cid, [])]
    seen, out = set(), []
    for p in base:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _last_commit_ts(root, rel: str):
    """Unix-время последнего коммита, тронувшего файл (с учётом project/custom-оверлея). -> int|None.

    None — «не знаю» (файла нет, не под git, без истории), а НЕ «свежо»: подмена запрещена."""
    inst = contours._resolve(Path(root), rel)
    if inst is None:
        return None
    try:
        rp = inst.relative_to(Path(root)).as_posix()
    except ValueError:
        rp = rel
    rc, out, _ = gitio.git(root, "log", "-1", "--format=%ct", "--", rp)
    if rc != 0 or not out.strip():
        return None
    try:
        return int(out.strip().splitlines()[0])
    except ValueError:
        return None


def feature_timestamps(root) -> list:
    """Поток фич дочки: по одной дате на фичу (`features/<id>/`) — новейшая среди её артефактов.
    -> [int, ...] (пусто, если каталога фич нет). Дата = когда фича последний раз двигалась."""
    fdir = Path(root) / "features"
    if not fdir.is_dir():
        return []
    out = []
    for sub in sorted(fdir.iterdir()):
        if not sub.is_dir():
            continue
        cand = [t for name in _FEATURE_FILES if (sub / name).is_file()
                for t in (_last_commit_ts(root, f"features/{sub.name}/{name}"),) if t is not None]
        if cand:
            out.append(max(cand))
    return out


def _iso(ts) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()


def _base(path: str) -> str:
    return Path(path).name or path


def assess(child_root, *, model: dict | None = None, feature_dates: list | None = None,
           now: float | None = None) -> dict:
    """Оценить свежесть фундамента и назвать главного кандидата на пересмотр (advisory).

    feature_dates — поток фич датами извне (напр. из intelligence.knowledge_graph); None -> модуль
    берёт даты сам из `features/<id>/` дочки. Так слой planning не тянет intelligence вверх, а
    инъекция всё равно доступна тому, у кого граф уже собран.

    -> {schema_version, kind, enforcement, foundation_contours, docs, reference_newest,
        feature_stream, candidate, state, note}.
    state ∈ {candidate, fresh, unknown, no_reference, no_foundation}.
    """
    model = model or contours.load_model()
    cfg = freshness_cfg(model)
    min_lag = cfg["min_lag_days"] * DAY
    min_feats = cfg["min_newer_features"]
    now = time.time() if now is None else now
    root = Path(child_root)

    fdocs = foundation_doc_paths(model, child_root)
    all_sot = _all_sot_paths(model, child_root)
    ref_paths = sorted({p for ps in all_sot.values() for p in ps} - set(fdocs))
    # Какому контуру принадлежит документ фундамента (для отчёта; у явно объявленных может быть None).
    doc_contour = {p: cid for cid in foundation_contours(model) for p in all_sot.get(cid, [])}

    # Присутствующие документы фундамента и их git-дата (None -> «не знаю», не «свежо»).
    present = {p: _last_commit_ts(root, p) for p in fdocs if contours._resolve(root, p) is not None}
    tracked = {p: t for p, t in present.items() if t is not None}
    ref_dates = {p: t for p in ref_paths
                 for t in (_last_commit_ts(root, p),) if t is not None}
    feats = list(feature_dates) if feature_dates is not None else feature_timestamps(root)

    base = {"schema_version": 1, "kind": "foundation-freshness", "enforcement": "advisory",
            "foundation_contours": foundation_contours(model)}
    ref_newest_ts = max(ref_dates.values(), default=None)
    ref_newest = None
    if ref_newest_ts is not None:
        rp = max(ref_dates, key=lambda k: ref_dates[k])
        ref_newest = {"path": rp, "ts": ref_newest_ts, "iso": _iso(ref_newest_ts)}
    feat_stream = {"count": len(feats), "newest_ts": max(feats, default=None),
                   "newest_iso": _iso(max(feats, default=None))}

    if not present:
        return {**base, "docs": [], "reference_newest": ref_newest, "feature_stream": feat_stream,
                "candidate": None, "state": "no_foundation",
                "note": "документов фундамента в репозитории нет — это забота ревью фундамента "
                        "(наличие артефактов), не свежести"}
    if not ref_dates and not feats:
        return {**base, "docs": _doc_rows(present, ref_dates, feats, doc_contour), "reference_newest": ref_newest,
                "feature_stream": feat_stream, "candidate": None, "state": "no_reference",
                "note": "не с чем сравнить: ни нижних документов с датой, ни фич — устаревание "
                        "фундамента не выдумываю"}
    if not tracked:
        return {**base, "docs": _doc_rows(present, ref_dates, feats, doc_contour), "reference_newest": ref_newest,
                "feature_stream": feat_stream, "candidate": None, "state": "unknown",
                "note": "у документов фундамента нет git-даты (не под контролем версий или без "
                        "истории) — свежесть неизвестна, «не знаю» ≠ «свежо»"}

    docs_rows = _doc_rows(present, ref_dates, feats, doc_contour)
    candidates = []
    for row in docs_rows:
        ts = row["last_change_ts"]
        if ts is None:
            continue
        newer_ref_ts = [t for t in ref_dates.values() if t > ts]
        newer_feat_ts = [t for t in feats if t > ts]
        lag_source = max(newer_ref_ts + newer_feat_ts, default=None)
        if lag_source is None:
            continue
        lag = lag_source - ts
        has_basis = bool(newer_ref_ts) or len(newer_feat_ts) >= min_feats
        if lag >= min_lag and has_basis:
            candidates.append((row, lag, lag_source, len(newer_ref_ts), len(newer_feat_ts)))

    if not candidates:
        return {**base, "docs": docs_rows, "reference_newest": ref_newest,
                "feature_stream": feat_stream, "candidate": None, "state": "fresh",
                "note": "фундамент не отстаёт от нижнего слоя измеримо — сигнала нет"}

    # Главный кандидат — наибольшее отставание (потом больше новых фич, потом путь для устойчивости).
    row, lag, lag_source, n_ref, n_feat = max(
        candidates, key=lambda c: (c[1], c[4], c[3], c[0]["path"]))
    candidate = {
        "path": row["path"], "contour": row["contour"],
        "last_change_ts": row["last_change_ts"], "last_change_iso": row["last_change_iso"],
        "lag_days": round(lag / DAY), "newer_references": row["newer_references"][:5],
        "newer_references_count": n_ref, "newer_features": n_feat,
        "reference_newer_iso": _iso(lag_source),
        "reason": _reason(row["path"], n_ref, n_feat, row["last_change_iso"], _iso(lag_source),
                          round(lag / DAY)),
    }
    return {**base, "docs": docs_rows, "reference_newest": ref_newest,
            "feature_stream": feat_stream, "candidate": candidate, "state": "candidate",
            "note": "фундамент отстал от нижнего слоя измеримо — назван кандидат на пересмотр (совет)"}


def _doc_rows(present: dict, ref_dates: dict, feats: list, doc_contour: dict | None = None) -> list:
    """Строки по каждому присутствующему документу фундамента: дата, отставание, что новее."""
    rows = []
    for p, ts in present.items():
        newer_refs = sorted(rp for rp, rt in ref_dates.items() if ts is not None and rt > ts)
        newer_feats = len([t for t in feats if ts is not None and t > ts]) if ts is not None else 0
        rows.append({"path": p, "contour": (doc_contour or {}).get(p), "tracked": ts is not None,
                     "last_change_ts": ts, "last_change_iso": _iso(ts),
                     "newer_references": newer_refs, "newer_features": newer_feats})
    return rows


def _reason(path: str, n_ref: int, n_feat: int, doc_iso, ref_iso, lag_days: int) -> str:
    """Человеческое объяснение кандидата: что именно новее и на сколько отстал."""
    parts = []
    if n_ref:
        parts.append(f"{n_ref} нижних документ(ов)")
    if n_feat:
        parts.append(f"{n_feat} фич(и)")
    newer = " и ".join(parts) if parts else "нижний слой"
    return (f"«{_base(path)}», скорее всего, устарел: {newer} новее фундамента "
            f"(последняя правка — {doc_iso}, самый свежий нижний слой — {ref_iso}, "
            f"отставание ≈ {lag_days} дн.)")
