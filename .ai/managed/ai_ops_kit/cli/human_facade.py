#!/usr/bin/env python3
"""Фасад владельца (P1 №8/№9 ревью): МАЛЕНЬКАЯ поверхность из 7 действий поверх 34 intents.

Вынесено из `ai_ops_cli` (ратчет module-size: фасад перевёл ai_ops_cli.py за порог монолита).
Это СЛОЙ поверх INTENTS, а не замена: `ai_ops_cli` импортирует символы отсюда и зовёт их в main()
как раньше. Ребро импорта одностороннее — `ai_ops_cli` -> `human_facade`; обратно (в main() для
переписанных argv) уходит НЕ импортом, а колбэком `run_main`, поэтому цикла нет. `_WORK_SUBS`
берём из ai_ops_cli_intents (он ai_ops_cli на верхнем уровне не импортирует). Слой прежний: оба
файла — `cli`; `_review_all` зовёт intelligence.nightly_review ВНИЗ по слоям (legal, как readout).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ai_ops_kit.cli.ai_ops_cli_intents import _WORK_SUBS  # проекция `work show <id>`


def _is_dir_safe(p):
    """#161: Path.is_dir() кидает OSError (ENAMETOOLONG и др.) вместо False, когда первый позиционный
    аргумент — не путь, а длинный текст задачи (>255 байт). На 3.11/3.12 это роняло main(); на 3.14
    stdlib глотает сам, и баг маскируется. Не-путь (в т.ч. слишком длинный) = не каталог."""
    try:
        return Path(p).is_dir()
    except OSError:
        return False


# ── Фасад владельца (P1 №8/№9 ревью): МАЛЕНЬКАЯ поверхность из 7 действий поверх 34 intents ─────────
# Владелец говорит НАМЕРЕНИЕМ человеческим языком; 34 intents остаются рабочей механикой и в лицо не
# лезут (их показывает `ai-ops help --all`). Это СЛОЙ поверх INTENTS, а не замена: len(INTENTS) === 34,
# каждый intent вызывается как раньше. Порядок глаголов ФИКСИРОВАН владельцем и не переставляется.
# Поля записи:
#   intents    — на какие внутренние намерения/пути ложится глагол; первое — первичное (см. `mode`);
#   mode       — "primary" (первичный intent) | "all" (владельческий обзор: все intents по очереди);
#   needs_task — нужен ли текст задачи (для меню и тестов);
#   label      — человеческое описание ОДНОЙ строкой (смысл, без внутренних терминов).
# Как каждый глагол реально запускается — в `facade_plan`/`_dispatch_human_action`:
#   • research — НЕ intent: запуск RESEARCH-воркфлоу движком через `run` с task_type=research
#     (workflows.yaml: selection_criteria.task_type включает 'research'; ai_route выбирает RESEARCH ->
#     вопрос -> доказательства EV-* -> пакет решения DP-* в .research/).
#   • start/check/release — переписываются на внутренние intents (check — обзор из трёх, read-only).
#   • work   — имя совпадает с intent-проекцией, поведение РАЗНОЕ: `work show <id>` — read-only
#     проекция (intent), `work "<текст>"` — фасадное «делай» -> run. Различаем по подкоманде.
#   • review — `review` (без «all») идёт в intent `review` (конкретное изменение, ВКЛЮЧАЯ
#     безопасность — отдельного security-глагола нет). `review all` — полный обзор продукта сейчас
#     (как ночной, по требованию): cli зовёт intelligence.nightly_review.run_nightly ВНИЗ по слоям.
#   • feedback — существующий intent `feedback` (мета-канал о самом ките), обычный разбор.
HUMAN_ACTIONS = {
    "research": {"intents": ("run",),                     "mode": "primary", "needs_task": True,
                 "label": "запускаю исследование: превращаю вопрос в доказательства и пакет решения"},
    "start":    {"intents": ("specify",),                 "mode": "primary", "needs_task": True,
                 "label": "хочу что-то изменить — опиши задачу, кит уточнит и подготовит"},
    "check":    {"intents": ("status", "inbox", "next"),  "mode": "all",     "needs_task": False,
                 "label": "где мы и можно ли двигаться: статус, что ждёт решения, что дальше"},
    "work":     {"intents": ("run", "do"),                "mode": "primary", "needs_task": True,
                 "label": 'делай:  ai-ops work "что нужно"  (сначала показывает план; --execute — выполнить)'},
    "review":   {"intents": ("review",),                  "mode": "primary", "needs_task": False,
                 "label": "проверь, можно ли выпускать и почему — включая безопасность "
                          "(review all — полный обзор продукта сейчас)"},
    "release":  {"intents": ("delivery",),                "mode": "primary", "needs_task": False,
                 "label": "готовлю к выпуску и проверяю зрелость; сам merge и деплой "
                          "подтверждаешь ты — кит не выпускает и не деплоит за тебя"},
    "feedback": {"intents": ("feedback",),                "mode": "primary", "needs_task": True,
                 "label": "сообщить КИТУ о проблеме кита (это про сам кит, не про твой продукт)"},
}


# ── Диспетч фасада владельца (7 действий поверх INTENTS) ───────────────────────────────────────────
def _first_nondir(rest):
    """Первый позиционный токен, который НЕ флаг и НЕ каталог репозитория, нижним регистром.

    Обёртка `./ai-ops` подставляет путь то в начало, то в хвост; подкоманда (`show`/`all`) каталогом
    не бывает — так мы отличаем `work show <id>` и `review all` от свободного текста задачи."""
    for tok in rest:
        if tok.startswith("-") or _is_dir_safe(tok):
            continue
        return tok.strip().lower()
    return ""


def _child_root_from(rest):
    """Каталог репозитория из позиционных фасадного вызова (или '.'). Тем же правилом, что и CLI."""
    for tok in rest:
        if not tok.startswith("-") and _is_dir_safe(tok):
            return tok
    return "."


def _inject_task_type(rest, task_type):
    """Копия rest, где в --signals ГАРАНТИРОВАН task_type (не перетирая явный).

    Так фасадный `research` доносит до движка research-тип задачи: ai_route по нему выбирает
    RESEARCH-воркфлоу. Нет --signals -> добавляем; есть JSON без task_type -> дополняем; явный
    task_type или не-JSON («обычные слова») -> уважаем как есть (не трогаем)."""
    rest = list(rest)
    if "--signals" in rest:
        i = rest.index("--signals")
        raw = rest[i + 1] if i + 1 < len(rest) else "{}"
        try:
            sig = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            sig = None  # обычные слова о размере/риске — не перезаписываем, уважаем вход
        if isinstance(sig, dict):
            sig.setdefault("task_type", task_type)
            rest[i + 1] = json.dumps(sig, ensure_ascii=False)
        return rest
    return rest + ["--signals", json.dumps({"task_type": task_type}, ensure_ascii=False)]


def facade_plan(argv):
    """Разложить фасадный вызов в список внутренних intent-argv. -> list[list[str]] | None.

    None означает «это не переписываемый фасадный вызов» — CLI разбирает argv как обычно (сюда
    попадают `review`/`review <опц.>` -> intent review, `feedback` -> intent feedback,
    `work show <id>` -> intent-проекция, и любой не-фасадный глагол). `review all` в argv-форму не
    ложится (это вызов функции обзора) и обрабатывается в `_dispatch_human_action` ДО facade_plan.
    Функция ЧИСТАЯ — её проверяют тесты роутинга."""
    if not argv:
        return None
    verb = argv[0]
    spec = HUMAN_ACTIONS.get(verb)
    if spec is None:
        return None
    rest = argv[1:]
    if verb in ("review", "feedback"):
        return None  # совпадают с intent по имени и поведению — обычный разбор
    if verb == "work":
        if _first_nondir(rest) in _WORK_SUBS:
            return None  # `work show <id>` — read-only проекция intent `work`
        return [["run"] + rest]
    if verb == "research":
        return [["run"] + _inject_task_type(rest, "research")]
    targets = spec["intents"] if spec.get("mode") == "all" else spec["intents"][:1]
    return [[t] + rest for t in targets]


def _review_all(rest):
    """`review all` — полный обзор продукта СЕЙЧАС (как ночной, по требованию). Только чтение:
    собираем дельту и бриф, владельцу не доставляем (deliver=False — без записи в инбокс/receipt).

    Слой: cli (entrypoints) зовёт intelligence.nightly_review ВНИЗ по слоям — как readout зовёт
    post_release_loop. planning/next_work/legal здесь не трогаем."""
    from ai_ops_kit.intelligence import nightly_review
    root = Path(_child_root_from(rest))
    if not (root / ".git").exists():
        print(f"review all: {root} — не git-репозиторий (обзор строится по истории коммитов)",
              file=sys.stderr)
        return 1
    result = nightly_review.run_nightly(root, deliver=False)
    print(result.get("brief") or "review all: обзор пуст (нет изменений с последней точки).")
    return 0


def _dispatch_human_action(argv, run_main):
    """Если argv — фасадный глагол владельца, выполнить его и вернуть код; иначе None.

    Вызывается из main() ПОСЛЕ человеческой двери и ДО argparse. Переписанные argv (`run`, `specify`,
    `status`…) снова проходят через main() (передан колбэком `run_main` — обратного импорта нет,
    цикла нет), но там уже не фасадные глаголы — рекурсии нет."""
    if not argv:
        return None
    verb = argv[0]
    if verb not in HUMAN_ACTIONS:
        return None
    rest = argv[1:]
    # `review all` — обзор продукта (вызов функции, не переписывание в intent).
    if verb == "review" and _first_nondir(rest) == "all":
        # убрать сам маркер `all` из позиционных (каталог, если был, останется)
        return _review_all([t for t in rest if t.strip().lower() != "all"])
    plan = facade_plan(argv)
    if plan is None:
        return None  # обычный разбор (review без all, feedback, work show <id>)
    worst = 0
    for sub in plan:
        rc = run_main(sub) or 0
        if rc:
            worst = rc
    return worst
