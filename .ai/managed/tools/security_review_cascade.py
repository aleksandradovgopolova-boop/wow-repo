#!/usr/bin/env python3
"""Security Review Cascade — asymmetric fail-closed security judge (v3.8.4-rc).

СТАТУС: experimental / qualification-only. НЕ подключён к рабочему strict security_review и НЕ даёт
auto-close. Held-out N=80 (deepseek оба прохода) НЕ квалифицировал каскад (1 unsafe_pass + низкий
clean_auto_pass); strict security остаётся за человеком (#5). Компонент сохранён как воспроизводимый
отрицательный эксперимент + инфраструктура будущих Bench для ДРУГОЙ модели, а не обязательный второй
вызов в каждом security-review (это цена+задержка без права на auto-close). Подключать к strict-path
только после НОВОГО held-out на более сильной/иной модели, дающего 0 unsafe_pass И порог автономности.

Проблема (доказана held-out v2/v3): одиночный дешёвый security-судья НЕ даёт одновременно
recall=1.0 И specificity≥0.90. Prompt-тюнинг разменивает переблок на пропуски — «умнее и точнее»
prompt поднял specificity 0.68→0.96, но пропустил 3 реальных дефекта (false_green 0→3). Для
security пропуск уязвимости — критический провал, поэтому такой путь тупиковый.

Каскад разделяет задачу на два РАЗНЫХ прохода + ДЕТЕРМИНИРОВАННЫЙ reducer (не третий LLM-вызов):

  Проход A (detector, high-recall): найти ВСЕ правдоподобные findings. Ошибается в сторону fail.
  Проход B (verifier, per-finding): для ОДНОГО finding восстановить data-flow, exploit-prerequisites
           и counter-evidence. Отличается от A ЗАДАЧЕЙ и ВХОДОМ (один finding, не весь ре-ревью) —
           чтобы ошибки двух проходов одной модели не коррелировали.
  Reducer (обычный код):  confirmed → fail;  uncertain → pending_human;
           disproved + РАСПОЗНАННОЕ структурное доказательство безопасности → pass для finding.

Асимметрия (fail-closed) — принципиальная поправка владельца: «не вижу exploit» ≠ доказанно
безопасно. Auto-pass finding возможен ТОЛЬКО когда verifier предъявил положительное структурное
доказательство безопасности (proof_type ∈ RECOGNIZED_PROOFS + cited_lines) И это доказательство
НЕ ОПРОВЕРГНУТО детерминированной корроборацией по коду. Иначе → pending_human. Verifier не может
превратить реальный дефект в pass без доказательства; корроборация ловит частые галлюцинации
(мнимая параметризация при f-string SQL, «checksum» на пароле, безопасный argv при shell=True).
Поэтому recall всего каскада ⩾ recall прохода A: B только ДАУНГРЕЙДИТ и только с доказательством,
а недоказанное опровержение уводит finding в human, не в pass.

Модель-нейтрально: detector/verifier инъектируются (в bench — deepseek оба прохода; в selftest —
детерминированные фейки, без сети). Reducer и корроборация — чистый код, тестируются оффлайн.

Контракт результата: kind=SecurityCascadeResult. check()/selftest().
  security_review_cascade.py --selftest
"""
from __future__ import annotations

import re
import sys

# Распознаваемые положительные доказательства безопасности. disproved засчитывается как безопасный
# pass ТОЛЬКО если verifier назвал один из них + процитировал строки. Иначе — fail-closed → human.
RECOGNIZED_PROOFS = {
    "validator_before_sink",       # валидирующая функция реально вызывается до sink
    "unreachable_from_user_input", # путь недостижим из пользовательского ввода
    "parameterized_query",         # SQL параметризован (доказано по коду)
    "not_a_secret",                # значение не является секретом
    "checksum_not_security",       # хэш как checksum/id, не для пароля/подписи
    "restricted_deserialization",  # десериализация ограничена доверенным форматом/источником
    "redirect_allowlist",          # redirect проверяется по allowlist
    "network_allowlist",           # исходящий запрос ограничен сетевым allowlist
    "safe_argv",                   # фиксированный/провалидированный argv, без shell
    "path_normalized",             # путь нормализован + containment-проверка (realpath/commonpath)
}

