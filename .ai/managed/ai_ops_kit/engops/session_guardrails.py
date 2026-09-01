#!/usr/bin/env python3
"""session_guardrails.py (v3.16.0 Development Culture Guardrails, WP3+WP5) — политика экономии сессии
(пороги контекста) + рекомендация по границе сессии + Task Completion Ritual.

WP3 SessionEconomyPolicy: пороги гигиены контекста (стартовые, калибруются на реальных данных).
WP5 Task Completion Ritual: после завершения WorkItem — обязательная SessionRecommendation с ТОЧНОЙ
командой (один из 4 исходов: continue / compact / clear / new_session), плюс defer на НЕбезопасной границе.

ГРАНИЦА ЧЕСТНОСТИ: кит НЕ управляет рантаймом Claude Code и НЕ форсит /clear|/compact — он даёт СИЛЬНЫЙ
совет с точной командой (решение владельца — enforcement=advise, не block). Кит не прерывает посреди
миграции/коммита: на небезопасной границе рекомендация — дождаться безопасной точки.

CLI:  session_guardrails.py <child_root> [--workitem WID] [--context N] [--next-relation R]
                            [--next "текст"] [--pr URL] [--checks "183/183"] [--unsafe] [--json]
      session_guardrails.py --selftest
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402
from ai_ops_kit.engops import session_telemetry  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

SESSION_ECONOMY_DEFAULTS = {
    "target_context_tokens": 150000,
    "compact_recommended_at": 250000,
    "new_session_recommended_at": 400000,
    "one_task_per_session": True,
    "allow_cross_task_session": False,
    # ПОТОЛОК СЕССИИ, а не одного прогона (2026-08-13). `engine/budget.py` ограничивает прогон
    # (max_model_calls/max_cost), `gates/economic_preflight` и ledger — вызовы моделей кита. Ни один
    # из них не видел главный источник расхода: сессию, которая живёт неделями и на КАЖДОМ ходе
    # заново оплачивает перечитывание истории. Считается по суммарным токенам всех ходов сессии,
    # включая кэш-чтения — именно они и есть плата за перечитывание.
    #
    # Число стартовое и калибруется, как и пороги контекста. Замер по двум настоящим транскриптам:
    # рабочая сессия на 45 ходов — 3.5M токенов; сессия, прожившая с 18.07 по 04.08 с девятью
    # компакциями — 2.9 МЛРД токенов при контексте 405k. Второй случай — ровно тот, который пороги
    # контекста не ловят: контекст «нормальный» после компакции, а перечитывание уже оплачено.
    "session_token_budget": 20000000,
    # ПОТОЛОК ПРОЦЕССНОЙ ФАЗЫ (решение владельца 2026-08-17). Отдельный предмет измерения: не
    # «сколько сессия прочитала всего», а «сколько ушло на ОПИСАНИЕ работы до первой правки кода».
    # Замер поля (ИИ-Среда, 15.08.2026): две сессии из пяти сожгли по 200 000+ токенов на
    # `onboard -> specify -> plan` и встали без кода — в потолок сессии (20M) это умещается целиком,
    # поэтому он такое залипание не видел и увидеть не мог. Механизм — `engops/process_spend.py`.
    "process_spend_ceiling_before_code": 50000,
}

CONTEXT_STATES = ("normal", "attention", "compact_recommended", "new_session_recommended", "unknown")
SPEND_STATES = ("normal", "attention", "over_budget", "unknown")
# отношение следующей задачи к текущей (словарь WP2; здесь принимается как вход)
CONTINUE_RELATIONS = ("same_task", "continuation")
NEW_RELATIONS = ("new_independent_task", "new_product")


# Виды артефактов модуля. Объявлены КОНСТАНТАМИ, потому что по ним идёт разводка проверки:
# строковый литерал в двух местах — ровно то, чем ошибка «kind должен быть CompletionRitual»
# и жила.
RITUAL_KIND = "CompletionRitual"
RECOMMENDATION_KIND = "SessionRecommendation"
OUTCOMES = ("continue", "compact", "clear", "new_session", "defer")
# Исходы, которые велят человеку уйти отсюда: они обязаны нести точную команду.
COMMANDED_OUTCOMES = ("compact", "clear", "new_session")


def load_policy(child_root):
    """SessionEconomyPolicy из .ai-ops.yaml (session_economy), поверх дефолтов."""
    pol = dict(SESSION_ECONOMY_DEFAULTS)
    cfg_p = Path(child_root) / ".ai-ops.yaml"
    if yaml and cfg_p.is_file():
        try:
            se = (yaml.safe_load(cfg_p.read_text(encoding="utf-8")) or {}).get("session_economy") or {}
            for k in SESSION_ECONOMY_DEFAULTS:
                if k in se:
                    pol[k] = se[k]
        except (yaml.YAMLError, OSError):
            pass
    return pol


def classify_context(tokens, policy=None):
    """Порог гигиены контекста. None -> unknown (честно: без числа не классифицируем)."""
    p = policy or SESSION_ECONOMY_DEFAULTS
    if tokens is None:
        return "unknown"
    if tokens >= p["new_session_recommended_at"]:
        return "new_session_recommended"
    if tokens >= p["compact_recommended_at"]:
        return "compact_recommended"
    if tokens >= p["target_context_tokens"]:
        return "attention"
    return "normal"


def classify_session_spend(total_tokens, policy=None):
    """Потолок РАСХОДА СЕССИИ. None -> unknown (без числа не классифицируем, и это не «норма»).

    Пороги контекста отвечают на «сколько сессия читает СЕЙЧАС», этот — на «сколько она прочитала
    ВСЕГО». Разница не теоретическая: после компакции контекст падает до нормального, а уже
    оплаченное перечитывание никуда не девается.
    """
    p = policy or SESSION_ECONOMY_DEFAULTS
    budget = p.get("session_token_budget")
    if total_tokens is None or not budget:
        return "unknown"
    if total_tokens >= budget:
        return "over_budget"
    if total_tokens >= budget * 0.7:
        return "attention"
    return "normal"


def _tok(n):
    return "н/д" if n is None else (f"{n / 1000000:.1f}M" if n >= 1000000
                                    else f"{n / 1000:.0f}k" if n >= 1000 else str(n))


def _compact_cmd(wid):
    return (f"/compact Сохрани цель {wid or 'текущего WorkItem'}, принятые решения, изменённые файлы, "
            "результаты проверок, открытые блокеры и следующий безопасный шаг. Удали подробные логи, "
            "повторяющиеся исследования и промежуточные попытки.")


def _clear_cmds(wid, next_task):
    nxt = f'\nai-ops do "{next_task}"' if next_task else '\nai-ops do "<следующая задача>"'
    return f"/clear {wid or 'task'}-completed{nxt}"


def _new_session_cmds(repo_path, next_task):
    nxt = next_task or "<следующая задача>"
    return (f"/exit\n# в терминале:\ncd {repo_path or '<путь/к/репозиторию>'}\nclaude\n"
            f'# затем:\nai-ops do "{nxt}"')


def _handoff_note(repo_path, session_id):
    """ПРАВДА о сессионном handoff, а не заявление о нём. -> (текст, путь или None).

    ЗАМЕР 17.08.2026, живая сессия кита `88c802ae`: обе фразы про handoff в этом файле были
    БЕЗУСЛОВНЫМИ («Handoff/решения сохранены в репозитории», «handoff сохранён»), а сессионного
    handoff в ките не существовало ни одного. То есть рекомендация уйти в новую сессию успокаивала
    ровно там, где обязана предупредить: уходи, всё записано — при пустом месте записи.

    Отсутствие пути тоже названо словами: «проверить нечем» ≠ «не сохранён». Вызывающий, не
    передавший `repo_path`, не даёт повода утверждать ни то, ни другое.
    """
    if not repo_path:
        return "handoff сессии проверить нечем: путь репозитория не передан", None
    try:
        from ai_ops_kit.engops import session_handoff
        p = session_handoff.latest(repo_path, session_id=session_id)
    except Exception:  # noqa: BLE001 — не смогли посмотреть -> так и говорим, а не «сохранён»
        return "handoff сессии проверить не удалось — считать несохранённым", None
    if p:
        return f"handoff сессии сохранён: {p}", str(p)
    return ("handoff сессии НЕ сохранён — записать перед уходом, иначе контекст этой сессии "
            "потеряется вместе с ней"), None


def recommend(snapshot, policy=None, next_relation="new_independent_task",
              next_task=None, at_safe_boundary=True, task_done=True, repo_path=None):
    """4 исхода + defer. Приоритет: гигиена сессии/контекста > продолжение/новизна задачи.
    strong-advice: outcome + причина + ТОЧНАЯ команда; НЕ блокирует прогон движка."""
    p = policy or SESSION_ECONOMY_DEFAULTS
    ctx = snapshot.get("context_current")
    state = classify_context(ctx, p)
    spent = snapshot.get("session_total_tokens")
    spend_state = classify_session_spend(spent, p)
    wid = snapshot.get("workitem_id")
    ctx_txt = "н/д" if ctx is None else f"{ctx/1000:.0f}k [{snapshot.get('context_status')}]"
    spend_txt = ("н/д" if spent is None
                 else f"{_tok(spent)} из {_tok(p.get('session_token_budget'))} [{spend_state}]")
    # Каждый исход несёт оба числа: рекомендация, показывающая только контекст, скрывала бы ровно
    # тот случай, ради которого потолок сессии и появился (контекст после компакции нормальный).
    handoff_txt, handoff_path = _handoff_note(repo_path, snapshot.get("session_id"))
    base = {"kind": RECOMMENDATION_KIND, "schema_version": 1,
            "context_state": state, "context": ctx_txt,
            "spend_state": spend_state, "session_spend": spend_txt,
            "measurement": snapshot.get("context_source") or snapshot.get("context_status"),
            # Состояние handoff несут ВСЕ исходы, а не только те, что советуют уйти: на `continue`
            # оно тоже правда о сессии, и молчание о нём читалось бы как «всё записано».
            "handoff": handoff_txt, "handoff_path": handoff_path}

    def out(outcome, reason, command=None):
        return dict(base, outcome=outcome, reason=reason, command=command)

    if not at_safe_boundary:
        return out("defer",
                   "небезопасная граница (миграция/незавершённый commit) — не прерываем; "
                   "дождаться безопасной точки, затем перезапросить рекомендацию.")

    same = next_relation in CONTINUE_RELATIONS

    # слишком тяжёлая сессия -> новая сессия ВНЕ зависимости от новизны (гигиена важнее)
    if state == "new_session_recommended" or spend_state == "over_budget":
        why = (f"контекст {ctx_txt} превысил порог новой сессии" if state == "new_session_recommended"
               else f"сессия прочитала {spend_txt} — потолок расхода сессии исчерпан")
        if same and not task_done:
            return out("compact",
                       f"{why}, но WorkItem не завершён и его контекст нужен — сначала compact "
                       "на безопасной границе.",
                       _compact_cmd(wid))
        return out("new_session",
                   f"{why}; не начинать новый блок работы здесь. {handoff_txt}.",
                   _new_session_cmds(repo_path, next_task))

    if same:
        # продолжение того же WorkItem
        if not task_done and state in ("compact_recommended",):
            return out("compact",
                       f"тот же WorkItem не завершён, контекст {ctx_txt} уже дорогой, логическая "
                       "часть закрыта — compact и продолжить.",
                       _compact_cmd(wid))
        return out("continue",
                   f"следующий шаг — тот же WorkItem {wid or ''}, контекст {ctx_txt}; "
                   "собранные знания переиспользуются, повторное исследование не нужно.")

    # новая независимая задача / новый продукт
    if not task_done:
        return out("continue",
                   "текущий WorkItem ещё не завершён — сначала закрыть его, потом переключаться.")
    if p.get("one_task_per_session", True):
        return out("clear",
                   f"новая независимая задача (one-task-per-session); продолжение здесь заставит "
                   f"перечитывать нерелевантную историю ({ctx_txt}). Задача закрыта. {handoff_txt}.",
                   _clear_cmds(wid, next_task))
    return out("continue", "cross-task-сессии разрешены политикой (allow_cross_task_session=true).")


def completion_ritual(snapshot, policy=None, *, workitem_id=None, pr=None, checks=None,
                      next_relation="new_independent_task", next_task=None,
                      at_safe_boundary=True, decisions_recorded=True,
                      committed=True, repo_path=None):
    """WP5: обязательный ритуал завершения WorkItem -> отчёт + usage + SessionRecommendation + NextCommand.

    ПАРАМЕТР `handoff_saved` УБРАН 17.08.2026, и это не переименование. Он имел значение по
    умолчанию `True`, ни один из двух вызывающих (`engine/ai_ops_run.py`, `ai-ops session`) его не
    передавал, и пункт `handoff_created` означал ровно «автор функции написал True». Замер на живой
    сессии кита: `ai-ops session` печатал «Что сохранено: … handoff_created …» при полном отсутствии
    сессионного handoff в природе. Теперь пункт ВЫВОДИТСЯ из наличия файла и соврать им нельзя —
    вернуть параметр значит вернуть ложный пункт в ритуале, который сам же проверяет честность.
    """
    p = policy or SESSION_ECONOMY_DEFAULTS
    rec = recommend(snapshot, p, next_relation=next_relation, next_task=next_task,
                    at_safe_boundary=at_safe_boundary, task_done=True, repo_path=repo_path)
    checklist = {
        "result_achieved": True, "checks_passed": bool(checks),
        "state_saved": True, "handoff_created": rec.get("handoff_path") is not None,
        "decisions_recorded": decisions_recorded, "commit_or_pr": bool(pr) or committed,
        "usage_counted": snapshot.get("turns", 0) > 0,
    }
    return {
        "kind": RITUAL_KIND, "schema_version": 1,
        "workitem_id": workitem_id or snapshot.get("workitem_id"),
        "completion_report": {"pr": pr, "checks": checks},
        "usage_summary": {
            "input_tokens": snapshot.get("input_tokens"), "output_tokens": snapshot.get("output_tokens"),
            "estimated_cost": snapshot.get("estimated_cost"), "cost_complete": snapshot.get("cost_complete"),
            "context_current": snapshot.get("context_current"), "context_status": snapshot.get("context_status"),
            "turns": snapshot.get("turns"),
            "session_total_tokens": snapshot.get("session_total_tokens"),
            "session_tokens_status": snapshot.get("session_tokens_status"),
        },
        "session_recommendation": rec,
        "next_command": rec.get("command"),
        "completion_checklist": checklist,
        "complete": all(checklist.values()),
    }


def check_recommendation(rec):
    """Валидация SessionRecommendation — СОБСТВЕННАЯ, а не заимствованная у ритуала.

    ПОВОД (найдено лентой A 19.08.2026 на чистой установке). `./ai-ops session` печатал в stderr
    «kind должен быть CompletionRitual» при КАЖДОМ запуске: на рекомендации звался `check()`,
    который проверяет ДРУГОЙ артефакт. Работа `session-ritual-validators-are-dead` объявила
    «check() зовётся на каждом produced-артефакте» и для одного из двух артефактов позвала чужой —
    то есть проверка была, и она не проверяла ничего, кроме собственного несовпадения.

    Проверяется то, ради чего рекомендация существует: исход из закрытого набора, причина, по
    которой человек может решить, и ТОЧНАЯ команда там, где исход велит уйти. Рекомендация «уйди»
    без команды заставляет человека вспоминать синтаксис ровно в тот момент, когда у него кончился
    контекст.
    """
    if not isinstance(rec, dict):
        return [f"SessionRecommendation должен быть словарём, получен {type(rec).__name__}"]
    if rec.get("kind") != RECOMMENDATION_KIND:
        return [f"kind должен быть {RECOMMENDATION_KIND}, получен {rec.get('kind')!r}"]
    e = []
    outcome = rec.get("outcome")
    if outcome not in OUTCOMES:
        e.append(f"недопустимый outcome: {outcome!r} (допустимы {', '.join(sorted(OUTCOMES))})")
    if not str(rec.get("reason") or "").strip():
        e.append(f"исход {outcome!r} без причины — совет, который нельзя ни принять, ни отвергнуть")
    if outcome in COMMANDED_OUTCOMES and not rec.get("command"):
        e.append(f"исход {outcome} обязан нести точную команду (NextCommand)")
    # Оба числа — часть контракта: рекомендация, показывающая только контекст, скрывала бы ровно
    # тот случай, ради которого потолок сессии и появился (контекст после компакции нормальный).
    for field in ("context_state", "spend_state"):
        if not str(rec.get(field) or "").strip():
            e.append(f"нет поля {field}: рекомендация обязана называть оба состояния, а не одно")
    return e


def check_ritual(r):
    """Валидация ритуала: 4 допустимых исхода(+defer), рекомендация присутствует, команда есть кроме continue."""
    e = []
    if not isinstance(r, dict) or r.get("kind") != RITUAL_KIND:
        return [f"kind должен быть {RITUAL_KIND}"]
    rec = r.get("session_recommendation") or {}
    if rec.get("outcome") not in OUTCOMES:
        e.append(f"недопустимый outcome: {rec.get('outcome')}")
    if rec.get("outcome") in COMMANDED_OUTCOMES and not rec.get("command"):
        e.append(f"исход {rec.get('outcome')} обязан нести точную команду (NextCommand)")
    return e


def check(r):
    """Единый шов модуля: артефакт проверяется тем, что проверяет ЕГО, а не соседа.

    Раньше `check` знал один артефакт из двух, и вызов на втором давал вечную ошибку о kind.
    Разводка по `kind` — не удобство: она делает невозможным то же самое повторение, потому что
    неизвестный вид теперь называется неизвестным, а не выдаётся за нарушение формы ритуала.
    """
    kind = r.get("kind") if isinstance(r, dict) else None
    if kind == RECOMMENDATION_KIND:
        return check_recommendation(r)
    if kind == RITUAL_KIND:
        return check_ritual(r)
    return [f"неизвестный артефакт session_guardrails: kind={kind!r} "
            f"(ожидается {RITUAL_KIND} или {RECOMMENDATION_KIND})"]


def render_block(ritual):
    """Обязательный пользовательский блок (WP5)."""
    us, rec = ritual["usage_summary"], ritual["session_recommendation"]
    cost = us["estimated_cost"]
    cost_txt = ("н/д" if cost is None else
                f"${cost:.4f}" + ("" if us["cost_complete"] else " (НЕПОЛНАЯ)"))
    ctx = us["context_current"]
    ctx_txt = "н/д" if ctx is None else f"{ctx/1000:.0f}k [{us['context_status']}]"
    saved = [k for k, v in ritual["completion_checklist"].items() if v]
    L = [f"Задача завершена: {ritual['workitem_id'] or '—'}.", "",
         f"PR: {ritual['completion_report']['pr'] or '—'}",
         f"Проверки: {ritual['completion_report']['checks'] or '—'}",
         f"Стоимость: {cost_txt}",
         f"Контекст сессии: {ctx_txt}, ходов: {us['turns']}",
         f"Расход сессии всего: {rec.get('session_spend', 'н/д')}",
         f"Что сохранено: {', '.join(saved) or '—'}",
         # Состояние handoff названо ОТДЕЛЬНОЙ строкой, а не только галочкой в списке: галочку
         # читают как формальность, а потеря контекста сессии — это потеря труда.
         f"Handoff сессии: {rec.get('handoff') or 'н/д'}",
         f"Рекомендация: {rec['outcome'].upper()} — {rec['reason']}"]
    if rec.get("command"):
        L += ["", "Точная команда:", rec["command"]]
    if not ritual["complete"]:
        missing = [k for k, v in ritual["completion_checklist"].items() if not v]
        L += ["", f"⚠ ритуал НЕ завершён — не закрыто: {', '.join(missing)}"]
    return "\n".join(L)


def main(argv):
    wid = ctx = nrel = nxt = pr = checks = None
    args, it = [], iter(argv)
    unsafe = "--unsafe" in argv
    for a in it:
        if a == "--workitem":
            wid = next(it, None)
        elif a == "--context":
            v = next(it, None); ctx = int(v) if v and v.isdigit() else None
        elif a == "--next-relation":
            nrel = next(it, None)
        elif a == "--next":
            nxt = next(it, None)
        elif a == "--pr":
            pr = next(it, None)
        elif a == "--checks":
            checks = next(it, None)
        elif not a.startswith("--"):
            args.append(a)
    root = args[0] if args else "."
    pol = load_policy(root)
    snap = session_telemetry.snapshot(root, workitem_id=wid, context_current=ctx)
    # v3.17.0 WP2: если отношение не задано явно, но есть текст следующей задачи — классифицируем.
    if not nrel and nxt:
        try:
            from ai_ops_kit.engops import session_boundary
            nrel, _ = session_boundary.classify(current_workitem=wid, new_task=nxt)
        except Exception:  # noqa: BLE001
            nrel = None
    rit = completion_ritual(snap, pol, workitem_id=wid, pr=pr, checks=checks,
                            next_relation=nrel or "new_independent_task", next_task=nxt,
                            at_safe_boundary=not unsafe, repo_path=str(Path(root).resolve()))
    # ПРОВЕРКА НА СОБСТВЕННОМ ПУТИ, а не только в тестах. Работа `session-ritual-validators-are-dead`
    # объявила «check() зовётся на каждом produced-артефакте», и это была правда для тестов и
    # неправда для команды: `./ai-ops session` идёт СЮДА (через установщик), и здесь ритуал печатался
    # непроверенным. Ошибка — предупреждение, а не отказ: команда read-only, и владелец должен
    # увидеть проблему, а не потерять ответ.
    for err in check(rit) + check_recommendation(rit.get("session_recommendation")):
        print(f"session-check: {err}", file=sys.stderr)
    print(json.dumps(rit, ensure_ascii=False, indent=2) if "--json" in argv else render_block(rit))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
