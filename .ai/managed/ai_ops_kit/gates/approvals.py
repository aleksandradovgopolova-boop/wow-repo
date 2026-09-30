#!/usr/bin/env python3
"""ApprovalRecord + доменные human_approval_conditions (v2.115 Preflight Truth).

Аудит: human-approval был обычным boolean (`signals.human_approved`), который технически мог передать
любой вызывающий код, без автора/времени/scope/revision, и доменные `human_approval_conditions`
security-доменов НЕ исполнялись. Здесь — настоящая запись одобрения и её проверка.

ApprovalRecord (features/<wid>/approvals/*.yaml):
  kind: ApprovalRecord
  approval: <domain_id>          # secrets | dependencies | authentication | ...
  approved_by: user@example.com  # автор (не пусто)
  scope: package.json            # что именно одобрено
  revision: <sha|->              # к какой ревизии относится
  created_at: 2026-07-18T..
  reason: <зачем>                # обоснование (не пусто)

Проверка (детерминированно, по сигналам — до правок): для каждого security-домена, чей триггер-сигнал
взведён, требуется валидный ApprovalRecord для этого домена. Нет записи -> preflight блокирует
(человек не пройден). boolean `human_approved` больше НЕ достаточен для доменных условий.

Использование:
  approvals.py require --signals '{...}'                 # какие одобрения нужны
  approvals.py check <child_root> <wid> --signals '{...}'# что есть/чего не хватает
  approvals.py record <child_root> <wid> --approval secrets --by u@x --scope f --reason r [--revision sha]
  approvals.py --selftest
"""
from __future__ import annotations

# v4: самодостаточный вход — файл можно запустить напрямую (без PYTHONPATH). Кладём корень пакета
# (маркер VERSION) в sys.path ДО пакетных импортов — раньше это делал плоский shim tools/ через
# _bootstrap; теперь точка входа сама себя обслуживает.
import sys as _sys
from pathlib import Path as _P_bootstrap
_root = next((_p for _p in _P_bootstrap(__file__).resolve().parents if (_p / "VERSION").is_file()), None)
if _root is not None and str(_root) not in _sys.path:
    _sys.path.insert(0, str(_root))

import argparse
import json
import sys
from pathlib import Path

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
# Домен -> сигналы-триггеры, которые ДЕТЕРМИНИРОВАННО (по сигналам, до диффа) делают домен применимым
# и требующим человеко-одобрения. Для secrets/dependencies applicability.signals в доменах пуст (они
# ловятся по содержимому диффа), поэтому им заданы явные сигналы намерения.
_EXPLICIT_TRIGGERS = {
    "secrets": ["secret_boundary"],
    "dependencies": ["dependency_addition", "dependency_added", "new_dependency"],
}


def load_domains():
    import yaml
    d = yaml.safe_load((PKG / "security" / "security-domains.yaml").read_text(encoding="utf-8"))
    doms = d.get("domains") if isinstance(d, dict) else d
    return doms or []


def _domain_triggers(dom):
    """Сигналы, взводящие требование одобрения для домена: applicability.signals + явные триггеры."""
    sigs = list(((dom.get("applicability") or {}).get("signals")) or [])
    sigs += _EXPLICIT_TRIGGERS.get(dom["id"], [])
    return sigs


def signals_from_findings(security_pack_result):
    """v2.123 (P0.2): вывести триггер-сигналы одобрения из РЕАЛЬНЫХ находок security pack (пост-дифф),
    даже если во ВХОДНЫХ signals их не было. Новая зависимость/секрет, добавленные самой правкой, обязаны
    требовать ApprovalRecord — иначе независимый reviewer мог бы закрыть их без человека. -> {signal: True}."""
    sigs = {}
    _map = {"new_dependency": "dependency_addition", "secret": "secret_boundary",
            "auth_change": "auth_change", "deploy_change": "deploy_change"}
    for r in (security_pack_result or {}).get("results", []) or []:
        for f in (r.get("findings") or []):
            sig = _map.get(f.get("type"))
            if sig:
                sigs[sig] = True
    return sigs


