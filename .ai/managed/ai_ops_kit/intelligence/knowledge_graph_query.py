#!/usr/bin/env python3
"""Вопросы к СОБРАННОМУ графу знаний: «зачем существует функция» и «что не покрыто результатом».

Сателлит фасада `knowledge_graph.py`. Там — СБОРКА графа из реальных источников (план, обучение,
паспорта функций, решения, история работ, вердикты ревью); здесь — чистые функции обхода над уже
собранным графом, без единого обращения к диску.

Разрез сделан по этой границе, когда фасад перерос потолок размера: сборка отвечает на вопрос «что
в графе есть», обход — «что из этого следует», и смешивать их в одном файле незачем.

ЧЕСТНОСТЬ здесь та же, что в сборке: чего в данных нет, то НАЗЫВАЕТСЯ пробелом, а не заменяется
догадкой. Цель, которой нет в плане, не швартует функцию; несколько целей над функцией не
разрешаются выбором первой; потерянное звено не пропадает молча.

Слой: `intelligence`. Наверх (`validation`) не импортирует, диска не касается.
"""
from __future__ import annotations

from ai_ops_kit.intelligence.knowledge_graph_util import _slug, _text


def _index(graph: dict):
    nodes = {n.get("id"): n for n in (graph.get("nodes") or [])
             if isinstance(n, dict) and n.get("id")}
    edges = [e for e in (graph.get("edges") or []) if isinstance(e, dict)]
    return nodes, edges


def _parents(edges, nodes, nid) -> list:
    """Родители узла по `contains` (только существующие узлы). Порядок рёбер сохраняется."""
    return [e["from"] for e in edges if e.get("type") == "contains"
            and e.get("to") == nid and e.get("from") in nodes]


def _path_down(nodes, edges, goal: str, fid: str):
    """Путь `goal -> … -> feature` по `contains`. -> (цепочка, найден ли путь на самом деле).

    Нужен только для ПОКАЗА цепочки: принадлежность функции цели уже установлена её паспортом.
    Пути в графе может не быть (объявленный эпик не стал связью — например, его имя занято другой
    сущностью). Тогда отдаётся прямая пара «цель → функция», и флаг говорит обходу, что промежуток
    показан НЕ по данным: молча выдавать домысел за путь нельзя.
    """
    queue: list = [[goal]]
    seen = {goal}
    while queue:
        path = queue.pop(0)
        if path[-1] == fid:
            return path, True
        for child in [e["to"] for e in edges if e.get("type") == "contains"
                      and e.get("from") == path[-1] and e.get("to") in nodes]:
            if child not in seen:
                seen.add(child)
                queue.append(path + [child])
    return [goal, fid], False


def _reachable_goals(nodes, edges, fid: str):
    """Цели, достижимые вверх по `contains`. -> ({цель: путь}, {заглушка: путь}, тупик).

    Считать неоднозначностью только НЕСКОЛЬКО ПРЯМЫХ родителей-целей было мало: эпик может
    принадлежать одной цели напрямую, а другой — через инициативу, и тогда прямых целей ровно одна,
    а целей над функцией всё равно две. Поэтому смотрим на всю достижимость вверх, а не на один
    уровень. `seen` по узлам достаточно: достижимость цели от пути не зависит, а циклы обрываются.

    Цель-ЗАГЛУШКА (`unresolved` — на неё сослались, а в плане её нет) идёт отдельным списком: считать
    её настоящей значило бы пришвартовать функцию к тому, чего не существует, и молча. Тупик —
    узел, у которого родителей НЕТ ВООБЩЕ (а не «все уже посещены»): иначе ответ зависел бы от
    порядка рёбер, то есть от того, чей паспорт прочитался первым.
    """
    found: dict = {}
    unresolved: dict = {}
    dead_end = [fid]
    queue: list = [[fid]]
    seen = {fid}
    while queue:
        path = queue.pop(0)
        head = path[0]
        if nodes.get(head, {}).get("type") == "goal":
            (unresolved if nodes[head].get("unresolved") else found)[head] = path
            continue                      # выше цели лестница не идёт
        if not _parents(edges, nodes, head) and len(path) > len(dead_end):
            dead_end = path               # самый длинный путь до узла, у которого родителей нет
        for p in [p for p in _parents(edges, nodes, head) if p not in seen]:
            seen.add(p)
            queue.append([p] + path)
    return found, unresolved, dead_end


