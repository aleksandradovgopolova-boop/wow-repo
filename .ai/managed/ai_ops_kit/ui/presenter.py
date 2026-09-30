#!/usr/bin/env python3
"""Human Communication Layer: между внутренним состоянием и человеком (v3.35.0).

Внутри кит говорит `GateResult`, `write_scope`, `tested_revision`, `preflight_block` — и обязан
продолжать: это точные имена, по которым работает код. Наружу они попадать не должны.

    внутреннее состояние -> UserMessage -> текст под выбранную аудиторию

`UserMessage` — контракт, а не совет по стилю (`registry/communication-policy.yaml`). Слой живёт в
КОДЕ, а не только в скилле, потому что соблюдение правил не может зависеть от того, вспомнила ли
конкретная модель вызвать скилл: иначе при смене runtime поведение теряется, а в половине прогонов
пользователь снова читает лог.

ЧТО СЛОЙ НЕ ДЕЛАЕТ. Не сглаживает. Простой язык — не мягкий: `degraded` остаётся `degraded` на всех
трёх уровнях, недоказанное называется недоказанным. «Готово» вместо «готово, но не проверено»
дороже любого жаргона, и presenter обязан этому мешать, а не помогать.

СТРУКТУРА (после разреза god-модуля). Фасад тонкий: коммуникационное ядро (`message`, `render`,
загрузка политики, глоссарий) живёт в `presenter_core.py`; проекции жизненного цикла работы
(`from_next_work`, `from_active_work`, `from_work_view`, `from_post_release_loop`) — в
`presenter_work.py`; проекции графа знаний и обратной связи (`from_graph_*`,
`from_kit_feedback_status`) — в `presenter_graph.py`. Остальные переводчики повседневных команд —
в `presenter_formatters.py`, доступны лениво через `__getattr__` (PEP 562). Фасад ре-экспортирует
всю публичную поверхность, поэтому внешний код по-прежнему зовёт `presenter.X`, а соседи
`presenter_formatters`/`presenter_report_formatters` берут `_q`/`message` из `presenter.*` как и
раньше.

Использование:
  presenter.py demo [--audience product|technical|debug]
"""
from __future__ import annotations

import argparse
import json
import sys

# ── Реэкспорт переводчиков, вынесенных в `presenter_formatters.py` ─────────────────────────────
# Большинство переводчиков (и повседневных команд, и внутренних отчётов) вынесены в модуль-сосед
# `presenter_formatters.py`, чтобы presenter не рос как god-модуль. Здесь остаётся только фасад:
# тонкая проводка (`demo`, `main`) и явный ре-экспорт публичной поверхности из ядра и сателлитов.
#
# Реэкспорт формата ЛЕНИВЫЙ (PEP 562), а не `from … import …` на верхнем уровне: сосед импортирует
# `message` и `_q` ИЗ этого модуля, а этот модуль на загрузке НЕ импортирует соседа обратно — иначе
# при импорте соседа первым получился бы цикл (сосед ещё не определил свои функции, а presenter их
# уже требует). При обращении к имени `presenter.from_*` формата модуль подгружается по требованию.
_FORWARDED_FORMATTERS = (
    "from_advice", "from_bootstrap", "from_contour_consistency", "from_discovery_draft",
    "from_doctor", "from_execution_preview", "from_first_hour", "from_intake_gap",
    "from_kit_feedback_recorded",
    "from_new_feature", "from_onboarding_profile", "from_plan_built", "from_process_spend",
    "from_product_health", "from_repository_understanding", "from_review", "from_session_economy",
    "from_short_path", "from_specification", "from_subsession_decision", "_CMD_RU",
)


def __getattr__(name):
    if name in _FORWARDED_FORMATTERS:
        from ai_ops_kit.ui import presenter_formatters
        return getattr(presenter_formatters, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def demo(audience="product"):
    """Один и тот же внутренний отчёт на трёх языках — то, что проверяют evals."""
    msg = message(
        status="needs_input",
        summary="Пока не начинаю разработку.",
        why_it_matters="Задача затрагивает защищённую часть проекта, поэтому мне нужно твоё "
                       "подтверждение. Остальное к работе готово.",
        decision={"question": "разрешить изменение модуля авторизации",
                  "recommendation": "разрешить только чтение агрегированных данных",
                  "on_approve": "начну реализацию и принесу результат на проверку",
                  "on_reject": "предложу вариант, который этот модуль не трогает"},
        next_steps=["после подтверждения — реализация и независимая проверка"],
        technical={"gate": "specification", "protected_paths": "auth/*",
                   "context": "128k / 150k", "approval_record": "missing"})
    return render(msg, audience=audience)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="presenter.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo")
    d.add_argument("--audience", choices=list(audiences()), default="product")
    d.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])
    if ns.cmd == "demo":
        if ns.json:
            print(json.dumps(load_policy().get("message_contract"), ensure_ascii=False, indent=2))
        else:
            print(demo(ns.audience))
    return 0


# ── Явный ре-экспорт публичной поверхности ────────────────────────────────────────────────────
# Внешний код делает `from ai_ops_kit.ui import presenter` и зовёт `presenter.X`; кроме того,
# `presenter_formatters` и `presenter_report_formatters` берут `_q`/`message` через
# `from ai_ops_kit.ui.presenter import _q, message`. Оба контракта держит явный ре-экспорт ниже —
# без `import *`, чтобы поверхность была видна глазом. Приватные имена (`_q`, `_humanize`,
# `_glossary`, `_contract`, `_human_evidence`) перечислены тоже: они — часть контракта соседей и
# проб.
from ai_ops_kit.ui.presenter_core import (  # noqa: E402
    POLICY,
    PolicyMissing,
    _CONTRACT,
    _GLOSSARY,
    _contract,
    _glossary,
    _humanize,
    _q,
    audience_from_config,
    audiences,
    load_policy,
    message,
    render,
    statuses,
)
from ai_ops_kit.ui.presenter_graph import (  # noqa: E402
    from_graph_build,
    from_graph_gaps,
    from_graph_questions,
    from_graph_trace,
    from_kit_feedback_status,
    from_scorecard,
)
from ai_ops_kit.ui.presenter_work import (  # noqa: E402
    _human_evidence,
    from_active_work,
    from_next_work,
    from_post_release_loop,
    from_work_view,
)

if __name__ == "__main__":
    sys.exit(main())