def required_approvals(signals, domains=None):
    """-> [{domain, condition, trigger}] для доменов с human_approval_conditions, чей триггер взведён."""
    signals = dict(signals or {})
    domains = domains if domains is not None else load_domains()
    out = []
    for dom in domains:
        ha = dom.get("human_approval_conditions")
        if not ha:
            continue
        trigger = next((s for s in _domain_triggers(dom) if signals.get(s)), None)
        if trigger:
            out.append({"domain": dom["id"], "condition": ha[0], "trigger": trigger})
    return out


def _approvals_dir(child_root, wid):
    return Path(child_root) / "features" / str(wid) / "approvals"


# ── v2.121 (P1.2): связывание ApprovalRecord ─────────────────────────────────────────────────────
# Аудит: одобрение было «слабо связано» — валидная запись требовала лишь непустых author/scope/reason,
# поэтому одобрение, выданное для одного плана/ревизии, автоматически покрывало ЛЮБЫЕ последующие
# изменения. Теперь запись связывается с: (1) хэшем плана/спеки (binds_to) — если план меняется после
# одобрения, запись перестаёт покрывать новую ревизию; (2) сроком (expires_at) — просроченное одобрение
# невалидно; (3) scope, который после диффа обязан покрывать реально изменённые пути (recheck_after_diff).
# v2.121 связывание было АДДИТИВНЫМ: старые записи без binds_to валидировались как раньше.
# v3.37 это отменено — привязка к содержимому БЕЗУСЛОВНА (повод: RR-014/EV-1105, у соседа по жанру
# она required по схеме для любого одобрения, у нас была привилегией high-risk доменов). Запись без
# `binds_to` невалидна; несвязываемая запись не создаётся вовсе (write_record бросает). Цена решения
# названа: legacy-записи без binds_to перестают проходить проверку и требуют перезаписи — это
# осознанный fail-closed, а не регрессия. Срок (expires_at), риск (risk) и доверенный source
# по-прежнему обязательны только для high-risk доменов: их цена выше, и они решаются отдельно.

def plan_binding_hash(child_root, wid):
    """Стабильный хэш плана+спеки WorkItem — то, к чему привязывается одобрение. None, если привязывать
    не к чему (нет ни run-plan.yaml, ни spec.yaml)."""
    import hashlib
    fdir = Path(child_root) / "features" / str(wid)
    blob = b""
    for name in ("run-plan.yaml", "spec.yaml"):
        p = fdir / name
        if p.is_file():
            blob += name.encode() + b"\0" + p.read_bytes() + b"\n"
    if not blob:
        return None
    return hashlib.sha256(blob).hexdigest()[:16]


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso(s):
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _is_expired(rec, now):
    """True, если у записи есть expires_at и он в прошлом относительно now (обе метки парсятся)."""
    exp = rec.get("expires_at")
    if not exp or not now:
        return False
    ed, nd = _parse_iso(exp), _parse_iso(now)
    return bool(ed and nd and nd > ed)


def covers_paths(rec, changed_paths):
    """Scope одобрения покрывает изменённые пути? scope трактуется как список путей/префиксов (через
    запятую/пробел/перевод строки). Пустой changed_paths -> покрывает (нечего проверять). Каждый путь
    должен попасть под хотя бы один префикс scope; иначе одобрение не покрывает реальные изменения."""
    paths = [p for p in (changed_paths or []) if p]
    if not paths:
        return True
    raw = str(rec.get("scope") or "")
    prefixes = [s.strip() for s in raw.replace(",", " ").replace("\n", " ").split(" ") if s.strip()]
    if not prefixes:
        return False
    def under(path):
        return any(path == pre or path.startswith(pre.rstrip("/") + "/") or pre == "." for pre in prefixes)
    return all(under(p) for p in paths)


def dep_fingerprint(finding):
    """v3.0-rc5 (P1.2): стабильный отпечаток находки новой зависимости (manifest:package@version:op)."""
    return (f"{finding.get('manifest')}:{(finding.get('name') or '').lower()}"
            f"@{finding.get('version') or '*'}:{finding.get('operation', 'add')}")


