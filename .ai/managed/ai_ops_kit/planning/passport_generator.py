#!/usr/bin/env python3
"""Генерация Product Passport из ФАКТИЧЕСКОГО состояния репозитория (PR-6).

Паспорт (`registry/artifact-registry.yaml -> product_passport`) НЕ заполняется шаблоном-заготовкой:
он собирается из того, что реально лежит в репозитории — версия, релизы, стек, CI, тесты, класс
зрелости. Ключевой инвариант — `is_file()` != «заполнен» (F-018/F-027): паспорт из одних заголовков
без содержимого — это Invalid, а не «Valid налегке», и генератор обязан либо назвать ФАКТ с
источником, либо честно сказать «неизвестно — нужно то-то», но не выдумать правдоподобное.

ЧЕГО КИТ НЕ ВЫВОДИТ ИЗ КОДА, ТО НЕ ВЫДУМЫВАЕТ. Аудитория, Problem/JTBD, владелец и его команда,
продуктовые риски из репозитория достоверно НЕ выводятся (`reconstruction.ability: none` у контура
стратегии). Такие разделы получают честный пробел с указанием, что нужно от владельца, — «не сказал»
и «не знаю» разные состояния, и второе нельзя красить первым.

Использование:
  passport_generator.py generate <repo> [-o <file>]     # напечатать/записать паспорт
  passport_generator.py sections <repo> [--json]         # разложить по разделам с источниками
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from ai_ops_kit.shared.gitio import git
from ai_ops_kit.planning import artifact_registry as AR
from ai_ops_kit.planning import repo_audit

VERIFIED, INFERRED, UNKNOWN = "verified", "inferred", "unknown"

# Разделы, которые генератор из кода достоверно НЕ выводит: их заполняет владелец (grounded в
# VISION/AGENTS), и перегенерация обязана их СОХРАНИТЬ, а не затереть на «неизвестно». Остальные
# разделы — машинные снимки (версия, здоровье, статус…), их перегенерируют на релизе/вехе.
OWNER_SECTIONS = ("Название и описание", "Аудитория и проблема", "Owner и команда")

_H = re.compile(r"^#{1,6}\s+(.*\S)\s*$", re.MULTILINE)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_L2 = re.compile(r"(?m)^## (.+?)[ \t]*$")  # заголовок раздела паспорта (level-2)


def _repo_name(root: Path) -> str:
    """Имя репозитория — как называется ПРОДУКТ, а не каталог, из которого его собирают.

    `Path(root).name` подводит в git-worktree: там это имя каталога worktree
    (`whale-status-dashboard-02f8c3`), а не продукта — паспорт печатал бы служебное имя. Спрашиваем
    git: сперва basename origin (имя продукта на форже), затем каталог главного репозитория
    (`--git-common-dir` -> родитель `.git`). Нет git/remote — падаем на `resolve().name`, как раньше.
    """
    import subprocess
    try:
        rc, out, _ = git(root, "remote", "get-url", "origin", timeout=10)
        if rc == 0 and out.strip():
            name = out.strip().rstrip("/").rsplit("/", 1)[-1]
            name = name[:-4] if name.endswith(".git") else name
            if name:
                return name
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        rc, out, _ = git(root, "rev-parse", "--git-common-dir", timeout=10)
        if rc == 0 and out.strip():
            common = Path(out.strip())
            if not common.is_absolute():
                common = (Path(root) / common).resolve()
            if common.name == ".git" and common.parent.name:
                return common.parent.name
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(root).resolve().name


def _latest_tag(root: Path) -> str | None:
    """Самый СВЕЖИЙ тег по дате создания, а не первый по алфавиту.

    `release_history` из repo_audit — это `git tag --list` в лексикографическом порядке, где `v0.8.0`
    идёт раньше `3.36.12`. Называть первый «последним релизом» — фактическая ошибка; берём по дате.
    """
    # Единый вход к git с таймаутом (см. shared/gitio). OSError (нет git) по-прежнему -> None.
    import subprocess
    try:
        rc, out, _ = git(root, "for-each-ref", "--sort=-creatordate",
                         "--format=%(refname:short)", "--count=1", "refs/tags", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    tag = out.splitlines() if rc == 0 else []
    return tag[0] if tag else None


def _read(root: Path, rel: str) -> str | None:
    p = Path(root) / rel
    try:
        return p.read_text(encoding="utf-8") if p.is_file() else None
    except OSError:
        return None


def _readme_summary(root: Path) -> tuple[str | None, str | None]:
    """Заголовок и первый содержательный абзац README. -> (title, description) или (None, None)."""
    text = _read(root, "README.md") or _read(root, "readme.md")
    if not text:
        return None, None
    title = None
    desc_lines = []
    for line in text.splitlines():
        s = line.strip()
        if title is None and s.startswith("#"):
            title = s.lstrip("#").strip()
            continue
        if title is not None:
            if not s or s.startswith(("#", "<!--", "![", "[!", "```", "|", "-", "*")):
                if desc_lines:
                    break
                continue
            desc_lines.append(s)
            if len(" ".join(desc_lines)) > 200:
                break
    return title, (" ".join(desc_lines) or None)


def _unknown(what: str) -> dict:
    return {"state": UNKNOWN, "value": f"_неизвестно — {what}_", "source": None}


def sections(repo_root: Path, evidence: dict | None = None) -> dict:
    """Разделы паспорта с источником и уровнем доверия. -> {section_title: {state, value, source}}.

    Разделы — РОВНО обязательные из реестра (`product_passport.structure.required_sections`), в том
    же порядке: сгенерированный паспорт обязан структурно совпасть со своим шаблоном.
    """
    root = Path(repo_root)
    ev = evidence if evidence is not None else repo_audit.discover(root)
    cls = repo_audit.classify(ev)

    title, desc = _readme_summary(root)
    version = (_read(root, "VERSION") or "").strip() or None
    releases = ev.get("release_history")
    # Свежий тег по дате; fallback на список repo_audit, если git недоступен (тогда честно «один из»).
    last_release = _latest_tag(root) or (releases[0] if releases else None)

    out = {}

    # 1. Название и описание — README, если есть; иначе честный пробел.
    if title or desc:
        out["Название и описание"] = {
            "state": INFERRED, "source": "README.md",
            "value": f"**{title or Path(root).name}** — {desc or '_описание в README не найдено_'}"}
    else:
        out["Название и описание"] = _unknown("в README нет заголовка/описания; назвать продукт "
                                              "должен владелец")

    # 2. Аудитория и проблема — НЕ выводится из кода (contour reconstruction: none).
    out["Аудитория и проблема"] = _unknown("аудитория и Problem/JTBD из кода не выводятся — нужен "
                                           "ответ владельца (контур product_strategy)")

    # 3. Repository и окружения — факт: имя репо, стек, наличие контейнеров/CI. production — за владельцем.
    stack = None
    prof = ev.get("profile") or {}
    if isinstance(prof, dict):
        langs = [s.get("language") for s in (prof.get("stacks") or []) if s.get("language")]
        stack = ", ".join(langs) if langs else None
    envs = []
    if ev.get("containers"):
        envs.append("контейнеры: " + ", ".join(ev["containers"]))
    if ev.get("ci"):
        envs.append("CI: " + ", ".join(ev["ci"][:3]))
    out["Repository и окружения"] = {
        "state": VERIFIED, "source": "структура репозитория",
        "value": f"Репозиторий `{_repo_name(root)}`" + (f", стек: {stack}" if stack else "")
                 + (". " + "; ".join(envs) if envs else "")
                 + ". Какое окружение считать production — за владельцем."}

    # 4. Owner и команда — намерение владельца, не факт репозитория.
    out["Owner и команда"] = _unknown("владелец и команда из кода достоверно не выводятся — назвать "
                                      "должен человек")

    # 5. Статус и зрелость — класс из classify() (inferred, подтверждает владелец).
    out["Статус и зрелость"] = {
        "state": INFERRED, "source": "repo_audit.classify",
        "value": f"Класс: **{cls.get('class')}** (уверенность {cls.get('confidence')}). "
                 f"{'; '.join(cls.get('reasons') or []) or 'признаки зрелости не собраны'}. "
                 f"Подтверждение владельца желательно."}

    # 6. Здоровье — три отдельных сигнала. Product: нечем измерить -> unknown, НЕ Green.
    tech = _tech_health(ev)
    out["Здоровье (продукт / технологии / delivery)"] = {
        "state": INFERRED if tech["state"] != UNKNOWN else UNKNOWN,
        "source": "CI/тесты (tech); метрики и наблюдения (product); релизы (delivery)",
        "value": f"Продукт: {_product_health(root)}. "
                 f"Технологии: **{tech['band']}** — {tech['reason']}. "
                 f"Delivery: {_delivery_health(ev)}."}

    # 7. Версия и последний релиз — VERSION + теги.
    if version or last_release:
        out["Версия и последний релиз"] = {
            "state": VERIFIED, "source": "VERSION, git tags",
            "value": (f"Версия: **{version}**. " if version else "Версия: _файл VERSION не найден_. ")
                     + (f"Последний релиз: **{last_release}**." if last_release
                        else "Релизов (тегов) не найдено.")}
    else:
        out["Версия и последний релиз"] = _unknown("нет ни VERSION, ни тегов — версию назвать нечем")

    # 8. Текущий milestone и прогресс — из плана/roadmap; иначе честный пробел.
    out["Текущий milestone и прогресс"] = _milestone(root)

    # 9. Риски и зависимости — зависимости выводимы (манифесты/API), риски продукта — нет.
    deps = list(ev.get("dependency_manifests") or []) + list(ev.get("api_schemas") or [])
    out["Риски и зависимости"] = {
        "state": INFERRED, "source": "манифесты зависимостей, API-схемы",
        "value": (f"Зависимости (по манифестам): {', '.join(deps)}. " if deps
                  else "Манифесты зависимостей не найдены. ")
                 + "Продуктовые риски из кода не выводятся — назвать должен владелец."}
    return out


def _tech_health(ev: dict) -> dict:
    """Техническое здоровье из наблюдаемого: CI + тесты. unknown, если дерево нечитаемо."""
    if not ev.get("tree_readable"):
        return {"state": UNKNOWN, "band": "unknown", "reason": "дерево репозитория нечитаемо"}
    ci = bool(ev.get("ci"))
    tests = (ev.get("test_files") or 0) > 0
    if ci and tests:
        return {"state": INFERRED, "band": "Green", "reason": "есть CI и тесты"}
    if ci or tests:
        return {"state": INFERRED, "band": "Yellow",
                "reason": "есть " + ("CI, но тестов не видно" if ci else "тесты, но CI не найден")}
    return {"state": INFERRED, "band": "Red", "reason": "ни CI, ни тестов не найдено"}


def _product_metrics_defined(root: Path) -> bool:
    """Заведён ли контракт метрик продукта — `ProductMetrics.md` заполнен СВЕРХ шаблона.

    Логику «шаблон vs заполнено» повторяем минимально (как `dashboard/build_data.py ->
    product_metrics_status`), НО без импорта dashboard: срезаем front-matter и заголовки — если
    остаётся содержательный текст, контракт метрик заведён, а не пустой каркас.
    """
    text = _read(root, "context/product/ProductMetrics.md")
    if not text:
        return False
    body = re.sub(r"^---.*?---", "", text, flags=re.DOTALL)   # front-matter
    body = re.sub(r"^#.*$", "", body, flags=re.MULTILINE)      # заголовки
    return bool(body.strip())


def _product_metric_observations(root: Path) -> int:
    """Сколько наблюдений продуктовых метрик снято. Устойчиво: нет файла/нет pyyaml -> 0.

    Берём `aggregate.n`, а если его нет — длину списка `observations`. Ничего не выдумываем: любой
    сбой чтения означает «наблюдений нет» (0), а не правдоподобное число.
    """
    text = _read(root, "context/product/metric-observations.yaml")
    if not text:
        return 0
    try:
        import yaml
    except ImportError:
        return 0
    try:
        data = yaml.safe_load(text) or {}
    except Exception:  # noqa: BLE001 — битый yaml: считаем «наблюдений нет», а не падаем
        return 0
    if not isinstance(data, dict):
        return 0
    n = (data.get("aggregate") or {}).get("n")
    if isinstance(n, int) and n >= 0:
        return n
    return len(data.get("observations") or [])


def _product_health(root: Path) -> str:
    """Строка продуктового здоровья ИЗ СОСТОЯНИЯ репозитория — без завышения вердикта.

    Ранее здесь стоял зашитый текст «неизвестно (нет метрик — контур analytics)», не смотревший,
    заведены ли метрики. Теперь три честных состояния (Green из этого раннего слоя НЕ ставим —
    скорингового экспорта `.ai-ops/product-metrics.yaml` у кита нет, а выборка мала):

      1. метрик нет и наблюдений нет -> «неизвестно — метрики продукта ещё не заведены»;
      2. метрики заведены + есть первые наблюдения (n>=1) -> «недостаточно данных для вердикта…
         (n=<n>) …, выборка мала, окна guardrail открыты»;
      3. метрики заведены, но наблюдений ещё нет -> «недостаточно данных — наблюдений ещё нет».
    """
    defined = _product_metrics_defined(root)
    n = _product_metric_observations(root)
    if not defined and n == 0:
        return "_неизвестно — метрики продукта ещё не заведены_"
    if n >= 1:
        return (f"_недостаточно данных для вердикта — метрики TTVO заведены, есть первые "
                f"наблюдения (n={n}), но выборка мала и окна guardrail ещё открыты_")
    return ("_недостаточно данных для вердикта — метрики TTVO заведены, "
            "но наблюдений ещё нет_")


def _delivery_health(ev: dict) -> str:
    rel = ev.get("release_history")
    if rel:
        return f"есть история релизов ({len(rel)} последних тегов) — _отклонение от плана нужно считать отдельно_"
    return "_неизвестно (истории релизов нет)_"


def _milestone(root: Path) -> dict:
    """Текущий milestone: сперва ИМЕНОВАННЫЙ из плана, иначе прокси из горизонта «Сейчас» ROADMAP.

    Прежде здесь был ТОЛЬКО прокси — первый пункт горизонта «Сейчас» из ROADMAP.md (INFERRED,
    «Из ROADMAP (Сейчас): …»): направление, а не названный результат. Теперь если план объявляет
    первоклассный `current_milestone` (`planning/plan.yaml`), паспорт печатает ИМЕННО его — имя,
    цель для пользователя и связанные направления. Ключа нет — откат к прежнему прокси целиком
    сохранён (молодой репозиторий без плана видит то же, что раньше).
    """
    # 1. ИМЕНОВАННЫЙ текущий milestone плана. Читаем через штатный загрузчик (он же резолвит путь
    #    плана в монорепо и падает PlanCorrupt на битом файле — на который откат к прокси честнее
    #    выдумывания вехи). Импорт локальный: планировщик тянут только когда паспорт правда строят.
    from ai_ops_kit.planning import delivery_plan as _plan
    try:
        plan = _plan.load(root)
    except _plan.PlanCorrupt:
        plan = None
    if plan:
        m = _plan.current_milestone(plan)
        if m and (str(m.get("name") or "").strip() or str(m.get("goal") or "").strip()):
            name = str(m.get("name") or "").strip() or str(m.get("id") or "").strip()
            goal = str(m.get("goal") or "").strip()
            linked = [str(g).strip() for g in (m.get("linked_goals") or []) if str(g).strip()]
            value = f"**{name}**" + (f" — {goal}" if goal else "")
            if linked:
                value += f" Связанные направления: {', '.join(linked)}."
            return {"state": VERIFIED, "source": "planning/plan.yaml -> current_milestone",
                    "value": value}

    # 2. Откат (прежнее поведение): прокси из горизонта «Сейчас» ROADMAP.
    # Горизонт читаем НЕ по литералу `Now`: канонический roadmap кита и русскоязычных дочек называет
    # его «Сейчас» (`ai_ops_kit/planning/roadmap.py -> HORIZONS`, где `now` = «сейчас»|«now»). Прежде
    # здесь стояло `if "now" in headers`, и на своём же roadmap кит выдавал «milestone неизвестно» —
    # паспорт не мог прочитать собственное направление. Теперь горизонт находит тот же парсер, что и
    # контракт roadmap, поэтому и «Сейчас», и «Now» одинаково распознаются.
    # Единый резолвер направления (SR-2): прежде читали `ROADMAP.md` ИЛИ `.ai-ops/ROADMAP.md` —
    # два пути в одной строке означали, что канонический источник не определён. Теперь путь решает
    # одно место (roadmap.resolve_roadmap_path), общее с health/drift/planning.
    from ai_ops_kit.planning import roadmap as _roadmap
    rp = _roadmap.resolve_roadmap_path(root)
    if rp.is_file():
        now = _roadmap.parse(rp.read_text(encoding="utf-8")).get("now") or {}
        items = now.get("items") or []
        if items:
            # Пункты «Сейчас» — маркированные строки; берём их первые строки (суть направления),
            # сняв markdown-маркер и inline-комментарии.
            body = " · ".join(_HTML_COMMENT.sub("", i).lstrip("-*").strip() for i in items)
            body = body.strip()
            if body:
                return {"state": INFERRED, "source": "ROADMAP.md -> Сейчас",
                        "value": f"Из ROADMAP (Сейчас): {body[:200]}"}
    return _unknown("текущий milestone нечем определить — ни ROADMAP (Сейчас), ни delivery-плана")


def generate(repo_root: Path, evidence: dict | None = None, reg: dict | None = None) -> str:
    """Собрать PRODUCT_PASSPORT.md из фактического состояния репозитория. -> markdown-текст.

    Совпадает по структуре со своим шаблоном (`product_passport`): маркер версии + все обязательные
    разделы. Версия берётся из реестра, чтобы проверка состояния дала Valid, а не Outdated.
    """
    reg = reg or AR.load()
    art = AR.artifact(reg, "product_passport") or {}
    version = (art.get("template") or {}).get("version", 1)
    secs = sections(repo_root, evidence)
    lines = [f"<!-- template-version: {version} -->",
             "<!-- сгенерировано из фактического состояния репозитория (PR-6); "
             "«_неизвестно_» — то, что кит из кода не выводит, а не пропуск -->",
             "# Product Passport", ""]
    for title, data in secs.items():
        lines.append(f"## {title}")
        lines.append(data["value"])
        if data.get("source"):
            lines.append(f"<!-- источник: {data['source']} · доверие: {data['state']} -->")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _split_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Разбить паспорт на (шапку, [(заголовок, тело)]).

    Тело — текст ПОСЛЕ строки `## <заголовок>` до следующего `## ` (или конца файла), включая свой
    источник-комментарий, ДОСЛОВНО (пробелы и переводы строк сохранены). Шапка — всё до первого `##`
    (маркер версии шаблона + преамбула + `# Product Passport`).
    """
    ms = list(_L2.finditer(text))
    if not ms:
        return text, []
    header = text[:ms[0].start()]
    secs = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        secs.append((m.group(1).strip(), text[m.end():end]))
    return header, secs