STATUS = {"pass", "fail", "pending_human"}
VERDICTS = {"confirmed", "disproved", "uncertain"}


def _corroborate(proof_type, code):
    """Детерминированная сверка заявленного доказательства безопасности с кодом фрагмента.
    Возвращает: False — доказательство ОПРОВЕРГНУТО кодом (галлюцинация verifier'а);
    True — доказательство подтверждено; None — детерминированно не проверяемо (доверяем структуре).
    Асимметрия: может ТОЛЬКО опровергнуть (→ human) или подтвердить, но не «придумать» дефект."""
    c = code or ""
    if proof_type == "parameterized_query":
        # опровергаем, если в execute(...) интерполяция переменных в строку SQL
        if re.search(r"execute\(\s*f[\"']", c) or re.search(r"execute\(\s*[\"'][^\"']*[\"']\s*[%+]", c) \
                or re.search(r"execute\(\s*[\"'].*\{.*\}.*[\"']\.format", c) \
                or re.search(r"execute\(\s*[\"'][^\"']*[\"']\s*\.format", c):
            return False
        if re.search(r"execute\(\s*[\"'][^\"']*[?]|execute\(\s*[\"'][^\"']*%s", c) and re.search(r",\s*\(", c):
            return True
        return None
    if proof_type == "checksum_not_security":
        # опровергаем, если md5/sha1 участвует в пароле/секрете/подписи
        if re.search(r"(md5|sha1)\s*\(", c, re.I) and re.search(
                r"(password|passwd|\bpwd\b|secret|token|signature|\bsign\b|hmac)", c, re.I):
            return False
        if re.search(r"(md5|sha1)\s*\(", c, re.I):
            return True
        return None
    if proof_type == "safe_argv":
        if re.search(r"shell\s*=\s*True", c):
            return False
        if re.search(r"subprocess\.(run|call|Popen|check_output)\(\s*\[", c):
            return True
        return None
    if proof_type == "restricted_deserialization":
        # опровергаем, если pickle/yaml.load на явно пользовательских данных
        if re.search(r"pickle\.loads?\(", c) and re.search(r"(user|request|payload|body|untrusted|input)", c, re.I):
            return False
        return None
    return None  # прочие доказательства детерминированно не проверяем — доверяем структуре (residual risk)


def _finding_id(f, i):
    return str(f.get("id") or f.get("finding_id") or f"finding-{i + 1}")


def reduce_verdicts(findings, verifications, code, corroborator=_corroborate):
    """Чистый детерминированный reducer. findings: [{id,defect,...}]; verifications:
    {finding_id: {verdict, counter_evidence:{proof_type, cited_lines}, ...}}. Fail-closed."""
    blockers, pending, downgraded = [], [], []
    for i, f in enumerate(findings):
        fid = _finding_id(f, i)
        vr = verifications.get(fid) or {}
        verdict = str(vr.get("verdict", "")).lower()
        ce = vr.get("counter_evidence") or {}
        proof = ce.get("proof_type")
        cites = ce.get("cited_lines")
        if verdict == "confirmed":
            blockers.append(fid)
        elif verdict == "disproved":
            # безопасный pass ТОЛЬКО при распознанном доказательстве + цитатах + не-опровергнутой корроборации
            if proof in RECOGNIZED_PROOFS and cites:
                corr = corroborator(proof, code)
                if corr is False:
                    pending.append(fid)          # доказательство опровергнуто кодом → human (fail-closed)
                else:
                    downgraded.append(fid)        # corr True/None → безопасный даунгрейд
            else:
                pending.append(fid)               # неструктурное «disproved» не доверяем → human
        else:
            pending.append(fid)                   # uncertain / пусто / мусор → human
    if blockers:
        status = "fail"
    elif pending:
        status = "pending_human"
    else:
        status = "pass"                            # либо findings нет, либо все безопасно даунгрейднуты
    return {"status": status, "blockers": blockers, "pending": pending, "downgraded_findings": downgraded}


