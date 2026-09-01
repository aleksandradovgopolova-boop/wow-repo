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
import re
import subprocess
import sys
from pathlib import Path

import yaml

from ai_ops_kit.planning import contours as _contours

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

# Сколько дней документ контура считается свежим. `stale` объявлен в словаре состояний модели,
# значит обязан ВЫВОДИТЬСЯ, а не быть словом в реестре: капабилити, которую никто не считает, —
# ровно то, что инварианты кита запрещают объявлять.
STALE_AFTER_DAYS = 180

VERIFIED, INFERRED, PARTIAL, MISSING, UNKNOWN, STALE, USER_CONFIRMED = (
    "verified", "inferred", "partial", "missing", "unknown", "stale", "user_confirmed")


def _git(root: Path, *args):
    """git с проглоченной ошибкой. -> stdout|None. None означает «история не читается»,
    и это НЕ ноль коммитов: разница между ними меняет класс репозитория."""
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                           timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


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


def classify(evidence: dict, model: dict | None = None) -> dict:
    """CLASSIFY — новый продукт, ранний, существующий или неизвестно.

    UNKNOWN — не «пустой». Пустой репозиторий ЧИТАЕТСЯ и даёт нули; нечитаемый не даёт ничего.
    Разница определяет, начинать ли bootstrap или сначала спросить, и поэтому она в коде, а не в
    интуиции читателя.
    """
    model = model or _contours.load_model()
    th = ((model.get("classification") or {}).get("thresholds") or {})
    src = evidence.get("source_files")
    commits = evidence.get("commits")

    if not evidence.get("tree_readable") or src is None:
        return {"class": "UNKNOWN", "confidence": "none",
                "reasons": ["дерево репозитория не читается — сигналы классификации недоступны"],
                "onboarding": "ask_before_acting"}

    has_ci = bool(evidence.get("ci"))
    has_tests = bool(evidence.get("test_files"))
    has_migr = bool(evidence.get("migrations"))
    has_rel = bool(evidence.get("release_history"))
    reasons = []

    if src <= th.get("new_max_source_files", 10) and not has_ci and not has_tests:
        if commits is None:
            reasons.append(f"файлов кода {src}, CI и тестов нет; история git не читается")
            conf = "medium"
        elif commits <= th.get("new_max_commits", 5):
            reasons.append(f"файлов кода {src}, коммитов {commits}, CI и тестов нет")
            conf = "high"
        else:
            reasons.append(f"файлов кода {src}, но коммитов {commits} — это не scaffold")
            return {"class": "EARLY_PRODUCT", "confidence": "medium", "reasons": reasons,
                    "onboarding": "reconstruct_then_bootstrap"}
        return {"class": "NEW_PRODUCT", "confidence": conf, "reasons": reasons,
                "onboarding": "product_bootstrap"}

    strong = sum([has_ci, has_tests, has_migr, has_rel])
    if commits is not None and commits >= th.get("existing_min_commits", 50) and strong >= 1:
        reasons.append(f"коммитов {commits}, файлов кода {src}")
        reasons.append("признаки живой системы: " + ", ".join(
            n for n, v in (("CI", has_ci), ("тесты", has_tests), ("миграции", has_migr),
                           ("релизы", has_rel)) if v))
        return {"class": "EXISTING_PRODUCT", "confidence": "high", "reasons": reasons,
                "onboarding": "reconstruct_first"}
    if strong >= 3:
        reasons.append("история короткая или не читается, но CI, тесты и данные на месте")
        return {"class": "EXISTING_PRODUCT", "confidence": "medium", "reasons": reasons,
                "onboarding": "reconstruct_first"}

    reasons.append(f"код есть (файлов {src}), продуктовой и архитектурной истории почти нет")
    if commits is not None:
        reasons.append(f"коммитов {commits}")
        # Порог `early_max_commits` объявлен в реестре и ОБЯЗАН читаться: реестр обещает, что пороги
        # правятся данными живых прогонов, а не кодом. Прежде он не читался никем — объявление без
        # реализации, то есть в точности то, что инварианты кита запрещают.
        early_max = th.get("early_max_commits", 50)
        if commits > early_max:
            reasons.append(f"история длиннее порога ранней стадии ({early_max}), но признаков "
                           f"живой системы (CI, тесты, миграции, релизы) меньше двух — "
                           f"состояние определяется как раннее осознанно")
    return {"class": "EARLY_PRODUCT", "confidence": "medium" if commits is not None else "low",
            "reasons": reasons, "onboarding": "reconstruct_then_bootstrap"}


