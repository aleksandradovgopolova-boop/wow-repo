#!/usr/bin/env python3
"""portfolio_patterns.py — ОБЕЗЛИЧЕННЫЙ портфельный вид повторяющихся отказов дочек (T5).

ПОВОД (направление `portfolio-intelligence`). Кит уже собирает наблюдения дочек в канал
`findings/from-children` (`engops/kit_feedback.py`) и уже группирует их по классу наблюдения
(`intelligence/precedent_ledger.py`). Но материнскому киту нельзя показывать этот срез КАК ЕСТЬ:
наблюдение несёт `child.path` (абсолютный путь машины), `child.commit`, `child.name` и вывод команд.
Портфельный вид «какие классы отказов повторяются по всем дочкам» обязан подниматься наверх
ОБЕЗЛИЧЕННО — без кода, без пользовательских данных, только паттерны и числа. Телеметрия в этом
репозитории ЗАПРЕЩЕНА; это добровольный reach-контур, и его портфельная проекция не смеет протащить
идентификацию задней дверью.

ЧТО НЕСЁТ ЗАПИСЬ ПОРТФЕЛЯ (`PortfolioPattern`) — РОВНО ТРИ ВИДА ПОЛЕЙ:
  * `observation_class` (+ человекочитаемая метка) — КЛАСС отказа, не его содержание;
  * счётчики: `case_count` (сколько наблюдений), `child_count` (сколько РАЗНЫХ дочек) и метка частоты;
  * `children` — список ОБЕЗЛИЧЕННЫХ ключей дочек (`proj-<sha256[:16]>`), и больше ничего.
Ни `child.path`, ни `child.commit`, ни `child.name`, ни вывода команд, ни текста наблюдения/улик
здесь НЕТ и быть не может — за это отвечает сторож `check_anonymized` (ниже), а не обещание в доке.

ОБЕЗЛИЧЕННЫЙ КЛЮЧ ДОЧКИ И ЕГО ЧЕСТНОЕ ОГРАНИЧЕНИЕ. Каноничный анонимный id проекта
(`engops.child_registry.project_anon_id`) = `proj-` + sha256(ПЕРВЫЙ коммит репозитория)[:16]. Он
стабилен на всю жизнь репозитория и одинаков на любой машине. НО наблюдение из `findings/from-children`
ПЕРВОГО коммита НЕ несёт — только `child.commit` (это HEAD, а не корень истории) и идентифицирующие
`path`/`name`. Поэтому здесь мы НЕ можем восстановить каноничный id и НЕ выдаём производный ключ за
него. Мы берём лучшее доступное идентифицирующее поле и делаем из него ОДНОСТОРОННИЙ ключ той же ФОРМЫ
(`proj-` + sha256(...)[:16]): он обезличивает (по хэшу путь/имя не восстановить) и стабилен для одной
машины, но это НЕ каноничный first-commit id — совпадёт с каноничным только случайно. Основание ключа
(что именно захэшировано) сводится в `key_basis` портфельного вида ЧИСЛАМИ, чтобы честность была видна
машинно, а не только на словах. Если наблюдение когда-нибудь начнёт нести готовый анонимный id — он
берётся как есть (basis `anon-id`), и ограничение снимается для таких записей.

ГДЕ ОН ЖИВЁТ. Пакет `intelligence` (слой выше ядра). Читает канал через `child_findings.load_findings`
(единственный владелец пути), охват — через `engops.child_registry` (слой ниже, зависимость ВНИЗ
разрешена), метку частоты переиспользует у `precedent_ledger` (тот же слой). НИЧЕГО не пишет и НЕ
импортирует `validation`/`cli` — это ПРОЕКЦИЯ, как `child_findings`.
"""
from __future__ import annotations

import hashlib
import re

# Приоритет идентифицирующих полей наблюдения для вывода ключа. Готовый анонимный id (если появится) —
# первым и без хэширования (он УЖЕ обезличен). Дальше — по убыванию стабильности/уникальности.
# `commit` НАМЕРЕННО не в списке: это HEAD, он меняется от коммита к коммиту — ключ скакал бы.
_KEY_FIELDS = ("anon_id", "path", "name")
_BASIS_LABEL = {
    "anon-id": "готовый анонимный id (ограничение снято)",
    "path": "хэш пути дочки (не каноничный first-commit id)",
    "name": "хэш имени дочки (не каноничный first-commit id)",
    "none": "идентифицирующего поля нет — общий обезличенный ключ",
}

