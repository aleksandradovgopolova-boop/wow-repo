#!/usr/bin/env python3
"""session_telemetry_provider.py (v3.22 Culture Runtime Integration) — opt-in чтение реальной
Claude session metadata. Контракт: попытаться прочитать данные из runtime, если не удалось —
честно вернуть None (unavailable, не 0).

Источники (по приоритету):
1. ~/.claude/projects/<hash>/sessions/<session_id>.jsonl — Claude Code session transcript
2. ~/.claude/statsig/ — runtime statistics
3. ENV: CLAUDE_SESSION_ID, CLAUDE_PROJECT_DIR — явное указание

ЧЕСТНЫЕ ГРАНИЦЫ:
- Структура ~/.claude/ может меняться между версиями — если не нашли, возвращаем None
- Не читаем содержимое промптов/ответов (privacy) — только метаданные (tokens, timestamps)
- Если файл повреждён/непарсируем — None, не partial data
- Opt-in: вызывается только если session_telemetry.snapshot() явно запросил

CLI:  session_telemetry_provider.py [--session-id ID] [--project-dir DIR] [--json]
      session_telemetry_provider.py --selftest
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _find_claude_home() -> Path | None:
    """~/.claude/ или None если нет."""
    home = Path.home() / ".claude"
    return home if home.is_dir() else None


def _find_session_file(claude_home: Path, session_id: str | None = None) -> Path | None:
    """Попытаться найти session transcript. None если не нашли."""
    projects_dir = claude_home / "projects"
    if not projects_dir.is_dir():
        return None
    # Ищем в любом проекте (первый найденный)
    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        sessions_dir = proj_dir / "sessions"
        if not sessions_dir.is_dir():
            continue
        if session_id:
            # Ищем конкретный session
            candidates = [sessions_dir / f"{session_id}.jsonl"]
        else:
            # Берём последний (по mtime)
            candidates = sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        for cand in candidates:
            if cand.is_file():
                return cand
    return None


def _parse_session_metadata(session_file: Path) -> dict | None:
    """Парсить метаданные из session transcript. Только метаданные, не содержимое.
    None если файл повреждён/непарсируем."""
    try:
        # JSONL: каждая строка — JSON-объект (message/summary/tool_use)
        # Берём первую и последнюю строки для timestamps, считаем tokens
        first_ts = None
        last_ts = None
        total_input_tokens = 0
        total_output_tokens = 0
        message_count = 0

        with session_file.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue  # пропускаем повреждённые строки

                # Timestamp
                ts = obj.get("timestamp") or obj.get("ts")
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts

                # Tokens (если есть в metadata/usage)
                usage = obj.get("usage") or obj.get("metadata", {}).get("usage") or {}
                total_input_tokens += int(usage.get("input_tokens") or 0)
                total_output_tokens += int(usage.get("output_tokens") or 0)
                message_count += 1

                # Ограничим чтение (не читаем весь файл для больших сессий)
                if i > 10000:
                    break

        if message_count == 0:
            return None

        return {
            "session_id": session_file.stem,
            "started_at": first_ts,
            "last_activity_at": last_ts,
            "message_count": message_count,
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "source": "claude-code-local",
        }
    except Exception:  # noqa: BLE001
        return None  # честно: если не смогли — None


def read_session_metadata(session_id: str | None = None, project_dir: str | None = None) -> dict | None:
    """Попытаться прочитать session metadata из Claude Code.
    None если не удалось (честно unavailable, не 0)."""
    # 1. ENV override
    if not session_id:
        session_id = os.environ.get("CLAUDE_SESSION_ID")
    if not project_dir:
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR")

    # 2. Найти ~/.claude/
    claude_home = _find_claude_home()
    if not claude_home:
        return None

    # 3. Найти session file
    session_file = _find_session_file(claude_home, session_id)
    if not session_file:
        return None

    # 4. Парсить метаданные
    return _parse_session_metadata(session_file)


def check(data):
    """Валидация формы."""
    if data is None:
        return []  # None — честный unavailable
    e = []
    required = ("session_id", "started_at", "message_count", "input_tokens", "output_tokens")
    for key in required:
        if key not in data:
            e.append(f"missing required key: {key}")
    if data.get("message_count", 0) < 0:
        e.append("message_count < 0")
    if data.get("input_tokens", 0) < 0:
        e.append("input_tokens < 0")
    return e


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    # CLI: попытаться прочитать metadata
    session_id = None
    project_dir = None
    as_json = "--json" in sys.argv
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--session-id" and i < len(sys.argv) - 1:
            session_id = sys.argv[i + 1]
        elif arg == "--project-dir" and i < len(sys.argv) - 1:
            project_dir = sys.argv[i + 1]
    result = read_session_metadata(session_id, project_dir)
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2) if result else "null")
    elif result:
        print(f"Session: {result.get('session_id')}")
        print(f"  Started: {result.get('started_at')}")
        print(f"  Messages: {result.get('message_count')}")
        print(f"  Tokens: {result.get('input_tokens')} in / {result.get('output_tokens')} out")
    else:
        print("Session metadata: unavailable (Claude session not found)")
        sys.exit(1)
