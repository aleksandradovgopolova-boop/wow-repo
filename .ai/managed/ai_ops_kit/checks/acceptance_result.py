"""Чистая проверка формы acceptance-result (вердикт независимого ревьюера приёмки). Вынесена из
`validation/validate_acceptance_result.py` вниз (лента №5), чтобы движок (acceptance_verify) звал
её ВНИЗ, без восходящего ребра engine -> validation.

Инварианты формы: criteria — непустой список {id, status ∈ met|unmet|undetermined}; met требует
quote+source; evidence ∈ present|absent, absent требует quote+source; unmet/undetermined требуют
reason или quote; охват сверяется с criterion_ids (None = «охват не проверяется»). Только stdlib.
"""
from __future__ import annotations

CRITERION_STATUS = {"met", "unmet", "undetermined"}
# Вид основания (второе ревью PR #118): `present` — цитата ЕСТЬ в результате; `absent` — её В ФАЙЛЕ
# НЕТ, и это доказательство для критериев об отсутствии. Второй вид требует source по построению:
# отсутствие подтверждается только чтением файла, «нигде не нашёл» доказательством не является.
EVIDENCE_KINDS = {"present", "absent"}


def check(data: dict, criterion_ids=None) -> list:
    """Ошибки формы вердикта. criterion_ids — объявленные id (None = «не знаю, не проверяю охват»).

    Различие None и пустого множества здесь такое же, как в `reviewer_result` / `_gate_ids`:
    «охват не проверялся» и «критериев нет» — разные факты, и путать их дороже, чем передать None.
    """
    errors = []
    if not isinstance(data, dict):
        return ["результат не является объектом"]
    if data.get("schema_version") is None:
        errors.append("нет schema_version")
    if data.get("kind") != "acceptance-result":
        errors.append("kind должен быть 'acceptance-result'")

    crits = data.get("criteria")
    if not isinstance(crits, list) or not crits:
        errors.append("criteria должен быть непустым списком")
        return errors

    seen = []
    for c in crits:
        if not isinstance(c, dict) or not c.get("id"):
            errors.append("критерий требует id:str")
            continue
        cid = str(c["id"])
        seen.append(cid)
        st = c.get("status")
        if st not in CRITERION_STATUS:
            errors.append(f"{cid}: status '{st}' не в {sorted(CRITERION_STATUS)}")
            continue
        quote = str(c.get("quote") or "").strip()
        source = str(c.get("source") or "").strip()
        reason = str(c.get("reason") or "").strip()
        if st == "met" and not quote:
            errors.append(f"{cid}: status=met без quote — «выполнен» без основания не проверяем")
        if st == "met" and not source:
            errors.append(f"{cid}: status=met без source — непонятно, где искать цитату")
        ev = str(c.get("evidence") or "present").strip().lower()
        if ev not in EVIDENCE_KINDS:
            errors.append(f"{cid}: evidence '{c.get('evidence')}' не в {sorted(EVIDENCE_KINDS)}")
        elif ev == "absent" and not (quote and source):
            errors.append(f"{cid}: evidence=absent требует quote и source — отсутствие "
                          f"подтверждается только чтением файла")
        if st in ("unmet", "undetermined") and not (reason or quote):
            errors.append(f"{cid}: status={st} требует reason или quote (вердикт без причины)")

    dupes = sorted({i for i in seen if seen.count(i) > 1})
    if dupes:
        errors.append(f"дубли вердиктов по критериям: {', '.join(dupes)}")
    if criterion_ids is not None:
        declared = {str(i) for i in criterion_ids}
        got = set(seen)
        missing = sorted(declared - got)
        extra = sorted(got - declared)
        if missing:
            errors.append(f"нет вердикта по критериям: {', '.join(missing)} — сверка неполна")
        if extra:
            errors.append(f"вердикт по необъявленным критериям: {', '.join(extra)}")
    return errors