def _climb_to_goal(nodes, edges, fid: str):
    """Подъём по `contains` до цели для функции, которая цель НЕ объявила. -> (goal, chain, gap).

    Принадлежность здесь УНАСЛЕДОВАНА от эпика/инициативы, а их отнесение к цели объявили паспорта
    соседних функций. Пока такая цель одна — это иерархия продукта. Если их несколько, молчаливый
    выбор был бы выдумкой (и зависел бы от порядка чтения каталогов): возвращаем НАЗВАННУЮ
    неоднозначность и никакой цели.
    """
    found, unresolved, dead_end = _reachable_goals(nodes, edges, fid)
    if len(found) > 1:
        return None, [fid], (f"функция отнесена сразу к нескольким целям "
                             f"({', '.join(sorted(found))}) — какая из них, по данным не решить; "
                             f"своей цели функция не объявила")
    if found:
        goal, path = next(iter(found.items()))
        return goal, path, None
    if unresolved:
        return None, [fid], (f"выше функции только цель «{sorted(unresolved)[0]}», которой нет в "
                             f"плане (planning/plan.yaml) — швартовать функцию не к чему")
    return None, dead_end, (f"выше «{dead_end[0]}» нет родителя — путь до цели (goal) неполон")


def trace(graph: dict, feature: str) -> dict:
    """«Зачем существует функция»: цепочка goal -> … -> feature -> outcome + вердикт + пробелы.

    Обобщение `validate_product_objects.trace_feature_rationale` до вопроса, задаваемого над
    СОБРАННЫМ графом: идёт вверх по `contains` до цели и вперёд по `targets` к outcome. Честная
    цепочка — если данных на полный путь нет, отдаётся лучший фрагмент с НАЗВАННЫМИ `gaps`.

    Результат: `{"feature", "chain": [{id,type,title}], "goal": id|None, "outcome": {...}|None,
    "decision": {id,title}|None, "built_by": [{id,title,pr}], "review": {...}|None, "verdict": str,
    "gaps": [...]}`.

    `decision` — из какого РЕШЕНИЯ (истории) появилась функция («зачем она есть»); `built_by` — какая
    РАБОТА/PR её построила («что построили и где»); `review` — КТО/ЧТО её проверил (персистентный
    вердикт: кем проверено + verified + ревизия). Все — словами человека, а не пересказом кода. Нет
    объявленной связи -> пусто (не пробел: связь просто не записана, а не потеряна).
    """
    nodes, edges = _index(graph)
    fid = _slug(feature)
    gaps: list[str] = []

    def summary(nid: str) -> dict:
        n = nodes.get(nid, {})
        return {"id": nid, "type": _text(n.get("type")) or "?",
                "title": _text(n.get("title")) or nid}

    if fid not in nodes:
        return {"feature": fid, "chain": [], "goal": None, "unresolved_goal": None,
                "outcome": None, "decision": None,
                "built_by": [], "review": None, "verdict": "unknown",
                "gaps": [f"узла «{fid}» нет в графе — цепочку строить не от чего"]}

    # Чья цель. Если паспорт функции цель ОБЪЯВИЛ, берём её с самой функции (`declared_goal`), а не
    # подъёмом по графу: эпик — общий узел, его родителя-цель объявляют паспорта соседних функций, и
    # подъём приписал бы функции чужую цель. Не объявил — поднимаемся по иерархии, как раньше.
    goal, unresolved_goal = None, None
    declared = _text(nodes[fid].get("declared_goal")) or None
    if declared:
        gnode = nodes.get(declared) or {}
        if gnode.get("type") == "goal" and not gnode.get("unresolved"):
            goal = declared
            chain_ids, path_found = _path_down(nodes, edges, declared, fid)
            if not path_found and not nodes[fid].get("broken_links"):
                # Причину «пути нет» называем ровно один раз: если уровень потерян из-за тёзки,
                # об этом скажет пробел ниже — два пункта про одно и то же человеку не помогают.
                gaps.append(f"между целью «{declared}» и функцией в графе пути нет — показана "
                            f"прямая связь, промежуточные уровни связью не стали")
        else:
            # Цель ОБЪЯВЛЕНА, но в плане её нет (или имя занято узлом другого типа). Считать такую
            # цель швартовкой значило бы выдать заглушку за историю: нить оборвана именно здесь, и
            # сказать это надо вслух. «Не сказал» и «не знаю» — разные состояния: автор сказал, а
            # названного не существует.
            unresolved_goal, chain_ids = declared, [fid]
            if declared in (nodes[fid].get("broken_links") or []):
                # Имя цели носит другая сущность графа (например, функция). Причина у потери одна,
                # и назвать её надо ТОЧНО: «цели нет в плане» тут соврало бы — цели нет не потому,
                # что её забыли завести, а потому, что имя уже занято.
                gaps.append(f"объявленная цель «{declared}» — имя другой сущности графа, а не цели "
                            f"плана: швартовать функцию не к чему")
            else:
                gaps.append(f"функция объявила цель «{declared}», которой нет в плане "
                            f"(planning/plan.yaml) — путь до цели оборван в данных")
    else:
        goal, chain_ids, climb_gap = _climb_to_goal(nodes, edges, fid)
        if climb_gap:
            gaps.append(climb_gap)
    # Уровень объявлен паспортом, но связью не стал: его имя занято другой сущностью графа. Для
    # ЦЕЛИ такой случай уже называется вслух (`unresolved`); молчать про эпик и инициативу значило
    # бы применять правило честности через раз — объявленное звено пропадало бы бесследно.
    for broken in (nodes[fid].get("broken_links") or []):
        if broken == unresolved_goal:
            continue                      # про это имя уже сказано выше, и сказано точнее
        gaps.append(f"объявленный уровень «{broken}» связью не стал: это имя в графе занято другой "
                    f"сущностью — звено истории потеряно")
    # Имя функции носит и работа плана. Работу в граф не заводим (иначе функция стала бы ею), но
    # человеку говорим: конфликт исправляется одним переименованием, если о нём знать.
    taken = nodes[fid].get("name_taken_in_plan")
    # Граф прежней версии нёс здесь булев флаг: печатать его как слово («носит True в плане») —
    # врать читателю опубликованного артефакта, у которого пересобрать граф может быть нечем.
    kind = "запись" if taken is True else _text(taken)
    if kind:
        dropped = nodes[fid].get("name_conflict_dropped") or 0
        cascade = (f", и вместе с ней за графом осталось ещё записей плана: {dropped}"
                   if dropped else "")
        gaps.append(f"это же имя носит {kind} в плане (planning/plan.yaml) — в нить вошла функция, "
                    f"запись плана осталась за графом{cascade}; переименуй одну из них")

    # Вперёд к outcome.
    outcome_ids = [e["to"] for e in edges if e.get("type") == "targets"
                   and e.get("from") == fid and e.get("to") in nodes]
    outcome = None
    if outcome_ids:
        onode = nodes[outcome_ids[0]]
        outcome = {"id": outcome_ids[0], "verdict": _text(onode.get("verdict")) or "pending",
                   "title": _text(onode.get("title")) or outcome_ids[0],
                   "measured_by": [e["to"] for e in edges if e.get("type") == "measured-by"
                                   and e.get("from") == outcome_ids[0]]}
        if not outcome["measured_by"]:
            gaps.append(f"у исхода «{outcome_ids[0]}» нет метрики (ребро measured-by) — "
                        f"измерить его нечем")
    else:
        gaps.append(f"у «{fid}» нет outcome (ребро targets) — зачем функция существует, "
                    f"не подтверждается измеримым результатом")

    # Решение, из которого функция появилась («зачем она есть» — из истории). Отсутствие решения
    # НЕ пробел: причина может быть просто не записана. Но если решение есть, история его называет.
    decision_ids = [e["from"] for e in edges if e.get("type") == "motivates"
                    and e.get("to") == fid and e.get("from") in nodes]
    decision = None
    if decision_ids:
        dn = nodes[decision_ids[0]]
        decision = {"id": decision_ids[0], "title": _text(dn.get("title")) or decision_ids[0]}

    # Работа/PR, построившая функцию («что построили» — из истории). Отсутствие НЕ пробел: история
    # могла просто не записать, кто построил. Но если работа объявлена, история её называет.
    built_by = []
    for wid in [e["from"] for e in edges if e.get("type") == "builds"
                and e.get("to") == fid and e.get("from") in nodes]:
        wn = nodes[wid]
        built_by.append({"id": wid, "title": _text(wn.get("title")) or wid,
                         "pr": _text(wn.get("pr")) or None})

    # Кто/что проверил функцию («проверено кем» — из ПЕРСИСТЕНТНОЙ записи, не из прогона). Отсутствие
    # НЕ пробел: проверки могло не быть, а её запись — не потеряна. Есть запись -> называем, кем.
    review = None
    review_ids = [e["from"] for e in edges if e.get("type") == "reviewed"
                  and e.get("to") == fid and e.get("from") in nodes]
    if review_ids:
        rn = nodes[review_ids[0]]
        review = {"id": review_ids[0], "title": _text(rn.get("title")) or review_ids[0],
                  "verified": bool(rn.get("verified")),
                  "reviewed_revision": _text(rn.get("reviewed_revision")) or None}

    verdict = _verdict(goal, outcome)
    return {"feature": fid, "chain": [summary(i) for i in chain_ids], "goal": goal,
            "unresolved_goal": unresolved_goal,
            "outcome": outcome, "decision": decision, "built_by": built_by, "review": review,
            "verdict": verdict, "gaps": gaps}


