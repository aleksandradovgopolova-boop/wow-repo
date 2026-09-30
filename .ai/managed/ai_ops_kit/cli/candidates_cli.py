"""`ai-ops candidates` — задачи-кандидаты из двух источников + приёмка пачкой.

Кит НАРЕЗАЕТ кандидатов, владелец ПРИНИМАЕТ пачкой (предохранитель: кит сам не дописывает активную
работу). Два источника: непокрытые направления роадмапа (`intelligence.roadmap_candidates`) и
наблюдения дочек/находки (`intelligence.child_findings`). Приёмку в plan.yaml делает
`planning.candidate_intake` — единственное место, что пишет план.

ПОЧЕМУ UNION ЗДЕСЬ, А НЕ В `planning`. Слой `planning` (ниже) не вправе импортировать `intelligence`
(вверх по слоям). Слой `entrypoints` (CLI) вправе звать оба источника — поэтому объединение живёт
здесь, а `candidate_intake` принимает уже собранный список.

Команда:
  candidates            — показать всех кандидатов с источником и причиной (только чтение, dry);
  candidates list       — то же;
  candidates accept <id> [<id>...]  — принять выбранных (пишет plan.yaml);
  candidates accept --all           — принять всех;
  candidates sync-issues [--apply]  — завести открытых кандидатов задачами в трекере (issue —
                                      ПРЕДЛОЖЕНИЕ; план не пишется, приём — по-прежнему `accept`).
Без глагола приёмки план НЕ меняется.
"""
from __future__ import annotations

import json
from pathlib import Path


def _load_outcome_doc(path: Path):
    """Прочитать outcome-объект (контракт/отчёт) read-only. Нет файла/битый → None (не бросаем:
    отсутствие замера — законное состояние фичи, а не сбой источника). Тот же приём, что у
    `ai_ops_cli_intents._feature_measured_outcome` — второго читателя формы не заводим по существу."""
    import yaml as _yaml
    if not path.is_file():
        return None
    try:
        doc = _yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, _yaml.YAMLError):
        return None
    return doc if isinstance(doc, dict) else None


def _collect_outcome_items(child_root) -> list:
    """Собрать `(feature, contract, readout, evaluation)` по `features/*/outcome-contract.yaml`.

    Загрузка файлов И вердикт (`evaluate_outcome`) живут ЗДЕСЬ, на слое CLI, СОЗНАТЕЛЬНО: `evaluate_
    outcome` — из `validation` (entrypoints), а `intelligence.decision_candidates` её не импортирует
    (это была бы зависимость вверх — тот же инвариант, что у `outcome_insight`/`knowledge_graph`).
    Поэтому «посчитать вердикт из чисел» делает точка входа, а проектор получает готовые входы."""
    from ai_ops_kit.validation import validate_product_objects as vpo
    root = Path(child_root)
    fdir = root / "features"
    items: list = []
    if not fdir.is_dir():
        return items
    for cpath in sorted(fdir.glob("*/outcome-contract.yaml")):
        contract = _load_outcome_doc(cpath)
        if contract is None:
            continue
        feature = cpath.parent.name
        readout = _load_outcome_doc(cpath.parent / "outcome-readout.yaml")
        evaluation = vpo.evaluate_outcome(contract, readout)
        items.append((feature, contract, readout, evaluation))
    return items


def outcome_decision_candidates(child_root) -> list:
    """Недобравшие фичи (вердикт `failed` по числам ИЛИ неподтверждённая гипотеза) → продуктовые
    РЕШЕНИЯ-кандидаты (DRAFT). READ-ONLY. Загрузку/вердикт делает `_collect_outcome_items` (CLI),
    проектор `intelligence.decision_candidates` — чистый. -> list[dict]."""
    from ai_ops_kit.intelligence import decision_candidates
    proj = decision_candidates.project_decision_candidates(_collect_outcome_items(child_root))
    return proj.get("candidates") or []


def gather_candidates(child_root) -> list:
    """Union кандидатов из ТРЁХ источников: находки дочек + непокрытые направления роадмапа +
    недобравшие фичи как продуктовые решения.

    `child_findings.project_findings` и `roadmap_candidates.project_uncovered_directions` fail-safe по
    построению (пропускают битые файлы / ловят PlanCorrupt) — зовём их напрямую. Третий источник,
    `outcome_decision_candidates`, делает диск-I/O по `features/*/` перед чистым проектором: его
    ОБОРАЧИВАЕМ симметрично инбоксу, чтобы сбой сбора исходов не ронял команду `candidates`. Глушителя
    `except: pass` тут нет — на сбое честно берём пустой список этого источника (это НЕ «кандидатов
    нет», а «источник не собрался»; остальные два всё равно отработают). -> list[dict]."""
    from ai_ops_kit.intelligence import child_findings, roadmap_candidates
    root = Path(child_root)
    out: list = []
    out.extend(child_findings.project_findings(root).get("candidates") or [])
    out.extend(roadmap_candidates.project_uncovered_directions(root).get("candidates") or [])
    try:
        decision = outcome_decision_candidates(root)
    except Exception:  # noqa: BLE001 — сбой источника исходов не роняет команду candidates
        decision = []
    out.extend(decision)
    return out