def covers_dependency(rec, finding):
    """v3.0-rc5 (P1.2): approval покрывает КОНКРЕТНУЮ зависимость? Нужен covers_packages, называющий имя
    пакета (и версию, если approval её фиксирует '<name>@<ver>'). Path-only approval (без covers_packages)
    supply-chain НЕ покрывает — иначе одобрение одной зависимости покрыло бы любую в том же манифесте."""
    covers = rec.get("covers_packages") or []
    if isinstance(covers, str):
        covers = [c.strip() for c in covers.replace(",", " ").split() if c.strip()]
    if not covers:
        return False
    nm = (finding.get("name") or "").lower()
    ver = finding.get("version")
    for c in covers:
        cname, _, cver = str(c).strip().lower().partition("@")
        if cname == nm and (not cver or cver == "*" or not ver or cver == str(ver).lower()):
            return True
    return False


def recheck_dependencies(child_root, wid, dep_findings, now=None, plan_hash=None, domains=None):
    """v3.0-rc5 (P1.2): КАЖДАЯ новая зависимость из диффа должна покрываться валидным ApprovalRecord
    dependencies с covers_packages, называющим ИМЕННО этот пакет. -> {ok, uncovered[]}."""
    recs = [r for r in load_approvals(child_root, wid) if r.get("approval") == "dependencies"]
    now = now or _now_iso()
    plan_hash = plan_hash if plan_hash is not None else plan_binding_hash(child_root, wid)
    domains_list = domains if domains is not None else load_domains()
    strict = _is_high_risk("dependencies", domains_list)
    valid = [r for r in recs if _record_valid(r, now=now, plan_hash=plan_hash, strict=strict)]
    uncovered = []
    for f in (dep_findings or []):
        if not any(covers_dependency(r, f) for r in valid):
            uncovered.append({"domain": "dependencies", "fingerprint": dep_fingerprint(f), "name": f.get("name"),
                              "reason": "нет ApprovalRecord dependencies с covers_packages для этого пакета"})
    return {"ok": not uncovered, "uncovered": uncovered}


def load_approvals(child_root, wid):
    """Прочитать все ApprovalRecord из features/<wid>/approvals/*.yaml. -> [record]."""
    import yaml
    d = _approvals_dir(child_root, wid)
    recs = []
    if d.is_dir():
        for p in sorted(d.glob("*.yaml")):
            try:
                r = yaml.safe_load(p.read_text(encoding="utf-8"))
                if isinstance(r, dict) and r.get("kind") == "ApprovalRecord":
                    recs.append(r)
            # Причина подавления (срез ратчета, 2026-08-12): FAIL-CLOSED и он верен — битая
            # запись НЕ становится одобрением. Тип широк намеренно: любой отказ чтения обязан
            # дать «одобрения нет», а не исключение наружу посреди проверки прав.
            except Exception:  # noqa: BLE001,S110 — битая запись не считается одобрением
                pass
    return recs


# ── v2.123 (Approval schema v2): для HIGH-RISK доменов legacy-записи без binding БОЛЬШЕ НЕ принимаются ──
# high-risk = домен severity high/critical (secrets/auth/authz/deploy/data-isolation/...) + destructive.
# Обязательны: binds_to (привязка к плану), expires_at (срок), risk, source ∈ доверенный контекст
# (не произвольная строка модели). medium/low домены (напр. dependencies) остаются аддитивными.
_TRUSTED_SOURCES = {"user", "ci", "human"}


def _is_high_risk(domain_id, domains=None):
    """Домен требует schema v2 (binding)? severity high/critical среди security-доменов; destructive — всегда."""
    if domain_id == "destructive":
        return True
    for d in (domains if domains is not None else load_domains()):
        if d.get("id") == domain_id:
            return ((d.get("severity_policy") or {}).get("default")) in ("high", "critical")
    return False