def _verdict(goal, outcome) -> str:
    """Короткий машиночитаемый вердикт цепочки функции."""
    if goal is None:
        return "unmoored"          # функция ни к какой цели не привязана
    if outcome is None:
        return "no-outcome"        # цель есть, измеримого результата — нет
    if not outcome.get("measured_by"):
        return "outcome-unmeasured"
    return {"met": "confirmed", "unmet": "refuted"}.get(outcome.get("verdict"), "pending")


def gaps(graph: dict) -> dict:
    """Что в графе НЕ покрыто измеримым результатом — три честных списка.

    * `outcomes_without_metric` — исходы, которые ничем не измеряются (нет `outcome -measured-by->
      metric`): цель объявлена, а сигнала нет;
    * `metrics_without_release` — метрики, которых не наблюдает ни один релиз (нет `release
      -observed-by-> metric`): считать вроде есть чем, а откуда придут данные — не сказано;
    * `features_without_outcome` — функции без ребра `targets`: строим, а зачем — не подтверждено.
    """
    nodes, edges = _index(graph)
    measured = {e["from"] for e in edges if e.get("type") == "measured-by"}
    observed = {e["to"] for e in edges if e.get("type") == "observed-by"}
    targeted = {e["from"] for e in edges if e.get("type") == "targets"}

    def title(nid):
        return _text(nodes.get(nid, {}).get("title")) or nid

    outcomes = [{"id": n["id"], "title": title(n["id"])} for n in nodes.values()
                if n.get("type") == "outcome" and n["id"] not in measured]
    metrics = [{"id": n["id"], "title": title(n["id"])} for n in nodes.values()
               if n.get("type") == "metric" and n["id"] not in observed]
    features = [{"id": n["id"], "title": title(n["id"])} for n in nodes.values()
                if n.get("type") == "feature" and n["id"] not in targeted]
    return {"outcomes_without_metric": outcomes,
            "metrics_without_release": metrics,
            "features_without_outcome": features}


