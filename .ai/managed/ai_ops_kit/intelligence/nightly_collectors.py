#!/usr/bin/env python3
"""Сбор сигналов для ночного дельта-обзора (read-only).

Замкнутый кластер, вынесенный из `nightly_review.py`: две группы читателей репозитория,
которые оркестрирует `collect_delta`, — и обе ТОЛЬКО ЧИТАЮТ, ничего не пишут.

  · git/репозиторий: коммиты, изменённые файлы, статус плана, наличие CI-workflow, открытые PR;
  · проверки (CHECKS): прогон шипнутых валидаторов процессом и сбор их ответов (`run_checks`).

Модуль импортируется САМИМ `nightly_review` (не-тестовый импортёр): вся аналитика собирается здесь,
а бриф и оркестрация остаются в `nightly_review`.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

import yaml
from datetime import datetime, timedelta
from pathlib import Path


def _git(root: Path, *args) -> tuple[int, str, str]:
    """Git command wrapper."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode, result.stdout, result.stderr
    # Узкий тип (фаза 0, 19.08.2026): запуск может не состояться (нет бинаря, права, битый
    # симлинк) или не уложиться в timeout. Любое ДРУГОЕ исключение здесь — дефект вызова, и он
    # обязан всплыть, а не превратиться в «rc=1» и молча стать «команда не сработала».
    # Тип ошибки НАЗЫВАЕТСЯ в тексте: «не смогли запустить» и «команда вернула ошибку» —
    # разные ответы, и по голому str(e) их не различить.
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", f"{type(e).__name__}: {e}"


def _get_recent_commits(root: Path, since: str | None = None) -> list[dict]:
    """Get commits since last review (or last 24h)."""
    if since:
        range_spec = f"{since}..HEAD"
    else:
        # Last 24 hours
        since_time = (datetime.now() - timedelta(hours=24)).isoformat()
        range_spec = f"--since={since_time}"

    rc, out, _ = _git(root, "log", range_spec, "--pretty=format:%H|%s|%an|%ai", "--no-merges")
    if rc != 0 or not out.strip():
        return []

    commits = []
    for line in out.strip().split("\n"):
        parts = line.split("|", 3)
        if len(parts) == 4:
            commits.append({
                "sha": parts[0][:8],
                "message": parts[1],
                "author": parts[2],
                "date": parts[3],
            })
    return commits


def _get_changed_files(root: Path, since: str | None = None) -> list[str]:
    """Get list of changed files since last review."""
    if since:
        range_spec = f"{since}..HEAD"
    else:
        since_time = (datetime.now() - timedelta(hours=24)).isoformat()
        # Get files from commits in last 24h
        rc, out, _ = _git(root, "log", f"--since={since_time}", "--name-only", "--pretty=format:")
        if rc != 0:
            return []
        files = set()
        for line in out.strip().split("\n"):
            line = line.strip()
            if line and not line.startswith("|"):
                files.add(line)
        return sorted(files)

    rc, out, _ = _git(root, "diff", range_spec, "--name-only")
    if rc != 0:
        return []
    return [f for f in out.strip().split("\n") if f]


def _check_plan_status(root: Path) -> dict:
    """Check plan.yaml for status changes."""
    plan_path = root / "planning" / "plan.yaml"
    if not plan_path.exists():
        return {"exists": False}

    try:
        with open(plan_path, encoding="utf-8") as f:
            plan = yaml.safe_load(f)
        work = plan.get("work", [])
        by_status = {}
        for w in work:
            s = w.get("status", "unknown")
            by_status[s] = by_status.get(s, 0) + 1
        return {"exists": True, "total": len(work), "by_status": by_status}
    # Узкий тип: файл может не читаться, YAML — не разбираться, а пустой документ даёт None и
    # падает на `.get`. Причина НАЗЫВАЕТСЯ: «план не прочитали» и «работ нет» — разные ответы,
    # и обзор, который их путает, отчитается о тишине там, где была поломка.
    except (OSError, yaml.YAMLError, AttributeError) as e:
        return {"exists": True, "error": f"план не разобран ({type(e).__name__}: {e})"}


def _check_ci_status(root: Path) -> dict:
    """Check if CI workflows exist (actual status requires GitHub API)."""
    workflows_dir = root / ".github" / "workflows"
    if not workflows_dir.exists():
        return {"workflows": 0}
    workflows = list(workflows_dir.glob("*.yml")) + list(workflows_dir.glob("*.yaml"))
    return {"workflows": len(workflows)}


