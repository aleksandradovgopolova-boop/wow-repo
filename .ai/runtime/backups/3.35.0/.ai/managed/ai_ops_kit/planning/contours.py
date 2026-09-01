#!/usr/bin/env python3
"""Контуры модели продуктового репозитория: состояние источников истины и связность (v3.35.0).

Модель объявлена в `registry/product-operating-model.yaml` — здесь только код, который по ней
работает. Две задачи:

  1. СОСТОЯНИЕ КОНТУРА — есть ли у него источник истины в этом репозитории (`sot_state`).
     Отсутствие обязательного источника — пробел модели, а не мелочь: контур объявлен, а отвечать
     на свои вопросы ему нечем.

  2. СВЯЗНОСТЬ — что изменилось в diff, что WorkItem заявил в `affects`, и совпадает ли это
     (`derive_affects` + `reconcile`). Ради этого модель и существует: нельзя закрыть работу,
     обновив React-компонент, если изменилась модель данных.

ТРИ СОСТОЯНИЯ, И ТРЕТЬЕ НЕ РАВНО ВТОРОМУ:
    changed      — сигнальный путь контура попал в diff;
    not_changed  — сигнальные пути в репозитории ЕСТЬ, и ни один не попал в diff;
    unknown      — сигнальных путей у контура в этом репозитории нет. Кит не умеет его видеть.

`unknown` НИКОГДА не сворачивается в `not_changed`. Это тот же инвариант, что `usage_status`
(3.10) и `unavailable != 0` (3.19): неизвестное нельзя считать нулём, иначе зелёный цвет
перестаёт что-либо значить. Разница между «не менялось» и «не умею видеть» — разница между
утверждением и признанием; репозиторий доопределяет сигналы в `.ai-ops.yaml`, а кит их не
выдумывает.

Использование:
  contours.py state <repo> [--json]                 # состояние источников истины по контурам
  contours.py affects <repo> --files a,b [--json]    # какие контуры затронуты изменением
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
           Path(__file__).resolve().parents[2])
MODEL_PATH = PKG / "registry" / "product-operating-model.yaml"

CHANGED, NOT_CHANGED, UNKNOWN = "changed", "not_changed", "unknown"

# Куда репозиторий вправе положить свою копию документа контекста (project/custom-оверлей —
# механизм 3.12: managed-слой кита отдельно от правды репозитория).
_OVERLAYS = ("", ".ai/project/", ".ai/custom/")


class ModelCorrupt(Exception):
    """Модель контуров недостоверна — по ней работают детект, гейт и doctor, догадки запрещены."""


def load_model(path: Path | None = None) -> dict:
    """Модель контуров из реестра. FAIL-CLOSED: битая модель — исключение, не пустой список.

    Пустая модель означала бы «контуров нет» -> связность проверять нечем -> любая работа
    выглядит согласованной. Это ровно тот класс дефекта, что порча `registry/tracks.yaml`,
    которую в 3.33 не ловил никто.
    """
    p = Path(path or MODEL_PATH)
    if not p.is_file():
        raise ModelCorrupt(f"модель контуров не найдена: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ModelCorrupt(f"модель контуров не разбирается ({p}): {e}") from e
    if not isinstance(data, dict) or not data.get("contours"):
        raise ModelCorrupt(f"модель контуров пуста или без ключа contours ({p})")
    return data


def contour_ids(model: dict) -> list:
    return [c["id"] for c in model.get("contours") or [] if c.get("id")]


def contour(model: dict, cid: str) -> dict | None:
    return next((c for c in model.get("contours") or [] if c.get("id") == cid), None)


def repo_overrides(child_root: Path) -> dict:
    """Сигналы, доопределённые репозиторием в `.ai-ops.yaml -> product_operating_model.contours`.

    Кит знает пути ТИПОВЫХ стеков, но не знает, что в этом продукте события лежат в
    `src/telemetry/`. Доопределение — способ превратить `unknown` в проверяемое состояние
    руками владельца, а не выдумкой кита.
    """
    cfg = Path(child_root) / ".ai-ops.yaml"
    if not cfg.is_file():
        return {}
    try:
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}                                  # битый конфиг — забота doctor'а, не наша
    pom = (data.get("product_operating_model") or {}).get("contours") or {}
    return {k: list((v or {}).get("change_signals") or []) for k, v in pom.items()}


def signals_for(model: dict, cid: str, overrides: dict | None = None) -> list:
    """Сигнальные пути контура: объявленные китом + доопределённые репозиторием."""
    base = list((contour(model, cid) or {}).get("change_signals") or [])
    return base + list((overrides or {}).get(cid) or [])


def _resolve(child_root: Path, rel: str) -> Path | None:
    """Путь источника истины с учётом project/custom-оверлея. -> существующий путь или None."""
    for pre in _OVERLAYS:
        p = Path(child_root) / (pre + rel)
        if p.exists():
            return p
    return None


def sot_state(child_root: Path, model: dict | None = None) -> dict:
    """Состояние источников истины по контурам.

    -> {contour_id: {"required_missing": [...], "optional_missing": [...], "present": [...],
                     "ok": bool, "question": str}}
    `ok` — только про ОБЯЗАТЕЛЬНЫЕ источники: необязательные отсутствуют законно (не у каждого
    продукта есть runbooks), и требовать их значило бы разводить ту самую бюрократию.
    """
    model = model or load_model()
    root = Path(child_root)
    out = {}
    for c in model.get("contours") or []:
        req_missing, opt_missing, present = [], [], []
        for s in c.get("source_of_truth") or []:
            rel = s.get("path") or ""
            if _resolve(root, rel):
                present.append(rel)
            elif s.get("required"):
                req_missing.append(rel)
            else:
                opt_missing.append(rel)
        out[c["id"]] = {"question": c.get("question", ""), "title": c.get("title", c["id"]),
                        "owner_role": c.get("owner_role"), "present": present,
                        "required_missing": req_missing, "optional_missing": opt_missing,
                        "ok": not req_missing}
    return out


def _matches(rel_path: str, pattern: str) -> bool:
    """Соответствие пути globу. Каталог-паттерн (`decisions/`) покрывает всё под ним.

    Реализовано на fnmatch, а не на pathlib.match: нужен `**`, пересекающий несколько сегментов
    (`**/migrations/**`), которого `Path.match` не даёт. Отдельно — префиксный случай для
    паттернов-каталогов, чтобы `schemas/` не требовал писать `schemas/**`.
    """
    rel = rel_path.replace("\\", "/")
    # НЕ lstrip("./"): это снятие НАБОРА символов, а не префикса, и `.ai-ops.yaml` превращался в
    # `ai-ops.yaml`. Следствие было тяжёлым: ни один dot-путь не совпадал со своим же сигнальным
    # паттерном, поэтому изменение исполняемой части контракта (protected_paths, approvals) и
    # CI-конвейера проходило гейт связности как согласованное. Снимаем ровно префикс `./`.
    while rel.startswith("./"):
        rel = rel[2:]
    pat = (pattern or "").replace("\\", "/")
    if not pat:
        return False
    if pat.endswith("/"):
        return rel.startswith(pat)
    if fnmatch.fnmatch(rel, pat):
        return True
    # `context/product/**` обязан покрывать и сам `context/product/x.md`, и вложенное глубже.
    if pat.endswith("/**") and rel.startswith(pat[:-3] + "/"):
        return True
    # `**/models/**` для пути без ведущих сегментов (`models/user.py`).
    if pat.startswith("**/") and fnmatch.fnmatch(rel, pat[3:]):
        return True
    return False


def _repo_has_signal(child_root: Path, patterns: list) -> bool:
    """Есть ли в репозитории ХОТЬ ОДИН путь, попадающий под сигналы контура.

    Это и есть граница между `not_changed` и `unknown`. Обход ограничен: заглядываем в объявленные
    префиксы, а не сканируем дерево целиком — на большом продукте полный обход стоил бы дороже
    самой проверки, а ответ нужен один бит.
    """
    root = Path(child_root)
    for pat in patterns or []:
        pat = pat.replace("\\", "/")
        head = pat.split("*")[0].rstrip("/")
        if head:
            p = root / head
            if p.exists():
                return True
            # `context/product/MetricCatalog.md` мог уехать в project/custom-оверлей
            if _resolve(root, head):
                return True
            continue
        # Паттерн без якоря (`**/models.py`, `**/*.proto`, `**/migrations/**`) — ищем ограниченно.
        # Хвост берём ПОСЛЕДНИМ ЗНАЧАЩИМ сегментом: у `**/migrations/**` последний сегмент — `**`,
        # а `rglob("**")` возвращает сам корень, то есть сигнал «есть» в любом каталоге. Из-за этого
        # `unknown` сворачивался в `not_changed` — главный инвариант модели нарушался везде.
        segs = [s for s in pat.split("/") if s and s != "**"]
        if not segs:
            continue                               # паттерн из одних `**` не является сигналом
        tail = segs[-1]
        try:
            if next(root.rglob(tail), None) is not None:
                return True
        except OSError:
            continue
    return False


def derive_affects(child_root: Path, changed_files: list, model: dict | None = None,
                   overrides: dict | None = None) -> dict:
    """Затронутые контуры — ВЫВОД из фактического изменения, а не заявление автора.

    -> {contour_id: {"state": changed|not_changed|unknown, "matched": [...], "signals": N,
                     "reason": str}}

    `changed_files` — пути относительно корня репозитория (обычно `git diff --name-only`).
    Пустой список НЕ означает `not_changed` для всех: он означает, что изменений не предъявлено,
    и такой ответ честнее пересчитывать на месте вызова, чем зеленить здесь.
    """
    model = model or load_model()
    root = Path(child_root)
    overrides = repo_overrides(root) if overrides is None else overrides
    files = [str(f).replace("\\", "/") for f in (changed_files or [])]

    # ПРАВИЛО СПЕЦИФИЧНОСТИ: точный путь сильнее чужого glob. `context/system/DataMap.md` — явный
    # сигнал и источник истины контура данных, но он же попадает под `context/system/**` контура
    # архитектуры; без этого правила КОРРЕКТНОЕ обновление модели данных давало ложную находку
    # `undeclared_change` по архитектуре. Гейт, выдающий шум, перестают читать — и он становится
    # хуже отсутствующего, поэтому шум здесь не косметика, а дефект. Найдено живой проверкой 3.35.
    exact_owner = {}
    for _cid in contour_ids(model):
        for _p in signals_for(model, _cid, overrides):
            if "*" not in _p and not _p.endswith("/"):
                exact_owner.setdefault(_p.replace("\\", "/"), set()).add(_cid)

    out = {}
    for cid in contour_ids(model):
        pats = signals_for(model, cid, overrides)
        if not pats:
            out[cid] = {"state": UNKNOWN, "matched": [], "signals": 0,
                        "reason": "у контура нет сигнальных путей — состояние не определяется"}
            continue
        exact_pats = [p for p in pats if "*" not in p and not p.endswith("/")]
        matched = []
        for f in files:
            hit_exact = any(_matches(f, p) for p in exact_pats)
            if hit_exact:
                matched.append(f)
                continue
            # Точный владелец этого пути есть, и это не мы -> путь принадлежит ему, не нам.
            owners = exact_owner.get(f.lstrip("./") if f.startswith("./") else f, set())
            if owners and cid not in owners:
                continue
            if any(_matches(f, p) for p in pats):
                matched.append(f)
        if matched:
            out[cid] = {"state": CHANGED, "matched": matched, "signals": len(pats),
                        "reason": f"изменение затрагивает сигнальные пути контура ({len(matched)})"}
        elif _repo_has_signal(root, pats):
            out[cid] = {"state": NOT_CHANGED, "matched": [], "signals": len(pats),
                        "reason": "сигнальные пути контура в репозитории есть и не затронуты"}
        else:
            out[cid] = {"state": UNKNOWN, "matched": [], "signals": len(pats),
                        "reason": "ни один сигнальный путь контура в репозитории не найден — "
                                  "кит не умеет видеть этот контур здесь "
                                  "(доопределить: .ai-ops.yaml -> product_operating_model)"}
    return out


def _sot_touched(child_root: Path, cid: str, changed_files: list, model: dict) -> list:
    """Какие источники истины контура попали в изменение (с учётом оверлея пути)."""
    c = contour(model, cid) or {}
    files = [str(f).replace("\\", "/") for f in (changed_files or [])]
    hit = []
    for s in c.get("source_of_truth") or []:
        rel = (s.get("path") or "").replace("\\", "/")
        if not rel:
            continue
        for f in files:
            f_norm = f
            for pre in _OVERLAYS:
                if pre and f_norm.startswith(pre):
                    f_norm = f_norm[len(pre):]
                    break
            if f_norm == rel or (rel.endswith("/") and f_norm.startswith(rel)):
                hit.append(rel)
                break
    return hit


def reconcile(child_root: Path, declared: dict, changed_files: list,
              model: dict | None = None, overrides: dict | None = None) -> dict:
    """Сверка заявленного `affects` с выведенным из изменения. -> находки гейта.

    `declared` — `affects` из WorkItem: {contour_id: bool} (или {contour_id: "changed"|...}).
    Три класса находок (имена — из `consistency.findings` модели):

      undeclared_change    — diff трогает контур, а WorkItem его не заявил. Источник истины
                             разойдётся молча, и следующая сессия прочтёт неправду.
      declared_not_updated — контур заявлен изменённым, но ни один его источник истины не
                             обновлён. Ровно случай «обновили компонент, модель данных нет».
      unknown_contour      — состояние контура не определяется. Не находка-ошибка, а признание.

    Отсутствие находок при пустом `changed_files` — не «согласовано», а «нечего сверять»:
    поле `comparable` говорит об этом прямо.
    """
    model = model or load_model()
    derived = derive_affects(child_root, changed_files, model, overrides)
    decl = {}
    for k, v in (declared or {}).items():
        if isinstance(v, bool):
            decl[k] = CHANGED if v else NOT_CHANGED
        elif isinstance(v, str) and v in (CHANGED, NOT_CHANGED, UNKNOWN):
            decl[k] = v
        else:
            decl[k] = UNKNOWN
    findings = []
    for cid in contour_ids(model):
        d = derived[cid]
        want = decl.get(cid)
        if d["state"] == UNKNOWN:
            findings.append({"id": "unknown_contour", "contour": cid, "severity": "info",
                             "detail": d["reason"]})
            continue
        if d["state"] == CHANGED and want != CHANGED:
            findings.append({"id": "undeclared_change", "contour": cid, "severity": "major",
                             "detail": f"изменение трогает {', '.join(d['matched'][:4])} — "
                                       f"контур не заявлен в affects "
                                       f"(заявлено: {want or 'ничего'})"})
        if want == CHANGED:
            touched = _sot_touched(child_root, cid, changed_files, model)
            if not touched:
                sot = [s.get("path") for s in (contour(model, cid) or {}).get("source_of_truth") or []]
                findings.append({"id": "declared_not_updated", "contour": cid, "severity": "major",
                                 "detail": "контур заявлен изменённым, но источник истины не "
                                           f"обновлён (ожидался один из: {', '.join(sot)})"})
    return {"comparable": bool(changed_files), "derived": derived, "declared": decl,
            "findings": findings,
            "verdict": ("ok" if not [f for f in findings if f["severity"] == "major"]
                        else "inconsistent")}


def suggest_affects(model: dict, work_type: str) -> dict:
    """Подсказка `affects` по типу работы: контур, который этот тип меняет ПО ОПРЕДЕЛЕНИЮ.

    Подсказка, а не замена детекту: тип говорит о намерении, diff — о факте, и расходятся они
    регулярно (задача типа `visual` вполне может тронуть контракт API).
    """
    wt = (model.get("work_types") or {}).get(work_type or "") or {}
    cid = wt.get("contour")
    return {cid: True} if cid else {}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="contours.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("state"); s.add_argument("repo"); s.add_argument("--json", action="store_true")
    a = sub.add_parser("affects"); a.add_argument("repo")
    a.add_argument("--files", default="", help="пути через запятую (git diff --name-only)")
    a.add_argument("--json", action="store_true")
    rc = sub.add_parser("reconcile"); rc.add_argument("repo")
    rc.add_argument("--files", default="", help="пути через запятую (git diff --name-only)")
    rc.add_argument("--workitem", help="features/<id>/workitem.yaml — источник объявленного affects")
    rc.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])
    root = Path(ns.repo)
    model = load_model()

    if ns.cmd == "state":
        st = sot_state(root, model)
        if ns.json:
            print(json.dumps(st, ensure_ascii=False, indent=2)); return 0
        gaps = 0
        for cid, v in st.items():
            mark = "✓" if v["ok"] else "✗"
            print(f"{mark} {v['title']} ({cid}) · роль {v['owner_role']}")
            print(f"    {v['question']}")
            if v["required_missing"]:
                gaps += 1
                print(f"    ⚠ нет обязательного источника истины: {', '.join(v['required_missing'])}")
        print(f"\nКОНТУРЫ: {len(st) - gaps}/{len(st)} с источником истины")
        return 0

    files = [x.strip() for x in (ns.files or "").split(",") if x.strip()]

    if ns.cmd == "reconcile":
        # Объявленный `affects` берём из WorkItem'а; его отсутствие — НЕ «ничего не меняем», а
        # «ещё не определяли», и находка `undeclared_change` про это и скажет.
        declared = {}
        if ns.workitem:
            wp = Path(ns.workitem)
            if wp.is_file():
                try:
                    declared = (yaml.safe_load(wp.read_text(encoding="utf-8")) or {}).get("affects") or {}
                except yaml.YAMLError:
                    declared = {}
        rep = reconcile(root, declared, files, model)
        if ns.json:
            print(json.dumps(rep, ensure_ascii=False, indent=2)); return 0
        print(f"СВЯЗНОСТЬ КОНТУРОВ: {rep['verdict']} · сверяемо {rep['comparable']}")
        for f in rep["findings"]:
            print(f"  {f['severity']:6} {f['id']} / {f['contour']}: {f['detail']}")
        # Гейт advisory: несогласованность НЕ роняет код возврата, но и не молчит.
        return 0

    der = derive_affects(root, files, model)
    if ns.json:
        print(json.dumps(der, ensure_ascii=False, indent=2)); return 0
    for cid, v in der.items():
        print(f"{v['state']:12} {cid} — {v['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