# ── Десять вопросов жизни фичи ──────────────────────────────────────────────────────────────────────
# Приёмка направления `product-memory-org-intelligence` дословно: на ОДНОЙ реальной фиче кит отвечает
# на десять вопросов её жизни ИЗ СВЯЗАННЫХ УЛИК, а не генерацией. `trace` уже даёт цепочку/решение/
# работу/ревью/исход; здесь — ОДИН связный ответ «почему мы вообще сделали эту функцию и что с ней
# стало» как явные десять вопросов, у каждого — ответ с уликой ИЛИ честное «неизвестно» с указанием,
# какого звена истории не хватает. Часть ответов несёт граф (решение/цель/работа/ревью/исход); часть
# живёт в ПЕРВОИСТОЧНИКЕ (гипотеза, обоснование решения, аудитория, выводы, следующий шаг) — граф их
# намеренно НЕ несёт (ревью просило «не расти вширь»). Их читает из связанного артефакта фасад
# (`knowledge_graph.feature_life_source`, у него и так есть диск) и передаёт сюда как `source`; связь
# при этом всё равно доказана графом. Нет `source` -> эти пять честно «неизвестно»: обход диска не
# касается, и выдумывать поле из воздуха не будет.

# Порядок — часть контракта: дословный порядок десяти вопросов приёмки направления.
TEN_QUESTIONS: list[tuple[str, str]] = [
    ("why_arose", "Зачем эта функция возникла?"),
    ("problem", "Какую проблему она решает?"),
    ("audience", "Для кого она?"),
    ("hypothesis", "Какая была гипотеза?"),
    ("solution_rationale", "Почему выбрали именно такое решение?"),
    ("built", "Что построили и где?"),
    ("proven", "Что доказано и кто проверял?"),
    ("after_release", "Что произошло после релиза?"),
    ("learnings", "Чему научились?"),
    ("next", "Что делать дальше?"),
]


