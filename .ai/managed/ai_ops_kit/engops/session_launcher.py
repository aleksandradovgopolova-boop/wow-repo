#!/usr/bin/env python3
"""session_launcher.py — кит САМ решает, запускать ли новую подсессию, и сам себе не даёт потратить
больше объявленного потолка.

ЗАЧЕМ (решение владельца 2026-08-13). `session_guardrails` умеет только СОВЕТОВАТЬ человеку
«здесь новую сессию не начинай»: рантаймом Claude Code кит не управляет, и это остаётся правдой.
Но пакет работы, ради которого нужна свежая сессия, кит умеет вести САМ — `claude -p` уже
используется как writer (`orchestrator_providers._claude_cli_call`: read-only контракт, usage
измеряется, транзиенты ретраятся). Не хватало ровно двух вещей: РАЗРЕШЕНИЯ и ПОТОЛКА.

ПОЧЕМУ ПОТОЛОК СТОИТ НА СВОЕЙ ТРАТЕ, А НЕ НА «РАСХОДЕ СЕССИИ ВООБЩЕ». Расход интерактивной сессии
человека кит в деньгах не измеряет (в ledger попадают только вызовы, которые сделал он сам), и
выдавать оценку за потолок нельзя. Зато трату, которую кит совершает автономно, он может ДОКАЗАТЬ
по своим же записям: `role="autonomous-subsession"` + `run_id="subsession:<session>:<n>"`. Потолок
ставится ровно на неё — на то, что он тратит без человека в цикле.

ШЕСТЬ ЧЕСТНЫХ ОТКАЗОВ, А НЕ ОДНО «НЕЛЬЗЯ» (у каждого своя причина и своё лечение):
  unsafe_boundary     — посреди миграции/незакрытого коммита не переключаемся ни при каком потолке;
  no_ceiling          — потолок НЕ ОБЪЯВЛЕН, поэтому автономная трата не разрешена вообще.
                        Это не «осталось ноль»: разница в том, что делать (объявить, а не ждать);
  session_unidentified— кит не смог опознать сессию, значит не может отнести трату к ней и доказать,
                        что остался под потолком. Недоказанное не считается разрешённым;
  spend_unprovable    — в ledger есть вызовы с неизвестной стоимостью (`cost_complete=false`):
                        сумма заведомо неполна, «мы под потолком» недоказуемо;
  over_ceiling        — измеренная автономная трата уже достигла потолка;
  over_count          — исчерпан объявленный лимит числа подсессий.

ГРАНИЦА ЧЕСТНОСТИ (что кит НЕ МОЖЕТ и не начнёт делать). Он не управляет интерактивной сессией
человека: не делает `/clear`, `/compact`, не перезапускает её и не «продлевает». Он может ровно
две вещи: (а) открыть СВОЮ подсессию `claude -p` под потолком, (б) ОТКАЗАТЬСЯ тратить. Всё
остальное остаётся сильным советом `session_guardrails`.

ЗАВИСИМОСТЬ, КОТОРУЮ НЕ ПРЯЧЕМ. Опознание сессии даёт `session_telemetry` (+ его провайдер). Пока
провайдер не читает сессию, `session_id` = `unlabelled`, и автономия честно ОТКАЗЫВАЕТ
(`session_unidentified`), а не тратит вслепую. То есть автономия включается ровно тогда, когда
появляется измерение, — а не раньше.

БРИФ БЕРЁТСЯ ИЗ РЕПОЗИТОРИЯ, а не из истории родителя. Иначе «новая сессия» унесла бы в себе тот
же контекст, ради избавления от которого её и открывают, и экономия исчезла бы. Проверяется тестом.

CLI:  session_launcher.py <child_root> [--context N] [--next-relation R] [--next "текст"]
                          [--workitem WID] [--unsafe] [--json]
      Здесь — ТОЛЬКО решение, трата отсюда невозможна: исполнителя и учёт расхода подключает
      вызывающий сверху (см. `spawn`, шов usage_hooks). Это не осторожность ради осторожности —
      композиция снизу означала бы импорт слоя моделей в этот слой, а он импортирует нас.
      session_launcher.py --selftest
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402,F401
from ai_ops_kit.engops import session_guardrails  # noqa: E402
from ai_ops_kit.engops import session_telemetry  # noqa: E402
from ai_ops_kit.shared import usage_ledger  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

# Потолок автономии. None = НЕ ОБЪЯВЛЕН -> автономная трата запрещена (fail-closed, не «ноль»).
AUTONOMY_DEFAULTS = {
    "max_autonomous_spend_usd": None,
    "max_subsessions_per_session": None,
}
# Роль в ledger, по которой автономная трата отличима от всякой другой. Не менять молча:
# по ней считается потолок, и переименование обнулит учёт (тест это ловит).
AUTONOMOUS_ROLE = "autonomous-subsession"
RUN_ID_PREFIX = "subsession"

REFUSALS = ("unsafe_boundary", "no_ceiling", "session_unidentified",
            "spend_unprovable", "over_ceiling", "over_count", "no_usage_accounting")
ACTIONS = ("spawn_subsession", "continue_here", "refuse")
# Свежая сессия оправдана только когда контекст ИЗМЕРЕННО дорогой И работа впереди — другая.
WARRANTING_STATES = ("compact_recommended", "new_session_recommended")


def load_ceiling(child_root):
    """Потолок автономии из `.ai-ops.yaml -> session_economy`, а у самого кита — из `config/`.

    Читается тот же блок, что у `session_guardrails`, но СВОИМИ ключами и своими дефолтами:
    пороги СОВЕТА и потолок ТРАТЫ — разные решения владельца, и слипаться им нельзя (порог совета
    можно поднять из удобства; потолок траты — нет).

    ВТОРОЙ ИСТОЧНИК И ПОЧЕМУ ОН НЕ ДРЕЙФ. `.ai-ops.yaml` описывает связь «репозиторий ↔ кит», и у
    самого кита такой связи нет — он и есть кит (это же зафиксировано в проверке самоприменения как
    «не применимо»). Без второго места автономия была бы навсегда недоступна ровно там, где её
    отлаживают. Поэтому: файл дочки — ГЛАВНЫЙ, `config/session-economy.yaml` читается ТОЛЬКО когда
    его нет. Два источника одновременно не складываются и не выбираются «по свежести».
    """
    out = dict(AUTONOMY_DEFAULTS)
    child_cfg = Path(child_root) / ".ai-ops.yaml"
    own_cfg = Path(child_root) / "config" / "session-economy.yaml"
    src, key = (child_cfg, "session_economy") if child_cfg.is_file() else (own_cfg, "session_economy")
    if yaml and src.is_file():
        try:
            se = (yaml.safe_load(src.read_text(encoding="utf-8")) or {}).get(key) or {}
        except (yaml.YAMLError, OSError):
            # Нечитаемый конфиг НЕ означает «потолка нет и трать сколько хочешь»: дефолты
            # fail-closed (None), поэтому итог — отказ no_ceiling. Молчаливого разрешения не будет.
            return out
        for k in AUTONOMY_DEFAULTS:
            if k in se:
                out[k] = se[k]
    return out


# Сколько подсессий должно уместиться в предлагаемую сумму. Не «побольше на всякий случай»: число
# входит в предложение владельцу, и он должен видеть, за что платит.
SUGGESTED_SUBSESSIONS = 4
# Меньше этого числа измеренных вызовов — выборка не выборка, предложения не делаем. Порог не
# выведен статистикой, он объявлен: на трёх вызовах p90 — это просто максимум.
MIN_SAMPLE_FOR_SUGGESTION = 20


def suggest_ceiling(child_root, subsessions=SUGGESTED_SUBSESSIONS, reference_roots=None):
    """Предложить сумму, ВЫВЕДЕННУЮ ИЗ ЗАМЕРА, а не выбранную на вкус.

    ЗАЧЕМ. Отказ `no_ceiling` требует от владельца назвать число, которого он знать не может: цену
    вызова видел только кит. Требовать решения, для которого у человека нет данных, — это переложить
    работу. Поэтому кит считает сам, а владельцу оставляет согласие.

    ЧЕСТНО О ОСНОВАНИИ. Считается p90 ИЗМЕРЕННОЙ стоимости вызовов из ledger (не средняя: платить
    придётся за дорогие, а не за типичные) и умножается на число подсессий. Основание всегда
    называется вместе с числом: `measured` + размер выборки, `borrowed` + откуда взято, либо
    `no_measurement` и тогда числа НЕТ. Выдуманного числа под видом расчёта не будет.
    """
    def _measured(root):
        return [float(r["cost"]) for r in usage_ledger.load_product(root)
                if r.get("cost") is not None and r.get("cost_status") == "measured"]

    own = _measured(child_root)
    costs, basis, source = own, "measured", str(child_root)
    if len(own) < MIN_SAMPLE_FOR_SUGGESTION and reference_roots:
        borrowed = []
        used = []
        for r in reference_roots:
            got = _measured(r)
            if got:
                borrowed += got
                used.append(str(r))
        if len(borrowed) >= MIN_SAMPLE_FOR_SUGGESTION:
            costs, basis, source = borrowed, "borrowed", ", ".join(used)
    if len(costs) < MIN_SAMPLE_FOR_SUGGESTION:
        return {"amount": None, "basis": "no_measurement", "sample": len(costs), "source": None,
                "reason": f"в этом репозитории измеренных вызовов {len(costs)} — меньше "
                          f"{MIN_SAMPLE_FOR_SUGGESTION}, поэтому предлагать сумму было бы гаданием."}
    ordered = sorted(costs)
    p90 = ordered[max(0, math.ceil(len(ordered) * 0.9) - 1)]
    amount = math.ceil(p90 * subsessions * 100) / 100.0
    return {"amount": amount, "basis": basis, "sample": len(costs), "source": source,
            "p90": round(p90, 4), "subsessions": subsessions,
            "reason": f"{subsessions} подсессии по ${p90:.2f} — это p90 измеренной стоимости вызова "
                      f"({len(costs)} вызовов, {'замер этого репозитория' if basis == 'measured' else 'замер: ' + source})."}


def autonomous_spend(child_root, session_id):
    """Сколько кит уже потратил АВТОНОМНО в этой сессии. Считается по своим же записям.

    -> {"cost": float, "calls": int, "cost_complete": bool}. `cost_complete=False` означает, что
    среди записей есть вызовы с неизвестной стоимостью: сумма НЕПОЛНА, и по ней нельзя утверждать
    «мы под потолком» (см. отказ spend_unprovable).
    """
    prefix = f"{RUN_ID_PREFIX}:{session_id}:"
    mine = [r for r in usage_ledger.load_product(child_root)
            if r.get("role") == AUTONOMOUS_ROLE and str(r.get("run_id") or "").startswith(prefix)]
    agg = usage_ledger.aggregate(mine)
    return {"cost": agg["cost"], "calls": agg["calls"], "cost_complete": agg["cost_complete"]}


def _refuse(code, reason, numbers):
    return {"kind": "SubsessionDecision", "schema_version": 1, "action": "refuse",
            "refusal": code, "reason": reason, "numbers": numbers}


def decide(child_root, snapshot, *, ceiling=None, next_relation="new_independent_task",
           task_done=True, at_safe_boundary=True, next_task=None):
    """Решение: открыть подсессию, продолжить здесь или отказаться — с названной причиной и числами.

    Порядок проверок не косметический: сначала безопасность границы (её нельзя купить потолком),
    затем разрешение тратить, затем доказуемость расхода, и только потом — оправданность свежей
    сессии. Иначе кит объяснял бы отказ второстепенной причиной.
    """
    c = ceiling if ceiling is not None else load_ceiling(child_root)
    limit = c.get("max_autonomous_spend_usd")
    max_count = c.get("max_subsessions_per_session")
    session_id = snapshot.get("session_id")
    state = session_guardrails.classify_context(snapshot.get("context_current"))
    numbers = {"ceiling_usd": limit, "max_subsessions": max_count,
               "context_state": state, "context_status": snapshot.get("context_status"),
               "session_id": session_id}

    if not at_safe_boundary:
        return _refuse("unsafe_boundary",
                       "небезопасная точка (миграция или незакрытый коммит) — работу здесь не "
                       "прерываем и подсессию не открываем; вернуться к решению на безопасной точке.",
                       numbers)
    if limit is None:
        sug = suggest_ceiling(child_root)
        numbers["suggested_usd"] = sug.get("amount")
        numbers["suggestion_basis"] = sug.get("basis")
        numbers["suggestion_reason"] = sug.get("reason")
        return _refuse("no_ceiling",
                       "потолок автономной траты не объявлен, поэтому тратить без человека нельзя. "
                       "Это не «закончились деньги»: нужно объявить `session_economy."
                       "max_autonomous_spend_usd`, и автономия включится."
                       + (f" Предлагаю ${sug['amount']}: {sug['reason']}" if sug.get("amount")
                          else f" Своё число предложить не могу: {sug['reason']}"),
                       numbers)
    if not session_id or session_id == "unlabelled":
        return _refuse("session_unidentified",
                       "сессия не опознана, значит трату нельзя отнести к ней и нельзя доказать, "
                       "что она осталась под потолком. Недоказанное не считается разрешённым.",
                       numbers)

    spent = autonomous_spend(child_root, session_id)
    numbers.update({"spent_usd": spent["cost"], "subsessions_used": spent["calls"],
                    "spend_provable": spent["cost_complete"]})
    if not spent["cost_complete"]:
        return _refuse("spend_unprovable",
                       "среди уже сделанных вызовов есть такие, чья стоимость неизвестна — сумма "
                       "расхода неполна, и утверждать «мы под потолком» было бы заявлением шире "
                       "измеренного.", numbers)
    if spent["cost"] >= float(limit):
        return _refuse("over_ceiling",
                       f"автономная трата в этой сессии уже {spent['cost']:.4f}$ при потолке "
                       f"{float(limit):.4f}$ — дальше без решения человека не тратим.", numbers)
    if max_count is not None and spent["calls"] >= int(max_count):
        return _refuse("over_count",
                       f"исчерпан объявленный лимит подсессий ({spent['calls']} из {int(max_count)}).",
                       numbers)

    # Разрешение есть. Дальше вопрос не «можно ли», а «нужно ли».
    if state == "unknown":
        return {"kind": "SubsessionDecision", "schema_version": 1, "action": "continue_here",
                "refusal": None,
                "reason": "объём контекста не измерен, а на догадке свежую сессию не начинаем: "
                          "неизмеренное — это «не знаю», а не «дорого».", "numbers": numbers}
    if not task_done:
        return {"kind": "SubsessionDecision", "schema_version": 1, "action": "continue_here",
                "refusal": None,
                "reason": "текущая работа не закрыта — сначала закрыть её, её контекст ещё нужен.",
                "numbers": numbers}
    if next_relation not in session_guardrails.NEW_RELATIONS:
        return {"kind": "SubsessionDecision", "schema_version": 1, "action": "continue_here",
                "refusal": None,
                "reason": "следующий шаг — та же работа; собранные знания переиспользуются, "
                          "открывать свежую сессию значило бы исследовать заново.",
                "numbers": numbers}
    if state not in WARRANTING_STATES:
        return {"kind": "SubsessionDecision", "schema_version": 1, "action": "continue_here",
                "refusal": None,
                "reason": f"контекст ещё недорогой ({state}) — тратить на отдельную сессию нечего.",
                "numbers": numbers}

    return {"kind": "SubsessionDecision", "schema_version": 1, "action": "spawn_subsession",
            "refusal": None,
            "reason": f"контекст {state} и впереди другая работа — вести её здесь значит платить за "
                      f"перечитывание ненужной истории. Осталось до потолка "
                      f"{float(limit) - spent['cost']:.4f}$.",
            "numbers": numbers, "next_task": next_task}


def build_brief(*, workitem_id=None, title=None, repo_path=None, safe_step=None, facts=None,
                max_chars=4000):
    """Бриф подсессии — ИЗ ФАКТОВ РЕПОЗИТОРИЯ, не из истории родителя.

    Приёмка (тест): бриф не содержит транскрипта родителя и ограничен по объёму. Если бриф начнёт
    расти историей, экономия исчезнет — свежая сессия унесёт с собой то, от чего её открывали.
    """
    L = ["Ты работаешь в отдельной сессии, открытой AI Ops под объявленным потолком расхода.",
         "Контекста прошлой сессии у тебя нет — и не нужен: всё необходимое ниже и в репозитории.", ""]
    if workitem_id or title:
        L.append(f"Работа: {workitem_id or '—'} — {title or '—'}")
    if repo_path:
        L.append(f"Репозиторий: {repo_path}")
    for f in (facts or []):
        L.append(f"- {f}")
    if safe_step:
        L += ["", f"Следующий безопасный шаг: {safe_step}"]
    L += ["", "Контракт: ты ЧИТАЕШЬ репозиторий и ПРЕДЛАГАЕШЬ изменение текстом. Применяет его кит "
              "через свои проверки — сам не пиши файлы, не коммить, не создавай PR."]
    brief = "\n".join(L)
    if len(brief) > max_chars:
        # Усечение НАЗЫВАЕТСЯ: молчаливое читалось бы как «в брифе всё».
        brief = brief[:max_chars] + f"\n\n[бриф усечён до {max_chars} символов — часть фактов не вошла]"
    return brief


def spawn(child_root, brief, snapshot, *, ceiling=None, provider=None, usage_hooks=None,
          workitem_id=None, decision=None):
    """Открыть подсессию — но только если решение это разрешило. Проверка ДО траты, не после.

    ДВА ШВА ИНЪЕКТИРУЮТСЯ, И ЭТО НЕ ради тестов, а из-за направления слоёв. Модели и учёт живут в
    `providers`, а `providers.cost_method` уже импортирует этот слой (`engops`); импорт обратно
    сделал бы восьмую взаимную пару и поднял циклы ядра 52 -> 78 (ратчет `test_layering` это
    ловит — замерено). Поэтому композицию делает вызывающий сверху:

      provider     — callable(prompt) -> text. Живой: `orchestrator_providers.make_claude_cli_provider()`
                     (тот же read-only `claude -p`, что кит уже использует как writer).
      usage_hooks  — объект с `set_context(**kw)` и `drain() -> list[dict]`. Живой: обёртка над
                     `providers.orchestrator_usage` (`set_call_context` / `drain_call_stats`).

    ПОЧЕМУ БЕЗ УЧЁТА НЕ ТРАТИМ. `_record_call` только накапливает записи в памяти; дренирует их
    вызывающий. Без этого шага автономная трата не попала бы в ledger, и СЛЕДУЮЩИЙ потолок считался
    бы по неполной сумме — то есть потолок тихо перестал бы работать. Поэтому отсутствие учёта — не
    «мелкая деталь конфигурации», а отказ `no_usage_accounting`.
    """
    c = ceiling if ceiling is not None else load_ceiling(child_root)
    d = decision or decide(child_root, snapshot, ceiling=c)
    if d["action"] == "spawn_subsession" and (provider is None or usage_hooks is None):
        d = _refuse("no_usage_accounting",
                    "тратить нельзя: не подключён учёт расхода (или исполнитель), а трата, которую "
                    "нельзя записать, сделала бы следующий потолок недействительным.",
                    d.get("numbers") or {})
    if d["action"] != "spawn_subsession":
        # Ни одного вызова модели: отказ означает «не потратили», а не «потратили и передумали».
        return {"kind": "SubsessionResult", "schema_version": 1, "spawned": False,
                "decision": d, "spend_before": None, "spend_after": None, "result": None,
                "ceiling_crossed_by": None}

    session_id = snapshot.get("session_id")
    before = autonomous_spend(child_root, session_id)
    run_id = f"{RUN_ID_PREFIX}:{session_id}:{before['calls'] + 1}"

    usage_hooks.drain()             # чужие записи в буфере не отнесём к своей подсессии
    usage_hooks.set_context(role=AUTONOMOUS_ROLE, run_id=run_id, trigger="initial",
                            workitem_id=workitem_id)
    try:
        text = provider(brief)
    finally:
        stats = usage_hooks.drain()
        written = usage_ledger.append(child_root, workitem_id, stats, run_id=run_id)

    after = autonomous_spend(child_root, session_id)
    limit = float(c["max_autonomous_spend_usd"])
    crossed = round(after["cost"] - limit, 6) if after["cost"] > limit else None
    return {"kind": "SubsessionResult", "schema_version": 1, "spawned": True, "decision": d,
            "run_id": run_id, "result": text,
            "spend_before": before, "spend_after": after, "records_written": written,
            # Потраченное не отменить. Честная половина — НАЗВАТЬ пересечение, а не спрятать его:
            # следующий `decide` откажет по over_ceiling, и причина будет видна человеку.
            "ceiling_crossed_by": crossed}


def check(d):
    """Валидация решения: исход из закрытого набора, у отказа названа причина, у отказа есть код."""
    e = []
    if not isinstance(d, dict) or d.get("kind") != "SubsessionDecision":
        return ["kind должен быть SubsessionDecision"]
    if d.get("action") not in ACTIONS:
        e.append(f"action ∉ {ACTIONS} (got {d.get('action')!r})")
    if d.get("action") == "refuse":
        if d.get("refusal") not in REFUSALS:
            e.append(f"refusal ∉ {REFUSALS} (got {d.get('refusal')!r})")
    elif d.get("refusal") is not None:
        e.append("refusal заполнен при action != refuse")
    if not (d.get("reason") or "").strip():
        e.append("решение без названной причины")
    return e


def render_block(d):
    """Человеку — простым языком: что решено, почему, и что от него нужно (если нужно)."""
    n = d.get("numbers") or {}
    ceiling = n.get("ceiling_usd")
    spent = n.get("spent_usd")
    head = {"spawn_subsession": "Беру эту работу в отдельную сессию.",
            "continue_here": "Продолжаю здесь, отдельная сессия не нужна.",
            "refuse": "Сам продолжить не могу."}[d["action"]]
    L = [head, "", d["reason"]]
    if ceiling is not None:
        used = "н/д" if spent is None else f"{spent:.4f}$"
        L.append(f"Потрачено самостоятельно: {used} из разрешённых {float(ceiling):.4f}$.")
    else:
        L.append("Разрешённая сумма самостоятельных трат пока не назначена.")
    return "\n".join(L)


def main(argv):
    if "--selftest" in argv:
        return _selftest()
    wid = ctx = nrel = nxt = None
    args, it = [], iter(argv)
    for a in it:
        if a == "--workitem":
            wid = next(it, None)
        elif a == "--context":
            v = next(it, None)
            ctx = int(v) if v and v.isdigit() else None
        elif a == "--next-relation":
            nrel = next(it, None)
        elif a == "--next":
            nxt = next(it, None)
        elif not a.startswith("--"):
            args.append(a)
    root = args[0] if args else "."
    snap = session_telemetry.snapshot(root, workitem_id=wid, context_current=ctx)
    d = decide(root, snap, next_relation=nrel or "new_independent_task", next_task=nxt,
               at_safe_boundary="--unsafe" not in argv)
    print(json.dumps(d, ensure_ascii=False, indent=2) if "--json" in argv else render_block(d))
    return 0


def _selftest():
    """Офлайн: без потолка — отказ; неопознанная сессия — отказ; ни один отказ не тратит."""
    import tempfile
    calls = []
    with tempfile.TemporaryDirectory() as td:
        snap = {"session_id": "s1", "context_current": 500000, "context_status": "measured"}
        d = decide(td, snap)
        assert d["action"] == "refuse" and d["refusal"] == "no_ceiling", d
        assert not check(d), check(d)
        ceil_ = {"max_autonomous_spend_usd": 1.0, "max_subsessions_per_session": 2}
        d2 = decide(td, {**snap, "session_id": "unlabelled"}, ceiling=ceil_)
        assert d2["refusal"] == "session_unidentified", d2
        r = spawn(td, "brief", {**snap, "session_id": "unlabelled"}, ceiling=ceil_,
                  provider=lambda p: calls.append(p) or "text")
        assert r["spawned"] is False and calls == [], (r, calls)
        d3 = decide(td, snap, ceiling=ceil_)
        assert d3["action"] == "spawn_subsession", d3
        brief = build_brief(workitem_id="w1", title="работа", repo_path=td)
        assert "транскрипт" not in brief.lower()
    print("session_launcher selftest: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
