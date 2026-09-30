"""Handoff работы между ролями-владельцами — named-переход `owner_role` (issue #639, первый клин).

Work не «лежит в backlog»: у неё есть роль-владелец на каждом этапе, и переход между владельцами —
ЯВНОЕ событие с причиной и брифом для следующего. PM закончил discovery → кит передаёт дизайнеру;
дизайнер закончил UX → кит передаёт имплементатору. Это ПЕРВЫЙ ограниченный шаг темы «AI-команда
как люди», а не полный Team OS: передаётся РОЛЬ, не человек. Сущностей Person / Worker / Availability
здесь нет — они горизонт, откладываемый до подтверждения живым прогоном.

Роль→роль, НЕ человек→человек — сознательно. Поле `assignee`/исполнитель запрещено инвариантом
модели (`release-notes.yaml` FORBIDDEN_ITEM_KEYS, `delivery_plan` его энфорсит): план называет
роль, а какой worker ей соответствует сейчас, выбирает роутер в момент Run. Handoff держит ту же
границу.

Живёт в `lifecycle` (ядро). `planning` ядру импортировать ЗАПРЕЩЕНО (kernel forbidden_imports),
поэтому словарь ролей читается ПО КОНТРАКТУ — прямой yaml-загрузкой того же
`registry/product-operating-model.yaml`, по которому `delivery_plan` валидирует `owner_role` плана,
а не импортом `planning.contours`. Форма файла — публичный реестр, чтение по нему устойчиво.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

_MODEL_REL = "registry/product-operating-model.yaml"


def _pkg_root() -> Path | None:
    """Корень пакета движка (маркер VERSION): в ките — репозиторий, в дочке — .ai/managed."""
    for p in Path(__file__).resolve().parents:
        if (p / "VERSION").is_file():
            return p
    return None


def roles_vocab() -> set:
    """Множество допустимых ролей из модели — тот же словарь, что валидирует `owner_role` плана.

    Пусто означает «словарь недоступен» (registry не прочитан), а НЕ «ролей нет»: вызывающий обязан
    отличать эти случаи, иначе непрочитанный реестр молча разрешил бы любую роль.
    """
    root = _pkg_root()
    if root is None:
        return set()
    try:
        data = yaml.safe_load((root / _MODEL_REL).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return set()
    return set((data.get("roles") or {}).keys())


def validate_role(role, vocab=None):
    """(ok, err). Роль обязана быть из словаря модели — иначе это исполнитель/выдумка, не роль."""
    vocab = roles_vocab() if vocab is None else vocab
    if not vocab:
        return False, "словарь ролей недоступен (registry/product-operating-model.yaml не прочитан)"
    if role not in vocab:
        return False, f"роль '{role}' вне словаря ролей модели ({', '.join(sorted(vocab))})"
    return True, None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def apply_handoff(entry: dict, to_role, reason, session, at=None, vocab=None):
    """Чистый переход `owner_role` работы -> (new_entry, err). Ничего не пишет на диск.

    Роль→роль: `to_role` из словаря; `reason` обязателен — передача без брифа это не передача, а
    молчаливая смена владельца, которую следующая сессия не поймёт. Прежний владелец уходит в запись
    перехода (`handoffs[]`), атрибуция не теряется. Возвращает НОВЫЙ dict, исходный не мутирует.
    """
    ok, err = validate_role(to_role, vocab)
    if not ok:
        return None, err
    if not (reason or "").strip():
        return None, "handoff без причины/брифа: следующему владельцу нужно знать, ЧТО передаётся"
    from_role = entry.get("owner_role")
    if from_role == to_role:
        return None, f"работа уже принадлежит роли '{to_role}' — передавать нечего"
    record = {"from": from_role, "to": to_role, "reason": reason.strip(),
              "at": at or _now_iso(), "session": session}
    new_entry = dict(entry)
    new_entry["owner_role"] = to_role
    new_entry["handoffs"] = list(entry.get("handoffs") or []) + [record]
    return new_entry, None
