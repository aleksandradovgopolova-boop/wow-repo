#!/usr/bin/env python3
"""Две проверки на ПРОТУХАНИЕ: то, что портится не изменением, а его отсутствием (14.08.2026).

ПОВОД — НАБЛЮДЕНИЕ ВЛАДЕЛЬЦА, а не идея. Кит за день ни разу не соврал, но занял пассивную роль:
честно отвечал на заданные вопросы и пропустил всё, о чём его не спросили. Рядом жили две неправды —
документация про мёртвый продукт и план, отставший на 31 изменение, — и обе нашёл вопрос человека,
а не кит.

ПОЧЕМУ СУЩЕСТВУЮЩИЕ ПРОВЕРКИ ЭТО НЕ ЛОВИЛИ. Кит умеет замечать расхождение, КОТОРОЕ СОЗДАНО
изменением: `contour_consistency` смотрит на дифф, `freshness` — на дату. Здесь оба слепы:
документация испортилась не правкой, а тем, что продукт ушёл вперёд, и её никто не тронул; а
документ без проставленной даты `freshness` считает свежим навсегда, и порог там полгода — при том
что здесь всё протухло за восемь дней.

ЧТО ПРОВЕРЯЕТСЯ ЗДЕСЬ — два дешёвых факта, не требующих модели:
  1. описание ссылается на то, чего НЕТ: команда, каталог или файл, упомянутые в документе, в
     репозитории отсутствуют. Это не «документ старый» (мнение), а «ссылка не резолвится» (факт);
  2. план отстал от истории: в репозиторий влиты изменения, а работ под них не объявлено.

ГДЕ ЭТО ЖИВЁТ. В ответе на вопрос «что дальше», а НЕ в автоматических гейтах. Протухание — повод
поговорить, а не остановить прогон: гейт, срабатывающий на каждый устаревший абзац, учат обходить.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from ai_ops_kit.shared import gitio

#: Где обычно живёт описание продукта. Ищем только то, что человек читает как правду о репозитории.
DOC_CANDIDATES = ("README.md", "ROADMAP.md", "AI-OPS-ONBOARDING.md")
DOC_DIRS = ("docs", ".ai/project/context")
#: `npm run <script>` / `make <target>` / `./<script>` — команды, которые документ обещает.
_NPM = re.compile(r"`npm run ([a-z][\w:-]*)`")
_PATHISH = re.compile(r"`([\w./-]+/[\w./-]+|[\w-]+\.(?:py|js|jsx|ts|tsx|sh|yaml|yml|json|md))`")
#: Пути, которые заведомо не про этот репозиторий: URL, плейсхолдеры, чужие пакеты.
#: Абсолютные пути (`/tmp/...`, `/Users/runner`) — примеры из чужих машин, а не ссылки
#: на этот репозиторий; URL, плейсхолдеры и чужие пакеты — тем более.
_SKIP = re.compile(r"^(https?:|<|@|/|node_modules/|\.\.)")
#: Документ ЗАКОННО перечисляет то, чего нет: «удалено», «legacy», «больше не поддерживается».
#: Полевая проверка на `ai-product-quest`: раздел «Legacy Removed» перечислял удалённые файлы, и
#: проверка объявила их мёртвыми ссылками. Жаловаться на документ за то, что он документирует
#: удаление, — верный способ научить владельца пролистывать раздел.
_REMOVAL = re.compile(r"(удал|removed|legacy|deprecat|больше не|устарел|deleted)", re.I)


def _docs(root: Path):
    for name in DOC_CANDIDATES:
        p = root / name
        if p.is_file():
            yield p
    for d in DOC_DIRS:
        base = root / d
        if base.is_dir():
            for p in sorted(base.rglob("*.md"))[:40]:      # потолок: обзор, а не полный аудит
                yield p


def dead_references(root, limit=5) -> list:
    """Ссылки описания на то, чего в репозитории нет. -> [{doc, line, ref, kind}].

    ФАКТ, А НЕ МНЕНИЕ. «Документ устарел» — оценка, спорить с ней можно бесконечно. «В README
    написано `npm run build:pages`, а такого скрипта в package.json нет» — проверяемое утверждение,
    и оно либо верно, либо нет.
    """
    root = Path(root)
    scripts = set()
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            scripts = set((json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {}).keys())
        except (OSError, json.JSONDecodeError):
            scripts = set()
    # Индекс «хвостов» путей: документ законно пишет `ui/storybook_adapter.py`, имея в виду
    # `ai_ops_kit/ui/storybook_adapter.py`. Сокращение — не ложь, и объявлять его мёртвой ссылкой
    # значит превратить проверку в шум, а шумную проверку отключают первой.
    tails = set()
    for f in root.rglob("*"):
        if f.is_file() and ".git/" not in f.as_posix():
            rel = f.relative_to(root).as_posix()
            parts = rel.split("/")
            for i in range(len(parts)):
                tails.add("/".join(parts[i:]))
    # ССЫЛКОЙ НА РЕПОЗИТОРИЙ считается только путь, чей ПЕРВЫЙ сегмент существует в корне
    # (полевая проверка на `ai-product-quest`: три из пяти находок были ложными). `motion/react` —
    # путь импорта пакета, `.tmp/qa-screens/` документ сам объявляет отсутствующим,
    # `owner/repo` — имя чужого репозитория. Ни одно из них не про наши файлы, и жаловаться на них
    # значит учить владельца пролистывать раздел.
    tops = {p.name for p in root.iterdir()} if root.is_dir() else set()
    out = []
    for doc in _docs(root):
        try:
            lines = doc.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        # ближайший предшествующий заголовок — контекст строки: раздел «Legacy Removed» объявляет
        # отсутствие своим названием, и это относится ко всем его пунктам.
        heading = ""
        for i, line in enumerate(lines, 1):
            if line.lstrip().startswith("#"):
                heading = line
            if _REMOVAL.search(line) or _REMOVAL.search(heading):
                continue
            for script in _NPM.findall(line):
                if pkg.is_file() and script not in scripts:
                    out.append({"doc": doc.relative_to(root).as_posix(), "line": i,
                                "ref": f"npm run {script}", "kind": "команда"})
            for ref in _PATHISH.findall(line):
                cand = ref[2:] if ref.startswith("./") else ref
                # `./ai-ops` и подобные — КОМАНДЫ дочернего репозитория, а не пути этого; проверять
                # их существование здесь бессмысленно (их создаёт установка у владельца).
                if ref.startswith("./") and "." not in cand.split("/")[-1]:
                    continue
                if _SKIP.match(ref) or (root / cand).exists() or cand in tails:
                    continue
                if cand.split("/")[0] not in tops:
                    continue                      # не про этот репозиторий (см. комментарий выше)
                out.append({"doc": doc.relative_to(root).as_posix(), "line": i,
                            "ref": ref, "kind": "путь"})
            if len(out) >= limit:
                return out[:limit]
    return out[:limit]


def plan_behind_history(root, plan_rel="planning/plan.yaml") -> dict | None:
    """Отстал ли план от истории. -> {commits, since, plan_rel} или None.

    Замер простой и потому надёжный: сколько коммитов легло в репозиторий ПОСЛЕ последней правки
    плана. Много изменений и молчащий план означают ровно одно — работа идёт мимо объявленного, и
    ответ «что дальше» опирается на устаревшую картину.
    """
    root = Path(root)
    plan = root / plan_rel
    if not plan.is_file():
        return None
    # Форма с `cwd=`, а не `-C`: единый вход к git с таймаутом через gitio.run (см. shared/gitio) —
    # зависший git не вешает ответ «что дальше».
    try:
        rc, sha, _ = gitio.run(["log", "-1", "--format=%H", "--", plan_rel], cwd=root, timeout=20)
        if rc != 0 or not sha:
            return None
        rc2, cnt_out, _ = gitio.run(["rev-list", "--count", f"{sha}..HEAD"], cwd=root, timeout=20)
        if rc2 != 0:
            return None
        n = int(cnt_out or 0)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return {"commits": n, "since": sha[:12], "plan_rel": plan_rel} if n else None


def assess(root, plan_rel="planning/plan.yaml") -> dict:
    """Обе проверки разом — то, что кит скажет в ответ «что дальше»."""
    return {"dead_references": dead_references(root),
            "plan_behind": plan_behind_history(root, plan_rel)}


if __name__ == "__main__":
    print(json.dumps(assess(sys.argv[1] if len(sys.argv) > 1 else "."),
                     ensure_ascii=False, indent=2))
