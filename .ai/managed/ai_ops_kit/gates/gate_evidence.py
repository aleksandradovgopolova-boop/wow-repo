#!/usr/bin/env python3
"""Gate evidence — классификация гейта + сбор/парсинг evidence (сателлит gate_executor).

Фундамент контура гейтов: не импортирует ни фасад `gate_executor`, ни раннеры `gate_runners`.
Здесь живёт всё, что отвечает на два вопроса — «каким способом проверяется этот гейт»
(`classify`/`closed_by`/`evidence_source`) и «что судья/валидатор на самом деле сказал»
(`extract_reviewer_json`, `evidence_from_*`, `collect_evidence`, `validate_evidence`). Фасад и
раннеры зовут это ВНИЗ, обратного ребра нет.

Требует pyyaml.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
# `pending_human`/`human_handoff` — часть формы evidence, а не приписка сбоку: гейт, который ждёт
# ЧЕЛОВЕКА, отличается от гейта, который нашёл дефект. Без объявления здесь такой признак в
# загруженном evidence считался бы «неизвестным полем».
_EVIDENCE_KEYS = {"status", "source", "provided", "checks", "evidence", "warnings", "blockers",
                  "override", "pending_human", "human_handoff"}
# веха 4.2 (#588): допустимые значения самодекларации источника доказательства.
_EVIDENCE_SOURCE_KINDS = ("deterministic", "ai_judgment", "human")


def validate_evidence(evidence) -> list:
    """Мини-валидация формы evidence по schemas/gate-evidence.schema.json (stdlib, без jsonschema).
    Возвращает список ошибок (пустой = валидно)."""
    errs = []
    if not isinstance(evidence, dict):
        return ["evidence: верхний уровень должен быть объектом {gate_id: {...}}"]
    for gid, e in evidence.items():
        if not isinstance(e, dict):
            errs.append(f"{gid}: значение должно быть объектом"); continue
        if e.get("status") not in ("pass", "warn", "fail"):
            errs.append(f"{gid}.status: '{e.get('status')}' вне [pass, warn, fail]")
        if "source" in e and e["source"] not in _EVIDENCE_SOURCE_KINDS:
            errs.append(f"{gid}.source: '{e['source']}' вне {list(_EVIDENCE_SOURCE_KINDS)}")
        for k in ("provided", "evidence", "warnings", "blockers"):
            if k in e and not (isinstance(e[k], list) and all(isinstance(x, str) for x in e[k])):
                errs.append(f"{gid}.{k}: должен быть списком строк")
        if "checks" in e:
            if not isinstance(e["checks"], list):
                errs.append(f"{gid}.checks: должен быть списком")
            else:
                for c in e["checks"]:
                    if not (isinstance(c, dict) and isinstance(c.get("id"), str)
                            and c.get("status") in ("pass", "warn", "fail")):
                        errs.append(f"{gid}.checks: элемент требует id:str + status∈[pass,warn,fail]")
        ov = e.get("override")
        if ov is not None and not (isinstance(ov, dict) and isinstance(ov.get("by"), str)
                                   and isinstance(ov.get("reason"), str)):
            errs.append(f"{gid}.override: требует by:str + reason:str")
        extra = set(e) - _EVIDENCE_KEYS
        if extra:
            errs.append(f"{gid}: неизвестные поля {sorted(extra)}")
    return errs


def load_evidence(path):
    """Загрузить evidence-файл и провалидировать по схеме; SystemExit при ошибках формы."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    errs = validate_evidence(data)
    if errs:
        raise SystemExit("evidence не соответствует schemas/gate-evidence.schema.json:\n  - "
                         + "\n  - ".join(errs))
    return data


# вердикт reviewer-стадии в ПРОЗЕ: строка вида "Recommendation: pass" / "status: passed" /
# "Вердикт: fail" / "Recommendation: needs_work". Якорь на начало строки намеренный: слово внутри
# фразы («passed the tests») вердиктом не считается.
_VERDICT_PASS = re.compile(
    r"(?:^|\n)\s*(?:recommendation|verdict|вердикт|status|итог)\s*[:=]?\s*\(?\s*"
    r"(pass|passed|approved|одобрено|принято)\b", re.I)
