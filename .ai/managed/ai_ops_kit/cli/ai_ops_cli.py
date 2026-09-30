#!/usr/bin/env python3
"""Intent-based UX поверх движка (v2.102, эпик Context Engineering, этап 6).

Снаружи AI Ops должен быть проще внутренней архитектуры. Обычный сценарий управляется намерениями,
а не флагами: пользователю не нужно помнить --engine pipeline / --author / --review / --baseline-diff
/ --sandbox — система сама подбирает workflow, стадии и нужные флаги (presets) и ПОКАЗЫВАЕТ
execution preview до запуска. Низкоуровневые флаги остаются доступны, но не обязательны.

Команды намерений:
  new · onboard · discuss · specify · plan · run · resume · review · status · health

Использование:
  ai_ops_cli.py <intent> [задача] <child_root> [--signals '{...}'] [--feature name] [--json] [--execute]
  ai_ops_cli.py preview <intent> [задача] <child_root> ...
  ai_ops_cli.py --selftest
"""
from __future__ import annotations

# v4: самодостаточный вход — файл можно запустить напрямую (без PYTHONPATH). Кладём корень пакета
# (маркер VERSION) в sys.path ДО пакетных импортов — раньше это делал плоский shim tools/ через
# _bootstrap; теперь точка входа сама себя обслуживает.
import sys as _sys
from pathlib import Path as _P_bootstrap
_root = next((_p for _p in _P_bootstrap(__file__).resolve().parents if (_p / "VERSION").is_file()), None)
if _root is not None and str(_root) not in _sys.path:
    _sys.path.insert(0, str(_root))

