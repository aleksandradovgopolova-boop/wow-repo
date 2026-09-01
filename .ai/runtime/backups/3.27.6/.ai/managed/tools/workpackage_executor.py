#!/usr/bin/env python3
"""Sequential WorkPackage Executor (v3.1) — исполнить план по пакетам, а не одним блобом.

Аудит: Atomic Planner создаёт КОНКРЕТНЫЕ WorkPackages (v2.111), но их никто не ИСПОЛНЯЛ — задача
всё равно шла одним общим tool loop. Здесь — последовательный исполнитель:

  Пакет 1 -> commit -> evidence -> gates -> handoff -> Пакет 2 -> ... -> итог

Инварианты:
  * пакеты идут в порядке order; зависимый пакет НЕ стартует, пока все его depends_on не подтверждены;
  * каждый пакет — свой прогон движка на общей ветке ai-ops/<wid> (resume поверх предыдущего): свой
    коммит/SHA, своё evidence, свои гейты, свой RunHandoff -> своя точка resume;
  * блок пакета (preflight/гейты/нет коммита) ОСТАНАВЛИВАЕТ последовательность — следующие не стартуют
    (честно: не притворяемся, что доделали);
  * исполнение последовательное (не параллельное): состояние передаётся через RunHandoff.

Использование (программно):
  execute_sequence(task, signals, child_root, packages, proposer_for, feature=wid, ...) -> отчёт.
CLI: workpackage_executor.py --selftest
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG / "tools"))


def _git(root, *a):
    import gitio
    return gitio.git(root, *a)   # v3.0.13 (блок C): единый git-хелпер с таймаутом


def _changed_files(root, sha):
    """Файлы, изменённые коммитом sha относительно его родителя (для пост-дифф проверки write_scope)."""
    rc, out, _ = _git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", sha)
    return [ln for ln in out.splitlines() if ln] if rc == 0 else []


def _pkg_hash(pkg):
    """v3.0-rc4 (P0.3): стабильный хэш ОПРЕДЕЛЕНИЯ WorkPackage (id/scope/deps/order/write_scope).
    Отчёт пакета привязывается к нему; при дрейфе определения старый отчёт не принимается за выполненный."""
    import hashlib, json as _j
    payload = _j.dumps({"id": pkg.get("id"), "scope": pkg.get("scope"),
                        "depends_on": sorted(pkg.get("depends_on") or []), "order": pkg.get("order"),
                        "write_scope": pkg.get("write_scope")}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _plan_hash(ordered):
    """Хэш всего SequencePlan = хэш последовательности хэшей пакетов (порядко-зависимый)."""
    import hashlib
    return hashlib.sha256("".join(_pkg_hash(p) for p in ordered).encode("utf-8")).hexdigest()[:16]


def _ordered(packages):
    return sorted(packages, key=lambda p: (p.get("order", 0), p.get("id", "")))


def _seq_err(wid, ordered, error, *, packages=None, completed=None, stopped_at=None, **extra):
    """v3.0.13 (блок C): единый конструктор отказного WorkPackageSequence-отчёта. Прежде этот словарь из
    ~11 ключей копировался вручную 8 раз (добавление поля = 8 правок). Поведение идентично; extra —
    дополнительные поля конкретного отказа (напр. plan_hash/saved_plan_hash при дрейфе)."""
    d = {"schema_version": 1, "kind": "WorkPackageSequence", "workitem_id": wid,
         "packages": packages if packages is not None else [],
         "completed": completed if completed is not None else [],
         "stopped_at": stopped_at, "executed_all": False, "ready_all": False,
         "aggregate_ready": False, "total": len(ordered), "error": error}
    d.update(extra)
    return d


def _durable_write_yaml(path, data, require_keys=()):
    """v3.0.7/v3.0.12 (finding аудита P0.3 / блок B): АТОМАРНАЯ + FAIL-CLOSED запись критического
    lifecycle-артефакта. Делегирует ЕДИНОМУ durable-контракту lifecycle_store.durable_write (tmp ->
    flush+fsync(файл) -> atomic rename -> fsync(КАТАЛОГ) -> перечитать+провалидировать). Прежде здесь была
    отдельная копия БЕЗ fsync каталога — теперь один источник истины для всех durable-записей."""
    import lifecycle_store
    return lifecycle_store.durable_write(path, data, require_keys=require_keys)


_SUPPORTED_PLAN_SCHEMA = 1


def _validate_sequence_plan_schema(doc, expected_wid=None):
    """v3.0.9/v3.0.10 (finding аудита P0/P1): ПОЛНАЯ integrity-валидация SequencePlan при КАЖДОМ чтении.
    Проверяем не только НАЛИЧИЕ полей, но и ЦЕЛОСТНОСТЬ: поддерживаемая schema_version, совпадение
    workitem_id (если задан expected_wid — иначе чужой план в каталоге WorkItem прошёл бы), уникальность
    package id и order, корректность depends_on (ссылки на существующие пакеты), отсутствие циклов,
    пересчёт КАЖДОГО pkg_hash и общего plan_hash. Любое расхождение -> lifecycle-corrupted.
    -> None (валиден) | причина."""
    if not isinstance(doc, dict):
        return "не dict"
    if doc.get("kind") != "SequencePlan":
        return f"kind != SequencePlan ({doc.get('kind')})"
    for k in ("schema_version", "workitem_id", "plan_hash", "base_ref", "sequence_base_sha", "packages"):
        if doc.get(k) in (None, ""):
            return f"нет обязательного поля '{k}'"
    if doc.get("schema_version") != _SUPPORTED_PLAN_SCHEMA:
        return f"schema_version {doc.get('schema_version')} не поддерживается (нужна {_SUPPORTED_PLAN_SCHEMA})"
    if expected_wid is not None and doc.get("workitem_id") != expected_wid:
        return (f"workitem_id плана '{doc.get('workitem_id')}' != текущего WorkItem '{expected_wid}' "
                "— чужой SequencePlan в каталоге")
    pkgs = doc.get("packages")
    if not isinstance(pkgs, list) or not pkgs:
        return "packages пуст/не список"
    ids, orders = [], []
    for i, p in enumerate(pkgs):
        if not isinstance(p, dict):
            return f"packages[{i}] не dict"
        for k in ("id", "pkg_hash", "order"):
            if p.get(k) in (None, ""):
                return f"packages[{i}] без '{k}'"
        if "depends_on" not in p:
            return f"packages[{i}] без depends_on"
        deps = p.get("depends_on")
        if not isinstance(deps, list):
            return f"packages[{i}] depends_on не список"
        ids.append(p["id"])
        orders.append(p["order"])
        # пересчёт pkg_hash из определения (id/scope/deps/order/write_scope) — дрейф определения ловится
        if _pkg_hash(p) != p.get("pkg_hash"):
            return f"packages[{i}] ('{p['id']}') pkg_hash не сходится с определением (подмена/дрейф)"
    if len(set(ids)) != len(ids):
        return f"дубли package id: {sorted({x for x in ids if ids.count(x) > 1})}"
    if len(set(orders)) != len(orders):
        return f"дубли order: {sorted({x for x in orders if orders.count(x) > 1})}"
    idset = set(ids)
    # depends_on обязаны ссылаться на существующие пакеты; самоссылка запрещена
    dep_map = {}
    for p in pkgs:
        for d in (p.get("depends_on") or []):
            if d == p["id"]:
                return f"пакет '{p['id']}' зависит от себя"
            if d not in idset:
                return f"пакет '{p['id']}' зависит от несуществующего '{d}'"
        dep_map[p["id"]] = list(p.get("depends_on") or [])
    # отсутствие циклов (обход в глубину с тремя состояниями)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {i: WHITE for i in idset}

    def _has_cycle(node, stack):
        color[node] = GRAY
        for nxt in dep_map.get(node, []):
            if color[nxt] == GRAY:
                return stack + [nxt]
            if color[nxt] == WHITE:
                r = _has_cycle(nxt, stack + [nxt])
                if r:
                    return r
        color[node] = BLACK
        return None

    for i in idset:
        if color[i] == WHITE:
            cyc = _has_cycle(i, [i])
            if cyc:
                return f"цикл зависимостей: {' -> '.join(cyc)}"
    # пересчёт общего plan_hash из упорядоченных пакетов
    recomputed = _plan_hash(_ordered(pkgs))
    if recomputed != doc.get("plan_hash"):
        return f"plan_hash не сходится с пакетами (сохранён {doc.get('plan_hash')}, пересчитан {recomputed})"
    return None


def _hard_stop(rep):
    """v2.120 (P0.3): причина ОСТАНОВИТЬ цепочку (нельзя строить зависимый пакет поверх), либо None.

    Стоп — на настоящем блокере: нет коммита, preflight-блок, регрессия базы, security FAIL,
    отрицательный вердикт ревьюера, нарушение package scope (попытка выйти за write-scope). НЕ стоп —
    «awaiting evidence» (нет author/review -> пакет исполнен, но не ready): его можно оставить как
    незавершённую работу, цепочка продолжается."""
    if rep.get("status") == "blocked":
        return "preflight-blocked"
    if not (rep.get("commit") or {}).get("sha"):
        return "no-commit"
    if (rep.get("baseline") or {}).get("regressions"):
        return "regression"
    # v3.0-rc2 (P0.2): security pack возвращает blocked/needs_review/clear — НИКОГДА "fail". Старое условие
    # overall=="fail" было недостижимо -> security-заблокированный пакет проходил как «awaiting evidence».
    # СТОП на НАСТОЯЩЕМ блокере: (1) блокирующая находка (overall=blocked: secrets/critical); (2) security-
    # гейт fail из-за ОТСУТСТВУЮЩЕГО человеко-approval (ApprovalRecord). НЕ стоп: needs_review без
    # поданного ревьюера — это awaiting evidence (как артефакт-гейты без author), цепочка продолжается.
    if (rep.get("security_scan") or {}).get("overall") in ("blocked", "fail"):
        return "security-fail"
    # v3.0-rc4 (P0.2): security-ГЕЙТ fail останавливает цепочку в ЛЮБОМ случае, КРОМE «awaiting» —
    # когда нужен независимый security-reviewer/человек, а он НЕ подан (аналог артефакт-гейтов без
    # author). Всё остальное = настоящий блокер: сбой сканера (fail-closed), блокирующая находка,
    # security-reviewer вынес НЕ pass, отсутствующий ApprovalRecord.
    for g in ((rep.get("gates") or {}).get("gate_results") or []):
        if g.get("gate") == "security" and g.get("status") == "fail":
            _blk = " ".join(g.get("blockers") or [])
            _awaiting = ("нужен независимый security-reviewer" in _blk) and "ApprovalRecord" not in _blk
            if not _awaiting:
                return "security-gate-fail"
    # v3.0-rc13 (finding аудита P0): источник истины — ИТОГОВЫЙ блокирующий вердикт ревью, НЕ сырой
    # status вызова. `warn` на БЛОКИРУЮЩЕМ гейте _run_reviews превращает в gate=fail и помечает
    # entry.closed_as="blocked" (v2.85) — но старое условие ловило только status=="fail" -> живой rc11-
    # результат (code_review=warn -> gate fail -> ready_for_pr=false) НЕ останавливал цепочку, и пакет
    # N+1 мог строиться поверх изменения, которое независимый ревьюер ЗАБЛОКИРОВАЛ. Ловим оба.
    for rv in (rep.get("reviews") or []):
        if (rv or {}).get("status") == "fail" or (rv or {}).get("closed_as") == "blocked":
            return "reviewer-blocked"
    # belt-and-suspenders: итоговый review-owned гейт со статусом fail в gate_results (напр. code_review,
    # ux_review) — блокирующий вердикт судьи, а не «awaiting evidence» (у awaiting нет вынесенного вердикта).
    _review_gates = {"code_review", "ux_review", "ai_red_team", "architecture_review", "accessibility_review"}
    for g in ((rep.get("gates") or {}).get("gate_results") or []):
        if g.get("gate") in _review_gates and g.get("status") == "fail":
            ev = " ".join(g.get("evidence") or [])
            if "reviewer verdict" in ev:      # вердикт РЕАЛЬНО вынесен (не пустой artifact-гейт)
                return "reviewer-blocked"
    # нарушение package scope: модель пыталась ПИСАТЬ вне write_scope пакета -> брокер отклонил.
    # Матчим именно scope-отказ (не любой denied — напр. блокировка git push НЕ является scope-violation).
    for reason in ((rep.get("loop") or {}).get("denied_reasons") or []):
        if "вне write_scope" in (reason or ""):
            return "scope-violation"
    return None


def _classify_failure(exc):
    """v3.0-rc13 (finding аудита P1): типизировать исключение пакета, а не звать всё «infra-error».
    Иначе внутренний баг executor'а не отличить от нестабильности провайдера. -> envelope dict."""
    import hashlib
    import traceback
    et = type(exc).__name__
    msg = str(exc)
    # сеть/провайдер — транзиентно, retryable; budget — не retryable без изменения лимита; парсер/валидация —
    # проблема данных; прочее -> engine (вероятный дефект, НЕ маскировать под провайдер).
    if isinstance(exc, (ConnectionError, TimeoutError)) or "Connection reset" in msg or "timed out" in msg:
        fclass, retryable = ("network", True)
    elif et in ("URLError", "HTTPError", "IncompleteRead", "RemoteDisconnected"):
        fclass, retryable = ("provider", True)
    elif "budget" in msg.lower() or et == "BudgetExceeded":
        fclass, retryable = ("budget", False)
    elif et in ("ValueError", "KeyError", "TypeError", "YAMLError", "JSONDecodeError"):
        fclass, retryable = ("validation", False)
    else:
        fclass, retryable = ("engine", False)       # вероятный программный дефект — не «нестабильность»
    tb = "".join(traceback.format_exception_only(type(exc), exc))
    return {"failure_class": fclass, "exception_type": et, "message": msg[:400],
            "retryable": retryable,
            "traceback_hash": hashlib.sha256(tb.encode("utf-8")).hexdigest()[:12]}