# needs_work / warn — ОТДЕЛЬНЫЙ класс, а не «почти pass»: на блокирующем гейте downstream
# превращает его в блок (как структурный warn), поэтому терять его в фолбэке нельзя. Живой
# code-reviewer часто заканчивает именно «Recommendation: needs_work» — раньше это не матчило ни
# один из двух паттернов и давало no-verdict на валидном заключении.
_VERDICT_WARN = re.compile(
    r"(?:^|\n)\s*(?:recommendation|verdict|вердикт|status|итог)\s*[:=]?\s*\(?\s*"
    r"(warn|warning|needs[_\s-]?work|нужны\s+правки|доработать|доработка|замечани)\b", re.I)
_VERDICT_FAIL = re.compile(
    r"(?:^|\n)\s*(?:recommendation|verdict|вердикт|status|итог)\s*[:=]?\s*\(?\s*"
    r"(fail|failed|blocker|blocked|отклонено|провален)\b", re.I)


def _last_prose_verdict(text):
    """Последний вердикт-в-прозе -> 'pass'|'warn'|'fail'|None (берём ИТОГОВЫЙ, а не первый).

    Прежде фолбэк проверял FAIL-паттерн ПЕРВЫМ и возвращал 'fail' при любом его совпадении. На тексте,
    где судья цитирует пример «Вердикт: fail», а СВОЙ итог выносит 'pass' (или наоборот), это давало
    вердикт, которого в заключении нет. Промпт роли требует вердикт В КОНЦЕ; всё раньше — цитата или
    пример. Поэтому решает ПОСЛЕДНЯЯ вердикт-строка по позиции, а не приоритет одного класса над
    другим — та же дисциплина «последний блок побеждает», что у extract_reviewer_json для JSON."""
    if not text:
        return None
    best_pos, best = -1, None
    for verdict, rx in (("pass", _VERDICT_PASS), ("warn", _VERDICT_WARN), ("fail", _VERDICT_FAIL)):
        for m in rx.finditer(text):
            if m.start() > best_pos:
                best_pos, best = m.start(), verdict
    return best


def extract_reviewer_json(text):
    """Достать структурное `reviewer-result` из ответа судьи. None — блока нет или он не тот.

    ЕДИНОЕ место разбора (v3.37): раньше эта эвристика жила в `orchestrator._write_reviewer_json`
    и повторялась глазами в gate-евалах. Две копии разбора вердикта — это две правды о том, что
    судья сказал; корпус C1 меряет устойчивость вердикта и обязан мерить ТОТ разбор, который
    работает в бою, а не свой похожий.

    Форма проверяется структурно (kind + status из словаря). Схему целиком сверяет
    `validation/validate_reviewer_result.py` — там, где вердикт принимают (orchestrator);
    здесь пакетного импорта валидатора нет намеренно: `gates` лежит слоем ниже `validation`.

    РАЗБОР ИСПРАВЛЕН ЗАМЕРОМ (первый живой прогон корпуса gate-евалов, 20.08.2026). Здесь стояло
    `re.search(r"[{].*[}]", text, re.S)` — жадный захват от ПЕРВОЙ `{` до ПОСЛЕДНЕЙ `}`. На живом
    ответе code-reviewer'а это сломалось сразу: ревью цитировало проверяемый код со строкой
    `issues.append({"type": "undefined_flag"})`, захват начался с неё, `json.loads` упал, и
    структурное заключение судьи было МОЛЧА ОТБРОШЕНО. Дальше срабатывал фолбэк на regex по прозе,
    и гейт получал безымянное «reviewer verdict FAIL @ …» вместо конкретных блокеров судьи
    (в трёх записанных ответах их 8, 4 и 6) и его же checks. Класс — не «редкий случай»: ревью питоновского или js-кода цитирует
    фигурную скобку почти всегда.

    Теперь кандидаты разбираются по одному через `raw_decode` (он корректно проходит строки и
    экранирование) и берётся ПОСЛЕДНИЙ валидный: промпт роли требует структурный блок в КОНЦЕ
    ответа, а всё, что раньше, — цитата или пример.
    """
    if not text:
        return None
    decoder = json.JSONDecoder()
    found = None
    idx = text.find("{")
    while idx != -1:
        try:
            obj, end = decoder.raw_decode(text, idx)
        except ValueError:
            idx = text.find("{", idx + 1)
            continue
        if (isinstance(obj, dict) and obj.get("kind") == "reviewer-result"
                and obj.get("status") in ("pass", "warn", "fail")):
            found = obj
            idx = text.find("{", end)
        else:
            idx = text.find("{", idx + 1)
    return found


