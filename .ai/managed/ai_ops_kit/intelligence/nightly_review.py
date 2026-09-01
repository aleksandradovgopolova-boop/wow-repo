#!/usr/bin/env python3
"""Nightly Product Health Review (v0, read-only).

Собирает delta по изменениям с последнего подтверждённого обзора и формирует утренний бриф.

Граница v0: НИЧЕГО НЕ ПРАВИТ. Только читает и синтезирует.

Структура брифа:
1. Главное одним предложением — что изменилось со вчера
2. Что требует решения (максимум 3 вопроса)
3. Чего система НЕ СТАЛА ДЕЛАТЬ и почему (обязательный раздел — строит доверие)
4. Одна рекомендация дня

Использование:
    nightly_review.py <child_root> [--since COMMIT] [--json]
    nightly_review.py --selftest

Возврат 0 — успех (бриф — данные, решение за людьми).
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


# ТОЧКА ОТСЧЁТА — ПОСЛЕДНИЙ ПОДТВЕРЖДЁННЫЙ ОБЗОР, А НЕ «24 ЧАСА» (v0, 20.08.2026).
#
# Работа обещает «delta по изменениям с последнего ПОДТВЕРЖДЁННОГО обзора». Сутки вместо
# подтверждения — не то же самое: пропущенная ночь молча теряет изменения, а разобранная дважды
# показывает одни и те же находки. Ни то ни другое не заметно человеку — он видит правдоподобный
# бриф в обоих случаях.
#
# Подтверждение — ДЕЙСТВИЕ ЧЕЛОВЕКА (`--confirm`), а не факт отправки брифа: отправленный и
# прочитанный — разные вещи, и точку отсчёта двигает второе.
CONFIRMED_REL = ".ai/project/nightly-review/last-confirmed.json"


def last_confirmed(root: Path) -> dict | None:
    """Последний подтверждённый обзор. -> dict | None (обзора ещё не было).

    Битую запись НЕ считаем отсутствием: «не прочитали» и «не было» — разные ответы, и второй
    молча сдвинул бы точку отсчёта на сутки, потеряв всё, что между.
    """
    p = Path(root) / CONFIRMED_REL
    if not p.is_file():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"unreadable": f"{type(e).__name__}: {e}"}
    return doc if isinstance(doc, dict) else {"unreadable": "не объект"}


def confirm_review(root: Path, sha: str | None = None) -> dict:
    """Отметить обзор разобранным: следующая дельта пойдёт отсюда."""
    rc, out, _ = _git(root, "rev-parse", "HEAD")
    head = sha or (out.strip() if rc == 0 else None)
    rec = {"schema_version": 1, "kind": "NightlyReviewConfirmation",
           "confirmed_at": datetime.now().isoformat(), "commit_sha": head}
    p = Path(root) / CONFIRMED_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return rec


def review_baseline(root: Path) -> dict:
    """Откуда считать дельту. -> {"since": str|None, "kind": ..., "reason": str}.

    `kind`: confirmed — от подтверждённого обзора; fallback — обзора ещё не было, взяли сутки;
    unreadable — запись есть и не читается, и это ТРЕТЬЕ состояние: работаем как fallback, но
    говорим об этом, а не выдаём за первое.
    """
    rec = last_confirmed(root)
    if rec is None:
        return {"since": None, "kind": "fallback",
                "reason": "подтверждённого обзора ещё не было — беру последние сутки"}
    if rec.get("unreadable"):
        return {"since": None, "kind": "unreadable",
                "reason": (f"запись о последнем обзоре не прочитана ({rec['unreadable']}) — "
                           f"беру последние сутки, но точка отсчёта НЕ подтверждена")}
    sha = rec.get("commit_sha")
    if not sha:
        return {"since": None, "kind": "unreadable",
                "reason": "в записи о последнем обзоре нет commit_sha — точка отсчёта неизвестна"}
    return {"since": sha, "kind": "confirmed",
            "reason": f"дельта с последнего подтверждённого обзора ({rec.get('confirmed_at')})"}


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


def collect_delta(root: Path, since: str | None = None) -> dict:
    """Collect all delta information."""
    baseline = review_baseline(root) if since is None else {
        "since": since, "kind": "explicit", "reason": "точка отсчёта задана вызывающим"}
    return {
        "baseline": baseline,
        "commits": _get_recent_commits(root, baseline["since"]),
        "changed_files": _get_changed_files(root, baseline["since"]),
        "plan": _check_plan_status(root),
        "ci": _check_ci_status(root),
        "prs": _check_open_prs(root),
        "findings": run_checks(root),
        "timestamp": datetime.now().isoformat(),
    }


def format_brief(delta: dict, root: Path) -> str:
    """Утренний бриф: ПЯТЬ вопросов в порядке, в котором их задаёт человек.

    Порядок из ROADMAP (`nightly-product-review`): что изменилось → что система сделала → чего НЕ
    стала делать и почему → где нужно решение → что важнее всего сегодня.

    ТРЕТИЙ РАЗДЕЛ — НЕ ВЕЖЛИВОСТЬ, А УСЛОВИЕ ДОВЕРИЯ. Обзор, который перечисляет только найденное,
    неотличим от обзора, который половину не смотрел. Здесь границы названы поимённо: что не
    проверено и почему, и что v0 не делает по решению, а не по недоделке.
    """
    b = delta.get("baseline") or {}
    commits = delta.get("commits", [])
    files = delta.get("changed_files", [])
    findings = delta.get("findings", [])
    bad = [f for f in findings if f.get("ok") is False]
    unknown = [f for f in findings if f.get("ok") is None]
    prs = delta.get("prs", {})
    open_prs = prs.get("open_prs")
    plan = delta.get("plan", {})

    L = ["# Утренний обзор продукта", ""]

    # 1. Что изменилось — и ОТ ЧЕГО считали.
    L += ["## Что изменилось", ""]
    L.append(f"Точка отсчёта: {b.get('reason', 'не названа')}.")
    if b.get("kind") in ("fallback", "unreadable"):
        L.append("**Это не подтверждённая точка отсчёта** — часть изменений могла остаться за кадром "
                 "или попасть в обзор второй раз.")
    if commits:
        L.append(f"С тех пор: {len(commits)} коммит(ов), {len(files)} файл(ов).")
    else:
        L.append("С тех пор изменений не зафиксировано.")

    # 2. Что система сделала — НАХОДКИ, а не количества.
    L += ["", "## Что я проверила", ""]
    if bad:
        L.append(f"Расхождений: **{len(bad)}**.")
        for f in bad:
            L.append(f"- **{f['check']}** ({f['subject']}): {f['detail']}")
    elif findings:
        L.append("Расхождений не найдено ни одной из выполненных проверок.")
    else:
        L.append("Проверки не выполнялись.")
    if isinstance(plan.get("by_status"), dict):
        L.append(f"- план: " + ", ".join(f"{k} — {v}" for k, v in sorted(plan["by_status"].items())))
    elif plan.get("error"):
        L.append(f"- план: {plan['error']}")

    # 3. Чего НЕ стала делать и почему.
    L += ["", "## Чего я не стала делать и почему", ""]
    L.append("- **Ничего не правила**: v0 работает только на чтение. Это граница выпуска, а не "
             "недоделка: автофикс без измеренного false-positive rate — тот же ложный green, "
             "только теперь он коммитит.")
    for f in unknown:
        L.append(f"- **{f['check']}** не проверена: {f['detail']}")
    if open_prs is None:
        L.append(f"- Состояние запросов на слияние не узнала: "
                 f"{prs.get('unavailable', 'причина не названа')}.")
    if not unknown and open_prs is not None:
        L.append("- Остальное из объявленного объёма проверено.")

    # 4. Где нужно решение.
    L += ["", "## Где нужно твоё решение", ""]
    asks = []
    if b.get("kind") == "unreadable":
        asks.append("Запись о последнем обзоре повреждена — подтвердить обзор заново "
                    "(`--confirm`), иначе точка отсчёта останется неизвестной.")
    if open_prs and open_prs > 3:
        asks.append(f"Открытых запросов на слияние: {open_prs} — очередь копится.")
    for f in bad[:3]:
        asks.append(f"«{f['check']}» разошлась с кодом: решить, чинить или признать границей.")
    if asks:
        L += [f"- {a}" for a in asks]
    else:
        L.append("- Ничего не жду. Решений от тебя сейчас не требуется.")

    # 5. Что важнее всего сегодня — ОДНО.
    L += ["", "## Что важнее всего сегодня", ""]
    if bad:
        L.append(f"Разобрать «{bad[0]['check']}»: {bad[0]['detail'][:160]}")
    elif b.get("kind") != "confirmed":
        L.append("Подтвердить этот обзор — тогда завтрашняя дельта будет считаться от него, "
                 "а не от суток.")
    elif unknown:
        L.append(f"Вернуть проверку «{unknown[0]['check']}»: пока она не идёт, её предмет "
                 f"не смотрит никто.")
    elif commits:
        L.append("Спокойная ночь. Проверить, что вчерашние изменения попали в план.")
    else:
        L.append("Спокойная ночь. Можно взять новую работу из плана.")
    return "\n".join(L)


# ─── КЛАСС A: детерминированный автофикс в worktree -> ОДИН черновой PR за ночь ────────────────
#
# Owner decision ep-2026-08-14-nightly-review, четыре класса правок:
#   A — детерминированное, обратимое, не меняющее поведение: можно автоматически, но НИКОГДА в main;
#       worktree -> проверки -> draft PR. B кит готовит, мержит человек. C — только рекомендация.
#       D (секрет/сломанный main/выключенный security-гейт) — срочно, не дожидаясь утра.
#
# КЛАСС A НА СТАРТЕ ПУСТ. Какие ИМЕННО пункты допускаются к автоматической правке — решение
# владельца по одному, и только после измеренной точности v0 (human_decision в карточке работы).
# Поэтому реестр фиксеров существует, но ВКЛЮЧЁННЫЙ список по умолчанию пуст (fail-closed): фиксер
# ездит выключенным, владелец открывает его отдельным решением. Ничего не правится, пока список пуст.
#
# ГРАНИЦЫ (все механизмами, не на словах):
#   · никогда в main — worktree.add отказывает main/master, PR всегда черновой, слияние не делаем;
#   · не более ОДНОГО PR за ночь — одна стабильная ветка + идемпотентный open_draft_pr (обновляет);
#   · ноль вызовов модели — класс A детерминирован (Budget(max_model_calls=0) как заявленный инвариант);
#   · kill-switch — env AI_OPS_NIGHTLY_AUTOFIX=off или файл-сигнал; пустой список — тоже «выключено»;
#   · откат при провале проверки — в worktree.apply_fixes_in_worktree (per-fixer revert / force remove);
#   · unknown не чинится — берём только доказанные расхождения, «не проверено» не трогаем.

NIGHTLY_AUTOFIX_ENV = "AI_OPS_NIGHTLY_AUTOFIX"          # =off (любой регистр) -> глушим
AUTOFIX_OFF_SENTINEL = ".ai/project/nightly-review/autofix-off"


class FixerSpec:
    """Детерминированный фиксер класса A. `apply(worktree_path) -> list[str]` изменённых путей.

    Обязан быть обратимым и не менять поведение. Свой allowlist путей фиксер держит сам — движок
    (worktree.apply_fixes_in_worktree) лишь применяет, проверяет и откатывает."""
    def __init__(self, key: str, description: str, apply):
        self.key = key
        self.description = description
        self.apply = apply


def _fix_trailing_whitespace(globs):
    """Фабрика эталонного фиксера: убрать хвостовые пробелы и добить один финальный перевод строки.

    Класс A в чистом виде — детерминированно, обратимо, не меняет поведение. Трогает ТОЛЬКО файлы по
    своим globs. Возвращает apply(worktree_path) -> list[str] изменённых относительных путей."""
    def apply(wt: Path) -> list:
        wt = Path(wt)
        changed = []
        for pat in globs:
            for p in sorted(wt.glob(pat)):
                if not p.is_file():
                    continue
                try:
                    text = p.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                fixed = "\n".join(line.rstrip() for line in text.split("\n"))
                fixed = fixed.rstrip("\n") + "\n" if fixed.strip() else fixed
                if fixed != text:
                    p.write_text(fixed, encoding="utf-8")
                    changed.append(str(p.relative_to(wt)))
        return changed
    return apply


# Реестр фиксеров класса A. Ключ -> FixerSpec. ВКЛЮЧАЮТСЯ отдельным решением владельца (см. ниже);
# по умолчанию не включён НИ ОДИН. Эталонный фиксер держим на узком allowlist (док-Markdown).
CLASS_A_FIXERS = {
    "trailing-whitespace": FixerSpec(
        "trailing-whitespace",
        "хвостовые пробелы и финальный перевод строки в Markdown-документах",
        _fix_trailing_whitespace(["docs/**/*.md", "*.md"])),
}


def autofix_kill_switch_off(root: Path) -> str | None:
    """Заглушён ли автофикс. -> причина (str) или None (не заглушён)."""
    val = (__import__("os").environ.get(NIGHTLY_AUTOFIX_ENV) or "").strip().lower()
    if val == "off":
        return f"{NIGHTLY_AUTOFIX_ENV}=off"
    if (Path(root) / AUTOFIX_OFF_SENTINEL).exists():
        return f"файл-сигнал {AUTOFIX_OFF_SENTINEL}"
    return None


def enabled_fixers(root: Path, enabled=None) -> list:
    """Включённые ключи фиксеров. По умолчанию (config nightly.autofix.enabled) — ПУСТО (fail-closed).

    `enabled` (список ключей) переопределяет config — для тестов и явного вызова."""
    if enabled is None:
        cfg = Path(root) / ".ai-ops.yaml"
        enabled = []
        if cfg.is_file():
            try:
                doc = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
                enabled = (((doc.get("nightly") or {}).get("autofix") or {}).get("enabled")) or []
            except (OSError, yaml.YAMLError):
                enabled = []
    return [k for k in enabled if k in CLASS_A_FIXERS]


def run_autofix(root: Path, *, dry_run: bool = False, enabled=None, date: str | None = None,
                policy=None, verify=None) -> dict:
    """Собрать включённые правки класса A в ОДИН черновой PR за ночь (или сказать, почему не собрала).

    Возврат (AutoFixResult): {status, reason?, branch?, base_sha?, head_sha?, applied, skipped, pr?,
    budget, enabled}. status: disabled | suggest-only | no_changes | prepared | dry_run | rolled_back.
    Никогда не пишет в main, не мержит, не зовёт модель."""
    from ai_ops_kit.engine import worktree
    from ai_ops_kit.governance import policy_engine
    from ai_ops_kit.shared.budget import Budget

    result = {"applied": [], "skipped": [], "enabled": [], "pr": None,
              "budget": {"max_model_calls": 0, "spent": 0}}

    off = autofix_kill_switch_off(root)
    if off:
        return {**result, "status": "disabled", "reason": f"автофикс заглушён ({off})"}

    keys = enabled_fixers(root, enabled)
    result["enabled"] = keys
    if not keys:
        return {**result, "status": "disabled",
                "reason": "класс A пуст — включённых фиксеров нет (открывается решением владельца)"}

    pol = policy if policy is not None else policy_engine.load_policy(root)
    # Черновой PR — действие уровня «подготовить» (кит готовит, мержит человек). suggest = даже не
    # готовим, только рекомендуем. prepare и выше — готовим черновой PR (но НЕ мержим никогда).
    if policy_engine.level_for("nightly_autofix", pol) == "suggest":
        return {**result, "status": "suggest-only",
                "reason": "policy: nightly_autofix=suggest — только рекомендация, правку не готовлю"}

    _ = Budget(max_model_calls=0)     # заявленный инвариант: класс A детерминирован, модель не зовёт
    day = date or datetime.now().strftime("%Y-%m-%d")
    branch = f"ai-ops/nightly-autofix/{day}"
    fixers = [{"key": k, "apply": CLASS_A_FIXERS[k].apply} for k in keys]

    res = worktree.apply_fixes_in_worktree(root, branch, fixers, base="HEAD", verify=verify)
    result["applied"] = res.get("applied", [])
    result["skipped"] = res.get("skipped", [])
    result["branch"] = branch
    result["base_sha"] = res.get("base_sha")
    result["head_sha"] = res.get("head_sha")

    if res["status"] == "no_changes":
        return {**result, "status": "no_changes", "reason": "включённые фиксеры не нашли что править"}
    if res["status"] in ("error", "rolled_back"):
        return {**result, "status": "rolled_back",
                "reason": res.get("reason", "правки откачены — ни одна не собрана")}

    if dry_run:
        return {**result, "status": "dry_run",
                "reason": "dry-run: правки собраны в ветку, PR не открыт"}

    from ai_ops_kit.delivery import pr_open
    body = ("Ночной автофикс класса A (детерминированные, обратимые правки).\n\n"
            f"База: {res.get('base_sha')}\nВершина: {res.get('head_sha')}\n\n"
            + "\n".join(f"- {a['key']}: {', '.join(a['files'])}" for a in res.get("applied", []))
            + "\n\nЧерновой PR: слияние — за человеком.")
    pr = pr_open.open_draft_pr(root, branch, "chore: ночной автофикс класса A", body,
                               delivery_id=f"nightly-autofix-{day}")
    result["pr"] = pr
    return {**result, "status": "prepared",
            "reason": "правки класса A собраны в один черновой PR (слияние за человеком)"}


def format_autofix_report(res: dict) -> str:
    """Человеческий отчёт об автофиксе: что собрано / что пропущено / почему выключено."""
    st = res.get("status")
    L = ["# Ночной автофикс (класс A)", ""]
    if st in ("disabled", "suggest-only"):
        L.append(f"Ничего не правила: {res.get('reason')}.")
        L.append("Это граница по решению, а не недоделка: класс A открывается по одному пункту.")
        return "\n".join(L)
    if st == "no_changes":
        L.append(f"Включённые фиксеры ({', '.join(res.get('enabled', []))}) не нашли что править.")
        return "\n".join(L)
    if st == "rolled_back":
        L.append(f"Правки откачены: {res.get('reason')}. В main и в PR ничего не ушло.")
        return "\n".join(L)
    applied = res.get("applied", [])
    L.append(f"Собрано правок: {len(applied)} (ветка {res.get('branch')}).")
    for a in applied:
        L.append(f"- **{a['key']}**: {', '.join(a['files'])}")
    if res.get("skipped"):
        L.append("")
        L.append("Пропущено (не прошло проверку или потолок):")
        for s in res["skipped"]:
            L.append(f"- {s.get('key')}: {s.get('reason')}")
    pr = res.get("pr") or {}
    L.append("")
    if st == "dry_run":
        L.append("Dry-run: PR не открыт.")
    elif pr.get("status") in ("opened", "updated"):
        L.append(f"Черновой PR {pr.get('status')}: {pr.get('url', pr.get('number'))}. Слияние — за тобой.")
    else:
        L.append(f"Черновой PR не открыт: {pr.get('note') or pr.get('status')} "
                 f"(правки собраны в ветке {res.get('branch')}).")
    return "\n".join(L)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Nightly Product Health Review (v0, read-only)")
    ap.add_argument("root", nargs="?", default=".", help="Repository root")
    ap.add_argument("--since", help="Commit SHA or ref to diff from")
    ap.add_argument("--json", action="store_true", help="Output delta as JSON")
    ap.add_argument("--confirm", action="store_true",
                    help="отметить обзор разобранным: завтрашняя дельта пойдёт отсюда")
    ap.add_argument("--autofix", action="store_true",
                    help="собрать включённые правки класса A в один черновой PR (по умолчанию класс A пуст)")
    ap.add_argument("--dry-run", action="store_true",
                    help="с --autofix: собрать правки в ветку, но НЕ открывать PR")
    ap.add_argument("--selftest", action="store_true", help="Run self-test")
    args = ap.parse_args()

    if args.selftest:
        # ЧЕСТНЫЙ --selftest (фаза 0, 19.08.2026). Здесь печаталась строка о пройденном
        # селфтесте и три строки «... : OK» — без единого вызова проверяемых функций. То есть
        # модуль УТВЕРЖДАЛ проверку, которой не было: ровно класс «объявлено, но не
        # исполняется», против которого стоит весь кит (ср. R-31/R-32 — две фиктивные проверки
        # в валидаторах). Образец честной формы — devtools/mutation_probe.py: модуль объясняет
        # себя и называет, где лежат его настоящие проверки. Правило репозитория (AGENTS.md):
        # тест модуля живёт в tests/, а не в продакшн-модуле, который едет в child-репозиторий.
        print(__doc__)
        print("Проверки модуля — в tests/unit/ (AGENTS.md: selftest не живёт в продакшн-модуле).")
        return 0

    root = Path(args.root).resolve()
    if not (root / ".git").exists():
        print(f"ERROR: {root} is not a git repository", file=sys.stderr)
        return 1

    if args.confirm:
        # ПОДТВЕРЖДЕНИЕ — ДЕЙСТВИЕ ЧЕЛОВЕКА, а не факт отправки брифа. Отправленный и разобранный
        # обзор — разные вещи, и точку отсчёта двигает второе. Иначе пропущенная ночь молча
        # теряла бы изменения, а разобранная дважды показывала одни и те же находки.
        rec = confirm_review(root)
        print(f"обзор подтверждён на {rec['commit_sha'] or 'неизвестном коммите'} "
              f"({rec['confirmed_at']}) — завтрашняя дельта пойдёт отсюда")
        return 0

    if args.autofix:
        # КЛАСС A: детерминированный автофикс в worktree -> один черновой PR. По умолчанию класс A
        # пуст (fail-closed) — тогда честно скажет, что ничего не правила и почему. Никогда не пишет
        # в main и не мержит.
        res = run_autofix(root, dry_run=args.dry_run)
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(format_autofix_report(res))
        return 0

    delta = collect_delta(root, args.since)

    if args.json:
        print(json.dumps(delta, indent=2, ensure_ascii=False))
    else:
        print(format_brief(delta, root))

    return 0


if __name__ == "__main__":
    sys.exit(main())