def _check_open_prs(root: Path) -> dict:
    """Check for open PRs (requires gh CLI)."""
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--state", "open", "--json", "number,title"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            prs = json.loads(result.stdout)
            return {"open_prs": len(prs), "prs": prs[:5]}  # First 5
        return {"open_prs": None, "unavailable": f"gh вернул код {result.returncode}"}
    # Узкий тип: gh может отсутствовать, не уложиться в timeout или отдать не-JSON.
    # Здесь стоял `pass`, и причина исчезала совсем; отсутствие данных выглядело так же, как
    # «открытых PR нет». `None` вместо `-1` — тот же инвариант, что и у usage: unavailable не
    # число и не ноль, а отдельное состояние, и оно названо в `unavailable`.
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return {"open_prs": None, "unavailable": f"{type(e).__name__}: {e}"}


# НАХОДКИ, А НЕ КОЛИЧЕСТВА (v0, 20.08.2026).
#
# Скелет обзора считал коммиты и файлы. «5 коммитов, 12 файлов» не расхождение: человеку нечего с
# этим делать, и бриф из таких строк перестают читать через неделю. Работа обещает НАХОДИТЬ
# расхождения — с документацией, тестами, архитектурой, Storybook, планом.
#
# СВОЮ АНАЛИТИКУ НЕ ПИШЕМ. В поставку дочки уже едут 24 валидатора, каждый из которых умеет
# отвечать на свой вопрос. Обзор — АГРЕГАТОР: он запускает их процессом (так же, как CI дочки) и
# собирает ответы. Писать вторую реализацию тех же проверок значило бы завести вторую правду —
# ровно то, что кит запрещает везде.
#
# ЧЕГО НЕ СМОГЛИ — НАЗЫВАЕТСЯ. Валидатор, которого нет в поставке или который не запустился,
# даёт `unknown`, а не «нарушений нет». Третье состояние не сворачивается во второе.
# КАК ЗВАТЬ КАЖДЫЙ — ОБЪЯВЛЕНО, А НЕ УГАДАНО (замер 20.08.2026).
#
# Первая редакция звала все валидаторы одинаково — путём к корню. Пять из восьми ответили
# `IsADirectoryError` или подсказкой по использованию, и обзор отчитался о них как о РАСХОЖДЕНИЯХ.
# То есть он выдал СВОЮ ошибку вызова за дефект продукта — худшее, что может сделать проверка:
# человек пошёл бы чинить то, что не сломано, а настоящие находки утонули бы в шуме.
#
# Способ вызова замерен по каждому:
#   root     — принимает корень репозитория;
#   none     — без аргумента проверяет пакет целиком;
#   artifact — принимает путь к КОНКРЕТНОМУ артефакту; нет артефакта -> «не проверено», НЕ находка.
CHECKS = (
    {"title": "документация", "name": "validate_freshness", "how": "root",
     "subject": "документы, у которых истёк срок ревизии"},
    {"title": "ссылки", "name": "validate_references", "how": "root",
     "subject": "ссылки, ведущие в никуда"},
    {"title": "артефакты", "name": "validate_cross_artifacts", "how": "root",
     "subject": "связность артефактов между собой"},
    {"title": "заявления", "name": "validate_claims", "how": "none",
     "subject": "публичные числа против кода"},
    # РОД ДОКУМЕНТА ОБЪЯВЛЕН, И ЭТО НЕ ПЕДАНТИЗМ (замер 20.08.2026). Здесь стояло
    # `planning/plan.yaml` — и `validate_plan_artifact` честно ответил «kind должен быть
    # plan-artifact», потому что проверяет RunPlan ФИЧИ, а не delivery-план репозитория.
    # Обзор выдал этот ответ за РАСХОЖДЕНИЕ и трижды сообщил владельцу о дефекте, которого нет.
    # Ошибка вызова второго рода: файл существует, валидатор запускается — и проверяет не то.
    # Поэтому род документа сверяется ДО запуска: не совпал — «не проверено», а не находка.
    {"title": "план работы", "name": "validate_plan_artifact", "how": "artifact",
     "artifact": "features/*/plan.yaml", "kind": "plan-artifact",
     "subject": "RunPlan фичи и его связность"},
    {"title": "события", "name": "validate_event_catalog", "how": "artifact",
     "artifact": "analytics/events.yaml", "kind": None,
     "subject": "каталог событий аналитики"},
)


def _validation_dir(root: Path) -> Path:
    """Где лежат валидаторы: в дочке — поставка, в самом ките — свой каталог."""
    shipped = Path(root) / ".ai" / "managed" / "ai_ops_kit" / "validation"
    return shipped if shipped.is_dir() else Path(root) / "ai_ops_kit" / "validation"


def _artifact_kind(path: Path) -> str | None:
    """Род документа из его же поля `kind`. -> str | None (не прочитали).

    Нужен, чтобы не звать валидатор на документе другого рода: он честно ответит «не то», а обзор
    выдаст этот ответ за расхождение продукта. Ровно так 20.08 родилась ложная находка про
    `write_scope`, о которой владельцу сообщили трижды.
    """
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return str(doc.get("kind")) if isinstance(doc, dict) and doc.get("kind") else None