def evidence_from_reviewer_result(gate: dict, rr: dict, source: str) -> dict:
    """Структурный вердикт судьи -> evidence одного гейта (источник истины, не regex по прозе)."""
    if rr["status"] == "fail":
        return {"status": "fail",
                "blockers": rr.get("blockers") or [f"reviewer FAIL @ {source}"],
                "evidence": [source]}
    # pass/warn: та же дисциплина — required_evidence авто-даём только ai-review
    prov = list(gate.get("required_evidence", []) or []) if classify(gate) == "ai-review" else []
    return {"status": rr["status"], "provided": prov,
            "checks": rr.get("checks", []), "evidence": [source]}


def evidence_from_markdown(gate: dict, text: str, source: str):
    """Фолбэк для артефактов без структурного заключения: строка вердикта в прозе.
    None — вердикта в тексте нет (и тогда гейт остаётся НЕзакрытым, а не «зелёным по умолчанию»)."""
    verdict = _last_prose_verdict(text or "")
    if verdict is None:
        return None
    if verdict == "fail":
        return {"status": "fail", "blockers": [f"reviewer verdict FAIL @ {source}"],
                "evidence": [source]}
    if verdict == "warn":
        # warn/needs_work ревьюера — то же, что структурный warn: на блокирующем гейте downstream
        # (evaluate_gate/_run_reviews) превращает его в блок. Тихим pass это не становится.
        return {"status": "warn", "warnings": [f"reviewer verdict NEEDS_WORK/WARN @ {source}"],
                "evidence": [source]}
    # pass:
    # Дисциплина evidence (v2.16): «pass» ревьюера — доказательство ТОЛЬКО для
    # ai-review гейтов (судья и есть evidence). Для детерминированных/human гейтов
    # слово ревьюера НЕ фабрикует required_evidence (build_passed/tests_passed/…):
    # их закрывают реальные валидаторы/факты, иначе «evidence» снова = «поверьте на слово».
    if classify(gate) == "ai-review":
        return {"status": "pass", "provided": list(gate.get("required_evidence", []) or []),
                "evidence": [f"reviewer verdict @ {source}"]}
    # provided пуст -> при наличии required_evidence evaluate_gate честно даст fail
    return {"status": "pass", "evidence": [f"reviewer verdict @ {source}"]}


# Отказ провайдера, у которого причина «человеческая»: модель отказалась отвечать — тут нужен
# человек. Пустой или обрезанный ответ — не человеческий случай: его чинит повтор с другим
# потолком, и помечать его ожиданием человека значило бы звать не того.
_REFUSAL_NEEDS_HUMAN = {"refused_by_model"}


def evidence_from_judge_refusal(gate: dict, refusal: dict, source: str):
    """Отказ судьи -> evidence, которое НАЗЫВАЕТ причину, а не растворяется в «нет заключения».

    Статус повторяет то, что дал бы гейт без evidence (блокирующий -> fail, advisory -> warn):
    отказ не строже и не мягче отсутствия вердикта — он ровно так же его не даёт. Меняется одно:
    человек читает, ЧТО именно случилось, вместо «нет заключения reviewer»."""
    reason = refusal.get("reason_text") or refusal.get("reason") or "причина не названа"
    detail = refusal.get("detail")
    who = refusal.get("provider") or "провайдер"
    text = (f"заключение судьи не получено ({who}): {reason}"
            f"{'; ' + detail if detail else ''}")
    ev = {"status": "fail" if gate.get("blocking") else "warn", "evidence": [source]}
    if gate.get("blocking"):
        ev["blockers"] = [text]
    else:
        ev["warnings"] = [text]
    if refusal.get("reason") in _REFUSAL_NEEDS_HUMAN:
        ev["pending_human"] = True
    return ev


