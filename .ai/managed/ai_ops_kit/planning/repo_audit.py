#!/usr/bin/env python3
"""Первый сценарий AI Ops: превратить репозиторий в управляемый продуктовый (v3.35.0).

    «Установи AI Ops»
        -> INSTALL      (installer: окружение, установка, doctor — не здесь)
        -> DISCOVER     изучить репозиторий
        -> CLASSIFY     new / early / existing / unknown
        -> RECONSTRUCT  восстановить всё, что можно ДОКАЗАТЬ
        -> AUDIT        сравнить с моделью продуктового репозитория
        -> ASK          одним пакетом запросить недостающее ЧЕЛОВЕЧЕСКОЕ знание
        -> BOOTSTRAP    создать направление и план из фактов (`product_bootstrap.py`, не этот модуль)
        -> PLAN         roadmap + delivery plan + состояние работы
        -> RECOMMEND    «вот что имеет смысл делать следующим» (`next_work.py`)

Здесь — DISCOVER…ASK. Онбординг заканчивается не документацией, а работой, поэтому финал сценария
считает `next_work.py`, а не этот модуль.

ТРИ ПРАВИЛА, БЕЗ КОТОРЫХ ЭТО ГЕНЕРАТОР ДОКУМЕНТАЦИИ, А НЕ ОНБОРДИНГ:

1. **PROVENANCE.** У каждого утверждения baseline есть статус и доказательство. Пользователь
   обязан видеть разницу между «увидел», «вывел», «спросил у человека» и «не знаю». `inferred`
   не публикуется как `verified` — состояние `verified` даёт факт репозитория, а не убедительность
   догадки.

2. **НЕ СПРАШИВАТЬ ТО, ЧТО ВИДНО.** Спрашивать «используется ли PostgreSQL» у репозитория с
   `alembic/` и `psycopg2` в зависимостях — неуважение к времени владельца. Вопрос задаётся
   ТОЛЬКО там, где контур объявлен невосстановимым (`reconstruction.ability`), и по возможности с
   предложением: «по коду предполагаю X — подтвердить?».

3. **ASK ONCE.** Не двадцать вопросов по одному, а один понятный пакет: «я могу сам собрать 5 из 8
   областей, для остальных нужны решения». Двадцать последовательных вопросов человек не
   доводит до конца, и онбординг остаётся половинчатым — это дороже, чем один длинный вопрос.

Использование:
  repo_audit.py <repo> [--json]            # весь сценарий DISCOVER..ASK
  repo_audit.py <repo> --classify [--json] # только классификация
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from ai_ops_kit.shared.gitio import git
from ai_ops_kit.planning import contours as _contours

# Классификация зрелости, реконструкция и состояние контуров вынесены в проб-свободный спутник
# repo_audit_analysis.py (module-size, структурный разрез). Статусы provenance и порог свежести
# объявлены ТАМ (одно место), а фасад ре-экспортирует их прежними именами: внешний код и тесты
# продолжают читать `repo_audit.VERIFIED`, `repo_audit.classify` и т.д. Направление импортов
# одностороннее — спутник фасад НЕ импортирует, обратного ребра нет.
from ai_ops_kit.planning.repo_audit_analysis import (  # noqa: E402,F401
    CONFLICTING,
    INFERRED,
    MISSING,
    PARTIAL,
    STALE,
    STALE_AFTER_DAYS,
    UNKNOWN,
    USER_CONFIRMED,
    VERIFIED,
)

# Расширения, считающиеся исходным кодом при классификации. Список узкий сознательно: markdown и
# yaml не делают репозиторий продуктом, иначе папка с документацией классифицировалась бы как
# EARLY_PRODUCT.
_SRC_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt", ".rb", ".php",
            ".swift", ".cs", ".c", ".cc", ".cpp", ".h", ".hpp", ".scala", ".ex", ".exs", ".dart",
            ".vue", ".svelte", ".sql"}
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next",
              "target", "vendor", ".ai", ".mypy_cache", ".pytest_cache", ".ruff_cache",
              # `.claude/worktrees/*` — копии этого же репозитория. Без исключения кит
              # предъявлял бы как доказательство путь во временной копии самого себя.
              ".claude"}


def _git(root: Path, *args):
    """git с проглоченной ошибкой (единый вход с таймаутом, см. shared/gitio). -> stdout|None.
    None означает «история не читается», и это НЕ ноль коммитов: разница меняет класс репозитория."""
    try:
        rc, out, _ = git(root, *args, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return out if rc == 0 else None


def _product_ci(root) -> list:
    """Признаки CI, ПРИНАДЛЕЖАЩЕГО продукту (не поставленного китом). -> [путь].

    `.github/workflows` считается признаком CI только если в нём есть хотя бы один workflow,
    который положил не кит. Файлы кита узнаются по имени (`ai-ops-*.yml`) — так их называет
    установщик, и это же имя проверяет `validate_ci_templates`.
    """
    from pathlib import Path as _P
    root = _P(root)
    found = []
    wf = root / ".github" / "workflows"
    if wf.is_dir():
        own = [f for f in wf.iterdir()
               if f.is_file() and f.suffix in (".yml", ".yaml") and not f.name.startswith("ai-ops-")]
        if own:
            found.append(".github/workflows")
    for rel in (".gitlab-ci.yml", "Jenkinsfile", ".circleci"):
        if (root / rel).exists():
            found.append(rel)
    return found


def _measure_storybook(root: Path) -> dict:
    """Измерить профиль Storybook ЗАМЕРОМ, а не package.json (storybook-profile-measured).

    Возвращает: {present: bool, config: bool, stories: int, build_script: bool, version: str|None}.
    present = config существует И stories существуют (не только dependency в package.json).
    """
    result = {"present": False, "config": False, "stories": 0,
              "build_script": False, "version": None}

    # Конфигурация: .storybook/ каталог или storybook в package.json scripts
    config_dir = root / ".storybook"
    has_config = config_dir.is_dir() and any(config_dir.glob("main.*"))
    result["config"] = has_config

    # Stories: файлы *.stories.* (измеряем количеством, не наличием)
    story_count = 0
    for ext in ("*.stories.tsx", "*.stories.ts", "*.stories.jsx", "*.stories.js",
                "*.stories.mdx"):
        story_count += len(list(root.rglob(ext)))
        if story_count >= 100:  # кап, чтобы не считать вечно
            break
    result["stories"] = story_count

    # Build script: storybook в package.json scripts
    pkg_json = root / "package.json"
    if pkg_json.is_file():
        try:
            import json
            pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
            scripts = pkg.get("scripts", {})
            result["build_script"] = any("storybook" in v for v in scripts.values()
                                         if isinstance(v, str))
            # Версия из dependencies
            for dep_key in ("dependencies", "devDependencies"):
                deps = pkg.get(dep_key, {})
                for name in ("@storybook/react", "@storybook/vue", "@storybook/svelte",
                             "@storybook/web-components", "storybook"):
                    if name in deps:
                        result["version"] = deps[name]
                        break
                if result["version"]:
                    break
        except (OSError, ValueError, KeyError):
            pass

    # present = config + stories (не только dependency)
    result["present"] = has_config and story_count > 0
    return result


def discover(child_root) -> dict:
    """DISCOVER — что фактически лежит в репозитории. Только факты + их источник.

    Ничего не интерпретирует: интерпретация — следующий шаг, и смешивать их значит терять
    возможность сказать «это вывод, а не наблюдение».
    """
    root = Path(child_root)
    ev = {}

    src, tests = 0, 0
    readable = root.is_dir()
    if readable:
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            # ОТНОСИТЕЛЬНО корня, а не абсолютно. `p.parts` включает предков корня, поэтому
            # репозиторий, лежащий под каталогом с именем `build`, `dist`, `vendor` или
            # `.claude/worktrees/*` (штатное место работы агентов Claude Code), терял ВЕСЬ код:
            # `source_files: 0` -> класс NEW_PRODUCT с уверенностью high и «работающей системы я не
            # нашёл» на живом продукте. Ниже, в `_find`, фильтр с самого начала был относительным —
            # два разных правила в одном модуле.
            if any(part in _SKIP_DIRS for part in p.relative_to(root).parts):
                continue
            if p.suffix.lower() in _SRC_EXT:
                src += 1
                name = p.name.lower()
                if name.startswith("test_") or name.endswith(("_test.py", ".test.ts", ".test.tsx",
                                                              ".test.js", ".spec.ts", ".spec.js")):
                    tests += 1

    commits = None
    if (root / ".git").exists():
        out = _git(root, "rev-list", "--count", "HEAD")
        if out and out.isdigit():
            commits = int(out)
    tags = _git(root, "tag", "--list")

    def _exists(*rels):
        return [r for r in rels if (root / r).exists()]

    ev["tree_readable"] = readable
    ev["source_files"] = src if readable else None
    ev["test_files"] = tests if readable else None
    ev["commits"] = commits
    ev["release_history"] = [t for t in (tags or "").splitlines() if t][:5] if tags is not None else None
    # CI ПРОДУКТА, А НЕ СВОЙ (F-019, живой прогон severnaya_traektoriya 2026-08-12). Каталог
    # `.github/workflows` создаёт САМ установщик — он кладёт туда `ai-ops-*.yml`. Прежде условие
    # «каталог существует» выполнялось этими файлами, и в репозитории БЕЗ собственного CI кит
    # объявлял владельцу «ci_pipeline: build/lint/test, status=verified», ссылаясь на артефакты,
    # которые сам же и записал секунду назад. Придуманный продуктовый факт со статусом
    # «подтверждено» — худший из возможных: владелец верит, что кит прочитал ЕГО конвейер.
    ev["ci"] = _product_ci(root)
    ev["containers"] = _exists("Dockerfile", "docker-compose.yml", "docker-compose.yaml")
    ev["dependency_manifests"] = _exists("package.json", "requirements.txt", "pyproject.toml",
                                         "go.mod", "Cargo.toml", "pom.xml", "Gemfile",
                                         "composer.json")

    def _find(pattern, only_dirs=False, limit=3):
        """Поиск по дереву с теми же исключениями, что при подсчёте кода.

        Без исключений кит предъявлял в доказательство пути внутри `.claude/worktrees/*` — копий
        самого себя. Доказательство, указывающее на временную копию, доказательством не является.
        """
        if not readable:
            return None
        out = []
        for p in root.glob(pattern):
            if any(part in _SKIP_DIRS for part in p.relative_to(root).parts):
                continue
            if only_dirs and not p.is_dir():
                continue
            out.append(str(p.relative_to(root)))
            if len(out) >= limit:
                break
        return out

    ev["migrations"] = _find("**/migrations", only_dirs=True)
    ev["api_schemas"] = _exists("openapi.yaml", "openapi.json", "schema.graphql") + \
                        (_find("**/*.proto") or [])
    ev["product_docs"] = _exists("README.md", "docs", "context/product")
    ev["architecture_docs"] = _exists("context/system", "docs/architecture", "ARCHITECTURE.md")
    ev["decisions"] = _exists("decisions", "docs/adr")
    ev["env_configs"] = _exists(".env.example", ".env.sample", "config")

    # storybook-profile-measured: профиль Storybook измеряется ЗАМЕРОМ, а не package.json.
    # F-019 класс: доказательство, указанное китом, указывает на артефакт, который кит сам создал.
    # Здесь: наличие @storybook/react в package.json — не доказательство работающего Storybook.
    # Доказательство: конфигурация существует + stories существуют + build-скрипт есть.
    ev["storybook"] = _measure_storybook(root)

    try:
        from ai_ops_kit.shared import project_detector
        ev["profile"] = project_detector.detect(root)
    except Exception as e:                             # noqa: BLE001 — детект стека не обязан ронять онбординг
        ev["profile"] = None
        ev["profile_error"] = f"{type(e).__name__}: {e}"
    return ev


# Работа с файлом ответов владельца (`.ai/project/onboarding-answers.yaml`) вынесена в
# проб-свободный спутник repo_audit_answers.py (module-size, P2). Ре-экспорт сохраняет прежние имена:
# внешние импортёры (`repo_audit.write_question_file`, `from ...repo_audit import read_answers,
# answers_path, AnswersCorrupt`) продолжают работать. Поведение не менялось — чистый перенос.
from ai_ops_kit.planning.repo_audit_answers import (  # noqa: E402,F401
    ANSWERS_REL,
    AnswersCorrupt,
    _ANSWER_KEY_RE,
    _inline_comment,
    _owner_comments,
    answers_path,
    read_answers,
    record_answer,
    write_question_file,
)


def audit(child_root, evidence: dict | None = None, model: dict | None = None) -> dict:
    """AUDIT — состояние репозитория против модели продуктового репозитория.

    Не `present/absent`, а семь состояний + кто закрывает пробел: кит сам, кит с подтверждением
    или только человек. Именно эта таблица отличает онбординг от генератора документации.
    """
    model = model or _contours.load_model()
    evidence = evidence if evidence is not None else discover(child_root)
    rows = [_contour_state(child_root, c, evidence, model)
            for c in (model.get("contours") or [])]
    by_state = {}
    for r in rows:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    ai_only = [r["contour"] for r in rows
               if r["state"] != VERIFIED and r["ai_can_reconstruct"] == "full" and not r["questions"]]
    human = [r["contour"] for r in rows if r["state"] != VERIFIED and r["needs_human"]]
    # Блокирующий пробел — ЛЮБОЕ незакрытое состояние на блокирующем ярусе, а не только полное
    # отсутствие. Прежде считались лишь missing/unknown, поэтому контур яруса `required_now` в
    # состоянии `partial` объявлялся незаблокированным — и один отчёт давал два противоположных
    # ответа о нём (`gap_plan` его блокирующим считал). В child-репозитории кита `.ai-ops.yaml`
    # есть ВСЕГДА, значит контур границ AI не попадал в blocking_gaps никогда.
    blocking_tiers = {t.get("id") for t in (model.get("gap_tiers") or []) if t.get("blocks_work")}
    # SR-1: версия стандарта репозитория (отдельная от версии пакета) + сходится ли отпечаток
    # состава требований с объявленным. Расхождение = требования правили, а версию не подняли.
    from ai_ops_kit.planning import standard as _standard
    std = {"version": _standard.current_version(),
           "fingerprint_in_sync": _standard.load().get("requirements_fingerprint")
           == _standard.compute_fingerprint()}
    return {"contours": rows, "by_state": by_state,
            "ready": [r["contour"] for r in rows if r["state"] == VERIFIED],
            "ai_can_build": ai_only, "needs_human": human,
            "standard": std,
            "blocking_gaps": [r["contour"] for r in rows
                              if r["state"] != VERIFIED and r["gap_tier"] in blocking_tiers]}


def gap_plan(audit_rep: dict, model: dict | None = None) -> dict:
    """Progressive Gap Plan: пробелы по СРОЧНОСТИ, а не одним списком из четырнадцати пунктов.

    «Сначала заполните 14 документов, потом можете работать» убивает продукт, которому три года.
    Поэтому: что нужно сейчас, что понадобится при следующем изменении соответствующего контура,
    что кит достроит сам, что улучшается по ходу.
    """
    model = model or _contours.load_model()
    tiers = {t["id"]: {"means": t.get("means"), "blocks_work": t.get("blocks_work", False),
                       "contours": []} for t in (model.get("gap_tiers") or [])}
    for r in audit_rep["contours"]:
        if r["state"] == VERIFIED:
            continue
        tiers.setdefault(r["gap_tier"], {"means": "", "blocks_work": False, "contours": []})
        tiers[r["gap_tier"]]["contours"].append(
            {"contour": r["contour"], "state": r["state"],
             "ai_can_reconstruct": r["ai_can_reconstruct"], "needs_human": r["needs_human"]})
    return tiers


def question_package(audit_rep: dict, reconstructed: dict | None = None) -> dict:
    """ASK ONCE — один пакет вопросов вместо двадцати по одному.

    Спрашивается ТОЛЬКО то, что из репозитория не выводится. Где есть основание, к вопросу
    прикладывается предложение («по коду предполагаю X — подтвердить?»): подтвердить дешевле, чем
    сформулировать, и это прямо снижает усилие владельца.
    """
    rec = reconstructed or {}
    qs = []
    for r in audit_rep["contours"]:
        if r["state"] == VERIFIED and not r["questions"]:
            continue
        for q in r["questions"]:
            hint = rec.get(q["id"])
            if hint and hint.get("status") == USER_CONFIRMED:
                continue                               # подтверждённое владельцем не переспрашиваем
            proposal = None
            if hint and hint.get("value") and hint.get("status") in (VERIFIED, INFERRED):
                proposal = {"value": hint["value"], "status": hint["status"],
                            "evidence": hint.get("evidence") or []}
            qs.append({"id": q["id"], "contour": r["contour"], "ask": q["ask"],
                       "why": f"контур «{r['title']}» в состоянии {r['state']}, "
                              f"из репозитория не выводится",
                       "proposal": proposal, "blocks_work": r["gap_tier"] == "required_now"})
    covered = len(audit_rep["ready"]) + len(audit_rep["ai_can_build"])
    total = len(audit_rep["contours"])
    n = len(qs)
    word = ("решение" if n % 10 == 1 and n % 100 != 11 else
            "решения" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "решений")
    return {"can_build_without_human": covered, "contours_total": total,
            "questions": qs,
            "summary": (f"могу самостоятельно собрать {covered} из {total} контуров; "
                        + (f"для остальных нужно {n} {word} владельца" if n
                           else "вопросов к владельцу нет"))}


def run(child_root) -> dict:
    """Весь сценарий DISCOVER..ASK одним вызовом. BOOTSTRAP/PLAN/RECOMMEND — выше, в CLI."""
    model = _contours.load_model()
    ev = discover(child_root)
    cls = classify(ev, model)
    rec = reconstruct(child_root, ev, model)
    aud = audit(child_root, ev, model)
    # Противоречия источников истины — на верхний уровень отчёта: их поднимает reconstruct на
    # факте, но человеку (`model`) и презентеру нужен явный список, а не раскопки в факте.
    conflicts = (rec.get("persistence") or {}).get("conflicts") or []
    return {"schema_version": 1, "kind": "repository-understanding",
            "classification": cls, "evidence": ev, "reconstructed": rec, "audit": aud,
            "conflicts": conflicts,
            "gap_plan": gap_plan(aud, model), "ask": question_package(aud, rec)}


def render(rep: dict) -> str:
    """Человеческий отчёт. Порядок разделов — порядок сценария, всегда одинаковый."""
    L = []
    cls = rep["classification"]
    L.append(f"КЛАССИФИКАЦИЯ: {cls['class']} (уверенность {cls['confidence']}) · "
             f"онбординг {cls['onboarding']}")
    for r in cls["reasons"]:
        L.append(f"  · {r}")

    L.append("\nЧТО Я ПОНЯЛ О ПРОЕКТЕ (со статусом и основанием)")
    for k, v in rep["reconstructed"].items():
        if v["status"] == UNKNOWN and not v.get("value"):
            continue
        val = v["value"] if not isinstance(v["value"], list) else ", ".join(map(str, v["value"]))
        L.append(f"  {v['status']:9} {k}: {val}")
        if v.get("evidence"):
            L.append(f"            основание: {', '.join(map(str, v['evidence'][:4]))}")
        if v.get("note"):
            L.append(f"            {v['note']}")
    unknowns = [k for k, v in rep["reconstructed"].items() if v["status"] == UNKNOWN]
    if unknowns:
        L.append(f"  не знаю (и не выдумываю): {', '.join(unknowns)}")

    conflicts = rep.get("conflicts") or []
    if conflicts:
        L.append("\nПРОТИВОРЕЧИЯ ИСТОЧНИКОВ (не выбираю сторону молча)")
        for c in conflicts:
            L.append(f"  ⚠ {c['category']}: {c['summary']}")
            for cl in c["claims"]:
                L.append(f"      {cl['source']} ({cl['path']}): {', '.join(cl['values'])}")

    L.append("\nКОНТУРЫ МОДЕЛИ")
    for r in rep["audit"]["contours"]:
        who = ("пробел закрыт" if r["state"] == VERIFIED
               else "нужен человек" if r["needs_human"]
               else "кит восстановит сам" if r["ai_can_reconstruct"] == "full"
               else "кит восстановит частично")
        L.append(f"  {r['state']:9} {r['title']} · {who} · срочность {r['gap_tier']}")
        if r["missing_required"]:
            L.append(f"            нет: {', '.join(r['missing_required'])}")
        # SR-6: пустая секция — отдельная находка, не «есть». Заголовок без тела выглядит закрытым.
        for sf in r.get("section_findings") or []:
            if sf["empty_sections"]:
                L.append(f"            {sf['path']}: разделы есть, но ПУСТЫ (выглядят закрытыми): "
                         f"{', '.join(sf['empty_sections'])}")
            if sf["missing_sections"]:
                L.append(f"            {sf['path']}: нет разделов: {', '.join(sf['missing_sections'])}")

    L.append("\nПЛАН ДОСТРОЙКИ (progressive — не «заполните 14 документов»)")
    for tid, t in rep["gap_plan"].items():
        if not t["contours"]:
            continue
        mark = "⚠" if t["blocks_work"] else "·"
        L.append(f"  {mark} {tid}: {', '.join(c['contour'] for c in t['contours'])}")

    ask = rep["ask"]
    L.append(f"\nВОПРОСЫ ОДНИМ ПАКЕТОМ — {ask['summary']}")
    for q in ask["questions"]:
        L.append(f"  {'⚠' if q['blocks_work'] else '·'} [{q['contour']}] {q['ask']}")
        if q["proposal"]:
            pv = q["proposal"]["value"]
            L.append(f"      предполагаю: {pv} ({q['proposal']['status']}) — подтвердить?")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="repo_audit.py")
    ap.add_argument("repo")
    ap.add_argument("--classify", action="store_true", help="только классификация")
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])
    root = Path(ns.repo)
    try:
        if ns.classify:
            rep = classify(discover(root))
        else:
            rep = run(root)
    except _contours.ModelCorrupt as e:
        print(f"ОШИБКА: {e}")
        return 1
    if ns.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    elif ns.classify:
        print(f"{rep['class']} (уверенность {rep['confidence']}) · онбординг {rep['onboarding']}")
        for r in rep["reasons"]:
            print(f"  · {r}")
    else:
        print(render(rep))
    return 0


# Аналитическое ядро (классификация зрелости + реконструкция + состояние/устаревание контуров)
# живёт в спутнике repo_audit_analysis.py. Явный ре-экспорт сохраняет прежнюю публичную поверхность
# фасада: `repo_audit.classify`, `repo_audit.reconstruct`, `repo_audit._contour_state` и т.д.
# продолжают импортироваться отсюда. `audit`/`run` выше зовут эти имена как модульные глобали —
# ре-экспорт делает их доступными в этом пространстве имён. Обратного импорта нет.
from ai_ops_kit.planning.repo_audit_analysis import (  # noqa: E402,F401
    _contour_state,
    _is_stale,
    _maturity,
    _reviewed_at,
    classify,
    owner_confirmed,
    reconstruct,
)


if __name__ == "__main__":
    sys.exit(main())