def _set_template_version(header: str, version) -> str:
    """Синхронизировать строку `<!-- template-version: N -->` в шапке с реестром (создать, если нет)."""
    line = f"<!-- template-version: {version} -->"
    new, n = re.subn(r"(?m)^<!-- template-version: \d+ -->[ \t]*$", line, header, count=1)
    return new if n else line + "\n" + header


def merge_owner_sections(existing: str, generated: str, reg: dict | None = None) -> str:
    """Свежие МАШИННЫЕ разделы из `generated`, но РАЗДЕЛЫ ВЛАДЕЛЬЦА и шапку-преамбулу — из `existing`.

    Инвариант перегенерации на релизе/вехе (см. `OWNER_SECTIONS`): машинные снимки (версия, здоровье,
    статус, milestone, риски) не должны отставать, а заполненные человеком разделы (Название,
    Аудитория и проблема, Owner и команда — grounded в VISION/AGENTS) генератор НЕ затирает на
    «неизвестно». Маркер версии шаблона синхронизируется с реестром. Порядок и разметку разделов
    берём из `existing` дословно; раздел, которого в `existing` ещё не было (структура генератора
    расширилась), дописывается в конце из `generated`.
    """
    reg = reg or AR.load()
    version = ((AR.artifact(reg, "product_passport") or {}).get("template") or {}).get("version", 1)
    _, gen_secs = _split_sections(generated)
    gen_body = dict(gen_secs)
    header, ex_secs = _split_sections(existing)
    out = [_set_template_version(header, version)]
    seen = set()
    for title, body in ex_secs:
        seen.add(title)
        # Владельческий раздел — дословно; машинный, известный генератору — свежий снимок; раздел,
        # который генератор не знает, тоже оставляем как есть (не роняем чужое содержимое).
        keep = title in OWNER_SECTIONS or title not in gen_body
        out.append(f"## {title}{body if keep else gen_body[title]}")
    for title, body in gen_secs:
        if title not in seen and title not in OWNER_SECTIONS:
            out.append(f"## {title}{body}")
    return "".join(out)