def run_cascade(code, tested_sha, detector, verifier, *, corroborator=_corroborate,
                detector_meta=None, verifier_meta=None):
    """Полный каскад. detector(code)->{findings:[...]} | None(schema-fail). verifier(code, finding)->
    {verdict, exploit_path, counter_evidence:{proof_type, detail, cited_lines}} | None. Возвращает
    SecurityCascadeResult. Fail-closed: любой schema-fail detector'а/verifier'а → pending_human (не pass)."""
    schema_ok = True
    det = detector(code)
    if not isinstance(det, dict) or not isinstance(det.get("findings"), list):
        # detector не дал валидного вывода — доверять «нет дефектов» нельзя → human
        return {"kind": "SecurityCascadeResult", "tested_sha": tested_sha,
                "primary": {**(detector_meta or {}), "findings": []},
                "verification": {**(verifier_meta or {}), "results": []},
                "final": {"status": "pending_human", "blockers": [], "pending": ["<detector-schema-fail>"],
                          "downgraded_findings": []},
                "schema_valid": False}
    findings = det["findings"]
    verifications, results = {}, []
    for i, f in enumerate(findings):
        fid = _finding_id(f, i)
        vr = None
        try:
            vr = verifier(code, f)
        except Exception:  # noqa: BLE001
            vr = None
        if not isinstance(vr, dict) or str(vr.get("verdict", "")).lower() not in VERDICTS:
            schema_ok = False
            vr = {"verdict": "uncertain", "exploit_path": None,
                  "counter_evidence": {}, "note": "verifier-schema-fail"}  # fail-closed → pending
        verifications[fid] = vr
        results.append({"finding_id": fid, "verdict": str(vr.get("verdict", "")).lower(),
                        "exploit_path": vr.get("exploit_path"),
                        "counter_evidence": vr.get("counter_evidence") or {}})
    red = reduce_verdicts(findings, verifications, code, corroborator)
    return {"kind": "SecurityCascadeResult", "tested_sha": tested_sha,
            "primary": {**(detector_meta or {}), "findings": findings},
            "verification": {**(verifier_meta or {}), "results": results},
            "final": {"status": red["status"], "blockers": red["blockers"],
                      "pending": red["pending"], "downgraded_findings": red["downgraded_findings"]},
            "schema_valid": schema_ok}


