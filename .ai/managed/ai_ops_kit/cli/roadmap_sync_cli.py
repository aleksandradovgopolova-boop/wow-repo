#!/usr/bin/env python3
"""CLI-адаптер команды `roadmap sync-issues`: сеть (`gh`) и оркестрация вокруг чистого ядра.

Вынесено из `ai_ops_cli_intents.py`, чтобы держать интенты под ратчетом размера модуля. Ядро сверки
(чистая функция на инъектируемом порте) — в `ai_ops_kit.planning.roadmap_issue_sync`; здесь только
сторона с побочными эффектами: адаптер `GhIssueClient` к `gh` и печать плана/итога.
"""
from __future__ import annotations

import json


class GhIssueClient:
    """Адаптер порта трекера к `gh` в каталоге дочки. Сеть и побочные эффекты — здесь, не в ядре."""

    def __init__(self, root):
        self._root = str(root)
        self._label_ensured = False

    def _run(self, args, *, input=None, check=True):
        import subprocess
        return subprocess.run(["gh", *args], cwd=self._root, input=input,
                              capture_output=True, text=True, check=check)

    def list(self):
        from ai_ops_kit.planning import roadmap_issue_sync as _s
        r = self._run(["issue", "list", "--label", _s.LABEL, "--state", "all",
                       "--json", "number,title,body,state", "--limit", "500"])
        rows = json.loads(r.stdout or "[]")
        return [_s.Issue(number=int(x["number"]), title=x.get("title") or "",
                         body=x.get("body") or "", state=(x.get("state") or "").lower())
                for x in rows]

    def _ensure_label(self):
        from ai_ops_kit.planning import roadmap_issue_sync as _s
        if self._label_ensured:
            return
        self._run(["label", "create", _s.LABEL, "--color", "1d76db",
                   "--description", "Трекер направления роадмапа (sync-issues)"], check=False)
        self._label_ensured = True

    def create(self, title, body, labels):
        self._ensure_label()
        args = ["issue", "create", "--title", title, "--body-file", "-"]
        for lb in labels:
            args += ["--label", lb]
        r = self._run(args, input=body)
        url = (r.stdout or "").strip().splitlines()[-1]
        return int(url.rstrip("/").split("/")[-1])

    def edit(self, number, body):
        self._run(["issue", "edit", str(number), "--body-file", "-"], input=body)

    def close(self, number):
        self._run(["issue", "close", str(number)])


def run_roadmap_sync(child_root, a):
    """`roadmap sync-issues`: свести GitHub Issues с роадмапом (эпик на направление + подзадачи).

    Дефолт — СУХОЙ прогон (показать план); `--apply` выполняет. Нет доступа к GitHub — честное
    третье состояние (код 2, «не проверено»), а НЕ пустой план с кодом 0."""
    import subprocess
    from ai_ops_kit.planning import roadmap_manager
    from ai_ops_kit.planning import roadmap_issue_sync as sync_mod
    from ai_ops_kit.planning import delivery_plan as _plan
    js = a.json
    apply = bool(getattr(a, "apply", False))
    try:
        rep = roadmap_manager.check(child_root)
    except _plan.PlanCorrupt as e:
        print(f"ОШИБКА: {e}")
        return 1
    if rep.get("errors"):
        for e in rep["errors"]:
            print(f"  ✗ {e}")
        return 1
    plan_yaml = _plan.load(child_root)
    items = _plan.items(plan_yaml) if plan_yaml else []
    client = GhIssueClient(child_root)
    try:
        result = sync_mod.sync(rep, items, client, apply=apply)
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
        print("Issue-трекер уже сведён с роадмапом — заводить и закрывать нечего.")
        return 0
    verb = "Выполнено" if apply else "План (сухой прогон)"
    print(f"{verb}: завести {len(result.creates)}, обновить {len(result.updates)}, "
          f"закрыть {len(result.closes)}.")
    for x in result.creates:
        tag = f"#{x.number}" if x.number else "(new)"
        print(f"  + завести {tag}: {x.title}")
    for x in result.updates:
        print(f"  ~ обновить #{x.number}: {x.title}")
    for x in result.closes:
        print(f"  − закрыть #{x.number}: {x.title} (направление/работа больше не открыты)")
    if not apply:
        print("\nЭто предпросмотр. Применить: `roadmap sync-issues --apply`.")
    return 0
