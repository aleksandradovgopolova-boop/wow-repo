#!/usr/bin/env python3
"""Горячие точки ночного обзора: агрегат ПО ИСТОРИИ — что краснеет чаще всего (read-only).

ПОВОД. Обзор `nightly_review` смотрел на СНИМОК (что разошлось со вчера) и на НЕДЕЛЬНЫЙ ТРЕНД (лучше
или хуже за неделю относительно ОДНОГО обзора-якоря). Ни то ни другое не отвечает на вопрос
владельца «что у нас болит ХРОНИЧЕСКИ»: какая проверка/ось краснеет из ночи в ночь, а не разово.
Работа `nightly-review-aggregates-hotspots-from-history` добавляет этот срез — агрегат по ВСЕЙ
доступной истории обзора, а не по последней точке.

ЧТО ЗДЕСЬ:
  · `compute_hotspots` — по последним N записям истории считает для каждой проверки, в скольких
    прогонах она краснела (`runs_red`) и сколько находок дала суммарно (`total_findings`), и
    сортирует по ЧАСТОТЕ покраснения (хроническое — выше разового). Окно N — `HOTSPOT_WINDOW`.
  · `compute_axis_hotspots` — тот же агрегат, но по ОСЯМ (`axis_counts` из той же записи истории).
  · `format_hotspots` / `format_axis_hotspots` / `format_hotspots_section` — раздел «Горячие точки»
    для брифа: топ хронических болячек по частоте.

ИСТОЧНИК ДАННЫХ — УЖЕ СУЩЕСТВУЮЩАЯ ИСТОРИЯ (`nightly_trends.read_history`,
`.ai/project/nightly-review/history.yaml`). НИЧЕГО НОВОГО НЕ СОБИРАЕМ и нового файла состояния НЕ
заводим — горячие точки это read-only агрегат поверх записей, что обзор уже пишет. Граница v0
(обзор ничего не правит в продукте) цела.

ЧЕСТНОСТЬ ПРЕВЫШЕ ПОЛНОТЫ. Мало записей для агрегата (`< MIN_ENTRIES_FOR_HOTSPOTS`) — горячие точки
НЕ выдумываются: обзор прямо говорит «мало истории». Пустая история — так и сказано. И «мало
данных» — это НЕ «горячих точек нет»: первое значит «пока не из чего судить», второе — измеренный
факт «за окно ничто не краснело», и эти два ответа не сворачиваются один в другой.
"""
from __future__ import annotations

from ai_ops_kit.intelligence import nightly_dimensions as nd
from ai_ops_kit.intelligence.nightly_trends import read_history

# ОКНО АГРЕГАТА — ПОСЛЕДНИЕ N ЗАПИСЕЙ ИСТОРИИ. Обзор ежедневный; две недели ловят хроническое, не
# растворяя его в кварталах. Как `TREND_WINDOW_DAYS` — модульная константа, меняется осознанно.
HOTSPOT_WINDOW = 14

# Порог честности: меньше стольких записей в окне — агрегат НЕ называется, называется «мало истории».
# Три — минимум, на котором «краснела в 2 из 3» уже про частоту, а не про одну случайную ночь.
MIN_ENTRIES_FOR_HOTSPOTS = 3


