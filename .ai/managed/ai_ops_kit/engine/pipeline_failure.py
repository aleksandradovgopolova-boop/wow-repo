#!/usr/bin/env python3
"""Failure analysis and environment qualification for the execution pipeline.

Extracted from execution_pipeline.py — failure signal detection, diff checks,
security verdict validation, environment symptom detection.
"""
from __future__ import annotations

import re


_ENV_SYMPTOMS = ("command not found", "not found", "no such file", "no module named",
                 "modulenotfounderror", "cannot find module", "is not recognized",
                 "executable not found", "no such command")


def _check_has_env_symptom(c):
    """У проверки есть симптом неподготовленного окружения (нет тулчейна/зависимости)?"""
    for run in ((c or {}).get("runs") or []):
        if run.get("ok"):
            continue
        if run.get("exit_code") == 127:
            return True
        if any(s in (run.get("output_tail") or "").lower() for s in _ENV_SYMPTOMS):
            return True
    return False


def _env_proven_ok(checks):
    """v2.121 (P1.4, строгий install-фикс): окружение считается ДОКАЗАННО рабочим ТОЛЬКО если хотя бы
    одна применимая проверка РЕАЛЬНО отработала — прошла (pass) ЛИБО упала по настоящей причине кода
    (fail БЕЗ env-симптома: тулчейн есть, тест честно красный)."""
    for c in (checks or {}).values():
        st = (c or {}).get("status")
        if st == "pass":
            return True
        if st in ("fail", "error") and not _check_has_env_symptom(c):
            return True
    return False


def _env_unqualified(checks):
    """Обратная форма для совместимости/наглядности: окружение НЕ квалифицировано доказательно."""
    return not _env_proven_ok(checks)


def _env_skipped_checks(checks):
    """Проверки, НЕ выполнившиеся ИЗ-ЗА среды (нет инструмента: exit 127 / `command not found` /
    `no module named`), а НЕ из-за кода. -> [(name, короткая_причина)], в порядке имён.

    Нужно для ЧЕСТНОГО вердикта: когда pytest прошёл, а ruff/typecheck недоступны, гейт остаётся
    незакрытым — но это дефицит СРЕДЫ, не кода. Без явного названия «гейт не закрыт» читается как
    «код плохой». Здесь среда называется средой."""
    out = []
    for name in sorted((checks or {}).keys()):
        c = (checks or {}).get(name) or {}
        if c.get("status") != "fail" or not _check_has_env_symptom(c):
            continue
        reason = "инструмент недоступен"
        for run in (c.get("runs") or []):
            if run.get("ok"):
                continue
            tail = [ln for ln in (run.get("output_tail") or "").strip().splitlines() if ln.strip()]
            if run.get("exit_code") == 127 or tail:
                reason = (tail[-1].strip()[:80] if tail else f"exit {run.get('exit_code')}")
                break
        out.append((name, reason))
    return out


def _env_degraded_note(checks):
    """Человеко-читаемая строка вердикта про env-дефицит — или None, если таких проверок нет.

    Формулировка честная в обе стороны: детерминированные проверки, которые ОТРАБОТАЛИ, зелены; эти
    ворота ПРОПУЩЕНЫ (среда), а не провалены (код). Не выдаёт незакрытый гейт за дефект правки."""
    skipped = _env_skipped_checks(checks)
    if not skipped:
        return None
    named = "; ".join(f"{n} ({why})" for n, why in skipped)
    return ("инструменты недоступны в среде прогона (дефицит среды, не дефект кода): " + named
            + " — детерминированные проверки, которые отработали, зелены; эти ворота ПРОПУЩЕНЫ, "
              "не провалены. Полный вердикт требует чистой среды (CI) или установки инструментов.")