def _record_reason_invalid(rec, now=None, plan_hash=None, strict=False):
    """Причина невалидности ApprovalRecord или None, если валиден.

    v2.121: срок (expires_at) и привязка (binds_to) проверялись аддитивно — при наличии поля.
    v2.123: strict=True (high-risk домен) требовал полный binding.

    v3.37 — ПРИВЯЗКА К СОДЕРЖИМОМУ СТАЛА БЕЗУСЛОВНОЙ. Повод — RR-014/EV-1105: у соседа по жанру
    (mestre-yoda) связывание одобрения с дайджестами обязательно по схеме для ЛЮБОГО одобрения, у
    нас же оно было привилегией high-risk доменов, а остальным «проверялось при наличии поля».
    Одобрение без привязки не отвечает на вопрос «что именно одобрено»: оно переживает любую
    правку плана и спеки, то есть человек одобрил один текст, а исполняется другой.

    Две проверки, и вторая не менее важна первой:
      * `binds_to` обязателен ВСЕГДА — записи без него невалидны (см. подсказку в сообщении);
      * привязка обязана быть СВЕРЯЕМОЙ: если сверять не с чем (`plan_hash` не передан или на
        диске нет ни run-plan.yaml, ни spec.yaml), запись отклоняется. Прежде такой вызов молча
        пропускал запись — поле есть, сверка не выполнялась (реальный путь: preflight звал
        `_record_valid(r)` без plan_hash для destructive).
    """
    if not (bool(rec.get("approval")) and bool(rec.get("approved_by"))
            and bool(rec.get("scope")) and bool(rec.get("reason"))):
        return "нужны approved_by/scope/reason"
    if _is_expired(rec, now):
        return f"одобрение просрочено (expires_at={rec.get('expires_at')})"
    if not rec.get("binds_to"):
        return ("одобрение не привязано к содержимому (нет binds_to): нельзя доказать, ЧТО именно "
                "одобрено. Перезапишите одобрение — `approvals.py record … --bind-to-plan`")
    if not plan_hash:
        return ("привязку одобрения не с чем сверить (нет хэша плана/спеки): при отсутствии "
                "run-plan.yaml и spec.yaml одобрять нечего, а вызов без plan_hash не проверяет "
                "привязку вовсе")
    if rec.get("binds_to") != plan_hash:
        return "одобрение выдано для другой ревизии плана/спеки (binds_to не совпадает)"
    if strict:
        missing = [f for f in ("binds_to", "expires_at", "risk") if not rec.get(f)]
        if missing:
            return (f"high-risk одобрение требует связывания (schema v2): нет {', '.join(missing)} "
                    f"(legacy-запись без binding не принимается)")
        src = str(rec.get("source") or "")
        if src not in _TRUSTED_SOURCES:
            return (f"high-risk одобрение требует source из доверенного контекста {sorted(_TRUSTED_SOURCES)} "
                    f"(получено: '{src or 'нет'}') — identity не из произвольной строки модели")
    return None


def _record_valid(rec, now=None, plan_hash=None, strict=False):
    """ApprovalRecord валиден: базовые поля + не просрочен + (если связан) привязан + (strict) полный binding."""
    return _record_reason_invalid(rec, now=now, plan_hash=plan_hash, strict=strict) is None


def check(signals, child_root, wid, domains=None, now=None, plan_hash=None):
    """-> {ok, required[], satisfied[], missing[], records_seen}. missing непуст -> человек не пройден.
    v2.121: одобрение проверяется на срок и привязку к текущему плану (plan_hash берётся с диска, now —
    реальное время, если не переданы явно — для детерминизма тестов можно передать)."""
    domains_list = domains if domains is not None else load_domains()
    req = required_approvals(signals, domains=domains_list)
    recs = load_approvals(child_root, wid)
    now = now or _now_iso()
    plan_hash = plan_hash if plan_hash is not None else plan_binding_hash(child_root, wid)
    satisfied, missing = [], []
    for r in req:
        strict = _is_high_risk(r["domain"], domains_list)   # v2.123: high-risk -> требуется binding
        rec = next((rc for rc in recs if rc.get("approval") == r["domain"]), None)
        if rec is not None and _record_valid(rec, now=now, plan_hash=plan_hash, strict=strict):
            satisfied.append(r)
        else:
            why = (_record_reason_invalid(rec, now=now, plan_hash=plan_hash, strict=strict) if rec
                   else None)
            missing.append({**r, "reason": (f"ApprovalRecord есть, но невалиден: {why}" if why
                                            else "нет валидного ApprovalRecord")})
    return {"ok": not missing, "required": req, "satisfied": satisfied,
            "missing": missing, "records_seen": len(recs)}