def is_filled(text: str, required_sections: list) -> tuple[bool, list]:
    """Заполнен ли паспорт СОДЕРЖАТЕЛЬНО, а не одними заголовками (F-018/F-027).

    -> (filled, пустые_разделы). Пустой = между заголовком и следующим нет ни одной содержательной
    строки (комментарии и пустые строки не считаются содержимым). Это и есть граница между «есть
    файл» и «заполнен».
    """
    no_comments = _HTML_COMMENT.sub("", text)
    positions = [(m.group(1).strip(), m.start(), m.end()) for m in _H.finditer(no_comments)]
    empty = []
    for sec in required_sections:
        idx = next((i for i, (h, _, _) in enumerate(positions) if sec in h), None)
        if idx is None:
            empty.append(sec)
            continue
        body_start = positions[idx][2]
        body_end = positions[idx + 1][1] if idx + 1 < len(positions) else len(no_comments)
        body = no_comments[body_start:body_end].strip()
        if not body:
            empty.append(sec)
    return (not empty, empty)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="passport_generator.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate"); g.add_argument("repo"); g.add_argument("-o", "--out")
    s = sub.add_parser("sections"); s.add_argument("repo"); s.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])

    if ns.cmd == "generate":
        text = generate(Path(ns.repo))
        if ns.out:
            Path(ns.out).write_text(text, encoding="utf-8")
            print(f"паспорт записан: {ns.out}")
        else:
            print(text)
        return 0

    secs = sections(Path(ns.repo))
    if ns.json:
        print(json.dumps(secs, ensure_ascii=False, indent=2)); return 0
    for title, d in secs.items():
        print(f"[{d['state']:8}] {title}: {d['value'][:100]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
