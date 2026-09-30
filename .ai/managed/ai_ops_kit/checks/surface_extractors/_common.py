"""Общие примитивы экстракторов поверхностей: записи по контракту схемы + разделяемые хелперы разбора.

Здесь лежит то, что нужно НЕСКОЛЬКИМ стекам сразу, чтобы одна реализация служила всем и не плодились
дубли:
  * `Surface` / `ParsedFile` — записи контракта схемы реестра фич и разобранный исходник дочки;
  * `_is_str_literal` — строковый ЛИТЕРАЛ (граница `verified`: значение взято из исходника буквально);
  * `_imports_module` — файл действительно импортирует модуль (сужает экстракторы к своему стеку).

Стек-специфичные хелперы (пути/имена конкретного фреймворка, конструкторы записей одного вида) живут
рядом со своим экстрактором, а не здесь — общим считается только то, что пересекает границы стеков.
Модуль читает лишь stdlib (`ast`) и не тянет ничего из ai_ops_kit — он в слое `checks` (primitives).
"""
from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True)
class Surface:
    """Одна запись поверхности по контракту схемы реестра фич.

    Поля ровно те, что требует `surface` в схеме: kind ∈ {route,cli,screen,api};
    ref = "file:line" или "file:line|symbol"; confidence ∈ {verified,inferred}; extractor — id
    экстрактора, породившего запись (из surface-extractors.yaml).
    """
    kind: str
    ref: str
    confidence: str
    extractor: str

    def as_record(self) -> dict:
        """Плоский dict строго по схеме surface (без лишних ключей)."""
        return {"kind": self.kind, "ref": self.ref,
                "confidence": self.confidence, "extractor": self.extractor}


@dataclass(frozen=True)
class ParsedFile:
    """Разобранный исходник дочки, передаваемый экстрактору.

    Несёт и СЫРОЙ текст, и AST. AST-экстрактор (`needs_ast=True`) читает `tree` — ядро гарантирует,
    что дерево разобрано (иначе экстрактор не вызывается). Текстовый экстрактор (`needs_ast=False`,
    напр. console_scripts из TOML/INI, которые не парсятся `ast`) читает `source`. Так добавление
    не-Python стека остаётся «функция + запись», а не форком обхода.
    """
    rel_path: str            # путь относительно корня дочки, posix (идёт в ref как "file")
    source: str              # сырой текст исходника (для текстовых экстракторов)
    tree: ast.AST | None     # AST исходника (None, если файл не Python или не разобрался)


def _is_str_literal(node: ast.expr) -> bool:
    """Узел — строковый ЛИТЕРАЛ (не f-строка, не переменная, не конкатенация)."""
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _imports_module(tree: ast.AST, modname: str) -> bool:
    """Файл импортирует модуль modname (`import modname[.x]` или `from modname[.x] import ...`).

    Экстракторы argparse/click/веб-фреймворков сужены этим условием: `.add_parser(...)`/`@x.command()`,
    `path(...)`/`router.register(...)`/`app.add_get(...)` доказывают поверхность лишь в файле, который
    действительно тянет соответствующий модуль. Без этого атрибут/вызов с тем же именем в чужом коде
    дал бы ложный verified — прямой запрет инварианта честной силы.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == modname or alias.name.startswith(modname + "."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == modname or mod.startswith(modname + "."):
                return True
    return False


def _matching_brace(text: str, open_idx: int) -> int:
    """Индекс `}` в паре к `{` на позиции open_idx, либо -1 (пары нет — обрыв файла).

    Разделяемый примитив текстовых экстракторов формальных грамматик с блоками `{…}`
    (GraphQL SDL, .proto): строки/комментарии к моменту вызова уже замаскированы, так что
    `{`/`}` внутри них не считаются.
    """
    depth = 0
    for k in range(open_idx, len(text)):
        c = text[k]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return k
    return -1
