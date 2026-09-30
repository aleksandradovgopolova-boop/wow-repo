#!/usr/bin/env python3
"""AI Decision Log (PR-18): значимые решения AI — что решено, почему, на каких данных, результат,
менял ли человек.

РАСШИРЯЕТ существующий `decisions/registry.yaml`, а НЕ заводит второй реестр (dp-001). Решение AI
кладётся как обычный эпизод (id/question/decision/reason/reversibility/date — то, что проверяет
validate_decisions) плюс AI-поля: `actor: ai`, `data` (на каких данных), `outcome` (результат),
`human_overrode` (менял ли человек).

ПОЧЕМУ ТЕКСТОВАЯ ВСТАВКА, А НЕ safe_dump ВСЕГО ФАЙЛА. Реестр вручную прокомментирован (принципы,
пояснения); `yaml.safe_dump` стёр бы все комментарии, а ruamel в кит не завозим (только
stdlib+yaml). Поэтому новый эпизод вставляется ТЕКСТОМ в конец блока `episodes:` — комментарии и
прочее содержимое остаются нетронутыми. После вставки файл перечитывается: если он не разбирается,
эпизода в нём нет или id не уникален — запись НЕ происходит (fail-closed).

Пакет governance — лист: этот модуль не импортирует другие пакеты ai_ops_kit (проверку полей
делает inline), поэтому в layering.yaml рёбер не добавляет.

Использование:  python3 -m ai_ops_kit.governance.decision_log <repo_root> --list
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

REGISTRY_REL = "decisions/registry.yaml"
AI_ACTOR = "ai"
REVERSIBILITY = ("one-way", "two-way")          # набор validate_decisions
CONFIDENCE = ("low", "medium", "high")          # калибровка, как у принципов
_REQUIRED = ("id", "question", "decision", "reason", "reversibility", "date")
_EXPECTED_OUTCOME_FIELDS = ("metric", "baseline", "target")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# начало строки верхнего уровня (не отступ, не комментарий) — граница секции YAML
_TOP_KEY = re.compile(r"^[^\s#]")


class RegistryError(Exception):
    """Реестр решений недостоверен или запись сделала бы его недостоверным. Fail-closed."""


def build_ai_episode(*, decision_id: str, question: str, decision: str, reason: str,
                     date: str, data: str, outcome=None, human_overrode: bool = False,
                     reversibility: str = "two-way", context=None,
                     confidence=None, expected_outcome=None, review_at=None) -> dict:
    """Собрать AI-эпизод. Порядок полей сохраняется при сериализации.

    Необязательные поля калибровки (все опциональны, отсутствие — норма):
      - confidence: насколько уверены в решении (low|medium|high);
      - expected_outcome: ожидаемый исход как измеримое обязательство — объект
        с непустыми metric (что двигаем), baseline (где сейчас), target (куда идём);
      - review_at: дата (YYYY-MM-DD), когда стоит сверить ожидание с фактом.
    """
    ep = {
        "id": decision_id,
        "actor": AI_ACTOR,
        "question": question,
        "decision": decision,
        "reason": reason,
        "data": data,
        "human_overrode": bool(human_overrode),
        "reversibility": reversibility,
        "date": date,
    }
    if confidence is not None:
        ep["confidence"] = confidence
    if expected_outcome is not None:
        ep["expected_outcome"] = expected_outcome
    if review_at is not None:
        ep["review_at"] = review_at
    if outcome is not None:
        ep["outcome"] = outcome
    if context is not None:
        ep["context"] = context
    errs = _episode_errors(ep)
    if errs:
        raise RegistryError("AI-эпизод недостоверен: " + "; ".join(errs))
    return ep


def _expected_outcome_errors(exp) -> list:
    """expected_outcome — измеримое обязательство: непустые metric/baseline/target."""
    if not isinstance(exp, dict):
        return ["expected_outcome должен быть объектом (metric/baseline/target)"]
    return [f"expected_outcome.{f} пусто" for f in _EXPECTED_OUTCOME_FIELDS
            if exp.get(f) in (None, "")]


def _episode_errors(ep: dict) -> list:
    errs = []
    for f in _REQUIRED:
        if not ep.get(f):
            errs.append(f"нет поля {f}")
    if ep.get("reversibility") not in REVERSIBILITY:
        errs.append(f"reversibility '{ep.get('reversibility')}' не в {REVERSIBILITY}")
    if not (isinstance(ep.get("date"), str) and _DATE_RE.match(ep["date"])):
        errs.append("date должен быть строкой YYYY-MM-DD")
    # опциональная калибровка: проверяем форму, только если поле присутствует
    conf = ep.get("confidence")
    if conf is not None and conf not in CONFIDENCE:
        errs.append(f"confidence '{conf}' не в {CONFIDENCE}")
    ra = ep.get("review_at")
    if ra is not None and not (isinstance(ra, str) and _DATE_RE.match(ra)):
        errs.append("review_at должен быть строкой YYYY-MM-DD")
    exp = ep.get("expected_outcome")
    if exp is not None:
        errs.extend(_expected_outcome_errors(exp))
    return errs


def _registry_path(root: Path, registry_rel: str) -> Path:
    return Path(root) / registry_rel


def _episodes_block_end(lines: list, start: int) -> int:
    """Индекс строки, на которой заканчивается блок `episodes:` (следующая секция верхнего уровня
    или конец файла)."""
    for i in range(start + 1, len(lines)):
        if _TOP_KEY.match(lines[i]):
            return i
    return len(lines)


def append_ai_decision(root: Path, episode: dict, registry_rel: str = REGISTRY_REL) -> None:
    """Вставить AI-эпизод текстом в блок episodes, сохранив комментарии. Fail-closed на любой
    недостоверности результата."""
    path = _registry_path(root, registry_rel)
    if not path.is_file():
        raise RegistryError(f"{registry_rel} не найден — расширять нечего")
    errs = _episode_errors(episode)
    if errs:
        raise RegistryError("AI-эпизод недостоверен: " + "; ".join(errs))

    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    existing = data.get("episodes")
    if not isinstance(existing, list):
        raise RegistryError(f"{registry_rel}: секция episodes не список")
    if any((e or {}).get("id") == episode["id"] for e in existing):
        raise RegistryError(f"id '{episode['id']}' уже есть в реестре — эпизоды не перезаписываем")

    lines = text.splitlines(keepends=True)
    ep_line = next((i for i, ln in enumerate(lines) if ln.rstrip("\n") == "episodes:"), None)
    if ep_line is None:
        raise RegistryError(f"{registry_rel}: нет секции 'episodes:'")
    end = _episodes_block_end(lines, ep_line)

    dumped = yaml.safe_dump([episode], sort_keys=False, allow_unicode=True)
    block = "".join("  " + ln if ln.strip() else ln for ln in dumped.splitlines(keepends=True))
    if lines and not lines[end - 1].endswith("\n"):
        lines[end - 1] += "\n"
    new_text = "".join(lines[:end]) + block + "".join(lines[end:])

    # перепроверка: файл разбирается, эпизод на месте ровно один раз, поля достоверны
    reparsed = yaml.safe_load(new_text) or {}
    got = [e for e in (reparsed.get("episodes") or []) if (e or {}).get("id") == episode["id"]]
    if len(got) != 1 or _episode_errors(got[0]):
        raise RegistryError("после вставки реестр недостоверен — запись отменена")
    path.write_text(new_text, encoding="utf-8")


def log_ai_decision(root: Path, *, registry_rel: str = REGISTRY_REL, **fields) -> dict:
    """Собрать AI-эпизод из полей и дописать в реестр. -> записанный эпизод."""
    episode = build_ai_episode(**fields)
    append_ai_decision(root, episode, registry_rel=registry_rel)
    return episode


def ai_decisions(root: Path, registry_rel: str = REGISTRY_REL) -> list:
    """Все AI-эпизоды реестра (actor == ai)."""
    path = _registry_path(root, registry_rel)
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [e for e in (data.get("episodes") or []) if (e or {}).get("actor") == AI_ACTOR]


def main(argv) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    root = Path(args[0])
    if not root.is_dir():
        print(f"не каталог: {root}", file=sys.stderr)
        return 2
    if "--list" in argv:
        print(json.dumps(ai_decisions(root), indent=2, ensure_ascii=False))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