def _aggregate_close_security(agg_sec, vroot, base_sha, final_sha, signals, reviewer_proposer, review,
                              security_reviewer_proposer=None, strict_judge_qualified=True,
                              wid=None, child_root=None):
    """v3.8.3-rc2 (#4/#4b — enforcement #5 на АГРЕГАТЕ): needs_review на integration-диффе закрывается ТОЛЬКО:
      (a) QUALIFIED security-судьёй — ОТДЕЛЬНЫЙ security_reviewer_proposer при strict_judge_qualified=True; ЛИБО
      (b) человеко-ApprovalRecord, привязанным к ИНТЕГРАЦИОННОМУ SHA (binds_to=final_sha).
    Общий code-reviewer (reviewer_proposer) БОЛЬШЕ НЕ закрывает aggregate security (раньше его pass -> clear —
    это и был незакрытый шов #5). blocked/error/clear не трогаем (fail-closed). Без судьи И без человеко-
    одобрения на integration-SHA needs_review остаётся needs_review (честный не-ready).
    -> (agg_sec', judge_result|None)."""
    agg_sec = dict(agg_sec or {})
    if agg_sec.get("overall") != "needs_review":
        return agg_sec, None

    # (a) QUALIFIED security-судья (writer≠judge, read-only) — только он, не общий reviewer
    if strict_judge_qualified and security_reviewer_proposer is not None:
        try:
            import execution_pipeline as _ep
            ctx = _ep._change_context_range(vroot, base_sha, final_sha)  # вся цепочка base..final
            status, res = _ep._review_security(security_reviewer_proposer, vroot, agg_sec, final_sha,
                                               {"max_model_calls": 12}, change_context=ctx)
            agg_sec["reviewer_status"] = status
            if status == "pass":
                agg_sec["overall"] = "clear"
                agg_sec["closed_by"] = "aggregate-qualified-security-judge"
            elif isinstance(res, dict) and res.get("invalid"):
                agg_sec["reviewer_invalid"] = res.get("invalid")   # false-green отклонён
            return agg_sec, res
        except Exception as e:  # noqa: BLE001 — сбой судьи на aggregate = fail-closed (не clear)
            agg_sec["overall"] = "error"
            agg_sec["review_error"] = str(e)
            return agg_sec, None

    # (b) человеко-ApprovalRecord на ИНТЕГРАЦИОННОМ SHA (#4b): КАЖДЫЙ needs_review-домен обязан иметь валидную
    # strict-запись, привязанную к integration-SHA (plan_hash=final_sha -> binds_to обязан == final_sha).
    # Проверяем напрямую по доменам aggregate (не через signals — needs_review уже перечислен).
    _root = child_root or vroot
    _nr = list(agg_sec.get("needs_review") or [])
    if wid and _root and _nr:
        try:
            import approvals as _appr
            _recs = _appr.load_approvals(_root, wid)
            _now = _appr._now_iso()
            _closed = [d for d in _nr if any(
                rc.get("approval") == d and _appr._record_valid(rc, now=_now, plan_hash=final_sha, strict=True)
                for rc in _recs)]
            _open = [d for d in _nr if d not in _closed]
            agg_sec["human_check"] = {"closed": _closed, "open": _open, "records_seen": len(_recs),
                                      "bound_to": "integration_sha"}
            if not _open:                                   # ВСЕ needs_review закрыты человеком на integration-SHA
                agg_sec["overall"] = "clear"
                agg_sec["closed_by"] = "human-approval-integration-sha"
            return agg_sec, None
        except Exception as e:  # noqa: BLE001 — сбой проверки одобрения = fail-closed
            agg_sec["overall"] = "error"
            agg_sec["review_error"] = str(e)
            return agg_sec, None

    # ни qualified-судьи, ни человека на integration-SHA -> awaiting (общий reviewer НЕ закрывает security)
    agg_sec.setdefault("closed_by", None)
    return agg_sec, None


def _aggregate_code_review(vroot, base_sha, final_sha, signals, reviewer_proposer, review):
    """v3.0-rc13 (finding аудита P0): независимый code_review ИНТЕГРИРОВАННОГО диффа. rc16 (P0): ревьюер
    получает контекст ВСЕЙ цепочки base..final (`_change_context_range`), а не только последний коммит —
    иначе риск взаимодействия пакет1↔пакет3 не виден. -> (ok, reviews|None). ok=False, если ревьюер
    ЗАБЛОКИРОВАЛ (fail / warn-на-блокирующем). Без ревью — ok=True (per-package код-ревью уже прошло)."""
    if not (review and reviewer_proposer):
        return True, None
    try:
        import execution_pipeline as _ep
        ctx = _ep._change_context_range(vroot, base_sha, final_sha)
        gev, revs = _ep._run_reviews(reviewer_proposer, vroot, ["code_review"], {},
                                     dict(signals or {}), final_sha, {"max_model_calls": 16},
                                     change_context=ctx)
        # v3.0-rc20 (finding аудита P0): ТОЛЬКО явный валидный pass закрывает aggregate code_review.
        # Раньше `return (not blocked)` был fail-OPEN: no-verdict/невалидная структура/timeout/budget
        # оставляли blocked=False -> ok=True -> aggregate_ready БЕЗ подтверждённого вердикта. Теперь
        # source of truth — gate_ev['code_review'].status=='pass' (ставится _run_reviews только при
        # валидном НЕ-fail вердикте); всё остальное -> ok=False (как per-package).
        ok = (gev.get("code_review") or {}).get("status") == "pass"
        return ok, revs
    except Exception:  # noqa: BLE001 — сбой aggregate-ревью = fail-closed
        return False, None


def retry_package(child_root, wid, pid, features_dir=None):
    """v3.0-rc13 (finding аудита P1): ДОВЕРЕННЫЙ retry заблокированного/сбойного пакета — без ручного
    git reset. (1) Архивирует проваленную попытку (work-packages/<pid> -> .../attempts/attempt-N,
    история не теряется); (2) восстанавливает ветку ai-ops/<wid> ТОЧНО на checkpoint предшественника
    (SHA пакета N-1 из его снимка, либо sequence_base_sha для первого пакета). После этого безопасно:
    execute_sequence(..., resume_from=pid) — exact-checkpoint совпадёт. -> dict {ok|error, ...}."""
    import shutil
    import yaml
    child_root = Path(child_root)
    features_dir = Path(features_dir) if features_dir else child_root / "features"
    fdir = features_dir / wid
    sp = fdir / "sequence-plan.yaml"
    if not sp.exists():
        return {"ok": False, "error": f"нет sequence-plan.yaml для '{wid}' — нечего retry (сначала прогон)"}
    try:
        plan = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"sequence-plan.yaml нечитаем: {e}"}
    ids = [p.get("id") for p in (plan.get("packages") or [])]
    if pid not in ids:
        return {"ok": False, "error": f"пакет '{pid}' не в плане последовательности {ids}"}
    idx = ids.index(pid)
    # checkpoint = коммит предшественника (его снимок) либо база последовательности для первого пакета
    if idx == 0:
        checkpoint = plan.get("sequence_base_sha")
        predecessor = None
    else:
        predecessor = ids[idx - 1]
        prep = fdir / "work-packages" / predecessor / "report.json"
        checkpoint = None
        if prep.is_file():
            try:
                checkpoint = ((json.loads(prep.read_text(encoding="utf-8")) or {}).get("commit") or {}).get("sha")
            except Exception:  # noqa: BLE001
                checkpoint = None
    if not checkpoint:
        return {"ok": False, "error": (f"не найден checkpoint предшественника для '{pid}' "
                                       f"(предшественник={predecessor or 'база'}) — снимок отсутствует")}

    # v3.0-rc16 (finding аудита P0): retry НИКОГДА не трогает основной checkout. Все preconditions
    # проверяются ДО любой git-операции; любая ошибка -> fail-closed (git-состояние не меняется):
    #   (1) выделенный worktree ОБЯЗАН существовать (нет fallback на child_root!);
    #   (2) vroot — НЕ основной checkout (toplevel worktree совпадает с ним и != child_root);
    #   (3) checkout ветки ai-ops/<wid> ОБЯЗАН пройти; после — HEAD реально на этой ветке;
    #   (4) checkpoint существует как commit И достижим из ветки (принадлежит этой цепочке).
    wt = child_root / ".ai" / "worktrees" / wid
    branch = f"ai-ops/{wid}"
    if not wt.is_dir():
        return {"ok": False, "error": (f"retry отказан (fail-closed): выделенный worktree "
                                       f".ai/worktrees/{wid} не существует — восстановление в основном "
                                       f"checkout запрещено. Пересоберите worktree/прогон.")}
    vroot = wt
    # (2) vroot — не основной checkout
    _rc_top, _top, _ = _git(vroot, "rev-parse", "--show-toplevel")
    _rc_mtop, _mtop, _ = _git(child_root, "rev-parse", "--show-toplevel")
    if _rc_top != 0 or Path((_top or "").strip() or ".").resolve() == Path((_mtop or "").strip() or "/x").resolve():
        return {"ok": False, "error": "retry отказан (fail-closed): worktree резолвится в основной checkout"}
    # (4) checkpoint — реальный commit
    if _git(vroot, "cat-file", "-e", f"{checkpoint}^{{commit}}")[0] != 0:
        return {"ok": False, "error": f"retry отказан (fail-closed): checkpoint {checkpoint[:8]} не найден в репозитории"}
    # (3) checkout ветки цепочки ОБЯЗАН пройти; проверяем результат ЯВНО (не полагаемся на reset)
    rc_co, _, err_co = _git(vroot, "checkout", "-q", branch)
    if rc_co != 0:
        return {"ok": False, "error": f"retry отказан (fail-closed): checkout {branch} не удался: {err_co}"}
    _rc_hb, _hb, _ = _git(vroot, "rev-parse", "--abbrev-ref", "HEAD")
    if _rc_hb != 0 or (_hb or "").strip() != branch:
        return {"ok": False, "error": (f"retry отказан (fail-closed): HEAD worktree не на {branch} "
                                       f"(факт: {(_hb or '?').strip()})")}
    # (4b) checkpoint достижим из ветки (принадлежит этой цепочке, а не чужой ревизии)
    if _git(vroot, "merge-base", "--is-ancestor", checkpoint, "HEAD")[0] != 0:
        return {"ok": False, "error": (f"retry отказан (fail-closed): checkpoint {checkpoint[:8]} не "
                                       f"является предком {branch} — не принадлежит этой цепочке")}

    # ВСЕ preconditions пройдены -> архивируем попытку, затем reset (git-состояние меняем только теперь)
    pkg_dir = fdir / "work-packages" / pid
    archived = None
    if pkg_dir.is_dir():
        attempts = pkg_dir / "attempts"
        attempts.mkdir(parents=True, exist_ok=True)
        n = 1 + len([d for d in attempts.iterdir() if d.is_dir() and d.name.startswith("attempt-")])
        dest = attempts / f"attempt-{n}"
        dest.mkdir(parents=True, exist_ok=True)
        for f in pkg_dir.iterdir():
            if f.name == "attempts":
                continue
            shutil.move(str(f), str(dest / f.name))
        archived = str(dest.relative_to(child_root)) if str(dest).startswith(str(child_root)) else str(dest)
    rc, _, err = _git(vroot, "reset", "--hard", checkpoint)
    if rc != 0:
        return {"ok": False, "error": f"git reset на checkpoint {checkpoint[:8]} не удался: {err}",
                "checkpoint": checkpoint, "archived_attempt": archived}
    return {"ok": True, "package": pid, "predecessor": predecessor, "checkpoint": checkpoint,
            "worktree": str(vroot), "archived_attempt": archived, "next": f"resume_from={pid}"}


