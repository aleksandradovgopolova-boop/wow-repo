"""ОДИН источник частых путей архитектурного документа и документа безопасности.

Стандарт кита канонизирует КОРЕНЬ (`ARCHITECTURE.md` / `SECURITY.md`), но дочки реально держат эти
документы в НЕ-каноничных местах (`docs/architecture/ARCHITECTURE.md`, `docs/security/*.md`,
`docs/quality/SECURITY*`). Раньше знание об этих местах было размазано: детектор CONFLICTING
(`source_conflict._ARCH`) знал один поднабор арх-путей, а back-fill установщика — только корень плюс
`context/system/*`. Из-за этого back-fill сеял пустой черновик `ARCHITECTURE.md`/`SECURITY.md` РЯДОМ
с реальным документом дочки и просил «заполнить» уже написанное — нарушая собственный принцип кита
«один источник правды». Теперь и детектор конфликтов, и back-fill спрашивают ОДИН список.

Модуль намеренно без зависимостей (только `pathlib`): его читают и пакет, и установщик-файл.
"""
from __future__ import annotations

from pathlib import Path

# Частые места архитектурного документа. Канонический — корневой `ARCHITECTURE.md` (стандарт кита);
# остальные — где дочки реально его держат (ii-среда: `docs/architecture/ARCHITECTURE.md`, 842 строки,
# «единственный источник правды»). Порядок: канонический корень первым — потребители, берущие
# «первый существующий», предпочтут его. Все пути литеральные (без glob).
ARCHITECTURE_PATHS = (
    "ARCHITECTURE.md",
    "docs/ARCHITECTURE.md",
    "docs/architecture.md",
    "docs/architecture/ARCHITECTURE.md",
    ".ai/project/context/architecture/ARCHITECTURE.md",
    "ARCHITECTURE.rst",
    "context/system/SystemOverview.md",
    "context/system/RepositoryMap.md",
)

# Частые места документа безопасности. Канонический — корневой `SECURITY.md`; остальные — где дочки
# реально держат политику/архитектуру безопасности (ii-среда: `docs/security/security-policy.md`,
# `docs/security/security-architecture.md`, `docs/quality/SECURITY_CHECKLIST.md`). Записи с `*` —
# glob по дереву дочки.
SECURITY_PATHS = (
    "SECURITY.md",
    "docs/SECURITY.md",
    ".github/SECURITY.md",
    "docs/security/*.md",
    "docs/quality/SECURITY*.md",
    "docs/quality/SECURITY*",
)

# Маркеры кит-черновика: back-fill ставит `status: draft` и текст «Это заготовка». Документ дочки с
# такими маркерами — не «существующий реальный», а прежний посев кита; его наличие не отменяет посева.
_DRAFT_MARKERS = ("status: draft", "Это заготовка")


def _is_real_doc(p: Path) -> bool:
    """Файл существует, непуст и НЕ кит-черновик (не `status: draft` / «Это заготовка»). -> bool."""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if not text.strip():
        return False
    head = text[:1000]
    return not any(marker in head for marker in _DRAFT_MARKERS)


def find_existing(root, patterns) -> str | None:
    """Первый РЕАЛЬНЫЙ (непустой, не-черновик) документ из `patterns`. -> относительный путь | None.

    Литеральные пути проверяются как есть; записи с `*`/`?`/`[` — glob по дереву дочки
    (детерминированный порядок). «Найдено» только если файл существует И проходит `_is_real_doc` —
    пустой файл и прежний кит-черновик не считаются существующим документом.
    """
    root = Path(root)
    for pat in patterns:
        if any(ch in pat for ch in "*?["):
            for p in sorted(root.glob(pat)):
                if p.is_file() and _is_real_doc(p):
                    return str(p.relative_to(root)).replace("\\", "/")
        else:
            p = root / pat
            if p.is_file() and _is_real_doc(p):
                return pat
    return None


# Артефакт стандарта (basename корневого канонического файла) -> частые места того же смысла.
_PATHS_BY_ARTIFACT = {
    "ARCHITECTURE.md": ARCHITECTURE_PATHS,
    "SECURITY.md": SECURITY_PATHS,
}


def existing_for_artifact(root, artifact_rel) -> str | None:
    """Есть ли у дочки реальный документ того же смысла, что канонический `artifact_rel`.

    `artifact_rel` — путь обязательного артефакта из манифеста (напр. `ARCHITECTURE.md`). Для
    артефактов без известных альтернативных мест -> None (back-fill сеет черновик как обычно).
    """
    patterns = _PATHS_BY_ARTIFACT.get(Path(artifact_rel).name)
    if not patterns:
        return None
    return find_existing(root, patterns)
