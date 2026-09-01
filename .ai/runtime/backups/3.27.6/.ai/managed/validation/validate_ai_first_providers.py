#!/usr/bin/env python3
"""Проверка реестров provider/model/runtime/tool + capability-index + routing-policy (Фаза 6).

Ловит то, что ломается при ручных правках реестров абстракции:
  1. невалидный YAML;
  2. model.provider ссылается на несуществующий provider;
  3. запись capability-index указывает на неизвестный entity_id (provider/runtime/tool);
  4. недопустимый status в capability-index (вне status_vocabulary);
  5. routing-policy prefer/forbid provider — неизвестный provider; prefer_model_class — неизвестный класс;
  6. секреты: имена env — только UPPER_SNAKE; литеральных значений credentials быть не должно;
  7. запуск движка маршрутизации (ai_route) на примерах — форма решения корректна.

Использование:  python3 02_tools/ci/validate_ai_first_providers.py
Возврат 0 — чисто, 1 — есть ошибки. Требует pyyaml.
"""

import re
import sys
from pathlib import Path

import yaml

PKG_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path.cwd()
REG = PKG_ROOT / "registry"
CI = PKG_ROOT / "validation"
sys.path.insert(0, str(CI))

ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
SECRETISH_RE = re.compile(r"(AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|-----BEGIN)")

errors = []


def fail(where, msg):
    errors.append(f"{where}: {msg}")


def load(name):
    p = REG / name
    if not p.exists():
        fail(name, "файл реестра отсутствует")
        return None
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        fail(name, f"невалидный YAML: {e}")
        return None


def check_models(providers, models):
    prov_ids = set(providers.get("providers", {}))
    classes = set(models.get("model_classes", {}))
    for m in models.get("models", []):
        if not isinstance(m, dict):
            continue
        if m.get("provider") not in prov_ids:
            fail("models.yaml", f"model '{m.get('id')}': provider '{m.get('provider')}' не найден")
        for c in m.get("classes", []):
            if c not in classes:
                fail("models.yaml", f"model '{m.get('id')}': класс '{c}' не объявлен в model_classes")
    return prov_ids, classes


def check_capability_index(cap, prov_ids, runtimes, tools):
    vocab = set(cap.get("status_vocabulary", []))
    rt_ids = set(runtimes.get("runtimes", {}))
    tool_ids = {t.get("id") for t in tools.get("tools", [])}
    for e in cap.get("entries", []):
        if not isinstance(e, dict):
            continue
        kind, eid = e.get("entity_kind"), e.get("entity_id")
        pool = {"provider": prov_ids, "runtime": rt_ids, "tool": tool_ids}.get(kind)
        if pool is not None and eid not in pool:
            fail("capability-index.yaml", f"{kind} '{eid}' не найден в реестре {kind}")
        if e.get("status") not in vocab:
            fail("capability-index.yaml", f"entry {kind}/{eid}/{e.get('capability')}: status '{e.get('status')}' вне словаря")


ADAPTER_DEPTH_VOCAB = {"executing", "generated-commands", "manual-assisted"}


def check_adapter_depth(runtimes):
    """Честность maturity-декларации адаптеров: каждый mvp_adapter обязан честно
    заявить adapter_depth (что адаптер реально делает), значение — из словаря."""
    for rid, rt in (runtimes.get("runtimes", {}) or {}).items():
        if not isinstance(rt, dict) or not rt.get("mvp_adapter"):
            continue
        depth = rt.get("adapter_depth")
        if depth is None:
            fail("runtimes.yaml", f"runtime '{rid}': mvp_adapter: true, но нет adapter_depth "
                                  f"(честная глубина адаптера обязательна)")
        elif depth not in ADAPTER_DEPTH_VOCAB:
            fail("runtimes.yaml", f"runtime '{rid}': adapter_depth '{depth}' вне словаря "
                                  f"{sorted(ADAPTER_DEPTH_VOCAB)}")


def check_secrets(providers):
    for pid, p in providers.get("providers", {}).items():
        auth = p.get("auth", {}) if isinstance(p, dict) else {}
        for env in auth.get("env", []) or []:
            if not ENV_NAME_RE.match(str(env)):
                fail("providers.yaml", f"provider '{pid}': '{env}' не похоже на имя env-переменной (UPPER_SNAKE)")
        # запрет литеральных секретов в тексте provider-записи
        if SECRETISH_RE.search(yaml.safe_dump(p, allow_unicode=True)):
            fail("providers.yaml", f"provider '{pid}': похоже на литеральный секрет")


def check_routing(policy, prov_ids, classes):
    for rule in policy.get("provider_rules", []):
        for key in ("prefer_provider", "forbid_provider"):
            for p in rule.get(key, []) or []:
                if p not in prov_ids:
                    fail("routing-policy.yaml", f"{key}: неизвестный provider '{p}'")
        mc = rule.get("prefer_model_class")
        if mc and mc not in classes:
            fail("routing-policy.yaml", f"prefer_model_class: неизвестный класс '{mc}'")


def check_router():
    try:
        import ai_route
    except Exception as e:
        fail("ai_route.py", f"не удалось импортировать движок: {e}")
        return
    rc = ai_route.selftest_quiet() if hasattr(ai_route, "selftest_quiet") else None
    # ai_route.selftest печатает; используем route() напрямую для тихой проверки формы
    for sc in getattr(ai_route, "SCENARIOS", []):
        d = ai_route.route(sc["inp"])
        for k in ai_route.REQUIRED_KEYS:
            if k not in d or d[k] in (None, ""):
                fail("ai_route", f"[{sc['name']}] нет ключа '{k}' в решении")
        for k, v in sc.get("expect", {}).items():
            if d.get(k) != v:
                fail("ai_route", f"[{sc['name']}] {k}={d.get(k)!r} != {v!r}")


def main():
    providers = load("providers.yaml")
    models = load("models.yaml")
    runtimes = load("runtimes.yaml")
    tools = load("tools.yaml")
    cap = load("capability-index.yaml")
    policy = load("routing-policy.yaml")
    if None in (providers, models, runtimes, tools, cap, policy):
        # хотя бы YAML-ошибки уже зафиксированы
        pass
    else:
        prov_ids, classes = check_models(providers, models)
        check_capability_index(cap, prov_ids, runtimes, tools)
        check_adapter_depth(runtimes)
        check_secrets(providers)
        check_routing(policy, prov_ids, classes)
        check_router()

    if errors:
        print(f"НАЙДЕНЫ ПРОБЛЕМЫ В РЕЕСТРАХ АБСТРАКЦИИ ({len(errors)}):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("OK: провайдеры/модели/среды/инструменты + capability-index + routing валидны.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
