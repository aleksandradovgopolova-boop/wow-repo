#!/usr/bin/env python3
"""Разговорные вопросы и ответы ОБЫЧНЫМИ СЛОВАМИ для описания задачи (#863).

ПРОБЛЕМА (живой zero-touch прогон): `specify` заводит `features/<wid>/spec.yaml` и говорит
владельцу «заполни разделы в <путь>» — то есть иди и правь YAML руками. Путь описания задачи не
обязан показывать владельцу файл вовсе: кит может спросить продуктовым языком («зачем эта
задача?», «как поймём, что готово?») и сам записать ответ в нужный раздел.

Здесь — ДВЕ вещи, и обе намеренно НЕ знают про `ai_ops_kit.gates.spec_levels` (層 layering:
`shared` — основание, `gates` — выше; наоборот нельзя, и в обе стороны — тоже, поэтому имена
разделов ниже — СВОИ константы, а не импорт чужих):

  * `SECTION_QUESTIONS` — как назвать раздел спецификации человеческим вопросом, и каким коротким
    словом на него отвечать (`--answers "<слово>=<ответ>; …"`). Читает `ui.presenter_formatters`
    (сообщение) и `cli.*` (разбор `--answers`) — оба ниже/на одном слое с `shared`, вверх не
    смотрят;
  * `parse_plain_answers` / `apply_answers` — сам разбор и запись ответа в `spec.yaml`. Пишет тем
    же способом, что и `spec_levels._render_spec` (шапка со словарём статусов + YAML), но не
    трогает уже заполненные разделы и не меняет уровень/`signals` — это работа `create_spec`.

НЕ ВЫДУМЫВАЕТ СОДЕРЖАНИЕ. Разбирает только то, что владелец реально написал; раздел, слово для
которого не нашлось (`unmatched`), называется вызывающему честно, а не молчаливо пропускается.
"""
from __future__ import annotations

from pathlib import Path
from collections.abc import Iterable

# section_id -> (короткое слово ответа, вопрос человеческим языком).
# Короткое слово — это же ключ, который понимает `parse_plain_answers` в `--answers
# "<слово>=<ответ>"`. Сам `section_id` тоже распознаётся как ключ (см. `_alias_lookup`) — тому, кто
# уже знает словарь spec.yaml, не нужно подбирать русское слово.
SECTION_QUESTIONS = {
    # L0 QUICK — база любой задачи
    "goal": ("зачем", "Зачем эта задача? Какую пользу она приносит?"),
    "scope": ("что", "Что именно нужно сделать (в двух словах — объём работы)?"),
    "expected_behavior": ("поведение", "Как всё должно вести себя после того, как задача сделана?"),
    "acceptance_criteria": ("как-поймём", "Как поймём, что задача готова?"),
    "constraints": ("ограничения", "Что нельзя трогать или ломать, делая эту задачу?"),
    "affected_files": ("файлы", "Какие места/файлы это затрагивает (если знаешь)?"),
    # L1 ENGINEERING
    "requirements": ("требования", "Какие конкретные требования должны выполняться?"),
    "acceptance_scenarios": ("сценарии", "Опиши сценарий: что делает пользователь и что видит в ответ?"),
    "contracts": ("контракты", "Какие интерфейсы/контракты (API, форматы) это задевает?"),
    "dependencies": ("зависимости", "От чего это зависит и что зависит от этого?"),
    "edge_cases": ("крайние-случаи", "Какие крайние/редкие случаи стоит предусмотреть?"),
    "architectural_constraints": ("архи-ограничения", "Какие архитектурные ограничения тут действуют?"),
    "implementation_plan": ("план", "Как по шагам предполагается это сделать?"),
    "write_scope": ("область-правок", "Какие файлы/области разрешено менять?"),
    "verification_strategy": ("проверка", "Чем и как это будет проверяться?"),
    # L2 PRODUCT
    "problem": ("проблема", "Чья это боль и в чём она?"),
    "users_jtbd": ("пользователи", "Кто пользователи и какую задачу они этим решают?"),
    "value": ("ценность", "В чём ценность для пользователя или бизнеса?"),
    "current_scenario": ("сейчас", "Как это работает сейчас (до задачи)?"),
    "target_scenario": ("будет", "Как это должно работать после задачи?"),
    "hypotheses": ("гипотезы", "Какая гипотеза стоит за этой задачей (если … то … потому что …)?"),
    "success_metrics": ("метрики", "По какому сигналу поймём, что задача сработала?"),
    "ux_states": ("состояния", "Какие состояния интерфейса это затрагивает (загрузка/ошибка/пусто)?"),
    "analytics": ("аналитика", "Что нужно замерять/логировать?"),
    "rollout": ("раскатка", "Как это раскатывать — сразу всем или постепенно?"),
    "risks": ("риски", "Какие риски у этой задачи?"),
    # L3 CRITICAL
    "threat_model": ("угрозы", "Какие угрозы безопасности здесь стоит учесть?"),
    "rollback_plan": ("откат", "Как откатить изменение, если что-то пошло не так?"),
    "migration_plan": ("миграция", "Нужна ли миграция данных/схемы и какая?"),
    "failure_modes": ("отказы", "Что может сломаться и как это будет выглядеть?"),
    "audit_requirements": ("аудит", "Что здесь должно быть прослеживаемо/аудируемо?"),
    "human_approvals": ("одобрения", "Чьё одобрение здесь обязательно перед выпуском?"),
    "compliance_constraints": ("комплаенс", "Какие требования комплаенса это задевает?"),
    "disaster_recovery": ("авария", "Как восстанавливаться после серьёзного сбоя?"),
}