def _baseline_failure_summary(checks, tail=500):
    """Свод падающих проверок базы с ФАКТИЧЕСКИМ выводом — чтобы модель знала, что чинить."""
    lines = []
    for name, c in (checks or {}).items():
        if (c or {}).get("status") != "fail":
            continue
        for run in (c.get("runs") or []):
            if run.get("ok"):
                continue
            out = (run.get("output_tail") or "")[-tail:]
            lines.append(f"[{name}] {run.get('command')} (exit {run.get('exit_code')}):\n{out}")
    return "\n".join(lines)


def _failure_signal(check):
    """Грубая метрика 'насколько плохо' для проверки: макс. число failed/errors в выводе."""
    n = 0
    for run in (check or {}).get("runs", []) or []:
        for m in re.finditer(r"(\d+)\s+(?:failed|errors?)\b", run.get("output_tail") or "", re.I):
            n = max(n, int(m.group(1)))
    return n


# v2.84: СТРУКТУРНЫЕ идентификаторы падений — чтобы ловить «починил один тест, сломал другой»
_FAILURE_ID_PATTERNS = [
    r"(?:FAILED|ERROR)\s+(\S+::\S+)",
    r"(\S+::\S+)\s+(?:FAILED|ERROR)\b",
    r"---\s+FAIL:\s+(\S+)",
    r"(\S+\.\w+\(\d+,\d+\)):\s*error\s+(TS\d+)",
    r"([\w./\-]+\.go):(\d+):(?:(\d+):)?\s*(.+)",
    r"([\w./\-]+\.\w+)\s*\((\d+)[,:](\d+)\):\s*(.+)",
    r"error\[(E\d+)\]",
    r"thread '([^']+)' .*?panicked at ([\w./\-]+\.rs):(\d+)",
    r"([\w.$]+\.[\w$]+)\s+--\s+Time elapsed[^\n]*<<<\s+(?:FAILURE|ERROR)",
    r"\[ERROR\]\s+([\w.$]+\.[\w$]+):(\d+)\b",
    r"([\w.$]+)\s+>\s+([\w$]+)\(\)\s+FAILED",
    r"(?:✕|×|✗)\s+(.+?)(?:\s+\(\d+\s*ms\))?\s*$",
    r"(?:^|\n)\s*FAIL\s+(\S+)",
    r"(?:^|\n)\s*(?:AssertionError|Error):\s*(.+)$",
]

# v2.88: волатильные токены в выводе -> РАЗНЫЙ id при ОДНОЙ и той же поломке
_VOLATILE_RE = re.compile(r"\b\d+(?:\.\d+)?\s*m?s\b|0x[0-9a-fA-F]+|\b\d+(?:\.\d+)?\s*ms\b")


def _normalize_failure_id(token):
    import re as _re
    return _re.sub(r"\s+", " ", _VOLATILE_RE.sub("", token)).strip()


def _failure_ids(check):
    """Множество нормализованных id падений из output_tail проверки (best-effort по раннерам)."""
    ids = set()
    for run in (check or {}).get("runs", []) or []:
        tail = run.get("output_tail") or ""
        tail = re.sub(r"\x1b\[[0-9;]*m", "", tail)
        for pat in _FAILURE_ID_PATTERNS:
            for m in re.finditer(pat, tail, re.I | re.M):
                token = _normalize_failure_id(" ".join(t for t in m.groups() if t).strip())
                if token:
                    ids.add(token[:200])
    return ids


def _diff_checks(baseline, after):
    """Сравнить проверки ДО и ПОСЛЕ правки. -> (regressions, fixed)."""
    baseline, after = baseline or {}, after or {}
    regressions, fixed = [], []
    real = ("pass", "fail")
    for name, a in after.items():
        b = baseline.get(name) or {}
        b_status, a_status = b.get("status"), a.get("status")
        if a_status == "fail" and b_status != "fail":
            regressions.append(name)
        elif b_status == "fail" and a_status == "pass":
            fixed.append(name)
        elif b_status == "fail" and a_status == "fail":
            b_ids, a_ids = _failure_ids(b), _failure_ids(a)
            new_ids = a_ids - b_ids
            if new_ids or _failure_signal(a) > _failure_signal(b):
                regressions.append(name)
            elif a_ids and (b_ids - a_ids):
                fixed.append(name)
        elif b_status in real and a_status not in real:
            regressions.append(name)
    return regressions, fixed