# Форма обезличенного ключа. Ровно как у child_registry.project_anon_id: `proj-` + 16 hex.
_ANON_KEY_RE = re.compile(r"^proj-[0-9a-f]{16}$")
_UNKNOWN_KEY = "proj-" + hashlib.sha256(b"ai-ops:portfolio:no-identifier").hexdigest()[:16]


def _text(v) -> str:
    return str(v or "").strip()


def _hash_key(value: str) -> str:
    """Односторонний ключ формы `proj-<sha256[:16]>` из строки. По хэшу исходник не восстановить."""
    return "proj-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def anon_key(obs: dict) -> tuple[str, str]:
    """Наблюдение → (обезличенный ключ дочки, основание ключа). См. «ЧЕСТНОЕ ОГРАНИЧЕНИЕ» в доке модуля.

    Готовый `anon_id` (если наблюдение его несёт) берётся как есть — он уже обезличен. Иначе хэшируем
    лучшее доступное идентифицирующее поле (path, затем name). Нет ни одного → общий ключ `none`
    (все безымянные дочки схлопнутся в один — честно, а не выдуманные разные id)."""
    child = obs.get("child") if isinstance(obs.get("child"), dict) else {}
    for field in _KEY_FIELDS:
        val = _text(child.get(field)) or _text(obs.get(field))
        if not val:
            continue
        if field == "anon_id":
            # Уже обезличен вызывающим/дочкой. Доверяем форме только если она наша; иначе всё равно
            # хэшируем — чужой «id» может оказаться сырым идентификатором.
            return (val, "anon-id") if _ANON_KEY_RE.match(val) else (_hash_key(val), "anon-id")
        return _hash_key(val), field
    return _UNKNOWN_KEY, "none"


# Человекочитаемые имена классов — переиспользуем у precedent_ledger, чтобы не завести второй словарь.
def _class_label(cls: str) -> str:
    from ai_ops_kit.intelligence import precedent_ledger
    return precedent_ledger._PATTERN_LABEL_RU.get(cls, cls)


# ГРАНИЦА ПО КЛАССУ НАБЛЮДЕНИЯ. `observation_class` в наблюдении дочки НИЧЕМ не ограничен на пути
# `child_findings.load_findings` (он намеренно обходит `kit_feedback.check`), поэтому дочка может
# положить в класс СВОБОДНЫЙ ТЕКСT — имя/хост/почту/секрет, — и он утёк бы в портфельный вид дословно
# (класс и его метка печатаются как есть). Поэтому классы сведены к КОНЕЧНОМУ НЕидентифицирующему
# набору: всё вне enum kit_feedback.OBSERVATION_CLASSES схлопывается в обобщённый `_FALLBACK_CLASS`
# ещё на входе (нормализация у источника), а сторож дополнительно краснеет на классе/метке вне набора
# (зубы: даже если нормализацию обойдут). Источник истины по enum — kit_feedback, второго не заводим.
_FALLBACK_CLASS = "наблюдение"


def _allowed_classes() -> frozenset:
    """Допустимые (НЕидентифицирующие) классы: enum наблюдений + обобщённый fallback."""
    from ai_ops_kit.engops import kit_feedback
    return frozenset(kit_feedback.OBSERVATION_CLASSES) | {_FALLBACK_CLASS}


def _allowed_labels() -> frozenset:
    """Допустимые метки классов — ровно метки допустимых классов, и ничего кроме."""
    return frozenset(_class_label(c) for c in _allowed_classes())


