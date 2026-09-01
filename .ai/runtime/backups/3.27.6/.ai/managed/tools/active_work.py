#!/usr/bin/env python3
"""Реестр активных работ репозитория (v2.22, связи задач — v2.23) — координация
параллельных сессий.

Несколько сессий Claude могут работать в одном репозитории одновременно (новая фича,
фикс интерфейса, аналитика, безопасность). Чтобы они не уничтожали работу друг друга,
каждая регистрирует свою работу здесь: id WorkItem, ветка, затрагиваемые зоны, сессия,
а также ЯВНЫЕ связи — от кого зависит (`depends_on`) и какие общие контракты трогает
(`shared_contracts`). Новая сессия видит карту и получает conflict forecast с типом:

  - area        — две сессии трогают одну зону кода/продукта;
  - contract    — две сессии трогают один общий контракт (схема данных, API, артефакт) →
                  риск расхождения контракта, зафиксируйте общий;
  - dependency  — задача ждёт другую активную задачу (её зависимость ещё не done);
  - cycle       — циклическая зависимость задач (ошибка, не предупреждение).

Реестр НЕ блокирует файлы жёстко, а предупреждает и предлагает решение.

Использование:
  active_work.py register <file> <id> --branch B --areas a,b --session S
                 [--workitem P] [--status in-progress] [--depends x,y] [--contracts p,q] [--at DATE]
  active_work.py list     <file> [--json]
  active_work.py check    <file> --areas a,b [--depends x,y] [--contracts p,q] [--exclude id] [--json]
  active_work.py finish   <file> <id>
  active_work.py --selftest
Возврат: 0 — ок (пересечения area/contract/dependency — предупреждения, не ошибка);
1 — ошибка использования/данных или циклическая зависимость при register.
"""

import argparse
import contextlib
import json
import sys
from pathlib import Path

import yaml

import lifecycle_store as _ls   # v3.0.12: durable запись + fail-closed чтение общего реестра

STATUS = {"in-progress", "review", "blocked", "done"}


class ActiveWorkCorrupt(Exception):
    """Реестр active-work недостоверен (повреждён/не сохранён) — координация сессий небезопасна."""


def load(path: Path):
    """v3.0.12 (finding аудита блок B): FAIL-CLOSED. Прежде safe_load(...) or {} на битом/пустом реестре
    возвращал ПУСТУЮ карту -> concurrency forecast «пересечений нет» на потерянных записях (две сессии
    сталкивались). Теперь: отсутствует -> fresh; повреждён -> raise (не тихая пустая карта)."""
    g = _ls.load_guarded(Path(path), kind="active-work")
    if g["state"] == "absent":
        return {"schema_version": 1, "kind": "active-work", "active": []}
    if g["state"] == "corrupt":
        raise ActiveWorkCorrupt(f"active-work реестр повреждён ({g['reason']}) — координация "
                                "параллельных сессий недостоверна; нужна явная recovery")
    data = g["data"]
    data.setdefault("schema_version", 1)
    data.setdefault("kind", "active-work")
    data.setdefault("active", [])
    return data


def save(path: Path, data: dict):
    """v3.0.12: АТОМАРНАЯ durable-запись общего реестра (tmp+fsync+rename+fsync-dir+перечитывание).
    Сбой -> raise (registration потеряна — не молчим)."""
    r = _ls.durable_write(Path(path), data, require_keys=("kind", "active"))
    if not r.get("ok"):
        raise ActiveWorkCorrupt(f"не удалось надёжно сохранить active-work: {r.get('error')}")