def _baseline_status_flips(baseline, after):
    """Проверки, чей статус pass<->fail РАЗЛИЧАЕТСЯ между базой и правкой (#405).

    Это ровно те проверки, из-за которых baseline-diff (и производный `_iv_baseline_exempt`)
    способен ПЕРЕВЕРНУТЬ вердикт readiness между двумя прогонами: date-зависимый/flaky тест,
    оказавшийся `pass` при захвате базы и `fail` после (или наоборот), молча снимает или выдаёт
    освобождение implementation_verification. Функция НЕ судит, регресс это или починка — она
    только НАЗЫВАЕТ нестабильную проверку, чтобы флип вердикта был атрибутирован, а не безмолвен.

    Детерминированна на одном входе: -> отсортированный список имён проверок.
    """
    baseline, after = baseline or {}, after or {}
    real = ("pass", "fail")
    flips = []
    for name, a in after.items():
        b = baseline.get(name) or {}
        b_status, a_status = b.get("status"), a.get("status")
        if b_status in real and a_status in real and b_status != a_status:
            flips.append(name)
    return sorted(flips)


def _evidence_ref_errors(dom, ev_items, reviewer_reads=None):
    """v3.0.10 (finding аудита P1): evidence домена — СТРУКТУРНЫЕ ссылки (EvidenceRef), а не строка."""
    errs = []
    if not (isinstance(ev_items, list) and ev_items):
        return [f"домен '{dom}': пустой/неструктурный список evidence"]
    reads = reviewer_reads if isinstance(reviewer_reads, list) else None

    def _read_match(path):
        p = str(path).strip().replace("\\", "/")
        for r in reads:
            rr = str(r or "").strip().replace("\\", "/")
            if not rr:
                continue
            if rr == p or rr.endswith("/" + p) or p.endswith("/" + rr):
                return True
        return False

    for ev in ev_items:
        if not isinstance(ev, dict):
            errs.append(f"домен '{dom}': evidence '{ev}' не структурная ссылка "
                        "(нужен {type, path/command/...}, не строка)")
            continue
        et = ev.get("type")
        if et in ("code-read", "read", "file", "source", "code"):
            path = ev.get("path")
            if not path:
                errs.append(f"домен '{dom}': code-read evidence без path")
            elif reads is not None and not _read_match(path):
                errs.append(f"домен '{dom}': code-read evidence ссылается на '{path}', которого нет среди "
                            "реально прочитанных ревьюером файлов — сфабрикованная ссылка")
        elif et == "test":
            if not ev.get("command"):
                errs.append(f"домен '{dom}': test evidence без command")
        elif et in ("finding", "scanner"):
            if not (ev.get("id") or ev.get("detail") or ev.get("path")):
                errs.append(f"домен '{dom}': {et} evidence без id/detail/path")
        elif ev.get("path"):
            if reads is not None and not _read_match(ev["path"]):
                errs.append(f"домен '{dom}': evidence ссылается на '{ev['path']}', которого нет среди "
                            "реально прочитанных ревьюером файлов — сфабрикованная ссылка")
        else:
            errs.append(f"домен '{dom}': evidence без распознаваемого type и без path "
                        f"(нужен code-read|test|finding + path/command, получено {et!r})")
    return errs