def _insight_for(nodes: dict, edges: list, fid: str) -> dict | None:
    """Узел урока, который ПИТАЕТ функцию (`insight -feeds-> feature`). Нет ребра -> None.

    Урок цепляется к функции только объявленной связью: совпадение имени в догадку не превращается.
    Несколько уроков -> первый по порядку рёбер (порядок фиксирован чтением каталога).
    """
    for e in edges:
        if e.get("type") == "feeds" and _slug(e.get("to")) == fid:
            n = nodes.get(_slug(e.get("from")))
            if isinstance(n, dict):
                return n
    return None


def _qa(qid: str, question: str, *, answer=None, evidence=None, unknown_reason=None) -> dict:
    """Один пункт из десяти. Ответ ЕСТЬ только если есть и текст ответа, и хотя бы одна улика."""
    answered = bool(answer) and bool(evidence)
    return {"id": qid, "question": question, "answered": answered,
            "answer": answer if answered else None,
            "evidence": list(evidence or []) if answered else [],
            "unknown_reason": None if answered
            else (unknown_reason or "в связанной истории нет улики")}


def _source_gap(insight: dict | None, source, what: str) -> str:
    """Причина «неизвестно» для вопроса, чей ответ живёт в первоисточнике (паспорт/FL).

    Три РАЗНЫЕ причины, потому что чинить их по-разному: урока/паспорта нет вовсе; связь есть, но
    первоисточник не прочитан (`source` не дан — граф один поле не несёт); прочитан, но поле пустое.
    """
    if what in ("гипотеза", "выводы", "обоснование решения", "следующий шаг") and insight is None:
        return f"к функции не привязан урок (ребро feeds) — {what} в связанной истории нет"
    if source is None:
        return (f"{what} несёт первоисточник (паспорт функции / FL-*), а не граф — дай корень "
                f"репозитория (child_root), чтобы прочитать {what} из связанной улики")
    return f"связь есть, но {what} в первоисточнике не заполнен(ы)"


_Q = dict(TEN_QUESTIONS)   # id -> текст вопроса, чтобы помощники не индексировали список по номеру


def _graph_bound(tr: dict, fid: str) -> dict:
    """Пять вопросов, ответ на которые несёт САМ ГРАФ: зачем возникла, проблема, построили, проверено.

    (Вопрос «что после релиза» дополняется числами в `_after_release` — тому нужен measured_outcome.)
    """
    out: dict = {}
    decision = tr.get("decision")
    out["why_arose"] = (_qa("why_arose", _Q["why_arose"],
                            answer=f"из решения «{decision.get('title')}»",
                            evidence=[decision.get("id"), f"{decision.get('id')} -motivates-> {fid}"])
                        if decision else
                        _qa("why_arose", _Q["why_arose"],
                            unknown_reason="нет решения (ребро motivates) — причина появления не "
                                           "записана; объяви её в blueprint links.decision"))
    goal, chain = tr.get("goal"), tr.get("chain") or []
    if goal:
        gtitle = next((c.get("title") for c in chain if c.get("id") == goal), goal)
        out["problem"] = _qa("problem", _Q["problem"], answer=f"служит цели «{gtitle}»",
                             evidence=[goal] + [c.get("id") for c in chain])
    else:
        out["problem"] = _qa("problem", _Q["problem"],
                             unknown_reason=(tr.get("gaps") or ["функция не привязана к цели"])[0])
    built_by = tr.get("built_by") or []
    if built_by:
        first = built_by[0]
        pr = f" (PR #{first['pr']})" if first.get("pr") else ""
        more = f" и ещё {len(built_by) - 1}" if len(built_by) > 1 else ""
        out["built"] = _qa("built", _Q["built"], answer=f"работой «{first.get('title')}»{pr}{more}",
                          evidence=[w.get("id") for w in built_by] + [f"{first.get('id')} -builds-> {fid}"])
    else:
        out["built"] = _qa("built", _Q["built"],
                          unknown_reason="нет работы (ребро builds) — кто и где построил функцию, "
                                         "история не записала; объяви в links.built_by")
    review = tr.get("review")
    if review:
        note = "" if review.get("verified") else " (держится на суждении, без машинной опоры)"
        rev = f", ревизия {review.get('reviewed_revision')}" if review.get("reviewed_revision") else ""
        out["proven"] = _qa("proven", _Q["proven"], answer=f"{review.get('title')}{note}{rev}",
                           evidence=[review.get("id"), f"{review.get('id')} -reviewed-> {fid}"])
    else:
        out["proven"] = _qa("proven", _Q["proven"],
                           unknown_reason="нет вердикта ревью (ребро reviewed) — проверка функции "
                                          "не записана как персистентная улика")
    return out