ANSWERS_REL = ".ai/project/onboarding-answers.yaml"


def answers_path(child_root) -> Path:
    return Path(child_root) / ANSWERS_REL


class AnswersCorrupt(Exception):
    """Файл ответов онбординга существует, но не разбирается.

    Отдельный тип, а не `return {}`: «ответов нет» и «ответы есть, но прочитать нечем» — разные
    ответы, и второй НЕ даёт права переспрашивать и перезаписывать. Ровно так был потерян
    заполненный файл в живом прогоне niti (F-023).
    """


def read_answers(child_root) -> dict:
    """Ответы человека из файла онбординга. Пустое значение — это «ещё не ответил», а не ответ."""
    p = answers_path(child_root)
    if not p.is_file():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        # НЕРАЗОБРАННЫЙ ФАЙЛ — ЭТО НЕ «ОТВЕТОВ НЕТ» (F-023, живой прогон niti 2026-08-12).
        # Прежде здесь стоял `return {}`, и это было началом потери данных: кит получал «ответов
        # нет», переспрашивал всё заново и ПЕРЕЗАПИСЫВАЛ файл пустым. Ответы владельца исчезали
        # молча — вместе с тем, что он вписал руками. Тот же инвариант, что «не смог прочитать» !=
        # «нет»: см. DeliveryReceipt и реестр гейтов.
        raise AnswersCorrupt(f"{p}: файл ответов не разбирается ({e}) — починить, а не отвечать "
                             f"заново; иначе ответы будут перезаписаны пустыми") from e
    ans = (data.get("answers") or {}) if isinstance(data, dict) else {}
    return {k: v for k, v in ans.items()
            if v not in (None, "", [], {}) and str(v).strip() not in ("", "?")}


_ANSWER_KEY_RE = re.compile(r"^ {2}([A-Za-z_][A-Za-z0-9_.-]*):(?:\s|$)")


def _inline_comment(rest: str) -> str:
    """Комментарий в хвосте строки значения. -> `# …` или пустая строка.

    Резать по первому `#` нельзя: значение пишется как JSON-строка и `#` внутри кавычек — часть
    ОТВЕТА, а не комментарий (`goal: "рост #1 по выручке"`). Поэтому кавычки считаются, экранирование
    учитывается, и решётка признаётся началом комментария только вне кавычек и после пробела —
    ровно правило YAML.
    """
    in_quotes, escaped = False, False
    for i, ch in enumerate(rest):
        if escaped:
            escaped = False
            continue
        if in_quotes and ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            continue
        if ch == "#" and not in_quotes and (i == 0 or rest[i - 1] in " \t"):
            return rest[i:].rstrip()
    return ""


def _owner_comments(text: str, kit_lines: set) -> dict:
    """Что в существующем файле написал ЧЕЛОВЕК, а не кит. -> {header, before, inline, tail}.

    ЗАЧЕМ (F-020, живой прогон niti 2026-08-12). `write_question_file` пересобирает файл целиком:
    значения переносились через `read_answers`, а комментарии — нет. Владелец вписал к каждому
    ответу источник (`file:line`), и после следующего `ai-ops model` их осталось НОЛЬ строк. Для
    файла, чья роль — «подтверждённые факты», это потеря ОСНОВАНИЯ: утверждение остаётся, а отличить
    подтверждённое от переписанного больше нечем. Обход в том прогоне — прятать ссылки внутрь
    значений — был обходом, а не решением.

    ЧЕЙ КОММЕНТАРИЙ — РЕШАЕТСЯ СРАВНЕНИЕМ С ТЕМ, ЧТО КИТ ПИШЕТ САМ (`kit_lines`), а не догадкой по
    форме. Строка, совпавшая с собственной строкой кита, принадлежит киту и будет сгенерирована
    заново; всё остальное — владельца и переносится дословно.

    ГРАНИЦА НАЗВАНА, А НЕ СПРЯТАНА: если формулировка вопроса ИЗМЕНИЛАСЬ между версиями кита, старая
    строка перестаёт совпадать с новой и будет сохранена как авторская — один раз, при первом
    прогоне после обновления. Выбор осознанный: лишняя строка видна и удаляется рукой, а потерянное
    основание не восстанавливается ничем. Обратный порядок предпочтений сделал бы ровно тот дефект,
    который здесь чинится.
    """
    header, before, inline, tail = [], {}, {}, []
    pending, in_answers = [], False
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not in_answers:
            if stripped == "answers:":
                in_answers = True
            elif stripped.startswith("#") and line not in kit_lines:
                header.append(stripped)
            continue
        m = _ANSWER_KEY_RE.match(line)
        if m:
            qid = m.group(1)
            if pending:
                before[qid] = list(pending)
                pending = []
            c = _inline_comment(line[m.end(1) + 1:])
            if c:
                inline[qid] = c
            continue
        if stripped.startswith("#") and line not in kit_lines:
            pending.append(stripped)
    tail = pending                      # комментарии после последнего ключа — тоже человеческие
    return {"header": header, "before": before, "inline": inline, "tail": tail}