def evidence_from_no_verdict(gate: dict, *, gate_id: str, stopped=None, reads=None,
                             errors=None, refusal=None):
    """Ревью-петля завершилась без разбираемого вердикта -> evidence, которое НАЗЫВАЕТ причину.

    Тот же принцип, что evidence_from_judge_refusal: гейт остаётся НЕзакрытым (blocking -> fail,
    advisory -> warn), но человек читает, ПОЧЕМУ вердикта нет, а не общее «нет заключения reviewer».

    ПОВОД — находка поля P0 (obs-2026-08-20, ai-ops-cockpit): code_review ОБА прогона кончился
    stopped=no-verdict, valid=false, а `_run_reviews` тихо ронял гейт (`if errs: continue`) — тот
    падал на общий `_unmet_reason`, не называя причину, и `_hard_stop` не распознавал reviewer-blocked
    (работа МОЛЧА вставала). Здесь причина названа, а `"reviewer verdict"` в evidence взводит
    распознавание reviewer-blocked (см. workpackage_executor._hard_stop).

    Различает под-случаи: провайдер назвал отказ (refusal) / бюджет вызовов исчерпан / лимит чтений
    исчерпан без вердикта / ответы судьи не разобрались в reviewer-result."""
    if refusal:                       # провайдер назвал причину (пусто/обрезано/отказ) — она первична
        ev = evidence_from_judge_refusal(gate, refusal, f"reviewer verdict @ {gate_id} (refusal)")
        return ev
    nreads = len(reads or []) if not isinstance(reads, int) else reads
    stopped = stopped or "no-verdict"
    if str(stopped).startswith("budget"):
        why, needs_human = f"судья исчерпал бюджет вызовов до вердикта ({stopped})", False
    elif nreads:
        why, needs_human = (f"судья прочитал {nreads} файл(ов), но на форс-ходе не вынес "
                            f"разбираемого reviewer-result"), True
    else:
        detail = "; ".join(errors or []) or str(stopped)
        why, needs_human = f"судья не вернул разбираемого reviewer-result ни разу ({detail})", True
    text = f"независимый ревьюер не вынес вердикт по гейту {gate_id}: {why}. Гейт не закрыт."
    ev = {"status": "fail" if gate.get("blocking") else "warn", "checks": [],
          "evidence": [f"reviewer verdict @ {gate_id} (no-verdict: {stopped})"]}
    if gate.get("blocking"):
        ev["blockers"] = [text]
    else:
        ev["warnings"] = [text]
    if needs_human:
        ev["pending_human"] = True
    return ev


def evidence_from_judge_output(gate: dict, text: str, source: str = "judge-output"):
    """Ответ судьи (сырой текст) -> evidence одного гейта. Тот же путь, что в бою:
    структурный reviewer-result имеет приоритет, проза — фолбэк, отсутствие вердикта -> None."""
    rr = extract_reviewer_json(text)
    if rr is not None:
        return evidence_from_reviewer_result(gate, rr, source)
    return evidence_from_markdown(gate, text, source)


