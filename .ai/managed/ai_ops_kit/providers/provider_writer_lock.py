#!/usr/bin/env python3
"""Machine-wide writer serialization lock — фундамент для orchestrator_providers.

Вынесено из orchestrator_providers.py (чистый разрез монолита): машинный замок локального писателя
(`claude -p`) и его проба. Ни от каких других провайдер-модулей НЕ зависит — на него опирается
`provider_calls` (граница `claude -p` сериализует реальный subprocess-вызов) и фасад. Поведение
байт-в-байт прежнее.
"""
from __future__ import annotations

import contextlib
import os
import tempfile


def writer_lock_path():
    """Путь машинного замка писателя. Переопределяется `AI_OPS_WRITER_LOCK_PATH`; по умолчанию —
    файл в системном tmp, общий для ВСЕХ worktree и процессов на машине (worktree — отдельные
    checkout'ы одного репо, поэтому замок в дереве репозитория их бы не сериализовал)."""
    return os.environ.get("AI_OPS_WRITER_LOCK_PATH") or os.path.join(
        tempfile.gettempdir(), "ai-ops-writer.lock")


def _writer_lock_poll_seconds():
    """Интервал (сек) между попытками взять занятый замок и между уведомлениями об ожидании.
    Дефолт 15 c; переопределяется `AI_OPS_WRITER_LOCK_POLL_SECONDS` (как `writer_lock_path()` —
    средой). Нечисло/≤0 игнорируем и берём дефолт: интервал управляет темпом уведомлений и сном,
    а нулевой сон превратил бы ожидание в busy-loop."""
    default = 15.0
    raw = os.environ.get("AI_OPS_WRITER_LOCK_POLL_SECONDS")
    if raw is None:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


def writer_lock_busy():
    """Неблокирующая ПРОБА машинного замка писателя (#661): держит ли писателя ПРЯМО СЕЙЧАС другой
    процесс на этой машине — без ожидания и без вставания в очередь. Пытается взять замок
    `LOCK_NB` и тут же отпускает: удалось → свободен (`False`); `OSError` → держит другой процесс
    (`True`). Проба сама замок НЕ удерживает (иначе стала бы вторым держателем и повлияла бы на
    очередь), поэтому не деадлочит и не мешает.

    Замок выключен (`AI_OPS_WRITER_LOCK=0`) или нет `fcntl` (Windows) → сериализации нет, значит и
    «занятости», о которой стоит сигналить, тоже нет → `False` (симметрично `_writer_serialization_lock`,
    который в этих случаях — no-op). Сбой открытия файла замка не выдаём за занятость: это не
    «другой держатель», а недоступность самого замка → `False`, проба не должна врать про очередь."""
    if os.environ.get("AI_OPS_WRITER_LOCK", "1") == "0":
        return False
    try:
        import fcntl
    except ImportError:
        return False
    try:
        f = open(writer_lock_path(), "w")
    except OSError:
        return False
    try:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True                       # держит другой процесс — писатель занят
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)  # свободен: сразу отпускаем, очередь не трогаем
        return False
    finally:
        f.close()


@contextlib.contextmanager
def _writer_serialization_lock(notify=None, poll_seconds=None):
    """МАШИННЫЙ ЗАМОК ПИСАТЕЛЯ (поле 02–03.09.2026): один локальный `claude -p` за раз на всю
    машину. Причина: локальный писатель — ЕДИНСТВЕННЫЙ разделяемый ресурс, и параллельные
    `run --execute` из разных worktree конкурируют за него без координации — прогоны виснут на
    старте (дочерний писатель не появляется, 0% CPU), и заезд приходится сериализовать руками.
    Замок делает сериализацию встроенной: ждущие встают в ОЧЕРЕДЬ, а не падают и не деадлочат.
    Освобождается при выходе И при смерти держателя (flock снимается ядром на закрытии fd) —
    зависший прогон не запирает остальных навсегда. Держится только на время самого
    subprocess-вызова, НЕ во время backoff и НЕ во время ожидания очереди — паузы очередь не блокируют.

    ВИДИМАЯ ОЧЕРЕДЬ (#652): пока замок занят, ждём НЕ молча. Прежний код сообщал об очереди один
    раз и уходил в блокирующий `flock(LOCK_EX)` — в поле прогон так простоял ~час, и человек не мог
    отличить ожидание от зависания. Теперь ожидание — цикл неблокирующих попыток со сном между ними;
    на каждом тике `notify(...)` сообщает, что писатель занят другим прогоном на этой машине, и
    сколько уже ждём. Первое уведомление — сразу при обнаружении занятости; `yield` (старт писателя)
    происходит ТОЛЬКО после того, как замок получен, чтобы ожидание не выглядело как фаза написания.

    Escape-hatch: `AI_OPS_WRITER_LOCK=0` — no-op (напр. один прогон, свой внешний планировщик).
    Без `fcntl` (Windows) деградирует до no-op — не хуже прежнего поведения (как lifecycle_store).
    В обоих случаях уведомлений об ожидании нет — ждать нечего."""
    if os.environ.get("AI_OPS_WRITER_LOCK", "1") == "0":
        yield
        return
    try:
        import fcntl
    except ImportError:
        yield
        return
    import time
    interval = poll_seconds if poll_seconds is not None else _writer_lock_poll_seconds()
    f = open(writer_lock_path(), "w")
    try:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)   # свободен — берём сразу
        except OSError:
            # Занят: ждём в видимой очереди — периодические уведомления + сон, пока не возьмём.
            # Счётчик — по РЕАЛЬНОМУ времени (monotonic), а не по сумме интервалов: сон неточен,
            # а человеку важно фактическое ожидание.
            t0 = time.monotonic()
            while True:
                if notify:
                    notify("ai-ops: жду очереди — писатель занят другим прогоном на этой машине, "
                           "~%.1f c…" % (time.monotonic() - t0))
                time.sleep(interval)            # обязательный сон: не busy-loop, не жжём CPU
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break                        # замок получен — выходим из очереди
                except OSError:
                    continue                     # всё ещё занят — следующий тик уведомит снова
        yield                                    # старт писателя — ТОЛЬКО с замком в руках
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()
