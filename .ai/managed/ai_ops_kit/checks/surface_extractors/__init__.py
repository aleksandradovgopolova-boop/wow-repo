"""Пакет экстракторов поверхностей продукта, сгруппированных по стекам (W2 feature-registry-coverage).

Публичный вход остаётся `ai_ops_kit.checks.surface_extraction`: он держит записи (`Surface`,
`ParsedFile`), контракт экстрактора (`Extractor`), сборку `DEFAULT_EXTRACTORS` и ядро обхода
(`extract_surfaces`). Здесь живут САМИ функции-экстракторы, разложенные по стекам, чтобы добавление
нового стека было «файл + запись», а не рост одного модуля:

  * `_common`      — общие для нескольких стеков примитивы разбора и записи (`Surface`, `ParsedFile`,
                     `_is_str_literal`, `_imports_module`);
  * `python_web`   — верифицированные HTTP-маршруты питон-веба (Flask/FastAPI, Django, DRF, aiohttp);
  * `python_cli`   — верифицированные CLI-команды (argparse, click, console_scripts);
  * `js_ui`        — экраны фронтенд-роутеров по тексту (React Router, Vue Router).

Каждая группа — когезивная единица одного назначения; ядро (`extract_surfaces`) парсит исходники и
раздаёт их применимым экстракторам, а какие функции существуют — объявлено записью в реестре
`registry/feature-registry/surface-extractors.yaml` (инвариант честных capability-деклараций).
"""
from __future__ import annotations
