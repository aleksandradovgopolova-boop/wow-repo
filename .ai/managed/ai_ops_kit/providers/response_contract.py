#!/usr/bin/env python3
"""Контракт формы ответа там, где ответ модели становится ВЕРДИКТОМ (C2, v3.37).

ПОВОД — ЗАМЕР. Весь AI-слой работал по схеме «промпт -> свободный текст -> разбор»: ни structured
outputs, ни tool-calling провайдера не использовались нигде. Для судейских ролей это влияет прямо —
вердикт гейта извлекался из прозы, и первый же живой прогон корпуса gate-евалов показал цену:
ревью процитировало фигурную скобку, разбор упал, заключение судьи вместе с восемью блокерами было
молча отброшено. Разбор починен (C1), но починка разбора — это лечение симптома: форма ответа
по-прежнему ничем не обеспечивалась.

ЧТО ЗДЕСЬ. Один контракт формы (`REVIEWER_RESULT`), одна честная карта того, кто умеет её
обеспечить механизмом провайдера, и типизированный ОТКАЗ на случай, когда ответа нужной формы не
получилось. Три состояния поддержки, и третье не сворачивается во второе:

  * `enforced`   — провайдер гарантирует СХЕМУ (Anthropic `output_config.format`,
                   OpenAI `response_format: json_schema`). Ответ либо по схеме, либо его нет.
  * `json_only`  — провайдер гарантирует, что ответ ВАЛИДНЫЙ JSON, но не его схему (режим
                   `json_object` у openai-совместимых вендоров). Кит всё равно сверяет схему сам и
                   говорит об этом вслух.
  * `unsupported`— механизма нет (`claude-cli`: локальная сессия без API формата). Работает как
                   раньше — проза плюс разбор, — и в отчёт это попадает словом, а не умолчанием.

ЧЕГО ЗДЕСЬ НЕТ НАМЕРЕННО. Ни один путь не выключается из-за отсутствия механизма: `claude-cli`
работает без ключа через локальную сессию и остаётся первоклассным путём. Backoff на транзиентных
отказах (находка поля F-011) живёт в `orchestrator_providers` и здесь не трогается — отказ по форме
и отказ по сети это разные вещи, и повторять надо второе, а не первое.

СХЕМА ОДНА, ПРОЕКЦИЙ ДВЕ. Источник истины — `schemas/reviewer-result.schema.json` (draft-07, его же
сверяет `validate_reviewer_result`). Провайдерам уходит СТРОГАЯ проекция: `additionalProperties:
false` и явный `required` — без них structured outputs не включаются. Две схемы — это две правды,
если их связь не проверяется, поэтому связь проверяет тест
(`tests/unit/test_response_contract_selftest.py`): всё, что принимает проекция, обязан принимать и
реестровый валидатор.
"""
from __future__ import annotations

import json
from pathlib import Path

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
           Path(__file__).resolve().parents[2])

# --- отказ -----------------------------------------------------------------

# Причины отказа перечислены, а не собираются строкой на месте: человек читает их в отчёте, и
# «ответ пуст» отличается от «ответ обрезан» ровно тем, что чинится по-разному.
REFUSAL_REASONS = {
    "empty_answer": "модель вернула пустой ответ",
    "truncated": "ответ обрезан на потолке max_tokens — вердикта в нём нет целиком",
    "shape_violated": "ответ не соответствует объявленной форме",
    "refused_by_model": "модель отказалась отвечать",
}


class ProviderRefusal(Exception):
    """Ответа нужной ФОРМЫ не получилось — и это отказ, а не вердикт.

    Прежде на этом месте возвращалась строка «(пустой ответ модели)»: она доезжала до разбора,
    вердикта в ней не находилось, и гейт краснел с формулировкой «нет заключения reviewer». Гейт
    был прав по существу и врал по причине — человека отправляли искать судью там, где ответ
    обрезало потолком токенов. Отказ несёт свою причину до самого отчёта."""

    def __init__(self, reason: str, detail: str = "", provider: str = "", model: str = ""):
        self.reason = reason
        self.detail = detail
        self.provider = provider
        self.model = model
        base = REFUSAL_REASONS.get(reason, reason)
        where = f" [{provider}{'/' + model if model else ''}]" if provider else ""
        super().__init__(f"{base}{where}{': ' + detail if detail else ''}")

    def as_dict(self) -> dict:
        return {"kind": "provider-refusal", "reason": self.reason,
                "reason_text": REFUSAL_REASONS.get(self.reason, self.reason),
                "detail": self.detail, "provider": self.provider, "model": self.model}