def write_question_file(child_root, ask: dict):
    """Создать/дополнить файл, в который человек ВПИШЕТ ответы. -> путь.

    Прежде `ai-ops model` печатал вопросы и ЗАВЕРШАЛСЯ: куда писать ответы, не сказано, интерактива
    нет — человек прочитал двенадцать вопросов и не может ответить. Тупик на главном шаге первого
    сценария, при том что онбординг обещал «соберу материалы и покажу на проверку».

    Существующие ответы НЕ затираются никогда: файл дополняется новыми вопросами, ответы остаются.
    ВМЕСТЕ С НИМИ ОСТАЮТСЯ КОММЕНТАРИИ ВЛАДЕЛЬЦА (F-020): и те, что стоят над ответом, и хвостовые
    в той же строке. Ответ без основания — это утверждение, которое нечем проверить, а основание
    владелец пишет именно комментарием: `main_goal_now: "…"  # docs/product.md:14`.

    ЛИШНЕЙ ЗАПИСИ НЕ ДЕЛАЕМ. Если содержимое не изменилось, файл не перезаписывается: `ai-ops model`
    зовут и просто «посмотреть состояние», и трогать mtime (а в чужом репозитории — показывать файл
    изменённым в `git status`) ради того же текста команда не вправе. Это единственный файл, который
    она создаёт, и создаёт ровно потому, что вопросам нужно место для ответа.
    """
    p = answers_path(child_root)
    # Если файл есть, но не читается — НЕ перезаписываем: это уничтожило бы ответы владельца.
    # Пусть ошибка дойдёт до человека словами «починить, а не отвечать заново» (F-023).
    existing = read_answers(child_root)
    header = [
        "# Ответы владельца на вопросы онбординга AI Ops.",
        "#",
        "# Здесь только то, что из кода честно не выводится: цели продукта, его пользователи,",
        "# границы, которые нельзя нарушать. Кит эти вещи не выдумывает — поэтому спрашивает.",
        "#",
        "# Как отвечать: впишите значение после двоеточия. Пустая строка означает «ещё не ответил»,",
        "# и вопрос останется. Ответ сильнее любого вывода кита: он становится подтверждённым фактом",
        "# (`user_confirmed`) и больше не переспрашивается.",
        "#",
        "# После заполнения запустите снова:  ./ai-ops model",
    ]
    # Строки, которые кит пишет САМ, — эталон для разбора «чей комментарий» (F-020). Собираются до
    # генерации, потому что разбор существующего файла обязан знать их заранее.
    kit_comments, kit_lines = {}, set(header)
    for q in ask.get("questions") or []:
        qid = q.get("id")
        if not qid or qid in kit_comments:
            continue
        block = [f"  # {q.get('ask', '')}"]
        if q.get("proposal") and q["proposal"].get("value"):
            block.append(f"  #   по коду предполагаю: {q['proposal']['value']} — подтвердите или "
                         f"замените")
        kit_comments[qid] = block
        kit_lines.update(block)

    own = {"header": [], "before": {}, "inline": {}, "tail": []}
    if p.is_file():
        try:
            own = _owner_comments(p.read_text(encoding="utf-8"), kit_lines)
        except OSError:
            pass                               # прочитать не смогли — комментариев не будет, но
            # ответы уже прочитаны выше через `read_answers`, и потерять их этот путь не может

    def _value_line(qid, val):
        """Строка значения вместе с хвостовым комментарием владельца, если он был."""
        dumped = json.dumps(val, ensure_ascii=False) if val is not None else '""'
        c = own["inline"].get(qid)
        return f"  {qid}: {dumped}" + (f"  {c}" if c else "")

    lines = header + own["header"] + ["answers:"]
    seen = set()
    for q in ask.get("questions") or []:
        qid = q.get("id")
        if not qid or qid in seen:
            continue
        seen.add(qid)
        lines.extend(kit_comments[qid])
        lines.extend(f"  {c}" for c in own["before"].get(qid, []))
        val = existing.get(qid)
        # ЗНАЧЕНИЕ ПИШЕТСЯ ОДНОЙ СТРОКОЙ, БЕЗ МАРКЕРОВ ДОКУМЕНТА (F-023, живой прогон niti).
        #
        # Прежде стоял `yaml.safe_dump(val, default_flow_style=True).strip()`, и он ломал файл
        # ДВАЖДЫ. Первое: для голого скаляра PyYAML дописывает маркер конца документа —
        # `safe_dump("текст")` даёт `'текст\n...\n'`, а `.strip()` убирает только пробелы, поэтому
        # в файл попадала строка `...`, начинавшая НОВЫЙ YAML-документ. Второе: дефолт `width=80`
        # переносил длинное значение, а префикс `  {qid}: ` добавлялся лишь к первой строке —
        # продолжение оказывалось на отступе соседних ключей.
        #
        # Итог был потерей данных: кит записывал файл, который сам не мог прочитать, `read_answers`
        # отдавал «ответов нет», кит переспрашивал всё заново и перезаписывал файл пустым. Ломалось
        # ровно на содержательных ответах — короткие выживали.
        #
        # `json.dumps` даёт валидный YAML (YAML — надмножество JSON): двойные кавычки, одна строка,
        # никаких маркеров документа.
        lines.append(_value_line(qid, val))
        lines.append("")
    # Ответы на вопросы, которых больше не задают, сохраняем: человек их дал, они факт.
    for qid, val in existing.items():
        if qid not in seen:
            # ТОТ ЖЕ json.dumps, ЧТО ВЫШЕ (F-021, вторая половина). Первая правка закрыла только
            # ветку «вопрос ещё задаётся», а ОТВЕЧЕННЫЕ идут ИМЕННО СЮДА — их уже не спрашивают.
            # То есть дефект остался ровно там, где живут данные: на niti ответы гибли и после
            # «исправления», пока это место не поправили. Поймано повторным прогоном на живом
            # продукте, а не чтением кода.
            lines.extend(f"  {c}" for c in own["before"].get(qid, []))
            lines.append(_value_line(qid, val))
    lines.extend(own["tail"])
    body = "\n".join(lines).rstrip() + "\n"
    if p.is_file():
        try:
            if p.read_text(encoding="utf-8") == body:
                return p                       # тот же текст — писать нечего
        except OSError:
            pass                               # прочитать не смогли — перезапишем, это не потеря
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def owner_confirmed(child_root) -> dict:
    """Факты, ПОДТВЕРЖДЁННЫЕ владельцем: `.ai-ops.yaml -> product_operating_model.confirmed`.

    Это единственный производитель состояния `user_confirmed`, и он обязан существовать: состояние
    объявлено в модели с `trust: high` и названо единственным способом повысить `inferred`. Слово в
    реестре, которого не вычисляет никто, — ровно та «capability без реализации», которую
    инварианты кита запрещают (то же правило уже применено к `stale`).
    """
    # Файл ответов читается НЕЗАВИСИМО от наличия `.ai-ops.yaml`: человек отвечает на вопросы
    # онбординга ДО того, как у репозитория появится настроенная конфигурация, и ранний выход по
    # отсутствию конфига обнулял его ответы молча.
    data = {}
    cfg = Path(child_root) / ".ai-ops.yaml"
    if cfg.is_file():
        try:
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            data = {}                                  # битый конфиг — забота doctor'а
    if not isinstance(data, dict):
        data = {}
    conf = dict((data.get("product_operating_model") or {}).get("confirmed") or {})
    # Файл ответов онбординга — ВТОРОЙ законный источник подтверждений и основной по факту: человек
    # отвечает там, а не правит конфигурацию руками. Ответ из файла имеет тот же вес.
    conf.update(read_answers(child_root))
    return {k: v for k, v in conf.items() if v not in (None, "")}


