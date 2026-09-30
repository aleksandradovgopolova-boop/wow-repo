"""Каталог фич для аналитика: человекочитаемый рендер реестра фич (W4b).

ЗАЧЕМ. Цель всей фичи feature-registry-coverage — чтобы системный аналитик за час разобрался во
ВСЕХ фичах продукта из ОДНОГО места. Схема (W1), извлечение поверхностей (W2) и судья охвата (W3)
дают машинную сторону; здесь — сторона ЧЕЛОВЕКА: из того же реестра фич собирается один читаемый
каталог. По каждой фиче видно ЧТО она делает (what), ДЛЯ КОГО (who), КАК проверить (verify), какими
поверхностями наблюдаема (surfaces с confidence) и в каком она статусе.

ЧИСТАЯ И ДЕТЕРМИНИРОВАННАЯ. Как и `checks/feature_coverage` (W3), эта логика — stdlib, без сети,
без модели и БЕЗ I/O: она принимает готовый dict реестра и возвращает markdown. Живёт в пакете
`checks` (слой primitives), чтобы её звали ВНИЗ по слоям: чтение реестра и запись документа делает
процессный вход (`devtools/feature_catalog_cli.py` для собственного дока кита; в дочке — доставляемый
entry). Значения каталога ВЫВЕДЕНЫ из реестра при каждом рендере, поэтому не расходятся с ним молча —
тот же принцип, что у `docs/capability-map.md` (генератор читает, не пишет источник).

КОНТРАКТ ВХОДА:
  registry — dict реестра фич по схеме feature-registry (W1):
    `{"features": [ {id, name, description:{what,who,verify}, surfaces:[...], status, owner}, ... ]}`.
  Пустой или отсутствующий реестр (None / {} / features пуст) — НЕ ошибка: рендерится честная
  заглушка «фичи ещё не заведены», а не падение.

КОНТРАКТ ВЫХОДА:
  Полная markdown-страница (включая маркер «СГЕНЕРИРОВАНО — руками не править» и сводку), строкой.
  Детерминирована по входу: одинаковый реестр -> одинаковый текст (нет дат/времени внутри страницы),
  поэтому committed-док можно стеречь сверкой «committed == рендер» (contract-тест).
"""
from __future__ import annotations

# Порядок и подписи статусов. Активные — первыми (их аналитик смотрит чаще всего), затем planned
# (объявлено, кода ещё нет), затем deprecated (выводится из эксплуатации). Неизвестный статус не
# теряется — он выносится в свою группу в конце, честно назван, а не молча спрятан.
STATUS_ORDER: tuple[str, ...] = ("active", "planned", "deprecated")
STATUS_LABEL: dict[str, str] = {
    "active": "active (живёт)",
    "planned": "planned (объявлена, кода ещё нет)",
    "deprecated": "deprecated (выводится из эксплуатации)",
}

_DEFAULT_REGEN = "python3 -m ai_ops_kit.devtools.feature_catalog_cli --write"


def _clean(value) -> str:
    """Одна строка без переносов и лишних пробелов (для ячеек таблицы и подписей)."""
    return " ".join(str(value or "").split())


def _status_of(feature: dict) -> str:
    return str(feature.get("status") or "—")


def _features(registry: dict | None) -> list[dict]:
    if not isinstance(registry, dict):
        return []
    feats = registry.get("features")
    return [f for f in feats if isinstance(f, dict)] if isinstance(feats, list) else []


def _status_rank(status: str) -> tuple[int, str]:
    """Ключ сортировки статуса: сначала известные в объявленном порядке, неизвестные — в конце."""
    return (STATUS_ORDER.index(status), "") if status in STATUS_ORDER else (len(STATUS_ORDER), status)


def summarize(registry: dict | None) -> dict:
    """Сводка каталога: всего фич и разбивка по статусам. Выведена из реестра, не захардкожена."""
    feats = _features(registry)
    by_status: dict[str, int] = {}
    for f in feats:
        by_status[_status_of(f)] = by_status.get(_status_of(f), 0) + 1
    return {"total": len(feats), "by_status": by_status}


