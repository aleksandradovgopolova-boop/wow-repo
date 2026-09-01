#!/usr/bin/env python3
"""short_path.py — уже описанная работа идёт КОРОТКИМ путём: исполнение + след, а не описание заново.

ПОВОД — ЗАМЕР ПОЛЯ (ИИ-Среда, ~/ii-sreda, 15.08.2026). Из ПЯТИ параллельных сессий в одном
репозитории ДВЕ сожгли по 200+ тысяч токенов на `onboard -> specify -> plan` и встали, не написав ни
строки кода и не открыв PR. Задача при этом была специфицирована ДО начала. Наблюдение той сессии
дословно: «когда работа описана, кит нужен для трассируемости, а не как первый шаг».

РЕШЕНИЕ ВЛАДЕЛЬЦА 2026-08-17 (плановая работа `kit-as-first-step-or-as-trace`, поле `human_decision`
снято этим решением) — три ответа, и здесь реализованы ровно они:
  (а) РЕШАЕТ КИТ, а не человек: распознал признаки — идёт коротким путём сам, не переспрашивая;
  (б) ПРИЗНАКИ = заявление владельца И проверенный киту минимум. Одного заявления НЕ хватает;
  (в) потолок траты на процессные шаги до первой правки кода — 50 тысяч токенов (см.
      `engops/process_spend.py`, это отдельный механизм: он ловит залипание, этот — устраняет повод).

МИНИМУМ — ТРИ ВЕЩИ, И КАЖДАЯ ПРОВЕРЯЕТСЯ, А НЕ ОБЪЯВЛЯЕТСЯ:
  goal              — чего добиваемся;
  acceptance_criteria — как поймём, что готово. Засчитывается не статусом `complete`, а РАЗБОРОМ
                      в список критериев (`acceptance_verify.parse_criteria`): `complete` в спеке
                      означает «раздел заполнен», а не «критерии есть» — этой разницей уже был
                      оплачен единственный ложный green квалификации (B2-14, 14.08.2026);
  affected_paths    — где править (`write_scope` работы плана, `affected_files` спеки, сигналы).

ЧЕГО ЭТОТ МОДУЛЬ НЕ ДЕЛАЕТ — И ЭТО ГЛАВНАЯ ГРАНИЦА. Он НЕ понижает уровень спецификации и не
объявляет незаполненные разделы неприменимыми. Уровень остаётся расчётным (инвариант `spec_levels`:
понизить молча нельзя), а разделы, которые короткий путь не требует, получают статус `declined` с
ОБЯЗАТЕЛЬНЫМ note — со ссылкой на это решение владельца. Поэтому в спеке видно ровно то, что
произошло: три раздела заполнены и проверены, остальные сознательно отклонены и сказано кем и когда.
Тихого «not_applicable» здесь нет: неприменимость — утверждение о задаче, отклонение — решение
владельца, и путать их нельзя.

ПОЧЕМУ СЛЕД ОБЯЗАТЕЛЕН. Короткий путь — это не «кит выключен». Ценность, за которой владелец сюда
и шла, — трассируемость: `features/<wid>/short-path.yaml` фиксирует, ПОЧЕМУ полное описание не
требовалось (заявление + чем подтверждён каждый пункт минимума + версия кита + время), а спека
остаётся настоящей спекой. Без этой записи короткий путь превратился бы в необъяснимый пропуск
проверок, и следующий человек не смог бы отличить его от дефекта.

ПОЧЕМУ МОДУЛЬ ЛЕЖИТ В `planning`, А НЕ В `lifecycle`. Он отвечает на вопрос «нужен ли процесс этой
работе» — то есть на вопрос планирования, и читает он ровно то, где работа объявлена: план поставки,
спеку, сигналы. Проверка направлений зависимостей это подтвердила числом: из `lifecycle` он замыкал
новую взаимную связь `lifecycle <-> planning` (потолок ратчета 7 пар, стало бы 8), из `planning` —
ни одной новой (`planning` не импортирует никто из ядра, точка входа одна, CLI).

CLI:  short_path.py assess <child_root> <wid> "текст задачи" [--signals '{...}'] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from ai_ops_kit.shared import _bootstrap  # noqa: E402,F401

# Минимум, без которого короткого пути нет. Порядок — порядок вопросов человека к работе.
MINIMUM = ("goal", "acceptance_criteria", "affected_paths")

HUMAN_NAME = {
    "goal": "чего добиваемся",
    "acceptance_criteria": "как поймём, что готово",
    "affected_paths": "где править",
}

# Разделы спеки, которыми закрываются пункты минимума (affected_paths — ещё и `write_scope` плана).
_SPEC_SECTION = {"goal": "goal", "acceptance_criteria": "acceptance_criteria",
                 "affected_paths": "affected_files"}

# ЗАЯВЛЕНИЕ ВЛАДЕЛЬЦА. Список намеренно короткий и буквальный: широкий список превратил бы любую
# уверенную формулировку задачи в заявление «это уже описано», а цена ошибки здесь — пропущенное
# описание там, где оно было нужно. Сигнал `already_described` — тот же ответ, только машинный.
_DECLARATION_SIGNAL = "already_described"
_DECLARATION_PHRASES = (
    r"уже\s+опис", r"уже\s+специфиц", r"описан[оа]?\s+заранее", r"спек[аи]\s+готов",
    r"т[зс]\s+готов", r"по\s+готов(ому|ой)\s+(описанию|спеке|спецификации)",
    r"дела[йи]\s+по\s+описанию", r"описание\s+есть", r"не\s+надо\s+(ничего\s+)?описывать",
)
_DECLARATION_RE = re.compile("|".join(_DECLARATION_PHRASES), re.IGNORECASE)

DECISION_REF = "решение владельца 2026-08-17 (короткий путь для уже описанной работы)"
DECLINE_NOTE = ("declined коротким путём: работа описана вне кита, минимум (цель, критерии "
                "готовности, где править) проверен — " + DECISION_REF)

# Шаги, которые короткий путь снимает. `run` в списке НЕТ: исполнение никуда не девается.
SKIPPED_STEPS = ("discuss", "specify", "plan")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def declaration(task, signals=None):
    """Заявил ли владелец, что работа уже описана. -> {declared, source, evidence}.

    Сигнал сильнее фразы: он приходит от того, кто вызывает кит программно, и не зависит от
    формулировки. Фраза — путь человека, пишущего задачу словами.
    """
    signals = dict(signals or {})
    if signals.get(_DECLARATION_SIGNAL) is True:
        return {"declared": True, "source": "signal", "evidence": f"{_DECLARATION_SIGNAL}=true"}
    m = _DECLARATION_RE.search(str(task or ""))
    if m:
        return {"declared": True, "source": "phrase", "evidence": m.group(0)}
    return {"declared": False, "source": None, "evidence": None}


def _spec_sections(child_root, wid):
    """(карта разделов спеки, проблема). Проблема — это «не знаю», а не «разделов нет»."""
    from ai_ops_kit.gates import spec_levels
    sp = spec_levels._spec_path(Path(child_root), wid)
    if not sp.is_file():
        return {}, None
    try:
        import yaml
        doc = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001 — спека есть, но не прочитана: это «не знаю» с причиной
        return {}, f"spec.yaml не разобран ({type(e).__name__}: {e})"[:200]
    sections = doc.get("sections")
    if sections is not None and not isinstance(sections, dict):
        return {}, f"sections в spec.yaml имеет форму {type(sections).__name__}, ожидался мэппинг"
    return dict(sections or {}), None


def _section_content(sections, sid):
    """Текст раздела спеки, если он заполнен осознанно. -> (текст, проблема).

    Разбор формы — общий с модулем сверки критериев: раздел в ките бывает и строкой, и мэппингом, и
    списком, и своей формы здесь быть не должно (двух разборов одного файла кит уже платил).
    """
    from ai_ops_kit.engine import acceptance_verify
    entry = sections.get(sid)
    if isinstance(entry, dict) and entry.get("status") in ("missing", "declined", "not_applicable"):
        return "", None
    text, problem = acceptance_verify._section_text(entry)
    return text, problem


def _plan_item(child_root, wid):
    """Работа плана с этим id или None. План — законный источник описания: в нём объявлены и цель,
    и области записи, и именно им пользуются продуктовые репозитории вместо спеки кита."""
    try:
        from ai_ops_kit.planning import delivery_plan
        plan = delivery_plan.load(Path(child_root))
    except Exception:  # noqa: BLE001 — нет плана/битый план: источника просто нет, это не ошибка здесь
        return None
    for w in (delivery_plan.items(plan) or []):
        if str(w.get("id")) == str(wid):
            return w
    return None


def minimum(child_root, wid, signals=None, task=None):
    """Проверить три пункта минимума по РЕАЛЬНЫМ источникам. -> {ключ: {present, source, detail}}.

    `present=None` — «не знаю» (источник есть, но не читается). Это НЕ «нет»: короткого пути не
    будет в обоих случаях, но сказать человеку надо разное.
    """
    signals = dict(signals or {})
    sections, spec_problem = _spec_sections(child_root, wid)
    item = _plan_item(child_root, wid)
    out = {}

    def _res(present, source, detail):
        return {"present": present, "source": source, "detail": detail}

    # 1) ЦЕЛЬ. Сигнал -> раздел спеки -> заголовок работы плана.
    if str(signals.get("goal") or "").strip():
        out["goal"] = _res(True, "signals", "goal передан сигналом")
    else:
        text, problem = _section_content(sections, _SPEC_SECTION["goal"])
        if problem:
            out["goal"] = _res(None, "spec", problem)
        elif text.strip():
            out["goal"] = _res(True, "spec", f"features/{wid}/spec.yaml -> goal")
        elif item and str(item.get("title") or "").strip():
            out["goal"] = _res(True, "plan", f"работа плана {wid}: {str(item['title'])[:80]}")
        elif spec_problem:
            out["goal"] = _res(None, "spec", spec_problem)
        else:
            out["goal"] = _res(False, None, "цель не названа ни сигналом, ни спекой, ни планом")

    # 2) КРИТЕРИИ ГОТОВНОСТИ. Засчитываются только РАЗОБРАННЫМИ в список — см. шапку модуля.
    from ai_ops_kit.engine import acceptance_verify
    sig_crit = signals.get("acceptance_criteria")
    parsed_sig = acceptance_verify.parse_criteria(sig_crit) if sig_crit else []
    if parsed_sig:
        out["acceptance_criteria"] = _res(True, "signals",
                                          f"критериев в сигналах: {len(parsed_sig)}")
    else:
        text, criteria, problem = acceptance_verify.criteria_from_spec(Path(child_root), wid)
        if problem:
            out["acceptance_criteria"] = _res(None, "spec", problem)
        elif criteria:
            out["acceptance_criteria"] = _res(True, "spec",
                                              f"критериев в спеке: {len(criteria)}")
        else:
            out["acceptance_criteria"] = _res(
                False, None, "критериев готовности нет: раздел пуст или не разбирается в пункты")

    # 3) ГДЕ ПРАВИТЬ. Сигнал -> `write_scope` работы плана -> раздел спеки.
    sig_paths = signals.get("write_scope") or signals.get("affected_files")
    if isinstance(sig_paths, str):
        sig_paths = [p for p in re.split(r"[,\s]+", sig_paths) if p]
    if sig_paths:
        out["affected_paths"] = _res(True, "signals", f"путей в сигналах: {len(list(sig_paths))}")
    elif item and (item.get("write_scope") or []):
        out["affected_paths"] = _res(True, "plan",
                                     "write_scope работы плана: "
                                     + ", ".join(map(str, item["write_scope"]))[:120])
    else:
        text, problem = _section_content(sections, _SPEC_SECTION["affected_paths"])
        if problem:
            out["affected_paths"] = _res(None, "spec", problem)
        elif text.strip():
            out["affected_paths"] = _res(True, "spec", f"features/{wid}/spec.yaml -> affected_files")
        elif spec_problem:
            out["affected_paths"] = _res(None, "spec", spec_problem)
        else:
            out["affected_paths"] = _res(False, None,
                                         "не сказано, где править: ни write_scope, ни affected_files")
    return out


def assess(task, signals, child_root, wid):
    """Решение о коротком пути. -> ShortPathDecision. Детерминированно, без модели.

    Короткий путь = заявление владельца И весь минимум подтверждён. Нет заявления — обычный путь и
    ни слова о пропуске: кит не предлагает себя выключить. Заявление есть, минимума нет — говорим
    ЧЕГО не хватает, потому что это единственное, что человеку нужно, чтобы получить короткий путь.
    """
    decl = declaration(task, signals)
    mins = minimum(child_root, wid, signals=signals, task=task)
    missing = [k for k in MINIMUM if mins[k]["present"] is False]
    unknown = [k for k in MINIMUM if mins[k]["present"] is None]
    short = bool(decl["declared"]) and not missing and not unknown

    if not decl["declared"]:
        reason = "владелец не заявлял, что работа уже описана — идём обычным путём"
    elif short:
        reason = ("работа описана: заявлено владельцем и минимум подтверждён источниками "
                  + "; ".join(f"{HUMAN_NAME[k]} — {mins[k]['detail']}" for k in MINIMUM))
    elif unknown:
        reason = ("заявлено, что работа описана, но проверить не могу: "
                  + "; ".join(f"{HUMAN_NAME[k]} — {mins[k]['detail']}" for k in unknown))
    else:
        reason = ("заявлено, что работа описана, но не хватает: "
                  + ", ".join(HUMAN_NAME[k] for k in missing))

    return {
        "schema_version": 1, "kind": "ShortPathDecision",
        # ЯРЛЫКИ ЕДУТ В ОТЧЁТЕ, А НЕ БЕРУТСЯ ИЗ ЭТОГО МОДУЛЯ ЧИТАТЕЛЕМ. Слой человеческого языка
        # (`ui/presenter`) лежит НИЖЕ ядра и импортировать его нельзя — проверка направлений
        # зависимостей поймала это первой же прогонкой. Отчёт, несущий свои названия, — не уступка
        # проверке: он остаётся понятным и в JSON, у которого никакого presenter'а нет.
        "human_names": {k: HUMAN_NAME[k] for k in MINIMUM},
        "missing_names": [HUMAN_NAME[k] for k in missing],
        "unknown_names": [HUMAN_NAME[k] for k in unknown],
        "workitem_id": str(wid), "short_path": short,
        "declared": decl["declared"], "declared_by": decl["source"],
        "declaration_evidence": decl["evidence"],
        "minimum": mins,
        "missing": missing, "unknown": unknown,
        "skipped_steps": list(SKIPPED_STEPS) if short else [],
        "decision_ref": DECISION_REF,
        "reason": reason,
    }


def record_path(child_root, wid):
    return Path(child_root) / "features" / str(wid) / "short-path.yaml"


def trace(child_root, wid, signals, decision):
    """СЛЕД короткого пути: настоящая спека + запись о решении. -> {spec, record, filled, declined}.

    Порядок важен. Сначала спека создаётся как обычно (её уровень считает `spec_levels` — короткий
    путь уровень НЕ трогает), потом три проверенных раздела заполняются содержимым ИЗ НАЙДЕННОГО
    источника, а остальные обязательные разделы отклоняются с note. После этого гейт полноты спеки
    проходит по-настоящему: блокирующих `missing` нет, и ни один раздел не выдан за заполненный.
    """
    import yaml
    from ai_ops_kit.gates import spec_levels
    child_root, wid = Path(child_root), str(wid)
    sp, created, add_rep = spec_levels.create_spec(child_root, wid, signals)
    try:
        doc = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001 — битую спеку не перезаписываем: чужая работа дороже гейта
        return {"spec": sp, "record": None, "filled": [], "declined": [],
                "error": f"spec.yaml не разобран ({type(e).__name__}: {e})"[:200]}
    sections = doc.get("sections")
    if not isinstance(sections, dict):
        return {"spec": sp, "record": None, "filled": [], "declined": [],
                "error": "spec.yaml не содержит карты разделов (sections)"}

    filled, declined = [], []
    for key in MINIMUM:
        sid = _SPEC_SECTION[key]
        info = decision["minimum"][key]
        if sid not in sections or info["present"] is not True:
            continue
        entry = sections.get(sid)
        already = isinstance(entry, dict) and entry.get("status") == "complete"
        if already:
            continue
        sections[sid] = {"status": "complete",
                         "content": _content_for(key, info, signals, child_root, wid),
                         "note": f"источник: {info['detail']} ({DECISION_REF})"}
        filled.append(sid)

    for sid, entry in sections.items():
        status = entry.get("status") if isinstance(entry, dict) else "complete"
        if status == "missing":
            sections[sid] = {"status": "declined",
                             "content": (entry.get("content") if isinstance(entry, dict) else "") or "",
                             "note": DECLINE_NOTE}
            declined.append(sid)

    doc["sections"] = sections
    sp.write_text(spec_levels._render_spec(doc), encoding="utf-8")

    rec = {
        "schema_version": 1, "kind": "ShortPathRecord", "workitem_id": wid,
        "at": _now(), "kit_version": _kit_version(),
        "decision_ref": DECISION_REF,
        "declared_by": decision["declared_by"],
        "declaration_evidence": decision["declaration_evidence"],
        "minimum_confirmed": {k: decision["minimum"][k]["detail"] for k in MINIMUM},
        "skipped_steps": decision["skipped_steps"],
        "spec": str(sp.relative_to(child_root)) if sp.is_relative_to(child_root) else str(sp),
        "sections_filled_from_sources": filled,
        "sections_declined": declined,
        "spec_created": created,
        "spec_sections_added": add_rep.get("added") or [],
    }
    rp = record_path(child_root, wid)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(yaml.safe_dump(rec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return {"spec": sp, "record": rp, "filled": filled, "declined": declined, "error": None}


def _content_for(key, info, signals, child_root, wid):
    """Содержимое раздела для следа: берём ИЗ источника, а не пересказываем.

    Пересказ здесь был бы худшим из возможных: спека начала бы расходиться с тем описанием, по
    которому работа реально делается, и сверка критериев с результатом сверяла бы копию.
    """
    signals = dict(signals or {})
    if info["source"] == "signals":
        val = (signals.get("goal") if key == "goal"
               else signals.get("acceptance_criteria") if key == "acceptance_criteria"
               else signals.get("write_scope") or signals.get("affected_files"))
        if isinstance(val, (list, tuple)):
            return "\n".join(f"- {v}" for v in val)
        return str(val or "").strip()
    if info["source"] == "plan":
        item = _plan_item(child_root, wid) or {}
        if key == "goal":
            return str(item.get("title") or "").strip()
        return "\n".join(f"- {p}" for p in (item.get("write_scope") or []))
    # source == "spec" — содержимое уже в спеке, переписывать нечего
    return ""


def _kit_version():
    try:
        from ai_ops_kit.shared._bootstrap import PKG
        return (PKG / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001 — версия для следа полезна, но её отсутствие след не отменяет
        return None


def render(d):
    """Человеческая строка для внутреннего вывода (наружу говорит presenter)."""
    L = [f"SHORT-PATH {d['workitem_id']}: {'ДА' if d['short_path'] else 'нет'} — {d['reason']}"]
    for k in MINIMUM:
        info = d["minimum"][k]
        mark = "✓" if info["present"] is True else ("?" if info["present"] is None else "✗")
        L.append(f"  {mark} {HUMAN_NAME[k]}: {info['detail']}")
    if d["short_path"]:
        L.append(f"  пропускаю шаги: {', '.join(d['skipped_steps'])} · {d['decision_ref']}")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="short_path.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("assess", help="описана ли работа и нужен ли короткий путь")
    a1.add_argument("child_root")
    a1.add_argument("wid")
    a1.add_argument("task", nargs="?", default="")
    a1.add_argument("--signals", default="{}")
    a1.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    d = assess(a.task, json.loads(a.signals), Path(a.child_root), a.wid)
    print(json.dumps(d, ensure_ascii=False, indent=2) if a.json else render(d))
    return 0 if d["short_path"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