def inbox_direction_candidates(child_root):
    """Непокрытые направления роадмапа как кандидаты (DRAFT) для входящих владельца. `project_uncovered
    _directions` сама fail-safe: план не читается -> readable:false (честное «не знаю», не «покрыто
    всё»). READ-ONLY. -> list|None (None = состояние покрытия неизвестно, во входящих молчим)."""
    from ai_ops_kit.intelligence import roadmap_candidates
    proj = roadmap_candidates.project_uncovered_directions(Path(child_root))
    return (proj.get("candidates") or []) if proj.get("readable") else None


def _positionals(a) -> list:
    """Позиционные аргументы БЕЗ каталога репозитория (`.`/абсолютный путь подставляет обёртка)."""
    def _is_dir(p):
        try:
            return Path(p).is_dir()
        except OSError:
            return False
    return [x for x in (getattr(a, "rest", None) or []) if not _is_dir(x)]


_SOURCE_LABEL = {
    "roadmap-direction": "направление роадмапа без работ",
    "child-finding": "наблюдение из прогона дочки",
    "outcome-decision": "недобравшая фича — решить следующий шаг",
}


def _source_label(cand: dict) -> str:
    return _SOURCE_LABEL.get(str(cand.get("source") or ""), "наблюдение из прогона")


def _list_candidates(child_root, cands, js: bool) -> int:
    """Показать кандидатов (только чтение)."""
    if js:
        print(json.dumps({"kind": "TaskCandidates", "count": len(cands), "candidates": cands},
                         ensure_ascii=False, indent=2))
        return 0
    if not cands:
        print("Кандидатов нет: непокрытых направлений роадмапа и открытых наблюдений не найдено.\n"
              "Это не «нечего делать» — это «резать пока нечего» по прочитанным источникам.")
        return 0
    print(f"Задачи-кандидаты ({len(cands)}) — черновики, активной работой станут только по приёмке:")
    for c in cands:
        print("")
        print(f"• {c.get('title')}")
        print(f"    откуда: {_source_label(c)}")
        if c.get("rationale"):
            print(f"    почему: {c['rationale']}")
        print(f"    принять: ./ai-ops candidates accept {c.get('id')}")
    print("\nПринять всех: ./ai-ops candidates accept --all")
    return 0


def _accept_candidates(child_root, cands, ids, take_all: bool, js: bool, goal=None) -> int:
    """Принять выбранных/всех кандидатов пачкой (пишет plan.yaml). `goal` — явное направление
    владельца (`--goal`) для кандидатов без своего source_goal."""
    from ai_ops_kit.planning import candidate_intake
    if take_all:
        chosen = [c.get("id") for c in cands if c.get("id")]
        if not chosen:
            # --all при пустом списке — не ошибка ввода, а «принимать нечего» (в т.ч. после того,
            # как всё уже принято — идемпотентно). Честный ноль, а не окрик про синтаксис.
            if js:
                print(json.dumps({"ok": True, "to_add": [], "skipped_existing": [],
                                  "applied": False, "reason": "кандидатов нет"}, ensure_ascii=False))
            else:
                print("Кандидатов нет — принимать нечего.")
            return 0
    else:
        chosen = list(ids)
        if not chosen:
            msg = ("candidates accept: назовите id кандидата(ов) или --all.\n"
                   "Список: ./ai-ops candidates")
            if js:
                print(json.dumps({"ok": False, "reason": "нет id и нет --all"}, ensure_ascii=False))
            else:
                print(msg)
            return 2
    rep = candidate_intake.accept_candidates(child_root, chosen, cands, goal=goal, apply=True)
    if js:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 1 if rep.get("error") else 0
    if rep.get("error"):
        print(f"Принять не удалось: {rep['error']}")
        return 1
    added = rep.get("to_add") or []
    skipped = rep.get("skipped_existing") or []
    no_goal = rep.get("skipped_no_goal") or []
    collision = rep.get("skipped_collision") or []
    if added:
        print(f"Добавлено в план работ ({len(added)}):")
        for it in added:
            print(f"  • {it['id']} — {it['title']}")
    if skipped:
        print(f"Пропущено (уже есть в плане) — {len(skipped)}: {', '.join(skipped)}")
    for ng in no_goal:
        print(f"Пропущено (нет направления) — {ng['id']}: {ng['reason']}")
    for cl in collision:
        print(f"Пропущено (совпал slug «{cl['slug']}» с кандидатом {cl['clashes_with']}) — "
              f"{cl['id']}: разные кандидаты дают одно имя работы, прими их по одному")
    if not (added or skipped or no_goal or collision):
        print("Ничего не добавлено: названные кандидаты не найдены среди актуальных.")
    return 0