def _after_release(tr: dict, fid: str, measured_outcome: dict) -> dict:
    """Вопрос 8: реальный сдвиг метрики (measured_outcome) либо объявленный исход (targets)."""
    mo, outcome = measured_outcome or {}, tr.get("outcome")
    if mo.get("measured"):
        took = "цель взята" if mo.get("verdict") == "met" else "цель пока не взята"
        return _qa("after_release", _Q["after_release"],
                   answer=f"было {mo.get('baseline')} → стало {mo.get('value')} при цели "
                          f"{mo.get('target')} — {took}",
                   evidence=[outcome.get("id") if outcome else fid, "measured_outcome"])
    if outcome:
        return _qa("after_release", _Q["after_release"],
                   answer=f"исход «{outcome.get('title')}» объявлен, вердикт {outcome.get('verdict')} "
                          f"(реальный замер ещё не накоплен)",
                   evidence=[outcome.get("id"), f"{fid} -targets-> {outcome.get('id')}"])
    return _qa("after_release", _Q["after_release"],
               unknown_reason="нет исхода (ребро targets) — что дал релиз, измеримым результатом "
                              "не подтверждено")


def _source_bound(c: dict, next_action: dict) -> dict:
    """Пять вопросов, чей ответ живёт в ПЕРВОИСТОЧНИКЕ: аудитория, гипотеза, решение, выводы, дальше.

    `c` — контекст (`fid/nodes/src/source/insight/iid/fl_edge/fl_ref`). Нет поля -> честный пробел с
    указанием, какого звена/поля не хватает, а не выдуманный ответ.
    """
    fid, src, source = c["fid"], c["src"], c["source"]
    insight, ev = c["insight"], [c["iid"], c["fl_edge"], c["fl_ref"]]
    out: dict = {}
    audience = _text(src.get("audience"))
    out["audience"] = (_qa("audience", _Q["audience"], answer=audience,
                           evidence=[fid, _text(c["nodes"][fid].get("blueprint"))])
                       if audience else
                       _qa("audience", _Q["audience"],
                           unknown_reason=(_source_gap(insight, source, "аудитория") if source is None
                                           else "аудитория не объявлена в blueprint "
                                                "(feature.audience) — для кого функция, история не знает")))
    hypothesis = _text(src.get("hypothesis"))
    out["hypothesis"] = (_qa("hypothesis", _Q["hypothesis"], answer=hypothesis, evidence=ev)
                         if hypothesis else
                         _qa("hypothesis", _Q["hypothesis"],
                             unknown_reason=_source_gap(insight, source, "гипотеза")))
    rationale = _text(src.get("solution_rationale"))
    if rationale:
        opt, did = _text(src.get("solution_chosen")), _text(src.get("solution_rationale_decision"))
        # Обоснование из решения, а не из урока -> улика указывает на решение и его ребро.
        why_ev = [did, f"{did} -motivates-> {fid}", "decisions/registry.yaml"] if did else ev
        out["solution_rationale"] = _qa("solution_rationale", _Q["solution_rationale"],
                                        answer=(f"выбрано «{opt}»: {rationale}" if opt else rationale),
                                        evidence=why_ev)
    else:
        out["solution_rationale"] = _qa("solution_rationale", _Q["solution_rationale"],
                                        unknown_reason=_source_gap(insight, source, "обоснование решения"))
    learnings = [x for x in (src.get("learnings") or []) if _text(x)]
    if learnings:
        more = f" и ещё {len(learnings) - 1}" if len(learnings) > 1 else ""
        out["learnings"] = _qa("learnings", _Q["learnings"], answer=f"{_text(learnings[0])}{more}",
                              evidence=ev)
    else:
        out["learnings"] = _qa("learnings", _Q["learnings"],
                              unknown_reason=_source_gap(insight, source, "выводы"))
    out["next"] = _next_step(c, next_action)
    return out