def _security_verdict_errors(res, revision, applicable_domains, vrr, reviewer_reads=None):
    """v3.0-rc16: строгая проверка security reviewer-result."""
    if not isinstance(res, dict):
        return ["security-reviewer не вернул структурный вердикт"]
    errs = list(vrr.check(res, gate_ids=None) or [])
    if res.get("gate") not in (None, "security"):
        errs.append(f"gate вердикта '{res.get('gate')}' != security")
    if revision and res.get("reviewed_revision") not in (None, revision):
        errs.append("reviewed_revision вердикта != проверяемой ревизии")
    checks = res.get("checks") if isinstance(res.get("checks"), list) else []
    if not checks:
        errs.append("security-вердикт без checks — нечем подтвердить проверенные домены")
    if applicable_domains:
        dr = res.get("domain_results")
        if not isinstance(dr, list) or not dr:
            errs.append("нет domain_results — один общий вердикт не доказывает каждый применимый домен")
        else:
            seen = [str((x or {}).get("domain")) for x in dr if isinstance(x, dict)]
            got = set(seen)
            need = set(applicable_domains)
            if len(seen) != len(got):
                errs.append("domain_results содержит дубли доменов")
            if got != need:
                missing = need - got
                extra = got - need
                if missing:
                    errs.append(f"domain_results не покрывает домены: {', '.join(sorted(missing))}")
                if extra:
                    errs.append(f"domain_results содержит неизвестные/лишние домены: {', '.join(sorted(extra))}")
            for x in dr:
                st = (x or {}).get("status")
                dom = (x or {}).get("domain")
                if st not in ("pass", "warn", "fail"):
                    errs.append(f"domain_result '{dom}' без валидного status")
                elif st != "pass" and (res.get("status") == "pass"):
                    errs.append(f"домен '{dom}' = {st}, но общий status=pass — несогласованно")
                dchecks = (x or {}).get("checks")
                if not (isinstance(dchecks, list) and dchecks):
                    errs.append(f"домен '{dom}' без domain-specific checks — доказательства по домену отсутствуют")
                else:
                    for c in dchecks:
                        if not isinstance(c, dict) or not c.get("id") or c.get("status") not in ("pass", "warn", "fail"):
                            errs.append(f"домен '{dom}': nested-check без id/валидного status ({c})")
                    if st == "pass" and not any(isinstance(c, dict) and c.get("status") == "pass" for c in dchecks):
                        errs.append(f"домен '{dom}' pass, но ни один его check не подтверждён (status=pass)")
                    if st in ("warn", "fail") and not (x or {}).get("blockers"):
                        errs.append(f"домен '{dom}' = {st} без blockers — блокирующий вердикт без причины")
                    if st == "pass":
                        _all_ev = []
                        _dom_ev = (x or {}).get("evidence")
                        if isinstance(_dom_ev, list):
                            _all_ev += _dom_ev
                        for c in dchecks:
                            _ce = c.get("evidence") if isinstance(c, dict) else None
                            if isinstance(_ce, list):
                                _all_ev += _ce
                            elif _ce:
                                _all_ev.append(_ce)
                        if not _all_ev:
                            errs.append(f"домен '{dom}' pass без evidence-ссылки — id+status не доказательство")
                        else:
                            errs += _evidence_ref_errors(dom, _all_ev, reviewer_reads)
    return errs

# Запуск скриптом ОБЪЯСНЯЕТ модуль, а не молчит (ревизия 2026-08-11).
#
# Здесь стояло `sys.exit(selftest())`, а сама функция удалена в v3.30 вместе с переносом
# селфтестов в pytest: любой запуск падал с `NameError`. Просто убрать блок — тоже неверно:
# модуль остаётся запускаемой точкой входа (`python3 -m ai_ops_kit.engine.pipeline_failure`), и молчаливый
# выход с кодом 0 — тот самый дефект «ноль и есть симптом».
# Поэтому вход делает осмысленную работу — печатает назначение модуля, как `invariants.py`.
# Проверки модуля — в `tests/unit/`.
if __name__ == "__main__":
    print(__doc__)
    print("Проверки этого модуля — в tests/unit/ (pytest), отдельного --selftest нет с v3.30.")