def _collect_base_checks_at(child_root, base_sha, sandbox):
    """v3.0-rc16/rc20 (finding аудита P0): baseline-проверки СТРОГО на sequence_base_sha в отдельном
    read-only detached-worktree. rc20: PROVENANCE — результат `worktree add` и HEAD ПРОВЕРЯЮТСЯ; baseline
    считается доказанным ТОЛЬКО если worktree создан и его HEAD == base_sha. Иначе -> None (вызывающий
    НЕ деградирует на другой checkout: нет доказанного baseline -> aggregate unavailable -> нет PR).
    -> {"checks":..., "sha": base_sha, "proven": True} | None."""
    if not base_sha:
        return None
    import project_detector as _pd, evidence_collector as _ec, tool_broker as _tb
    child_root = Path(child_root)
    if _git(child_root, "rev-parse", "--is-inside-work-tree")[0] != 0:
        return None
    tmp = child_root / ".ai" / "worktrees" / f"_base-{base_sha[:12]}"
    try:
        rc_add, _, _ = _git(child_root, "worktree", "add", "--detach", "-f", str(tmp), base_sha)
        if rc_add != 0 or not tmp.is_dir():
            return None                                   # worktree add не удался -> baseline НЕ доказан
        rc_h, head, _ = _git(tmp, "rev-parse", "HEAD")
        if rc_h != 0 or (head or "").strip() != base_sha:  # HEAD обязан быть РОВНО на base_sha
            return None
        pol = (_tb.sandbox_policy(child_root=str(tmp)) if sandbox
               else _tb.Policy(level="execution", child_root=str(tmp), block_push=True))
        checks = _ec.collect(_pd.detect(tmp), tmp, pol)["checks"]
        return {"checks": checks, "sha": base_sha, "proven": True}
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            _git(child_root, "worktree", "remove", "--force", str(tmp))
        except Exception:  # noqa: BLE001
            pass


def _aggregate_verify(child_root, wid, sandbox, final_sha, base_checks, sequence_base_sha,
                      signals, reviewer_proposer, review, baseline_proven,
                      security_reviewer_proposer=None, strict_judge_qualified=True):
    """v3.0.13 (блок C): AGGREGATE-верификация финального интегрированного SHA — вынесена из
    execute_sequence БЕЗ изменения поведения (чистый вход->aggregate-dict). Перепроверяет результат
    ЦЕЛИКОМ на final_sha против sequence_base_sha: регрессии проверок, дерево чистое, HEAD==final_sha,
    evidence на final_sha, агрегатный security (полный дифф base..final) и aggregate code_review."""
    import project_detector as _pd2, evidence_collector as _ec2, tool_broker as _tb2
    import execution_pipeline as _ep
    import security_pack as _sp2
    try:
        wt = Path(child_root) / ".ai" / "worktrees" / wid
        vroot = wt if wt.is_dir() else Path(child_root)
        # v3.0-rc2 (P0.4): проверяем ИМЕННО финальный SHA — HEAD worktree обязан == final_sha.
        head_sha = _git(vroot, "rev-parse", "HEAD")[1]
        revision_ok = (head_sha == final_sha)
        _vpol = (_tb2.sandbox_policy(child_root=str(vroot)) if sandbox
                 else _tb2.Policy(level="execution", child_root=str(vroot), block_push=True))
        coll = _ec2.collect(_pd2.detect(vroot), vroot, _vpol)
        final_checks = coll["checks"]
        _is_git = _git(vroot, "rev-parse", "--is-inside-work-tree")[0] == 0
        tree_clean = _ep._tree_clean_after_checks(vroot) if _is_git else True
        agg_reg, _agg_fix = _ep._diff_checks(base_checks, final_checks) if base_checks else ([], [])
        # v3.0-rc13 (P0): база security = sequence_base_sha (HEAD ДО пакета 1); деградация -> первый родитель
        _base_sha = sequence_base_sha
        if not _base_sha and final_sha:
            _fp = _git(vroot, "rev-list", "--max-parents=0", final_sha)[1].split("\n")[0]
            _base_sha = _fp or None
        agg_sec = None
        try:
            agg_sec = _sp2.run_pack(vroot, base=(_base_sha or None), signals=(signals or {}))
        except Exception:  # noqa: BLE001
            agg_sec = {"overall": "error"}
        # needs_review != провал — awaiting: закрываем независимым security-reviewer на aggregate-диффе
        agg_sec, agg_sec_review = _aggregate_close_security(
            agg_sec, vroot, _base_sha, final_sha, signals, reviewer_proposer, review,
            security_reviewer_proposer=security_reviewer_proposer,
            strict_judge_qualified=strict_judge_qualified, wid=wid, child_root=child_root)
        agg_sec_ok = (agg_sec or {}).get("overall") == "clear"
        agg_code_ok, agg_code_reviews = _aggregate_code_review(
            vroot, _base_sha, final_sha, signals, reviewer_proposer, review)
        return {"verified": True, "regressions": agg_reg, "no_regressions": not agg_reg,
                "final_sha": final_sha, "sequence_base_sha": _base_sha,
                "baseline_proven": baseline_proven,   # rc20 (P0): baseline доказан на точной базе
                "revision_ok": revision_ok, "tree_clean": tree_clean,
                "evidence_revision": coll.get("revision"),
                "evidence_revision_ok": (coll.get("revision") == final_sha),
                "security_overall": (agg_sec or {}).get("overall"), "security_ok": agg_sec_ok,
                "security_reviewer_status": (agg_sec or {}).get("reviewer_status"),
                "code_review_ok": agg_code_ok, "code_reviews": agg_code_reviews,
                "checks": {k: (v or {}).get("status") for k, v in (final_checks or {}).items()}}
    except Exception as e:  # noqa: BLE001
        return {"verified": False, "error": str(e)}