def run_checks(root: Path, timeout: int = 120) -> list[dict]:
    """Прогнать шипнутые валидаторы и собрать их ответы. -> список находок.

    `ok`: True — сошлось, False — расхождение, None — НЕ ПРОВЕРЕНО (валидатора нет в поставке,
    артефакта нет, запуск не состоялся). Третье значение существует намеренно и не сворачивается
    во второе: «не смотрели» и «нарушений нет» — разные ответы, и второй дороже.
    """
    base = _validation_dir(root)
    out = []
    for spec in CHECKS:
        title, name, how = spec["title"], spec["name"], spec["how"]
        rec = {"check": title, "subject": spec["subject"]}
        script = base / f"{name}.py"
        if not script.is_file():
            out.append({**rec, "ok": None, "detail": "валидатор не поставлен — проверить нечем"})
            continue
        if how == "root":
            argv = [str(root)]
        elif how == "none":
            argv = []
        else:
            pattern = spec["artifact"]
            if "*" in pattern:
                found = sorted(Path(root).glob(pattern))
                art = found[0] if found else None
            else:
                art = Path(root) / pattern
                art = art if art.is_file() else None
            if art is None:
                out.append({**rec, "ok": None,
                            "detail": f"артефакта {pattern} нет — проверять нечего"})
                continue
            want = spec.get("kind")
            if want:
                got = _artifact_kind(art)
                if got != want:
                    out.append({**rec, "ok": None,
                                "detail": (f"{art.name}: документ рода '{got or 'неизвестен'}', "
                                           f"а проверка про '{want}' — проверять нечем")})
                    continue
            argv = [str(art)]
        try:
            r = subprocess.run([sys.executable, str(script), *argv],
                               capture_output=True, text=True, timeout=timeout, cwd=str(root))
        except (OSError, subprocess.SubprocessError) as e:
            out.append({**rec, "ok": None,
                        "detail": f"не запустился ({type(e).__name__}: {e})"})
            continue
        full = (r.stdout + r.stderr).strip()
        lines = full.splitlines()
        # ОШИБКА ВЫЗОВА — НЕ НАХОДКА. Трейсбек или подсказка по использованию означают, что мы
        # позвали не так, а не что продукт сломан. Выдать одно за другое — послать человека
        # чинить исправное.
        #
        # ИСКАТЬ ОБЯЗАНО ВО ВСЁМ ВЫВОДЕ, А НЕ В ПОСЛЕДНЕЙ СТРОКЕ (замер 20.08.2026 на трёх живых
        # дочках). Прежде маркер искали в `detail`, а `detail` брал ПОСЛЕДНЮЮ строку. Настоящий
        # отказ валидатора выглядит так:
        #     ОШИБКА: ожидался путь к файлу заявлений, получено '<каталог>' — это каталог.
        #     Использование: validate_claims.py [путь/к/claims.yaml] [--json]
        #     Без аргумента берётся knowledge/claims.yaml пакета.
        # Маркер стоит во ВТОРОЙ строке, а последняя — безобидная подсказка. Защита не срабатывала,
        # и обзор сообщал «расхождение: Без аргумента берётся …» — предложение, из которого человек
        # не поймёт даже, о чём речь. На трёх дочках из трёх это была ПОЛОВИНА всех находок.
        wrong_call = "Traceback" in full or re.search(r"(?i)использование:|usage:", full)
        if wrong_call:
            # Показываем ПЕРВУЮ строку: в отказе по вызову она и есть суть жалобы, а последняя —
            # хвост подсказки. Раньше человек получал именно хвост.
            detail = lines[0][:220] if lines else f"код {r.returncode}, вывод пуст"
            out.append({**rec, "ok": None, "detail": f"позвали неверно — {detail}"})
            continue
        detail = lines[-1][:220] if lines else f"код {r.returncode}, вывод пуст"
        out.append({**rec, "ok": r.returncode == 0, "detail": detail})

    # ПОСТУПЛЕНИЕ СОБЫТИЙ — ОТДЕЛЬНЫЙ ВОПРОС, И ЕГО НЕ ЗАКРЫВАЕТ КАТАЛОГ. `validate_event_catalog`
    # отвечает «что мы обещали слать»; доехало ли хоть одно — не знает никто. Цепочка продукта
    # (Outcome Contract -> Tracking Plan -> реализация -> ПОСТУПЛЕНИЕ -> Product Health) рвётся
    # ровно здесь и рвётся молча: план выглядит выполненным, дашборд пустой.
    from ai_ops_kit.intelligence import event_arrival
    rep = event_arrival.assess(root)
    out.append({
        "check": "поступление событий",
        "subject": "объявленные события доезжают в аналитику",
        "ok": (None if not rep.get("checked") else not rep.get("missing")),
        "detail": event_arrival.render(rep).replace("\n", "; ")[:220],
    })
    return out