def reconstruct(child_root, evidence: dict, model: dict | None = None) -> dict:
    """RECONSTRUCT — что кит утверждает о репозитории и НА КАКОМ ОСНОВАНИИ.

    -> {ключ: {"value": …, "status": verified|inferred|unknown, "evidence": [...]}}

    `verified` — только факт репозитория (файл, манифест, миграция, CI). Всё, что получено
    рассуждением о структуре, — `inferred`, и повышению не подлежит: подтвердить может человек
    (`user_confirmed`), а не убедительность вывода.
    """
    model = model or _contours.load_model()
    root = Path(child_root)
    out = {}

    prof = evidence.get("profile") or {}
    langs = [s.get("language") for s in (prof.get("stacks") or []) if s.get("language")]
    if langs:
        out["languages"] = {"value": langs, "status": VERIFIED,
                            "evidence": sorted({e for s in prof.get("stacks") or []
                                                for e in (s.get("evidence_source") or [])})[:6]}
    else:
        out["languages"] = {"value": None, "status": UNKNOWN,
                            "evidence": [], "note": "манифесты зависимостей не распознаны"}

    fw = sorted({f for s in (prof.get("stacks") or []) for f in (s.get("frameworks") or [])})
    if fw:
        out["frameworks"] = {"value": fw, "status": VERIFIED, "evidence": ["манифесты зависимостей"]}

    if evidence.get("migrations"):
        out["persistence"] = {"value": "реляционная СУБД с миграциями", "status": VERIFIED,
                              "evidence": list(evidence["migrations"])}
    elif evidence.get("containers"):
        out["persistence"] = {"value": None, "status": UNKNOWN,
                              "evidence": list(evidence["containers"]),
                              "note": "хранилище может быть объявлено в compose — не разобрано"}

    if evidence.get("api_schemas"):
        out["api_contracts"] = {"value": "контракты API объявлены файлами", "status": VERIFIED,
                                "evidence": list(evidence["api_schemas"])}

    if evidence.get("ci"):
        cmds = {}
        for s in prof.get("stacks") or []:
            for k, v in (s.get("commands") or {}).items():
                if v:
                    cmds[k] = v
        # ССЫЛКА СОВПАДАЕТ С ИСТОЧНИКОМ (F-019, часть 2). Значения команд читаются из МАНИФЕСТОВ
        # стека (`package.json` -> scripts и т.п.), а не из шагов workflow — прежде evidence
        # указывал только на CI, то есть «подтверждено» ссылалось не туда, где взяты данные.
        _cmd_ev = list(evidence.get("dependency_manifests") or [])
        out["ci_pipeline"] = {"value": sorted(cmds) or "CI объявлен", "status": VERIFIED,
                              "evidence": list(evidence["ci"]) + _cmd_ev,
                              "note": "CI продукта обнаружен; перечисленные команды взяты из "
                                      "манифестов стека, шаги workflow не разбирались"}

    if evidence.get("containers"):
        out["deployment"] = {"value": "контейнерная поставка", "status": INFERRED,
                             "evidence": list(evidence["containers"]),
                             "note": "какое окружение считается production — решение владельца"}

    src, tests = evidence.get("source_files") or 0, evidence.get("test_files") or 0
    if src:
        out["test_coverage_presence"] = {
            "value": f"{tests} тестовых файлов на {src} файлов кода",
            "status": VERIFIED if tests else MISSING, "evidence": ["обход дерева"]}

    # Архитектурный стиль — самый соблазнительный вывод и самый опасный: имена папок не являются
    # архитектурой. Поэтому статус всегда inferred, и рядом стоит основание.
    style, why = None, []
    if (root / "src" / "modules").is_dir() or (root / "modules").is_dir():
        style, why = "modular_monolith", ["src/modules/*"]
    elif any((root / d).is_dir() for d in ("services", "apps", "packages")):
        style, why = "multi-service / monorepo", [d for d in ("services", "apps", "packages")
                                                  if (root / d).is_dir()]
    elif src:
        style, why = "single application", ["структура каталогов"]
    if style:
        out["architecture_style"] = {"value": style, "status": INFERRED, "evidence": why,
                                     "note": "вывод из структуры; подтверждение владельца желательно"}

    # То, что из кода НЕ выводится. Молчать об этом нельзя: молчание читается как «нечего сказать».
    for key, note in (("primary_user", "пользователи продукта из репозитория не выводятся"),
                      # Ключ назван так же, как id ВОПРОСА в модели (`main_goal_now`): иначе ответ
                      # человека никогда не встретится со своим вопросом — ровно тот дефект, что был
                      # у `production_env` (ключ реконструкции не совпадал с id вопроса).
                      ("main_goal_now", "цель продукта из репозитория не выводится"),
                      ("sensitive_data", "чувствительность данных определяет владелец")):
        # `asks_human: True` — ключ, который кит НЕ ВЫВОДИТ ПО ОПРЕДЕЛЕНИЮ, а не «пока не нашёл».
        # Разница машиночитаемая, потому что от неё зависит, задавать ли вопрос: `languages`
        # неизвестны на пустом репозитории, но выводятся из манифестов — спрашивать их у человека
        # неуважительно. И у каждого такого ключа обязан быть ПАРНЫЙ вопрос в модели, иначе ответ
        # никогда не встретится со своим вопросом (так уже случилось дважды).
        out[key] = {"value": None, "status": UNKNOWN, "evidence": [], "note": note,
                    "asks_human": True}

    # Ключ реконструкции обязан совпадать с id ВОПРОСА (`production_env` в модели), иначе
    # предложение «по коду предполагаю X — подтвердить?» не доходит до вопроса никогда — именно так
    # обещание и не исполнялось. Основание для догадки есть там, где есть контейнер или CI-деплой;
    # где его нет, состояние остаётся `unknown`, а не выдумывается.
    if evidence.get("containers") or evidence.get("ci"):
        out["production_env"] = {
            "value": "окружение, куда деплоит существующий конвейер", "status": INFERRED,
            "evidence": list(evidence.get("containers") or evidence.get("ci") or []),
            "note": "какое окружение считается production — решение владельца",
            "asks_human": True}
    else:
        out["production_env"] = {"value": None, "status": UNKNOWN, "evidence": [],
                                 "note": "признаков деплоя не найдено", "asks_human": True}

    # ПОСЛЕДНИМ: подтверждение владельца перебивает и `inferred`, и `unknown`. Порядок не случаен —
    # человек сильнее любого вывода кита, и обратное затирание сделало бы подтверждение бесполезным.
    _answers = read_answers(child_root)
    for key, value in owner_confirmed(child_root).items():
        where = (ANSWERS_REL if key in _answers else ".ai-ops.yaml")
        out[key] = {"value": value, "status": USER_CONFIRMED,
                    "evidence": [f"подтверждено владельцем ({where})"],
                    "note": "подтверждение человека сильнее вывода кита",
                    "asks_human": (out.get(key) or {}).get("asks_human", False)}
    return out