def check(res):
    """Структурная валидация SecurityCascadeResult."""
    e = []
    if not isinstance(res, dict) or res.get("kind") != "SecurityCascadeResult":
        return ["kind должен быть SecurityCascadeResult"]
    if not res.get("tested_sha"):
        e.append("нет tested_sha (результат обязан быть привязан к SHA)")
    fin = res.get("final")
    if not isinstance(fin, dict):
        e.append("нет final")
    else:
        if fin.get("status") not in STATUS:
            e.append(f"final.status ∉ {sorted(STATUS)}")
        for k in ("blockers", "pending", "downgraded_findings"):
            if not isinstance(fin.get(k), list):
                e.append(f"final.{k} должен быть списком")
        # инвариант fail-closed: fail только при blockers; pass — без blockers и без pending
        if fin.get("status") == "fail" and not fin.get("blockers"):
            e.append("status=fail без blockers (не fail-closed)")
        if fin.get("status") == "pass" and (fin.get("blockers") or fin.get("pending")):
            e.append("status=pass при наличии blockers/pending (нарушение fail-closed)")
        if fin.get("status") == "pending_human" and not fin.get("pending") and not fin.get("blockers"):
            e.append("status=pending_human без pending (нет причины)")
    for side in ("primary", "verification"):
        if not isinstance(res.get(side), dict):
            e.append(f"нет {side}")
    return e


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    SHA = "deadbeef"

    def det_none(_):  # detector нашёл 0 findings
        return {"findings": []}

    def det_one(defect="x", fid="f1"):
        return lambda _: {"findings": [{"id": fid, "defect": defect}]}

    def ver(verdict, proof=None, cites=None):
        def _v(_code, _f):
            ce = {}
            if proof is not None:
                ce = {"proof_type": proof, "detail": "d", "cited_lines": cites or [1]}
            return {"verdict": verdict, "exploit_path": "e" if verdict == "confirmed" else None,
                    "counter_evidence": ce}
        return _v

    # 1) нет findings → pass
    r = run_cascade("safe code", SHA, det_none, ver("disproved"))
    expect("нет findings → pass", r["final"]["status"] == "pass" and check(r) == [])

    # 2) confirmed → fail
    r = run_cascade("bad", SHA, det_one(), ver("confirmed"))
    expect("verifier confirmed → fail", r["final"]["status"] == "fail" and r["final"]["blockers"] == ["f1"])

    # 3) disproved + распознанное доказательство + корроборация True → pass (даунгрейд)
    code_param = "conn.execute('SELECT * FROM u WHERE n=?', (name,))"
    r = run_cascade(code_param, SHA, det_one(), ver("disproved", "parameterized_query", [1]))
    expect("disproved + parameterized_query (corr True) → pass",
           r["final"]["status"] == "pass" and r["final"]["downgraded_findings"] == ["f1"])

    # 4) disproved БЕЗ распознанного доказательства → pending_human (fail-closed)
    r = run_cascade("bad", SHA, det_one(), ver("disproved", proof=None))
    expect("disproved без структурного доказательства → pending_human",
           r["final"]["status"] == "pending_human" and "f1" in r["final"]["pending"])

    # 5) КЛЮЧЕВОЙ: verifier ВРЁТ disproved parameterized_query, но код — f-string SQL → корроборация
    #    опровергает → pending_human, НЕ pass. Реальный дефект не превращается в false green.
    code_fstring = "conn.execute(f\"SELECT * FROM u WHERE n='{name}'\")"
    r = run_cascade(code_fstring, SHA, det_one(defect="sqli"), ver("disproved", "parameterized_query", [1]))
    expect("disproved с ОПРОВЕРГНУТЫМ доказательством → pending_human (не false green)",
           r["final"]["status"] == "pending_human" and "f1" in r["final"]["pending"])

    # 6) uncertain → pending_human
    r = run_cascade("bad", SHA, det_one(), ver("uncertain"))
    expect("uncertain → pending_human", r["final"]["status"] == "pending_human")

    # 7) detector schema-fail → pending_human (не pass)
    r = run_cascade("bad", SHA, lambda _: None, ver("disproved"))
    expect("detector schema-fail → pending_human (fail-closed)",
           r["final"]["status"] == "pending_human" and r["schema_valid"] is False)

    # 8) verifier schema-fail на finding → pending_human
    r = run_cascade("bad", SHA, det_one(), lambda _c, _f: {"verdict": "garbage"})
    expect("verifier schema-fail → pending_human", r["final"]["status"] == "pending_human")

    # 9) корроборация checksum_not_security: md5 на пароле → опровергнуто → pending
    code_pw = "return hashlib.md5(password.encode()).hexdigest()"
    r = run_cascade(code_pw, SHA, det_one(defect="weak-crypto"),
                    ver("disproved", "checksum_not_security", [1]))
    expect("md5-на-пароле, заявлен checksum → опровергнуто → pending_human",
           r["final"]["status"] == "pending_human")

    # 10) корроборация safe_argv: shell=True → опровергнуто → pending
    code_shell = "subprocess.run('ping ' + host, shell=True)"
    r = run_cascade(code_shell, SHA, det_one(defect="cmdi"), ver("disproved", "safe_argv", [1]))
    expect("shell=True, заявлен safe_argv → опровергнуто → pending_human",
           r["final"]["status"] == "pending_human")

    # 11) множественные findings: один confirmed → весь fail (blocker доминирует над downgrade)
    def det_two(_):
        return {"findings": [{"id": "a", "defect": "x"}, {"id": "b", "defect": "y"}]}

    def ver_mixed(_code, f):
        if f["id"] == "a":
            return {"verdict": "disproved", "counter_evidence": {"proof_type": "not_a_secret", "cited_lines": [1]}}
        return {"verdict": "confirmed", "exploit_path": "e", "counter_evidence": {}}
    r = run_cascade("x", SHA, det_two, ver_mixed)
    expect("blocker доминирует: один confirmed при одном downgrade → fail",
           r["final"]["status"] == "fail" and r["final"]["blockers"] == ["b"])

    # check() отвергает несогласованный результат
    expect("check: pass с blockers → ошибка",
           any("fail-closed" in x for x in check({"kind": "SecurityCascadeResult", "tested_sha": "s",
               "primary": {}, "verification": {},
               "final": {"status": "pass", "blockers": ["z"], "pending": [], "downgraded_findings": []}})))

    print("security_review_cascade selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
