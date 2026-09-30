#!/usr/bin/env python3
"""Реестр стандартных артефактов Product Operating Layer как ДАННЫЕ (PR-4).

Состав слоя (`.ai-ops/`: Product Passport, Roadmap, Delivery, Policy, templates/) объявлен в
`registry/artifact-registry.yaml` — здесь только код, который по нему работает. Три задачи:

  1. ЧТЕНИЕ (`load`) — FAIL-CLOSED. Битый или пустой реестр — исключение, а не пустой список:
     пустой список артефактов означал бы «слой не описан», и тогда bootstrap ничего не разложит, а
     валидация ничего не проверит — и оба промолчат. Тот же класс, что порча `registry/tracks.yaml`,
     которую в 3.33 не ловил никто.

  2. ИНВАРИАНТЫ (`check`) — реестр внутренне цел И согласован с моделью контуров. Главное:
     ССЫЛОЧНАЯ ЦЕЛОСТНОСТЬ. `owner_role`/`source_contour` каждого артефакта обязаны существовать в
     `registry/product-operating-model.yaml`. Роль или контур, переименованные там без правки здесь,
     — расхождение реестра с реальностью, и оно обязано краснеть.

  3. РАСХОЖДЕНИЕ С РЕПОЗИТОРИЕМ (`divergence`) — сверка реестра с ФАЙЛАМИ. Для каждого артефакта с
     шаблоном: шаблон на объявленном пути либо ещё не создан (`template_pending`, info — его кладёт
     работа `product-layer-templates-versioned`), либо создан, и тогда версия в файле обязана
     совпасть с версией в реестре. Разошлись — `template_version_mismatch` (major): шаблон
     подняли, а реестр не обновили (или наоборот), и bootstrap разложит не ту версию.

`check` и `divergence` разведены НАМЕРЕННО: первое — про целостность реестра и его связь с другими
реестрами, второе — про совпадение реестра с файлами на диске. Тест держит оба зелёными на реальном
реестре и краснеет, если любое из двух разойдётся.

Использование:
  artifact_registry.py list [--json]                 # что объявлено в реестре
  artifact_registry.py check [--json]                # инварианты + ссылочная целостность
  artifact_registry.py divergence [<repo>] [--json]   # сверка реестра с шаблонами репозитория
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
           Path(__file__).resolve().parents[2])
REGISTRY_PATH = PKG / "registry" / "artifact-registry.yaml"
MODEL_PATH = PKG / "registry" / "product-operating-model.yaml"

LIFECYCLE = ("missing", "invalid", "outdated", "valid")   # PR-5: четыре состояния, порядок значим
AUTONOMY = ("suggest", "prepare", "execute", "require_approval")   # PR-19
KINDS = {"document": "markdown", "config": "yaml", "directory": "dir"}
UPDATE_MODES = ("auto", "ai_assisted", "human")
TIERS = ("tier1", "tier2", "tier3")                        # #609: ярусы обязательности, замкнутый словарь
APPLIES_WHEN = ("always", "production", "ai_native")       # #609: условие применимости яруса


class RegistryCorrupt(Exception):
    """Реестр артефактов недостоверен. По нему работают bootstrap и валидация — догадки запрещены."""


def load(path: Path | None = None) -> dict:
    """Реестр из файла. FAIL-CLOSED: отсутствие/порча/пустота -> исключение, не пустой словарь.

    Пустой реестр означал бы «стандартных артефактов нет» -> слой не разложить и не проверить.
    """
    p = Path(path or REGISTRY_PATH)
    if not p.is_file():
        raise RegistryCorrupt(f"реестр артефактов не найден: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise RegistryCorrupt(f"реестр артефактов не разбирается ({p}): {e}") from e
    if not isinstance(data, dict) or not data.get("artifacts"):
        raise RegistryCorrupt(f"реестр артефактов пуст или без ключа artifacts ({p})")
    if data.get("registry_type") != "artifact-registry":
        raise RegistryCorrupt(f"реестр артефактов: registry_type != artifact-registry ({p})")
    return data


def artifacts(reg: dict) -> list:
    return list(reg.get("artifacts") or [])


def artifact(reg: dict, aid: str) -> dict | None:
    return next((a for a in artifacts(reg) if a.get("id") == aid), None)


def artifact_ids(reg: dict) -> list:
    return [a["id"] for a in artifacts(reg) if a.get("id")]


def required_artifacts(reg: dict) -> list:
    """Обязательные артефакты слоя: без любого из них слой неполон (PR-3)."""
    return [a for a in artifacts(reg) if a.get("required")]


def lifecycle_states(reg: dict) -> list:
    """Состояния жизненного цикла в объявленном порядке (Missing -> Invalid -> Outdated -> Valid)."""
    return sorted((reg.get("lifecycle_states") or []), key=lambda s: s.get("order", 0))


def obligation_tiers(reg: dict) -> list:
    """Ярусы обязательности артефактов (#609): tier1/tier2/tier3."""
    return list(reg.get("obligation_tiers") or [])


def profiles(reg: dict) -> dict:
    """Профили стандарта: имя профиля -> {includes_tiers, means} (#609)."""
    return dict(reg.get("profiles") or {})


def standard_catalog(reg: dict) -> list:
    """Каталог стандарта репозитория по ярусам (#609): элементы с tier/локатором/ai_check."""
    return list(reg.get("standard_catalog") or [])


def tiers_for_profile(reg: dict, profile: str) -> list:
    """Ярусы, применимые к профилю дочки (#609). Неизвестный профиль -> пустой список.

    Именно это делает профиль ВЫБОРОМ набора, а не украшением: состав ярусов один, различается
    их применимость. Пустой список для неизвестного профиля — fail-closed: ничего не «применимо».
    """
    prof = profiles(reg).get(profile) or {}
    known = {t.get("id") for t in obligation_tiers(reg)}
    return [t for t in (prof.get("includes_tiers") or []) if t in known]


def catalog_for_profile(reg: dict, profile: str) -> list:
    """Элементы каталога, обязательные для профиля: те, чей ярус применим к профилю (#609)."""
    applicable = set(tiers_for_profile(reg, profile))
    return [c for c in standard_catalog(reg) if c.get("tier") in applicable]


def _model_refs(model_path: Path = MODEL_PATH) -> tuple[set, set]:
    """Известные роли и id контуров из product-operating-model.yaml. -> (roles, contours).

    Пустые множества, если модель недоступна: тогда ссылочную целостность проверить нечем, и `check`
    об этом молчит, а не выдаёт ложные ошибки «роли нет». Саму модель проверяет validate_product_model.
    """
    try:
        m = yaml.safe_load(Path(model_path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return set(), set()
    roles = set((m.get("roles") or {}).keys())
    contours = {c.get("id") for c in (m.get("contours") or []) if c.get("id")}
    return roles, contours


def check(reg: dict, model_path: Path = MODEL_PATH) -> list:
    """Инварианты реестра + ссылочная целостность к модели контуров. -> список ошибок (пустой = ok)."""
    e = []
    if not isinstance(reg, dict):
        return ["реестр артефактов не является mapping"]

    arts = reg.get("artifacts") or []
    if not arts:
        return ["в реестре нет артефактов — слой не описан, bootstrap и валидация беззубы"]

    # Уровни автономии и состояния жизненного цикла — замкнутые словари.
    levels = list(reg.get("autonomy_levels") or [])
    if sorted(levels) != sorted(AUTONOMY):
        e.append(f"autonomy_levels обязаны быть ровно {list(AUTONOMY)} (PR-19), объявлено {levels}")
    states = [s.get("id") for s in (reg.get("lifecycle_states") or [])]
    if sorted(states) != sorted(LIFECYCLE):
        e.append(f"lifecycle_states обязаны быть ровно {list(LIFECYCLE)} (PR-5, четыре состояния), "
                 f"объявлено {states}")
    if not (reg.get("layer_root") or "").strip():
        e.append("layer_root не объявлен — неизвестно, где живёт слой в дочернем репозитории")
    layer_root = (reg.get("layer_root") or "").replace("\\", "/")

    ids = [a.get("id") for a in arts]
    dup = sorted({i for i in ids if i and ids.count(i) > 1})
    if dup:
        e.append(f"дубли id артефактов: {dup}")
    if not any(a.get("required") for a in arts):
        e.append("ни один артефакт не обязателен — слой без обязательного состава не является слоем")

    roles, contours = _model_refs(model_path)
    seen_paths = {}
    for a in arts:
        aid = a.get("id") or "<без id>"
        for field in ("title", "purpose", "path"):
            if not (a.get(field) or "").strip():
                e.append(f"артефакт '{aid}': пустое обязательное поле '{field}'")
        if not isinstance(a.get("required"), bool):
            e.append(f"артефакт '{aid}': поле required обязано быть bool (обязательность артефакта)")

        kind = a.get("kind")
        if kind not in KINDS:
            e.append(f"артефакт '{aid}': kind '{kind}' вне {list(KINDS)}")
        elif a.get("format") != KINDS[kind]:
            e.append(f"артефакт '{aid}': format '{a.get('format')}' не соответствует kind "
                     f"'{kind}' (ожидался '{KINDS[kind]}')")

        path = (a.get("path") or "").replace("\\", "/")
        if path:
            pp = Path(path)
            if pp.is_absolute() or ".." in pp.parts:
                e.append(f"артефакт '{aid}': path '{path}' обязан быть относительным и без '..'")
            elif layer_root and not path.startswith(layer_root):
                e.append(f"артефакт '{aid}': path '{path}' вне layer_root '{layer_root}'")
            if path in seen_paths:
                e.append(f"артефакт '{aid}': путь '{path}' уже занят '{seen_paths[path]}' (коллизия)")
            else:
                seen_paths[path] = aid

        # Ссылочная целостность — расхождение реестра с реальностью модели контуров.
        if roles and a.get("owner_role") not in roles:
            e.append(f"артефакт '{aid}': owner_role '{a.get('owner_role')}' нет в "
                     f"product-operating-model.yaml roles")
        if contours and a.get("source_contour") not in contours:
            e.append(f"артефакт '{aid}': source_contour '{a.get('source_contour')}' нет в "
                     f"product-operating-model.yaml contours")

        # Шаблон обязателен у document/config и запрещён у directory.
        tpl = a.get("template")
        if kind in ("document", "config"):
            if not isinstance(tpl, dict) or not tpl.get("path") or not isinstance(tpl.get("version"), int):
                e.append(f"артефакт '{aid}': нужен template {{path, version:int}} — без версии "
                         f"шаблона нельзя отличить Outdated от Valid (PR-5)")
        elif kind == "directory" and tpl is not None:
            e.append(f"артефакт '{aid}': kind=directory не имеет собственного шаблона")

        upd = a.get("update") or {}
        if upd and upd.get("mode") not in UPDATE_MODES:
            e.append(f"артефакт '{aid}': update.mode '{upd.get('mode')}' вне {list(UPDATE_MODES)}")

        for act in a.get("ai_actions") or []:
            if not (act.get("action") or "").strip():
                e.append(f"артефакт '{aid}': ai_action без имени action")
            if act.get("autonomy") not in AUTONOMY:
                e.append(f"артефакт '{aid}': ai_action '{act.get('action')}' с autonomy "
                         f"'{act.get('autonomy')}' вне {list(AUTONOMY)} (PR-19)")

        # #609: у обязательного артефакта объявлено, ЧТО проверяет AI-агент (контракт с AI-разработчиком).
        if a.get("required") and not (a.get("ai_check") or "").strip():
            e.append(f"артефакт '{aid}': обязательный артефакт без ai_check — не объявлено, что "
                     f"проверяет AI-агент по этому артефакту (#609)")

    e.extend(_check_tiers(reg, ids, contours))
    return e


def _check_tiers(reg: dict, artifact_ids: list, contours: set) -> list:
    """Инварианты ярусов, профилей и каталога стандарта (#609). -> список ошибок.

    Отдельная функция, а не разбухший `check`: таксономия ярусов проверяется как самостоятельный
    контракт (замкнутые словари ярусов и применимости, ссылочная целостность каталога к реестрам).
    """
    e = []
    tier_defs = reg.get("obligation_tiers")
    prof_defs = reg.get("profiles")
    catalog = reg.get("standard_catalog")
    # Если таксономии нет вовсе — молчим (её ввели #609; синтетический реестр без неё легитимен).
    if not any([tier_defs, prof_defs, catalog]):
        return e

    tier_ids = [t.get("id") for t in (tier_defs or [])]
    if sorted(tier_ids) != sorted(TIERS):
        e.append(f"obligation_tiers обязаны быть ровно {list(TIERS)} (#609), объявлено {tier_ids}")
    for t in tier_defs or []:
        if t.get("applies_when") not in APPLIES_WHEN:
            e.append(f"ярус '{t.get('id')}': applies_when '{t.get('applies_when')}' вне "
                     f"{list(APPLIES_WHEN)} (#609)")
        if not (t.get("means") or "").strip():
            e.append(f"ярус '{t.get('id')}': пустое means")
    tier_set = set(tier_ids)

    if not prof_defs:
        e.append("profiles не объявлены — профиль дочки не сможет выбрать применимый ярус (#609)")
    for pname, pdef in (prof_defs or {}).items():
        inc = (pdef or {}).get("includes_tiers")
        if not inc:
            e.append(f"профиль '{pname}': пустой includes_tiers — профиль ничего не выбирает")
        for ti in inc or []:
            if ti not in tier_set:
                e.append(f"профиль '{pname}': includes_tiers ссылается на неизвестный ярус '{ti}'")

    seen = set()
    for c in catalog or []:
        cid = c.get("id") or "<без id>"
        if cid in seen:
            e.append(f"каталог: дубль id '{cid}'")
        seen.add(cid)
        if not (c.get("name") or "").strip():
            e.append(f"каталог '{cid}': пустое name")
        if c.get("tier") not in tier_set:
            e.append(f"каталог '{cid}': tier '{c.get('tier')}' вне объявленных ярусов (#609)")
        if not (c.get("ai_check") or "").strip():
            e.append(f"каталог '{cid}': пустой ai_check — не объявлено, что проверяет AI-агент (#609)")
        # Локатор — ровно один из трёх, и ссылка целостна.
        locators = [k for k in ("ref", "contour", "path") if (c.get(k) or "").strip()]
        if len(locators) != 1:
            e.append(f"каталог '{cid}': нужен РОВНО один локатор из ref/contour/path, "
                     f"объявлено {locators}")
        if c.get("ref") and c["ref"] not in artifact_ids:
            e.append(f"каталог '{cid}': ref '{c['ref']}' не найден в artifacts этого реестра")
        if c.get("contour") and contours and c["contour"] not in contours:
            e.append(f"каталог '{cid}': contour '{c['contour']}' нет в product-operating-model.yaml")
    return e


_MD_VERSION = re.compile(r"template[-_]version:\s*(\d+)")


def _read_template_version(path: Path) -> int | None:
    """Версия шаблона из файла: yaml-ключ `template_version` или markdown-маркер.

    Markdown: строка вида `<!-- template-version: N -->`. YAML: верхнеуровневый `template_version`.
    None — маркер не найден: шаблон есть, но версию не объявляет, и это отдельная находка, а не 0.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if path.suffix in (".yaml", ".yml"):
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            data = {}
        v = data.get("template_version") if isinstance(data, dict) else None
        return v if isinstance(v, int) else None
    m = _MD_VERSION.search(text)
    return int(m.group(1)) if m else None


def divergence(reg: dict, repo_root: Path | None = None) -> list:
    """Расхождение реестра с ФАЙЛАМИ репозитория (шаблоны). -> список находок.

    Находки:
      template_pending          (info)  — шаблон объявлен, но файла ещё нет (кладёт работа шаблонов);
      template_version_missing  (major) — файл шаблона есть, но версии не объявляет: Outdated не
                                          отличить от Valid;
      template_version_mismatch (major) — версия в файле != версии в реестре: bootstrap разложит не
                                          ту версию, а миграция не сработает.

    `repo_root` — корень, относительно которого резолвятся пути шаблонов (по умолчанию корень кита).
    """
    root = Path(repo_root or PKG)
    out = []
    for a in artifacts(reg):
        tpl = a.get("template")
        if not isinstance(tpl, dict) or not tpl.get("path"):
            continue                                   # directory или уже пойман в check
        declared = tpl.get("version")
        tpath = root / tpl["path"]
        if not tpath.is_file():
            out.append({"id": "template_pending", "artifact": a.get("id"), "severity": "info",
                        "detail": f"шаблон ещё не создан: {tpl['path']} "
                                  f"(работа product-layer-templates-versioned)"})
            continue
        actual = _read_template_version(tpath)
        if actual is None:
            out.append({"id": "template_version_missing", "artifact": a.get("id"), "severity": "major",
                        "detail": f"шаблон {tpl['path']} не объявляет version — Outdated не "
                                  f"отличить от Valid"})
        elif actual != declared:
            out.append({"id": "template_version_mismatch", "artifact": a.get("id"), "severity": "major",
                        "detail": f"версия шаблона в файле {tpl['path']} = {actual}, в реестре "
                                  f"= {declared} — реестр и шаблон разошлись"})
    return out


def has_major(findings: list) -> bool:
    return any(f.get("severity") == "major" for f in findings)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="artifact_registry.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").add_argument("--json", action="store_true")
    sub.add_parser("check").add_argument("--json", action="store_true")
    ct = sub.add_parser("catalog")                          # #609: ярусный каталог стандарта
    ct.add_argument("--profile", default=None, help="показать только применимое к профилю")
    ct.add_argument("--json", action="store_true")
    dv = sub.add_parser("divergence")
    dv.add_argument("repo", nargs="?", default=None)
    dv.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])

    try:
        reg = load()
    except RegistryCorrupt as exc:
        print(json.dumps({"errors": [str(exc)], "ok": False}, ensure_ascii=False, indent=2)
              if getattr(ns, "json", False) else f"РЕЕСТР АРТЕФАКТОВ: {exc}")
        return 1

    if ns.cmd == "list":
        if ns.json:
            print(json.dumps(artifacts(reg), ensure_ascii=False, indent=2)); return 0
        for a in artifacts(reg):
            mark = "обяз." if a.get("required") else "опц."
            print(f"[{mark:5}] {a.get('id'):18} {a.get('path'):30} роль {a.get('owner_role')}")
        print(f"\nАРТЕФАКТОВ: {len(artifacts(reg))}, из них обязательных {len(required_artifacts(reg))}")
        return 0

    if ns.cmd == "check":
        errs = check(reg)
        if ns.json:
            print(json.dumps({"errors": errs, "ok": not errs}, ensure_ascii=False, indent=2))
        elif errs:
            print("РЕЕСТР АРТЕФАКТОВ: ошибки:")
            for x in errs:
                print(f"  - {x}")
        else:
            print("РЕЕСТР АРТЕФАКТОВ-OK: инварианты и ссылочная целостность сходятся.")
        return 1 if errs else 0

    if ns.cmd == "catalog":
        items = catalog_for_profile(reg, ns.profile) if ns.profile else standard_catalog(reg)
        if ns.json:
            print(json.dumps(items, ensure_ascii=False, indent=2)); return 0
        for t in obligation_tiers(reg):
            rows = [c for c in items if c.get("tier") == t.get("id")]
            if not rows:
                continue
            print(f"\n{t.get('id')} ({t.get('applies_when')}): {t.get('means')}")
            for c in rows:
                loc = c.get("ref") or c.get("contour") or c.get("path")
                print(f"  {c.get('name'):28} [{loc}] — {c.get('ai_check')}")
        return 0

    findings = divergence(reg, Path(ns.repo) if ns.repo else None)
    if ns.json:
        print(json.dumps({"findings": findings, "ok": not has_major(findings)},
                         ensure_ascii=False, indent=2))
    else:
        print(f"РАСХОЖДЕНИЕ РЕЕСТР↔РЕПОЗИТОРИЙ: {'есть' if has_major(findings) else 'нет'}")
        for f in findings:
            print(f"  {f['severity']:6} {f['id']} / {f['artifact']}: {f['detail']}")
    return 1 if has_major(findings) else 0


if __name__ == "__main__":
    sys.exit(main())
