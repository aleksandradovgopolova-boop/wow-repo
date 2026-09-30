#!/usr/bin/env python3
"""parallel_live_dispatch.py — проводка НАСТОЯЩЕГО конкурентного мультипакетного прогона в живой
run-путь через ЯВНЫЙ opt-in `ai-ops run --parallel`.

ЗАЧЕМ. `engine/parallel_live.run_live_concurrent` (disposable-клон+ветка+прогон на пакет,
governed fan-in на СВЕЖЕМ integration-SHA) был построен и протестирован, но не имел ни одного
рабочего вызывателя — жил дормантным инвентарём («построено, но не проведено в контур»). Этот
модуль — тонкий диспетч: он декомпозирует задачу (`atomic_planner`) и, если план мультипакетный,
проводит его через `run_live_concurrent`. Так у concurrent-пути появляется настоящий вызыватель.

ГРАНИЦЫ (осознанно узкие, чтобы не дестабилизировать дефолт):
  - Дефолтное поведение `run` НЕ меняется: диспетч зовётся ТОЛЬКО при явном `--parallel`.
  - Основной checkout НЕ трогается: `run_live_concurrent` работает на одноразовых клонах.
  - Задача атомарна (или один пакет) -> диспетч возвращает None: вызыватель делает обычный прогон.
  - Диспетч НЕ владеет доставкой: `run_live_concurrent` возвращает DeliveryPlan для канонического
    controller'а; прямого push/gh здесь нет.

Только stdlib + существующие модули кита.
"""
from __future__ import annotations

from pathlib import Path

from ai_ops_kit.shared import gitio                       # noqa: E402
from ai_ops_kit.engine import atomic_planner              # noqa: E402
from ai_ops_kit.engine import parallel_live               # noqa: E402


def _base_sha(child_root) -> str | None:
    """Текущий HEAD-SHA дочернего репозитория — база для клонов пакетов. None, если истории нет."""
    rc, out, _ = gitio.git(child_root, "rev-parse", "HEAD")
    return out if (rc == 0 and out) else None


def _package_task(task: str, pkg: dict) -> str:
    """Инструкция пакета: общий текст задачи + подзадача пакета (title). Пакет исполняется своим
    прогоном в своём клоне, поэтому получает СВОЙ, а не одинаковый со всеми, текст."""
    title = pkg.get("title") or pkg["id"]
    return f"{task}\n\nПодзадача пакета {pkg['id']}: {title}".strip()


def build_work_graph(signals, wid, child_root):
    """decompose -> (WorkGraph, task_map). (None, None), если задача атомарна или пакет один.

    Чистая (кроме чтения плана через atomic_planner): удобно проверять маршрутизацию на данных.
    """
    wp = atomic_planner.decompose(signals, wid=wid, child_root=Path(child_root))
    pkgs = wp.get("work_packages") or []
    if not (wp.get("should_decompose") and len(pkgs) >= 2):
        return None, None
    wg = {"schema_version": 1, "kind": "WorkGraph", "id": wid, "feature": wid,
          "execution_mode": "hybrid", "packages": pkgs}
    task_text = signals.get("task_text") or ""
    task_map = {p["id"]: _package_task(task_text, p) for p in pkgs}
    return wg, task_map


def run_parallel_live(task, signals, child_root, *, feature=None, provider=None, model=None,
                      base=None, open_pr=False, max_steps=40, repo_slug=None,
                      contract_shas=None, run_fn=None, runner=None):
    """ЯВНЫЙ opt-in `--parallel`: декомпозировать задачу и провести мультипакетный план через
    `parallel_live.run_live_concurrent` (настоящая конкурентность, disposable-клон на пакет).

    -> rec (dict), обогащённый `rec["parallel"]=True`, при мультипакетном плане;
       None, если задача атомарна — тогда вызыватель делает обычный (single-path) прогон.

    `run_fn`/`runner` — точки инъекции для тестов (по умолчанию `ai_ops_run.run` и
    `run_live_concurrent`), чтобы проверять маршрутизацию без запуска реальных клонов/провайдера.
    """
    child_root = Path(child_root)
    signals = dict(signals or {})
    signals.setdefault("task_text", task)
    wid = feature or signals.get("feature") or "WG"
    wg, task_map = build_work_graph(signals, wid, child_root)
    if wg is None:
        return None  # атомарна/один пакет -> обычный прогон (дефолт не меняется)
    base_sha = _base_sha(child_root)
    if not base_sha:
        return {"proceed": False, "stage": "preflight", "parallel": True,
                "reason": "нет базового коммита (git rev-parse HEAD): --parallel требует "
                          "git-репозиторий с историей"}
    if run_fn is None:
        from ai_ops_kit.engine import ai_ops_run
        run_fn = ai_ops_run.run
    dispatch = runner or parallel_live.run_live_concurrent
    rec = dispatch(wg, str(child_root), base_sha, task_map, signals, run_fn,
                   repo_slug=repo_slug, contract_shas=contract_shas)
    if isinstance(rec, dict):
        rec["parallel"] = True
    return rec


def _blocked(rec) -> bool:
    return rec.get("stage") in ("plan", "preflight", "contract-first", "isolation")


def print_parallel(rec) -> None:
    """Человекочитаемая сводка результата --parallel."""
    if _blocked(rec) and not rec.get("proceed"):
        print(f"PARALLEL ОТКАЗ ({rec.get('stage')}): {rec.get('reason') or rec.get('errors') or '—'}")
        return
    agg = rec.get("aggregate") or {}
    dp = rec.get("delivery_plan") or {}
    print(f"PARALLEL {rec.get('id') or ''}: proceed={rec.get('proceed')} · "
          f"concurrency={rec.get('execution_concurrency')} · isolation={rec.get('isolation')} · "
          f"integration_sha={(rec.get('integration_sha') or '')[:12] or '—'} · "
          f"conflicts={agg.get('conflicts')} · доставка_готова={dp.get('ready')}")
    for pid, res in (rec.get("package_results") or {}).items():
        sha = (res.get("sha") or "")[:12] or "—"
        print(f"  [{pid}] {res.get('status')} · sha={sha}")


def exit_code(rec) -> int:
    """0 — зелёный aggregate и доставка открыта; 2 — блок/ошибка; 1 — исполнено, но не готово."""
    if rec.get("proceed") and (rec.get("delivery") or {}).get("open_pr"):
        return 0
    if _blocked(rec) and not rec.get("proceed"):
        return 2
    return 0 if rec.get("proceed") else 1
