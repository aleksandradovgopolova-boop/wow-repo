#!/usr/bin/env python3
"""Кросс-артефактная консистентность (v2.3; идея — Spec Kit `analyze`).

Первый и главный класс расхождений (подтверждён боевым прогоном ii-sreda):
события, на которые ссылается dashboard-spec (Source events, Funnels),
обязаны быть объявлены в tracking plan той же фичи. Каждый артефакт по
отдельности валиден — вместе противоречивы; это ловим механически.

Правила:
  1. dashboard-spec есть, tracking plan отсутствует -> PROBLEM;
  2. событие из dashboard-spec не объявлено в tracking plan -> PROBLEM;
  3. артефакты отсутствуют или таблица событий не парсится -> мягкая деградация:
     SKIP/WARN, не ложный fail (гипотеза №2 прогона ii-sreda);
  4. событие объявлено, но нигде не используется -> WARN (информационно).

События извлекаются из markdown-таблиц: tracking plan — первая колонка таблицы
раздела Events; dashboard-spec — snake_case-токены в колонке Source events и в
разделе Funnels. Таксономия object_action (snake_case) — из шаблонов кита.

Использование:  validate_cross_artifacts.py <feature-dir> [...] | --selftest
Возврат 0 — чисто/skip, 1 — есть PROBLEM. Требует pyyaml (для селфтеста — нет).
"""

import re
import sys
import tempfile
from pathlib import Path

EVENT_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
TRACKING = "analytics/tracking-plan.md"
DASHBOARD = "analytics/dashboard-spec.md"


def md_section(text: str, title_re: str) -> str:
    """Вернуть текст раздела '## <title>' до следующего '## '."""
    m = re.search(rf"^##\s+{title_re}.*?$", text, re.MULTILINE | re.IGNORECASE)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"^##\s+", rest, re.MULTILINE)
    return rest[:nxt.start()] if nxt else rest


def declared_events(tracking_text: str):
    """События из первой колонки таблицы раздела Events tracking plan'а."""
    section = md_section(tracking_text, r"Events")
    events = set()
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        first = line.strip("|").split("|")[0].strip().strip("`")
        if re.fullmatch(EVENT_RE.pattern, first):
            events.add(first)
    return events


def used_events(dashboard_text: str):
    """snake_case-токены из колонки Source events и раздела Funnels."""
    used = set()
    for line in dashboard_text.splitlines():
        if line.strip().startswith("|") and "---" not in line:
            cells = [c.strip() for c in line.strip("|").split("|")]
            # эвристика: колонки после третьей в таблице Blocks — source events;
            # надёжнее взять токены из всех ячеек, содержащих snake_case
            for c in cells:
                used.update(EVENT_RE.findall(c))
    used.update(EVENT_RE.findall(md_section(dashboard_text, r"Funnels")))
    return used


def check_feature(feature_dir: Path):
    """-> (problems, warns, skipped_note|None)"""
    tp = feature_dir / TRACKING
    ds = feature_dir / DASHBOARD
    if not ds.exists():
        return [], [], f"{feature_dir.name}: dashboard-spec отсутствует — сверять нечего (skip)"
    if not tp.exists():
        return [f"{feature_dir.name}: dashboard-spec есть, а tracking plan ({TRACKING}) — нет"], [], None
    declared = declared_events(tp.read_text(encoding="utf-8"))
    if not declared:
        return [], [f"{feature_dir.name}: таблица Events в tracking plan не распарсилась — "
                    "сверка пропущена (проверьте формат)"], None
    used = used_events(ds.read_text(encoding="utf-8"))
    problems = [f"{feature_dir.name}: dashboard-spec использует событие '{e}', "
                f"не объявленное в tracking plan" for e in sorted(used - declared)]
    warns = []
    unused = declared - used
    if used and unused:
        warns.append(f"{feature_dir.name}: события объявлены, но не используются "
                     f"в dashboard-spec: {sorted(unused)}")
    return problems, warns, None


TP_OK = """# Tracking Plan
## Events
| Event name | Trigger | Properties | Required | Owner |
|---|---|---|---|---|
| checkout_started | клик | cart_value | yes | pa |
| checkout_completed | заказ | order_id | yes | pa |
"""
DS_OK = """# Dashboard Specification
## Blocks
| Block | Metric(s) | Visualisation | Source events | Segment / filter |
|---|---|---|---|---|
| Conversion | CR | line | checkout_started, checkout_completed | all |
## Funnels
checkout_started -> checkout_completed
"""
DS_BAD = DS_OK.replace("checkout_completed", "checkout_finished")


def selftest():
    ok = True

    def expect(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"{'PASS' if good else 'FAIL'} {name}" + ("" if good else f" (got {got})"))

    with tempfile.TemporaryDirectory() as td:
        def mk(name, tp=None, ds=None):
            d = Path(td) / name
            (d / "analytics").mkdir(parents=True)
            if tp is not None:
                (d / TRACKING).write_text(tp, encoding="utf-8")
            if ds is not None:
                (d / DASHBOARD).write_text(ds, encoding="utf-8")
            return d

        p, w, s = check_feature(mk("a", TP_OK, DS_OK))
        expect("согласованная пара -> чисто", (len(p), len(w)), (0, 0))
        p, _, _ = check_feature(mk("b", TP_OK, DS_BAD))
        expect("необъявленное событие в дашборде -> PROBLEM", len(p) > 0, True)
        p, _, s = check_feature(mk("c", TP_OK, None))
        expect("нет dashboard-spec -> skip без ошибок", (len(p), s is not None), (0, True))
        p, _, _ = check_feature(mk("d", None, DS_OK))
        expect("дашборд без tracking plan -> PROBLEM", len(p), 1)
        p, w, _ = check_feature(mk("e", "# Tracking Plan\nбез таблицы\n", DS_OK))
        expect("нераспарсиваемый tracking plan -> WARN, не fail", (len(p), len(w)), (0, 1))
    print("cross-artifacts selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    if not argv:
        print("использование: validate_cross_artifacts.py <feature-dir> [...] | --selftest")
        return 1
    all_p = []
    for d in argv:
        p, w, s = check_feature(Path(d).resolve())
        for x in p:
            print(f"  [PROBLEM] {x}")
        for x in w:
            print(f"  [WARN] {x}")
        if s:
            print(f"  [SKIP] {s}")
        all_p += p
    if all_p:
        print(f"НАЙДЕНЫ КРОСС-АРТЕФАКТНЫЕ РАСХОЖДЕНИЯ ({len(all_p)}).")
        return 1
    print(f"OK: кросс-артефактная консистентность чиста ({len(argv)} функций).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