def _next_step(c: dict, next_action: dict) -> dict:
    """Вопрос 10: следующий шаг по уроку (next_action из петли) либо follow_up из связанного урока."""
    na, src = next_action or {}, c["src"]
    follow_up = [x for x in (src.get("follow_up") or []) if _text(x)]
    if na.get("action"):
        because = f" — потому что {na.get('because')}" if na.get("because") else ""
        return _qa("next", _Q["next"], answer=f"{na.get('action')}{because}",
                   evidence=[c["iid"] or c["fid"], "next_action"])
    if follow_up:
        more = f" и ещё {len(follow_up) - 1}" if len(follow_up) > 1 else ""
        return _qa("next", _Q["next"], answer=f"{_text(follow_up[0])}{more}",
                   evidence=[c["iid"], c["fl_edge"], c["fl_ref"]])
    return _qa("next", _Q["next"], unknown_reason=_source_gap(c["insight"], c["source"], "следующий шаг"))


def answer_ten_questions(graph: dict, feature: str, *,
                         source: dict | None = None,
                         trace_result: dict | None = None,
                         measured_outcome: dict | None = None,
                         next_action: dict | None = None) -> dict:
    """Ответить на десять вопросов жизни фичи из связанной истории.

    `graph` — собранный Knowledge Graph. `source` — поля из СВЯЗАННЫХ первоисточников (аудитория,
    гипотеза, обоснование решения, выводы, следующий шаг), прочитанные фасадом
    (`knowledge_graph.feature_life_source`); без него эти вопросы честно «неизвестно». `trace_result`
    при наличии берётся готовым. `measured_outcome` / `next_action` — реальный сдвиг метрики и шаг по
    уроку (считает слой CLI из контракта фичи); переданы -> обогащают вопросы 8 и 10.

    -> `{"feature", "questions": [<десять В ПОРЯДКЕ приёмки>], "answered_count", "total", "complete",
    "verdict"}`.
    """
    fid = _slug(feature)
    nodes, edges = _index(graph)
    tr = trace_result if trace_result is not None else trace(graph, feature)

    if tr.get("verdict") == "unknown" or fid not in nodes:
        reason = (f"узла «{fid}» нет в графе — историю фичи строить не из чего "
                  f"(собери граф: ./ai-ops graph build)")
        qs = [_qa(qid, text, unknown_reason=reason) for qid, text in TEN_QUESTIONS]
        return {"feature": fid, "questions": qs, "answered_count": 0, "total": len(TEN_QUESTIONS),
                "complete": False, "verdict": "unknown"}

    insight = _insight_for(nodes, edges, fid)
    iid = insight.get("id") if insight else None
    c = {"fid": fid, "nodes": nodes, "src": source or {}, "source": source, "insight": insight,
         "iid": iid, "fl_edge": f"{iid} -feeds-> {fid}" if iid else None,
         "fl_ref": _text(insight.get("ref")) if insight else None}

    merged = {**_graph_bound(tr, fid), **_source_bound(c, next_action)}
    merged["after_release"] = _after_release(tr, fid, measured_outcome)

    # Порядок ответов — дословный порядок приёмки (TEN_QUESTIONS), не порядок сборки помощников.
    a = [merged[qid] for qid, _ in TEN_QUESTIONS]
    answered = sum(1 for q in a if q["answered"])
    return {"feature": fid, "questions": a, "answered_count": answered,
            "total": len(TEN_QUESTIONS), "complete": answered == len(TEN_QUESTIONS),
            "verdict": tr.get("verdict")}