def execute_sequence(task, signals, child_root, packages, proposer_for, feature,
                     features_dir=None, base=None, provider_name="mock", model=None,
                     author=False, author_proposer=None, review=False, reviewer_proposer=None,
                     baseline_diff=True, install_deps=False, signals_for=None,
                     sandbox=False, open_pr=False, max_steps=40, write_scope_for=None, resume_from=None,
                     security_reviewer_proposer=None, strict_judge_qualified=True):
    """Исполнить список WorkPackages последовательно. proposer_for(pkg)->proposer; signals_for(pkg)->
    доп. сигналы пакета (опц.); write_scope_for(pkg)->список путей write-scope пакета (опц.).
    v2.120: НАСЛЕДУЕТ sandbox/install_deps/max_steps/провайдера обычного пути (containment не теряется);
    open_pr применяется к финальному пакету (интегрированная ветка). -> {kind, workitem_id, packages,
    completed, stopped_at, executed_all, ready_all, final_sha, sequential_chain}."""
    import ai_ops_run
    child_root = Path(child_root)
    features_dir = Path(features_dir) if features_dir else child_root / "features"
    wid = feature
    ordered = _ordered(packages)
    results, completed, stopped_at, final_sha = [], set(), None, None
    prev_sha = None
    chain_ok = True

    # v2.124/v3.0-rc4 (P0.3): IMMUTABLE parent SequencePlan С ХЭШАМИ. Фиксируем порядок/зависимости
    # ОДИН раз; при повторном вызове план мог быть перестроен planner'ом (тот же id — другой scope).
    # Если сохранённый план существует и его hash РАСХОДИТСЯ с текущим -> дрейф: resume запрещён
    # (нужен явный replan). Не перетираем родительский план локальным планом последнего пакета.
    cur_plan_hash = _plan_hash(ordered)
    pdir = features_dir / wid
    _sp = pdir / "sequence-plan.yaml"
    saved_plan = None
    # v3.0.8 (finding аудита P0.3): ФАЙЛ ОТСУТСТВУЕТ -> fresh; ЕСТЬ и валиден -> resume/existing; ЕСТЬ, но
    # НЕЧИТАЕМ/невалиден -> lifecycle-corrupted, ОСТАНОВКА (не молчаливая перезапись повреждённого источника).
    import yaml as _y
    try:
        pdir.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    if _sp.exists():
        try:
            _loaded = _y.safe_load(_sp.read_text(encoding="utf-8"))
        except Exception as _e:  # noqa: BLE001
            _loaded = _CORRUPT = object()   # маркер: файл есть, но не парсится
            _corrupt_reason = f"YAML не парсится: {_e}"
        else:
            _corrupt_reason = None
        _schema_err = (None if _corrupt_reason is not None
                       else _validate_sequence_plan_schema(_loaded, expected_wid=wid))
        if _corrupt_reason is not None or _schema_err is not None:
            import hashlib as _h
            _raw = b""
            try:
                _raw = _sp.read_bytes()
            except Exception:  # noqa: BLE001
                pass
            return _seq_err(wid, ordered,
                    (f"lifecycle-corrupted: {_sp} существует, но невалиден "
                     f"({_corrupt_reason or _schema_err}) — выполнение запрещено "
                     f"(повреждён источник истины). Нужна явная recovery, не автоперезапись."),
                    corrupt_sha256=(_h.sha256(_raw).hexdigest()[:16] if _raw else None),
                    corrupt_path=str(_sp))
        saved_plan = _loaded
    # NB: fresh SequencePlan пишется НИЖЕ, ПОСЛЕ разрешения base (иначе base_ref=None для auto) и
    # v3.0.7 (P0.3) — FAIL-CLOSED (atomic write + перечитывание): без immutable-плана нельзя доказать
    # base/порядок/hashes/checkpoint, поэтому сбой записи останавливает последовательность.
    # дрейф плана -> отказ (P0.3/v3.0.10): сохранённый план — ИММУТАБЕЛЬНЫЙ источник истины. Проверяем при
    # ЛЮБОМ существующем плане (не только resume_from): другой plan_hash от текущих пакетов = planner
    # перестроил план, исполнять по нему поверх старых отчётов небезопасно. Нужен явный replan.
    if saved_plan and saved_plan.get("plan_hash") and saved_plan["plan_hash"] != cur_plan_hash:
        return _seq_err(wid, ordered,
                ("SequencePlan дрейфнул с прошлого прогона (planner перестроил пакеты) — "
                 "resume по старым отчётам небезопасен. Нужен явный replan (пересобрать план "
                 "и переисполнить с нуля), а не resume_from."),
                plan_hash=cur_plan_hash, saved_plan_hash=saved_plan["plan_hash"])
    # v3.0.2/v3.0.7 (finding аудита P0): base_ref из СОХРАНЁННОГО плана — источник истины на resume/retry;
    # для fresh — строгий резолв (auto/explicit). Явная другая base на resume -> base-contract-drift.
    import execution_pipeline as _ep
    if saved_plan and saved_plan.get("base_ref"):
        if resume_from and base and base != saved_plan["base_ref"]:
            return _seq_err(wid, ordered,
                    (f"base-contract-drift: последовательность зафиксирована на base_ref="
                     f"'{saved_plan['base_ref']}', а передан --base '{base}'. Смена базы = другой "
                     f"контракт доставки — нужен явный replan, не resume с новой базой."))
        base = saved_plan["base_ref"]   # auto или совпадение -> берём сохранённую
    else:
        _br = _ep._resolve_base(child_root, base)   # base=None -> auto; явная -> строго
        if _br.get("mode") == "explicit" and not _br.get("resolved"):
            return _seq_err(wid, ordered,
                    (f"base-preflight: явная база '{base}' не разрешается в ветку "
                     f"({_br.get('reason')}) — последовательность не запущена (ноль пакетов)"))
        base = _br.get("base_ref") or base

    # v3.0.8 (finding аудита P0.2): SEQUENCE BASE SHA разрешается ДО записи плана и входит в тот же
    # АТОМАРНЫЙ _durable_write_yaml — никакого последующего best-effort дописывания. sequence_base_sha
    # связывает baseline/aggregate/resume/base-drift/verdict, поэтому обязан быть durable сразу.
    _cur_base = None
    try:
        _rc, _bs, _ = _git(child_root, "rev-parse", base or "HEAD")
        _cur_base = ((_bs or "").strip() if _rc == 0
                     else (_git(child_root, "rev-parse", "HEAD")[1] or "").strip()) or None
    except Exception:  # noqa: BLE001
        _cur_base = None
    base_drift = None
    if saved_plan is not None:
        # existing/resume: base_sha из сохранённого плана — источник истины; дрейф не молчим
        sequence_base_sha = saved_plan.get("sequence_base_sha")
        if _cur_base and sequence_base_sha and _cur_base != sequence_base_sha:
            base_drift = {"saved": sequence_base_sha, "current_base": _cur_base}
    else:
        # fresh: пишем ПОЛНЫЙ immutable-план ОДНИМ атомарным действием (fail-closed)
        sequence_base_sha = _cur_base
        _plan_doc = {"schema_version": 1, "kind": "SequencePlan", "workitem_id": wid, "total": len(ordered),
                     "plan_hash": cur_plan_hash, "base_ref": base, "sequence_base_sha": sequence_base_sha,
                     "packages": [{"id": p.get("id"), "order": p.get("order"),
                                   "depends_on": p.get("depends_on") or [], "scope": p.get("scope"),
                                   "write_scope": p.get("write_scope"), "pkg_hash": _pkg_hash(p)} for p in ordered]}
        _wr = _durable_write_yaml(_sp, _plan_doc,
                                  require_keys=("plan_hash", "base_ref", "sequence_base_sha", "packages"))
        if not _wr.get("ok"):
            return _seq_err(wid, ordered,
                    (f"lifecycle fail-closed: не удалось надёжно сохранить SequencePlan "
                     f"({_wr.get('error')}) — без immutable-плана нельзя доказать base/порядок/"
                     f"hashes/checkpoint/sequence_base_sha; последовательность не запущена"))
        saved_plan = _plan_doc

    # v3.0-rc16/rc20 (finding аудита P0): baseline СТРОГО на sequence_base_sha (detached worktree с
    # проверкой HEAD). rc20: БЕЗ fallback на child_root — если baseline не доказан на точной базе,
    # aggregate НЕДОСТУПЕН (baseline_proven=False) -> PR не открывается. Иначе sequence от develop мог
    # сравниться с baseline от main -> false green.
    _base_res = _collect_base_checks_at(child_root, sequence_base_sha, sandbox)
    base_checks = (_base_res or {}).get("checks") if _base_res else None
    baseline_proven = bool(_base_res and _base_res.get("proven"))

    # v2.124: resume с КОНКРЕТНОГО пакета — пакеты до него считаются исполненными в прошлом прогоне
    # (их SHA/готовность восстанавливаются из снимков work-packages/<pid>/report.json).
    start_index = 0
    if resume_from:
        _ids = [p.get("id") for p in ordered]
        # v3.0-rc2 (P0.3): неизвестный resume_from -> ОШИБКА, а не тихий старт с нуля.
        if resume_from not in _ids:
            return _seq_err(wid, ordered,
                    f"resume_from='{resume_from}' нет в SequencePlan (пакеты: {_ids})")
        start_index = _ids.index(resume_from)

    _branch = f"ai-ops/{wid}"
    _wt = child_root / ".ai" / "worktrees" / wid
    _rev_root = _wt if _wt.is_dir() else child_root

    def _verify_skipped(pid):
        # v3.0-rc2 (P0.3): пакет можно считать выполненным ТОЛЬКО при подтверждении: отчёт есть, SHA есть,
        # SHA реально коммит в sequence-ветке и предок её HEAD, пакет executed и без hard-блокера.
        prior = features_dir / wid / "work-packages" / pid / "report.json"
        if not prior.is_file():
            return None, "нет отчёта прошлого прогона"
        try:
            rep = json.loads(prior.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return None, f"битый отчёт: {e}"
        sha = (rep.get("commit") or {}).get("sha")
        if not sha:
            return None, "в отчёте нет commit SHA"
        if _git(_rev_root, "cat-file", "-e", sha + "^{commit}")[0] != 0:
            return None, f"SHA {sha[:8]} не существует в репозитории"
        if _git(_rev_root, "merge-base", "--is-ancestor", sha, _branch)[0] != 0:
            return None, f"SHA {sha[:8]} не в sequence-ветке {_branch} (не предок HEAD)"
        if not rep.get("ready_for_pr") and _hard_stop(rep) is not None:
            return None, f"пакет имел hard-блокер: {_hard_stop(rep)}"
        return rep, None

    for i, pkg in enumerate(ordered):
        pid = pkg.get("id", f"pkg-{i+1}")
        if i < start_index:
            rep_ok, why = _verify_skipped(pid)
            if rep_ok is None:
                # неподтверждённый пропуск -> НЕ добавляем в completed, останавливаем resume честной ошибкой
                return _seq_err(wid, ordered,
                        f"resume: пакет '{pid}' до resume_from не подтверждён — {why}",
                        packages=results, completed=sorted(completed), stopped_at=pid)
            psha = (rep_ok.get("commit") or {}).get("sha")
            completed.add(pid)
            prev_sha = final_sha = psha
            results.append({"id": pid, "sha": psha, "ready": bool(rep_ok.get("ready_for_pr")),
                            "executed": True, "status": "resumed-skip", "resume_point": psha})
            continue
        # v3.0-rc4 (P0.4): ТОЧНЫЙ checkpoint — перед исполнением resume_from-пакета HEAD sequence-ветки
        # обязан быть РОВНО на коммите предшественника (prev_sha), а не «где-то потомком». Иначе ветка
        # могла уже содержать попытки package N+1, и новый прогон строился бы поверх чужого HEAD.
        if start_index > 0 and i == start_index and prev_sha:
            _head = _git(_rev_root, "rev-parse", _branch)[1]
            if _head != prev_sha:
                return _seq_err(wid, ordered,
                        (f"resume: HEAD ветки {_branch} ({(_head or '?')[:8]}) НЕ на checkpoint "
                         f"предшественника ({prev_sha[:8]}) — ветка ушла вперёд (попытки "
                         f"последующих пакетов?). Сбрось worktree на {prev_sha[:8]} и повтори."),
                        packages=results, completed=sorted(completed), stopped_at=pid)
        unmet_deps = [d for d in (pkg.get("depends_on") or []) if d not in completed]
        if unmet_deps:
            results.append({"id": pid, "status": "blocked-dependency",
                            "unmet_deps": unmet_deps, "sha": None, "ready": False})
            stopped_at = pid
            break

        sig_pkg = dict(signals or {})
        sig_pkg["affected_areas"] = pkg.get("scope") or sig_pkg.get("affected_areas") or ["core"]
        sig_pkg["size"] = "small"
        # исполнитель = подтверждение декомпозиции: пакет атомарен, его id выбран, и передан
        # АВТОРИТЕТНЫЙ список id плана (preflight валидирует work_package_id против него — P0.4/P0.5).
        sig_pkg["work_package_id"] = pid
        sig_pkg["_sequence_plan_ids"] = [p.get("id") for p in ordered]
        sig_pkg["_sequence_internal"] = True   # v3.0-rc4 (P0.1): внутренний per-package resume ≠ replan
        if signals_for:
            sig_pkg.update(signals_for(pkg) or {})

        is_last = (i == len(ordered) - 1)
        # v3.0-rc19 (finding живого sequential): каждый пакет получал ПОЛНУЮ многочастную задачу с
        # общим ярлыком -> writer лез в чужие подсистемы (напр. в pkg-1 писал pricing/*) -> брокер
        # отклонял, но _hard_stop справедливо стопал цепочку на попытке эскейпа. Явно ограничиваем
        # writer'а рамками ЕГО подсистемы (остальные части делают отдельные пакеты) — это НЕ ослабляет
        # containment (брокер/post-diff по-прежнему в силе), а убирает первопричину: writer не выходит
        # за scope. Ревьюер по-прежнему судит независимо.
        _pkg_scope = pkg.get("scope") or []
        _pkg_ws = pkg.get("write_scope") or []
        _scope_note = ""
        if _pkg_scope:
            _scope_note = (
                f"\n\n=== ГРАНИЦЫ ЭТОГО ПАКЕТА ({pid}) ===\n"
                f"Реализуй ТОЛЬКО часть, относящуюся к подсистеме: {', '.join(_pkg_scope)}. "
                f"Пиши ИСКЛЮЧИТЕЛЬНО в пути: {', '.join(_pkg_ws) or _pkg_scope}. "
                f"НЕ трогай файлы других подсистем — их реализуют отдельные пакеты последовательности. "
                f"Если задача упоминает другие подсистемы — это контекст, не работа этого пакета.")
        pkg_task = f"{task} — пакет {pid}: {pkg.get('title', '')}{_scope_note}".strip()
        # v3.0-rc12 (finding живого sequential): исключение провайдера/инфры (напр. ConnectionReset
        # от kimi ПОСЛЕ исчерпания ретраев _http_post_json) НЕ должно ронять всю транзакцию traceback'ом
        # и терять per-package lifecycle. Ловим -> пакет честно фейлится (infra-error) -> цепочка
        # hard-stop с ясной причиной; прежние пакеты/план/снимки сохранены (durable/resumable).
        infra_error = None
        try:
            rep = ai_ops_run.run(pkg_task, sig_pkg, child_root, features_dir=str(features_dir),
                                 engine="pipeline", proposer=proposer_for(pkg), execute=True,
                                 feature=wid, resume=(i > 0 or start_index > 0), base=base, provider_name=provider_name,
                                 model=model, author=author, author_proposer=author_proposer,
                                 review=review, reviewer_proposer=reviewer_proposer,
                                 baseline_diff=baseline_diff, install_deps=install_deps,
                                 # v2.124 (P0.4): пакеты НИКОГДА не открывают PR — доставка отдельным шагом
                                 # ПОСЛЕ агрегатного вердикта (ready_all). Иначе готовый финальный пакет открыл
                                 # бы PR, пока ранний пакет awaiting-evidence -> PR при ready_all=false.
                                 sandbox=sandbox, max_steps=max_steps, open_pr=False,
                                 write_scope=(write_scope_for(pkg) if write_scope_for else None))
        except (KeyboardInterrupt, SystemExit):
            raise                                       # намеренное прерывание/честный fail ключа — не глотаем
        except Exception as _e:  # noqa: BLE001 — belt-and-suspenders: если run сам НЕ поймал (не должен)
            infra_error = _classify_failure(_e)
            rep = {"schema_version": 1, "kind": "run-report", "status": "error",
                   "error": f"{infra_error['exception_type']}: {infra_error['message']}",
                   "failure": infra_error, "commit": {}, "loop": {}, "gates": {}, "reviews": []}
        # v3.0-rc17: ai_ops_run сам ловит и типизирует сбой провайдера/инфры (единая точка containment) —
        # читаем failure-envelope из его error-отчёта, чтобы пакет честно фейлился с типом сбоя.
        if infra_error is None and rep.get("status") == "error" and rep.get("failure"):
            infra_error = rep.get("failure")

        sha = (rep.get("commit") or {}).get("sha")
        # v2.120 (P0.3): ОСТАНОВ цепочки на НАСТОЯЩЕМ блокере — нельзя строить зависимый пакет поверх.
        # Отличаем «awaiting evidence» (нет author/review -> исполнен, но не ready, цепочка идёт) от
        # deterministic/security/reviewer FAIL и регрессии (обязаны остановить).
        stop_reason = (f"{infra_error['failure_class']}-error: {infra_error['exception_type']}"
                       if infra_error else _hard_stop(rep))
        # v2.123 (P0.3): ПОСТ-ДИФФ проверка write_scope — пакет не должен был изменить НИЧЕГО вне своего
        # каталога (belt-and-suspenders поверх брокера, который отклоняет out-of-scope записи в петле).
        # Escape = scope-violation -> останавливает последовательность (нельзя строить поверх «уехавшего»).
        pkg_scope = write_scope_for(pkg) if write_scope_for else None
        if stop_reason is None and sha and pkg_scope:
            import approvals as _appr
            wt = child_root / ".ai" / "worktrees" / wid
            changed = _changed_files(wt if wt.is_dir() else child_root, sha)
            # v2.124.1 (finding живого прогона): write_scope ограничивает КОД модели, а НЕ артефакты,
            # которые пишет сам движок (pre-authoring: .ai/runplan/, openspec/, features/, .ai/). Иначе
            # authored requirements/openspec ложно ловятся как scope-violation и убивают цепочку на п.1.
            outside = [f for f in changed
                       if not f.startswith((".ai/", "openspec/", "features/"))
                       and not _appr.covers_paths({"scope": " ".join(pkg_scope)}, [f])]
            if outside:
                stop_reason = f"scope-violation: пакет изменил пути вне write_scope: {', '.join(outside[:5])}"
        hard_blocked = rep.get("status") == "blocked"
        executed = bool(sha) and stop_reason is None
        ready = bool(rep.get("ready_for_pr"))
        blocked = stop_reason is not None

        # сохранить per-package отчёт + СНИМОК lifecycle-артефактов (v2.124): родительские
        # features/<wid>/{run-plan,run-handoff,...} перетираются следующим пакетом -> у каждого пакета
        # свой неизменный lifecycle-каталог work-packages/<pid>/ (P1 аудита).
        pkg_dir = features_dir / wid / "work-packages" / pid
        # v3.0.12 (finding аудита блок B): report.json — ЧЕКПОИНТ resume/retry. Пишем АТОМАРНО (tmp+fsync+
        # replace); сбой БОЛЬШЕ НЕ гаснет молча (иначе пакет помечен completed, следующий строится поверх
        # коммита, но durable-чекпоинта нет) -> HARD-STOP цепочки. Снимки lifecycle — best-effort аудит-копии.
        _persist_err = None
        try:
            import os as _os
            pkg_dir.mkdir(parents=True, exist_ok=True)
            _tmp = pkg_dir / "report.json.tmp"
            with open(_tmp, "w", encoding="utf-8") as _f:
                _f.write(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
                _f.flush()
                _os.fsync(_f.fileno())
            _os.replace(_tmp, pkg_dir / "report.json")
        except OSError as _e:
            _persist_err = f"{type(_e).__name__}: {_e}"
        if _persist_err is None:
            try:
                for _art in ("run-plan.yaml", "run-handoff.yaml", "context-bundle.yaml", "spec-coverage.yaml"):
                    _src = features_dir / wid / _art
                    if _src.is_file():
                        (pkg_dir / _art).write_text(_src.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass
        else:
            blocked = True   # чекпоинт не сохранён durable -> нельзя строить следующий пакет поверх
            if not stop_reason:
                stop_reason = f"checkpoint-persist-failed: {_persist_err}"

        # последовательность: коммит пакета — потомок коммита предыдущего (строим поверх, не параллельно)
        if executed and prev_sha and sha:
            wt = child_root / ".ai" / "worktrees" / wid
            rc, _, _ = _git(wt if wt.is_dir() else child_root, "merge-base", "--is-ancestor", prev_sha, sha)
            if rc != 0:
                chain_ok = False

        status = (stop_reason if stop_reason else ("executed" if executed else "no-commit"))
        results.append({"id": pid, "sha": sha, "ready": ready, "executed": executed,
                        "blocked": blocked, "stop_reason": stop_reason,
                        "pkg_hash": _pkg_hash(pkg),   # v3.0-rc4 (P0.3): отчёт привязан к хэшу определения
                        "gates_unmet": (rep.get("gates") or {}).get("unmet"),
                        "resume_point": sha,   # точка resume пакета
                        "handoff": (rep.get("handoff") or {}).get("next_action"),
                        "failure": infra_error,   # v3.0-rc13 (P1): типизированный envelope (None если не исключение)
                        "status": status})
        # v3.0.14 (#3): event journal — package_end (Run->Package->Gate: gates_unmet + статус пакета)
        try:
            import lifecycle_store as _lsj
            _lsj.journal_append(features_dir / wid / "lifecycle-journal.jsonl",
                                {"kind": "package_end", "run_id": wid, "workitem_id": wid,
                                 "package_id": pid, "pkg_hash": _pkg_hash(pkg), "sha": sha,
                                 "ready": bool(ready), "status": status,
                                 "gates_unmet": (rep.get("gates") or {}).get("unmet")})
        except Exception:  # noqa: BLE001 — журнал не роняет исполнение
            pass

        if executed:
            completed.add(pid)
            prev_sha = sha
            final_sha = sha
        if not executed or blocked:
            stopped_at = pid
            break

    executed_all = len(completed) == len(ordered) and stopped_at is None
    ready_all = executed_all and all(r.get("ready") for r in results)

    # v2.124: AGGREGATE verification на ФИНАЛЬНОМ интегрированном SHA — перепроверяем результат ЦЕЛИКОМ
    # (не только конъюнкцию per-package вердиктов), чтобы поймать межпакетные взаимодействия (каждый
    # пакет зелен по отдельности, но интеграция сломана). Сравниваем финальные проверки с БАЗОЙ (до п.1).
    # v3.0.13 (блок C): тело aggregate-верификации вынесено в _aggregate_verify (чистый вход->dict) —
    # execute_sequence перестал быть god-функцией на этом участке, поведение идентично.
    aggregate = {"verified": False}
    if executed_all and final_sha:
        aggregate = _aggregate_verify(child_root, wid, sandbox, final_sha, base_checks,
                                      sequence_base_sha, signals, reviewer_proposer, review, baseline_proven,
                                      security_reviewer_proposer=security_reviewer_proposer,
                                      strict_judge_qualified=strict_judge_qualified)
    # v3.0-rc2/rc4 (P0.4/P1.1): FAIL-CLOSED. aggregate_ready ТОЛЬКО если верификация РЕАЛЬНО выполнена
    # И чиста: verified, нет регрессий, HEAD==final_sha, evidence на final_sha, дерево чистое, агрегатный
    # security clear на полном диффе. Сбой/недоступность -> НЕ ready.
    # v3.0-rc20 (finding аудита P0): + baseline ДОКАЗАН на точной sequence_base_sha (нет fallback-базы),
    # + НЕТ base_drift (base-ветка не сдвинулась с начала цепочки) — иначе evidence против не той базы.
    agg_ok = bool(aggregate.get("verified") and aggregate.get("no_regressions")
                  and aggregate.get("baseline_proven")
                  and aggregate.get("revision_ok") and aggregate.get("tree_clean")
                  and aggregate.get("evidence_revision_ok") and aggregate.get("security_ok")
                  and aggregate.get("code_review_ok", True))
    aggregate_ready = ready_all and chain_ok and agg_ok and (base_drift is None)

    # v2.124 (P0.4): доставка draft PR — ОТДЕЛЬНЫЙ шаг ПОСЛЕ агрегатного вердикта, на финальном
    # интегрированном SHA. PR открывается ТОЛЬКО при aggregate_ready — не по готовности отдельного пакета.
    pr, delivery = None, {"requested": bool(open_pr), "status": "not-requested" if not open_pr else None}
    if open_pr:
        if aggregate_ready and final_sha:
            wt = child_root / ".ai" / "worktrees" / wid
            _drt = wt if wt.is_dir() else child_root
            # v3.0-rc20 (finding аудита P0): DELIVERY BASE BINDING — evidence собрано против
            # sequence_base_sha; перед PR сверяем АКТУАЛЬНУЮ remote base с этой базой. Разошлась
            # (remote main сдвинулся после старта цепочки) -> НЕ открываем PR (проверенное состояние
            # != потенциальному merge-состоянию); нужна ревалидация. Иначе — «verified» PR был бы ложью.
            # v3.0.9 (finding аудита P0): ЕДИНЫЙ fail-closed RemoteBaseVerifier (как single-run). Раньше
            # sequential был fail-OPEN: remote_base=None (нет origin/сети/ветки) -> else -> открывал PR.
            # Теперь: verified-equal -> PR; verified-moved -> revalidation; unverifiable -> unavailable.
            _rv = _ep._verify_remote_base(_drt, base, sequence_base_sha)
            _vd = _rv.get("verdict")
            if _vd == "verified-equal":
                try:
                    import pr_open
                    pr = pr_open.open_draft_pr(_drt, f"ai-ops/{wid}", base=base,
                                               title=f"ai-ops: {task[:60]}",
                                               body=(f"Sequential WorkPackages: {len(ordered)} пакет(ов). "
                                                     f"База {base} ({(sequence_base_sha or '?')[:12]}) → финал {final_sha}. "
                                                     f"Агрегатный вердикт: aggregate_ready."))
                    delivery["status"] = (pr or {}).get("status") or "failed"
                    delivery["validated_base"] = sequence_base_sha
                    delivery["base_ref"] = base
                except Exception as e:  # noqa: BLE001
                    delivery["status"] = "failed"
                    delivery["error"] = str(e)
            elif _vd == "verified-moved":
                delivery["status"] = "not-attempted"
                delivery["base_moved"] = {"base_ref": base, "validated_base": sequence_base_sha,
                                          "remote_base": _rv.get("remote_sha")}
                delivery["reason"] = ("remote base сдвинулась с момента сбора evidence — нужна ревалидация; "
                                      "PR не открыт (иначе непроверенное merge-состояние)")
            else:   # unverifiable -> доставка НЕДОСТУПНА (fail-closed), НЕ открываем PR
                delivery["status"] = "unavailable"
                delivery["reason"] = f"remote-base-unverified: {_rv.get('reason')} — доставка невозможна fail-closed"
        else:
            delivery["status"] = "not-attempted"   # последовательность не готова -> PR НЕ открываем

    seq = {"schema_version": 1, "kind": "WorkPackageSequence", "workitem_id": wid,
           "packages": results, "completed": sorted(completed), "stopped_at": stopped_at,
           "executed_all": executed_all, "ready_all": ready_all, "aggregate_ready": aggregate_ready,
           "final_sha": final_sha, "sequential_chain": chain_ok, "total": len(ordered),
           "sequence_base_sha": sequence_base_sha, "base_drift": base_drift,   # rc16 (P0/P1)
           "aggregate": aggregate, "delivery": delivery, "draft_pr": pr,
           "resumed_from": resume_from}
    # v3.0.14 (finding аудита #2): sequence-report — durable (атомарно); сбой фиксируем в отчёте, не молчим
    import lifecycle_store as _ls2
    _sr = _ls2.durable_write(features_dir / wid / "sequence-report.yaml", seq)
    if not _sr.get("ok"):
        seq["report_persist_error"] = _sr.get("error")
    return seq


def selftest():
    import tempfile
    import io
    import contextlib
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    import atomic_planner

    def mkrepo(td):
        (Path(td) / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        for a in (("init", "-q"), ("config", "user.email", "t@t"), ("config", "user.name", "t"),
                  ("add", "-A"), ("commit", "-q", "-m", "i")):
            _git(td, *a)
        return _git(td, "rev-parse", "--abbrev-ref", "HEAD")[1]

    def author(prompt):
        if "requirements-artifact" in prompt:
            return ("schema_version: 1\nkind: requirements-artifact\nrequirements:\n"
                    "  - id: R1\n    statement: пакет реализован\n    acceptance:\n      - when готово then тест зелёный\n")
        if "spec-change" in prompt:
            return ("schema_version: 1\nkind: spec-change\ncapability: mod\nwhy: нужно\n"
                    "what_changes:\n  - изменение\ntasks:\n  - шаг\nrequirements:\n"
                    "  - name: R\n    text: The system SHALL work.\n    scenarios:\n"
                    "      - {name: T, when: x, then: y}\n")
        return ("schema_version: 1\nkind: plan-artifact\nwork_packages:\n"
                "  - id: WP1\n    summary: пакет\n    depends_on: []\nwrite_scope:\n  - .\n")

    def _pass_reviewer(prompt):
        # v3.0.1: mock-ревьюер pass. Для security-промпта (SecurityVerdict v2) парсит применимые
        # домены из промпта и эмитит domain_results по каждому — иначе строгий контракт отклонит.
        import re as _re
        import json as _json
        p = prompt or ""
        # v3.0.11: ревьюер СНАЧАЛА реально читает изменённый файл (реальный trace), затем выносит pass —
        # иначе блокирующий гейт (code_review/ux_review) не закрывается по 0-read рубер-стампу, а security-
        # evidence code-read не сойдётся с trace. Файл берём из seeded-диффа (fallback calc.py).
        _cand = _re.search(r"\+\+\+ b/(\S+)", p)
        _path = _cand.group(1) if _cand else "calc.py"
        if f"--- {_path} ---" not in p:                      # ещё не читал -> читаем
            return _json.dumps({"op": "read", "path": _path})
        res = {"kind": "reviewer-result", "status": "pass", "checks": [{"id": "ok", "status": "pass"}]}
        m = _re.search(r"применимым доменам:\s*([^\n(]+)", p)
        if m:
            doms = [d.strip() for d in m.group(1).split(",") if d.strip()]
            if doms:
                # evidence code-read ссылается на РЕАЛЬНО прочитанный файл (сверка с trace, без basename-fallback)
                res["domain_results"] = [{"domain": d, "status": "pass",
                                          "checks": [{"id": f"{d}_ok", "status": "pass"}],
                                          "evidence": [{"type": "code-read", "path": _path, "lines": "1-10"}]}
                                         for d in doms]
        return _json.dumps(res, ensure_ascii=False)
    reviewer = _pass_reviewer

    # ENGINEERING по 3 подсистемам -> 3 пакета с цепочкой зависимостей
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cur = mkrepo(td)
        sig = {"task_type": "ENGINEERING", "size": "large", "risk": "low",
               "affected_areas": ["catalog", "orders", "billing"]}
        wp = atomic_planner.decompose(sig, wid="seq", child_root=root)
        pkgs = wp["work_packages"]
        expect("executor: план дал 3 пакета by-subsystem с deps",
               len(pkgs) == 3 and pkgs[1]["depends_on"] == [pkgs[0]["id"]])

        # per-package proposer: каждый пакет пишет свой файл
        def prop_for(pkg):
            fname = f"src/{pkg['id']}.py"
            it = iter([{"op": "write", "path": fname, "content": f"# {pkg['id']}\nx=1\n"},
                       {"done": True}])
            return lambda c: next(it)

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            seq = execute_sequence("большой рефактор", sig, root, pkgs, prop_for, feature="seq",
                                   base=cur, author=True, author_proposer=author,
                                   review=True, reviewer_proposer=reviewer)
        shas = [p.get("sha") for p in seq["packages"]]
        expect("executor: все 3 пакета исполнены (executed_all)", seq["executed_all"] is True)
        expect("executor: у каждого пакета свой уникальный SHA (свой коммит)",
               all(shas) and len(set(shas)) == 3)
        expect("executor: последовательная цепочка (пакет N поверх N-1)", seq["sequential_chain"] is True)
        expect("executor: per-package отчёты на диске",
               all((root / "features" / "seq" / "work-packages" / p["id"] / "report.json").is_file()
                   for p in seq["packages"]))
        expect("executor: sequence-report сохранён", (root / "features" / "seq" / "sequence-report.yaml").is_file())
        # каждый пакет имеет точку resume (SHA) и запись handoff
        expect("executor: у каждого пакета точка resume (SHA)",
               all(p.get("resume_point") for p in seq["packages"]))
        # v2.124: immutable parent SequencePlan + per-package lifecycle-снимок + агрегатный вердикт
        expect("v2.124: immutable sequence-plan.yaml записан",
               (root / "features" / "seq" / "sequence-plan.yaml").is_file())
        # v3.0.8 (finding аудита P0.2): sequence_base_sha записан durable СРАЗУ (в том же плане, не best-effort)
        import yaml as _yy
        _plan = _yy.safe_load((root / "features" / "seq" / "sequence-plan.yaml").read_text(encoding="utf-8"))
        expect("v3.0.8: sequence_base_sha + base_ref в durable-плане сразу (не дописан позже)",
               bool(_plan.get("sequence_base_sha")) and bool(_plan.get("base_ref")))
        # v3.0.8 (finding аудита P0.3): ПОВРЕЖДЁННЫЙ SequencePlan -> lifecycle-corrupted (halt), НЕ перезапись
        _corrupt_root = None
        with tempfile.TemporaryDirectory() as _tdc:
            _rc = Path(_tdc); cur_c = mkrepo(_tdc)
            _pk = atomic_planner.decompose(sig, wid="seqc", child_root=_rc)["work_packages"]
            (_rc / "features" / "seqc").mkdir(parents=True, exist_ok=True)
            (_rc / "features" / "seqc" / "sequence-plan.yaml").write_text("{ это: [не, валидный, yaml", encoding="utf-8")
            _before = (_rc / "features" / "seqc" / "sequence-plan.yaml").read_text(encoding="utf-8")
            seq_c = execute_sequence("x", sig, _rc, _pk, prop_for, feature="seqc", base=cur_c,
                                     author=True, author_proposer=author, review=True, reviewer_proposer=reviewer)
            _after = (_rc / "features" / "seqc" / "sequence-plan.yaml").read_text(encoding="utf-8")
            expect("v3.0.8 P0.3: повреждённый SequencePlan -> lifecycle-corrupted, 0 пакетов, файл НЕ перезаписан",
                   "lifecycle-corrupted" in (seq_c.get("error") or "") and not seq_c.get("packages")
                   and _after == _before and seq_c.get("corrupt_sha256"))
        expect("v2.124: у каждого пакета снимок lifecycle (run-plan.yaml в своём каталоге)",
               all((root / "features" / "seq" / "work-packages" / p["id"] / "run-plan.yaml").is_file()
                   for p in seq["packages"]))
        expect("v2.124: агрегатный вердикт (aggregate_ready) в отчёте", "aggregate_ready" in seq)
        expect("v2.124: aggregate verify на финальном SHA выполнен (verified)",
               (seq.get("aggregate") or {}).get("verified") is True
               and (seq.get("aggregate") or {}).get("final_sha") == seq["final_sha"])

        # v3.0-rc4 (P0.4): после полного прогона HEAD ветки на пакете 3 -> resume с пакета 2 запрещён
        # (ветка ушла вперёд checkpoint предшественника).
        seq_drift = execute_sequence("x", sig, root, pkgs, prop_for, feature="seq", base=cur,
                                     author=True, author_proposer=author, review=True, reviewer_proposer=reviewer,
                                     resume_from=pkgs[1]["id"])
        expect("v3.0-rc4 resume (P0.4): HEAD не на checkpoint предшественника -> error",
               "error" in seq_drift and "checkpoint" in (seq_drift.get("error") or "").lower())
        # валидный resume: сбрасываем ветку на checkpoint пакета 1, затем resume с пакета 2
        _git(root / ".ai" / "worktrees" / "seq", "reset", "--hard", seq["packages"][0]["sha"])
        buf2 = io.StringIO()
        with contextlib.redirect_stderr(buf2):
            seq_r = execute_sequence("большой рефактор", sig, root, pkgs, prop_for, feature="seq",
                                     base=cur, author=True, author_proposer=author,
                                     review=True, reviewer_proposer=reviewer,
                                     resume_from=pkgs[1]["id"])
        skipped = [p for p in seq_r["packages"] if p.get("status") == "resumed-skip"]
        expect("v2.124/rc4 resume: валидный checkpoint -> пакет 1 resumed-skip, дальше исполняется",
               "error" not in seq_r and seq_r.get("resumed_from") == pkgs[1]["id"] and len(skipped) == 1
               and skipped[0]["id"] == pkgs[0]["id"] and skipped[0].get("sha"))
        # v3.0-rc2 (P0.3): неизвестный resume_from -> ОШИБКА, не тихий старт с нуля
        seq_bad = execute_sequence("x", sig, root, pkgs, prop_for, feature="seq", base=cur,
                                   resume_from="pkg-НЕТ-ТАКОГО")
        expect("v3.0-rc2 resume: неизвестный resume_from -> error (не старт с нуля)",
               "error" in seq_bad and seq_bad["executed_all"] is False and not seq_bad["packages"])
        # v3.0-rc4 (P0.3): дрейф SequencePlan (planner перестроил пакеты) -> resume запрещён
        pkgs_drift = [dict(p) for p in pkgs]
        pkgs_drift[0] = {**pkgs_drift[0], "scope": ["ДРУГАЯ-ПОДСИСТЕМА"]}   # тот же id, другой scope -> др. hash
        seq_pd = execute_sequence("x", sig, root, pkgs_drift, prop_for, feature="seq", base=cur,
                                  resume_from=pkgs[1]["id"])
        expect("v3.0-rc4 resume (P0.3): дрейф SequencePlan -> error (нужен replan)",
               "error" in seq_pd and "дрейф" in (seq_pd.get("error") or "").lower())
        # v3.0.10 (finding аудита P1): дрейф ловится и БЕЗ resume_from (сохранённый план — иммутабелен).
        seq_pd2 = execute_sequence("x", sig, root, pkgs_drift, prop_for, feature="seq", base=cur)
        expect("v3.0.10: дрейф SequencePlan БЕЗ resume_from -> error (план иммутабелен)",
               "error" in seq_pd2 and "дрейф" in (seq_pd2.get("error") or "").lower())

        # v3.0.10 (finding аудита P1): ПОЛНАЯ integrity-валидация SequencePlan (чистая функция).
        def _valid_plan(wid="seq"):
            _o = _ordered([{"id": "WP1", "order": 1, "depends_on": [], "scope": "a", "write_scope": ["."]},
                           {"id": "WP2", "order": 2, "depends_on": ["WP1"], "scope": "b", "write_scope": ["."]}])
            return {"schema_version": 1, "kind": "SequencePlan", "workitem_id": wid, "total": 2,
                    "plan_hash": _plan_hash(_o), "base_ref": "main", "sequence_base_sha": "deadbeef",
                    "packages": [{"id": p["id"], "order": p["order"], "depends_on": p["depends_on"],
                                  "scope": p["scope"], "write_scope": p["write_scope"],
                                  "pkg_hash": _pkg_hash(p)} for p in _o]}
        expect("v3.0.10 plan-integrity: валидный план -> None",
               _validate_sequence_plan_schema(_valid_plan(), expected_wid="seq") is None)
        expect("v3.0.10 plan-integrity: чужой workitem_id -> ошибка",
               "чужой" in (_validate_sequence_plan_schema(_valid_plan("OTHER"), expected_wid="seq") or ""))
        expect("v3.0.10 plan-integrity: неподдерживаемая schema_version -> ошибка",
               "schema_version" in (_validate_sequence_plan_schema({**_valid_plan(), "schema_version": 2},
                                                                    expected_wid="seq") or ""))
        def _reseal(plan):   # пересчитать pkg_hash каждого пакета + общий plan_hash (чтобы тест изолировал
            for _p in plan["packages"]:                     # СТРУКТУРНОЕ нарушение, а не дрейф хэша)
                _p["pkg_hash"] = _pkg_hash(_p)
            plan["plan_hash"] = _plan_hash(_ordered(plan["packages"]))
            return plan
        _dup = _valid_plan(); _dup["packages"][1]["id"] = "WP1"; _reseal(_dup)
        expect("v3.0.10 plan-integrity: дубль package id -> ошибка",
               "дубли package id" in (_validate_sequence_plan_schema(_dup, expected_wid="seq") or ""))
        _dupord = _valid_plan(); _dupord["packages"][1]["order"] = 1; _reseal(_dupord)
        expect("v3.0.10 plan-integrity: дубль order -> ошибка",
               "дубли order" in (_validate_sequence_plan_schema(_dupord, expected_wid="seq") or ""))
        _baddep = _valid_plan(); _baddep["packages"][1]["depends_on"] = ["WP-NONE"]; _reseal(_baddep)
        expect("v3.0.10 plan-integrity: depends_on на несуществующий пакет -> ошибка",
               "несуществующего" in (_validate_sequence_plan_schema(_baddep, expected_wid="seq") or ""))
        _cyc = _valid_plan(); _cyc["packages"][0]["depends_on"] = ["WP2"]; _reseal(_cyc)   # WP1<->WP2 цикл
        expect("v3.0.10 plan-integrity: цикл зависимостей -> ошибка",
               "цикл" in (_validate_sequence_plan_schema(_cyc, expected_wid="seq") or ""))
        _badpk = _valid_plan(); _badpk["packages"][0]["pkg_hash"] = "0" * 16
        expect("v3.0.10 plan-integrity: подменённый pkg_hash -> ошибка",
               "pkg_hash не сходится" in (_validate_sequence_plan_schema(_badpk, expected_wid="seq") or ""))
        _badph = _valid_plan(); _badph["plan_hash"] = "0" * 16
        expect("v3.0.10 plan-integrity: подменённый plan_hash -> ошибка",
               "plan_hash не сходится" in (_validate_sequence_plan_schema(_badph, expected_wid="seq") or ""))
        # v3.0-rc2 (P0.3): пакет до resume_from без подтверждённого снимка -> error (не добавляем в completed)
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td_e:
            re = Path(td_e); cur_e = mkrepo(td_e)
            pkgs_e = atomic_planner.decompose(sig, wid="seqe", child_root=re)["work_packages"]
            seq_e = execute_sequence("x", sig, re, pkgs_e, prop_for, feature="seqe", base=cur_e,
                                     resume_from=pkgs_e[1]["id"])   # снимков нет — пакет 1 не подтверждён
            expect("v3.0-rc2 resume: неподтверждённый пропущенный пакет -> error (не в completed)",
                   "error" in seq_e and pkgs_e[0]["id"] not in seq_e.get("completed", []))

        # v3.0-rc12 (finding живого sequential): исключение провайдера/инфры (ConnectionReset и т.п.)
        # ПОСЛЕ исчерпания ретраев НЕ роняет всю транзакцию traceback'ом — пакет честно фейлится
        # (infra-error), цепочка hard-stop, последующие пакеты НЕ исполняются, снимок пакета сохранён.
        with _tf.TemporaryDirectory() as td_x:
            rx = Path(td_x); cur_x = mkrepo(td_x)
            pkgs_x = atomic_planner.decompose(sig, wid="seqx", child_root=rx)["work_packages"]
            def prop_boom(pkg):
                return lambda c: {"done": True}          # не достигается — author падает раньше
            def boom_author(prompt):                     # воспроизводит живой сбой: ConnectionReset в author-вызове
                raise ConnectionResetError("[Errno 54] Connection reset by peer")
            seq_x = execute_sequence("x", sig, rx, pkgs_x, prop_boom, feature="seqx",
                                     base=cur_x, author=True, author_proposer=boom_author, review=False)
            p0 = seq_x["packages"][0] if seq_x.get("packages") else {}
            expect("v3.0-rc12: исключение провайдера -> НЕ traceback, пакет 1 честно остановлен",
                   bool(p0.get("stop_reason")) and "error" in (p0.get("stop_reason") or ""))
            # v3.0-rc13 (P1): типизированный failure envelope — ConnectionReset -> network, retryable
            expect("v3.0-rc13: failure классифицирован (network/retryable), не blanket infra-error",
                   (p0.get("failure") or {}).get("failure_class") == "network"
                   and (p0.get("failure") or {}).get("retryable") is True
                   and (p0.get("failure") or {}).get("exception_type") == "ConnectionResetError"
                   and (p0.get("failure") or {}).get("traceback_hash"))
            expect("v3.0-rc12: цепочка стоп на пакете 1, пакеты 2/3 НЕ исполнены (durable stop)",
                   seq_x.get("stopped_at") == pkgs_x[0]["id"] and len(seq_x["packages"]) == 1
                   and seq_x["executed_all"] is False and seq_x["ready_all"] is False)
            expect("v3.0-rc12: per-package снимок пакета 1 сохранён (транзакция не потеряла состояние)",
                   (rx / "features" / "seqx" / "work-packages" / pkgs_x[0]["id"] / "report.json").is_file())

        # v2.124.1 (finding живого прогона): с write_scope_for + author артефакты движка (.ai/runplan,
        # openspec) НЕ должны ловиться как scope-violation — write_scope ограничивает КОД, не артефакты.
        def prop_ws(pkg):
            sub = (pkg.get("scope") or ["core"])[0]
            it = iter([{"op": "write", "path": f"src/{sub}/mod.py", "content": "x = 1\n"}, {"done": True}])
            return lambda c: next(it)
        buf3 = io.StringIO()
        with contextlib.redirect_stderr(buf3):
            seq_ws = execute_sequence("рефактор со scope", sig, root, pkgs, prop_ws, feature="seqws",
                                      base=cur, author=True, author_proposer=author,
                                      review=True, reviewer_proposer=reviewer,
                                      write_scope_for=lambda pkg: pkg.get("write_scope"))
        expect("v2.124.1: authored-артефакты движка (.ai/openspec) НЕ ловятся как scope-violation",
               not any("scope-violation" in (p.get("stop_reason") or "") for p in seq_ws["packages"]))
        # с author+review+openspec — пакеты доходят до ready (если openspec доступен)
        import shutil
        if shutil.which("openspec"):
            expect("executor: с author+review+openspec — вся последовательность ready", seq["ready_all"] is True)

        # v2.120 (P0.3): _hard_stop различает настоящий блокер и «awaiting evidence»
        expect("v2.120 _hard_stop: нет коммита -> stop",
               _hard_stop({"commit": {"sha": None}}) == "no-commit")
        expect("v2.120 _hard_stop: регрессия -> stop",
               _hard_stop({"commit": {"sha": "a"}, "baseline": {"regressions": ["test"]}}) == "regression")
        expect("v2.120 _hard_stop: security fail -> stop",
               _hard_stop({"commit": {"sha": "a"}, "security_scan": {"overall": "fail"}}) == "security-fail")
        expect("v2.120 _hard_stop: reviewer fail -> stop",
               _hard_stop({"commit": {"sha": "a"}, "reviews": [{"gate": "code_review", "status": "fail"}]}) == "reviewer-blocked")
        # v3.0-rc13 (P0): reviewer WARN на блокирующем гейте (closed_as=blocked) ТОЖЕ останавливает —
        # это и был живой rc11-результат (warn -> gate fail -> ready_for_pr=false), раньше проскакивал.
        expect("v3.0-rc13 _hard_stop: reviewer WARN-blocking (closed_as=blocked) -> stop",
               _hard_stop({"commit": {"sha": "a"},
                           "reviews": [{"gate": "code_review", "status": "warn", "closed_as": "blocked"}]}) == "reviewer-blocked")
        expect("v3.0-rc13 _hard_stop: итоговый code_review-гейт fail с вынесенным вердиктом -> stop",
               _hard_stop({"commit": {"sha": "a"}, "reviews": [],
                           "gates": {"gate_results": [{"gate": "code_review", "status": "fail",
                                     "evidence": ["independent reviewer verdict @ abc"]}]}}) == "reviewer-blocked")
        expect("v3.0-rc13 _hard_stop: reviewer WARN на НЕблокирующем (closed_as!=blocked) -> НЕ стоп",
               _hard_stop({"commit": {"sha": "a"},
                           "reviews": [{"gate": "code_review", "status": "warn", "closed_as": "warn"}]}) is None)
        expect("v2.120 _hard_stop: scope-violation (write вне scope) -> stop",
               _hard_stop({"commit": {"sha": "a"}, "loop": {"denied_reasons": ["'x' вне write_scope ['src']"]}}) == "scope-violation")
        expect("v2.120 _hard_stop: awaiting evidence (гейты unmet, но коммит есть, без fail) -> НЕ стоп",
               _hard_stop({"commit": {"sha": "a"}, "gates": {"blocked": True, "unmet": ["requirements"]}}) is None)
        expect("v2.120 _hard_stop: заблокированный push (не scope) -> НЕ scope-violation",
               _hard_stop({"commit": {"sha": "a"}, "loop": {"denied_reasons": ["git push запрещён политикой"]}}) is None)
        # v3.0-rc2 (P0.2): security-стоп по РЕАЛЬНОМУ вердикту (pack blocked / security-гейт fail),
        # а не по недостижимому overall=="fail". Иначе security-блок проходил как awaiting evidence.
        expect("v3.0-rc2 _hard_stop: security_scan blocked -> стоп",
               _hard_stop({"commit": {"sha": "a"}, "security_scan": {"overall": "blocked"}}) == "security-fail")
        def _g(blk):
            return {"commit": {"sha": "a"}, "gates": {"gate_results": [
                {"gate": "security", "status": "fail", "blockers": [blk]}]}}
        expect("v3.0-rc4 _hard_stop: security-гейт fail — нет ApprovalRecord -> стоп",
               _hard_stop(_g("dependencies: нет валидного ApprovalRecord")) == "security-gate-fail")
        expect("v3.0-rc4 _hard_stop: security-гейт fail — сбой сканера (fail-closed) -> стоп",
               _hard_stop(_g("security scan упал (fail-closed): boom")) == "security-gate-fail")
        expect("v3.0-rc4 _hard_stop: security-гейт fail — reviewer не вынес pass -> стоп",
               _hard_stop(_g("security-reviewer не вынес pass")) == "security-gate-fail")
        expect("v3.0-rc4 _hard_stop: needs_review без поданного ревьюера (awaiting) -> НЕ стоп",
               _hard_stop({"commit": {"sha": "a"}, "security_scan": {"overall": "needs_review"},
                           "gates": {"gate_results": [{"gate": "security", "status": "fail",
                           "blockers": ["нужен независимый security-reviewer/человек по доменам: input_validation"]}]}}) is None)

    # v2.124 (P0.4): open_pr запрошен, но последовательность НЕ ready_all -> draft PR НЕ открывается
    # (доставка ПОСЛЕ агрегатного вердикта, не по готовности отдельного пакета).
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cur = mkrepo(td)
        sig = {"task_type": "ENGINEERING", "size": "large", "risk": "low",
               "affected_areas": ["catalog", "orders"]}
        pkgs = atomic_planner.decompose(sig, wid="seqpr", child_root=root)["work_packages"]
        def prop_pr(pkg):
            it = iter([{"op": "write", "path": f"src/{pkg['id']}.py", "content": "x=1\n"}, {"done": True}])
            return lambda c: next(it)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            # без author/review -> пакеты исполнены, но НЕ ready (артефакт-гейты unmet) -> ready_all=False
            seqpr = execute_sequence("рефактор", sig, root, pkgs, prop_pr, feature="seqpr",
                                     base=cur, open_pr=True)
        _dpr = seqpr.get("delivery") or {}
        expect("v2.124 (P0.4): не ready_all + open_pr -> PR НЕ открыт (delivery=not-attempted, draft_pr=None)",
               seqpr["ready_all"] is False and _dpr.get("status") == "not-attempted"
               and seqpr.get("draft_pr") is None and _dpr.get("requested") is True)

    # v2.120 (P0.3): reviewer FAIL на пакете 1 останавливает цепочку (пакет 2 не стартует)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cur = mkrepo(td)
        sig = {"task_type": "ENGINEERING", "size": "large", "risk": "low",
               "affected_areas": ["catalog", "orders", "billing"]}
        pkgs = atomic_planner.decompose(sig, wid="seqr", child_root=root)["work_packages"]
        def prop_for(pkg):
            it = iter([{"op": "write", "path": f"src/{pkg['id']}.py", "content": "x=1\n"}, {"done": True}])
            return lambda c: next(it)
        fail_reviewer = lambda p: '{"kind":"reviewer-result","status":"fail","checks":[{"id":"c","status":"fail"}],"blockers":["плохо"]}'
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            seqr = execute_sequence("рефактор с fail-ревью", sig, root, pkgs, prop_for, feature="seqr",
                                    base=cur, author=True, author_proposer=author,
                                    review=True, reviewer_proposer=fail_reviewer)
        ids_seen = [p["id"] for p in seqr["packages"]]
        expect("v2.120/rc13 executor: reviewer FAIL на пакете 1 -> цепочка остановлена (reviewer-blocked)",
               seqr["stopped_at"] == pkgs[0]["id"] and seqr["executed_all"] is False
               and seqr["packages"][0]["stop_reason"] == "reviewer-blocked" and pkgs[2]["id"] not in ids_seen)

    # v3.0-rc13 (P0): reviewer WARN-на-блокирующем (closed_as=blocked) тоже стоп — живой rc11-случай
    with tempfile.TemporaryDirectory() as td:
        root = Path(td); cur = mkrepo(td)
        sig = {"task_type": "ENGINEERING", "size": "large", "risk": "low",
               "affected_areas": ["catalog", "orders", "billing"]}
        pkgs = atomic_planner.decompose(sig, wid="seqw", child_root=root)["work_packages"]
        def prop_for(pkg):
            it = iter([{"op": "write", "path": f"src/{pkg['id']}.py", "content": "x=1\n"}, {"done": True}])
            return lambda c: next(it)
        warn_reviewer = lambda p: ('{"kind":"reviewer-result","status":"warn",'
                                   '"checks":[{"id":"c","status":"warn"}],"blockers":["сомнение по API"]}')
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            seqw = execute_sequence("рефактор с warn-ревью", sig, root, pkgs, prop_for, feature="seqw",
                                    base=cur, author=True, author_proposer=author,
                                    review=True, reviewer_proposer=warn_reviewer)
        ids_w = [p["id"] for p in seqw["packages"]]
        expect("v3.0-rc13 executor: reviewer WARN-blocking на пакете 1 -> цепочка стоп, пакет 3 не стартует",
               seqw["stopped_at"] == pkgs[0]["id"] and seqw["executed_all"] is False
               and seqw["packages"][0]["stop_reason"] == "reviewer-blocked" and pkgs[2]["id"] not in ids_w)

    # v3.0-rc13 (P1): доверенный retry_package — архив попытки + reset на checkpoint предшественника
    with tempfile.TemporaryDirectory() as td:
        root = Path(td); cur = mkrepo(td)
        sig = {"task_type": "ENGINEERING", "size": "large", "risk": "low",
               "affected_areas": ["catalog", "orders", "billing"]}
        pkgs = atomic_planner.decompose(sig, wid="seqrt", child_root=root)["work_packages"]
        def prop_for(pkg):
            it = iter([{"op": "write", "path": f"src/{pkg['id']}.py", "content": "x=1\n"}, {"done": True}])
            return lambda c: next(it)
        pass_reviewer = _pass_reviewer
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            seqrt = execute_sequence("рефактор для retry", sig, root, pkgs, prop_for, feature="seqrt",
                                     base=cur, author=True, author_proposer=author,
                                     review=True, reviewer_proposer=pass_reviewer)
        p1_sha = seqrt["packages"][0].get("sha")
        wt = root / ".ai" / "worktrees" / "seqrt"
        expect("v3.0-rc13 retry: предусловие — пакеты исполнены, снимок sequence-plan с базой",
               p1_sha and (root / "features" / "seqrt" / "sequence-plan.yaml").is_file())
        rt = retry_package(root, "seqrt", pkgs[1]["id"])
        head_after = _git(wt if wt.is_dir() else root, "rev-parse", "HEAD")[1]
        expect("v3.0-rc13 retry: reset ветки на checkpoint предшественника (пакет 1 SHA), без ручного git",
               rt.get("ok") is True and rt.get("checkpoint") == p1_sha and head_after == p1_sha
               and rt.get("predecessor") == pkgs[0]["id"])
        expect("v3.0-rc13 retry: проваленная попытка пакета 2 заархивирована (история не потеряна)",
               (root / "features" / "seqrt" / "work-packages" / pkgs[1]["id"] / "attempts" / "attempt-1" / "report.json").is_file())
        expect("v3.0-rc13 retry: неизвестный пакет -> честная ошибка (не тихий reset)",
               retry_package(root, "seqrt", "НЕТ-ТАКОГО").get("ok") is False)
        # v3.0.2 (finding аудита P0): resume/retry с ДРУГОЙ base (цепочка зафиксирована на base_ref) ->
        # base-contract-drift, не молчаливая смена контракта доставки. SequencePlan seqrt.base_ref==cur.
        seq_bd = execute_sequence("другая база", sig, root, pkgs, prop_for, feature="seqrt",
                                  base="release-xyz", author=True, author_proposer=author,
                                  review=True, reviewer_proposer=pass_reviewer,
                                  resume_from=pkgs[1]["id"])
        expect("v3.0.2 base-contract-drift: resume с другой base -> честная ошибка (нужен replan)",
               "error" in seq_bd and "base-contract-drift" in (seq_bd.get("error") or ""))

    # v3.0-rc16 (finding аудита P0): retry БЕЗ выделенного worktree -> fail-closed; основной checkout
    # (HEAD + рабочее дерево) НЕ ТРОГАЕТСЯ. Раньше vroot фолбэчил на child_root и reset --hard мог
    # сбросить основную ветку.
    import shutil as _sh_test
    with tempfile.TemporaryDirectory() as td:
        root = Path(td); cur = mkrepo(td)
        sig = {"task_type": "ENGINEERING", "size": "large", "risk": "low",
               "affected_areas": ["catalog", "orders", "billing"]}
        pkgs = atomic_planner.decompose(sig, wid="seqsafe", child_root=root)["work_packages"]
        def prop_for(pkg):
            it = iter([{"op": "write", "path": f"src/{pkg['id']}.py", "content": "x=1\n"}, {"done": True}])
            return lambda c: next(it)
        pass_reviewer = _pass_reviewer
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            execute_sequence("рефактор safe-retry", sig, root, pkgs, prop_for, feature="seqsafe",
                             base=cur, author=True, author_proposer=author,
                             review=True, reviewer_proposer=pass_reviewer)
        # снимок основного checkout ДО retry
        main_head_before = _git(root, "rev-parse", "HEAD")[1]
        main_status_before = _git(root, "status", "--porcelain")[1]
        # УДАЛЯЕМ выделенный worktree (симулируем повреждение/отсутствие)
        wt = root / ".ai" / "worktrees" / "seqsafe"
        _git(root, "worktree", "remove", "--force", str(wt))
        _sh_test.rmtree(wt, ignore_errors=True)
        rt_unsafe = retry_package(root, "seqsafe", pkgs[1]["id"])
        main_head_after = _git(root, "rev-parse", "HEAD")[1]
        main_status_after = _git(root, "status", "--porcelain")[1]
        expect("v3.0-rc16 retry-safety: нет worktree -> fail-closed (ok=False)",
               rt_unsafe.get("ok") is False and "fail-closed" in (rt_unsafe.get("error") or ""))
        expect("v3.0-rc16 retry-safety: основной checkout НЕ тронут (HEAD + рабочее дерево неизменны)",
               main_head_after == main_head_before and main_status_after == main_status_before)

    # v3.0-rc20 (finding аудита P0): aggregate code_review — ТОЛЬКО явный валидный pass; no-verdict/invalid
    # -> ok=False (раньше fail-OPEN). И _collect_base_checks_at: несуществующая база -> None (не доказан).
    with tempfile.TemporaryDirectory() as td:
        root = Path(td); cur = mkrepo(td)
        # no-verdict reviewer (всегда невалидный текст) -> code_review не закрыт -> ok=False
        nover = lambda p: "я не буду выносить структурный вердикт, просто текст"
        ok_nv, _ = _aggregate_code_review(root, cur, cur, {"task_type": "ENGINEERING"}, nover, True)
        expect("v3.0-rc20 aggregate-review: no-verdict/invalid -> ok=False (не fail-open)", ok_nv is False)
        # без ревью (не запрошено) -> ok=True (per-package ревью уже было)
        ok_nr, _ = _aggregate_code_review(root, cur, cur, {}, None, False)
        expect("v3.0-rc20 aggregate-review: без ревью -> ok=True (не блокируем на этом уровне)", ok_nr is True)
        # baseline provenance: несуществующий base_sha -> None (baseline НЕ доказан -> нет fallback)
        expect("v3.0-rc20 baseline-provenance: несуществующая база -> None (не доказан)",
               _collect_base_checks_at(root, "0" * 40, False) is None)
        _res = _collect_base_checks_at(root, _git(root, "rev-parse", "HEAD")[1], False)
        expect("v3.0-rc20 baseline-provenance: валидная база -> proven=True + HEAD==base",
               isinstance(_res, dict) and _res.get("proven") is True)

    # v3.8.3-rc2 (#4/#4b): enforcement #5 на АГРЕГАТЕ — общий reviewer НЕ закрывает; qualified-судья ИЛИ
    # человеко-approval на ИНТЕГРАЦИОННОМ SHA закрывают. Прямой unit на _aggregate_close_security.
    import approvals as _appr_t
    _isha = "a" * 40
    _agg_nr = {"overall": "needs_review", "needs_review": ["rate_limiting"], "results": []}
    _gen_reviewer = lambda *a, **k: "VERDICT: pass"   # общий code-reviewer (не должен закрывать security)
    # (i) общий reviewer_proposer + НЕТ qualified-судьи, НЕТ человека -> остаётся needs_review
    _r_i, _ = _aggregate_close_security(dict(_agg_nr), Path("."), None, _isha, {}, _gen_reviewer, True,
                                        security_reviewer_proposer=None, strict_judge_qualified=False,
                                        wid=None, child_root=None)
    expect("rc2 #4: общий code-reviewer НЕ закрывает aggregate security (остаётся needs_review)",
           _r_i.get("overall") == "needs_review" and _r_i.get("closed_by") is None)
    with tempfile.TemporaryDirectory() as _hd:
        _appr_t.write_record(_hd, "seq-agg", approval="rate_limiting", approved_by="human@owner",
                             scope="security rate_limiting", reason="человек одобрил integration-SHA",
                             created_at="2026-07-29", binds_to=_isha, expires_at="2026-12-31",
                             risk="high", source="human")
        # (ii) человеко-approval, привязанный К ИНТЕГРАЦИОННОМУ SHA -> закрывает
        _r_ii, _ = _aggregate_close_security(dict(_agg_nr), Path(_hd), None, _isha, {}, _gen_reviewer, True,
                                             security_reviewer_proposer=None, strict_judge_qualified=False,
                                             wid="seq-agg", child_root=_hd)
        expect("rc2 #4b: человеко-approval на integration-SHA закрывает aggregate security",
               _r_ii.get("overall") == "clear" and _r_ii.get("closed_by") == "human-approval-integration-sha")
        # (iii) тот же approval, но проверяем на ДРУГОМ integration-SHA -> binds_to mismatch -> НЕ закрывает
        _r_iii, _ = _aggregate_close_security(dict(_agg_nr), Path(_hd), None, "b" * 40, {}, _gen_reviewer, True,
                                              security_reviewer_proposer=None, strict_judge_qualified=False,
                                              wid="seq-agg", child_root=_hd)
        expect("rc2 #4b: approval на ДРУГОМ SHA -> aggregate security НЕ закрыт (SHA-binding)",
               _r_iii.get("overall") == "needs_review")

    # v2.120 (P0.2): sandbox наследуется в per-package прогон (containment не теряется)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cur = mkrepo(td)
        sig = {"task_type": "QUICK", "size": "large", "risk": "low",
               "affected_areas": ["catalog", "orders", "billing"]}
        pkgs = atomic_planner.decompose(sig, wid="seqs", child_root=root)["work_packages"]
        def prop_for(pkg):
            it = iter([{"op": "write", "path": f"src/{pkg['id']}.py", "content": "x=1\n"}, {"done": True}])
            return lambda c: next(it)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            execute_sequence("рефактор в sandbox", sig, root, pkgs, prop_for, feature="seqs",
                             base=cur, sandbox=True, baseline_diff=False)
        rep0 = json.loads((root / "features" / "seqs" / "work-packages" / pkgs[0]["id"] / "report.json").read_text())
        expect("v2.120 executor: sandbox=True наследуется -> containment.sandbox=True в прогоне пакета",
               (rep0.get("containment") or {}).get("sandbox") is True
               and (rep0.get("containment") or {}).get("shell_mode") == "allowlist")

    # блок пакета останавливает последовательность (пакет 2 с secret_boundary без approval -> preflight блок)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cur = mkrepo(td)
        sig = {"task_type": "ENGINEERING", "size": "large", "risk": "low",
               "affected_areas": ["catalog", "orders", "billing"]}
        wp = atomic_planner.decompose(sig, wid="seqb", child_root=root)
        pkgs = wp["work_packages"]

        def prop_for(pkg):
            it = iter([{"op": "write", "path": f"src/{pkg['id']}.py", "content": "x=1\n"}, {"done": True}])
            return lambda c: next(it)

        def sig_for(pkg):
            return {"secret_boundary": True} if pkg["id"] == pkgs[1]["id"] else {}

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            # v2.121: author=True снимает spec-first блок heavy для пакетов 1/3 -> изолируем ИМЕННО
            # блок пакета 2 по secret_boundary (approvals-гейт независим от author).
            seq2 = execute_sequence("рефактор с блоком", sig, root, pkgs, prop_for, feature="seqb",
                                    base=cur, signals_for=sig_for, author=True, author_proposer=author)
        # пакет 1 исполнен, пакет 2 заблокирован preflight (secret_boundary без ApprovalRecord),
        # пакет 3 НЕ стартовал
        ids_seen = [p["id"] for p in seq2["packages"]]
        expect("executor: блок пакета 2 останавливает последовательность (пакет 3 НЕ стартовал)",
               seq2["stopped_at"] == pkgs[1]["id"] and pkgs[0]["id"] in seq2["completed"]
               and pkgs[2]["id"] not in ids_seen and seq2["executed_all"] is False)

    print("workpackage_executor selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
