"""Обновление living-status дочки на доставке (#404).

ПОЧЕМУ ФАЙЛ ПОЯВИЛСЯ. Автономный прогон открывал PR, не трогая статус-док дочки
(`ProductStatus.md` / `PROJECT_STATUS.md`). Этот volatile-док протухает по дате
(`reviewed_at` + класс свежести), и required-проверка status-freshness дочки краснела — кит по
СВОИМ ЖЕ правилам отдавал немёржабельный PR. Замысел уже был объявлен в манифесте
(`living_status.doc`, «обновлять на PR, меняющем готовность», `updated_in: ai-finish-task`), но
автономный пайплайн его не исполнял.

ЧЕСТНО, А НЕ ПОДМЕНОЙ ДАТЫ. Доставленная работа реально меняет «что готово / живёт», а это ровно
то, что фиксирует living-status. Поэтому прогон на шаге доставки ДОПИСЫВАЕТ факт доставки в журнал
дока и обновляет `reviewed_at` — это запись о случившемся, а не косметический бамп даты. Дока нет,
у него нет frontmatter, или он помечен `template: true` -> no-op: лжи не добавляем, статус-док
чужого репозитория кит наугад не сочиняет.

Путь дока берётся из `.ai-ops.yaml` дочки (`living_status.doc`), иначе пробуются известные
умолчания. Управляется только док, следующий конвенции свежести кита (frontmatter с `reviewed_at`
/ `stability`); произвольный файл без этой конвенции не трогается.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import yaml

# Кандидаты в порядке приоритета; первый существующий и управляемый выигрывает.
# Явный `living_status.doc` из .ai-ops.yaml добавляется перед ними.
_DEFAULT_CANDIDATES = ("context/product/ProductStatus.md", "PROJECT_STATUS.md")

_DELIVERY_HEADING = "## Журнал доставки (AI Ops)"
_FM_RE = re.compile(r"^(---\n)(.*?\n)(---)", re.DOTALL)
_REVIEWED_RE = re.compile(r"^(\s*)reviewed_at\s*:.*$", re.MULTILINE)


def _config_doc_rel(root: Path) -> str | None:
    """Путь living-status из `.ai-ops.yaml` дочки (`living_status.doc`). -> относительный путь или None."""
    for fname in (".ai-ops.yaml", ".ai-ops.yml"):
        cfg = root / fname
        if not cfg.is_file():
            continue
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 — битый конфиг не должен рушить доставку
            return None
        doc = (data.get("living_status") or {}).get("doc")
        return doc if isinstance(doc, str) and doc.strip() else None
    return None


def _frontmatter(md: str) -> dict:
    """frontmatter документа как dict (пустой, если его нет / он битый)."""
    if md.startswith("---"):
        parts = md.split("---", 2)
        if len(parts) >= 3:
            try:
                return yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                return {}
    return {}


def _is_managed(md: str) -> bool:
    """Управляемый ли living-status: есть frontmatter конвенции свежести и это НЕ шаблон кита."""
    if not md.startswith("---"):
        return False
    fm = _frontmatter(md)
    if fm.get("template") is True:
        return False
    # Конвенция свежести: документ размечен reviewed_at или stability (иначе freshness к нему неприменим).
    return ("reviewed_at" in fm) or ("stability" in fm)


def resolve_doc(root: Path, *, config_rel: str | None = None) -> Path | None:
    """Найти управляемый living-status дока в `root`. -> Path или None (тогда обновлять нечего)."""
    rels: list[str] = []
    cfg = config_rel if config_rel is not None else _config_doc_rel(root)
    if cfg:
        rels.append(cfg)
    rels.extend(_DEFAULT_CANDIDATES)
    seen: set[str] = set()
    for rel in rels:
        if rel in seen:
            continue
        seen.add(rel)
        p = root / rel
        if not p.is_file():
            continue
        try:
            md = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if _is_managed(md):
            return p
    return None


def _bump_reviewed_at(md: str, today: str) -> str:
    """Обновить (или добавить) `reviewed_at` во frontmatter, сохранив остальное форматирование."""
    m = _FM_RE.match(md)
    if not m:
        return md
    fm_block = m.group(2)
    if _REVIEWED_RE.search(fm_block):
        new_fm = _REVIEWED_RE.sub(lambda mm: f"{mm.group(1)}reviewed_at: {today}", fm_block, count=1)
    else:
        # добавить строку в конец frontmatter (перед закрывающим ---)
        new_fm = fm_block.rstrip("\n") + f"\nreviewed_at: {today}\n"
    return md[:m.start(2)] + new_fm + md[m.end(2):]


def _append_entry(md: str, entry: str) -> str:
    """Дописать строку журнала доставки: под существующий заголовок или создав его в конце файла."""
    line = f"- {entry}"
    idx = md.find(_DELIVERY_HEADING)
    if idx != -1:
        # вставить сразу после строки заголовка (свежие записи сверху)
        eol = md.find("\n", idx)
        if eol == -1:
            return md + f"\n{line}\n"
        return md[: eol + 1] + f"\n{line}\n" + md[eol + 1 :]
    sep = "" if md.endswith("\n") else "\n"
    return f"{md}{sep}\n{_DELIVERY_HEADING}\n\n{line}\n"


def _short_task(task: str, limit: int = 80) -> str:
    one_line = " ".join(str(task).split())
    return one_line[: limit - 1] + "…" if len(one_line) > limit else one_line


def refresh(work_root, wid: str, task: str, *, today: str | None = None,
            config_rel: str | None = None) -> dict | None:
    """Обновить living-status дочки фактом доставки работы `wid`.

    Записывает в найденный управляемый док строку журнала о доставке и бампает `reviewed_at`, чтобы
    volatile-док не протух и status-freshness дочки не блокировал авто-PR. Управляемого дока нет ->
    None (no-op, без ложной записи). -> dict(doc, reviewed_at, entry) или None.
    """
    root = Path(work_root)
    today = today or date.today().isoformat()
    doc = resolve_doc(root, config_rel=config_rel)
    if doc is None:
        return None
    md = doc.read_text(encoding="utf-8")
    entry = f"{today} — доставлена работа {wid}: {_short_task(task)}"
    updated = _append_entry(_bump_reviewed_at(md, today), entry)
    if updated == md:
        return None
    doc.write_text(updated, encoding="utf-8")
    return {"doc": str(doc.relative_to(root)), "reviewed_at": today, "entry": entry}


def _existing_candidate(root: Path, config_rel: str | None) -> tuple[str | None, Path | None]:
    """Первый СУЩЕСТВУЮЩИЙ файл-кандидат в статус-доки (управляемый или нет). Нужен, чтобы назвать
    причину-исключение точнее, чем «дока нет»: шаблон / без разметки свежести — разные причины."""
    rels: list[str] = []
    cfg = config_rel if config_rel is not None else _config_doc_rel(root)
    if cfg:
        rels.append(cfg)
    rels.extend(_DEFAULT_CANDIDATES)
    seen: set[str] = set()
    for rel in rels:
        if rel in seen:
            continue
        seen.add(rel)
        p = root / rel
        if p.is_file():
            return rel, p
    return None, None


def describe(work_root, *, config_rel: str | None = None, today: str | None = None) -> dict:
    """Read-only: как прогон обошёлся со статус-доком репозитория — для ЯВНОЙ строки в теле PR (#404).

    Прогон обязан на доставке либо обновить статус-доки, либо назвать причину, по которой не обновил;
    без этого сгенерированный PR молча упирается в собственный гейт свежести. Эта функция НИЧЕГО не
    пишет — только сообщает исход (обновление `refresh` уже случилось на фазе commit):
      * управляемый док найден -> {"managed": True, "doc": rel, "reviewed_at": str|None,
        "fresh_today": bool} (fresh_today — reviewed_at бампнут на сегодня этой доставкой);
      * управляемого нет -> {"managed": False, "reason": <человекочитаемая причина-исключение>}.
    Свежесть определяется локально (reviewed_at == today), без импорта слоя validation — ядро не
    зависит от validation вверх по слоям.
    """
    try:
        return _describe(work_root, config_rel=config_rel, today=today)
    except Exception:  # noqa: BLE001 — описание судьбы статус-дока не должно рушить доставку PR
        return {"managed": False, "reason": "статус-док не удалось прочитать"}


def _describe(work_root, *, config_rel: str | None, today: str | None) -> dict:
    """Ядро describe без обёртки-страховки (см. describe)."""
    root = Path(work_root)
    today = today or date.today().isoformat()
    doc = resolve_doc(root, config_rel=config_rel)
    if doc is not None:
        reviewed = _frontmatter(doc.read_text(encoding="utf-8")).get("reviewed_at")
        return {"managed": True, "doc": str(doc.relative_to(root)),
                "reviewed_at": (str(reviewed) if reviewed else None),
                "fresh_today": str(reviewed) == today}
    rel, p = _existing_candidate(root, config_rel)
    if p is None:
        reason = "в репозитории нет статус-дока (living-status) — обновлять нечего, гейт свежести неприменим"
    else:
        try:
            md = p.read_text(encoding="utf-8")
        except OSError:
            md = ""
        if _frontmatter(md).get("template") is True:
            reason = f"статус-док {rel} помечен как шаблон — кит его не трогает"
        else:
            reason = (f"статус-док {rel} без разметки свежести (reviewed_at/stability) — "
                      "гейт свежести к нему неприменим")
    return {"managed": False, "reason": reason}
