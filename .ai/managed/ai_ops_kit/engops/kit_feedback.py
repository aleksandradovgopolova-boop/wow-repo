#!/usr/bin/env python3
"""kit_feedback.py — наблюдения о КИТЕ из продуктового репозитория доезжают до кита как ДАННЫЕ.

ПОВОД — ЗАМЕР СОБСТВЕННОГО ПЛАНА (17.08.2026). Три работы в `planning/plan.yaml`
(`fixes-must-reach-the-child`, `run-execute-dies-on-ii-sreda`, `kit-as-first-step-or-as-trace`)
ссылаются на источник «сообщение параллельной сессии». То есть КАЖДОЕ наблюдение о ките, сделанное в
продуктовом репозитории, доехало потому, что человек его пересказал. Канала не существовало: поиск по
коду (`родител`, `parent_repo`, `report_to_parent`, `feedback_to_kit`) не давал ни одного механизма, а
контур `.research/` ходит в обратную сторону — от кита к дочке.

ЦЕНА УЖЕ ЗАПЛАЧЕНА ТРИЖДЫ: F-030 в ИИ-Среде ЖДАЛ срабатывания и был бы найден автоматически; дефект
«кит подогнал тест под сделанное» записан в памяти проекта ИИ-Среды и в плане кита не отражён вовсе;
наблюдение про трассируемость дошло фразой в комментарии, а не замером с уликами.

ТРИ ПРАВИЛА, БЕЗ КОТОРЫХ КАНАЛ БЕСПОЛЕЗЕН ИЛИ ВРЕДЕН:

1. НАБЛЮДЕНИЕ БЕЗ УЛИКИ ОСТАЁТСЯ НАБЛЮДЕНИЕМ. `defect` требует хотя бы одной улики — файла с
   цитатой или команды с выводом. Иначе канал производил бы «дефекты» из впечатлений, а кит уже
   платил за обратное направление той же ошибки (evidence-слой, ложный green): утверждение без
   основания дороже отсутствия утверждения.
2. КАНАЛ ДВУСТОРОННИЙ. Наблюдение получает состояние — принято, стало работой, отклонено с причиной —
   и состояние возвращается В ДОЧКУ, в тот же файл. Канал в одну сторону перестают наполнять; он
   умирает молча, и никто не знает, что он умер.
3. НИКУДА НЕ ОТПРАВЛЯЕМ. Сборка — локальная, с машины владельца: дочка может быть закрытым
   продуктом, а наблюдение несёт её пути и вывод команд. Сети здесь нет вообще — ни запроса, ни
   токена. Публикация наружу, если понадобится, — отдельное решение владельца.

ГДЕ ЧТО ЛЕЖИТ:
  дочка: `.ai/kit-feedback/<id>.yaml`         — наблюдение и его состояние (источник истины о нём);
  кит:   `findings/from-children/<id>.yaml`   — доставленная копия с указанием, откуда она.

CLI (в дочке):  kit_feedback.py record <child_root> "текст" [--evidence-file path=цитата]
                                       [--evidence-command "cmd=вывод"] [--severity p1] [--class defect]
       (в дочке) kit_feedback.py status <child_root>
       (в ките)  kit_feedback.py collect <kit_root> <child_root> [<child_root> ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402,F401

OBSERVATION_CLASSES = ("defect", "friction", "question", "idea")
SEVERITIES = ("p0", "p1", "p2")
# Состояния наблюдения. `new` живёт в дочке, `delivered` ставит сборка, остальные — решение по киту.
STATES = ("new", "delivered", "accepted", "became_work", "rejected")
EVIDENCE_KINDS = ("file", "command", "note")

# ЯРЛЫКИ СОСТОЯНИЙ ЕДУТ В ОТЧЁТЕ. Слой человеческого языка (`ui/presenter`) лежит НИЖЕ ядра и
# импортировать его отсюда нельзя, а он не вправе импортировать нас — поэтому названия несёт сам
# отчёт. Без этого владелец читал бы `became_work`/`rejected` внутренними словами: первая же проба
# канала напечатала их человеку в ответе «что стало с твоими замечаниями».
STATE_NAMES = {"new": "записано", "delivered": "дошло до кита",
               "accepted": "принято", "became_work": "стало работой",
               "rejected": "отклонено"}

# Класс, для которого улика ОБЯЗАТЕЛЬНА. Остальные классы — законные без улик: трение и вопрос это
# впечатление и незнание, им нечего цитировать, и требовать от них доказательство значило бы просто
# запретить о них говорить.
_CLASS_REQUIRES_EVIDENCE = ("defect",)

CHILD_DIR = Path(".ai") / "kit-feedback"
KIT_DIR = Path("findings") / "from-children"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(text, limit=32):
    s = re.sub(r"[^0-9a-zа-яё]+", "-", str(text or "").lower(), flags=re.IGNORECASE)
    return s.strip("-")[:limit] or "observation"


def make_id(statement, at=None):
    """Идентификатор наблюдения — ДЕТЕРМИНИРОВАННЫЙ по тексту.

    Дата в имени для человека, хвост хэша — для уникальности. Детерминированность важнее красоты:
    один и тот же текст, записанный дважды, обязан дать один файл, иначе журнал наполнится копиями
    одного наблюдения, и «сколько раз это случилось» перестанет читаться.
    """
    day = (at or _now())[:10]
    tail = hashlib.sha1(str(statement or "").strip().encode("utf-8")).hexdigest()[:8]
    return f"obs-{day}-{_slug(statement, 24)}-{tail}"


def _kit_version(root):
    for p in (Path(root) / "VERSION", Path(root) / ".ai" / "managed" / "VERSION"):
        try:
            return p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return None


def _git_head(root):
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return (r.stdout or "").strip()[:12] or None if r.returncode == 0 else None


def build(child_root, statement, *, evidence=None, severity=None, observation_class=None, at=None):
    """Собрать KitObservation (без записи). Определяет, чем ЭТО подтверждено, и не додумывает."""
    child_root = Path(child_root)
    at = at or _now()
    ev = list(evidence or [])
    cls = (observation_class or "").strip().lower() or ("defect" if ev else "friction")
    return {
        "schema_version": 1, "kind": "KitObservation",
        "id": make_id(statement, at), "at": at,
        "statement": str(statement or "").strip(),
        "observation_class": cls,
        "severity": (severity or "").strip().lower() or None,
        "kit_version": _kit_version(child_root),
        "child": {"name": child_root.resolve().name, "path": str(child_root.resolve()),
                  "commit": _git_head(child_root)},
        "evidence": ev,
        "state": "new", "state_reason": None, "delivered_to": None,
    }


def _looks_like_path(statement):
    """Похоже ли утверждение на путь, а не на наблюдение.

    Узко намеренно: длину наблюдения здесь НЕ проверяем. Короткое замечание («тормозит») законно, и
    отказ по длине отшивал бы настоящую обратную связь — а это дороже, чем один мусорный файл.
    """
    s = statement.strip()
    if s in (".", "..", "./", "../"):
        return True
    if any(c in s for c in (" ", "\n", "\t")):     # фраза, а не путь
        return False
    return (s.startswith(("/", "./", "../", "~/")) or s.endswith("/")) and Path(s).expanduser().exists()


def check(obs):
    """Валидация наблюдения. -> список ошибок. Главная из них — дефект без улики."""
    e = []
    if not isinstance(obs, dict) or obs.get("kind") != "KitObservation":
        return ["kind должен быть KitObservation"]
    st = str(obs.get("statement") or "").strip()
    if not st:
        e.append("statement пуст: наблюдение без утверждения — это не наблюдение")
    elif _looks_like_path(st):
        # ПРОБА КАНАЛА НА ЖИВОЙ ДОЧКЕ (18.08.2026): `./ai-ops feedback .` записала наблюдение с
        # содержанием «.» и ответила «записал». Разбор позиционных отдал путь репозитория в текст, а
        # запись его приняла. Отказ живёт ЗДЕСЬ, а не только в CLI: канал наполняют и модулем, и
        # командой, а запись, которой нельзя верить, хуже отсутствующей — она попадает в сборку,
        # тратит решение по себе и учит не доверять каналу.
        e.append(f"statement похож на путь, а не на наблюдение ({st!r}): "
                 "скажите, ЧТО кит сделал не так — путь репозитория он подставляет сам")
    cls = obs.get("observation_class")
    if cls not in OBSERVATION_CLASSES:
        e.append(f"observation_class ∉ {OBSERVATION_CLASSES} (got {cls!r})")
    sev = obs.get("severity")
    if sev is not None and sev not in SEVERITIES:
        e.append(f"severity ∉ {SEVERITIES} (got {sev!r})")
    if obs.get("state") not in STATES:
        e.append(f"state ∉ {STATES} (got {obs.get('state')!r})")
    if obs.get("state") == "rejected" and not str(obs.get("state_reason") or "").strip():
        e.append("rejected без причины: отклонение без объяснения останавливает канал")
    ev = obs.get("evidence")
    if ev is None or not isinstance(ev, list):
        e.append("evidence должен быть списком (пустым — тоже, но списком)")
        ev = []
    for i, item in enumerate(ev):
        if not isinstance(item, dict):
            e.append(f"evidence[{i}] не объект")
            continue
        k = item.get("kind")
        if k not in EVIDENCE_KINDS:
            e.append(f"evidence[{i}].kind ∉ {EVIDENCE_KINDS} (got {k!r})")
        elif k == "file" and not item.get("path"):
            e.append(f"evidence[{i}]: улика-файл без path")
        elif k == "command" and not item.get("command"):
            e.append(f"evidence[{i}]: улика-команда без command")
    if cls in _CLASS_REQUIRES_EVIDENCE and not ev:
        e.append(f"класс '{cls}' требует улику (файл с цитатой или команду с выводом) — "
                 "иначе это впечатление, а не дефект")
    return e


def evidenced(obs):
    """Есть ли у наблюдения основание. Отдельно от класса: класс объявляют, улику проверяют."""
    return bool(obs.get("evidence"))


def child_path(child_root, obs_id):
    return Path(child_root) / CHILD_DIR / f"{obs_id}.yaml"


def kit_path(kit_root, obs_id):
    return Path(kit_root) / KIT_DIR / f"{obs_id}.yaml"


def _dump(path, doc):
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _load(path):
    """(документ, ошибка). Непрочитанный файл — ОШИБКА, а не отсутствие наблюдения.

    «Файла нет» и «файл не разбирается» — РАЗНЫЕ факты, и они названы по-разному. Первая проба канала
    на настоящих наблюдениях это и показала: отсутствующее наблюдение сообщало «не разобран
    (FileNotFoundError)», то есть звало искать порчу там, где искать надо опечатку в имени. Модуль,
    который сам требует отличать «не знаю» от «нет», не вправе путать это у себя.
    """
    path = Path(path)
    if not path.is_file():
        return None, f"{path.name}: наблюдения нет по этому пути"
    try:
        import yaml
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 — файл есть, но не разобран: молчать нельзя
        return None, f"{path.name}: не разобран ({type(exc).__name__}: {exc})"[:200]
    if not isinstance(doc, dict):
        return None, f"{Path(path).name}: не объект"
    return doc, None


def record(child_root, statement, *, evidence=None, severity=None, observation_class=None):
    """Записать наблюдение в дочке. -> (path, created, errors).

    Повторная запись того же текста НЕ создаёт второй файл и НЕ трогает уже выставленное состояние:
    наблюдение, по которому уже принято решение, не должно возвращаться в `new` от того, что человек
    сказал о нём второй раз.
    """
    obs = build(child_root, statement, evidence=evidence, severity=severity,
                observation_class=observation_class)
    errors = check(obs)
    p = child_path(child_root, obs["id"])
    if p.is_file():
        return p, False, errors
    if errors:
        return p, False, errors
    _dump(p, obs)
    return p, True, []


def load_all(child_root):
    """(наблюдения, ошибки) из дочки. Порядок — по id, то есть по дате и тексту."""
    d = Path(child_root) / CHILD_DIR
    out, errors = [], []
    if not d.is_dir():
        return out, errors
    for f in sorted(d.glob("*.yaml")):
        doc, err = _load(f)
        if err:
            errors.append(err)
            continue
        problems = check(doc)
        if problems:
            errors.append(f"{f.name}: " + "; ".join(problems))
            continue
        out.append(doc)
    return out, errors


def collect(kit_root, child_roots, *, dry_run=False):
    """Собрать наблюдения дочек в кит и вернуть состояние В ДОЧКУ. -> отчёт.

    Сухой прогон по умолчанию НЕ выбран сознательно: команду зовёт владелец кита на своей машине, и
    запись идёт в его же репозиторий и в его же дочки. Но `--dry-run` есть, потому что запись в чужое
    дерево человек вправе увидеть до того, как она произошла.

    ЧТО СЧИТАЕТСЯ ДОСТАВКОЙ: файл в ките И состояние `delivered` в дочке. Одно без другого — не
    доставка: копия без отметки соберётся ещё раз, отметка без копии соврёт дочке.
    """
    kit_root = Path(kit_root)
    report = {"schema_version": 1, "kind": "KitFeedbackCollect", "at": _now(),
              "kit_root": str(kit_root.resolve()), "dry_run": bool(dry_run),
              "children": [], "delivered": [], "already": [], "unevidenced": [], "errors": []}
    for cr in child_roots:
        cr = Path(cr)
        obs_list, errors = load_all(cr)
        report["children"].append({"path": str(cr.resolve()), "observations": len(obs_list),
                                   "errors": len(errors)})
        report["errors"] += [f"{cr.name}: {e}" for e in errors]
        for obs in obs_list:
            if obs.get("state") != "new":
                report["already"].append({"id": obs["id"], "state": obs.get("state"),
                                          "child": obs.get("child", {}).get("name")})
                continue
            if not evidenced(obs):
                # Не отказ и не тишина: наблюдение доедет, но названо тем, что оно есть.
                report["unevidenced"].append({"id": obs["id"],
                                              "statement": obs["statement"][:120]})
            if dry_run:
                report["delivered"].append({"id": obs["id"], "written": None,
                                            "child": obs.get("child", {}).get("name")})
                continue
            landed = dict(obs, state="delivered",
                          delivered_to={"kit_root": str(kit_root.resolve()), "at": _now()})
            kp = _dump(kit_path(kit_root, obs["id"]), landed)
            _dump(child_path(cr, obs["id"]), landed)
            report["delivered"].append({"id": obs["id"], "written": str(kp),
                                        "child": obs.get("child", {}).get("name")})
    return report


def set_state(kit_root, child_root, obs_id, state, reason=None, work_item=None):
    """Решение по наблюдению — и в кит, и обратно в дочку. -> (обновлённое, ошибки).

    Обе записи или ни одной: расхождение состояний между китом и дочкой хуже, чем его отсутствие, —
    дочка увидит «принято» там, где кит уже отклонил, и перестанет верить каналу.
    """
    if state not in STATES:
        return None, [f"state ∉ {STATES} (got {state!r})"]
    kp, cp = kit_path(kit_root, obs_id), child_path(child_root, obs_id)
    src = kp if kp.is_file() else cp
    if not src.is_file():
        # Отдельная ветка, а не «не разобран»: тут человек скорее всего ошибся в id, и сообщение
        # обязано вести к опечатке, а не к поиску порчи файла.
        return None, [f"наблюдения {obs_id} нет ни в ките, ни в дочке — проверь id "
                      f"(`kit_feedback.py status <дочка>` покажет настоящие)"]
    doc, err = _load(src)
    if err or doc is None:
        return None, [err or f"наблюдение {obs_id} не читается"]
    doc = dict(doc, state=state, state_reason=reason,
               decided_at=_now(), became_work=work_item if state == "became_work" else None)
    problems = check(doc)
    if problems:
        return None, problems
    _dump(kp, doc)
    if cp.is_file() or Path(child_root).is_dir():
        _dump(cp, doc)
    return doc, []


def status(child_root):
    """Что дочка знает о судьбе своих наблюдений. -> отчёт для человека."""
    obs, errors = load_all(child_root)
    by_state = {}
    for o in obs:
        by_state.setdefault(o.get("state"), []).append(o)
    return {"schema_version": 1, "kind": "KitFeedbackStatus",
            "child": str(Path(child_root).resolve()),
            "total": len(obs),
            "by_state": {k: len(v) for k, v in sorted(by_state.items())},
            "state_names": dict(STATE_NAMES),
            "waiting": [{"id": o["id"], "statement": o["statement"][:120],
                         "state": o.get("state"),
                         "state_name": STATE_NAMES.get(o.get("state"), o.get("state"))}
                        for o in by_state.get("new", []) + by_state.get("delivered", [])],
            "decided": [{"id": o["id"], "state": o["state"],
                         "state_name": STATE_NAMES.get(o["state"], o["state"]),
                         "statement": o["statement"][:120],
                         "reason": o.get("state_reason")}
                        for o in obs if o.get("state") in ("accepted", "became_work", "rejected")],
            "errors": errors}


def render_collect(rep):
    L = [f"COLLECT{' (сухой прогон)' if rep['dry_run'] else ''}: доставлено {len(rep['delivered'])}, "
         f"уже было {len(rep['already'])}, без улик {len(rep['unevidenced'])}, "
         f"ошибок {len(rep['errors'])}"]
    for d in rep["delivered"]:
        L.append(f"  → {d['id']} (из {d['child'] or '—'})")
    for u in rep["unevidenced"]:
        L.append(f"  ? без улики: {u['id']} — {u['statement']}")
    for e in rep["errors"]:
        L.append(f"  ✗ {e}")
    return "\n".join(L)


def render_status(rep):
    L = [f"KIT-FEEDBACK {rep['child']}: наблюдений {rep['total']} · "
         + (", ".join(f"{k}={v}" for k, v in rep["by_state"].items()) or "—")]
    for w in rep["waiting"]:
        L.append(f"  · ждёт ответа: {w['id']} — {w['statement']}")
    for d in rep["decided"]:
        L.append(f"  · {d['state']}: {d['id']}" + (f" — {d['reason']}" if d["reason"] else ""))
    for e in rep["errors"]:
        L.append(f"  ✗ {e}")
    return "\n".join(L)


def _parse_pair(raw):
    """`ключ=значение` -> (ключ, значение). Значение вправе содержать `=`."""
    s = str(raw or "")
    if "=" not in s:
        return s.strip(), ""
    k, v = s.split("=", 1)
    return k.strip(), v.strip()


def evidence_from_args(files, commands, notes):
    ev = []
    for raw in files or []:
        path, quote = _parse_pair(raw)
        ev.append({"kind": "file", "path": path, "quote": quote or None})
    for raw in commands or []:
        cmd, out = _parse_pair(raw)
        ev.append({"kind": "command", "command": cmd, "output": out or None})
    for raw in notes or []:
        ev.append({"kind": "note", "text": str(raw)})
    return ev


def main(argv=None):
    ap = argparse.ArgumentParser(prog="kit_feedback.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="записать наблюдение о ките (в дочке)")
    r.add_argument("child_root")
    r.add_argument("statement")
    r.add_argument("--evidence-file", action="append", metavar="ПУТЬ=ЦИТАТА")
    r.add_argument("--evidence-command", action="append", metavar="КОМАНДА=ВЫВОД")
    r.add_argument("--evidence-note", action="append", metavar="ТЕКСТ")
    r.add_argument("--severity", choices=list(SEVERITIES))
    r.add_argument("--class", dest="observation_class", choices=list(OBSERVATION_CLASSES))
    r.add_argument("--json", action="store_true")

    s = sub.add_parser("status", help="судьба наблюдений этой дочки")
    s.add_argument("child_root")
    s.add_argument("--json", action="store_true")

    c = sub.add_parser("collect", help="собрать наблюдения дочек в кит (в ките)")
    c.add_argument("kit_root")
    c.add_argument("child_roots", nargs="+")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--json", action="store_true")

    d = sub.add_parser("decide", help="решение по наблюдению — и в кит, и обратно в дочку")
    d.add_argument("kit_root")
    d.add_argument("child_root")
    d.add_argument("obs_id")
    d.add_argument("state", choices=list(STATES))
    d.add_argument("--reason")
    d.add_argument("--work-item")
    d.add_argument("--json", action="store_true")

    a = ap.parse_args(argv)

    if a.cmd == "record":
        ev = evidence_from_args(a.evidence_file, a.evidence_command, a.evidence_note)
        p, created, errors = record(a.child_root, a.statement, evidence=ev,
                                    severity=a.severity, observation_class=a.observation_class)
        if a.json:
            print(json.dumps({"path": str(p), "created": created, "errors": errors},
                             ensure_ascii=False, indent=2))
        elif errors:
            print("НЕ ЗАПИСАНО: " + "; ".join(errors))
        else:
            print(f"RECORD: {'создано' if created else 'уже было'} {p}")
        return 1 if errors else 0

    if a.cmd == "status":
        rep = status(a.child_root)
        print(json.dumps(rep, ensure_ascii=False, indent=2) if a.json else render_status(rep))
        return 1 if rep["errors"] else 0

    if a.cmd == "collect":
        rep = collect(a.kit_root, a.child_roots, dry_run=a.dry_run)
        print(json.dumps(rep, ensure_ascii=False, indent=2) if a.json else render_collect(rep))
        return 1 if rep["errors"] else 0

    if a.cmd == "decide":
        doc, errors = set_state(a.kit_root, a.child_root, a.obs_id, a.state,
                                reason=a.reason, work_item=a.work_item)
        if a.json:
            print(json.dumps({"observation": doc, "errors": errors}, ensure_ascii=False, indent=2))
        elif errors:
            print("НЕ ЗАПИСАНО: " + "; ".join(errors))
        else:
            print(f"DECIDE: {a.obs_id} -> {a.state}")
        return 1 if errors else 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
