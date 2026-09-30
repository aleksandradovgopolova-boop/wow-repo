#!/usr/bin/env python3
"""Расписание и доставка ночного обзора.

Замкнутый кластер, вынесенный из `nightly_review.py`. Работа
`delta_review_runs_nightly_and_briefs_the_owner` требует двух свойств, и оба легко подделать
словами:

  «идёт ночью»  — кит НЕ крутит демон и не планировщик. Единственное честное «ночью» — это
                  РЕАЛЬНЫЙ триггер в репозитории (CI-workflow с `schedule: cron`, который зовёт
                  обзор). Обещание в конфиге триггером не является: оно не сработает ни разу.
                  Честность та же, что у EnvironmentMap: ОБЪЯВЛЕНО ≠ ВИДНО. Отсюда три состояния —
                  detected (триггер есть), declared_not_detected (конфиг просит, триггера нет),
                  absent (ни того ни другого). Второе НЕ сворачивается в первое.

  «брифует владельца» — бриф, оставшийся в stdout, владельца не достиг. ПРОИЗВЕДЁН ≠ ДОСТАВЛЕН
                  (тот же инвариант, что у `--confirm`: отправленный ≠ прочитанный). Доставка —
                  запись в durable-инбокс обзоров + указатель `latest.md` + receipt. Прочитал ли
                  владелец — отдельный вопрос, и его закрывает `--confirm`, а не факт записи.

Модуль импортируется САМИМ `nightly_review` (не-тестовый импортёр); оркестрация ночного прогона
(`run_nightly`) остаётся в `nightly_review` и зовёт `deliver_brief` отсюда.
"""
from __future__ import annotations

import yaml
from datetime import datetime
from pathlib import Path

SCHEDULE_WORKFLOW_REL = ".github/workflows/nightly-review.yml"
BRIEFS_DIR_REL = ".ai/project/nightly-review/briefs"


def _shipped_script_path(root: Path) -> str:
    """Как звать обзор из CI дочки. В дочке — из поставки, в самом ките — из пакета."""
    rel = (".ai/managed/ai_ops_kit/intelligence/nightly_review.py"
           if (Path(root) / ".ai" / "managed").is_dir()
           else "ai_ops_kit/intelligence/nightly_review.py")
    return rel


def _nightly_declared(root: Path) -> bool:
    """Просит ли конфиг ночной обзор (`nightly.enabled` / `nightly.schedule`)?"""
    cfg = Path(root) / ".ai-ops.yaml"
    if not cfg.is_file():
        return False
    try:
        doc = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False
    n = doc.get("nightly") or {}
    return bool(n.get("enabled")) or bool(n.get("schedule"))


