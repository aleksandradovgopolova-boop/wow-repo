#!/usr/bin/env python3
"""child_doctor.py — диагностика установки СИЛАМИ САМОЙ ДОЧКИ, без клона кита рядом.

ПОВОД — ЗАМЕР (аудит 19.08.2026). `./ai-ops doctor` в подключённом репозитории отвечал:

    Команда 'doctor' обслуживает сам кит, а его исходник рядом не найден.
    Укажите, где он лежит: AI_OPS_HOME=/путь/к/ai-ops-kit ./ai-ops doctor

Сообщение честное, а возможности нет: `installer/ai_ops.py` в поставку НЕ едет (он обновляет сам
кит, и ставить его в дочку значило бы дать ей себя же обновлять). Практическое следствие для
основы, на которую должны опираться чужие команды: **чтобы диагностировать свою установку, они
обязаны воспроизвести раскладку каталогов автора** — клон по пути `~/ai-ops-kit` или
`AI_OPS_HOME`. Второй человек и вторая машина этого не имеют.

ЧТО ЗДЕСЬ И ЧЕГО ЗДЕСЬ НЕТ — граница названа, а не размыта.

Дочка может проверить всё, что видно ИЗНУТРИ неё: зоны, целостность managed-слоя, заполненность
конфига, каркасы направления и плана, рабочие проверки child-валидатора, наличие точки входа.
Дочка НЕ может проверить то, для чего нужен сам пакет: свежа ли установленная версия относительно
источника, из выпуска ли она поставлена, есть ли `.pth`-пояс в окружении разработчика кита. Эти
пункты здесь не имитируются и не молчат — они называются как «нужен кит рядом», потому что
«не проверено» и «в порядке» — разные ответы, и путать их здесь дороже всего.

ВТОРОЙ ПРАВДЫ НЕ ЗАВОДИМ. Полный `doctor` установщика остаётся единственным полным; этот модуль
покрывает его child-подмножество и говорит, чего в подмножестве нет.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
           Path(__file__).resolve().parents[2])

ZONES = ("managed", "project", "custom", "generated", "runtime")

# ЗАМЕЧАНИЕ — НЕ ОТКАЗ (правка 19.08.2026, поймано сквозным тестом пути владельца).
# Первая редакция возвращала 1 на ЛЮБОЙ несошедшийся пункт, и свежая установка сразу давала
# ненулевой код: `project.name` в ней по построению ещё заготовка. Полный `doctor` установщика так
# не делает и говорит «Работать можно, но есть замечания» — смешивать «кит сломан» и «допишите имя
# проекта» значит обесценить оба ответа.
# Блокирует только то, после чего кит НЕ РАБОТАЕТ; остальное — замечание, видимое и не мешающее.
BLOCKING = {"зона managed", "версия установленной копии", "точка входа ./ai-ops",
            "ai_managed_checksums", "validate_ai_ops_child"}

# Пункты, которых в child-подмножестве нет, и почему. Список ОБЪЯВЛЕН, а не подразумевается:
# молчаливое отсутствие пункта неотличимо от пройденного пункта.
NEEDS_THE_KIT = {
    "версия против источника": "сравнить установленную версию с пакетом можно только имея пакет",
    "происхождение установки": "из выпуска или из рабочей ветки — знает источник, не копия",
    "пояса путей импорта": "проверяются в окружении, где стоит кит, а не в дочке",
}


def _zone_state(root: Path) -> list:
    out = []
    for z in ZONES:
        p = root / ".ai" / z
        out.append({"check": f"зона {z}", "ok": p.is_dir(),
                    "detail": "" if p.is_dir() else "каталог отсутствует"})
    return out


def _run_validator(root: Path, name: str, *args, target: str = None) -> dict:
    """Запустить шипнутый валидатор ПРОЦЕССОМ — так же, как это делает CI дочки.

    Импортом звать нельзя: валидаторы двурежимны и рассчитаны на запуск процессом (зона-исключение
    в AGENTS.md). Плюс процесс изолирует падение: сбой одного валидатора не уносит весь отчёт.
    """
    script = root / ".ai" / "managed" / "ai_ops_kit" / "validation" / f"{name}.py"
    if not script.is_file():
        return {"check": name, "ok": None, "detail": "валидатор не поставлен — проверить нечем"}
    try:
        # ЦЕЛЬ У ВАЛИДАТОРОВ РАЗНАЯ, И ЭТО НЕ МЕЛОЧЬ: `ai_managed_checksums verify` ждёт каталог,
        # где лежит `.checksums.json` (то есть `.ai/managed`), остальные — корень репозитория.
        # Первая редакция подавала всем корень, и на ИСПРАВНОЙ установке проверка целостности
        # краснела «нет .checksums.json» — ложный красный из собственной ошибки вызова.
        r = subprocess.run([sys.executable, str(script), *args, target or str(root)],
                           capture_output=True, text=True, timeout=120, cwd=str(root))
    except (OSError, subprocess.SubprocessError) as e:
        return {"check": name, "ok": None, "detail": f"не запустился ({type(e).__name__}: {e})"}
    tail = (r.stdout + r.stderr).strip().splitlines()
    return {"check": name, "ok": r.returncode == 0,
            "detail": tail[-1][:200] if tail else f"код {r.returncode}, вывод пуст"}


def assess(child_root) -> dict:
    """Отчёт о состоянии установки. -> dict.

    `ok` у пункта: True — сошлось, False — не сошлось, **None — не проверено**. Третье значение
    существует намеренно: «не смогли проверить» не сворачивается в «в порядке» (тот же инвариант,
    что `unknown != not_changed` в модели контуров и `unavailable != 0` в учёте стоимости).
    """
    root = Path(child_root).resolve()
    checks = []
    managed = root / ".ai" / "managed"

    if not managed.is_dir():
        return {"schema_version": 1, "kind": "ChildDoctorReport", "root": str(root),
                "installed": False, "checks": [],
                "not_covered": NEEDS_THE_KIT,
                "verdict": "кит в этот репозиторий не установлен (нет .ai/managed)"}

    checks += _zone_state(root)

    ver = (managed / "VERSION")
    checks.append({"check": "версия установленной копии", "ok": ver.is_file(),
                   "detail": ver.read_text(encoding="utf-8").strip() if ver.is_file()
                   else "файл VERSION не поставлен"})

    entry = root / "ai-ops"
    checks.append({"check": "точка входа ./ai-ops", "ok": entry.is_file(),
                   "detail": "" if entry.is_file() else "обёртка отсутствует — подсказки кита не исполнимы"})

    checks.append(_run_validator(root, "ai_managed_checksums", "verify", target=str(managed)))
    checks.append(_run_validator(root, "validate_ai_ops_child"))
    checks.append(_run_validator(root, "validate_child_config_filled"))

    bad = [c for c in checks if c["ok"] is False]
    blockers = [c for c in bad if c["check"] in BLOCKING]
    unknown = [c for c in checks if c["ok"] is None]
    if blockers:
        verdict = (f"работать нельзя: {len(blockers)} — "
                   + "; ".join(c["check"] for c in blockers[:3]))
    elif bad:
        verdict = f"работать можно, но есть замечания: {len(bad)}"
    elif unknown:
        verdict = f"работать можно; не проверено пунктов: {len(unknown)} — это не «в порядке»"
    else:
        verdict = "установка в порядке по тем пунктам, которые видны изнутри репозитория"
    return {"schema_version": 1, "kind": "ChildDoctorReport", "root": str(root),
            "installed": True, "checks": checks, "not_covered": NEEDS_THE_KIT,
            "blocking": [c["check"] for c in blockers], "verdict": verdict}


def render(report: dict) -> str:
    lines = []
    if not report.get("installed"):
        return report["verdict"]
    mark = {True: "✓", False: "✗", None: "?"}
    for c in report["checks"]:
        d = f" — {c['detail']}" if c.get("detail") else ""
        lines.append(f"{mark[c['ok']]} {c['check']}{d}")
    lines.append("")
    lines.append(report["verdict"])
    lines.append("Чего эта проверка НЕ покрывает (нужен сам кит рядом):")
    for k, v in report["not_covered"].items():
        lines.append(f"  · {k}: {v}")
    return "\n".join(lines)


def main(argv):
    root = "."
    js = "--json" in argv
    for a in argv[1:]:
        if not a.startswith("-"):
            root = a
            break
    rep = assess(root)
    print(json.dumps(rep, ensure_ascii=False, indent=2) if js else render(rep))
    return 1 if rep.get("blocking") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
