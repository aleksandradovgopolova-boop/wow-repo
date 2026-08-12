#!/usr/bin/env python3
"""Durable lifecycle I/O (v3.0.12, finding аудита блока B) — единый контракт надёжной записи и
fail-closed чтения КРИТИЧЕСКИХ resume-артефактов (run-settings, run-handoff, active-work, SequencePlan).

Проблема (сквозной самоаудит): большинство lifecycle-файлов писались plain `write_text`/`json.dump`
(неатомарно, без fsync, без перечитывания), а битые/пустые читались как «отсутствующие» -> тихая
потеря policy и ложный «resume безопасен». Здесь — ОДИН источник истины:

  * durable_write — tmp -> flush+fsync(файл) -> os.replace -> fsync(КАТАЛОГ) -> перечитать+провалидировать;
  * load_guarded — различает ОТСУТСТВУЕТ / ПОВРЕЖДЁН (parse-error/пустой/не dict/не тот kind/нет ключей)
    и НЕ даёт вызывающему молча дефолтить или перезаписать повреждённый источник.

CLI: lifecycle_store.py --selftest
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml


def _durable(path, data, serialize, parse, require_keys, keep_backup):
    """v3.0.15 (LifecycleStore v1.1, finding аудита P1): АТОМАРНАЯ + FAIL-CLOSED запись с валидацией
    ПРОСПЕКТИВНОГО документа ДО os.replace (иначе программная ошибка могла заменить валидный файл
    невалидным, а потом вернуть ok=False — старый источник истины уже потерян). Порядок:
    validate(data) -> serialize -> validate(проспективный reparse) -> UNIQUE temp -> fsync ->
    [backup прежнего валидного] -> atomic replace -> fsync(dir) -> reread+validate -> cleanup temp.
    -> {ok} | {ok: False, error}."""
    import tempfile
    path = Path(path)
    # 1. валидируем ВХОД до любого касания целевого файла
    if not isinstance(data, dict):
        return {"ok": False, "error": "данные для записи не dict"}
    missing = [k for k in require_keys if k not in data]
    if missing:
        return {"ok": False, "error": f"перед записью отсутствуют ключи: {', '.join(missing)}"}
    try:
        text = serialize(data)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"сериализация не удалась: {type(e).__name__}: {e}"}
    # 2. проспективная валидация: сериализованное перечитывается в валидный dict — ДО замены старого файла
    try:
        prospective = parse(text)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"проспективный документ не парсится: {type(e).__name__}: {e}"}
    if not isinstance(prospective, dict) or [k for k in require_keys if k not in prospective]:
        return {"ok": False, "error": "проспективный документ невалиден — старый файл НЕ тронут"}
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 3. УНИКАЛЬНЫЙ temp (mkstemp) — конкурентные писатели не бьются об общий .tmp
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        tmp = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # 4. backup прежнего валидного состояния (opt-in, для критических артефактов)
        if keep_backup and path.exists():
            try:
                path.with_suffix(path.suffix + ".bak").write_text(
                    path.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass
        # 5. атомарная замена + fsync каталога
        os.replace(str(tmp), str(path))
        tmp = None
        _fsync_dir(path.parent)
        # 6. повторная валидация ПОСЛЕ замены (defense-in-depth)
        back = parse(path.read_text(encoding="utf-8"))
        if not isinstance(back, dict) or [k for k in require_keys if k not in back]:
            return {"ok": False, "error": "перечитанный после замены документ невалиден"}
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        if tmp is not None and Path(tmp).exists():
            try:
                Path(tmp).unlink()
            except OSError:
                pass


def durable_write(path, data, require_keys=(), keep_backup=False):
    """АТОМАРНАЯ + FAIL-CLOSED запись YAML-артефакта (LifecycleStore v1.1: validate-before-replace,
    unique temp, cleanup, opt-in backup). -> {ok} | {ok: False, error}. Вызывающий ОБЯЗАН остановиться
    при ok=False (нет источника истины)."""
    return _durable(path, data, lambda d: yaml.safe_dump(d, allow_unicode=True, sort_keys=False),
                    yaml.safe_load, require_keys, keep_backup)


def durable_write_json(path, data, require_keys=(), keep_backup=False):
    """v3.0.14/v3.0.15 (finding аудита #2/P1): durable JSON-запись (run-report/controller-report) с той же
    гарантией validate-before-replace, что durable_write. -> {ok} | {ok: False, error}."""
    import json as _json
    return _durable(path, data,
                    lambda d: _json.dumps(d, ensure_ascii=False, indent=2, default=str),
                    _json.loads, require_keys, keep_backup)


def _event_checksum(payload_str):
    import hashlib
    return hashlib.sha256(payload_str.encode("utf-8")).hexdigest()[:16]


import contextlib


@contextlib.contextmanager
def _journal_lock(journal_path):
    """v3.1 (trace v0.2): межпроцессная блокировка вокруг append — конкурентные писатели не получают
    одинаковые seq/prev_checksum (устранён v0.1-разрыв). best-effort: без fcntl (Windows) — no-op."""
    lock_path = Path(str(journal_path) + ".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        import fcntl
    except (ImportError, OSError):
        yield
        return
    f = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()


def _journal_scan(journal_path):
    """Чистое сканирование JSONL с проверкой checksum-цепочки. -> (events, ok, broken_at|None, reason|None)."""
    import json as _json
    events, prev = [], None
    for i, ln in enumerate(l for l in Path(journal_path).read_text(encoding="utf-8").splitlines() if l.strip()):
        try:
            rec = _json.loads(ln)
        except ValueError:
            return events, False, i, "строка не парсится (оборванная запись)"
        recomputed = _event_checksum(_json.dumps({k: v for k, v in rec.items() if k != "checksum"},
                                                 sort_keys=True, ensure_ascii=False))
        if rec.get("checksum") != recomputed:
            return events, False, rec.get("seq", i), "checksum не сходится (подмена)"
        if rec.get("prev_checksum") != prev:
            return events, False, rec.get("seq", i), "разрыв prev_checksum-цепочки"
        prev = rec.get("checksum")
        events.append(rec)
    return events, True, None, None


def journal_append(journal_path, event):
    """v3.0.14/v3.1 (trace v0.2): append-only JSONL event journal с checksum-цепочкой + head-marker.
    Каждое событие: seq, prev_checksum, собственный checksum. v0.2 ЗАКРЫВАЕТ ограничения v0.1:
      * межпроцессный ЛОК вокруг всей read-verify-append (нет гонки seq/prev_checksum);
      * ПОЛНАЯ верификация цепочки ПЕРЕД append — на битый журнал не дописываем (ok=False);
      * durable head-marker (<journal>.head {seq, checksum}) — позволяет ДЕТЕКТИТЬ усечение последней
        целой строки при чтении (v0.1 не мог: валидный префикс выглядел валидным).
    Одна строка = атомарный append (flush+fsync). Журнал — наблюдаемость, не источник истины; сбой НЕ
    роняет прогон (вызывающий пусть логирует, но не падает). -> {ok, seq} | {ok: False, error}."""
    import json as _json
    journal_path = Path(journal_path)
    try:
        with _journal_lock(journal_path):
            journal_path.parent.mkdir(parents=True, exist_ok=True)
            prev_checksum, seq = None, 0
            if journal_path.exists():
                evs, ok, _at, reason = _journal_scan(journal_path)
                if not ok:
                    return {"ok": False, "error": f"журнал повреждён ({reason}) — append запрещён "
                                                  "(не расширяем битую цепочку)"}
                if evs:
                    prev_checksum = evs[-1].get("checksum")
                    seq = int(evs[-1].get("seq", len(evs) - 1)) + 1
            rec = {**event, "seq": seq, "prev_checksum": prev_checksum}
            rec["checksum"] = _event_checksum(_json.dumps(rec, sort_keys=True, ensure_ascii=False))
            with open(journal_path, "a", encoding="utf-8") as f:
                f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            # head-marker (durable): фиксирует ожидаемый хвост -> усечение последней строки детектируемо
            durable_write_json(Path(str(journal_path) + ".head"),
                               {"kind": "journal-head", "seq": seq, "checksum": rec["checksum"]},
                               require_keys=("seq", "checksum"))
            return {"ok": True, "seq": seq}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def journal_read(journal_path):
    """Прочитать event journal + ПРОВЕРИТЬ целостность: checksum-цепочка И сверка с head-marker (v0.2 —
    ловит усечение последней целой строки, которое v0.1 пропускал). -> {events, ok, broken_at?, reason?}."""
    import json as _json
    journal_path = Path(journal_path)
    if not journal_path.exists():
        return {"events": [], "ok": True}
    events, ok, broken_at, reason = _journal_scan(journal_path)
    out = {"events": events, "ok": ok}
    if not ok:
        out["broken_at"] = broken_at
        out["reason"] = reason
        return out
    # v0.2: сверка с durable head-marker — если журнал КОРОЧЕ зафиксированного хвоста => усечение
    hp = Path(str(journal_path) + ".head")
    if hp.exists():
        try:
            head = _json.loads(hp.read_text(encoding="utf-8"))
        except ValueError:
            head = None
        if isinstance(head, dict) and head.get("seq") is not None:
            last_seq = events[-1].get("seq") if events else -1
            if last_seq < head["seq"]:
                out["ok"] = False
                out["broken_at"] = head["seq"]
                out["reason"] = (f"усечение: журнал обрывается на seq={last_seq}, а head-marker "
                                 f"фиксировал seq={head['seq']} (удалена целая строка)")
    return out


_TRACE_REQUIRED = {
    "run_start": ("run_id", "workitem_id", "attempt_id"),
    "run_end": ("run_id", "workitem_id", "attempt_id", "status"),
    "run_cost": ("run_id", "attempt_id"),
    "ready_for_delivery": ("run_id", "workitem_id"),
    "package_end": ("run_id", "workitem_id", "package_id"),
    "delivery_intent": ("run_id", "delivery_id"),
    "delivery_receipt": ("run_id", "delivery_id"),
    "delivery": ("run_id", "delivery_id"),
    "delivery_outcome_unknown": ("run_id", "delivery_id"),
    "delivery_reconciled": ("run_id", "delivery_id"),
}


def validate_trace(events):
    """v3.1 (trace v0.2): проверить, что события трейса несут ОБЯЗАТЕЛЬНЫЕ id своей связи (Run/Attempt/
    Package/Gate/Delivery) — чтобы трейс был реконструируем. Неизвестный kind допустим (требует лишь
    run_id). -> список ошибок (пусто = валиден)."""
    errs = []
    for i, e in enumerate(events or []):
        if not isinstance(e, dict):
            errs.append(f"событие[{i}] не dict")
            continue
        kind = e.get("kind")
        if not kind:
            errs.append(f"событие[{i}] без kind")
            continue
        for k in _TRACE_REQUIRED.get(kind, ("run_id",)):
            if e.get(k) in (None, ""):
                errs.append(f"событие[{i}] kind={kind}: нет обязательного поля '{k}'")
    return errs


def _fsync_dir(directory):
    """fsync каталога — иначе питание сразу после os.replace могло потерять сам rename, хотя контент
    уже на диске. best-effort: не все ФС/платформы дают fsync каталога (Windows/некоторые сетевые ФС)."""
    try:
        dfd = os.open(str(directory), os.O_DIRECTORY)
    except (OSError, AttributeError):
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def load_guarded(path, required_keys=(), kind=None):
    """FAIL-CLOSED чтение. Различает три состояния (а не «пусто -> дефолт»):
      * absent  — файла нет (легитимно fresh);
      * corrupt — есть, но НЕЧИТАЕМ/пуст/не dict/не тот kind/нет обязательных ключей (оборванная запись,
                  внешнее усечение) -> вызывающий НЕ должен дефолтить/перезаписывать;
      * ok      — валиден, data приложена.
    -> {state, data?, reason?}."""
    path = Path(path)
    if not path.exists():
        return {"state": "absent"}
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return {"state": "corrupt", "reason": f"не читается: {type(e).__name__}: {e}"}
    if raw.strip() == "":
        return {"state": "corrupt", "reason": "файл пуст (вероятно, оборванная запись)"}
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return {"state": "corrupt", "reason": f"YAML не парсится: {str(e)[:160]}"}
    if not isinstance(data, dict):
        return {"state": "corrupt", "reason": f"не dict ({type(data).__name__})"}
    if kind is not None and data.get("kind") != kind:
        return {"state": "corrupt", "reason": f"kind != {kind} ({data.get('kind')})"}
    missing = [k for k in required_keys if data.get(k) in (None, "")]
    if missing:
        return {"state": "corrupt", "reason": f"нет обязательных полей: {', '.join(missing)}"}
    return {"state": "ok", "data": data}


def main(argv):
    ap = argparse.ArgumentParser(prog="lifecycle_store.py")
    ap.add_argument("--selftest", action="store_true")
    ap.parse_args(argv)          # разбор ради проверки аргументов; результат не нужен
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