def _cron_of_nightly_workflow(root: Path) -> str | None:
    """Найти РЕАЛЬНЫЙ ночной триггер обзора: workflow с `schedule: cron`, зовущий nightly_review.

    -> строка cron первого такого триггера, либо None (триггера нет). Требуем ОБА признака:
    расписание И вызов обзора — иначе это чужой запланированный workflow, а не наш «ночью».
    """
    wf_dir = Path(root) / ".github" / "workflows"
    if not wf_dir.is_dir():
        return None
    for wf in sorted(list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml"))):
        try:
            raw = wf.read_text(encoding="utf-8")
        except OSError:
            continue
        if "nightly_review" not in raw:
            continue
        try:
            doc = yaml.safe_load(raw)
        except yaml.YAMLError:
            continue
        if not isinstance(doc, dict):
            continue
        # PyYAML разбирает `on:` в YAML 1.1 как булев ключ True — учитываем оба написания.
        on = doc.get("on", doc.get(True))
        sched = on.get("schedule") if isinstance(on, dict) else None
        if isinstance(sched, list):
            for item in sched:
                if isinstance(item, dict) and item.get("cron"):
                    return str(item["cron"])
    return None


def schedule_status(root: Path) -> dict:
    """Идёт ли обзор ночью НА САМОМ ДЕЛЕ. -> {"state", "reason", "cron"?, "workflow"?}.

    state: detected — есть CI-триггер `schedule: cron`, зовущий обзор; declared_not_detected —
    конфиг просит ночной обзор, но триггера нет (объявлено, но не сработает); absent — ни того ни
    другого. Кит не планировщик: «ночью» подтверждает триггер в репозитории, а не намерение.
    """
    root = Path(root)
    cron = _cron_of_nightly_workflow(root)
    if cron:
        return {"state": "detected", "cron": cron, "workflow": SCHEDULE_WORKFLOW_REL,
                "reason": f"ночной обзор запускается CI по расписанию `{cron}`"}
    if _nightly_declared(root):
        return {"state": "declared_not_detected",
                "reason": ("ночной обзор объявлен в .ai-ops.yaml, но CI-триггера нет — "
                           "расписание НЕ сработает; поставить триггер: `--install-schedule`")}
    return {"state": "absent",
            "reason": "ночного триггера обзора нет и он не объявлен — поставить: `--install-schedule`"}


def _workflow_yaml(cron: str, script_rel: str) -> str:
    """Текст CI-workflow: ночной запуск обзора с доставкой брифа владельцу.

    PYTHONPATH — каталог, СОДЕРЖАЩИЙ пакет `ai_ops_kit` (в дочке `.ai/managed`, в ките `.`): без
    него запуск скрипта по пути падает в CI на `import ai_ops_kit` (script-mode не видит пакет).
    Права `contents: read` — обзор только читает git и пишет бриф в файловую систему раннера.
    """
    pkg_suffix = "ai_ops_kit/intelligence/nightly_review.py"
    pythonpath = script_rel[:-len(pkg_suffix)].rstrip("/") or "."
    return (
        "# Сгенерировано AI Ops (nightly_review.install_schedule). Правьте cron через\n"
        "# `--install-schedule --cron ...` или .ai-ops.yaml (nightly.schedule).\n"
        "name: nightly-product-review\n"
        "on:\n"
        "  schedule:\n"
        f"    - cron: \"{cron}\"\n"
        "  workflow_dispatch: {}\n"
        "permissions:\n"
        "  contents: read\n"
        "jobs:\n"
        "  review:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "        with:\n"
        "          fetch-depth: 0\n"
        "      - uses: actions/setup-python@v5\n"
        "        with:\n"
        "          python-version: \"3.12\"\n"
        "      - run: pip install pyyaml\n"
        "      - name: Ночной дельта-обзор и бриф владельцу\n"
        f"        env: {{PYTHONPATH: \"{pythonpath}\"}}\n"
        f"        run: python {script_rel} . --deliver | tee -a \"$GITHUB_STEP_SUMMARY\"\n"
    )


def install_schedule(root: Path, cron: str = "0 3 * * *") -> dict:
    """Поставить РЕАЛЬНЫЙ ночной триггер: CI-workflow с `schedule: cron`, зовущий обзор.

    Идемпотентно: тот же файл создаётся/обновляется, второго workflow не плодит. -> {"status":
    created|updated, "workflow", "cron"}. Кит не мержит и не деплоит — кладёт триггер, дальше CI.
    """
    root = Path(root)
    wf = root / SCHEDULE_WORKFLOW_REL
    existed = wf.is_file()
    wf.parent.mkdir(parents=True, exist_ok=True)
    wf.write_text(_workflow_yaml(cron, _shipped_script_path(root)), encoding="utf-8")
    return {"status": "updated" if existed else "created",
            "workflow": SCHEDULE_WORKFLOW_REL, "cron": cron}


def deliver_brief(root: Path, brief: str, *, date: str | None = None) -> dict:
    """Доставить бриф владельцу: durable-инбокс + указатель latest + receipt.

    Произведён ≠ доставлен: бриф в stdout владельца не достиг. Пишем датированный файл и
    `latest.md` (что открыть, не выбирая), возвращаем receipt как доказательство доставки.
    """
    root = Path(root)
    day = date or datetime.now().strftime("%Y-%m-%d")
    inbox = root / BRIEFS_DIR_REL
    inbox.mkdir(parents=True, exist_ok=True)
    body = brief if brief.endswith("\n") else brief + "\n"
    dated = inbox / f"{day}.md"
    latest = inbox / "latest.md"
    dated.write_text(body, encoding="utf-8")
    latest.write_text(body, encoding="utf-8")
    return {"kind": "NightlyBriefReceipt", "schema_version": 1,
            "delivered_at": datetime.now().isoformat(), "date": day,
            "path": f"{BRIEFS_DIR_REL}/{day}.md", "latest": f"{BRIEFS_DIR_REL}/latest.md"}


def format_schedule_status(st: dict) -> str:
    """Человеческий ответ про расписание — состоянием, а не намёком."""
    state = st.get("state")
    if state == "detected":
        return f"Обзор идёт ночью: CI по расписанию `{st.get('cron')}` ({st.get('workflow')})."
    if state == "declared_not_detected":
        return f"Расписание объявлено, но не сработает. {st.get('reason')}"
    return f"Ночного расписания нет. {st.get('reason')}"