def _sync_issues(child_root, cands, apply: bool, js: bool) -> int:
    """`candidates sync-issues`: завести открытых кандидатов задачами в трекере через `gh`.

    Дефолт — СУХОЙ прогон (показать план); `--apply` выполняет. Зеркалит `run_roadmap_sync`:
    issue — только ПРЕДЛОЖЕНИЕ, план не пишется. Нет доступа к GitHub — честное третье состояние
    (код 2, «не проверено»), а НЕ пустой план с кодом 0. Адаптер `GhIssueClient` переиспользуем из
    roadmap_sync_cli — второго адаптера к `gh` не заводим."""
    import subprocess
    from ai_ops_kit.cli.roadmap_sync_cli import GhIssueClient
    from ai_ops_kit.planning import candidate_issue_sync as sync_mod
    client = GhIssueClient(child_root)
    try:
        result = sync_mod.sync(cands, client, apply=apply)
    except FileNotFoundError:
        print("  · не проверено: не найден `gh` — установите GitHub CLI и `gh auth login`")
        return 2
    except subprocess.CalledProcessError as e:
        reason = (e.stderr or e.stdout or str(e)).strip().splitlines()[-1:] or [str(e)]
        print(f"  · не проверено: GitHub недоступен — {reason[0]}")
        return 2
    if js:
        print(json.dumps({
            "apply": apply, "in_sync": result.in_sync,
            "create": [{"key": x.key, "title": x.title, "number": x.number} for x in result.creates],
            "update": [{"key": x.key, "number": x.number} for x in result.updates],
            "close": [{"key": x.key, "number": x.number} for x in result.closes],
        }, ensure_ascii=False, indent=2))
        return 0
    if result.in_sync:
        print("Трекер уже сведён с кандидатами — заводить и закрывать нечего.")
        return 0
    verb = "Выполнено" if apply else "План (сухой прогон)"
    print(f"{verb}: завести {len(result.creates)}, обновить {len(result.updates)}, "
          f"закрыть {len(result.closes)}. Issue — предложение; план не пишется.")
    for x in result.creates:
        tag = f"#{x.number}" if x.number else "(new)"
        print(f"  + завести {tag}: {x.title}")
    for x in result.updates:
        print(f"  ~ обновить #{x.number}: {x.title}")
    for x in result.closes:
        print(f"  − закрыть #{x.number}: {x.title} (кандидат больше не открыт)")
    if not apply:
        print("\nЭто предпросмотр. Применить: `candidates sync-issues --apply`.")
    return 0


def run_candidates(child_root, a) -> int:
    """Точка входа команды `candidates`. Диспетч: list (dry) / accept (пишет) / sync-issues (трекер)."""
    js = bool(getattr(a, "json", False))
    root = Path(child_root)
    args = _positionals(a)
    verb = (args[0].strip().lower() if args else "list")
    cands = gather_candidates(root)
    if verb in ("", "list"):
        return _list_candidates(root, cands, js)
    if verb == "accept":
        take_all = bool(getattr(a, "all", False))
        ids = args[1:]
        return _accept_candidates(root, cands, ids, take_all, js, goal=getattr(a, "goal", None))
    if verb == "sync-issues":
        return _sync_issues(root, cands, bool(getattr(a, "apply", False)), js)
    # Неизвестный глагол — назвать, что умеет, а не молча вернуть успех.
    if js:
        print(json.dumps({"ok": False, "reason": f"неизвестная подкоманда: {verb}",
                          "subcommands": ["list", "accept", "sync-issues"]}, ensure_ascii=False))
    else:
        print(f"candidates: неизвестная подкоманда «{verb}». "
              f"Доступно: list, accept <id..>|--all, sync-issues [--apply].")
    return 2


def _intent_candidates(task, child_root, signals, a) -> int:
    """Обработчик интента `candidates` (регистрируется в ai_ops_cli)."""
    return run_candidates(Path(child_root), a)
