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


def selftest():
    import tempfile
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        p = root / "sub" / "x.yaml"
        # durable_write: round-trip + создание каталога + перечитывание
        w = durable_write(p, {"kind": "t", "a": 1}, require_keys=("kind", "a"))
        expect("durable_write: ok, файл создан с каталогом", w["ok"] and p.is_file())
        expect("durable_write: временный .tmp не остался", not (root / "sub" / "x.yaml.tmp").exists())
        # require_keys не выполнены -> ok=False (fail-closed)
        w2 = durable_write(root / "y.yaml", {"kind": "t"}, require_keys=("kind", "missing"))
        expect("durable_write: отсутствует required key -> ok=False", not w2["ok"] and "missing" in w2["error"])

        # load_guarded: ok
        g = load_guarded(p, required_keys=("kind", "a"), kind="t")
        expect("load_guarded: валидный -> state=ok + data", g["state"] == "ok" and g["data"]["a"] == 1)
        # absent
        expect("load_guarded: нет файла -> absent", load_guarded(root / "nope.yaml")["state"] == "absent")
        # corrupt: пустой файл
        (root / "empty.yaml").write_text("", encoding="utf-8")
        expect("load_guarded: пустой файл -> corrupt (не absent)",
               load_guarded(root / "empty.yaml")["state"] == "corrupt")
        # corrupt: битый YAML
        (root / "bad.yaml").write_text("a: [1, 2\n  b: {", encoding="utf-8")
        expect("load_guarded: битый YAML -> corrupt", load_guarded(root / "bad.yaml")["state"] == "corrupt")
        # corrupt: не dict
        (root / "scalar.yaml").write_text("just a string\n", encoding="utf-8")
        expect("load_guarded: не dict -> corrupt", load_guarded(root / "scalar.yaml")["state"] == "corrupt")
        # corrupt: не тот kind
        expect("load_guarded: не тот kind -> corrupt",
               load_guarded(p, kind="other")["state"] == "corrupt")
        # corrupt: нет обязательного ключа
        expect("load_guarded: нет обязательного ключа -> corrupt",
               load_guarded(p, required_keys=("kind", "zzz"))["state"] == "corrupt")

        # v3.0.14 (#2): durable_write_json — round-trip + require_keys
        jp = root / "r.json"
        wj = durable_write_json(jp, {"kind": "run-report", "status": "ok"}, require_keys=("kind", "status"))
        expect("durable_write_json: ok + файл создан", wj["ok"] and jp.is_file())
        expect("durable_write_json: отсутствует required key -> ok=False",
               not durable_write_json(root / "r2.json", {"kind": "x"}, require_keys=("kind", "miss"))["ok"])

        # v3.0.15 (LifecycleStore v1.1, P1): validate-before-replace — невалидная запись НЕ затирает
        # прежний валидный файл (валидация до os.replace), и не остаётся мусорных temp.
        good = root / "src.yaml"
        durable_write(good, {"kind": "t", "n": 1}, require_keys=("kind", "n"))
        _before = good.read_text(encoding="utf-8")
        _bad = durable_write(good, {"kind": "t"}, require_keys=("kind", "n"))   # нет required n
        expect("v3.0.15: невалидная запись -> ok=False", not _bad["ok"])
        expect("v3.0.15: прежний валидный файл НЕ затёрт невалидной записью",
               good.read_text(encoding="utf-8") == _before)
        expect("v3.0.15: не dict -> ok=False, файл цел",
               not durable_write(good, "не dict")["ok"]
               and good.read_text(encoding="utf-8") == _before)
        expect("v3.0.15: нет остаточных .tmp после операций",
               not any(x.name.endswith(".tmp") or ".tmp" in x.name for x in good.parent.iterdir()))
        # keep_backup сохраняет ссылку на прежнее валидное состояние
        durable_write(good, {"kind": "t", "n": 2}, require_keys=("kind", "n"), keep_backup=True)
        expect("v3.0.15: keep_backup -> .bak с прежним валидным состоянием",
               (good.with_suffix(good.suffix + ".bak")).is_file())

        # v3.0.14 (#3): event journal — append-only, checksum-цепочка, обнаружение подмены
        jn = root / "journal.jsonl"
        journal_append(jn, {"kind": "run_start", "run_id": "R1", "workitem_id": "w"})
        journal_append(jn, {"kind": "package_start", "run_id": "R1", "package_id": "WP1"})
        journal_append(jn, {"kind": "gate", "run_id": "R1", "package_id": "WP1", "gate": "security", "status": "pass"})
        jr = journal_read(jn)
        expect("journal: 3 события, цепочка цела (ok)", jr["ok"] and len(jr["events"]) == 3)
        expect("journal: seq монотонный 0,1,2", [e["seq"] for e in jr["events"]] == [0, 1, 2])
        expect("journal: Run->Package->Gate связи сохранены",
               jr["events"][2]["run_id"] == "R1" and jr["events"][2]["package_id"] == "WP1"
               and jr["events"][2]["gate"] == "security")
        # подмена средней строки -> цепочка/checksum рвётся -> ok=False
        _lines = jn.read_text(encoding="utf-8").splitlines()
        import json as _js
        _tamp = _js.loads(_lines[1]); _tamp["status"] = "HACKED"
        _lines[1] = _js.dumps(_tamp, ensure_ascii=False)
        jn.write_text("\n".join(_lines) + "\n", encoding="utf-8")
        expect("journal: подмена строки -> обнаружено (ok=False, broken_at)",
               journal_read(jn)["ok"] is False)
        # усечение (crash в середине записи) -> оборванная строка -> ok=False
        jn2 = root / "j2.jsonl"
        journal_append(jn2, {"kind": "run_start", "run_id": "R2"})
        with open(jn2, "a", encoding="utf-8") as _f:
            _f.write('{"kind": "run_end", "run_i')   # оборванная запись без \n
        expect("journal: усечённая последняя строка -> ok=False", journal_read(jn2)["ok"] is False)

        # v3.1 (trace v0.2): verify-before-append — на битый журнал не дописываем
        jn3 = root / "j3.jsonl"
        journal_append(jn3, {"kind": "run_start", "run_id": "R3"})
        journal_append(jn3, {"kind": "package_end", "run_id": "R3", "package_id": "P1"})
        _l3 = jn3.read_text(encoding="utf-8").splitlines()
        _t = _js.loads(_l3[0]); _t["run_id"] = "TAMPER"; _l3[0] = _js.dumps(_t, ensure_ascii=False)
        jn3.write_text("\n".join(_l3) + "\n", encoding="utf-8")   # подмена -> цепочка битая
        _ap = journal_append(jn3, {"kind": "run_end", "run_id": "R3"})
        expect("v3.1 journal v0.2: append на битую цепочку -> ok=False (не расширяем повреждённое)",
               _ap["ok"] is False and "повреждён" in _ap.get("error", ""))

        # v3.1 (trace v0.2): head-marker ловит усечение ЦЕЛОЙ последней строки (v0.1 не мог)
        jn4 = root / "j4.jsonl"
        journal_append(jn4, {"kind": "run_start", "run_id": "R4"})
        journal_append(jn4, {"kind": "run_end", "run_id": "R4"})
        expect("v3.1 journal v0.2: цела -> ok (head-marker есть)",
               journal_read(jn4)["ok"] and (root / "j4.jsonl.head").exists())
        _l4 = jn4.read_text(encoding="utf-8").splitlines()
        jn4.write_text(_l4[0] + "\n", encoding="utf-8")   # удаляем последнюю ЦЕЛУЮ строку (валидный префикс)
        _r4 = journal_read(jn4)
        expect("v3.1 journal v0.2: удалена целая последняя строка -> ok=False (head-marker детектит усечение)",
               _r4["ok"] is False and "усечение" in (_r4.get("reason") or ""))

        # v3.1 (trace v0.2): validate_trace — обязательные ID своей связи
        _good = [{"kind": "run_start", "run_id": "R", "workitem_id": "R", "attempt_id": "R#a1"},
                 {"kind": "package_end", "run_id": "R", "workitem_id": "R", "package_id": "WP1"},
                 {"kind": "delivery_receipt", "run_id": "R", "delivery_id": "d1"},
                 {"kind": "run_end", "run_id": "R", "workitem_id": "R", "attempt_id": "R#a1", "status": "delivered"}]
        expect("v3.1 trace-schema: полный валидный трейс -> нет ошибок", validate_trace(_good) == [])
        expect("v3.1 trace-schema: package_end без package_id -> ошибка",
               any("package_id" in e for e in validate_trace([{"kind": "package_end", "run_id": "R", "workitem_id": "R"}])))
        expect("v3.1 trace-schema: delivery без delivery_id -> ошибка",
               any("delivery_id" in e for e in validate_trace([{"kind": "delivery", "run_id": "R"}])))
        expect("v3.1 trace-schema: run_start без attempt_id -> ошибка",
               any("attempt_id" in e for e in validate_trace([{"kind": "run_start", "run_id": "R", "workitem_id": "R"}])))

    print("lifecycle_store selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    ap = argparse.ArgumentParser(prog="lifecycle_store.py")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
