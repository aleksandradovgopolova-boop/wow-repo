#!/usr/bin/env python3
"""child_registry.py — ДОБРОВОЛЬНАЯ регистрация дочек: честный охват БЕЗ телеметрии.

ПОВОД. Кит ставится в каждый репозиторий локально и НИКУДА не сообщает о себе — это осознанный
инвариант приватности (дочка может быть закрытым продуктом). Цена инварианта: владелец кита не знает
охвата — «сколько репозиториев подключено, не только мои». Телеметрия («звонок домой») эту цену
снимала бы ценой приватности, поэтому она ПРЯМО ЗАПРЕЩЕНА. Здесь — противоположный механизм: не кит
собирает о дочке, а ВЛАДЕЛЕЦ ДОЧКИ сам решает поделиться записью. Всё opt-in, всё рукой владельца,
сети нет вообще — ни запроса, ни токена (ровно как у канала `findings/from-children`, см.
`engops/kit_feedback.py` — это шаблон канала доставки).

ТРИ СЛОЯ, ВСЕ ДОБРОВОЛЬНЫЕ:

1. ОТМЕТКА О ПОДКЛЮЧЕНИИ (регистрация). При онбординге кит ОДИН РАЗ предлагает отметиться. Явное
   «да» (`reach register`) -> запись-регистрация: имя проекта + версия кита + дата + стабильный
   анонимный id проекта. НИКОГДА не включаем абсолютные пути, содержимое кода, e-mail. «Нет»
   (`reach decline`) или пропуск -> ничего не создаётся, онбординг проходит полностью, гейты
   зелёные. Решение записывается (decision.yaml) -> повторный онбординг не переспрашивает
   (идемпотентность). Согласие отзывается удалением записи (`reach forget`).

2. СВОДКА ИСПОЛЬЗОВАНИЯ (второй opt-in). По явной команде (`reach summary`) кит собирает ЛОКАЛЬНО
   сводку: имя проекта + число прогонов + типы задач + версия кита. Показывает человеку целиком до
   передачи. Отдаёт только сам владелец — автоматической отправки нет.

3. ПРОДУКТОВЫЙ СТАТУС из Product Passport (третий слой). В сводку включается СНИМОК продуктового
   статуса, взятый из УЖЕ существующего артефакта Product Passport (`planning/passport_generator`).
   Статус НЕ собирается заново и НЕ выдумывается: отметки достоверности verified/inferred/unknown
   переносятся как есть; паспорт с пробелом -> в сводке пробел, а не правдоподобное значение.
   Пассаж от passport-слоя (planning) сюда впрыскивает вызывающий (CLI) — как здоровье в contract:
   engops не тянет planning, а получает готовые разделы параметром.

ЧЕГО ЗДЕСЬ НЕТ НАМЕРЕННО (границы работы): любая автоматическая отправка, сетевые запросы, фоновые
heartbeat; принудительная регистрация или регистрация по умолчанию; идентификация человека/машины
сверх того, что владелец добавил сам.

ГДЕ ЧТО ЛЕЖИТ:
  дочка: `.ai/reach/decision.yaml`       — решение владельца (registered|declined) для идемпотентности;
         `.ai/reach/registration.yaml`   — сама запись-регистрация (создаётся только на «да»);
  кит:   `registry/child-registrations/<id>.yaml` — собранные регистрации (реестр = источник истины
         охвата: счёт выводится из НАБОРА файлов, а не из отдельного самодекларированного числа).

CLI (в дочке):  child_registry.py register <child_root>
                child_registry.py decline  <child_root>
                child_registry.py forget   <child_root>
                child_registry.py status   <child_root>
                child_registry.py summary  <child_root> [--json]
       (в ките)  child_registry.py coverage <kit_root>
       (в ките)  child_registry.py collect  <kit_root> <child_root> [<child_root> ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402,F401
from ai_ops_kit.shared.gitio import git  # noqa: E402

# ЧЕСТНАЯ CAPABILITY-ДЕКЛАРАЦИЯ. Модуль объявляет РОВНО то, что делает: читает и пишет локальные
# файлы дочки/кита, и НЕ звонит домой. Это не украшение — соблюдение проверяется тестом
# (`tests/unit/test_child_registry.py::test_module_declares_no_network_and_keeps_the_promise`),
# который читает эту декларацию и сверяет её с исходником: `network: False` обязан подтверждаться
# отсутствием сетевых импортов/вызовов в коде. Так «не звонит домой» перестаёт быть обещанием на
# словах и становится проверяемым фактом.
CAPABILITIES = {
    "network": False,          # ни запроса, ни сокета, ни токена — сборка и доставка локальны
    "reads": ["local_files"],  # features/*, VERSION, .ai/reach/*, registry/child-registrations/*
    "writes": ["local_files"], # .ai/reach/*, registry/child-registrations/*
    "sends": False,            # никакой автоматической отправки — отдаёт только владелец руками
}

# Решения владельца. `registered` ставит явное «да», `declined` — явное «нет»/пропуск. Оба делают
# онбординг идемпотентным: пока решения НЕТ, кит вправе предложить снова; как только оно есть — молчит.
DECISIONS = ("registered", "declined")

CHILD_DIR = Path(".ai") / "reach"
DECISION_FILE = "decision.yaml"
REGISTRATION_FILE = "registration.yaml"
SUMMARY_FILE = "summary.yaml"
# Реестр охвата в ките — источник истины: счёт выводится из набора этих файлов.
KIT_DIR = Path("registry") / "child-registrations"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today():
    return _now()[:10]


def _kit_version(root):
    """Версия кита из репозитория. Порядок как у kit_feedback: VERSION дочки, затем поставка."""
    for p in (Path(root) / "VERSION", Path(root) / ".ai" / "managed" / "VERSION"):
        try:
            return p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return None


def _project_name(root):
    """Имя проекта — имя каталога. resolve() обязателен: `Path('.').name` пусто (та же ловушка,
    что в passport_generator._repo_name). Имя — ЕДИНСТВЕННОЕ, что владелец делит по умолчанию;
    абсолютный путь при этом НЕ берём — он выдал бы раскладку машины."""
    return Path(root).resolve().name


def project_anon_id(child_root):
    """Стабильный АНОНИМНЫЙ id проекта. -> строка или None (тогда id генерирует вызывающий).

    Берём хэш ПЕРВОГО коммита репозитория: он стабилен на всю жизнь репозитория, одинаков на любой
    машине с этим репозиторием и НЕ раскрывает ни путь, ни имя, ни автора. Это лучше случайного id:
    переустановил кит, отозвал и снова отметился — id тот же, дубля в охвате не будет. Нет git
    (или нет коммитов) -> None: тогда register() кладёт разово сгенерированный id и хранит его,
    чтобы он был стабилен между запусками.
    """
    try:
        rc, out, _ = git(child_root, "rev-list", "--max-parents=0", "HEAD", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if rc != 0 or not out:
        return None
    first = out.splitlines()[0].strip()
    if not first:
        return None
    return "proj-" + hashlib.sha256(first.encode("utf-8")).hexdigest()[:16]


def decision_path(child_root):
    return Path(child_root) / CHILD_DIR / DECISION_FILE


def registration_path(child_root):
    return Path(child_root) / CHILD_DIR / REGISTRATION_FILE


def summary_path(child_root):
    return Path(child_root) / CHILD_DIR / SUMMARY_FILE


def kit_path(kit_root, reg_id):
    return Path(kit_root) / KIT_DIR / f"{reg_id}.yaml"


def kit_summary_path(kit_root, reg_id):
    return Path(kit_root) / KIT_DIR / f"{reg_id}.summary.yaml"


def _dump(path, doc):
    import yaml
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _load(path):
    """(документ, ошибка). «Файла нет» и «файл не разобран» — РАЗНЫЕ факты (как в kit_feedback._load)."""
    path = Path(path)
    if not path.is_file():
        return None, None  # отсутствие — не ошибка: решения/регистрации может просто не быть
    try:
        import yaml
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 — файл есть, но не разобран: молчать нельзя
        return None, f"{path.name}: не разобран ({type(exc).__name__}: {exc})"[:200]
    if not isinstance(doc, dict):
        return None, f"{path.name}: не объект"
    return doc, None


def read_decision(child_root):
    """Решение владельца или None. Ошибку разбора не глушим — возвращаем как есть во втором поле."""
    return _load(decision_path(child_root))


def has_decided(child_root):
    """Принято ли уже решение (для идемпотентности онбординга). Битый файл -> True: переспрашивать
    поверх повреждённого решения хуже, чем промолчать; починку покажет `reach status`."""
    doc, err = read_decision(child_root)
    if err:
        return True
    return bool(doc and doc.get("decision") in DECISIONS)


def read_registration(child_root):
    """Запись-регистрация дочки или None (+ ошибка разбора)."""
    return _load(registration_path(child_root))


# ── СЛОЙ 1: отметка о подключении ────────────────────────────────────────────────────────────────

def build_registration(child_root, *, anon_id=None, at=None):
    """Собрать ChildRegistration (без записи). Состав УЗОК намеренно (инвариант приватности).

    ВНУТРИ РОВНО ЧЕТЫРЕ ФАКТА: имя проекта, версия кита, дата, анонимный id. Ни абсолютного пути, ни
    e-mail, ни содержимого кода здесь нет и быть не может — в отличие от kit_feedback, который несёт
    `child.path` и вывод команд (там наблюдение о дефекте, здесь — только «я подключён»). Проверяется
    тестом на СОСТАВ записи.
    """
    return {
        "schema_version": 1,
        "kind": "ChildRegistration",
        "id": anon_id or project_anon_id(child_root) or ("proj-" + uuid.uuid4().hex[:16]),
        "project": _project_name(child_root),
        "kit_version": _kit_version(child_root),
        "registered_at": at or _today(),
        "state": "new",
    }


def register(child_root):
    """Явное «да»: записать решение и создать запись-регистрацию. -> (path, created, record).

    Идемпотентно: повторный `register` НЕ плодит вторую запись и сохраняет прежний анонимный id
    (стабильность охвата). Согласие по умолчанию НЕ выбрано — запись появляется ТОЛЬКО здесь, по
    явной команде владельца.
    """
    existing, _err = read_registration(child_root)
    anon_id = (existing or {}).get("id")
    rec = build_registration(child_root, anon_id=anon_id)
    created = existing is None
    _dump(registration_path(child_root), rec)
    _dump(decision_path(child_root),
          {"schema_version": 1, "kind": "ReachDecision", "decision": "registered",
           "decided_at": _now(), "project_id": rec["id"]})
    return registration_path(child_root), created, rec


def decline(child_root):
    """Явное «нет»/пропуск: записать решение, НИЧЕГО не создавать. -> path решения.

    Существующую регистрацию (если владелец передумал) убираем — «нет» отзывает и запись тоже.
    Онбординг после этого проходит полностью и молча: ни один гейт от отказа не краснеет.
    """
    rp = registration_path(child_root)
    if rp.is_file():
        rp.unlink()
    return _dump(decision_path(child_root),
                 {"schema_version": 1, "kind": "ReachDecision", "decision": "declined",
                  "decided_at": _now()})


def forget(child_root):
    """Отозвать согласие: удалить запись-регистрацию И решение. -> список удалённого.

    После forget кит снова считает решение непринятым — онбординг вправе предложить отметиться
    заново. Это и есть отзыв согласия «удалением записи».
    """
    removed = []
    for p in (registration_path(child_root), decision_path(child_root)):
        if p.is_file():
            p.unlink()
            removed.append(str(p))
    return removed


def registration_state(child_root):
    """Текущее состояние отметки этой дочки для человека. -> отчёт (только чтение)."""
    dec, dec_err = read_decision(child_root)
    reg, reg_err = read_registration(child_root)
    errors = [e for e in (dec_err, reg_err) if e]
    return {
        "schema_version": 1, "kind": "ReachState",
        "project": _project_name(child_root),
        "decided": bool(dec and dec.get("decision") in DECISIONS),
        "decision": (dec or {}).get("decision"),
        "registered": bool(reg),
        "registration": reg,
        "errors": errors,
    }


# ── СЛОЙ 3: снимок продуктового статуса из Product Passport ──────────────────────────────────────

# Разделы паспорта, которые несут продуктовый статус (версия/релизы/стек/CI/тесты/зрелость). Ключи —
# подстроки заголовков из passport_generator.sections(); значение — короткое имя поля в снимке.
_PASSPORT_SECTIONS = (
    ("версия и последний релиз", "version_release"),
    ("repository и окружения", "stack_and_env"),
    ("статус и зрелость", "maturity"),
    ("здоровье", "health"),
)


def product_status_snapshot(sections):
    """Снимок продуктового статуса ИЗ разделов паспорта. -> {поле: {state, value}}.

    НЕ собирает статус заново и НЕ выдумывает: берёт из уже готовых разделов
    (`passport_generator.sections`) отметку достоверности как есть. Паспорт с пробелом (`unknown`)
    даёт в снимке `unknown` — правдоподобного значения на его месте не появляется. Раздела нет вовсе
    -> тоже `unknown` (а не пропуск): «не знаю» называется, а не замалчивается.
    """
    sections = sections or {}
    # заголовок(lower) -> данные раздела; сопоставление по подстроке, как в passport_generator._milestone
    lower = {str(k).strip().lower(): v for k, v in sections.items() if isinstance(v, dict)}
    out = {}
    for needle, field in _PASSPORT_SECTIONS:
        data = next((v for h, v in lower.items() if needle in h), None)
        if data is None:
            out[field] = {"state": "unknown", "value": None}
            continue
        # Переносим state и value КАК ЕСТЬ — доверие паспорта не переоцениваем и не занижаем.
        out[field] = {"state": data.get("state", "unknown"), "value": data.get("value")}
    return out


# ── СЛОЙ 2: локальная сводка использования ───────────────────────────────────────────────────────

def scan_usage(child_root):
    """Число прогонов и типы задач ЛОКАЛЬНО, из фич дочки. -> {runs, features, task_types}.

    Прогоны берём из того же приёмника, что и work_view: событийный журнал
    `features/<id>/lifecycle-journal.jsonl` (одна попытка = один run_start), с откатом на
    `run-report.json`. Тип задачи — из `run-plan.yaml -> base_workflow` или `workitem.yaml ->
    task_type`. Нет фич -> нули и пустые типы (честно, а не выдуманное «активно используется»).
    """
    import yaml
    root = Path(child_root)
    fdir = root / "features"
    runs = 0
    features = 0
    task_types = {}
    if not fdir.is_dir():
        return {"runs": 0, "features": 0, "task_types": {}}
    for fd in sorted(p for p in fdir.iterdir() if p.is_dir()):
        features += 1
        # прогоны: считаем попытки по run_start в журнале; иначе одна запись из run-report.json
        journal = fd / "lifecycle-journal.jsonl"
        counted = 0
        if journal.is_file():
            try:
                for ln in journal.read_text(encoding="utf-8").splitlines():
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        ev = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(ev, dict) and ev.get("kind") == "run_start":
                        counted += 1
            except OSError:
                counted = 0
        if counted == 0 and (fd / "run-report.json").is_file():
            counted = 1
        runs += counted
        # тип задачи: run-plan.yaml -> base_workflow, иначе workitem.yaml -> task_type
        tt = None
        for rel, key in (("run-plan.yaml", "base_workflow"), ("workitem.yaml", "task_type")):
            p = fd / rel
            if not p.is_file():
                continue
            try:
                doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except (yaml.YAMLError, OSError):
                continue
            if isinstance(doc, dict) and doc.get(key):
                tt = str(doc.get(key))
                break
        if tt:
            task_types[tt] = task_types.get(tt, 0) + 1
    return {"runs": runs, "features": features, "task_types": task_types}


def build_summary(child_root, product_status=None):
    """Собрать ChildUsageSummary (второй opt-in). -> отчёт для показа человеку ДО передачи.

    product_status — снимок из Product Passport, впрыснутый вызывающим (`product_status_snapshot`).
    Не передан -> честный `unknown` по всем полям, а не пропуск. Сводка ничего не отправляет: её
    показывает `reach summary` и отдаёт только сам владелец.
    """
    usage = scan_usage(child_root)
    reg, _err = read_registration(child_root)
    return {
        "schema_version": 1, "kind": "ChildUsageSummary", "at": _now(),
        # id связывает сводку с регистрацией при агрегации охвата (тот же анонимный id).
        "id": (reg or {}).get("id") or project_anon_id(child_root),
        "project": _project_name(child_root),
        "kit_version": _kit_version(child_root),
        "registered": bool(reg),
        "runs": usage["runs"], "features": usage["features"],
        "task_types": usage["task_types"],
        "product_status": product_status if product_status is not None
        else product_status_snapshot(None),
    }


def write_summary(child_root, product_status=None):
    """Собрать сводку И сохранить её в `.ai/reach/summary.yaml`, чтобы владелец мог поделиться файлом.
    -> (path, rep). Файл — на диске владельца; кит его никуда не отправляет (доставка — руками)."""
    rep = build_summary(child_root, product_status=product_status)
    return _dump(summary_path(child_root), rep), rep


# ── СЛОЙ охвата (в ките): доставка и счёт ────────────────────────────────────────────────────────

def collect(kit_root, child_roots, *, dry_run=False):
    """Собрать регистрации дочек в реестр кита. -> отчёт. Мирроринг kit_feedback.collect.

    Локально, с машины владельца кита: сети нет. Берёт `.ai/reach/registration.yaml` каждой дочки
    (только если владелец дочки её создал — отказ дочки нечего собирать) и кладёт копию в
    `registry/child-registrations/<id>.yaml`. `--dry-run` показывает, что будет записано, до записи.
    """
    kit_root = Path(kit_root)
    report = {"schema_version": 1, "kind": "ReachCollect", "at": _now(),
              "kit_root": str(kit_root.resolve()), "dry_run": bool(dry_run),
              "collected": [], "skipped": [], "errors": []}
    for cr in child_roots:
        cr = Path(cr)
        reg, err = read_registration(cr)
        if err:
            report["errors"].append(f"{cr.name}: {err}")
            continue
        if not reg:
            # Не ошибка: дочка не отметилась (отказ или ещё не решала) — собирать нечего.
            report["skipped"].append({"child": cr.name, "reason": "не отмечена"})
            continue
        landed = dict(reg, state="collected",
                      collected_from={"project": reg.get("project"), "at": _now()})
        # Сводка (слой 2) — отдельный opt-in: собираем её ТОЛЬКО если владелец дочки её сохранил.
        summ, summ_err = _load(summary_path(cr))
        if summ_err:
            report["errors"].append(f"{cr.name}: {summ_err}")
        if dry_run:
            report["collected"].append({"id": reg["id"], "project": reg.get("project"),
                                        "written": None, "with_summary": bool(summ)})
            continue
        kp = _dump(kit_path(kit_root, reg["id"]), landed)
        _dump(registration_path(cr), landed)  # состояние возвращается в дочку (двусторонность канала)
        if summ:
            _dump(kit_summary_path(kit_root, reg["id"]), summ)
        report["collected"].append({"id": reg["id"], "project": reg.get("project"),
                                    "written": str(kp), "with_summary": bool(summ)})
    return report


def load_registrations(kit_root):
    """(регистрации, ошибки) из реестра кита. Счёт охвата выводится ОТСЮДА — из набора файлов.

    Файлы сводок (`*.summary.yaml`) сюда НЕ попадают: они читаются отдельно (`load_summaries`), а
    счёт репозиториев считается по регистрациям, а не по сводкам.
    """
    d = Path(kit_root) / KIT_DIR
    out, errors = [], []
    if not d.is_dir():
        return out, errors
    for f in sorted(d.glob("*.yaml")):
        if f.name.endswith(".summary.yaml"):
            continue
        doc, err = _load(f)
        if err:
            errors.append(err)
        elif doc and doc.get("kind") == "ChildRegistration":
            out.append(doc)
        else:
            errors.append(f"{f.name}: не ChildRegistration")
    return out, errors


def load_summaries(kit_root):
    """Собранные сводки по анонимному id проекта. -> {id: ChildUsageSummary}."""
    d = Path(kit_root) / KIT_DIR
    out = {}
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.summary.yaml")):
        doc, err = _load(f)
        if not err and doc and doc.get("kind") == "ChildUsageSummary":
            out[doc.get("id")] = doc
    return out


def coverage(kit_root):
    """Охват = число отметившихся репозиториев, выведенное из НАБОРА файлов реестра. -> отчёт.

    ОБЯЗАТЕЛЬНАЯ ОГОВОРКА: это НИЖНЯЯ ГРАНИЦА. Отметились не все (механизм добровольный), поэтому
    число — не полный список пользователей, а «сколько согласилось попасть в счёт». Выдать его за
    полный охват значило бы соврать тем же способом, каким врёт телеметрия, только в другую сторону.
    """
    regs, errors = load_registrations(kit_root)
    summaries = load_summaries(kit_root)  # агрегируем сводки (слой 2), если владельцы ими поделились
    by_version = {}
    by_maturity = {}
    total_runs = 0
    projects = []
    for r in regs:
        ver = r.get("kit_version") or "неизвестна"
        by_version[ver] = by_version.get(ver, 0) + 1
        summ = summaries.get(r.get("id"))
        # Продуктовый статус берём из СВОДКИ (регистрация его не несёт — приватность). Нет сводки или
        # в ней пробел -> «неизвестен», а не выдуманный класс.
        mat = None
        if summ:
            ps = summ.get("product_status") or {}
            m = (ps.get("maturity") or {}) if isinstance(ps, dict) else {}
            if m.get("state") != "unknown":
                mat = m.get("value")
            total_runs += int(summ.get("runs") or 0)
        key = str(mat) if mat else "неизвестен"
        by_maturity[key] = by_maturity.get(key, 0) + 1
        projects.append({"id": r.get("id"), "project": r.get("project"),
                         "kit_version": r.get("kit_version"),
                         "runs": (summ or {}).get("runs") if summ else None,
                         "has_summary": bool(summ)})
    return {
        "schema_version": 1, "kind": "ReachCoverage", "at": _now(),
        "count": len(regs),
        "summaries_shared": len(summaries),
        "total_runs": total_runs,
        "by_version": by_version,
        "by_maturity": by_maturity,
        "projects": projects,
        "is_lower_bound": True,
        "note": "нижняя граница: отметились не все (регистрация добровольная) — это не полный "
                "список пользователей, а сколько репозиториев согласилось попасть в счёт",
        "errors": errors,
    }


# ── Рендеры для человека ─────────────────────────────────────────────────────────────────────────

def render_state(rep):
    if rep.get("errors"):
        return ("REACH " + rep["project"] + ": состояние отметки не прочитать — "
                + "; ".join(rep["errors"]))
    if not rep["decided"]:
        return (f"REACH {rep['project']}: решение об отметке ещё не принято. "
                "Отметиться: reach register · отказаться: reach decline")
    if rep["decision"] == "registered":
        rid = (rep.get("registration") or {}).get("id")
        return f"REACH {rep['project']}: отмечен (id {rid}). Отозвать: reach forget"
    return f"REACH {rep['project']}: отметка отклонена. Передумать: reach register"


def render_summary(rep):
    L = [f"СВОДКА ИСПОЛЬЗОВАНИЯ — {rep['project']} (версия кита {rep.get('kit_version') or 'н/д'})",
         f"  прогонов: {rep['runs']} · фич: {rep['features']}"]
    tt = rep.get("task_types") or {}
    L.append("  типы задач: " + (", ".join(f"{k}×{v}" for k, v in sorted(tt.items())) or "нет данных"))
    L.append("  продуктовый статус (из Product Passport, достоверность как в паспорте):")
    for field, data in (rep.get("product_status") or {}).items():
        st = data.get("state", "unknown")
        val = data.get("value")
        shown = "_неизвестно_" if (st == "unknown" or val is None) else str(val)[:80]
        L.append(f"    · {field} [{st}]: {shown}")
    L.append("  Это ЛОКАЛЬНАЯ сводка. Кит её никуда не отправляет — поделиться можешь только ты сам, "
             "передав файл владельцу кита.")
    return "\n".join(L)


def render_coverage(rep):
    L = [f"ОХВАТ: {rep['count']} отметившихся репозиториев (НИЖНЯЯ ГРАНИЦА)."]
    L.append(f"  {rep['note']}.")
    if rep.get("summaries_shared"):
        L.append(f"  сводками поделились: {rep['summaries_shared']} · всего прогонов: {rep['total_runs']}")
    if rep["by_version"]:
        L.append("  по версиям кита: "
                 + ", ".join(f"{k}×{v}" for k, v in sorted(rep["by_version"].items())))
    if rep["by_maturity"]:
        L.append("  по классу зрелости: "
                 + ", ".join(f"{k}×{v}" for k, v in sorted(rep["by_maturity"].items())))
    for p in rep["projects"]:
        L.append(f"  · {p.get('project') or p.get('id')} (версия {p.get('kit_version') or 'н/д'})")
    for e in rep["errors"]:
        L.append(f"  ✗ {e}")
    return "\n".join(L)


def render_collect(rep):
    L = [f"COLLECT{' (сухой прогон)' if rep['dry_run'] else ''}: собрано {len(rep['collected'])}, "
         f"пропущено {len(rep['skipped'])}, ошибок {len(rep['errors'])}"]
    for c in rep["collected"]:
        L.append(f"  → {c['id']} ({c.get('project') or '—'})")
    for s in rep["skipped"]:
        L.append(f"  · пропущено {s['child']}: {s['reason']}")
    for e in rep["errors"]:
        L.append(f"  ✗ {e}")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="child_registry.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, help_ in (("register", "отметиться (явное согласие; в дочке)"),
                        ("decline", "отказаться от отметки (в дочке)"),
                        ("forget", "отозвать согласие: удалить отметку (в дочке)"),
                        ("status", "состояние отметки этой дочки"),
                        ("summary", "локальная сводка использования (в дочке)"),
                        ("coverage", "охват по реестру регистраций (в ките)")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("root")
        p.add_argument("--json", action="store_true")

    c = sub.add_parser("collect", help="собрать регистрации дочек в кит (в ките)")
    c.add_argument("kit_root")
    c.add_argument("child_roots", nargs="+")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--json", action="store_true")

    a = ap.parse_args(argv)

    if a.cmd == "register":
        p, created, rec = register(a.root)
        if a.json:
            print(json.dumps({"path": str(p), "created": created, "registration": rec},
                             ensure_ascii=False, indent=2))
        else:
            print(f"REACH: {'отмечен' if created else 'уже отмечен'} — {rec['project']} "
                  f"(id {rec['id']}, версия кита {rec.get('kit_version') or 'н/д'})")
        return 0

    if a.cmd == "decline":
        p = decline(a.root)
        print(json.dumps({"path": str(p), "decision": "declined"}, ensure_ascii=False, indent=2)
              if a.json else "REACH: отметка отклонена — ничего не создано, онбординг не затронут")
        return 0

    if a.cmd == "forget":
        removed = forget(a.root)
        print(json.dumps({"removed": removed}, ensure_ascii=False, indent=2)
              if a.json else f"REACH: согласие отозвано, удалено записей: {len(removed)}")
        return 0

    if a.cmd == "status":
        rep = registration_state(a.root)
        print(json.dumps(rep, ensure_ascii=False, indent=2) if a.json else render_state(rep))
        return 1 if rep["errors"] else 0

    if a.cmd == "summary":
        # Модульный CLI без passport (его впрыскивает интент CLI): product_status -> unknown честно.
        p, rep = write_summary(a.root)
        if a.json:
            print(json.dumps(dict(rep, saved_to=str(p)), ensure_ascii=False, indent=2))
        else:
            print(render_summary(rep))
            print(f"  Сохранено: {p} — поделиться можно, передав этот файл владельцу кита.")
        return 0

    if a.cmd == "coverage":
        rep = coverage(a.root)
        print(json.dumps(rep, ensure_ascii=False, indent=2) if a.json else render_coverage(rep))
        return 1 if rep["errors"] else 0

    if a.cmd == "collect":
        rep = collect(a.kit_root, a.child_roots, dry_run=a.dry_run)
        print(json.dumps(rep, ensure_ascii=False, indent=2) if a.json else render_collect(rep))
        return 1 if rep["errors"] else 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
