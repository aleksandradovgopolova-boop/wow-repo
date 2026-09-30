#!/usr/bin/env python3
"""writer_contention.py (#661) — сигнал «писатель занят + работа независима → годится параллельный
субагентный путь».

ПОВОД. Внутренний писатель движка (`run --execute` → `claude -p`) СЕРИАЛИЗОВАН машинным замком:
один писатель на машину. Когда замок держит другой прогон, новый встаёт в ОЧЕРЕДЬ. Очередь теперь
видима (#652 — прогон честно сообщает, что ждёт), но кит всё равно молча выбирает худший из
доступных путей — ожидание — не называя альтернативы. А альтернатива есть: в среде «Клод в
приложении, без ключа» независимые писатели-СУБАГЕНТЫ — это отдельные инстансы Клода, общего
`claude -p` у них нет, значит нет и замка, и они идут ПАРАЛЛЕЛЬНО (skill `ai-ops`, раздел
«Параллельные писатели через субагентов»). Пока этот путь выбирает ЧЕЛОВЕК вручную; кит его сам не
предлагает.

ГРАНИЦА движок↔координатор (инвариант: движок субагентов НЕ спавнит). Движок умеет РАСПОЗНАТЬ
ситуацию из двух фактов, которые у него уже есть:
  * «писатель занят» — неблокирующая проба замка (`providers.orchestrator_providers.writer_lock_busy`);
  * «работа независима» — пустой список пересечений concurrency-preflight (`active_work.classify`):
    у этой работы нет общих областей/контрактов/зависимостей с активными сессиями.
Соединение этих фактов — здесь. Движок ПИШЕТ машиночитаемый сигнал и (когда путь годится) кладёт
честный ВЫБОР в шину внимания (#633), чтобы он дошёл до inbox, а не потерялся в stderr. На сигнал
реагирует КООРДИНАТОР (приложение Клода) фан-аутом писателей-субагентов. Сам движок остаётся при
безопасном дефолте: если координатор путь не подхватил — прогон всё равно дождётся очереди
(поведение не ломается, прогон не застревает).

Артефакт живёт под `<child_root>/.ai/writer-contention/<fid>.signal.json` — тот же durable-дом вне
worktree-дерева, что у reviewer-requests: переживает пересборку worktree и виден координатору.
"""
from __future__ import annotations

import json
from pathlib import Path

_DIR_REL = (".ai", "writer-contention")


def _dir(root) -> Path:
    return Path(root).joinpath(*_DIR_REL)


def signal_path(root, fid) -> Path:
    """Путь машиночитаемого сигнала (кит его ПИШЕТ, координатор ЧИТАЕТ)."""
    return _dir(root) / f"{fid}.signal.json"


def assess(writer_busy, conflicts):
    """Чистое решение: годится ли параллельный субагентный путь ПРЯМО СЕЙЧАС. Ничего не печатает и
    не пишет — возвращает dict {parallel_viable, reason}.

    parallel_viable ⇔ писатель ЗАНЯТ другим прогоном И у этой работы НЕТ пересечений с активной
    (пустой `conflicts` из concurrency-preflight). Fail-closed, как parallel_planner: ЛЮБАЯ находка
    (area/contract/dependency/branch/same-work) закрывает параллель — независимость не доказана,
    значит не рекомендуем. Писатель свободен → очереди нет → рекомендовать нечего."""
    conflicts = list(conflicts or [])
    if not writer_busy:
        return {"parallel_viable": False, "reason": "писатель свободен — очереди нет"}
    if conflicts:
        kinds = sorted({str(c.get("kind")) for c in conflicts if isinstance(c, dict)})
        return {"parallel_viable": False,
                "reason": "работа пересекается с активной (%s) — параллельный путь небезопасен"
                          % (", ".join(kinds) or "неизвестно")}
    return {"parallel_viable": True,
            "reason": "писатель занят другим прогоном, а эта работа независима — "
                      "субагентный путь идёт параллельно, без очереди"}


def record(child_root, fid, decision, *, wait_seconds=None, areas=None, work_id=None):
    """Пишет машиночитаемый сигнал под `<child_root>/.ai/writer-contention/<fid>.signal.json` и —
    когда параллельный путь годится — кладёт повод-РЕШЕНИЕ в шину внимания (#633), чтобы честный
    выбор дошёл до inbox. Возвращает путь артефакта (или None, если писать некуда).

    Best-effort и durable: сигнал advisory (безопасный дефолт — очередь), поэтому его сбой не должен
    ронять прогон; но и молча исчезать он не должен — запись идёт в durable-дом. Идемпотентно по
    `fid`: повторная проба ОБНОВЛЯЕТ сигнал, а не плодит второй."""
    if child_root is None or not fid:
        return None
    p = signal_path(child_root, fid)
    viable = bool(decision.get("parallel_viable"))
    payload = {
        "schema_version": 1,
        "kind": "writer-contention-signal",
        "workitem_id": fid,
        "writer_busy": True,
        "parallel_viable": viable,
        "reason": decision.get("reason", ""),
        # рекомендованный путь называется машиночитаемо: координатор реагирует на `parallel-subagent`
        # фан-аутом; `queue-wait` — тот же безопасный дефолт, что и без сигнала.
        "recommended_path": "parallel-subagent" if viable else "queue-wait",
        "wait_estimate_seconds": wait_seconds,
        "areas": list(areas or []),
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if viable:
        # Повод — DECISION (какой путь), НЕ BLOCKED: работа не остановлена, продолжит в очереди,
        # если координатор не подхватит. Так честный выбор доходит до inbox через ту же шину, что
        # и остальные вызовы человека (#633), а не только в stderr видимой очереди.
        from ai_ops_kit.lifecycle import attention_bus as _ab
        _ab.record(child_root, key=f"writer-contention:{fid}",
                   source="прогон: писатель занят",
                   reason=("писатель занят другим прогоном на этой машине, а эта работа независима — "
                           "можно вести параллельно писателем-субагентом вместо ожидания в очереди"),
                   kind=_ab.DECISION, work_id=work_id or fid)
    return p