def portfolio_patterns(kit_root) -> dict:
    """READ-ONLY обезличенный портфельный вид отказов дочек из канала `findings/from-children`.

    -> {observation_count, child_count, patterns: [PortfolioPattern...], registered_children,
        key_basis, empty_state}. Группировка — по классу наблюдения; в каждой записи только
        обезличенный ключ + класс + счётчики (см. доку модуля). Ничего не пишет.

    ЧЕСТНЫЕ ПУСТЫЕ СОСТОЯНИЯ (`empty_state`), НИКОГДА не подделываются и РАЗЛИЧНЫ:
      * `no_children`     — наблюдений нет И ни одна дочка не отметилась в охвате: показывать нечего,
                            потому что дочек ещё нет;
      * `no_observations` — дочки в охвате есть, но отказов пока не наблюдалось: это НЕ «нет дочек».
      * None              — есть что показать.
    """
    from ai_ops_kit.intelligence import child_findings, precedent_ledger
    from ai_ops_kit.engops import child_registry

    observations = child_findings.load_findings(kit_root)
    regs, _errs = child_registry.load_registrations(kit_root)
    registered = len(regs)

    allowed = _allowed_classes()
    groups: dict[str, list[str]] = {}      # класс -> список обезличенных ключей (по наблюдению)
    basis_counts: dict[str, int] = {}
    all_children: set[str] = set()
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        key, basis = anon_key(obs)
        basis_counts[basis] = basis_counts.get(basis, 0) + 1
        all_children.add(key)
        # НОРМАЛИЗАЦИЯ У ИСТОЧНИКА: класс вне enum (мог нести идентификацию) → обобщённый fallback.
        # Так свободный текст дочки не может всплыть в портфельный вид ни классом, ни его меткой.
        raw_cls = _text(obs.get("observation_class"))
        cls = raw_cls if raw_cls in allowed else _FALLBACK_CLASS
        groups.setdefault(cls, []).append(key)

    patterns: list[dict] = []
    for cls, keys in groups.items():
        distinct = sorted(set(keys))
        conf = precedent_ledger.confidence_from_cases(len(keys))
        patterns.append({
            "schema_version": 1,
            "kind": "PortfolioPattern",
            "observation_class": cls,
            "class_label": _class_label(cls),
            "case_count": len(keys),
            "child_count": len(distinct),
            "children": distinct,                # ТОЛЬКО обезличенные ключи
            "confidence": conf,
            "frequency_label": precedent_ledger.FREQUENCY_LABEL_RU[conf],
        })
    # Самое частое сверху (по числу случаев), затем по классу — детерминированно, БЕЗ вывода «важнее».
    patterns.sort(key=lambda p: (-p["case_count"], p["observation_class"]))

    if not observations:
        empty_state = "no_children" if registered == 0 else "no_observations"
    else:
        empty_state = None

    return {
        "schema_version": 1,
        "kind": "PortfolioPatterns",
        "observation_count": len(observations),
        "child_count": len(all_children),
        "registered_children": registered,
        "patterns": patterns,
        "key_basis": {_BASIS_LABEL.get(b, b): n for b, n in sorted(basis_counts.items())},
        "empty_state": empty_state,
    }


# ── СТОРОЖ ГРАНИЦЫ ОБЕЗЛИЧИВАНИЯ (зубы DoD) ────────────────────────────────────────────────────────

# Ключи-поля, которых в портфельной записи быть НЕ ДОЛЖНО: они несут идентификацию/содержание.
_FORBIDDEN_KEYS = frozenset({
    "path", "child_path", "commit", "sha", "name", "child_name", "child",
    "statement", "evidence", "output", "stdout", "stderr", "command", "quote",
})
# Голый git-sha: hex-токен длиной 7..40. Каноничный HEAD дочки — 12 hex, короткий — 7+.
_SHA_RE = re.compile(r"(?<![0-9a-zA-Z])[0-9a-f]{7,40}(?![0-9a-zA-Z])")


def _looks_like_path(s: str) -> bool:
    """Похоже ли значение на путь файловой системы (абсолютный/домашний/относительный/Windows)."""
    s = s.strip()
    if re.match(r"^[A-Za-z]:\\", s):                       # C:\...
        return True
    # корень (^ или пробел) + опц. ~/. + сегмент(ы) через /: ловит /Users/x, /tmp, ~/kit, ./a/b
    return bool(re.search(r"(?:^|\s)(?:~|\.{1,2})?/[\w.\-]+(?:/[\w.\-]+)*", s))