# --- контракт --------------------------------------------------------------

class ResponseContract:
    """Имя формы + схема для провайдера + постфактум-проверка того же."""

    def __init__(self, name: str, wire_schema: dict, required: tuple):
        self.name = name
        self.wire_schema = wire_schema
        self.required = required

    def violations(self, obj) -> list:
        """Проверка ФОРМЫ постфактум — она нужна ВСЕГДА, а не только там, где механизма нет.

        Провайдер с `enforced` обещает схему; обещание проверяется. Гейт, поверивший обещанию без
        проверки, — это ровно «зелёное по декларации», против которого стоит весь кит."""
        if not isinstance(obj, dict):
            return [f"{self.name}: ожидался объект, получен {type(obj).__name__}"]
        errs = [f"{self.name}: нет обязательного поля '{k}'" for k in self.required if k not in obj]
        props = self.wire_schema.get("properties", {})
        for k, v in obj.items():
            spec = props.get(k)
            if spec is None:
                errs.append(f"{self.name}: неизвестное поле '{k}'")
                continue
            enum = spec.get("enum")
            if enum is not None and v not in enum:
                errs.append(f"{self.name}.{k}: '{v}' вне {enum}")
        return errs


def _reviewer_result_wire_schema() -> dict:
    """Строгая проекция `schemas/reviewer-result.schema.json` для structured outputs.

    Почему проекция, а не сам файл: draft-07 с `const` и вложенными необязательными объектами
    провайдеры для constrained decoding не принимают — нужен закрытый объект с явным `required`.
    Связь проекции с источником проверяет тест, чтобы схемы не разъехались молча."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "kind", "gate", "status", "summary", "checks", "blockers"],
        "properties": {
            "schema_version": {"type": "integer"},
            # ЧЕЛОВЕК ЧИТАЕТ РЕВЬЮ, А НЕ JSON. Structured outputs делают весь ответ объектом, так
            # что проза, ради которой ревью и существует, исчезла бы из артефакта стадии. Поле
            # обязательное: заключение без объяснения — это оценка без основания.
            "summary": {"type": "string"},
            "kind": {"type": "string", "enum": ["reviewer-result"]},
            "gate": {"type": "string"},
            "reviewer": {"type": "string"},
            "reviewed_revision": {"type": "string"},
            "status": {"type": "string", "enum": ["pass", "warn", "fail"]},
            "checks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "status"],
                    "properties": {
                        "id": {"type": "string"},
                        "status": {"type": "string", "enum": ["pass", "warn", "fail"]},
                    },
                },
            },
            "blockers": {"type": "array", "items": {"type": "string"}},
        },
    }


REVIEWER_RESULT = ResponseContract(
    name="reviewer-result",
    wire_schema=_reviewer_result_wire_schema(),
    required=("schema_version", "kind", "gate", "status", "checks"),
    # `summary` в required проекции есть, а в постфактум-проверке нет намеренно: реестровая схема
    # его не требует, и старый артефакт без него остаётся валидным. Проекция требует его от НОВЫХ
    # ответов, реестр судит уже записанные — ужесточать задним числом нечестно.
)


def registry_schema(name: str = "reviewer-result") -> dict:
    """Схема-источник из `schemas/` — для теста связи проекции с реестром."""
    return json.loads((PKG / "schemas" / f"{name}.schema.json").read_text(encoding="utf-8"))


# --- карта поддержки -------------------------------------------------------

ENFORCED = "enforced"        # схема гарантирована механизмом провайдера
JSON_ONLY = "json_only"      # гарантирован валидный JSON, схема — нет
UNSUPPORTED = "unsupported"  # механизма нет; проза плюс разбор, как раньше

# КАРТА ЧЕСТНАЯ, А НЕ ОПТИМИСТИЧНАЯ. Здесь перечислено только то, что реализовано адаптером в
# `orchestrator_providers`; вендор, чей режим не проверен нами, объявлен `unsupported`, а не
# «наверное умеет». Завышенная декларация здесь дала бы гейту право сказать «форма обеспечена» там,
# где она не обеспечена, — то есть ровно ту ложь, которую весь этот срез убирает.
SHAPE_SUPPORT = {
    "anthropic": {"mode": ENFORCED, "mechanism": "output_config.format (json_schema)",
                  "note": "ответ приходит одним текстовым блоком с валидным по схеме JSON"},
    "openai": {"mode": ENFORCED, "mechanism": "response_format: json_schema (strict)",
               "note": "Structured Outputs OpenAI; схема закрытая (additionalProperties: false)"},
    "deepseek": {"mode": JSON_ONLY, "mechanism": "response_format: json_object",
                 "note": "вендор гарантирует валидный JSON, но не схему — кит сверяет схему сам"},
    "qwen": {"mode": JSON_ONLY, "mechanism": "response_format: json_object",
             "note": "вендор гарантирует валидный JSON, но не схему — кит сверяет схему сам"},
    "kimi": {"mode": JSON_ONLY, "mechanism": "response_format: json_object",
             "note": "вендор гарантирует валидный JSON, но не схему — кит сверяет схему сам"},
    "openai-compatible": {
        "mode": UNSUPPORTED, "mechanism": None,
        "note": "endpoint произвольный: что он умеет, из имени не следует. Назовите вендора "
                "(--provider deepseek|qwen|kimi) или задайте AI_OPS_RESPONSE_FORMAT=json_object, "
                "если ваш endpoint это поддерживает"},
    "claude-cli": {
        "mode": UNSUPPORTED, "mechanism": None,
        "note": "локальная сессия `claude -p`: у CLI нет параметра формата ответа. Путь остаётся "
                "первоклассным (работает без ключа), форма обеспечивается разбором постфактум, и "
                "в отчёте это сказано словом"},
    "claude-code-local": {
        "mode": UNSUPPORTED, "mechanism": None,
        "note": "то же, что claude-cli — алиас того же адаптера"},
    "mock": {"mode": UNSUPPORTED, "mechanism": None,
             "note": "заглушка вердиктов не выносит: отдай она валидный reviewer-result, гейт "
                     "получил бы вердикт от того, кто ничего не читал. Офлайн-путь «форма "
                     "обеспечена» проверяется тестами живых адаптеров с подменённым HTTP"},
}

_DEFAULT_SUPPORT = {"mode": UNSUPPORTED, "mechanism": None,
                    "note": "провайдер не объявлен в карте форм — считаем, что механизма нет"}


def shape_support(provider_name: str) -> dict:
    """Что провайдер умеет с формой ответа. Неизвестный -> unsupported, а не «наверное умеет»."""
    return dict(SHAPE_SUPPORT.get(provider_name) or _DEFAULT_SUPPORT)


def shape_report(providers=None) -> str:
    """Человекочитаемая граница: кто умеет, кто нет и что происходит там, где нет."""
    names = sorted(providers or SHAPE_SUPPORT)
    width = max(len(n) for n in names)
    lines = ["ФОРМА ОТВЕТА ТАМ, ГДЕ РЕШАЕТСЯ ВЕРДИКТ",
             "  enforced   — схему гарантирует провайдер",
             "  json_only  — гарантирован валидный JSON, схему сверяет кит",
             "  unsupported— механизма нет: проза и разбор постфактум", ""]
    for n in names:
        s = shape_support(n)
        lines.append(f"  {n.ljust(width)}  {s['mode']:<11} {s['mechanism'] or '—'}")
    lines.append("")
    lines.append("  Отказ по форме (пусто / обрезано / не по схеме) — это ОТКАЗ с причиной, а не "
                 "пустой вердикт: гейт остаётся незакрытым и говорит, что именно случилось.")
    return "\n".join(lines)