import argparse
import json
import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402
from ai_ops_kit.cli import human_help  # noqa: E402 — человеческая «дверь» (#675 Human API)
# intent -> (описание, какое действие, нужен ли текст задачи)
INTENTS = {
    # Третье поле — нужен ли текст задачи. `new` и `resume` его ИСПОЛЬЗУЮТ (заголовок работы,
    # выбор фичи), поэтому объявлены честно: расхождение объявления с использованием и было тем,
    # из-за чего `new` принимал текст задачи за каталог репозитория.
    "new":     ("создать новую фичу/каркас", "scaffold", True),
    "onboard": ("определить стек и команды репозитория", "onboard", False),
    "discuss": ("обсудить идею до спецификации (discovery)", "discuss", True),
    "specify": ("построить спецификацию нужной глубины", "specify", True),
    "plan":    ("построить RunPlan + контекст + оценку пакета (без правок)", "plan", True),
    "run":     ("выполнить задачу движком (авто-подбор стадий)", "run", True),
    "do":      ("автономный прогон: run --execute + авторазрешение блокировщиков", "do", True),
    "advise":  ("инженерный совет: окружения, delivery plan, альтернативы (без исполнения)", "advise", True),
    "resume":  ("продолжить прерванную работу по фиче", "resume", True),
    "review":  ("независимый ревью произведённого", "review", True),
    "status":  ("статус активной работы", "status", False),
    "health":  ("здоровье продукта", "health", False),
    # v3.36.13 (session-command-reaches-the-child): команда session перенесена из установщика в CLI,
    # чтобы работала из установленной дочки. Показывает снимок телеметрии сессии и рекомендацию.
    "session": ("снимок телеметрии сессии + рекомендация (continue/compact/clear/new_session)",
                "session", False),
    # 19.08.2026 (аудит): диагностика установки СИЛАМИ ДОЧКИ. Полный `doctor` живёт в установщике,
    # а он в поставку не едет — значит в дочке без клона кита команда отвечала «исходник рядом не
    # найден». Этот интент покрывает то, что видно изнутри репозитория, и НАЗЫВАЕТ, чего не видно.
    "doctor":  ("проверить установку изнутри репозитория (полная проверка — у кита)", "doctor", False),
    # v3.35 Product Operating Model: план продукта и его связность.
    "next":    ("что взять следующим: где мы, что идёт, что блокирует, что можно параллельно", "next", False),
    # #539: владельческая карточка одной задачи — что с ней прямо сейчас: стадия, что готово, что
    # мешает и ПОЧЕМУ (последствием, простыми словами), следующий шаг и оценка стоимости. Read-only.
    "explain": ("что с моей задачей прямо сейчас: стадия, что готово, что мешает и почему, "
                "следующий шаг, оценка стоимости", "explain", False),
    # #540: единая владельческая очередь — ОДИН список всего, что ждёт решения человека: решения
    # (карточка что/варианты/рекомендация), остановленные работы, ждущие подтверждения, свежий ночной
    # обзор, предупреждения о выпуске. Пусто -> честное «ничего не ждёт». Только чтение.
    "inbox":   ("что ждёт твоего решения: решения, остановленные работы, подтверждения, обзор, "
                "предупреждения о выпуске — одной очередью", "inbox", False),
    # #549: единая машинная ПРОЕКЦИЯ «работы» по id. Подкоманда первым словом (как backlog):
    # `work show <id>` сводит четыре источника (заявка/реестр идущих работ/граф пакетов/план) в одну
    # read-only карточку. Ничего не пишет, нового рантайма не заводит — агрегатор поверх существующего.
    "work":    ("проекция одной работы по id: work show <id> — стадия, кто ведёт, области, "
                "зависимости, артефакты, решения из четырёх источников", "work", True),
    "model":   ("модель продуктового репозитория: классификация, контуры, пробелы, вопросы", "model", False),
    # Product Contract (единый объект продукта): агрегирует идентичность, стандарт, артефакты слоя,
    # источники истины контуров и здоровье в ОДИН объект с одним вердиктом. Ничего не пишет.
    "contract": ("единый контракт продукта: идентичность/стандарт/артефакты/контуры/здоровье + вердикт",
                 "contract", False),
    # Что продукту нужно дальше: кит ВЕДЁТ с продуктового совета (product_advice) человеческим языком,
    # а настройку самого кита после `update` (что изменилось, можно ли двигаться, Storybook) подаёт
    # отдельно и в технические детали по запросу. Оркестрация готовых кирпичей через presenter. Read-only.
    "propose": ("что продукту нужно дальше: рекомендации человеческим языком (что важно, где ценность "
                "и риск) + можно ли двигаться; настройка кита и детали — по запросу", "propose", False),
    # Product Registry (флот): сводный вердикт по ВСЕМ продуктам из реестра флота (products.yaml /
    # $AI_OPS_PRODUCTS). «Увидеть состояние всех продуктов разом». Только чтение.
    "products": ("флот продуктов: (без арг.) сводный вердикт по всем | register — добавить текущий репозиторий",
                 "products", False),
    # Подробная карточка ОДНОГО продукта флота по id (контракт+вердикт+здоровье+риски) — без cd в его
    # репозиторий. id — единственный аргумент. Только чтение.
    "inspect": ("карточка одного продукта флота по id: контракт, вердикт, здоровье, риски", "inspect", True),
    # Team status (Фаза 4): здоровье×3 + топ-риски + блокеры + следующие задачи + milestone одним
    # снимком. Только чтение.
    "team":    ("статус команды: здоровье, риски, блокеры, следующие задачи, milestone", "team", False),
    # Governance (Фаза 4): активная политика автономии + журнал решений AI + человеческие
    # переопределения. Только чтение (enforcement не трогаем — это отдельное решение).
    "governance": ("governance продукта: политика автономии, журнал решений AI, переопределения человека",
                   "governance", False),
    # Фаза 3 (лента 4): roadmap Now/Next/Later и delivery-план из backlog.
    "roadmap": ("roadmap Now/Next/Later из плана + отклонение от авторского ROADMAP.md", "roadmap", False),
    "delivery": ("delivery-план из backlog под milestone: порядок, прогноз-оценка, риски, блокеры; "
                 "record <feature-id> --pr <n> — подтверждённая расписка для вручную влитого PR",
                 "delivery", False),
    # v3.35.2 (тир 4): BOOTSTRAP существовал СТРОКОЙ в реестре — кит не создавал ни направления, ни
    # плана, и владелец после онбординга оставался с пониманием и без работы. Сухой прогон по
    # умолчанию: запись в чужой репозиторий он обязан увидеть до того, как она произошла.
    "bootstrap": ("создать первое направление и план из фактов репозитория (--apply — записать)",
                  "bootstrap", False),
    # 2026-08-17: наблюдения о САМОМ КИТЕ из продуктового репозитория. Раньше они доезжали только
    # пересказом человека — три работы плана кита ссылаются на «сообщение параллельной сессии».
    # Без текста — показать судьбу уже записанных (канал обязан быть двусторонним).
    "feedback": ("рассказать киту, что он сделал не так (без текста — судьба уже сказанного)",
                 "feedback", True),
    # voluntary-child-registration: ДОБРОВОЛЬНЫЙ охват без телеметрии. Подкоманда первым словом:
    # register|decline|forget|status|summary (в дочке) · coverage|collect (в ките). Всё opt-in,
    # рукой владельца, без сети. Без подкоманды — показать состояние отметки.
    "reach":   ("добровольная отметка о подключении и охват (без телеметрии): "
                "register|decline|forget|status|summary · coverage|collect", "reach", True),
    # Backlog Intelligence (Фаза 2): GitHub Issues как операционная единица. Подкоманда — первым
    # словом: classify | dedup | prioritize | graph. Без доступа к GitHub отвечает «не проверено»
    # с причиной, а не пустотой. Форма ещё меняется — интент experimental.
    "backlog": ("backlog из GitHub Issues: classify | dedup | prioritize | graph", "backlog", True),
    # Knowledge Graph как ЗАПРАШИВАЕМАЯ технология (тонкий слой поверх registry/entities.yaml +
    # validate_knowledge_graph). Подкоманда первым словом (как backlog): собирает один граф из
    # plan.yaml + FL-*.yaml + feature blueprints и отвечает на вопрос, который иначе требует ручного
    # чтения трёх файлов. `build` — собрать/показать (--apply — записать knowledge/graph.yaml);
    # `trace <feature>` — цепочка goal→…→feature→outcome + вердикт + пробелы; `gaps` — что не
    # покрыто измеримым результатом. Только чтение (кроме `build --apply`).
    "graph":   ("knowledge graph: build | trace <feature> | gaps — зачем функция существует и "
                "измерен ли её исход, из плана+обучения+blueprint одним графом", "graph", True),
    # Autonomous Replanning (Фаза 5, капстоун): цикл сам сводит приоритеты плана к реальности. Без
    # флага — отчёт (read-only превью: что переупорядочено и почему + структурные ПРЕДЛОЖЕНИЯ).
    # С `--apply` — записывает переприоритизацию (класс A, обратимо, состав работ не меняет) в
    # машинный артефакт дочки; авторский plan.yaml и main не трогает. Структурные правки — только
    # предложением, автоматически не применяются.
    "replan":  ("перепланирование: сам переприоритизирует план под реальность (--apply — записать), "
                "структурные изменения — предложением", "replan", False),
    # #545 outcome-loop: ЕДИНЫЙ пост-релизный путь одним вызовом — PRR -> проверка прихода событий в
    # аналитику -> проекция исхода -> один вердикт. Только чтение. Честный дефолт при отсутствии
    # выгрузки аналитики -> «выпуск рекомендовать не могу», а не «healthy». Реальный флип исхода цели
    # ждёт реального выпуска дочки.
    "readout": ("пост-релизная петля: доставили -> дошли ли события в аналитику -> исход -> один "
                "вердикт (без выгрузки аналитики — честно «ещё нечем проверить»)", "readout", False),
    # kit-product-scorecard: ЕДИНАЯ карта продукта кита из 5 метрик — кит мерит СЕБЯ как продукт, а
    # не числом возможностей. Метрики 1 (доля фич идея->релиз) и 3 (доля фич с полной историей жизни)
    # считаются из графа знаний; метрика 2 (доля утверждений с evidence) — доля поверх реестра
    # честности; метрики 4 (результат после релиза) и 5 (уроки->решения) стоят честным каркасом «не
    # измерено» (у самого кита живой аналитики нет). Только чтение. Форма карты ещё устаканивается.
    "scorecard": ("карта продукта кита: 5 метрик, которыми кит мерит себя как продукт (а не числом "
                  "возможностей); нет данных — честно «не измерено»", "scorecard", True),
    # auto-slice-candidates: кит НАРЕЗАЕТ задачи-кандидаты из двух источников (непокрытые направления
    # роадмапа + наблюдения дочек), владелец ПРИНИМАЕТ пачкой одной командой. Предохранитель: кит сам
    # не дописывает активную работу — без глагола `accept` план не меняется. `candidates`/`list` —
    # только чтение; `accept <id..>`/`accept --all` — пишет plan.yaml (явное действие владельца).
    "candidates": ("задачи-кандидаты (непокрытые направления роадмапа + наблюдения дочек): без "
                   "аргумента — список; accept <id..>|--all — принять пачкой в план", "candidates", True),
    # portfolio-intelligence (T5): ОБЕЗЛИЧЕННЫЙ портфельный вид отказов дочек. Класс отказа + числа +
    # обезличенный ключ дочки (`proj-…`), и НИЧЕГО идентифицирующего — сторож границы краснеет на
    # утечке пути/коммита/имени/вывода команд. Только чтение; честные разные пустые состояния.
    "portfolio": ("портфель отказов дочек обезличенно: какие классы повторяются по всем дочкам "
                  "(только паттерны и числа, без кода и путей)", "portfolio", False),
    # #1183: стиль кода держит машина — профиль линтеров поверх конфига дочки + заморозка существующего.
    "lint-profile": ("единый стиль кода: имена латиницей и границы слоёв поверх ваших линтеров; "
                     "--apply — включить, check — новое расхождение краснеет", "lint-profile", False),
}