def _scan_value(where: str, value, out: list[str]) -> None:
    """Рекурсивно проверить значение на утечку идентификации. Обезличенные ключи (`proj-…`) — чисто."""
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).strip().lower() in _FORBIDDEN_KEYS:
                out.append(f"{where}.{k}: запрещённое идентифицирующее поле в портфельной записи")
            _scan_value(f"{where}.{k}", v, out)
        return
    if isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _scan_value(f"{where}[{i}]", v, out)
        return
    if not isinstance(value, str):
        return
    s = value.strip()
    if not s or _ANON_KEY_RE.match(s):                     # обезличенный ключ — законное значение
        return
    if "\n" in value:
        out.append(f"{where}: многострочный текст (похоже на сырой вывод команды)")
        return
    if _looks_like_path(s):
        out.append(f"{where}: значение похоже на путь файловой системы ({s[:40]!r})")
    elif _SHA_RE.search(s):
        out.append(f"{where}: значение содержит git-sha-подобный hex ({s[:40]!r})")


def check_anonymized(view_or_patterns) -> list[str]:
    """Сторож границы: КРАСНЕЕТ на любой идентификации, протёкшей в портфельный вид. -> список нарушений.

    Принимает и полный вид (`PortfolioPatterns`), и просто список записей. Проверяет ТРОЯКО:
      1. СТРУКТУРНО — запрещённые имена полей (`path`/`commit`/`name`/`output`/…) где угодно в записи;
      2. ПОЗИТИВНО — каждый элемент `children` ОБЯЗАН быть обезличенным ключом `proj-<16hex>`; сырое
         имя дочки (напр. `bnbm`) детекторами формы не ловится, поэтому ловится здесь;
      3. ПО ФОРМЕ — любое прочее строковое значение не должно быть путём, git-sha или многострочным
         выводом.
    Пусто → границу не нарушили. Это и есть DoD направления в исполняемом виде."""
    is_view = isinstance(view_or_patterns, dict)
    patterns = view_or_patterns.get("patterns") if is_view else view_or_patterns
    allowed_cls = _allowed_classes()
    allowed_lbl = _allowed_labels()
    out: list[str] = []
    for i, rec in enumerate(patterns or []):
        where = f"pattern[{i}]"
        if not isinstance(rec, dict):
            out.append(f"{where}: запись не объект")
            continue
        # 2a. Позитивная проверка ключей дочек — до общего скана (сырое имя иначе проскочит).
        for j, key in enumerate(rec.get("children") or []):
            if not (isinstance(key, str) and _ANON_KEY_RE.match(key.strip())):
                out.append(f"{where}.children[{j}]: не обезличенный ключ ({str(key)[:40]!r}) — "
                           "имя/путь/идентификатор дочки не должен попадать в портфель")
        # 2b. Позитивная проверка класса и метки: только из конечного НЕидентифицирующего набора.
        # Голый низкоэнтропийный токен (имя/хост/почта) в классе форму-детекторы не ловят — ловим тут.
        oc = rec.get("observation_class")
        if oc is not None and oc not in allowed_cls:
            out.append(f"{where}.observation_class: класс вне допустимого набора ({str(oc)[:40]!r}) — "
                       "мог протащить идентификацию свободным текстом")
        cl = rec.get("class_label")
        if cl is not None and cl not in allowed_lbl:
            out.append(f"{where}.class_label: метка вне допустимого набора ({str(cl)[:40]!r})")
        # 1 + 3. Структурные имена и форма значений — по всей записи.
        _scan_value(where, rec, out)
    # Скан полей верхнего уровня вида (не patterns — их проверили пореестрово выше): docstring обещает
    # покрытие всего вида, а не только записей.
    if is_view:
        top = {k: v for k, v in view_or_patterns.items() if k != "patterns"}
        _scan_value("view", top, out)
    return out


def is_anonymized(view_or_patterns) -> bool:
    """True, если портфельный вид не нарушает границу обезличивания (сторож молчит)."""
    return not check_anonymized(view_or_patterns)
