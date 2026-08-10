#!/usr/bin/env python3
"""validate_research_artifacts.py — валидатор артефактов research-модуля (v0.2).

Проверяет .research/ (живой экземпляр), examples/research-demo/ и examples схем:
  1. Соответствие JSON-схемам (research-request / research-evidence / decision-package):
     required/const/enum/pattern/minLength/minItems/types/additionalProperties + if/then
     (evidence: inference -> derived_from; DP: confidence high -> review).
  2. Ссылочная целостность RR -> EV -> DP: request_id указывает на существующий RR;
     evidence_ids/derived_from/superseded_by указывают на существующие EV (переиспользование
     EV между запросами разрешено — это research memory); status: superseded требует superseded_by;
     EV-id, упомянутые в rationale/decision_brief DP, обязаны входить в evidence_ids.
  3. Freshness (структурно): volatile: true требует expires_at; даты — ISO; просроченный
     active-EV — WARNING (temporal-контроль — у еженедельного freshness_sweep), --strict -> ошибка.
  4. Quote grounding (конвенция v0.2): для EV с captured_at >= 2026-07-23, непустым source.url
     и is_primary: true отсутствие citation.quote — WARNING (required планируется с v0.3).

Использование: python3 validation/validate_research_artifacts.py [--strict] [--selftest]
Exit code: 0 — ок (warnings допустимы), 1 — ошибки (или warnings при --strict).
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUOTE_CONVENTION_SINCE = dt.date(2026, 7, 23)
EV_REF = re.compile(r'\bEV-\d{3,}\b')


def _type_ok(instance, tlist):
    pymap = {'object': dict, 'array': list, 'string': str, 'boolean': bool}
    if 'null' in tlist and instance is None:
        return True
    return any(isinstance(instance, pymap.get(x, object)) for x in tlist if x != 'null')


def check_schema(instance, schema, path='.'):
    errs = []
    t = schema.get('type')
    tlist = t if isinstance(t, list) else ([t] if t else None)
    if 'const' in schema and instance != schema['const']:
        errs.append(f'{path}: const {schema["const"]!r} != {instance!r}')
    if 'enum' in schema and instance not in schema['enum']:
        errs.append(f'{path}: {instance!r} not in enum')
    if 'pattern' in schema and isinstance(instance, str) and not re.search(schema['pattern'], instance):
        errs.append(f'{path}: {instance!r} !~ {schema["pattern"]}')
    if 'minLength' in schema and isinstance(instance, str) and len(instance) < schema['minLength']:
        errs.append(f'{path}: короче minLength ({schema["minLength"]})')
    if tlist and instance is not None and not _type_ok(instance, tlist):
        errs.append(f'{path}: type mismatch ({tlist})')
    if isinstance(instance, dict) and ('properties' in schema or schema.get('required')):
        for req in schema.get('required', []):
            if req not in instance:
                errs.append(f'{path}: нет required {req!r}')
        props = schema.get('properties', {})
        addl = schema.get('additionalProperties')
        if addl is False:
            for k in instance:
                if k not in props:
                    errs.append(f'{path}: лишний ключ {k!r}')
        elif isinstance(addl, dict):
            for k, v in instance.items():
                if k not in props:
                    errs.extend(check_schema(v, addl, f'{path}.{k}'))
        for k, v in instance.items():
            if k in props:
                errs.extend(check_schema(v, props[k], f'{path}.{k}'))
    if isinstance(instance, list) and 'items' in schema:
        if 'minItems' in schema and len(instance) < schema['minItems']:
            errs.append(f'{path}: меньше minItems')
        for i, item in enumerate(instance):
            errs.extend(check_schema(item, schema['items'], f'{path}[{i}]'))
    if 'if' in schema and 'then' in schema and isinstance(instance, dict):
        cond = True
        for k, sub in schema['if'].get('properties', {}).items():
            v = instance.get(k)
            if 'const' in sub and v != sub['const']:
                cond = False
            if 'properties' in sub and isinstance(v, dict):
                for k2, sub2 in sub['properties'].items():
                    if 'const' in sub2 and v.get(k2) != sub2['const']:
                        cond = False
        if cond:
            for req in schema['then'].get('required', []):
                if not isinstance(instance.get(req), (dict, list, str)):
                    errs.append(f'{path}: if/then — требуется {req!r}')
    return errs


def check_links(rrs, evs, dps):
    """Ссылочная целостность RR->EV->DP. rrs/evs/dps: dict id -> объект."""
    errs = []
    for ev_id, ev in evs.items():
        if ev.get('request_id') not in rrs:
            errs.append(f'{ev_id}: request_id {ev.get("request_id")} — RR-файл не найден')
        for ref in ev.get('derived_from') or []:
            if ref not in evs:
                errs.append(f'{ev_id}: derived_from {ref} не существует')
        sup = ev.get('superseded_by')
        if sup and sup not in evs:
            errs.append(f'{ev_id}: superseded_by {sup} не существует')
        if ev.get('status') == 'superseded' and not sup:
            errs.append(f'{ev_id}: status superseded без superseded_by')
    for dp_id, dp in dps.items():
        if dp.get('request_id') not in rrs:
            errs.append(f'{dp_id}: request_id {dp.get("request_id")} — RR-файл не найден')
        eids = dp.get('evidence_ids') or []
        for eid in eids:
            if eid not in evs:
                errs.append(f'{dp_id}: evidence_ids {eid} не существует')
        # EV-упоминания в тексте пакета обязаны входить в evidence_ids (улов judge DP-106 r2)
        text = ' '.join(str(dp.get(k, '')) for k in
                        ('decision_brief', 'rationale', 'risks', 'assumptions', 'unknowns'))
        for ref in sorted(set(EV_REF.findall(text))):
            if ref not in eids:
                errs.append(f'{dp_id}: {ref} упомянут в тексте, но отсутствует в evidence_ids')
    return errs


def check_freshness_and_quotes(evs, today):
    """Возвращает (errors, warnings)."""
    errs, warns = [], []
    for ev_id, ev in evs.items():
        fresh = ev.get('freshness') or {}
        if fresh.get('volatile') is True and not fresh.get('expires_at'):
            errs.append(f'{ev_id}: volatile без expires_at')
        exp = fresh.get('expires_at')
        if exp:
            try:
                exp_d = dt.date.fromisoformat(str(exp))
            except ValueError:
                errs.append(f'{ev_id}: expires_at не ISO-дата: {exp}')
                continue
            if ev.get('status') == 'active' and exp_d < today:
                warns.append(f'{ev_id}: просрочен ({exp}) и всё ещё active — дело freshness_sweep')
        try:
            cap = dt.date.fromisoformat(str(ev.get('captured_at')))
        except (ValueError, TypeError):
            errs.append(f'{ev_id}: captured_at не ISO-дата')
            continue
        url = (ev.get('source') or {}).get('url') or ''
        primary = (ev.get('source') or {}).get('is_primary') is True
        has_quote = bool(((ev.get('citation') or {}).get('quote') or '').strip())
        if cap >= QUOTE_CONVENTION_SINCE and url and primary and not has_quote:
            warns.append(f'{ev_id}: первичный URL-источник без citation.quote '
                         f'(конвенция quote-grounding v0.2; required с v0.3)')
    return errs, warns


def load_set(base):
    schemas = {k: json.load(open(os.path.join(ROOT, 'schemas', f'{k}.schema.json')))
               for k in ('research-request', 'research-evidence', 'decision-package')}
    sets = {'research-request': os.path.join(base, 'requests', '*.yaml'),
            'research-evidence': os.path.join(base, 'evidence', '*.yaml'),
            'decision-package': os.path.join(base, 'decisions', '*.yaml')}
    objs = {'research-request': {}, 'research-evidence': {}, 'decision-package': {}}
    errs = []
    for kind, pattern in sets.items():
        for f in sorted(glob.glob(pattern)):
            obj = yaml.safe_load(open(f))
            errs.extend(f'{os.path.relpath(f, ROOT)} {e}' for e in check_schema(obj, schemas[kind]))
            objs[kind][obj.get('id')] = obj
    return objs, errs, schemas


def selftest():
    rrs = {'RR-001': {}}
    evs = {'EV-001': {'request_id': 'RR-001', 'status': 'active',
                      'freshness': {'volatile': True, 'expires_at': '2026-01-01'},
                      'captured_at': '2026-07-23',
                      'source': {'url': 'https://e', 'is_primary': True}, 'citation': {}},
           'EV-002': {'request_id': 'RR-001', 'status': 'superseded', 'superseded_by': None,
                      'freshness': {'volatile': False}, 'captured_at': '2026-07-01',
                      'source': {}, 'citation': {}}}
    dps = {'DP-001': {'request_id': 'RR-001', 'evidence_ids': ['EV-001'],
                      'rationale': ['опирается на EV-001 и EV-999']}}
    link_errs = check_links(rrs, evs, dps)
    assert any('superseded без superseded_by' in e for e in link_errs), link_errs
    assert any('EV-999' in e for e in link_errs), link_errs
    f_errs, f_warns = check_freshness_and_quotes(evs, dt.date(2026, 7, 23))
    assert any('просрочен' in w for w in f_warns), f_warns
    assert any('без citation.quote' in w for w in f_warns), f_warns
    assert not f_errs, f_errs
    bad = check_schema({'schema_version': 2}, {'type': 'object',
                       'properties': {'schema_version': {'const': 1}},
                       'required': ['schema_version'], 'additionalProperties': False})
    assert bad
    print('SELFTEST-OK')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--strict', action='store_true', help='warnings становятся ошибками')
    ap.add_argument('--selftest', action='store_true')
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return 0

    all_errs, all_warns = [], []
    today = dt.date.today()
    for base, label in ((os.path.join(ROOT, '.research'), '.research'),
                        (os.path.join(ROOT, 'examples', 'research-demo'), 'examples/research-demo')):
        if not os.path.isdir(base):
            continue
        objs, schema_errs, schemas = load_set(base)
        all_errs.extend(schema_errs)
        all_errs.extend(f'[{label}] {e}' for e in check_links(
            objs['research-request'], objs['research-evidence'], objs['decision-package']))
        f_errs, f_warns = check_freshness_and_quotes(objs['research-evidence'], today)
        all_errs.extend(f'[{label}] {e}' for e in f_errs)
        all_warns.extend(f'[{label}] {w}' for w in f_warns)
        n = sum(len(v) for v in objs.values())
        print(f'[{label}] артефактов: {n}')
    # examples внутри самих схем
    for k in ('research-request', 'research-evidence', 'decision-package'):
        schema = json.load(open(os.path.join(ROOT, 'schemas', f'{k}.schema.json')))
        for i, ex in enumerate(schema.get('examples', [])):
            all_errs.extend(f'schema {k} example[{i}] {e}' for e in check_schema(ex, schema))

    for w in all_warns:
        print(f'WARN: {w}')
    for e in all_errs:
        print(f'ERROR: {e}')
    failed = bool(all_errs) or (args.strict and bool(all_warns))
    print(('RESEARCH-ARTIFACTS-FAIL' if failed else 'RESEARCH-ARTIFACTS-OK')
          + f' (errors={len(all_errs)}, warnings={len(all_warns)})')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
