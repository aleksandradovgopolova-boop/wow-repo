"""Детектор CONFLICTING — когда источники истины ПРОТИВОРЕЧАТ друг другу об одном факте.

Восьмое состояние знания рядом с семью из `repo_audit` (`verified/inferred/partial/missing/
unknown/stale/user_confirmed`). Те семь отвечают на вопрос «насколько я уверен в ОДНОМ источнике».
Это — на другой вопрос: «а что, если источников несколько и они говорят РАЗНОЕ?».

Классический кейс продуктового ревью: README пишет «PostgreSQL», ARCHITECTURE пишет «SQLite», а
зависимости кода тянут `psycopg2`. Молча выбрать сторону — соврать: неизвестно, какой документ
отстал. Честный ответ — CONFLICTING: «источники противоречат, определить актуальный не могу»,
С ПЕРЕЧИСЛЕНИЕМ расходящихся источников и того, что каждый утверждает.

Это НЕ то же, что `drift` (`planning/drift.py`): drift — «код изменился, описание отстало» (одно
направление, известно кто новее). CONFLICTING — «два описания активно спорят», направление
неизвестно принципиально. Обе находки advisory: кит показывает, не чинит.

Осознанно узкий первый охват — ОДНА категория (СУБД), потому что это ровно пример ревью и потому
что ложная тревога здесь дороже молчания: детектор поднимает CONFLICTING только при
ДОКАЗУЕМОМ расхождении (два источника называют НЕПЕРЕСЕКАЮЩИЕСЯ множества), а не при любом
несовпадении слов. «Мигрировали с SQLite на PostgreSQL» в одном README — не конфликт: множество
{sqlite, postgres} пересекается с обоими, консенсус не нарушен.
"""
from __future__ import annotations

import re
from pathlib import Path

from ai_ops_kit.planning.canonical_docs import ARCHITECTURE_PATHS as _ARCH

CONFLICTING = "conflicting"

# Максимум символов на документ: источники истины — проза, а не дампы; читать целиком незачем,
# и защита от случайного гигантского файла в дереве.
_MAX_DOC_CHARS = 40000

# Канонические СУБД и как они называются в прозе. Слова с границами (\b), чтобы «my» не ловило
# mysql и «no» не ловило mongo. Порядок не важен — множество на выходе.
_DB_PROSE = {
    "postgres": (r"postgres", r"postgresql", r"\bpg\b"),
    "sqlite": (r"sqlite",),
    "mysql": (r"mysql", r"mariadb"),
    "mongodb": (r"mongodb", r"\bmongo\b"),
    "redis": (r"redis",),
    "cassandra": (r"cassandra",),
    "dynamodb": (r"dynamodb", r"dynamo db"),
}

# Драйверы/пакеты в манифестах зависимостей -> канональная СУБД. Это «голос кода»: не проза о
# намерении, а факт того, что тянется на исполнение. `sqlite3` НЕ включён — это stdlib Python,
# его присутствие в коде ничего не доказывает о продукте (ложный голос).
_DB_DRIVER = {
    "postgres": (r"psycopg2?", r"asyncpg", r"pg-promise", r"\bpg\b", r"node-postgres"),
    "sqlite": (r"better-sqlite3", r"aiosqlite", r"sqlalchemy-sqlite"),
    "mysql": (r"mysqlclient", r"pymysql", r"mysql2", r"mysql-connector"),
    "mongodb": (r"pymongo", r"mongoose", r"motor\b", r"mongodb"),
    "redis": (r"redis", r"ioredis", r"aioredis"),
    "cassandra": (r"cassandra-driver",),
    "dynamodb": (r"boto3",),  # слабый сигнал — boto3 тянут и без dynamo; поэтому dynamo в прозе тоже
}

_README = ("README.md", "README.rst", "README.txt", "README")
# `_ARCH` (импортирован выше) — единый список частых мест архитектурного документа (canonical_docs):
# тот же, что использует back-fill установщика, чтобы не сеять черновик поверх реального
# `docs/architecture/…`.
_MANIFESTS = ("requirements.txt", "pyproject.toml", "Pipfile", "setup.cfg", "setup.py",
              "package.json", "go.mod", "Gemfile", "pom.xml", "build.gradle", "Cargo.toml")


def _read(root: Path, rel: str) -> str | None:
    p = root / rel
    if not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:_MAX_DOC_CHARS].lower()
    except OSError:
        return None


def _match(text: str, vocab: dict) -> set:
    """Множество канонических значений, названных в тексте (по границам слова)."""
    found = set()
    for canon, patterns in vocab.items():
        for pat in patterns:
            if re.search(pat, text):
                found.add(canon)
                break
    return found


def _doc_claim(root: Path, rels, vocab: dict):
    """Первый существующий документ из `rels` и что он называет. -> (rel, values) | None."""
    for rel in rels:
        text = _read(root, rel)
        if text is None:
            continue
        return rel, _match(text, vocab)
    return None


def _code_claim(root: Path):
    """Что называют манифесты зависимостей (голос кода). -> (rel, values) | None."""
    values, seen = set(), None
    for rel in _MANIFESTS:
        text = _read(root, rel)
        if text is None:
            continue
        seen = seen or rel
        values |= _match(text, _DB_DRIVER)
    if seen is None:
        return None
    return seen, values


def _sources(root: Path) -> list:
    """Голоса об СУБД: README, ARCHITECTURE, код. Только те, что существуют И называют хоть одну."""
    voices = []
    for label, claim in (("README", _doc_claim(root, _README, _DB_PROSE)),
                         ("ARCHITECTURE", _doc_claim(root, _ARCH, _DB_PROSE)),
                         ("код (зависимости)", _code_claim(root))):
        if claim is None:
            continue
        rel, values = claim
        if values:
            voices.append({"source": label, "path": rel, "values": sorted(values)})
    return voices


def _disjoint_pair(voices) -> bool:
    """Есть ли два голоса с непересекающимися множествами. Строгий критерий конфликта: не «слова не
    совпали», а «нет ни одной общей СУБД» — тогда молчаливый выбор был бы выдумкой."""
    for i in range(len(voices)):
        for j in range(i + 1, len(voices)):
            if not (set(voices[i]["values"]) & set(voices[j]["values"])):
                return True
    return False


def detect(child_root) -> list:
    """Список конфликтов источников истины. Пустой — противоречий не найдено (НЕ «нет источников»).

    -> [{"category": "database", "state": "conflicting", "claims": [{source, path, values}...],
         "summary": "..."}]

    Молчаливого выбора нет по построению: находка перечисляет КАЖДЫЙ голос и что он называет.
    """
    root = Path(child_root)
    out = []
    voices = _sources(root)
    # Конфликт только при >=2 голосах И доказуемом расхождении (непересекающаяся пара).
    if len(voices) >= 2 and _disjoint_pair(voices):
        named = "; ".join(f"{v['source']}: {', '.join(v['values'])}" for v in voices)
        out.append({
            "category": "database", "state": CONFLICTING, "claims": voices,
            "summary": f"источники называют разные СУБД ({named}) — определить актуальную не могу, "
                       f"выбор за владельцем"})
    return out
