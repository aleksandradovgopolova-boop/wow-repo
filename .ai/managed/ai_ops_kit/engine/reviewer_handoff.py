#!/usr/bin/env python3
"""Handoff ревью оркестратору, когда провайдер ревьюера недоступен в среде (#160, сессия Клода).

ПОВОД — заявка #160 и ПОСТОЯННАЯ среда команды. Кит запускается ТОЛЬКО изнутри активной сессии
Claude Code (без ANTHROPIC_API_KEY): вложенный `claude -p` — ревьюер code_review — обрывается ДО
модели (ProviderEnvUnavailableError). Прежде это либо роняло весь прогон, либо давало ГЛУХОЙ
no-verdict, который не закрывался НИ НА КАКОЙ правке — гейт «нет заключения reviewer» держал работу
навсегда. Это не транзиентный сбой и не дефект кита: причина в самой среде, повтор её не лечит.

РЕШЕНИЕ — handoff. Когда провайдер ревьюера структурно недоступен, прогон НЕ падает и НЕ штампует
no-verdict, а встаёт в состояние `awaiting_reviewer`: записывает МАШИНОЧИТАЕМЫЙ запрос на ревью
(что за гейт, чек-лист, проверяемая ревизия, изменённые файлы, ТОЧНЫЙ путь артефакта-вердикта).
Вердикт выносит ОРКЕСТРАТОР (Клод в приложении) — НЕЗАВИСИМАЯ identity, не писатель, — записывая
`reviewer-result` в названный артефакт. Следующий прогон (resume/reevaluate) перечитывает артефакт
и закрывает гейт.

ГРАНИЦЫ (инвариант 0 false-green):
  * артефакт принимается, только если он на ТЕКУЩЕЙ ревизии (`reviewed_revision == revision`),
    по форме `reviewer-result` (checks.reviewer_result.check) и АТРИБУТИРОВАН не-писателю
    (writer ≠ judge — часть 6); заземление pass-вердикта идёт тем же путём, что живой вердикт
    (Fix C: цитата изменённого файла на проверенном SHA), поэтому здесь не дублируется;
  * пустой / отсутствующий / устаревший-SHA / писательский артефакт -> `load_verdict` вернёт None
    (fail-closed): гейт остаётся закрытым, а прогон снова встаёт в awaiting_reviewer с новым запросом.

Ридер вердикта — `gate_executor.evidence_from_reviewer_result` (тот же контракт, что читает
`collect_evidence`); переиспользуем его, а не заводим второй разбор вердикта. Артефакты живут под
`<root>/.ai/reviewer-requests/<gate>.{request,reviewer}.json` (тот же `<child_root>/.ai`, что и
кеш reevaluate — вне worktree-дерева, поэтому переживают пересборку worktree и видны оркестратору).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# writer-маркеры: вердикт, приписанный ПИСАТЕЛЮ (или анонимный/пустой), инвариант writer≠judge НЕ
# проходит — независимого суждения он не подтверждает. Список замкнут и в нижнем регистре.
_WRITER_IDENTITIES = {"", "writer", "author", "self", "claude-cli", "claude-code-local"}

_DIR_REL = (".ai", "reviewer-requests")


def _dir(root) -> Path:
    return Path(root).joinpath(*_DIR_REL)


def verdict_path(root, gate_id) -> Path:
    """Путь артефакта-ВЕРДИКТА, который заполняет оркестратор (кит его ЧИТАЕТ)."""
    return _dir(root) / f"{gate_id}.reviewer.json"


def request_path(root, gate_id) -> Path:
    """Путь артефакта-ЗАПРОСА, который пишет кит (оркестратор его читает)."""
    return _dir(root) / f"{gate_id}.request.json"


def _reviewer_identity(rr: dict) -> str:
    """Кому атрибутирован вердикт: поле `reviewer` (agent id ревьюера) либо `owner`."""
    v = rr.get("reviewer") or rr.get("owner")
    return v.strip() if isinstance(v, str) else ""


def open_request(root, gate_id, *, checklist, reviewed_revision, changed_files, blocking,
                 required_evidence=None, cause=None):
    """Записать машиночитаемый запрос на ревью и вернуть evidence состояния `awaiting_reviewer`.

    Запрос НЕ выдаёт вердикт — он говорит оркестратору, ЧТО заполнить: точный путь артефакта-вердикта,
    чек-лист гейта, проверяемую ревизию, список изменённых файлов. Гейт остаётся ЗАКРЫТЫМ (blocking ->
    fail), но ОТЛИЧИМ и от глухого no-verdict (записан запрос + `human_handoff`), и от hard-error
    (прогон не падает). Ложным зелёным это не является: статус — fail на блокирующем гейте.

    `cause` — ПОЧЕМУ открыт handoff (среда недоступна / провайдер вернул пусто): едет в текст человеку
    и в запрос, чтобы причина не подменялась одной дежурной формулировкой (следствие #570-follow-up:
    awaiting открывается не только на env-unavailable, но и на пустом ответе провайдера)."""
    vpath = verdict_path(root, gate_id)
    rpath = request_path(root, gate_id)
    cause = cause or "исполнитель ревьюера недоступен в этой среде (сессия Клода, #160)"
    req = {
        "schema_version": 1, "kind": "reviewer-request", "gate": gate_id,
        "reviewed_revision": reviewed_revision,
        "changed_files": sorted(changed_files or []),
        "checklist": checklist or "",
        "required_evidence": list(required_evidence or []),
        "cause": cause,
        "verdict_artifact": str(vpath),
        "instructions": (
            f"Вынеси НЕЗАВИСИМОЕ ревью гейта '{gate_id}' и запиши reviewer-result "
            f"(schemas/reviewer-result.schema.json) в {vpath}. ОБЯЗАТЕЛЬНО: поле `reviewer` = твоя "
            f"независимая identity (оркестратор/ревьюер, НЕ писатель); `reviewed_revision` = "
            f"{reviewed_revision}; при status=pass хотя бы один check с evidence [{{file,lines}}] на "
            f"ИЗМЕНЁННЫЙ файл из changed_files (иначе гейт не закроется). Затем повтори прогон с "
            f"resume — кит перечитает вердикт и закроет гейт."),
    }
    d = _dir(root)
    d.mkdir(parents=True, exist_ok=True)
    rpath.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")
    text = (f"ревьюер гейта {gate_id} не вынес вердикт ({cause}) — прогон встал в awaiting_reviewer: "
            f"запрос на ревью записан в {rpath}, вердикт ожидается в {vpath} "
            f"(заполняет независимый оркестратор), после чего resume закроет гейт")
    ev = {"status": "fail" if blocking else "warn", "checks": [],
          "evidence": [f"awaiting_reviewer @ {gate_id}: handoff-запрос записан ({rpath.name})"],
          # human_handoff (а не pending_human) — отдельный признак: гейт ЖДЁТ ревью оркестратора, а
          # не нашёл дефект. gate_executor разворачивает оба флага в awaiting_human=True.
          "human_handoff": True}
    if blocking:
        ev["blockers"] = [text]
    else:
        ev["warnings"] = [text]
    return ev


def load_verdict(root, gate_id, *, revision):
    """Перечитать вердикт-артефакт оркестратора -> (reviewer_result|None, note).

    Возвращает разобранный reviewer-result, ТОЛЬКО если он: (1) валиден по форме
    (checks.reviewer_result.check); (2) вынесен ИМЕННО по этому гейту; (3) на ТЕКУЩЕЙ ревизии
    (`reviewed_revision == revision` — вердикт на другом/неизвестном SHA не принимается); (4)
    атрибутирован НЕ-писателю (writer ≠ judge). Иначе -> (None, причина): гейт остаётся незакрытым
    (fail-closed). ЗАЗЕМЛЕНИЕ pass здесь НЕ проверяется намеренно — вызывающий (`_run_reviews`)
    прогоняет вердикт по ТОМУ ЖЕ пути рубер-штампа, что и живой вердикт (Fix C), чтобы механизм
    заземления был один, а не два."""
    from ai_ops_kit.checks import reviewer_result as _vrr  # чистая проверка формы (лента №5, вниз)
    vp = verdict_path(root, gate_id)
    if not vp.is_file():
        return None, "нет артефакта-вердикта"
    try:
        rr = json.loads(vp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "артефакт-вердикт нечитаем/не JSON"
    if not isinstance(rr, dict):
        return None, "артефакт-вердикт не объект"
    errs = _vrr.check(rr)
    if errs:
        return None, "вердикт не по форме reviewer-result: " + "; ".join(str(e) for e in errs[:3])
    if rr.get("gate") != gate_id:
        return None, f"вердикт вынесен по другому гейту ({rr.get('gate')})"
    rev = rr.get("reviewed_revision")
    if revision and rev != revision:
        return None, (f"вердикт на другом/неизвестном SHA ({str(rev)[:12] or 'нет'} != "
                      f"{str(revision)[:12]}) — не принимается")
    who = _reviewer_identity(rr)
    if who.lower() in _WRITER_IDENTITIES:
        return None, ("вердикт не атрибутирован независимому ревьюеру (reviewer='"
                      f"{who}') — writer≠judge не подтверждён")
    return rr, "принят"


def main(argv):
    print(__doc__)
    print("Проверки этого модуля — в tests/unit/test_reviewer_handoff.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