def recheck_after_diff(child_root, wid, changed_paths, signals=None, domains=None, now=None, plan_hash=None):
    """v2.121 (P1.2, п.4): ПОСЛЕ диффа перепроверить, что одобрение покрывает РЕАЛЬНО изменённые пути.
    -> {ok, uncovered[]}. Для каждого требуемого домена с валидной записью scope обязан покрыть
    changed_paths; иначе домен попадает в uncovered (одобрено не то, что изменилось)."""
    domains_list = domains if domains is not None else load_domains()
    req = required_approvals(signals or {}, domains=domains_list)
    recs = load_approvals(child_root, wid)
    now = now or _now_iso()
    plan_hash = plan_hash if plan_hash is not None else plan_binding_hash(child_root, wid)
    by_domain = {rc["approval"]: rc for rc in recs
                 if _record_valid(rc, now=now, plan_hash=plan_hash,
                                  strict=_is_high_risk(rc.get("approval"), domains_list))}
    uncovered = []
    for r in req:
        rec = by_domain.get(r["domain"])
        if rec is not None and not covers_paths(rec, changed_paths):
            uncovered.append({"domain": r["domain"], "scope": rec.get("scope"),
                              "changed": list(changed_paths or []),
                              "reason": "scope одобрения не покрывает изменённые пути"})
    return {"ok": not uncovered, "uncovered": uncovered}


def write_record(child_root, wid, approval, approved_by, scope, reason, revision="-", created_at=None,
                 binds_to=None, expires_at=None, risk=None, bind_to_plan=False, source=None,
                 covers_packages=None):
    """Создать ApprovalRecord на диске (features/<wid>/approvals/<approval>.yaml). created_at обязателен
    в проде (передаётся вызывающим — детерминированность/отсутствие скрытого времени). v2.121: связать с
    планом (binds_to или bind_to_plan=True -> хэш с диска), сроком (expires_at) и типом риска (risk)."""
    import yaml
    d = _approvals_dir(child_root, wid)
    d.mkdir(parents=True, exist_ok=True)
    # v3.37: привязка безусловна (см. _record_reason_invalid), поэтому связываем ВСЕГДА, а не только
    # по флагу bind_to_plan — иначе запись создавалась бы заведомо невалидной. Флаг оставлен ради
    # совместимости вызовов, но на поведение больше не влияет.
    if binds_to is None:
        binds_to = plan_binding_hash(child_root, wid)
    if not binds_to:
        # Отказ на ЗАПИСИ, а не на проверке: несвязываемое одобрение нельзя сохранить, иначе на диске
        # появится запись, которая никогда не пройдёт проверку, и человек узнает об этом позже и в
        # другом месте. Если плана и спеки нет — одобрять пока нечего.
        raise ValueError(
            f"ApprovalRecord '{approval}' нечем связать: в features/{wid}/ нет ни run-plan.yaml, "
            f"ни spec.yaml. Одобрение обязано ссылаться на содержимое (v3.37) — сначала план/спека, "
            f"потом одобрение")
    rec = {"schema_version": 1, "kind": "ApprovalRecord", "approval": approval,
           "approved_by": approved_by, "scope": scope, "revision": revision,
           "created_at": created_at or "unspecified", "reason": reason}
    if binds_to:
        rec["binds_to"] = binds_to
    if expires_at:
        rec["expires_at"] = expires_at
    if risk:
        rec["risk"] = risk
    if source:                       # v2.123: доверенный источник identity (user|ci|human), не модель
        rec["source"] = source
    if covers_packages:              # v3.0-rc5 (P1.2): какие пакеты покрывает (supply-chain fingerprint)
        rec["covers_packages"] = list(covers_packages)
    p = d / f"{approval}.yaml"
    # v3.0.14 (finding аудита #2): ApprovalRecord — человеческий источник истины для high-risk; пишем
    # DURABLE (атомарно+fsync+перечитывание), а не plain write_text (частичная запись = потеря одобрения).
    from ai_ops_kit.shared import lifecycle_store as _ls
    _r = _ls.durable_write(p, rec, require_keys=("approval", "approved_by", "scope"))
    if not _r.get("ok"):
        raise OSError(f"не удалось надёжно сохранить ApprovalRecord {p}: {_r.get('error')}")
    return p


