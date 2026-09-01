#!/usr/bin/env python3
"""General helper functions for the execution pipeline.

Extracted from execution_pipeline.py — profile summary, intake evidence,
gate checklist, reviewable gates, YAML parsing, openspec validation.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
for _p in (PKG / "tools", PKG / "validation"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ai_ops_kit.gates import gate_executor  # noqa: E402


def work_produced(rep) -> bool:
    """Была ли РЕАЛЬНО произведена работа в этом прогоне. -> bool.

    ОДИН ПРЕДИКАТ НА ВСЕХ ПОТРЕБИТЕЛЕЙ. Счётчик `loop.applied_writes` считает правки, прошедшие
    через брокера, — и это НЕ то же самое, что «работа сделана»: writer уровня `claude -p` правит
    файлы своими инструментами, `sed -i` правит через shell, а модель может закоммитить сама.
    Из-за подмены одного другим ии-среда дважды за день получила «код не написан — правок 0» при
    живом коммите, и работа помечалась blocked: по отчёту кит выглядел неработающим, хотя работал.

    Ground truth — git: есть коммит и в нём есть файлы. Счётчик брокера остаётся запасным путём для
    не-git деревьев, где сверять не с чем.
    """
    rep = rep or {}
    commit = rep.get("commit") or {}
    if commit.get("sha") and (commit.get("changed_files") or []):
        return True
    return ((rep.get("loop") or {}).get("applied_writes") or 0) > 0


def delivery_pending(rep) -> bool:
    """Работа готова на ветке, а новых правок в этом прогоне нет. -> bool.

    B2-20 (повтор незакрытой B2-12, живой прогон 14.08.2026): `resume` завершённой-но-НЕДОСТАВЛЕННОЙ
    работы снова звал писателя, получал ноль правок — делать уже нечего — и хоронил готовый
    READY_FOR_PR в `blocked: код не написан`. Работа с коммитом на ветке исчезала из активного
    состояния, и владелец видел «кит не справился» там, где кит справился и ждал доставки.

    Предикат отвечает ровно на этот вопрос и живёт рядом с `work_produced`, потому что это его
    обратная сторона: там «работа произведена СЕЙЧАС», здесь «произведена РАНЬШЕ и ждёт заявки».
    """
    rep = rep or {}
    return bool((rep.get("resume") or {}).get("reused_branch")) and not work_produced(rep)


def acceptance_blocks_ready(acceptance_criteria) -> tuple:
    """Приёмка НЕ пускает работу в READY_FOR_PR? -> (block: bool, reason | None).

    Блокирует, когда: `verified` и есть `unmet` (B2-30); ЛИБО `attempted` без `verified` — судья
    приёмки поднят и отработал, но сверка не состоялась (green-means-checked). Судью не поднимали
    вовсе (`attempted=False`) — НЕ блокируем (граница #176). Разбор — в
    tests/unit/test_acceptance_rubber_stamp_not_ready.py.
    """
    ac = acceptance_criteria or {}
    if not ac.get("declared"):
        return False, None                       # нечего сверять — нечем и блокировать
    if ac.get("verified"):
        if ac.get("unmet"):
            return True, (f"сверка приёмки состоялась: НЕ ВЫПОЛНЕНО {len(ac.get('unmet') or [])} "
                          f"из {ac.get('count')} ({', '.join(ac.get('unmet') or [])}) — задача не "
                          f"доведена, READY_FOR_PR объявлять нельзя")
        return False, None
    if ac.get("attempted"):
        return True, ((ac.get("reason") or "приёмка объявлена, но сверка не состоялась")
                      + " — судья приёмки был поднят, но результат с критериями не сверил; закрыть "
                        "можно сверкой судьёй, который читает результат, либо приёмкой человеком")
    return False, None


def _profile_summary(profile):
    stacks = profile.get("stacks") or []
    langs = ", ".join(s.get("language", "?") for s in stacks) or "не определён"
    cmds = {}
    for s in stacks:
        for k, v in (s.get("commands") or {}).items():
            if v and k not in cmds:
                cmds[k] = v
    return f"Стек: {langs}. Команды проверки: {cmds or 'нет'}."


# Соответствие required_evidence гейта intake_completeness входным сигналам.
INTAKE_SIGNAL_MAP = {"classified_type": "task_type", "size": "size", "risk": "risk"}

# Что кит классифицирует САМ: task_type выводит роутер (run_plan.build_plan -> base_workflow),
# поэтому спрашивать его у пользователя не нужно. Всё остальное — продуктовое суждение вызывающего:
# вывести размер и риск из репозитория нечем, а угадать их значило бы сфабриковать evidence
# блокирующего гейта. Значит, их надо ЗАПРОСИТЬ до старта, а не завалить гейт после прогона.
DERIVED_INTAKE_FLAGS = {"classified_type"}

# Словари значений — из мест, где они реально используются: size ниже сверяется с
# atomic_planner.SIZE_FILES, risk — уровни эскалации spec_levels (critical/high -> L3).
INTAKE_SIGNAL_VALUES = {
    "size": ("small", "medium", "large", "xl"),
    "risk": ("low", "medium", "high", "critical"),
}


def missing_intake_signals(signals):
    """Обязательные intake-сигналы, которых нет во входе, — с допустимыми значениями.

    Источник истины — `required_evidence` гейта intake_completeness в quality/gates.yaml, а не
    список в коде: иначе он разойдётся с реестром (тот же класс дрейфа, что checks_count).

    Зачем: без `size` блокирующий гейт intake_completeness падает «бездоказательным pass», но
    узнаётся это только ПОСЛЕ прогона — в живой квалификации так сгорело 6 прогонов из 6, один
    из них 36 минут. Спросить надо до старта."""
    sig = signals or {}
    try:
        gate = (gate_executor.load_gates() or {}).get("intake_completeness") or {}
        required = list(gate.get("required_evidence") or [])
    except Exception:   # noqa: BLE001 — недоступный реестр не должен ронять прогон
        required = list(INTAKE_SIGNAL_MAP)
    out = []
    for flag in required:
        if flag in DERIVED_INTAKE_FLAGS:
            continue
        key = INTAKE_SIGNAL_MAP.get(flag, flag)
        if not sig.get(key):
            out.append({"flag": flag, "signal": key,
                        "allowed": list(INTAKE_SIGNAL_VALUES.get(key) or [])})
    return out


def intake_signals_command(missing):
    """Готовая строка ответа: `--signals '{"size":"small"}'`. -> строка или None.

    Отдельная функция, потому что эту же строку печатает человекочитаемый слой, а собирать её
    вторым способом значило бы завести второе правило: разойдутся именно в допустимых значениях.
    """
    if not missing:
        return None
    example = ", ".join(f'"{m["signal"]}":"{(m["allowed"] or ["<значение>"])[0]}"' for m in missing)
    return f"--signals '{{{example}}}'"


def intake_signals_hint(missing, task="<задача>"):
    """Готовая к печати подсказка: чего не хватает и как это передать одной строкой."""
    if not missing:
        return None
    names = ", ".join(m["signal"] for m in missing)
    one = len(missing) == 1
    lines = [f"intake неполон: не {'задан' if one else 'заданы'} {names} — "
             f"блокирующий гейт intake_completeness {'его' if one else 'их'} требует"]
    for m in missing:
        allowed = " | ".join(m["allowed"]) if m["allowed"] else "значение"
        lines.append(f"  · {m['signal']}: {allowed}")
    lines.append(f"  добавь: {intake_signals_command(missing)}")
    return lines


def _intake_evidence(signals):
    """intake_completeness evidence из сигналов: классификация уже сделана (реальный evidence,
    не фабрикация). Маппинг сигнал->required_evidence-флаг; provided только для присутствующих."""
    sig = signals or {}
    provided = [flag for flag, key in INTAKE_SIGNAL_MAP.items() if sig.get(key)]
    if not provided:
        return None
    return {"status": "pass", "provided": provided,
            "evidence": [f"intake из сигналов: {', '.join(provided)}"]}


# v2.85 (finding аудита): гейты, которые НЕЛЬЗЯ закрывать автоматическим ревьюером той же модели —
# слишком консеквентны для self-attestation.
NO_SELF_REVIEW = {"security", "ai_red_team"}


def _reviewable_gates(gate_ids, signals):
    """v2.83/2.85: гейты плана, которые НЕЗАВИСИМЫЙ ревьюер той же модели может закрыть легитимно —
    только ai-review (writer ≠ judge), И НЕ из NO_SELF_REVIEW."""
    gates = gate_executor.load_gates()
    out = []
    for gid in gate_ids:
        if gid in NO_SELF_REVIEW:
            continue
        g = gates.get(gid) or {}
        if gate_executor.classify(g, signals) == "ai-review":
            out.append(gid)
    return out


def _gate_checklist(gate):
    """Короткий чек-лист для ревьюера: required_evidence + ответственная роль."""
    req = gate.get("required_evidence", []) or []
    role = gate.get("responsible_role", "reviewer")
    parts = [f"роль: {role}"]
    if req:
        parts.append("подтверди по факту: " + ", ".join(req))
    return "; ".join(parts)


def _parse_yaml_block(text):
    """Достать YAML-артефакт из ответа author-модели. v3.0-rc5 (finding живого прогона kimi): терпимо к
    РАЗНЫМ стилям вывода моделей — несколько ```-блоков, проза вокруг, YAML без ограды после текста."""
    import yaml
    # `import re` здесь был дублем модульного (строка 10) — снят ревизией 2026-08-11.
    if isinstance(text, dict):
        return text
    s = text or ""
    candidates = []
    for m in re.finditer(r"```[ \t]*[A-Za-z0-9]*\n(.*?)```", s, re.S):
        candidates.append(m.group(1))
    for marker in ("schema_version:", "kind:"):
        i = s.find(marker)
        if i >= 0:
            candidates.append(s[i:])
    candidates.append(s)
    for c in candidates:
        try:
            data = yaml.safe_load(c)
        except yaml.YAMLError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _openspec_validate(work_root, change_id):
    """v2.89: прогнать НАСТОЯЩИЙ openspec CLI на произведённом change. -> (available, ok, output)."""
    try:
        r = subprocess.run(["openspec", "validate", change_id, "--strict"],
                           cwd=str(work_root), capture_output=True, text=True, timeout=120,
                           env={**os.environ, "OPENSPEC_TELEMETRY": "0"})
        return True, r.returncode == 0, (r.stdout + r.stderr)[-600:]
    except FileNotFoundError:
        return False, False, "openspec CLI не найден в PATH (npm i -g @fission-ai/openspec)"
    except subprocess.TimeoutExpired:
        return True, False, "openspec validate: timeout"


def _authoring_specs():
    """v2.86: артефакт-гейты, которые движок умеет ЗАКРЫВАТЬ производством артефакта + детерминированной
    проверкой ФОРМЫ (не «качества»). specification обрабатывается ОТДЕЛЬНО."""
    # Чистые проверки формы живут ВНИЗ, в пакете `checks` (слой primitives): зовём их вниз, без
    # восходящего ребра engine -> validation (лента №5).
    from ai_ops_kit.checks import requirements_artifact as vra
    from ai_ops_kit.checks import plan_artifact as vpa
    return {
        "requirements": ("requirements.yaml", vra, "requirements-artifact",
                         "requirements: список объектов {id, statement (тестируемое требование), "
                         "acceptance: [сценарии приёмки]}"),
        "plan_readiness": ("plan.yaml", vpa, "plan-artifact",
                           "work_packages: [{id, summary, depends_on: [id,...]}], "
                           "write_scope: [пути]"),
    }

# Запуск скриптом ОБЪЯСНЯЕТ модуль, а не молчит (ревизия 2026-08-11).
#
# Здесь стояло `sys.exit(selftest())`, а сама функция удалена в v3.30 вместе с переносом
# селфтестов в pytest: любой запуск падал с `NameError`. Просто убрать блок — тоже неверно:
# `tools/pipeline_helpers.py` остаётся объявленной точкой входа, и молчаливый выход с кодом 0 — тот
# самый дефект, который ловит `tests/unit/test_alias_entry_points.py` («ноль и есть симптом»).
# Поэтому вход делает осмысленную работу — печатает назначение модуля, как `invariants.py`.
# Проверки модуля — в `tests/unit/`.
if __name__ == "__main__":
    print(__doc__)
    print("Проверки этого модуля — в tests/unit/ (pytest), отдельного --selftest нет с v3.30.")