@contextlib.contextmanager
def _locked(path: Path):
    """v3.0.12 (finding аудита блок B): межпроцессная блокировка вокруг read-modify-write общего реестра,
    чтобы конкурентные register/finish не теряли записи друг друга (last-writer-wins TOCTOU). best-effort:
    на платформах без fcntl (Windows) деградирует до no-op — не хуже прежнего поведения."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        import fcntl
    except ImportError:
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


def _active_others(active, exclude_id):
    return [w for w in active if w.get("status") != "done" and w.get("id") != exclude_id]


def classify(active, entry):
    """Классифицировать пересечения новой/проверяемой работы с активными.
    entry: dict с id, affected_areas, depends_on, shared_contracts. Возвращает список
    находок с полем kind ∈ {area, contract, dependency}."""
    wid = entry.get("id")
    areas = set(entry.get("affected_areas") or [])
    deps = set(entry.get("depends_on") or [])
    contracts = set(entry.get("shared_contracts") or [])
    others = _active_others(active, wid)
    out = []
    for w in others:
        shared_areas = sorted(areas & set(w.get("affected_areas") or []))
        if shared_areas:
            out.append({"kind": "area", "id": w.get("id"), "branch": w.get("branch"),
                        "owner_session": w.get("owner_session"), "detail": shared_areas})
        shared_contracts = sorted(contracts & set(w.get("shared_contracts") or []))
        if shared_contracts:
            out.append({"kind": "contract", "id": w.get("id"), "branch": w.get("branch"),
                        "owner_session": w.get("owner_session"), "detail": shared_contracts})
        if w.get("id") in deps:
            out.append({"kind": "dependency", "id": w.get("id"), "branch": w.get("branch"),
                        "owner_session": w.get("owner_session"), "detail": w.get("status")})
    return out


def find_cycle(active, entry):
    """Есть ли цикл в графе depends_on после добавления entry? Возвращает путь цикла или []."""
    graph = {w.get("id"): list(w.get("depends_on") or []) for w in active}
    graph[entry.get("id")] = list(entry.get("depends_on") or [])
    start = entry.get("id")
    stack = [(start, [start])]
    seen_paths = []
    # DFS с поиском возврата к уже посещённому в текущем пути
    def dfs(node, path):
        for nxt in graph.get(node, []):
            if nxt == start and len(path) >= 1:
                return path + [nxt]
            if nxt in path:
                return path[path.index(nxt):] + [nxt]
            if nxt in graph:
                r = dfs(nxt, path + [nxt])
                if r:
                    return r
        return None
    return dfs(start, [start]) or []


def _forecast_lines(confs):
    lines = []
    label = {"area": "зона", "contract": "контракт", "dependency": "зависимость"}
    for c in confs:
        k = c["kind"]
        if k == "dependency":
            lines.append(f"  ⚠ зависимость: '{c['id']}' ещё в работе (статус {c['detail']}, "
                         f"ветка {c['branch']}, сессия {c['owner_session']})")
        else:
            what = "зоны" if k == "area" else "контракты"
            lines.append(f"  ⚠ {label[k]}: пересечение с '{c['id']}' (ветка {c['branch']}, "
                         f"сессия {c['owner_session']}): общие {what} {', '.join(c['detail'])}")
    if confs:
        lines.append("  Варианты: дождаться · перенести зависимость · объединить задачи · "
                     "зафиксировать общий контракт · работать в разных слоях.")
    return lines


def register(path, wid, branch, areas, session, workitem=None, status="in-progress",
             depends=None, contracts=None, at=None):
    if branch in (None, "", "main", "master"):
        print("ОШИБКА: работа не должна вестись в main/master — задайте ветку/worktree.")
        return 1
    if status not in STATUS:
        print(f"ОШИБКА: status '{status}' не в {sorted(STATUS)}")
        return 1
    if not areas:
        print("ОШИБКА: нужны affected_areas (основа conflict forecast).")
        return 1
    # v3.0.12: весь read-modify-write под межпроцессной блокировкой (иначе конкурентная сессия могла
    # перезаписать нашу регистрацию — last-writer-wins — и concurrency-forecast увидел бы неполную карту).
    with _locked(path):
        data = load(path)
        entry = {"id": wid, "branch": branch, "status": status,
                 "affected_areas": list(areas), "owner_session": session}
        if workitem:
            entry["workitem"] = workitem
        if depends:
            entry["depends_on"] = list(depends)
        if contracts:
            entry["shared_contracts"] = list(contracts)
        if at:
            entry["started_at"] = at
        # цикл зависимостей — это ошибка, а не предупреждение
        cycle = find_cycle(data["active"], entry)
        if cycle:
            print(f"ОШИБКА: циклическая зависимость задач: {' -> '.join(cycle)}. "
                  f"Разорвите цикл (одна задача не может транзитивно зависеть от себя).")
            return 1
        confs = classify(data["active"], entry)
        data["active"] = [w for w in data["active"] if w.get("id") != wid] + [entry]
        save(path, data)
    print(f"ACTIVE-WORK: зарегистрирована работа '{wid}' (ветка {branch}, сессия {session}).")
    for line in _forecast_lines(confs):
        print(line)
    return 0


def list_cmd(path, as_json=False):
    data = load(path)
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    act = [w for w in data["active"] if w.get("status") != "done"]
    if not act:
        print("ACTIVE-WORK: активных работ нет.")
        return 0
    print(f"ACTIVE-WORK: {len(act)} активных работ:")
    for w in act:
        extra = ""
        if w.get("depends_on"):
            extra += f" зависит от: {', '.join(w['depends_on'])};"
        if w.get("shared_contracts"):
            extra += f" контракты: {', '.join(w['shared_contracts'])};"
        print(f"  - {w.get('id')} [{w.get('status')}] ветка {w.get('branch')} "
              f"зоны: {', '.join(w.get('affected_areas') or [])} (сессия {w.get('owner_session')}){extra}")
    return 0


def check_cmd(path, areas, depends=None, contracts=None, exclude_id=None, as_json=False):
    data = load(path)
    probe = {"id": exclude_id, "affected_areas": list(areas),
             "depends_on": list(depends or []), "shared_contracts": list(contracts or [])}
    confs = classify(data["active"], probe)
    if as_json:
        print(json.dumps({"schema_version": 1, "kind": "conflict-forecast",
                          "areas": list(areas), "conflicts": confs}, ensure_ascii=False, indent=2))
        return 0
    if not confs:
        print(f"CONFLICT-FORECAST: пересечений по зонам {', '.join(areas)} нет — можно стартовать.")
        return 0
    print(f"CONFLICT-FORECAST: возможны пересечения:")
    for line in _forecast_lines(confs):
        print(line)
    return 0


def finish_cmd(path, wid):
    # v3.0.12: read-modify-write под блокировкой (симметрично register — без гонки на общем реестре)
    with _locked(path):
        data = load(path)
        found = False
        for w in data["active"]:
            if w.get("id") == wid:
                w["status"] = "done"
                found = True
        if not found:
            print(f"ACTIVE-WORK: работа '{wid}' не найдена.")
            return 1
        save(path, data)
    print(f"ACTIVE-WORK: работа '{wid}' помечена done.")
    return 0


def selftest():
    import tempfile
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "active-work.yaml"
        register(p, "dashboard-editing", "feature/dashboard-editing",
                 ["dashboard-editor", "session-context"], "session-1",
                 contracts=["schemas/dashboard.schema.json"], at="2026-07-15")
        d = load(p)
        expect("register: запись добавлена", any(w["id"] == "dashboard-editing" for w in d["active"]))
        expect("register в main -> ошибка", register(p, "x", "main", ["a"], "s") == 1)
        expect("register без areas -> ошибка", register(p, "x", "feature/x", [], "s") == 1)

        # area-конфликт
        confs = classify(d["active"], {"id": "new", "affected_areas": ["session-context", "catalog"]})
        expect("classify: area-конфликт", any(c["kind"] == "area" and "session-context" in c["detail"] for c in confs))

        # contract-конфликт
        confs = classify(d["active"], {"id": "new", "affected_areas": ["x"],
                                       "shared_contracts": ["schemas/dashboard.schema.json"]})
        expect("classify: contract-конфликт", any(c["kind"] == "contract" for c in confs))

        # dependency: новая зависит от активной
        confs = classify(d["active"], {"id": "new", "affected_areas": ["x"],
                                       "depends_on": ["dashboard-editing"]})
        expect("classify: dependency на активную", any(c["kind"] == "dependency" for c in confs))

        # непересекающееся -> пусто
        confs = classify(d["active"], {"id": "new", "affected_areas": ["catalog", "api"]})
        expect("classify: непересекающееся -> пусто", confs == [])

        # exclude себя
        confs = classify(d["active"], {"id": "dashboard-editing", "affected_areas": ["dashboard-editor"]})
        expect("classify: сама себя не считает", confs == [])

        # цикл зависимостей -> ошибка register
        register(p, "a", "feature/a", ["za"], "s", depends=["b"], at="2026-07-15")
        rc = register(p, "b", "feature/b", ["zb"], "s", depends=["a"], at="2026-07-15")
        expect("цикл зависимостей a<->b -> ошибка", rc == 1)

        # done не участвует
        finish_cmd(p, "dashboard-editing")
        confs = classify(load(p)["active"], {"id": "new", "affected_areas": ["dashboard-editor"]})
        expect("done не даёт конфликт", all(c["id"] != "dashboard-editing" for c in confs))

        # v3.0.12 (finding аудита блок B): durable + fail-closed чтение общего реестра.
        expect("v3.0.12: save пишет атомарно (нет остаточного .tmp)",
               p.is_file() and not p.with_suffix(p.suffix + ".tmp").exists())
        p.write_text("", encoding="utf-8")   # оборванная запись
        try:
            load(p)
            expect("v3.0.12: пустой реестр -> ActiveWorkCorrupt (не тихая пустая карта)", False)
        except ActiveWorkCorrupt:
            expect("v3.0.12: пустой реестр -> ActiveWorkCorrupt (не тихая пустая карта)", True)
        p.write_text("kind: active-work\nactive: [ :::\n", encoding="utf-8")   # битый YAML
        try:
            load(p)
            expect("v3.0.12: битый YAML реестр -> ActiveWorkCorrupt", False)
        except ActiveWorkCorrupt:
            expect("v3.0.12: битый YAML реестр -> ActiveWorkCorrupt", True)
        p.unlink()
        expect("v3.0.12: отсутствующий реестр -> fresh (не ошибка)", load(p)["active"] == [])

    print("active_work selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _split(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def main(argv):
    if "--selftest" in argv:
        return selftest()
    ap = argparse.ArgumentParser(prog="active_work.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("register")
    r.add_argument("file"); r.add_argument("id")
    r.add_argument("--branch", required=True)
    r.add_argument("--areas", required=True, help="через запятую")
    r.add_argument("--session", required=True)
    r.add_argument("--workitem")
    r.add_argument("--status", default="in-progress")
    r.add_argument("--depends", help="id задач-зависимостей через запятую")
    r.add_argument("--contracts", help="пути общих контрактов через запятую")
    r.add_argument("--at")

    l = sub.add_parser("list")
    l.add_argument("file"); l.add_argument("--json", action="store_true")

    c = sub.add_parser("check")
    c.add_argument("file"); c.add_argument("--areas", required=True)
    c.add_argument("--depends"); c.add_argument("--contracts")
    c.add_argument("--exclude"); c.add_argument("--json", action="store_true")

    f = sub.add_parser("finish")
    f.add_argument("file"); f.add_argument("id")

    a = ap.parse_args(argv)
    if a.cmd == "register":
        return register(Path(a.file), a.id, a.branch, _split(a.areas), a.session,
                        a.workitem, a.status, _split(a.depends), _split(a.contracts), a.at)
    if a.cmd == "list":
        return list_cmd(Path(a.file), a.json)
    if a.cmd == "check":
        return check_cmd(Path(a.file), _split(a.areas), _split(a.depends),
                        _split(a.contracts), a.exclude, a.json)
    if a.cmd == "finish":
        return finish_cmd(Path(a.file), a.id)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
