"""Проверка реестра ArchitectureDecision (decisions/adr/*.yaml). Вынесена из
`validation/validate_adr_registry.py` вниз (лента №5), чтобы рантайм
(intelligence.evolution_triggers) звал `check_registry()`/`DEFAULT_DIR` ВНИЗ, без восходящего ребра
intelligence -> validation.

check_registry читает КАТАЛОГ реестра (glob ADR-*.yaml + парсинг) — read-only I/O, поэтому логика
переезжает сюда целиком, а не как «чистое check(data)». Поверх поштучной структурной проверки
(`architecture_decision.check`) держит кросс-целостность: имя файла == id; id уникальны;
related-ссылки резолвятся; supersede-цепочка двунаправленно согласована; ui_impact ∈ вокабуляре.

Раньше проверка ui_impact бралась из `gate_policy.UI_IMPACT` (пакет `gates`, capabilities) — из
слоя primitives тянуть его нельзя, поэтому вокабуляр берётся из `architecture_decision.UI_IMPACT`
(та же величина, но в этом же слое). Зависит только от stdlib и pyyaml.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from ai_ops_kit.checks import architecture_decision as vad

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[2])

DEFAULT_DIR = PKG / "decisions" / "adr"


def check_registry(adr_dir: Path):
    errors = []
    adrs = {}
    files = sorted(Path(adr_dir).glob("ADR-*.yaml")) if Path(adr_dir).is_dir() else []
    for f in files:
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
        except yaml.YAMLError as ex:
            errors.append(f"{f.name}: не парсится YAML ({ex})")
            continue
        for e in vad.check(data):
            errors.append(f"{f.name}: {e}")
        aid = (data or {}).get("id")
        if isinstance(aid, str):
            if f.stem != aid:
                errors.append(f"{f.name}: имя файла не совпадает с id ({aid})")
            if aid in adrs:
                errors.append(f"дубликат id {aid}")
            else:
                adrs[aid] = data
    ids = set(adrs)
    for aid, d in adrs.items():
        sup, by = d.get("supersedes"), d.get("superseded_by")
        if sup == aid or by == aid:
            errors.append(f"{aid}: само-supersede запрещён")
        if sup:
            if sup not in ids:
                errors.append(f"{aid}.supersedes -> несуществующий {sup}")
            elif adrs[sup].get("superseded_by") != aid:
                errors.append(f"{aid} supersedes {sup}, но {sup}.superseded_by != {aid} (несогласовано)")
        if by:
            if by not in ids:
                errors.append(f"{aid}.superseded_by -> несуществующий {by}")
            elif adrs[by].get("supersedes") != aid:
                errors.append(f"{aid}.superseded_by={by}, но {by}.supersedes != {aid} (несогласовано)")
        for r in d.get("related", []) or []:
            if r not in ids:
                errors.append(f"{aid}.related -> несуществующий {r}")
        ui = d.get("ui_impact")
        if ui is not None and ui not in vad.UI_IMPACT:
            errors.append(f"{aid}.ui_impact '{ui}' не в вокабуляре UI_IMPACT (допустимые значения ui_impact)")
    return errors, adrs