def _reviewed_at(path: Path):
    """`reviewed_at` из frontmatter документа. -> date|None. None означает «дата не объявлена»,
    и это НЕ «свежий»: судить о свежести документа без даты нечем, поэтому решение остаётся за
    `validate_freshness`, владельцем этой области, а не за онбордингом."""
    import datetime as _dt
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:800]
    except OSError:
        return None
    if not head.startswith("---"):
        return None
    for line in head.split("---", 2)[1].splitlines():
        if line.strip().startswith("reviewed_at:"):
            raw = line.split(":", 1)[1].strip().strip('"\'')
            try:
                return _dt.date.fromisoformat(raw)
            except ValueError:
                return None
    return None


def _is_stale(root: Path, rels, today=None) -> bool:
    """Устарел ли хоть один источник истины контура. Дата не объявлена -> не устарел (см. выше)."""
    import datetime as _dt
    today = today or _dt.date.today()
    for rel in rels:
        for pre in ("", ".ai/project/", ".ai/custom/"):
            p = root / (pre + rel)
            if p.is_file():
                d = _reviewed_at(p)
                if d and (today - d).days > STALE_AFTER_DAYS:
                    return True
                break
    return False


def _contour_state(child_root, c: dict, evidence: dict, model: dict) -> dict:
    """Состояние контура в терминах семи статусов + что с ним делать.

    Логика намеренно скучная: есть обязательные источники истины -> `verified`; часть есть ->
    `partial`; ничего нет, но контур восстановим -> `inferred` возможен, фактическое состояние
    `missing`; ничего нет и восстановить нечем -> `missing`. `unknown` остаётся за случаем, когда
    дерево не читается: тогда неизвестно даже отсутствие.
    """
    root = Path(child_root)
    # `sot_for`, а не поле контура. Прежде онбординг читал ТОЛЬКО дефолт кита, поэтому `ai-ops model`
    # — единственное, что видит человек — давал ответ, ПРОТИВОПОЛОЖНЫЙ `contours.sot_state`:
    # владелец объявил, где лежит его правда, а кит продолжал требовать свой путь и переспрашивать
    # вечно. Правило «объявление владельца сильнее догадки кита» до этого модуля не доехало.
    _sot = _contours.sot_for(model, c["id"], root)
    req = [s for s in _sot if s.get("required")]
    opt = [s for s in _sot if not s.get("required")]

    def _has(rel):
        for pre in ("", ".ai/project/", ".ai/custom/"):
            if (root / (pre + rel)).exists():
                return True
        return False

    if not evidence.get("tree_readable"):
        state = UNKNOWN
    else:
        have_req = [s["path"] for s in req if _has(s["path"])]
        have_opt = [s["path"] for s in opt if _has(s["path"])]
        if req and len(have_req) == len(req):
            state = STALE if _is_stale(root, have_req) else VERIFIED
        elif have_req or have_opt:
            state = PARTIAL
        else:
            state = MISSING

    # Заготовка плана НЕ закрывает контур планирования: файл есть, а плана нет.
    if state == VERIFIED and c.get("id") == "planning_execution":
        try:
            from ai_ops_kit.planning import delivery_plan as _dp
            if _dp.is_template(_dp.load(root)):
                state = PARTIAL
        # «НЕ СМОГ ПРОВЕРИТЬ» НЕ РАВНО «ПРОВЕРЕНО» (срез ратчета, 2026-08-12). Прежний `pass`
        # оставлял состояние VERIFIED: при БИТОМ `planning/plan.yaml` контур объявлялся
        # подтверждённым, хотя проверка не состоялась. И это не гипотеза — `delivery_plan.load()`
        # по контракту БРОСАЕТ `PlanCorrupt` на неразобранном файле («„работы нет“ и „файл не
        # заполнен“ это разные ответы»), то есть путь достижим ровно там, где ошибка дороже всего.
        # Тот же класс, что F-018: существование файла принималось за заполненность.
        except Exception:  # noqa: BLE001 — любой отказ проверки -> UNKNOWN, а не VERIFIED
            state = UNKNOWN

    rec = c.get("reconstruction") or {}
    qs = c.get("questions") or []
    # ПРОБЕЛ ЗАКРЫТ — ВОПРОСОВ НЕТ. Контур с полным источником истины отвечает на свои вопросы сам;
    # спрашивать «кто основной пользователь» у репозитория с заполненным ProductOverview — то же
    # самое, что спрашивать про PostgreSQL при наличии миграций. Устаревший источник (`stale`)
    # вопросы возвращает: он отвечает, но неизвестно, на какой год.
    closed = state == VERIFIED
    return {"contour": c["id"], "title": c.get("title"), "question": c.get("question"),
            "state": state, "owner_role": c.get("owner_role"),
            "present": [s["path"] for s in (c.get("source_of_truth") or []) if _has(s["path"])],
            "missing_required": [s["path"] for s in req if not _has(s["path"])],
            "ai_can_reconstruct": rec.get("ability", "none"),
            "reconstruct_from": rec.get("from") or [],
            "needs_human": (not closed) and (bool(qs) or rec.get("ability") == "none"),
            "gap_tier": c.get("gap_tier", "opportunistic"),
            "questions": [] if closed else [dict(q) for q in qs]}


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
    return {"contours": rows, "by_state": by_state,
            "ready": [r["contour"] for r in rows if r["state"] == VERIFIED],
            "ai_can_build": ai_only, "needs_human": human,
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
    return {"schema_version": 1, "kind": "repository-understanding",
            "classification": cls, "evidence": ev, "reconstructed": rec, "audit": aud,
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

    L.append("\nКОНТУРЫ МОДЕЛИ")
    for r in rep["audit"]["contours"]:
        who = ("пробел закрыт" if r["state"] == VERIFIED
               else "нужен человек" if r["needs_human"]
               else "кит восстановит сам" if r["ai_can_reconstruct"] == "full"
               else "кит восстановит частично")
        L.append(f"  {r['state']:9} {r['title']} · {who} · срочность {r['gap_tier']}")
        if r["missing_required"]:
            L.append(f"            нет: {', '.join(r['missing_required'])}")

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


if __name__ == "__main__":
    sys.exit(main())