def compute_hotspots(history, *, window: int = HOTSPOT_WINDOW,
                     min_entries: int = MIN_ENTRIES_FOR_HOTSPOTS,
                     counts_key: str = "counts") -> dict:
    """Горячие точки по последним `window` записям истории. -> dict.

    -> {"has_enough", "considered", "window", "min_entries", "rows", "reason"}.
    `rows` (только когда `has_enough`): по строке на проверку/ось, что краснела хоть раз в окне —
    `{check, runs_red, total_findings, window_runs}`, отсортированы по частоте покраснения (сначала
    в скольких прогонах краснела, при равенстве — по сумме находок, затем по имени для стабильности).
    `counts_key` — под каким ключом лежат счётчики в записи (`counts` — по проверкам, `axis_counts`
    — по осям), как в `nightly_trends.trends_from_counts`.

    ЧЕСТНОСТЬ: записей в окне меньше `min_entries` (в т.ч. пустая история) — `has_enough=False`,
    `rows=[]`, агрегат НЕ выдуман. «Мало данных» (`has_enough=False`) и «за окно ничто не краснело»
    (`has_enough=True`, `rows=[]`) — РАЗНЫЕ ответы и не сворачиваются один в другой.
    """
    entries = [e for e in (history or []) if isinstance(e, dict)]
    considered = entries[-window:] if window and window > 0 else entries
    n = len(considered)
    if n < min_entries:
        if n == 0:
            reason = "истории ещё нет — горячие точки появятся, когда накопятся ночные обзоры"
        else:
            reason = (f"мало истории для горячих точек: записей {n}, нужно ≥{min_entries} — "
                      f"это «мало данных», а не «горячих точек нет»")
        return {"has_enough": False, "considered": n, "window": window,
                "min_entries": min_entries, "rows": [], "reason": reason}
    agg: dict = {}
    for e in considered:
        counts = e.get(counts_key)
        if not isinstance(counts, dict):
            continue
        for check, raw in counts.items():
            try:
                c = int(raw)
            except (TypeError, ValueError):
                continue
            if c <= 0:
                continue
            rec = agg.setdefault(check, [0, 0])
            rec[0] += 1
            rec[1] += c
    rows = [{"check": k, "runs_red": v[0], "total_findings": v[1], "window_runs": n}
            for k, v in agg.items()]
    rows.sort(key=lambda r: (-r["runs_red"], -r["total_findings"], str(r["check"])))
    reason = (f"агрегат по последним {n} обзорам; порядок — по частоте покраснения "
              f"(хроническое выше разового)")
    return {"has_enough": True, "considered": n, "window": window,
            "min_entries": min_entries, "rows": rows, "reason": reason}


def compute_axis_hotspots(history, *, window: int = HOTSPOT_WINDOW,
                          min_entries: int = MIN_ENTRIES_FOR_HOTSPOTS) -> dict:
    """Горячие точки ПО ОСЯМ — тот же агрегат и ТА ЖЕ история, что у `compute_hotspots`, не второй.

    Осевые счётчики (`axis_counts`, их пишет `nightly_dimensions.axis_finding_counts` в ту же запись
    истории) агрегируются по окну так же, как счётчики по проверкам.
    """
    return compute_hotspots(history, window=window, min_entries=min_entries,
                            counts_key="axis_counts")


def _format(hot: dict, *, what: str, title_of=None) -> list[str]:
    """Строки о горячих точках. `what` — что агрегируем («проверка»/«ось») для человеческой прозы;
    `title_of` переводит ключ в человеческое имя (для осей)."""
    if not hot.get("has_enough"):
        return [f"Горячие точки ({what}): {hot.get('reason', 'истории пока мало')}.",
                "«Мало данных» — это не «горячих точек нет»: агрегат появится, когда накопятся записи."]
    n = hot.get("considered")
    rows = hot.get("rows") or []
    if not rows:
        return [f"Горячие точки ({what}): за последние {n} обзоров ничто не краснело — "
                f"хронических болячек не видно (это измеренный факт, а не нехватка истории)."]
    label = title_of or (lambda k: k)
    L = [f"Хронические болячки по {what} за последние {n} обзоров (по частоте покраснения):"]
    for r in rows:
        L.append(f"- **{label(r['check'])}**: краснела в {r['runs_red']} из {n} обзоров "
                 f"({r['total_findings']} находок суммарно).")
    return L


def format_hotspots(hot: dict) -> list[str]:
    """Строки раздела «Горячие точки» по ПРОВЕРКАМ. «Мало истории» остаётся «мало истории»."""
    return _format(hot, what="проверкам")


def format_axis_hotspots(hot: dict) -> list[str]:
    """Строки горячих точек по ОСЯМ (переводит ключ оси в её название), как `format_axis_trends`."""
    titles = {d["key"]: d["title"] for d in nd.DIMENSIONS}
    return _format(hot, what="осям", title_of=lambda k: titles.get(k, k))


def format_hotspots_section(root) -> list[str]:
    """Готовый раздел «Горячие точки» для брифа (по проверкам И по осям), считая из истории дочки.

    Тонкая обёртка для `nightly_review.format_brief`: читает уже существующую историю обзора и рендерит
    раздел целиком, чтобы оркестратор не рос — вся логика горячих точек живёт здесь, в сателлите.
    """
    history = read_history(root)
    return ["", "## Горячие точки", "",
            *format_hotspots(compute_hotspots(history)),
            "", *format_axis_hotspots(compute_axis_hotspots(history))]