# Интенты, которые ИСПОЛНЯЮТСЯ, а не показывают превью. Список обязан совпадать с тем, что умеет
# `_run_intent`: расхождение означает «обработчик есть, до него не доходит» — молчаливый no-op с
# кодом 0, самый дорогой вид отказа, потому что выглядит успехом. Сверяется тестом.
DIRECT_INTENTS = ("onboard", "status", "health", "plan", "new", "discuss", "review", "advise",
                  "next", "explain", "model", "bootstrap", "feedback", "session", "doctor",
                  "roadmap", "delivery", "backlog", "contract", "propose", "products", "team",
                  "governance", "inspect", "replan", "inbox", "work", "readout", "graph", "reach",
                  "scorecard", "candidates", "portfolio", "lint-profile")


# ── Фасад владельца (P1 №8/№9 ревью): 7 действий человеческим языком поверх 34 intents ──────────────
# Символы фасада (HUMAN_ACTIONS, facade_plan, диспетч, помощники) вынесены в спутник `human_facade`
# (ратчет module-size: фасад перевёл этот файл за порог монолита). Ре-экспорт именами этого модуля
# держит резолвинг у вызывающих/тестов (`ai_ops_cli.HUMAN_ACTIONS`, `ai_ops_cli.facade_plan`, …) и
# у main(). Ребро импорта одностороннее: `human_facade` НЕ импортирует ai_ops_cli на верхнем уровне —
# обратно в main() он уходит колбэком `run_main`, поэтому цикла нет.
from ai_ops_kit.cli.human_facade import (  # noqa: E402,F401 — ре-экспорт фасада для вызывающих/тестов
    HUMAN_ACTIONS,
    facade_plan,
    _dispatch_human_action,
    _review_all,
    _inject_task_type,
    _first_nondir,
    _child_root_from,
    _is_dir_safe,
)


def resolve_flags(signals):
    """Авто-подбор внутренних флагов по классу задачи (preset). Пользователь их не задаёт вручную."""
    from ai_ops_kit.gates import spec_levels
    tt = (signals.get("task_type") or "QUICK").upper()
    flags = {"engine": "pipeline", "sandbox": True, "baseline_diff": True,
             "review": False, "author": False}
    if tt in ("ENGINEERING", "PRODUCT", "CRITICAL", "AI_FEATURE", "RESEARCH"):
        flags["review"] = True
        flags["author"] = True
    # Быстрый путь (заявка владельца): МЕЛКАЯ и НИЗКО-рисковая работа не гоняется через план и
    # приёмку — остаётся обычный PR, то есть человеческое ревью и CI. Опасное сюда не попадает по
    # построению: классификатор эскалирует необратимое/деструктивное/секретное/высокий риск к L3, а
    # быстрый путь открыт только ниже L2 (L0/L1). Поэтому пометить рискованное как small не поможет:
    # уровень считает не size, а сигналы риска. PRODUCT/CRITICAL полный путь сохраняют.
    if (signals.get("size") or "").lower() == "small" and (signals.get("risk") or "").lower() == "low":
        if spec_levels.classify(signals).get("level", 0) < 2:
            flags["review"] = False
            flags["author"] = False
            flags["fast_path"] = True
    if signals.get("fix") or tt == "QUICK" and signals.get("require_fix"):
        flags["require_fix"] = True
    return flags


# Шаги, которые ПОДХВАТЫВАЮТ сохранённые на specify сигналы (полевой замер cockpit, 06.09.2026):
# `specify` их пишет, эти шаги их читают. Перенос идёт только сюда, чтобы не менять поведение
# команд, которые к сигналам уровня отношения не имеют (status/next/health/…).
_SIGNAL_CARRY_INTENTS = ("specify", "plan", "run", "do")


def _carry_stored_signals(task, child_root, signals, feature):
    """Долить сигналы, сохранённые прошлым `specify` в features/<wid>/spec.yaml.

    Правило слияния: сохранённое — БАЗА, переданное на этом вызове (--signals) — СВЕРХУ. Значит
    явный --signals по-прежнему переопределяет сохранённое, а его отсутствие больше не откатывает
    уровень: ENGINEERING-задача не едет молча как QUICK. Нет спеки / нет блока signals -> вернём
    вход без изменений (обратная совместимость)."""
    from ai_ops_kit.gates import spec_levels
    try:
        wid = _wid_for(task, signals, feature)
        stored = spec_levels.carried_signals(Path(child_root), wid)
    except Exception:  # noqa: BLE001 — перенос сигналов не должен ронять команду
        return signals
    if not stored:
        return signals
    return {**stored, **signals}