def _header(regen_cmd: str) -> str:
    return (
        f"<!-- СГЕНЕРИРОВАНО: каталог фич (ai_ops_kit.checks.feature_catalog). РУКАМИ НЕ ПРАВИТЬ. -->\n"
        f"<!-- Перегенерировать: {regen_cmd} -->\n"
        "\n"
        "# Каталог фич продукта\n"
        "\n"
        "Один достоверный список всех фич продукта — чтобы разобраться во ВСЕХ фичах из ОДНОГО места,\n"
        "а не собирать картину по коду и тикетам. По каждой фиче: **что** она делает, **для кого**,\n"
        "**как проверить**, какими поверхностями наблюдаема в коде и в каком она статусе.\n"
        "\n"
        "Каталог **выведен из реестра фич** при генерации (схема `registry/feature-registry/\n"
        "feature-registry.schema.yaml`), а не написан руками, поэтому не расходится с реестром молча.\n"
        "В продуктовом репозитории источник — собственный реестр фич дочки; здесь, в самом ките,\n"
        "своего продуктового реестра нет, поэтому каталог собран из ОБРАЗЦА\n"
        "`registry/feature-registry/feature-registry.example.yaml` и служит демонстрацией формы.\n"
    )


def _stub(regen_cmd: str) -> str:
    """Честная заглушка для пустого/отсутствующего реестра — не падение."""
    lines = [_header(regen_cmd), "## Фичи ещё не заведены", ""]
    lines += [
        "Реестр фич пуст или отсутствует — заводить каталог не из чего. Это не ошибка: продукт на",
        "ранней стадии либо реестр фич ещё не заполнен. Как только в реестр добавят первую фичу",
        "(по схеме feature-registry), она появится здесь при следующей генерации.",
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _feature_block(feature: dict) -> list[str]:
    """Markdown одной фичи: заголовок, что/для кого/как проверить, владелец и таблица поверхностей."""
    fid = _clean(feature.get("id")) or "—"
    name = _clean(feature.get("name")) or fid
    desc = feature.get("description") if isinstance(feature.get("description"), dict) else {}
    lines = [f"### {name} (`{fid}`)", ""]
    lines += [
        f"- **Что делает:** {_clean(desc.get('what')) or '—'}",
        f"- **Для кого:** {_clean(desc.get('who')) or '—'}",
        f"- **Как проверить:** {_clean(desc.get('verify')) or '—'}",
        f"- **Владелец:** {_clean(feature.get('owner')) or '—'}",
        "",
    ]
    surfaces = feature.get("surfaces")
    surfaces = [s for s in surfaces if isinstance(s, dict)] if isinstance(surfaces, list) else []
    if surfaces:
        lines += ["| Поверхность | Ссылка в коде | Уверенность | Экстрактор |", "|---|---|---|---|"]
        # Детерминированный порядок строк: по виду, затем по ссылке — чтобы diff краснел по сути.
        for s in sorted(surfaces, key=lambda x: (_clean(x.get("kind")), _clean(x.get("ref")))):
            lines.append(
                f"| {_clean(s.get('kind')) or '—'} | `{_clean(s.get('ref')) or '—'}` | "
                f"{_clean(s.get('confidence')) or '—'} | {_clean(s.get('extractor')) or '—'} |")
        lines.append("")
    else:
        lines += ["_Поверхности не заявлены._ Для `planned`-фичи это законно (кода ещё нет); "
                  "для `active` — сигнал, что фича не связана с кодом.", ""]
    return lines


def render_catalog(registry: dict | None, *, regen_cmd: str = _DEFAULT_REGEN) -> str:
    """Собрать человекочитаемый каталог фич из реестра. Пустой/None реестр -> честная заглушка.

    regen_cmd — командная строка перегенерации, вписанная в маркер шапки. По умолчанию — команда
    devtools-обёртки кита; доставляемый entry дочки передаёт свою.
    """
    feats = _features(registry)
    if not feats:
        return _stub(regen_cmd)

    summary = summarize(registry)
    lines = [_header(regen_cmd), "## Сводка", ""]
    lines += ["| Статус | Фич |", "|---|---|", f"| **Всего** | **{summary['total']}** |"]
    for status in sorted(summary["by_status"], key=_status_rank):
        label = STATUS_LABEL.get(status, f"{status} (статус вне схемы)")
        lines.append(f"| {label} | {summary['by_status'][status]} |")
    lines.append("")

    # Фичи сгруппированы по статусу (active -> planned -> deprecated -> прочее), внутри — по id.
    grouped: dict[str, list[dict]] = {}
    for f in feats:
        grouped.setdefault(_status_of(f), []).append(f)
    for status in sorted(grouped, key=_status_rank):
        lines += [f"## {STATUS_LABEL.get(status, f'Статус: {status}')}", ""]
        for f in sorted(grouped[status], key=lambda x: _clean(x.get("id"))):
            lines += _feature_block(f)
    return "\n".join(lines).rstrip() + "\n"