def _normalize(key: str) -> str:
    return (key or "").strip().lower().replace("ё", "е")


def _alias_lookup() -> dict:
    """slovo/section_id (нормализованные) -> section_id."""
    out = {}
    for sid, (word, _question) in SECTION_QUESTIONS.items():
        out[_normalize(word)] = sid
        out[_normalize(sid)] = sid
    return out


def questions_for(section_ids: Iterable[str]) -> list[str]:
    """Разделы -> человеческие вопросы (в порядке `section_ids`). Раздел без словаря — честная
    заглушка по id (лучше показать техническое имя, чем промолчать о том, что раздел вообще есть)."""
    out = []
    for sid in section_ids or ():
        _word, question = SECTION_QUESTIONS.get(sid, (sid, f"Опиши раздел «{sid}»"))
        out.append(question)
    return out


def answer_words_for(section_ids: Iterable[str]) -> list[str]:
    """Разделы -> короткие слова ответа (для примера `--answers "слово1=...; слово2=..."`)."""
    return [SECTION_QUESTIONS.get(sid, (sid, None))[0] for sid in (section_ids or ())]


def parse_plain_answers(text: str) -> tuple[dict[str, str], list[str]]:
    """`"зачем=нужно клиентам X; как-поймём=тест проходит"` -> ({section_id: value}, [unmatched]).

    Разделитель пар — `;` или перенос строки; внутри пары — первый `=`. Ключ ищется среди коротких
    слов `SECTION_QUESTIONS` И среди самих id разделов (нормализация: нижний регистр, `ё`->`е`,
    обрезка пробелов). Ключ без совпадения идёт в `unmatched` — молчать о нераспознанном ответе
    нельзя, иначе владелец решит, что кит его услышал, а он не услышал.
    """
    lookup = _alias_lookup()
    matched, unmatched = {}, []
    for chunk in (text or "").replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        raw_key, _, value = chunk.partition("=")
        value = value.strip()
        if not value:
            continue
        sid = lookup.get(_normalize(raw_key))
        if sid:
            matched[sid] = value
        else:
            unmatched.append(raw_key.strip())
    return matched, unmatched


# Тот же текст шапки, что у `spec_levels._render_spec` (F-013: словарь статусов прямо в файле).
# Дублируем константой, а не импортом: `shared` не вправе зависеть от `gates` (layering.yaml).
_SECTION_STATUSES_TEXT = "complete | declined | missing | needs_human | not_applicable"


def apply_answers(child_root: str | Path, wid: str, text: str) -> dict:
    """Записать разобранные ответы в `features/<wid>/spec.yaml`: раздел получает
    `status: complete` и введённый текст как `content`. Разделы вне ответа не трогаются.

    Fail-closed: спеки нет или файл битый/без карты разделов -> ничего не пишем, честно называем
    причину (`error`) — потерять уже описанное дороже незакрытого гейта (тот же принцип, что у
    `spec_levels._add_missing_sections`).
    -> {"applied": [id, ...], "unmatched": [ключ, ...], "error": str | None}.
    """
    import yaml

    matched, unmatched = parse_plain_answers(text)
    sp = Path(child_root) / "features" / str(wid) / "spec.yaml"
    if not sp.is_file():
        return {"applied": [], "unmatched": unmatched,
                "error": f"спецификации {sp} ещё нет — сначала запусти specify"}
    try:
        doc = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 — битый spec.yaml не переписываем (см. докстринг)
        return {"applied": [], "unmatched": unmatched, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(doc, dict) or not isinstance(doc.get("sections"), dict):
        return {"applied": [], "unmatched": unmatched,
                "error": "spec.yaml не содержит карты разделов (sections)"}
    if not matched:
        return {"applied": [], "unmatched": unmatched, "error": None}
    sections = doc["sections"]
    for sid, value in matched.items():
        sections[sid] = {"status": "complete", "content": value, "note": None}
    sp.write_text(
        f"# status раздела: {_SECTION_STATUSES_TEXT}\n"
        "# declined требует note с объяснением.\n"
        + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    return {"applied": sorted(matched), "unmatched": unmatched, "error": None}
