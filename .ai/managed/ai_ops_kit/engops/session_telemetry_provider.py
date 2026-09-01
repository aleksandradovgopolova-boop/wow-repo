#!/usr/bin/env python3
"""session_telemetry_provider.py — ИЗМЕРЕНИЕ расхода живой сессии Claude Code по её транскрипту.

Контракт: попытаться прочитать данные из runtime; не удалось — честно вернуть None
(unavailable, НЕ 0) и никогда не подставить сессию другого проекта.

РАСКЛАДКА (проверена на машине владельца, 15 каталогов, 14 из 14 с транскриптами сошлись):

    ~/.claude/projects/<slug>/<session_id>.jsonl        # транскрипт сессии
    ~/.claude/projects/<slug>/<session_id>/subagents/   # транскрипты сабагентов

`slug` — это рабочий каталог, из которого запущен рантайм, где каждый символ вне `[A-Za-z0-9-]`
заменён на `-`: `/Users/sasad/msh_news_bot_v2` -> `-Users-sasad-msh-news-bot-v2`,
`/Users/sasad/.claude-jobs/x` -> `-Users-sasad--claude-jobs-x`.

ПОЧЕМУ ЭТО ПЕРЕПИСАНО (найдено полем 2026-08-13). Прежняя версия искала транскрипт по пути
`~/.claude/projects/<proj>/sessions/<session_id>.jsonl`. Каталога `sessions/` не существует ни в
одном проекте — значит провайдер не прочитал НИ ОДНОЙ сессии никогда и ни на одной машине с такой
раскладкой. Каскад: `session_telemetry` оставлял контекст estimated по usage-ledger (а в ledger
лежат вызовы моделей самого кита, не ходы сессии), `session_guardrails.classify_context(None)`
возвращал `unknown`, и ни один из четырёх исходов (continue/compact/clear/new_session) на живой
работе не срабатывал. Вся политика экономии сессии была объявлена и мертва. Второй дефект того же
места: перебор брал ПЕРВЫЙ каталог проекта, то есть после починки пути подставил бы чужую сессию.

ЧТО ИЗМЕРЯЕТСЯ (а не оценивается). В записях `type: "assistant"` есть `message.usage`:
`input_tokens` + `cache_read_input_tokens` + `cache_creation_input_tokens` — это ровно то, что
модель фактически прочитала на этом ходе, то есть размер контекста. `context_current` — сумма по
ПОСЛЕДНЕМУ ходу, `context_peak` — максимум по ходам. Компакция распознаётся по настоящему маркеру
(`type: "system"`, `subtype: "compact_boundary"`, `compactMetadata.trigger`), а не угадывается по
падению контекста; нет маркера — `last_compaction_at` остаётся None.

ЧЕСТНЫЕ ГРАНИЦЫ:
- Приватность: читаются ТОЛЬКО метаданные (числа, timestamps, id, рабочий каталог). Ни `content`,
  ни текст промптов/ответов не читается и наружу не отдаётся — проверяется тестом.
- Один ход рантайм пишет несколькими строками (стриминг) с одинаковым `message.id`; ходы
  дедуплицируются по нему, иначе 75 строк выглядели бы как 75 ходов при 34 настоящих.
- `isSidechain: true` — ходы сабагента: у них своё окно контекста, в контекст сессии не входят.
- Структура `~/.claude/` версионная. Не нашли — None, а не частичные данные и не нули.

CLI:  session_telemetry_provider.py [--session-id ID] [--project-dir DIR] [--json]
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

# Рантайм отдаёт id сессии в ENV. Имя проверено на живой машине: `CLAUDE_CODE_SESSION_ID`.
# `CLAUDE_SESSION_ID` оставлен как исторический синоним (его ждала прежняя версия модуля).
ENV_SESSION_ID_KEYS = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")
ENV_PROJECT_DIR_KEYS = ("CLAUDE_PROJECT_DIR",)

# Строки транскрипта — до сотен килобайт каждая, а нужны нам единицы полей. Дешёвый префильтр по
# подстроке снимает разбор с записей вида `{"type": "mode", ...}`: 63 МБ / 28k строк -> 0.2 с.
_LINE_HINTS = ('"usage"', '"timestamp"')

_SLUG_FORBIDDEN = re.compile(r"[^A-Za-z0-9-]")


def project_slug(path) -> str:
    """Путь рабочего каталога -> имя каталога в `~/.claude/projects/`.

    Правило выведено сверкой, а не догадкой: для каждого каталога проекта взят самый свежий
    транскрипт, из него прочитан записанный `cwd`, и `project_slug(cwd)` совпал с именем каталога
    в 14 случаях из 14 (пятнадцатый каталог транскриптов не содержит вовсе).
    """
    return _SLUG_FORBIDDEN.sub("-", str(path))


def _env(keys) -> str | None:
    for k in keys:
        v = os.environ.get(k)
        if v:
            return v
    return None


def _find_claude_home() -> Path | None:
    """~/.claude/ или None если нет."""
    home = Path.home() / ".claude"
    return home if home.is_dir() else None


def _slug_candidates(project_dir) -> list[str]:
    """Имена каталогов-кандидатов для одного репозитория.

    Две формы, потому что рантайм пишет `cwd` как есть, а вызывающий может дать симлинк: путь как
    передали и он же разрешённый (на macOS `/tmp` -> `/private/tmp`). Порядок сохраняем, дубли — нет.
    """
    p = Path(project_dir)
    forms = [str(p)]
    try:
        forms.append(str(p.resolve()))
    except OSError:
        pass
    out = []
    for f in forms:
        slug = project_slug(f)
        if slug not in out:
            out.append(slug)
    return out


def find_session_file(claude_home: Path, session_id: str | None = None,
                      project_dir: str | None = None) -> tuple[Path, str] | None:
    """Транскрипт сессии + как он найден. None если соответствия нет.

    Порядок намеренный:
      1. id сессии (из ENV или явно) — он глобально уникален, поэтому подмена проектом невозможна;
         сначала смотрим в каталоге ЭТОГО репозитория, потом по всем каталогам по точному имени.
      2. без id — только каталог, соответствующий репозиторию, внутри него самый свежий по mtime.
      3. соответствия нет -> None.

    Чего здесь СПЕЦИАЛЬНО нет: «взять первый найденный каталог проекта». Это подставляло чужую
    сессию — числа чужой работы в рекомендации по своей.
    """
    projects_dir = claude_home / "projects"
    if not projects_dir.is_dir():
        return None

    slugs = _slug_candidates(project_dir) if project_dir else []

    if session_id:
        name = f"{session_id}.jsonl"
        for slug in slugs:
            cand = projects_dir / slug / name
            if cand.is_file():
                return cand, "session-id-in-project"
        # id уникален: поиск по всем каталогам не может привести к чужой сессии.
        try:
            entries = sorted(projects_dir.iterdir())
        except OSError:
            return None
        for proj_dir in entries:
            cand = proj_dir / name
            if proj_dir.is_dir() and cand.is_file():
                return cand, "session-id-scan"
        return None

    for slug in slugs:
        proj_dir = projects_dir / slug
        if not proj_dir.is_dir():
            continue
        files = [f for f in proj_dir.glob("*.jsonl") if f.is_file()]
        if files:
            return max(files, key=lambda f: f.stat().st_mtime), "project-slug-latest"
    return None


def _turn_usage(usage: dict) -> dict:
    """`message.usage` -> числа одного хода. Контекст = всё, что модель прочитала на входе."""
    inp = int(usage.get("input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    return {
        "context": inp + cache_read + cache_write,
        "input_tokens": inp,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "output_tokens": int(usage.get("output_tokens") or 0),
    }


def parse_session_metadata(session_file: Path) -> dict | None:
    """Метаданные расхода из транскрипта. None если файл нечитаем/непарсируем целиком.

    Читается построчно; ни одно поле с текстом (`content`, `message.content`, `toolUseResult`) не
    берётся — наружу уходят только числа, timestamps, id и рабочий каталог.
    """
    order: list[str] = []
    turns: dict[str, dict] = {}
    first_ts = last_ts = None
    project_dir = None
    compactions = 0
    last_compaction_at = None
    last_compaction_trigger = None
    parsed_lines = 0

    try:
        with session_file.open("r", encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f):
                if not any(h in line for h in _LINE_HINTS) and "compact_boundary" not in line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue  # повреждённую строку пропускаем, остальное измеряем
                if not isinstance(obj, dict):
                    continue
                parsed_lines += 1

                ts = obj.get("timestamp")
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                if project_dir is None and obj.get("cwd"):
                    project_dir = obj["cwd"]

                # Компакция — настоящий маркер рантайма, а не вывод из падения контекста.
                if obj.get("subtype") == "compact_boundary":
                    compactions += 1
                    last_compaction_at = ts or last_compaction_at
                    meta = obj.get("compactMetadata")
                    if isinstance(meta, dict):
                        last_compaction_trigger = meta.get("trigger") or last_compaction_trigger
                    continue

                if obj.get("type") != "assistant" or obj.get("isSidechain"):
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict) or not usage:
                    continue

                # Стриминг пишет один ход несколькими строками с одним `message.id`; последняя
                # строка хода несёт итоговый usage.
                key = str(msg.get("id") or obj.get("requestId") or f"line-{lineno}")
                if key not in turns:
                    order.append(key)
                turns[key] = _turn_usage(usage)
    except OSError:
        return None

    if parsed_lines == 0:
        return None  # нечего измерять — честно unavailable, а не нули

    seq = [turns[k] for k in order]
    if seq:
        context_current = seq[-1]["context"]
        context_peak = max(t["context"] for t in seq)
        context_status = "measured"
    else:
        # Файл есть, но ни одного завершённого хода модели — размер контекста вывести не из чего.
        context_current = context_peak = None
        context_status = "unavailable"

    def _sum(field):
        return sum(t[field] for t in seq)

    total = _sum("input_tokens") + _sum("cache_read_tokens") + _sum("cache_write_tokens") + _sum("output_tokens")
    return {
        "session_id": session_file.stem,
        "transcript": str(session_file),
        "project_dir": project_dir,
        "started_at": first_ts,
        "last_activity_at": last_ts,
        "turns": len(seq),
        "message_count": len(seq),          # прежнее имя того же числа (совместимость)
        "input_tokens": _sum("input_tokens"),
        "output_tokens": _sum("output_tokens"),
        "cache_read_tokens": _sum("cache_read_tokens"),
        "cache_write_tokens": _sum("cache_write_tokens"),
        "cache_status": "measured" if seq else "unavailable",
        "total_tokens": total if seq else None,
        "total_tokens_status": "measured" if seq else "unavailable",
        "context_current": context_current,
        "context_peak": context_peak,
        "context_status": context_status,
        "compactions": compactions,
        "last_compaction_at": last_compaction_at,
        "last_compaction_trigger": last_compaction_trigger,
        "last_compaction_status": "measured" if last_compaction_at else "unavailable",
        "source": "claude-code-local",
    }


def read_session_metadata(session_id: str | None = None, project_dir: str | None = None) -> dict | None:
    """Расход текущей сессии, измеренный по транскрипту. None если сессию не нашли.

    `project_dir` — каталог репозитория, по которому выводится каталог проекта рантайма. Без него и
    без id сессии соответствия нет, и тогда ответ None: подставлять чью-то другую сессию нельзя.
    """
    session_id = session_id or _env(ENV_SESSION_ID_KEYS)
    project_dir = project_dir or _env(ENV_PROJECT_DIR_KEYS)

    claude_home = _find_claude_home()
    if not claude_home:
        return None
    found = find_session_file(claude_home, session_id, project_dir)
    if not found:
        return None
    session_file, discovered_via = found
    data = parse_session_metadata(session_file)
    if data is None:
        return None
    data["discovered_via"] = discovered_via
    return data


def lookup_reason(session_id: str | None = None, project_dir: str | None = None) -> str:
    """ПОЧЕМУ расход не измерен. «Не нашли» и «сессия не идёт» — разные ответы.

    Правильный ответ «нет данных», выданный по неверной причине, чинить нечем: человек не знает,
    сломан ли путь, не задан ли id или рантайм действительно не пишет транскрипт. Поэтому причина
    называется словами, а не выводится из пустоты.
    """
    session_id = session_id or _env(ENV_SESSION_ID_KEYS)
    project_dir = project_dir or _env(ENV_PROJECT_DIR_KEYS)
    claude_home = _find_claude_home()
    if not claude_home:
        return f"каталога рантайма {Path.home() / '.claude'} нет — сессия Claude Code здесь не идёт"
    if not (claude_home / "projects").is_dir():
        return f"в {claude_home} нет каталога projects — транскрипты сессий не пишутся"
    if not session_id and not project_dir:
        return ("не задан ни id сессии (ENV " + "/".join(ENV_SESSION_ID_KEYS) + "), ни каталог "
                "репозитория — искать сессию наугад нельзя, подставилась бы чужая")
    if session_id:
        return (f"транскрипта сессии {session_id}.jsonl нет ни в одном каталоге "
                f"{claude_home / 'projects'}")
    slugs = ", ".join(_slug_candidates(project_dir))
    return (f"для {project_dir} нет каталога сессий (искали {slugs}) — сессия рантайма запущена из "
            "другого каталога; передай id сессии через ENV "
            + "/".join(ENV_SESSION_ID_KEYS))


def check(data):
    """Валидация формы + честности: unavailable не притворяется нулём."""
    if data is None:
        return []  # None — честный unavailable
    e = []
    required = ("session_id", "started_at", "message_count", "input_tokens", "output_tokens")
    for key in required:
        if key not in data:
            e.append(f"missing required key: {key}")
    for key in ("message_count", "input_tokens", "output_tokens"):
        v = data.get(key)
        if isinstance(v, int) and v < 0:
            e.append(f"{key} < 0")
    if data.get("context_status") == "unavailable" and data.get("context_current") is not None:
        e.append("context_status=unavailable, но context_current не None (unknown как значение)")
    if data.get("context_status") == "measured" and data.get("context_current") is None:
        e.append("context_status=measured, но context_current отсутствует")
    return e


def _fmt(d: dict) -> str:
    def tok(n):
        return "н/д" if n is None else (f"{n / 1000:.0f}k" if n >= 1000 else str(n))
    L = [f"Сессия: {d.get('session_id')} (найдена: {d.get('discovered_via')})",
         f"  рабочий каталог: {d.get('project_dir') or 'н/д'}",
         f"  начата: {d.get('started_at') or 'н/д'}   последняя активность: {d.get('last_activity_at') or 'н/д'}",
         f"  контекст (тек/пик): {tok(d.get('context_current'))}/{tok(d.get('context_peak'))} "
         f"[{d.get('context_status')}]",
         f"  ходов: {d.get('turns')}   всего токенов: {tok(d.get('total_tokens'))}",
         f"  cache чтение/запись: {tok(d.get('cache_read_tokens'))}/{tok(d.get('cache_write_tokens'))} "
         f"[{d.get('cache_status')}]"]
    if d.get("last_compaction_at"):
        L.append(f"  последняя компакция: {d['last_compaction_at']} "
                 f"({d.get('last_compaction_trigger') or 'trigger н/д'}), всего: {d.get('compactions')}")
    else:
        L.append("  компакция: не обнаружена в транскрипте [unavailable]")
    return "\n".join(L)


def main(argv):
    session_id = project_dir = None
    it = iter(argv)
    for a in it:
        if a == "--session-id":
            session_id = next(it, None)
        elif a == "--project-dir":
            project_dir = next(it, None)
    result = read_session_metadata(session_id, project_dir)
    if "--json" in argv:
        print(json.dumps(result, ensure_ascii=False, indent=2) if result else "null")
    elif result:
        print(_fmt(result))
    else:
        print(f"Расход сессии: НЕ ИЗМЕРЕН — {lookup_reason(session_id, project_dir)}")
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
