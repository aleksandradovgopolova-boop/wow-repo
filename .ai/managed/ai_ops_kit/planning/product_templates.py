#!/usr/bin/env python3
"""Версионные шаблоны Product Operating Layer и состояние артефакта Missing/Invalid/Outdated/Valid (PR-5).

Реестр (`registry/artifact-registry.yaml`) объявляет ФОРМУ; здесь — код, который держит официальные
ШАБЛОНЫ этой формы и умеет сказать, в каком состоянии находится экземпляр артефакта. Три задачи:

  1. МАШИННАЯ ВАЛИДАЦИЯ ШАБЛОНОВ (`check`). Каждый артефакт реестра с шаблоном обязан иметь файл
     шаблона; версия в файле = версии в реестре; шаблон содержит ВСЕ обязательные разделы
     (markdown) или поля (yaml), объявленные в реестре. Плюс главное правило миграций: нельзя
     поднять версию шаблона, не положив миграцию на каждый шаг (иначе «совместимость либо миграция»
     из PR-5 остаётся словом без исполнения).

  2. ЧЕТЫРЕ СОСТОЯНИЯ ЭКЗЕМПЛЯРА (`state_of`), А НЕ ДВА. `is_file()` != «заполнен» (F-018/F-027):
     файл на диске может быть пустым (Invalid) или отставшим от версии шаблона (Outdated).
        missing  — файла нет;
        invalid  — есть, но структура нарушена: нет маркера версии или обязательного раздела/поля;
        outdated — структура цела, но версия шаблона старее реестра — нужна миграция;
        valid    — есть, структура полна, версия актуальна.
     Порядок — лестница ухудшения; сворачивать «пусто» в «есть» запрещено так же, как unknown в ok.

  3. МИГРАЦИИ ВЕРСИЙ (`missing_migrations`). Отдельная ось от миграций пакета; раскладка и правило —
     в `migrations/product-layer-templates/README.md`.

Состояние экземпляра (state_of) — логика формы; отчёт по репозиторию и CLI `ai-ops validate
product-layer` строит работа `product-layer-validation` поверх этой функции, не дублируя её.

Использование:
  product_templates.py check [--json]                 # шаблоны валидны и покрывают реестр
  product_templates.py state <repo> [--json]           # состояние экземпляров артефактов в репо
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

from ai_ops_kit.planning import artifact_registry as AR

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
           Path(__file__).resolve().parents[2])
TEMPLATES_DIR = PKG / "templates" / "product-layer"
MIGRATIONS_DIR = PKG / "migrations" / "product-layer-templates"

MISSING, INVALID, OUTDATED, VALID = "missing", "invalid", "outdated", "valid"

_HEADER = re.compile(r"^#{1,6}\s+(.*\S)\s*$", re.MULTILINE)


def _markdown_headers(text: str) -> list:
    return [m.group(1).strip() for m in _HEADER.finditer(text)]


def _has_section(headers: list, section: str) -> bool:
    """Раздел присутствует, если хоть один заголовок СОДЕРЖИТ его текст (устойчиво к нумерации/суффиксам)."""
    s = section.strip()
    return any(s in h for h in headers)


def _empty_sections(text: str, sections: list) -> list:
    """Разделы, которые ПРИСУТСТВУЮТ заголовком, но без содержательного тела. -> список названий.

    Комментарии и пустые строки телом не считаются: `is_file()` != заполнен. Отсутствующие разделы
    здесь НЕ учитываются — их ловит проверка структуры отдельно (отсутствие != пустота)."""
    no_comments = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    positions = [(m.group(1).strip(), m.start(), m.end()) for m in _HEADER.finditer(no_comments)]
    empty = []
    for sec in sections:
        idx = next((i for i, (h, _, _) in enumerate(positions) if sec in h), None)
        if idx is None:
            continue
        body_start = positions[idx][2]
        body_end = positions[idx + 1][1] if idx + 1 < len(positions) else len(no_comments)
        if not no_comments[body_start:body_end].strip():
            empty.append(sec)
    return empty


def required_migration_steps(version: int) -> list:
    """Шаги миграции, обязательные для шаблона версии V: (1->2), (2->3), …, (V-1->V). V<=1 -> []."""
    return [(n, n + 1) for n in range(1, int(version or 1))]


def _step_dir(artifact_id: str, frm: int, to: int, pkg_root: Path = PKG) -> Path:
    return Path(pkg_root) / "migrations" / "product-layer-templates" / artifact_id / f"v{frm}-to-v{to}"


def missing_migrations(reg: dict, pkg_root: Path = PKG) -> list:
    """Отсутствующие шаги миграции для артефактов с версией шаблона > 1. -> список ошибок.

    Правило PR-5: подъём версии обязан прийти со своей миграцией. Без этого дочка на старой версии
    не догонит новую, а гейт «совместимость либо миграция» окажется беззубым.
    """
    errs = []
    for a in AR.artifacts(reg):
        tpl = a.get("template")
        if not isinstance(tpl, dict) or not isinstance(tpl.get("version"), int):
            continue
        for frm, to in required_migration_steps(tpl["version"]):
            d = _step_dir(a["id"], frm, to, pkg_root)
            for script in ("up.py", "down.py"):
                if not (d / script).is_file():
                    errs.append(f"артефакт '{a['id']}': нет миграции шаблона v{frm}->v{to} "
                                f"({d.relative_to(pkg_root)}/{script}) — версию нельзя поднять без миграции")
    return errs


def check(reg: dict | None = None, pkg_root: Path = PKG) -> list:
    """Машинная валидация шаблонов: покрытие реестра, версии, обязательные разделы/поля, миграции.
    -> список ошибок (пустой = все шаблоны валидны).
    """
    reg = reg or AR.load()
    root = Path(pkg_root)
    errs = []
    for a in AR.artifacts(reg):
        aid = a.get("id")
        tpl = a.get("template")
        if not isinstance(tpl, dict) or not tpl.get("path"):
            continue                                   # directory — шаблона нет по определению
        tpath = root / tpl["path"]
        if not tpath.is_file():
            errs.append(f"артефакт '{aid}': шаблон не найден: {tpl['path']}")
            continue
        declared = tpl.get("version")
        actual = AR._read_template_version(tpath)
        if actual is None:
            errs.append(f"артефакт '{aid}': шаблон {tpl['path']} не объявляет версию "
                        f"(маркер template-version/template_version)")
        elif actual != declared:
            errs.append(f"артефакт '{aid}': версия шаблона {tpl['path']} = {actual}, "
                        f"в реестре = {declared}")
        struct = a.get("structure") or {}
        try:
            text = tpath.read_text(encoding="utf-8")
        except OSError as e:
            errs.append(f"артефакт '{aid}': шаблон не читается: {e}")
            continue
        if a.get("format") == "markdown":
            headers = _markdown_headers(text)
            for sec in struct.get("required_sections") or []:
                if not _has_section(headers, sec):
                    errs.append(f"артефакт '{aid}': в шаблоне нет обязательного раздела «{sec}»")
        elif a.get("format") == "yaml":
            try:
                data = yaml.safe_load(text) or {}
            except yaml.YAMLError as e:
                errs.append(f"артефакт '{aid}': шаблон-yaml не разбирается: {e}")
                data = {}
            if not isinstance(data, dict):
                data = {}
            for field in struct.get("required_fields") or []:
                if field not in data:
                    errs.append(f"артефакт '{aid}': в шаблоне нет обязательного поля '{field}'")
    errs.extend(missing_migrations(reg, pkg_root))
    return errs


def _resolve_instance(repo_root: Path, rel: str) -> Path | None:
    """Экземпляр артефакта в репозитории с учётом project/custom-оверлея. -> путь или None."""
    for pre in ("", ".ai/project/", ".ai/custom/"):
        p = Path(repo_root) / (pre + rel)
        if p.exists():
            return p
    return None


def state_of(repo_root: Path, artifact: dict, reg: dict | None = None) -> dict:
    """Состояние ЭКЗЕМПЛЯРА артефакта в репозитории: missing/invalid/outdated/valid + причина.

    Проверяется СОДЕРЖИМОЕ, а не факт файла: пустой или неполный артефакт — Invalid, отставший по
    версии — Outdated. Оба честнее «Valid», и подмена запрещена так же, как unknown->ok.
    """
    reg = reg or AR.load()
    kind = artifact.get("kind")
    rel = artifact.get("path") or ""
    inst = _resolve_instance(repo_root, rel)
    if inst is None:
        return {"state": MISSING, "reason": f"нет файла {rel}"}

    if kind == "directory":
        return ({"state": VALID, "reason": "каталог на месте"} if inst.is_dir()
                else {"state": INVALID, "reason": f"{rel} существует, но это не каталог"})

    declared = ((artifact.get("template") or {}).get("version"))
    struct = artifact.get("structure") or {}
    try:
        text = inst.read_text(encoding="utf-8")
    except OSError as e:
        return {"state": INVALID, "reason": f"файл не читается: {e}"}

    if artifact.get("format") == "markdown":
        version = AR._read_template_version(inst)
        headers = _markdown_headers(text)
        missing_sec = [s for s in (struct.get("required_sections") or []) if not _has_section(headers, s)]
        if version is None:
            return {"state": INVALID, "reason": "нет маркера версии шаблона"}
        if missing_sec:
            return {"state": INVALID, "reason": f"нет обязательных разделов: {', '.join(missing_sec[:4])}"}
        if isinstance(declared, int) and version < declared:
            return {"state": OUTDATED, "reason": f"версия шаблона {version} < {declared} — нужна миграция"}
        # СОДЕРЖИМОЕ, а не только структура: если реестр требует непустых разделов, пустой раздел —
        # Invalid, а НЕ Valid. Это и есть «пустая секция != отсутствующая, но и != заполненная»
        # (F-018/F-027): файл на месте, заголовок на месте, а под ним ничего.
        if "non_empty_sections" in (artifact.get("validation") or []):
            empty = _empty_sections(text, struct.get("required_sections") or [])
            if empty:
                return {"state": INVALID,
                        "reason": f"разделы присутствуют, но пусты: {', '.join(empty[:4])} — "
                                  f"пустая секция не равна заполненной"}
        return {"state": VALID, "reason": "структура полна, содержимое есть, версия актуальна"}

    if artifact.get("format") == "yaml":
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError as e:
            return {"state": INVALID, "reason": f"yaml не разбирается: {e}"}
        if not isinstance(data, dict):
            return {"state": INVALID, "reason": "yaml не является mapping"}
        version = data.get("template_version")
        missing_f = [f for f in (struct.get("required_fields") or []) if f not in data]
        if not isinstance(version, int):
            return {"state": INVALID, "reason": "нет поля template_version"}
        if missing_f:
            return {"state": INVALID, "reason": f"нет обязательных полей: {', '.join(missing_f)}"}
        if isinstance(declared, int) and version < declared:
            return {"state": OUTDATED, "reason": f"версия шаблона {version} < {declared} — нужна миграция"}
        return {"state": VALID, "reason": "поля на месте, версия актуальна"}

    return {"state": INVALID, "reason": f"неизвестный format '{artifact.get('format')}'"}


def report(repo_root: Path, reg: dict | None = None) -> dict:
    """Состояние всех артефактов реестра в репозитории. -> {artifact_id: {state, reason}} + свод."""
    reg = reg or AR.load()
    out = {a["id"]: state_of(repo_root, a, reg) for a in AR.artifacts(reg)}
    counts = {s: sum(1 for v in out.values() if v["state"] == s)
              for s in (MISSING, INVALID, OUTDATED, VALID)}
    return {"artifacts": out, "counts": counts,
            "valid": counts[MISSING] == 0 and counts[INVALID] == 0 and counts[OUTDATED] == 0}


def _instance_version(inst: Path, artifact: dict) -> int | None:
    """Текущая версия ЭКЗЕМПЛЯРА: markdown — маркер template-version, yaml — поле template_version."""
    if artifact.get("format") == "markdown":
        return AR._read_template_version(inst)
    try:
        data = yaml.safe_load(inst.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    v = data.get("template_version")
    return v if isinstance(v, int) else None


def migrate_instance(repo_root: Path, artifact: dict, reg: dict | None = None,
                     pkg_root: Path = PKG) -> dict:
    """ПРИМЕНИТЬ миграции версий к ЭКЗЕМПЛЯРУ артефакта в дочке: провести его с текущей версии до
    объявленной в реестре, выполнив `up.py` каждого шага (v_cur -> … -> v_declared).

    Это НЕДОСТАЮЩАЯ ПОЛОВИНА механизма: `state_of` ставил диагноз OUTDATED, а миграцию до сих пор
    НИКТО не применял — «обратная совместимость либо миграция» (PR-5) оставалась словом без
    исполнения на стороне применения. Мигрирует ТОЛЬКО OUTDATED-экземпляр (для VALID/MISSING/INVALID
    мигрировать нечего — честный отказ, не тихий «успех»). Каждый `up.py` вызывается как
    `python up.py <путь-экземпляра>`, exit 0 = успех (контракт
    migrations/product-layer-templates/README.md). FAIL-CLOSED: нет шага или `up.py` упал — прекращаем
    и НЕ объявляем успех. Успех подтверждается ПОВТОРНЫМ state_of == VALID, а не фактом запуска
    (green-means-checked). -> {artifact, migrated, from, to, steps, state_after, reason}.
    """
    reg = reg or AR.load()
    aid = artifact.get("id")
    st = state_of(repo_root, artifact, reg)
    if st["state"] != OUTDATED:
        return {"artifact": aid, "migrated": False, "state_after": st["state"],
                "reason": f"состояние {st['state']} — мигрировать нечего"}
    inst = _resolve_instance(repo_root, artifact.get("path") or "")
    declared = (artifact.get("template") or {}).get("version")
    cur = _instance_version(inst, artifact)
    if not isinstance(cur, int) or not isinstance(declared, int):
        return {"artifact": aid, "migrated": False, "state_after": st["state"],
                "reason": "версия экземпляра или реестра не число — переход невозможен"}
    steps = []
    for frm, to in [(n, n + 1) for n in range(cur, declared)]:
        up = _step_dir(aid, frm, to, pkg_root) / "up.py"
        if not up.is_file():
            return {"artifact": aid, "migrated": False, "from": cur, "to": declared, "steps": steps,
                    "reason": f"нет миграции v{frm}->v{to} ({up}) — переход невозможен"}
        r = subprocess.run([sys.executable, str(up), str(inst)], capture_output=True, text=True)
        steps.append({"step": f"v{frm}->v{to}", "rc": r.returncode,
                      "output": (r.stdout + r.stderr).strip()[:300]})
        if r.returncode != 0:
            return {"artifact": aid, "migrated": False, "from": cur, "to": declared, "steps": steps,
                    "reason": f"миграция v{frm}->v{to} упала (rc={r.returncode})"}
    after = state_of(repo_root, artifact, reg)
    return {"artifact": aid, "migrated": after["state"] == VALID, "from": cur, "to": declared,
            "steps": steps, "state_after": after["state"], "reason": after["reason"]}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="product_templates.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").add_argument("--json", action="store_true")
    st = sub.add_parser("state")
    st.add_argument("repo")
    st.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv if argv is not None else sys.argv[1:])

    try:
        reg = AR.load()
    except AR.RegistryCorrupt as exc:
        print(f"ШАБЛОНЫ СЛОЯ: реестр недостоверен: {exc}")
        return 1

    if ns.cmd == "check":
        errs = check(reg)
        if ns.json:
            print(json.dumps({"errors": errs, "ok": not errs}, ensure_ascii=False, indent=2))
        elif errs:
            print("ШАБЛОНЫ СЛОЯ: ошибки:")
            for x in errs:
                print(f"  - {x}")
        else:
            print("ШАБЛОНЫ СЛОЯ-OK: все шаблоны версионны, покрывают реестр и мигрируемы.")
        return 1 if errs else 0

    rep = report(Path(ns.repo), reg)
    if ns.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2)); return 0
    for aid, v in rep["artifacts"].items():
        print(f"  {v['state']:9} {aid}: {v['reason']}")
    c = rep["counts"]
    print(f"\nСОСТОЯНИЕ СЛОЯ: valid {c['valid']} · outdated {c['outdated']} · "
          f"invalid {c['invalid']} · missing {c['missing']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