def collect_evidence(workflow_id: str, run_dir) -> dict:
    """Собрать evidence из артефактов reviewer-стадий (orchestrator --collect-evidence).
    Для каждого гейта ищем ответственную стадию (gate.stage / gate.responsible_role),
    читаем её артефакт stage-<id>.md и извлекаем вердикт. Reviewer'ский pass = доказательство
    гейта (provided := required_evidence); fail — блокер. Эвристика по структурной строке вердикта."""
    workflows, gates = load_workflows(), load_gates()
    wf = workflows.get(workflow_id, {})
    stages = wf.get("stages", [])
    run_dir = Path(run_dir)
    ev = {}
    for gid in wf.get("quality_gates", []) or []:
        g = gates.get(gid, {})
        stage_id = next((s.get("id") for s in stages
                         if s.get("id") == g.get("stage") or s.get("owner") == g.get("responsible_role")),
                        None)
        if not stage_id:
            continue
        # ОТКАЗ ЧИТАЕТСЯ ПЕРВЫМ (v3.37, C2): если судья вердикта не вынес и провайдер назвал
        # причину, эта причина и есть то, что человеку надо знать. Разбирать после отказа нечего —
        # артефакт стадии содержит объяснение отказа, а не заключение.
        rfl = run_dir / f"stage-{stage_id}.refusal.json"
        if rfl.exists():
            try:
                rec = json.loads(rfl.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                rec = None
            if isinstance(rec, dict) and rec.get("kind") == "provider-refusal":
                ev[gid] = evidence_from_judge_refusal(g, rec, rfl.name)
                continue
        # v2.33: структурный reviewer-result — ИСТОЧНИК ИСТИНЫ (не regex по markdown).
        # Если рядом со стадией есть stage-<id>.reviewer.json (schemas/reviewer-result.schema.json),
        # берём вердикт/blockers из него; markdown-regex остаётся фолбэком для старых артефактов.
        rjson = run_dir / f"stage-{stage_id}.reviewer.json"
        if rjson.exists():
            try:
                rr = json.loads(rjson.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                rr = None
            if isinstance(rr, dict) and rr.get("status") in ("pass", "warn", "fail"):
                ev[gid] = evidence_from_reviewer_result(g, rr, rjson.name)
                continue
        art = run_dir / f"stage-{stage_id}.md"
        if not art.exists():
            continue
        e = evidence_from_markdown(g, art.read_text(encoding="utf-8"), art.name)
        if e is not None:
            ev[gid] = e
    return ev


def load_gates():
    return yaml.safe_load((PKG / "quality" / "gates.yaml").read_text(encoding="utf-8")).get("gates", {})


def load_workflows():
    return yaml.safe_load((PKG / "registry" / "workflows.yaml").read_text(encoding="utf-8")).get("workflows", {})


def risk_calibrated_config(root) -> "bool | None":
    """Owner-флаг `gates.risk_calibrated_enforcement` из .ai-ops.yaml ДОЧЕРНЕГО репозитория (#543).

    None -> ключ не задан (флаг не инжектим -> остаётся дефолт OFF, поведение не меняется). Читает
    репо-конфиг (не PKG кита): флаг — решение владельца конкретного продукта. Битый yaml/нет файла ->
    None (fail-safe к OFF)."""
    for name in (".ai-ops.yaml", ".ai-ops.yml"):
        p = Path(root) / name
        if not p.is_file():
            continue
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return None
        g = data.get("gates")
        if isinstance(g, dict) and "risk_calibrated_enforcement" in g:
            return bool(g.get("risk_calibrated_enforcement"))
        return None
    return None


def override_effective(gate: dict, override) -> bool:
    """Снимает ли override блокировку гейта — с учётом ПОЛИТИКИ гейта (v2.16).
    Раньше любой override с by+reason обходил любой блокирующий гейт, игнорируя
    `bypass_policy: forbidden` — это ломало главную гарантию. Теперь:
      - нет override / нет by+reason -> нет;
      - bypass_policy == forbidden -> НИКОГДА (обход запрещён контрактом);
      - override_policy.allowed == true -> да (с субъектом и причиной);
      - иначе (нет явного разрешения) -> нет (доказательства, а не слова)."""
    if not (isinstance(override, dict) and override.get("by") and override.get("reason")):
        return False
    if gate.get("bypass_policy") == "forbidden":
        return False
    op = gate.get("override_policy")
    return bool(isinstance(op, dict) and op.get("allowed"))


def _approval_required(gate: dict, signals: dict = None) -> bool:
    """human_approval: True -> всегда; dict {required_when:[...]} -> только если условие активно
    в сигналах задачи (finding аудита: условный approval не должен блокировать безусловно).
    Токены условий сверяются с сигналами. v2.107 (finding аудита): единый набор алиасов —
    secret_boundary_change ~ security_surface_changed ~ secret_boundary (spec_levels/security_pack
    используют secret_boundary; раньше гейт не срабатывал от него -> дрейф имён сигнала)."""
    ha = gate.get("human_approval")
    if ha is True:
        return True
    if isinstance(ha, dict):
        conds = ha.get("required_when", []) or []
        sig = signals or {}
        alias = {"secret_boundary_change": ["security_surface_changed", "secret_boundary"]}
        for c in conds:
            names = [c] + (alias.get(c) or [])
            if any(sig.get(n) for n in names):
                return True
        return False
    return bool(ha)


def classify(gate: dict, signals: dict = None) -> str:
    """Способ проверки гейта: human-approval | deterministic | ai-review | writer-check.
    Условный human_approval становится human-approval ТОЛЬКО когда условие активно (signals)."""
    if _approval_required(gate, signals):
        return "human-approval"
    if gate.get("validator"):
        return "deterministic"
    if gate.get("review_mode") == "read-only":
        return "ai-review"
    return "writer-check"


# КТО ЗАКРЫВАЕТ ГЕЙТ — четыре ответа, а не три (работа `gate-map-says-who-closes-it`).
#
# Замер 19.08.2026: из 35 гейтов 19 не имеют исполняемого валидатора. Их «зелёное» — не результат
# машины, и дочка об этом не знала: в отчёте прогона все гейты выглядели одинаково.
#
# ЧЕТВЁРТОЕ ЗНАЧЕНИЕ (`writer`) добавлено НЕ для полноты таксономии. Гейт с `review_mode: writer`
# и без валидатора закрывает СВОЯ ЖЕ стадия — тот, кто произвёл работу, объявляет её проверенной.
# Назвать это `judge` значило бы напечатать в отчёте ровно то утверждение, против которого стоит
# инвариант «writer ≠ judge».
CLOSED_BY = {
    "deterministic": "validator",   # машина: детерминированный валидатор
    "ai-review":     "judge",       # мнение независимого судьи-роли (read-only)
    "writer-check":  "writer",      # самозаявление стадии, которая работу и сделала
    "human-approval": "human",      # решение человека
}
CLOSED_BY_VALUES = tuple(dict.fromkeys(CLOSED_BY.values()))

# ИСТОЧНИК ДОКАЗАТЕЛЬСТВА — БИНАРНАЯ ЧЕСТНОСТЬ ПОВЕРХ ЧЕТЫРЁХ «КТО ЗАКРЫВАЕТ» (веха 4.2, #588).
#
# Замер `closed_by` уже различает validator/judge/writer/human. Но вопрос вехи 4.2 другой и грубее:
# «этому МОЖНО ВЕРИТЬ как доказательству, или это МНЕНИЕ?». Ответов ровно три:
#   - `deterministic` — тест/lint/CI/schema-валидатор: воспроизводимо, тот же вход даёт тот же ответ;
#   - `ai_judgment`   — заключение AI-судьи ИЛИ самозаявление писателя: advisory-мнение, НЕ доказательство
#                       (если evidence генерит AI, а проверяет другой AI — ground truth нет);
#   - `human`         — решение человека, ответственность названа поимённо.
#
# Писатель и судья схлопываются в `ai_judgment` НАМЕРЕННО: оба — суждение, ни одно не воспроизводимо;
# для вопроса «можно ли верить» они по одну сторону от машины. `closed_by` сохраняет их различие
# (writer ≠ judge), эта карта отвечает на более грубый вопрос доверия. Отличать от `evidence_mode`
# в gate-result-v2 (это ПОЛИТИКА — как гейт СЛЕДУЕТ оценивать); здесь — ФАКТИЧЕСКИЙ источник закрытия.
EVIDENCE_SOURCE = {
    "validator": "deterministic",
    "judge":     "ai_judgment",
    "writer":    "ai_judgment",
    "human":     "human",
}
EVIDENCE_SOURCE_VALUES = tuple(dict.fromkeys(EVIDENCE_SOURCE.values()))


def closed_by(gate: dict, signals: dict = None) -> str:
    """Кто закрывает гейт СЕЙЧАС: validator | judge | writer | human.

    Выводится из той же классификации, по которой гейт исполняется, а не объявляется рядом:
    второе объявление разошлось бы с поведением на первой же правке. Реестр `quality/gates.yaml`
    несёт то же значение для ДОЧКИ (она читает реестр, а не код), и тест сверяет их между собой.
    """
    return CLOSED_BY[classify(gate, signals)]


def evidence_source(gate: dict, signals: dict = None) -> str:
    """Источник доказательства гейта: deterministic | ai_judgment | human (веха 4.2, #588).

    Выводится из `closed_by` — той же классификации, что ИСПОЛНЯЕТ гейт, а не из самозаявления:
    AI-стадия не может пометить своё «зелёное» как детерминированное, потому что метку ставит
    структура гейта, а не producer evidence. Это тот же принцип, что у `closed_by`.
    """
    return EVIDENCE_SOURCE[closed_by(gate, signals)]