def main(argv):
    ap = argparse.ArgumentParser(prog="approvals.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rq = sub.add_parser("require"); rq.add_argument("--signals", default="{}"); rq.add_argument("--json", action="store_true")
    ck = sub.add_parser("check"); ck.add_argument("child_root"); ck.add_argument("wid")
    ck.add_argument("--signals", default="{}"); ck.add_argument("--json", action="store_true")
    rc = sub.add_parser("record"); rc.add_argument("child_root"); rc.add_argument("wid")
    rc.add_argument("--approval", required=True); rc.add_argument("--by", required=True)
    rc.add_argument("--scope", required=True); rc.add_argument("--reason", required=True)
    rc.add_argument("--revision", default="-"); rc.add_argument("--created-at")
    rc.add_argument("--expires-at", help="срок действия одобрения (ISO 8601); после — невалидно")
    rc.add_argument("--risk", help="тип риска, который покрывает одобрение (обязателен для high-risk)")
    rc.add_argument("--source", help="доверенный источник identity: user|ci|human (обязателен для high-risk)")
    rc.add_argument("--package", action="append", default=None,
                    help="dependencies: пакет(ы), которые покрывает одобрение (имя или name@version); "
                         "повторяемо. Без него supply-chain approval не покроет конкретную зависимость")
    rc.add_argument("--bind-to-plan", action="store_true",
                    help="привязать к хэшу текущего run-plan.yaml+spec.yaml (при смене плана — невалидно)")
    a = ap.parse_args(argv)
    if a.cmd == "require":
        req = required_approvals(json.loads(a.signals))
        print(json.dumps(req, ensure_ascii=False, indent=2) if a.json
              else ("нужны одобрения: " + (", ".join(r["domain"] for r in req) or "нет")))
        return 0
    if a.cmd == "check":
        res = check(json.loads(a.signals), Path(a.child_root), a.wid)
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"APPROVALS: ok={res['ok']} · требуется {len(res['required'])} · не хватает {len(res['missing'])}")
            for m in res["missing"]:
                print(f"  ✗ {m['domain']}: {m['reason']} (условие: {m['condition']})")
        return 0 if res["ok"] else 1
    if a.cmd == "record":
        # v2.123: high-risk домен -> запись обязана быть связанной (schema v2), требуем risk+source,
        # иначе запись будет невалидна при проверке (сообщаем сразу, не молча).
        # v3.37: привязка к содержимому безусловна, поэтому `bind` больше не вычисляется — связывает
        # сам write_record и отказывает, если связывать не с чем.
        high_risk = _is_high_risk(a.approval)
        if high_risk and (not a.risk or (a.source or "") not in _TRUSTED_SOURCES):
            print(f"APPROVAL-RECORD: домен '{a.approval}' high-risk (schema v2) — обязательны --risk и "
                  f"--source из {sorted(_TRUSTED_SOURCES)}; запись без них будет отклонена при проверке.")
            return 2
        try:
            p = write_record(Path(a.child_root), a.wid, a.approval, a.by, a.scope, a.reason,
                             revision=a.revision, created_at=a.created_at,
                             expires_at=a.expires_at, risk=a.risk, source=a.source,
                             covers_packages=a.package)
        except ValueError as e:                      # нечем связать -> говорим прямо, не пишем мусор
            print(f"APPROVAL-RECORD: {e}")
            return 2
        print(f"APPROVAL-RECORD: записан {p}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