def _parse_signals_arg(raw):
    """`--signals` понимает JSON (рабочий, внутренний формат) И обычные слова о размере/риске
    задачи (#864: «небольшая, неопасная» вместо `'{"size":"small","risk":"low"}'`).

    JSON пробуем ПЕРВЫМ — он остаётся основным путём, ничьё поведение не меняется. Если строка не
    парсится как JSON, это не ошибка формата: пробуем прочитать её как обычную фразу
    (`ai_ops_kit.shared.signal_words`). Ни слова, ни JSON не нашли — тот же `json.JSONDecodeError`,
    что был бы раньше (fail-closed, никто ничего не выдумывает)."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        from ai_ops_kit.shared import signal_words
        parsed = signal_words.parse_plain_signals(raw)
        if not parsed:
            raise
        return parsed


def _build_signals(intent, task, child_root, a):
    """Собрать сигналы вызова: --signals + feature + перенос сохранённого со specify.

    Вынесено из `main` (ратчет func-size: main упиралась в потолок). Правило переноса — то же,
    что у plan/run: сохранённое на specify — база, переданное на этом вызове — сверху (см.
    `_carry_stored_signals`). Явный --signals по-прежнему побеждает; его отсутствие больше не
    роняет уровень (ENGINEERING-задача не едет молча как QUICK). Перенос — только для интентов
    `_SIGNAL_CARRY_INTENTS`, чтобы не менять поведение команд, к уровню отношения не имеющих.
    """
    signals = _parse_signals_arg(a.signals)
    if a.feature:
        signals["feature"] = a.feature
    if intent in _SIGNAL_CARRY_INTENTS:
        signals = _carry_stored_signals(task, child_root, signals, a.feature)
    # #543 shadow->live: owner-флаг risk_calibrated_enforcement из .ai-ops.yaml -> в сигналы (если не
    # задан явным --signals). По умолчанию ключа нет -> флаг OFF -> строгость гейтов не меняется.
    from ai_ops_kit.gates import gate_executor as _ge
    _rc = _ge.risk_calibrated_config(child_root)
    if _rc is not None and "risk_calibrated_enforcement" not in (signals.get("gates") or {}):
        signals.setdefault("gates", {})["risk_calibrated_enforcement"] = _rc
    return signals


# --- Реестр обработчиков команд (v3.38). Прежде диспетч был цепочкой `if intent == …` в
# `_run_intent` (файл ~1377 строк): добавить команду значило править монолит, и цена этой
# проводки — корень того, что модули продуктовых операций написаны, но не заведены. Теперь
# команда подключается регистрацией обработчика; список ключей реестра сверяется с DIRECT_INTENTS
# тестом (tests/contracts/test_direct_intents_match_handler.py).
_INTENT_HANDLERS = {}


def _intent(name):
    """Зарегистрировать обработчик команды. Повторное имя — ошибка, а не тихое затирание."""
    def _register(fn):
        if name in _INTENT_HANDLERS:
            raise ValueError(f"intent {name!r} зарегистрирован дважды")
        _INTENT_HANDLERS[name] = fn
        return fn
    return _register


# --- Перенесённые проб-свободные обработчики и хелперы (ai_ops_cli_intents). Ре-экспорт именами
# этого модуля держит резолвинг у вызывающих/тестов и диспетча; спутник НЕ импортирует ai_ops_cli на
# верхнем уровне (обратные обращения — ленивым импортом), поэтому цикла нет.
from ai_ops_kit.cli.ai_ops_cli_intents import (  # noqa: E402,F401 — ре-экспорт для вызывающих/тестов
    build_preview, _print_preview,
    _run_backlog, _BACKLOG_SUBS,
    _product_health_report, _product_risks,
    _intent_products, _intent_model, _intent_contract, _intent_propose,
    _intent_inspect, _intent_plan, _intent_session,
    _intent_roadmap, _intent_replan, _intent_new, _intent_governance,
    _intent_bootstrap, _intent_discuss, _intent_health, _intent_team,
    _intent_onboard, _intent_doctor, _copy_affects_from_plan,
    _intent_explain, _intent_inbox, _intent_work, _intent_readout, _intent_graph,
    _intent_reach, _intent_scorecard, _WORK_SUBS,
)

# --- Слой реализации команд (ai_ops_cli_commands): проб-несущие обработчики намерений и путь
# исполнения run/do с сессионными стражами. Общие хелперы вывода/идентификации переехали туда же и
# ре-экспортируются отсюда — вызывающие и тесты резолвят их прежними именами ai_ops_cli.<name>.
# Ребро импорта одностороннее: сосед НЕ импортирует ai_ops_cli, поэтому цикла нет.
from ai_ops_kit.cli.ai_ops_cli_commands import (  # noqa: E402,F401 — ре-экспорт + диспетч/регистрация
    _wid_for, _intake_command_carrying_task_type, _say, _audience,
    _intent_backlog, _intent_feedback, _intent_status, _enrich_running_with_work_view,
    _intent_next, _intent_review, _intent_advise,
    _session_identity, _announce_start, _session_guard_before_start,
    _process_gate, _main_run_execute,
)

# auto-slice-candidates: команда `candidates` живёт в собственном спутнике (ратчет module-size).
from ai_ops_kit.cli.candidates_cli import _intent_candidates  # noqa: E402,F401 — ре-экспорт/регистрация
# delivery-record: интент `delivery` вынесен спутником из product-хаба (ратчет размера модуля).
from ai_ops_kit.cli.ai_ops_cli_delivery import _intent_delivery  # noqa: E402,F401 — ре-экспорт/регистрация
# portfolio-intelligence (T5) и профиль стиля (#1183) — тоже отдельные спутники.
from ai_ops_kit.cli.portfolio_cli import _intent_portfolio  # noqa: E402,F401 — ре-экспорт/регистрация
from ai_ops_kit.cli.lint_profile_cli import _intent_lint_profile  # noqa: E402,F401 — регистрация

# Регистрация перенесённых обработчиков в общий реестр интентов (декоратор и реестр живут здесь).
for _name, _fn in (("products", _intent_products), ("delivery", _intent_delivery),
                   ("model", _intent_model), ("contract", _intent_contract),
                   ("propose", _intent_propose),
                   ("inspect", _intent_inspect), ("plan", _intent_plan),
                   ("session", _intent_session),
                   # вторая волна выноса (deepcut, глубже):
                   ("roadmap", _intent_roadmap), ("replan", _intent_replan),
                   ("new", _intent_new), ("governance", _intent_governance),
                   ("bootstrap", _intent_bootstrap), ("discuss", _intent_discuss),
                   ("health", _intent_health), ("team", _intent_team),
                   ("onboard", _intent_onboard), ("doctor", _intent_doctor),
                   ("explain", _intent_explain), ("inbox", _intent_inbox),
                   ("work", _intent_work), ("readout", _intent_readout),
                   ("graph", _intent_graph), ("reach", _intent_reach),
                   ("scorecard", _intent_scorecard),
                   # проб-несущие обработчики из ai_ops_cli_commands:
                   ("backlog", _intent_backlog), ("feedback", _intent_feedback),
                   ("status", _intent_status), ("next", _intent_next),
                   ("review", _intent_review), ("advise", _intent_advise),
                   ("candidates", _intent_candidates),
                   ("portfolio", _intent_portfolio), ("lint-profile", _intent_lint_profile)):
    _intent(_name)(_fn)
del _name, _fn


def _run_intent(intent, task, child_root, signals, a):
    """v2.112 Intent UX: РЕАЛЬНОЕ действие для намерения. -> код возврата или None (нет спец-действия)."""
    child_root = Path(child_root)
    handler = _INTENT_HANDLERS.get(intent)
    return handler(task, child_root, signals, a) if handler else None


def _build_cli_arg_parser():
    """Собрать argparse интент-CLI — тело main() без разбора аргументов и диспетчеризации."""
    ap = argparse.ArgumentParser(prog="ai_ops_cli.py")
    ap.add_argument("intent", choices=list(INTENTS) + ["preview"])
    ap.add_argument("rest", nargs="*")
    ap.add_argument("--signals", default="{}")
    ap.add_argument("--feature")
    # #612 (первый час): `model --answer <qid> "<value>"` записывает ответ онбординга без ручной
    # правки .ai/project/onboarding-answers.yaml. `--why` — основание (источник), станет комментарием.
    # One-screen: флаг ПОВТОРЯЕМ — `--answer a "x" --answer b "y"` записывает несколько ответов за
    # один вызов; с `--flow --apply` тем же вызовом применяет первый час на свежих фактах.
    ap.add_argument("--answer", nargs=2, action="append", metavar=("QID", "VALUE"), default=None,
                    help="model: записать ответ(ы) онбординга (без ручной правки YAML); повторяем")
    ap.add_argument("--why", action="append", default=None,
                    help="model --answer: основание ответа (источник) — ляжет комментарием; "
                         "повторяем, по одному на ответ в том же порядке (лишние ответы — без основания)")
    # #863 (живой zero-touch прогон): `specify --answers "зачем=...; как-поймём=..."` записывает
    # ответы обычными словами в features/<wid>/spec.yaml БЕЗ ручной правки YAML. Разбор — в
    # ai_ops_kit.shared.spec_answers (те же короткие слова, что называет сообщение presenter'а).
    ap.add_argument("--answers", default=None,
                    help='specify: ответить на вопросы описания задачи словами, не открывая файл '
                         '— например --answers "зачем=нужно клиентам X; как-поймём=тест зелёный"')
    # #647 (первый час): `model --flow` сшивает понимание → вопросы → (если ответы есть) направление
    # и план → следующую работу ОДНИМ нарративом. `--apply` записывает направление/план (иначе сухой
    # предпросмотр). Не новый интент — режим `model`, чтобы не плодить top-level команды (#632).
    ap.add_argument("--flow", action="store_true",
                    help="model: первый час одним нарративом (понял → знаю/не знаю → направление и "
                         "план → следующая работа); с --apply записывает направление и план")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="resume: продолжить даже при нужной ревалидации (осознанно)")
    ap.add_argument("--takeover", action="store_true",
                    help="run: перенять брошенную/устаревшую заявку на работу или ветку "
                         "(прежний держатель остаётся записан в taken_over_from — атрибуция цела)")
    ap.add_argument("--takeover-reason", default=None,
                    help="run --takeover: причина переятия (для атрибуции)")
    ap.add_argument("--base", default=None, help="resume/review: base-ветка (по умолчанию auto: upstream/remote-default/текущая)")
    # v3.28.x (P0-1): дефолта `mock` больше НЕТ. Для `run --execute`/`do` провайдера выбирает резолв
    # (.ai-ops.yaml + ключ в env -> claude в PATH -> mock с громким предупреждением); явный --provider
    # (в т.ч. mock) всегда побеждает. Для `review`/`resume` остаётся прежний офлайн-дефолт mock.
    ap.add_argument("--provider", default=None,
                    help="провайдер (mock|anthropic|openai|claude-cli|qwen|deepseek|kimi). "
                         "run --execute без флага — авторезолв (AI_OPS_PROVIDER_AUTORESOLVE=0 выключает); "
                         "review: провайдер ревьюера (не mock -> живой вердикт)")
    ap.add_argument("--model", help="review: модель ревьюера")
    ap.add_argument("--sequential", action="store_true",
                    help="run: неатомарную задачу исполнять по WorkPackages последовательно (v3.1)")
    ap.add_argument("--parallel", action="store_true",
                    help="run: ЯВНЫЙ opt-in настоящей конкурентности — мультипакетный план исполнить "
                         "по disposable-клону на пакет с governed fan-in (#542). По умолчанию ВЫКЛ; "
                         "атомарная задача откатывается в обычный прогон. Основной checkout не трогается")
    ap.add_argument("--open-pr", action="store_true",
                    help="run: открыть draft PR по результату (нужен GITHUB_TOKEN)")
    ap.add_argument("--max-steps", type=int, default=40, help="run: потолок шагов tool-loop — предохранитель от убежавшего писателя, не рабочая точка (типовой расход единицы-десятки шагов; обоснование у поля max_steps в run_context)")
    ap.add_argument("--resume-from", help="run --sequential: продолжить с конкретного WorkPackage (id); "
                                          "пакеты до него берутся из снимков прошлого прогона")
    ap.add_argument("--retry-package", help="run --sequential: ДОВЕРЕННЫЙ retry заблокированного пакета (id) "
                                            "— архивирует проваленную попытку, восстанавливает ветку на "
                                            "checkpoint предшественника и продолжает (без ручного git reset)")
    ap.add_argument("--replan", action="store_true",
                    help="resume: осознанно сменить классификацию/policy (replan c ревалидацией)")
    ap.add_argument("--deliver-only", action="store_true", dest="deliver_only",
                    help="resume: доставить УЖЕ готовый READY-коммит без перезапуска писателя "
                         "(#403: перепроверка существующего HEAD без нового evidence-коммита, затем "
                         "доставка) — при сбое доставки после READY не плодит новые коммиты")
    ap.add_argument("--reevaluate-only", action="store_true", dest="reevaluate_only",
                    help="run: ПЕРЕОЦЕНИТЬ гейты существующей фичи БЕЗ переавторинга и без вызова "
                         "модели (план/SHA стабильны) — например, оркестратор записал вердикт-ревью "
                         "или человек добавил ApprovalRecord: гейт закрывается по артефакту, работа "
                         "доходит до ready/доставки. Нужен --execute + --feature. engine=pipeline")
    ap.add_argument("--budget", type=int, default=None,
                    help="next: остаток бюджета в токенах (нет значения -> unknown, НЕ ноль)")
    ap.add_argument("--approved", default=None,
                    help="backlog merge: файл с одобренными парами дублей "
                         "({approved: [{duplicate, canonical}]}); слияние без него кит не делает")
    ap.add_argument("--backlog", default=None,
                    help="delivery: файл backlog {tasks, milestones, capacity, today} (лента 3); "
                         "по умолчанию .ai-ops/backlog.yaml")
    ap.add_argument("--milestone", default=None,
                    help="delivery: id milestone, под который строить delivery-план и прогноз")
    ap.add_argument("--pr", default=None,
                    help="delivery record: номер вручную влитого PR (или его URL) — по нему кит "
                         "сверяет слияние с GitHub и записывает подтверждённую расписку о поставке")
    # auto-slice-candidates: `candidates accept <id> --goal <goal-id>` — направление, к которому
    # отнести кандидата без своего source_goal (у находок его нет). В многоцелевом плане без него
    # такой кандидат не принимается (иначе work item без goal — ошибка валидатора).
    ap.add_argument("--goal", default=None,
                    help="candidates accept: id направления (goal) для кандидата без своего source_goal")
    # #545 readout (пост-релизная петля): PRR-файл и опциональные OutcomeContract/OutcomeReadout.
    ap.add_argument("--prr", default=None,
                    help="readout: путь к PRR-файлу (PostReleaseReadout); без него — поиск в дочке")
    ap.add_argument("--outcome-contract", default=None, dest="outcome_contract",
                    help="readout: путь к OutcomeContract для проекции исхода (опционально)")
    ap.add_argument("--outcome-readout", default=None, dest="outcome_readout",
                    help="readout: путь к OutcomeReadout (опционально; без него исход — pending)")
    ap.add_argument("--apply", action="store_true",
                    help="bootstrap: РЕАЛЬНО создать отсутствующие направление и план "
                         "(без флага — сухой прогон: показать, что будет создано)")
    # Решение владельца 2026-08-17: короткий путь для уже описанной работы + потолок траты на
    # описание. Оба флага — осознанный выход из автоматики, а не режим по умолчанию.
    ap.add_argument("--full-process", action="store_true",
                    help="discuss/specify/plan: пройти полный путь даже на уже описанной работе "
                         "(короткий путь не применять)")
    ap.add_argument("--spend-ok", action="store_true",
                    help="discuss/specify/plan: продолжить разбор, зная что потолок траты на "
                         "описание до первой правки кода уже пробит")
    # Улики для `feedback`: наблюдение класса «дефект» без улики не записывается — иначе канал
    # производил бы дефекты из впечатлений.
    ap.add_argument("--evidence-file", action="append", metavar="ПУТЬ=ЦИТАТА",
                    help="feedback: файл и строка из него как основание наблюдения")
    ap.add_argument("--evidence-command", action="append", metavar="КОМАНДА=ВЫВОД",
                    help="feedback: команда и её вывод как основание наблюдения")
    ap.add_argument("--evidence-note", action="append", metavar="ТЕКСТ",
                    help="feedback: пояснение к наблюдению (уликой не считается)")
    ap.add_argument("--severity", choices=["p0", "p1", "p2"],
                    help="feedback: насколько это мешает")
    ap.add_argument("--class", dest="observation_class",
                    choices=["defect", "friction", "question", "idea"],
                    help="feedback: дефект / трение / вопрос / идея (по умолчанию выводится из улик)")
    # voluntary-child-registration: `onboard . --reach register|decline` записывает РЕШЕНИЕ о
    # добровольной отметке прямо в онбординге (opt-in, без сети). По умолчанию (флага нет) онбординг
    # НИЧЕГО не создаёт — только предлагает; согласие остаётся явным действием владельца.
    ap.add_argument("--reach", choices=["register", "decline"], default=None,
                    help="onboard: записать решение о добровольной отметке (opt-in, без сети): "
                         "register — отметиться, decline — отказаться")
    ap.add_argument("--json", action="store_true")
    # #675: `help --all` — показать весь список команд, а не только человеческую дверь. Разбирается
    # в human_help.handle ДО argparse; объявлен здесь, чтобы флаг был настоящим (printed-commands-runnable).
    ap.add_argument("--all", action="store_true", help="help: показать все команды, не только дверь")
    return ap


def _parse_task_and_root(intent, rest):
    """Разобрать позиционные `[задача] child_root` -> (task, child_root). Вынесено из main (ратчет
    func-size). `rest` мутируется на месте (pop): остаток виден вызывающему.

    КАТАЛОГ РЕПОЗИТОРИЯ — ПОСЛЕДНИЙ АРГУМЕНТ (его подставляет `./ai-ops`), текст задачи — перед ним.
    Прежде разбор шёл слева и опирался на флаг `needs_task`: у `new` он стоял `False`, и
    `./ai-ops new "добавить экспорт в CSV"` объявлял каталогом репозитория… текст задачи. Каркас
    создавался в `./добавить экспорт в CSV/features/wi-unknown/`, код возврата 0 — кит молча работал
    не в том репозитории и сообщал об успехе. Нашлось тестом разводки команд.
    КАТАЛОГ РАСПОЗНАЁТСЯ И В НАЧАЛЕ, И В КОНЦЕ — иначе один из двух путей вызова ломается (F-032,
    третье подтверждение 17.08.2026, чистая установка). Обёртка `./ai-ops` подставляет путь СРАЗУ
    ПОСЛЕ интента (`<intent> <путь> "текст" --флаг`): хвостовой аргумент после флагов argparse в
    позиционную группу не берёт вовсе (B2-15, 14.08.2026), а разбор искал каталог ТОЛЬКО в хвосте —
    и `./ai-ops specify "текст" --feature X` терял текст задачи.

    Порядок правил важен: сначала абсолютный путь в НАЧАЛЕ (так делает только обёртка), потом хвост
    (так пишет человек), потом относительный путь в начале (`./ai-ops specify . "текст"`).
    Абсолютность в первом правиле отличает подстановку обёртки от текста задачи, случайно совпавшего
    с именем каталога.
    """
    needs_task = INTENTS.get(intent, ("", "", False))[2]
    task, child_root = None, "."
    if len(rest) >= 2 and _is_dir_safe(rest[0]) and Path(rest[0]).is_absolute():
        child_root = rest.pop(0)
    elif rest and _is_dir_safe(rest[-1]):
        child_root = rest.pop()
    elif len(rest) >= 2 and _is_dir_safe(rest[0]):
        child_root = rest.pop(0)
    if rest:
        task = rest.pop(0)
    elif needs_task:
        task = ""
    return task, child_root


def main(argv):
    # #675 Human API: пустой вызов и `help` показывают человеческую дверь (короткий набор команд
    # владельца), а не argparse-стену из 36 интентов и 30 флагов. `help --all` — весь список.
    _door = human_help.handle(argv, INTENTS, HUMAN_ACTIONS)
    if _door is not None:
        return _door
    # Фасад владельца (7 действий человеческим языком поверх 34 intents). ДО argparse: research/start/
    # check/release и `review all` — не intent-имена, argparse их не знает; work/review/feedback имена
    # разделяют с intents и разрешаются внутри (см. _dispatch_human_action).
    _facade = _dispatch_human_action(argv, main)
    if _facade is not None:
        return _facade
    ap = _build_cli_arg_parser()
    a = ap.parse_args(argv)

    intent = a.intent
    rest = list(a.rest)
    preview_mode = intent == "preview"
    if preview_mode:
        intent = rest.pop(0) if rest else "run"
    # разбор [задача] child_root (вынесен из main: ратчет func-size). rest мутируется на месте (pop),
    # поэтому остаток позиционных виден вызывающему (ветка resume ниже его дочитывает).
    task, child_root = _parse_task_and_root(intent, rest)
    # Сигналы вызова + перенос сохранённого со specify (в _build_signals: ратчет func-size). ДО
    # процессного гейта и диспетча — одни и те же сигналы видят и short_path/потолок, и план, и движок.
    signals = _build_signals(intent, task, child_root, a)

    # ЗАДАЧА ИЗ СПЕКИ, когда `run`/`do` вызваны без текста (только `--feature`). Движок строит задачу
    # писателю ТОЛЬКО из позиционного аргумента (`ctx = task + профиль`), а НЕ из spec.yaml — и вызов
    # без текста давал писателю пустой блок «=== ЗАДАЧА ===» (живой прогон #587). Спека — источник
    # истины: собираем задачу из её разделов. Fail-closed: нет спеки/разделов -> task пуст, как раньше.
    if intent in ("run", "do") and not (task or "").strip():
        from ai_ops_kit.gates import spec_levels as _sl
        _spec_task = _sl.task_from_spec(child_root, _wid_for(task, signals, a.feature))
        if _spec_task:
            task = _spec_task

    # ПЕРЕД процессным шагом: уже описанная работа идёт коротким путём, залипший разбор
    # останавливается вопросом владельцу. Место выбрано так, что ни один процессный шаг мимо не
    # проходит: ниже команды расходятся по ветвям, и проверку пришлось бы повторять в каждой.
    _gate_rc = _process_gate(intent, task, child_root, signals, a, preview_mode)
    if _gate_rc is not None:
        return _gate_rc

    if intent == "resume":
        from ai_ops_kit.engine import ai_ops_run
        # `ai-ops resume . <feature>` — путь репозитория ПЕРВЫМ позиционным (живой прогон на child,
        # 2026-08-14). Разбор каталога в хвосте эту форму не ловит: "." уезжал в task, оттуда в
        # workitem_id -> ValueError со стеком. Здесь она разбирается явно, и только когда за
        # каталогом реально стоит второй позиционный — обычный `resume "текст задачи"` не задет.
        if task and rest and Path(task).is_dir():
            child_root, task = task, rest.pop(0)
        # v2.109 Real Resume: --execute реально продолжает прогон (не рестарт); без флага — preflight.
        argv2 = ["resume", child_root, a.feature or (task or ""), "--base", a.base]
        argv2 += ["--session", _session_identity(child_root)]   # #695: та же личность, что у `run`
        # v3.0-rc2 (P0.1): intent CLI ПРОВОДИТ provider/model/signals в низкоуровневый resume (иначе
        # `--provider X` молча уходил в mock). F-026: провайдера НЕ подставляем — решает та же логика run.
        argv2 += ["--signals", a.signals]
        if a.provider:
            argv2 += ["--provider", a.provider]
        if a.model:
            argv2 += ["--model", a.model]
        if getattr(a, "replan", False):
            argv2.append("--replan")   # v3.0-rc4 (P0.1): осознанная смена policy при продолжении
        if getattr(a, "deliver_only", False):
            # #403: delivery-only resume — переиспользуем существующий режим reevaluate-only
            # (пропуск писателя, HEAD как committed_sha без нового коммита), затем обычная доставка.
            # Интент этот флаг раньше не прокидывал, поэтому resume всегда переавторил и плодил коммиты.
            argv2.append("--reevaluate-only")
        if getattr(a, "open_pr", False):
            argv2.append("--open-pr")  # #695: resume доводит готовую работу до ОТКРЫТОГО PR
        if getattr(a, "takeover", False):
            argv2.append("--takeover")  # #695: resume снимает брошенную/утёкшую заявку
            if getattr(a, "takeover_reason", None):
                argv2 += ["--takeover-reason", a.takeover_reason]
        if a.execute:
            argv2.append("--execute")
        if a.force:
            argv2.append("--force")
        if a.json:
            argv2.append("--json")
        return ai_ops_run.main(argv2)

    # v2.110 Real Spec-First: `specify` РЕАЛЬНО создаёт spec-артефакт нужной глубины (не только
    # превью). Тело — в спутнике _intent_specify (ратчет func-size main); Исход 3 #768 — там же.
    if intent == "specify":
        from ai_ops_kit.cli.ai_ops_cli_lifecycle import _intent_specify
        _intent_specify(task, Path(child_root), signals, a)
        return 0   # specify всегда завершается 0 (как и прежним inline-блоком) — код виден в main

    # v2.112 Intent UX: настоящие действия (не только превью). preview_mode -> всегда показать превью.
    # v2.116: `review` тоже настоящий intent — read-only ревью действующей ветки.
    # 2026-08-19: +session. Обработчик в `_run_intent` был написан, интент объявлен в INTENTS, а
    # сюда имя не внесли — и команда МОЛЧА печатала общую заглушку с кодом 0. То есть работа
    # `session-command-reaches-the-child` довела команду до дочки и не довела до исполнения.
    # Расхождение этого списка с тем, что реально умеет `_run_intent`, теперь краснеет тестом
    # `test_direct_intents_match_the_handler` — рукой список больше не забудут.
    if not preview_mode and intent in DIRECT_INTENTS:
        rc = _run_intent(intent, task, Path(child_root), signals, a)
        if rc is not None:
            return rc

    pv = build_preview(intent, task, Path(child_root), signals)
    # v3.28.x (F-015): роутер классифицировал тип задачи ВНУТРИ build_preview — но там он работает
    # с копией signals, и наружу классификация не выходила. Движок получал сигналы без task_type,
    # терял evidence `classified_type` и валил блокирующий intake_completeness у пользователя,
    # который всё указал правильно. Материализуем решение роутера в сигналы прогона.
    _understood_type = (pv.get("understood") or {}).get("task_type")
    if _understood_type and not signals.get("task_type"):
        signals["task_type"] = _understood_type
    # #702: если do/run --execute сейчас упрётся в недостающие intake-сигналы, НЕ показываем превью
    # «вот что я сделаю / запускай когда готов» — оно противоречит следующему сообщению «данных не
    # хватает». Пусть говорит одно: чего не хватает (это скажет intake-gap в _main_run_execute).
    _blocked_on_intake = False
    _will_execute_now = (intent == "run" and a.execute) or intent == "do"
    if _will_execute_now:
        from ai_ops_kit.engine import pipeline_helpers as _ph
        _blocked_on_intake = bool(_ph.missing_intake_signals(signals))
    # #708: немедленный старт (do/run --execute) -> превью говорит «запускаю сейчас», не «когда готов».
    pv["will_execute_now"] = _will_execute_now
    if a.json:
        print(json.dumps(pv, ensure_ascii=False, indent=2))
    elif not _blocked_on_intake:
        # Смысл — всегда; внутренний разбор превью (стадии, флаги, бюджет) — на technical/debug,
        # как у `next` и `model`. Разбор не выброшен: без него не отладить неверный подбор режима.
        _say(Path(child_root), "from_execution_preview", pv)
        if _audience(Path(child_root)) != "product":
            print()
            _print_preview(pv)

    # только `run --execute` и `do` реально запускают движок; остальное — превью/делегация
    return _main_run_execute(intent, task, child_root, signals, a, pv)


def _main_guarded(argv):
    """Граница CLI: отказ провайдера, который повтор не лечит, — человеку ФРАЗОЙ и кодом, а не трейсбеком.

    ЗАМЕР ПОЛЯ 20.08.2026 (obs 99aa67ef): при исчерпании лимита сессии claude-cli наружу выходил
    RuntimeError с полным питоновским трейсбеком. Провайдер теперь поднимает типизированный
    `ProviderLimitError`; здесь он превращается в сообщение и код возврата 3 («модель недоступна»).

    ЗАЯВКА #160: та же граница ловит структурную недоступность исполнителя в этой среде (запуск
    изнутри активной сессии Claude) — `ProviderEnvUnavailableError`. Наружу — последствие и оба
    выхода фразой, код 3, без трейсбека и без пяти бессмысленных повторов внутри.

    Ловим ТОЛЬКО эти типы — прочие ошибки по-прежнему всплывают, чтобы дефекты не тонули в тихом отказе.
    """
    from ai_ops_kit.providers.orchestrator_providers import (
        ProviderEnvUnavailableError,
        ProviderLimitError,
    )
    try:
        return main(argv)
    except (ProviderLimitError, ProviderEnvUnavailableError) as e:
        print(e.human_message(), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(_main_guarded(sys.argv[1:]))
