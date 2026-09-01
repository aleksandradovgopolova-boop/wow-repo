#!/usr/bin/env python3
"""Security Pack -> доменный security-вердикт (v2.101, эпик Context Engineering, этап 5).

Security review как набор ПРИМЕНИМЫХ доменов (security/security-domains.yaml), а не один вердикт.
Проверяются только применимые к изменению домены (frontend-only не запускает database audit, но
проверяет XSS/secrets). Детерминированные проверки (secret_scan/dependency_diff/injection_scan)
берутся из tools/security_scan.py; остальное — вход для независимого security-reviewer/человека.

Честность: домен нельзя закрыть фразой «уязвимостей нет». Авто-закрыть можно ТОЛЬКО домены, чьё
required_evidence целиком покрыто пройденными детерминированными проверками (secrets, dependencies).
Домены с security_reviewer/human_approval в required_evidence остаются needs_review (судья/человек).
Находка -> домен fail (блокирует по severity_policy).

Использование:
  security_pack.py <child_root> [--base <sha>] [--signals '{...}'] [--json]
  security_pack.py --selftest
Возврат 0 — блокеров нет; 1 — есть блокирующие находки (или ошибка); 2 — скан НЕ ВЫПОЛНЕН и
причина названа (без базы сравнения охват неопределим: см. `_scan_scope`).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PKG = next((_p for _p in Path(__file__).resolve().parents if (_p / "VERSION").is_file()),
            Path(__file__).resolve().parents[1])
from ai_ops_kit.shared import _bootstrap  # noqa: E402
from ai_ops_kit.security import security_scan  # noqa: E402
import yaml           # noqa: E402

DETERMINISTIC = {"secret_scan", "dependency_diff", "injection_scan"}

# ПРОЗА НЕ РЕАЛИЗУЕТ ПОВЕДЕНИЕ (B2-09, живой прогон 14.08.2026).
#
# Совпадение ПО СОДЕРЖИМОМУ введено в v2.104 правильно: auth-логика в файле, чей путь не матчит,
# иначе не поднимала домен. Но матч — это подстрока без границ слова, и в тексте документации она
# ловит слова, а не поведение. Замер на живом прогоне: изменение ОДНОГО markdown-файла потребовало
# независимого security-ревьюера по четырём доменам, потому что
#   `log|logger|audit`         совпало с «BookTitle**Logo**»,
#   `route|param|query`        — со словом «ROUTES» в описании роутера,
#   `endpoint|route|api`       — с ним же,
#   `\.env|deploy`             — с фразой «переменные окружения перечислены в `.env.example`».
# Цена пере-срабатывания не «лишний ревью»: требование, не связанное с изменением, обесценивает
# требование вообще — человек учится обходить гейт, который срабатывает всегда, и обходит его там,
# где ревьюер действительно нужен.
#
# ПОЭТОМУ: для прозы отключается ТОЛЬКО матч по содержимому. Путь и сигналы работают по-прежнему
# (изменил сам `.github/workflows/deploy.yml` — домен поднимется по пути), детерминированные
# домены объявлены шаблоном `.*` и применимы всегда, поэтому секрет, забытый в markdown, ловится
# как и раньше. Под-срабатывания это не создаёт: markdown не выполняется.
#
# Список НЕ импортируется из `gates/verification_tiers.py` сознательно: слой security не должен
# зависеть от слоя gates (это добавило бы взаимную пару в ратчет связности), и понятия там разные —
# там «что не требует прогона проверок», здесь «что не может реализовывать поведение». Соотношение
# двух списков закреплено тестом шва.
PROSE_SUFFIXES = (".md", ".markdown", ".txt", ".rst", ".adoc")
PROSE_DIRS = ("docs/", "doc/", "context/", "decisions/")


def _is_prose(path: str) -> bool:
    """Файл — человеческий текст, а не исполняемое/конфигурационное содержимое."""
    p = str(path or "").replace("\\", "/").lstrip("./").lower()
    return p.endswith(PROSE_SUFFIXES) or any(seg in p for seg in PROSE_DIRS)


# ЗАМЕР 19.08.2026 (180 настоящих коммитов трёх живых продуктов: niti, ii-sreda, msh_news_bot_v2).
# Класс «домен применим, находок ноль, закрыть нечем» — 126 прогонов из 180. Из них в 95 (53% ВСЕХ
# прогонов) КАЖДЫЙ такой домен поднят ТОЛЬКО совпадением по содержимому: подстрока без границ слова
# в файле, чей путь домену не соответствует. Это не находка и не сигнал — это догадка.
#
# РАЗВИЛКА БЫЛА НАЗВАНА В ОТЧЁТЕ ПРОГОНА (B2-24) И РЕШЕНА ЗАМЕРОМ, А НЕ ВКУСОМ:
#   (а) поднимать security-судью на QUICK — цена: судья на 7 из 10 мелких правок (126/180);
#   (б) домен, поднятый ТОЛЬКО содержимым и БЕЗ находок, предупреждает, а не блокирует — цена:
#       окно для настоящего дефекта в файле, чей путь не матчит.
# Выбрано (б). Окно ограничено и названо: детерминированные проверки (secret/injection/dependency)
# объявлены шаблоном `.*` и применимы ВСЕГДА, поэтому секрет и инъекция в таком файле ловятся
# по-прежнему. Теряется только требование СУЖДЕНИЯ по домену, который никто не подтвердил ничем,
# кроме подстроки.
#
# ИНВАРИАНТ НЕ ТРОНУТ: домен с находками critical/high блокирует. Домен, поднятый ПУТЁМ или
# СИГНАЛОМ, остаётся needs_review — там основание настоящее (правка Dockerfile это правка Dockerfile).
def _content_only(reasons) -> bool:
    """Домен применён ТОЛЬКО совпадением по содержимому — ни пути, ни сигнала, ни «всегда»."""
    rs = list(reasons or [])
    return bool(rs) and all(str(r).startswith("содержимое ") for r in rs)


def load_domains():
    p = PKG / "security" / "security-domains.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.is_file() else {}
    return data.get("domains", []), data.get("allowed_evidence_sources", [])


def _applies(domain, signals, files_content):
    """Домен применим по сигналу, ИЛИ по пути изменённого файла, ИЛИ по его СОДЕРЖИМОМУ.
    v2.104 (finding самоаудита): раньше проверялся только ПУТЬ -> auth-логика в файле, чей путь
    не матчит (напр. src/users.py c 'password'), не поднимала домен -> security авто-проходил
    (ложный green). Совпадение по содержимому шире -> под-срабатывание (опасное) устранено;
    пере-срабатывание -> лишний needs_review (fail-closed, безопасно)."""
    reasons = []
    app = domain.get("applicability", {}) or {}
    for sig in app.get("signals", []) or []:
        if signals.get(sig):
            reasons.append(f"сигнал {sig}")
    for pat in app.get("file_patterns", []) or []:
        if pat == ".*":
            reasons.append("применим всегда (детерминированная проверка)")
            break
        rx = re.compile(pat)
        hit = None
        for f, content in files_content.items():
            if rx.search(f):
                hit = f"файл {f}"; break
            # матч по содержимому — только для файлов, которые могут РЕАЛИЗОВЫВАТЬ поведение
            if content and not _is_prose(f) and rx.search(content):
                hit = f"содержимое {f}"; break
        if hit:
            reasons.append(hit)
    return reasons


def _root_commit_files(root, base):
    """ЕДИНСТВЕННЫЙ законный случай неразрешимой базы: `<sha>~1`, где `<sha>` — КОРНЕВОЙ коммит.
    Родителя нет ПО ПОСТРОЕНИЮ, и охват тогда — файлы самого коммита, а не весь репозиторий.
    -> список путей; None, если случай другой (тогда вызывающий обязан отказаться)."""
    import subprocess
    m = re.fullmatch(r"(.+)~1", str(base))
    if not m:
        return None
    sha = m.group(1)
    # родителя нет -> это корень; у любого другого коммита `<sha>^` разрешается
    if subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{sha}^"],
                      capture_output=True, text=True).returncode == 0:
        return None
    r = subprocess.run(["git", "-C", str(root), "diff-tree", "--root", "--no-commit-id",
                        "--name-only", "-r", sha], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def _scan_scope(child_root, base):
    """Файлы для скана и НАЗВАННЫЙ охват. -> (files, scope) или RuntimeError с причиной.

    ПОЛЕ 17-18.08.2026 (заявка #139, ИИ-Среда): при `base=None` здесь брался ВЕСЬ репозиторий
    (`git ls-files`), и прогон блокировался находкой в ДАВНЕМ файле, которого правка не касалась.
    Замер с контролем на пробном репозитории: правка одного безобидного файла -> без базы
    `overall=blocked` (домен input_validation, находка в чужом legacy), с базой -> `clear`.
    Заявка читалась как «блокирует без находок» именно поэтому: находки были настоящие, но
    принадлежали чужому коду — врал не вердикт, а ОХВАТ.

    ПОЭТОМУ ОТСУТСТВИЕ БАЗЫ — РЕШЕНИЕ, А НЕ ПУСТОЕ ЗНАЧЕНИЕ, и оба молчаливых варианта запрещены:
    весь репозиторий превращает любой прогон в аудит чужого legacy, а пустой диф дал бы ложное
    `clear` (скан без единого файла — не «чисто», а «не проверено»). Отказ называет причину."""
    if base is None:
        raise RuntimeError(
            "security-скан без базы сравнения: fail-closed. Весь репозиторий сканировать нельзя "
            "(находка в файле, которого правка не касалась, блокирует чужой работой — заявка #139), "
            "пустой диф нельзя выдавать за 'clear'. Передай base=<sha|ветка> тем, кто вызывает "
            "run_pack, или задай --base у security_pack.py")
    changed = security_scan._git_changed_files(child_root, base)
    if changed is not None:
        return changed, {"mode": "diff", "base": str(base)}
    root_files = _root_commit_files(child_root, base)
    if root_files is not None:
        # первый коммит репозитория: сравнивать не с чем, и охват «файлы этого коммита» — точный,
        # а не «весь репозиторий по случайности совпал».
        return root_files, {"mode": "initial-commit", "base": str(base)}
    # v3.0.11 (finding аудита P1): git-энумерация упала -> FAIL-CLOSED (raise), НЕ changed=[].
    # Прежде rc!=0 -> [] -> нет находок -> overall='clear': реальная правка при git-сбое
    # признавалась чистой. Исключение ловит вызывающий (_security_scan_error -> security=fail).
    raise RuntimeError(
        f"база сравнения '{base}' не разрешается в {child_root}: диф не получен, и это не первый "
        f"коммит — файлы для security-скана определить нечем — fail-closed")


def run_pack(child_root=None, base=None, signals=None, files_content=None):
    """Доменный security-вердикт. files_content: {path: text} для offline-теста; иначе — из git diff.

    `scan_scope` в результате называет, ЧТО сравнивалось (diff/initial-commit/переданная карта):
    вердикт без охвата непроверяем — см. `_scan_scope`."""
    signals = dict(signals or {})
    domains, allowed = load_domains()

    # источник изменённых файлов: переданная карта (тест) или git diff коммита
    scan_scope = {"mode": "given", "base": None}
    if files_content is None:
        files_content = {}
        scan_scope = {"mode": "empty", "base": None}
        if child_root is not None:
            changed, scan_scope = _scan_scope(child_root, base)
            files_content = security_scan._read_files(child_root, changed)

    # детерминированные находки (один раз)
    secrets = security_scan.scan_secrets(files_content)
    injections = security_scan.scan_injection(files_content)
    mani = {p: c for p, c in files_content.items() if Path(p).name in security_scan.DEP_MANIFESTS}
    before = {p: (security_scan._git_show(child_root, base, p) if (child_root and base) else "") for p in mani}
    # `new_deps` (недетальный вариант) снят ревизией 2026-08-11: результат не использовался с
    # перехода на `new_deps_detailed` в v3.0-rc5 — считался лишний проход по манифестам.
    new_deps_detailed = security_scan.new_dependencies_detailed(before, mani)   # v3.0-rc5 (P1.2): fingerprint

    results, blocking, needs_review, advisory = [], [], [], []
    for d in domains:
        reasons = _applies(d, signals, files_content)
        if not reasons:
            continue
        checks = set(d.get("deterministic_checks", []) or [])
        findings = []
        if "secret_scan" in checks:
            findings += [{"type": "secret", "path": s["path"], "line": s["line"], "id": s["id"]} for s in secrets]
        if "injection_scan" in checks:
            findings += [{"type": "injection", "path": i["path"], "line": i["line"], "id": i["id"]} for i in injections]
        if "dependency_diff" in checks:
            # v3.0-rc5 (P1.2): finding несёт fingerprint (manifest/package/version/operation) — approval
            # supply-chain привязывается к нему, а не к пути файла (иначе одобрение одной зависимости
            # покрыло бы любую другую в том же requirements.txt/package.json).
            findings += [{"type": "new_dependency", "name": dd["name"], "version": dd.get("version"),
                          "manifest": dd.get("manifest"), "operation": dd.get("operation", "add")}
                         for dd in new_deps_detailed]

        req = set(d.get("required_evidence", []) or [])
        severity = (d.get("severity_policy", {}) or {}).get("default", "medium")
        # статус домена. ИНВАРИАНТ (finding аудита v2.104->исправлен): status=fail НИКОГДА не даёт
        # overall=clear. critical/high -> blocking (reviewer не переопределяет); medium/low ->
        # needs_review (нужен судья/человек — напр. новая зависимость требует одобрения).
        if findings:
            status = "fail"
            if severity in ("critical", "high"):
                blocking.append(d["id"])
            else:
                needs_review.append(d["id"])
        elif req and req <= DETERMINISTIC:
            # всё required_evidence — детерминированное и прошло чисто -> можно авто-закрыть
            status = "pass"
        elif _content_only(reasons):
            # догадка по подстроке, ничем не подтверждённая -> предупреждение, а не ворота (замер выше)
            status = "advisory"
            advisory.append(d["id"])
        else:
            status = "needs_review"           # нужен security_reviewer/человек (не закрываем сами)
            needs_review.append(d["id"])
        results.append({
            "domain": d["id"], "applies_because": reasons, "status": status,
            "severity": severity, "findings": findings,
            "required_evidence": sorted(req),
            "remediation": (d.get("remediation_template", {}) or {}).get("summary"),
        })

    return {
        "schema_version": 1, "kind": "security-pack-result",
        "applicable_domains": [r["domain"] for r in results],
        "results": results,
        "blocking": sorted(set(blocking)),
        "needs_review": sorted(set(needs_review)),
        # ПРЕДУПРЕЖДЕНИЕ — ОТДЕЛЬНЫЙ ВЕРДИКТ, А НЕ `clear`: «проверено и чисто» и «домен подняли
        # догадкой, проверять было нечего» обязаны выглядеть по-разному, иначе это ложный зелёный.
        "advisory": sorted(set(advisory)),
        "overall": ("blocked" if blocking else
                    ("needs_review" if needs_review else
                     ("advisory" if advisory else "clear"))),
        # ОХВАТ РЯДОМ С ВЕРДИКТОМ: что сравнивалось. Вердикт без охвата непроверяем — заявка #139
        # читалась как «блокирует без находок» именно потому, что охват был не назван нигде.
        "scan_scope": scan_scope,
        "allowed_evidence_sources": allowed,
    }


# ─── проекция вердикта в отчёт (заявка #139, вторая половина) ────────────────────────────────────
# ЗАМЕР 18.08.2026 на 3.36.12 и на живом отчёте дочки ИИ-Среда от 17.08: в `run-report.json` попадали
# ровно четыре поля (overall/applicable_domains/blocking/needs_review), а `domain_results` — где лежат
# САМИ находки и `applies_because` — не попадали ВОВСЕ. Гейт при этом говорит человеку «блокирующие
# домены (critical/high находки)» и отправляет его в отчёт. В отчёте находок нет — значит утверждение
# гейта НЕПРОВЕРЯЕМО из того артефакта, на который он сам ссылается. Именно отсюда родилась заявка
# «блокирует без единой находки»: человек прочитал отчёт и находок не увидел.
#
# ПОЧЕМУ ПРОЕКЦИЯ, А НЕ ПРОСТО `results` ЦЕЛИКОМ. Находка — это ПУТЬ, СТРОКА И КЛАСС, но никогда не
# значение: отчёт лежит в репозитории и уезжает в PR, поэтому секрет в нём был бы вынесенным секретом.
# Список полей — БЕЛЫЙ (не «удалим лишнее»): новое поле находки, если оно однажды принесёт значение,
# в отчёт не попадёт само по себе — его придётся внести здесь осознанно.
FINDING_REPORT_FIELDS = ("type", "path", "line", "id", "name", "version", "manifest", "operation")


def for_report(result):
    """Вердикт пака -> то, что кладётся в `run-report.json`. Без содержимого файлов и значений
    секретов; с находками, основаниями применимости домена и охватом скана. None -> None."""
    if not result:
        return None
    # .get, а не [] — на путях деградации вердикт бывает формы {"overall": "error"}, и проекция
    # обязана донести ЭТО, а не упасть: упавшая проекция вернула бы отчёт вообще без security.
    return {
        "overall": result.get("overall"),
        "applicable_domains": result.get("applicable_domains") or [],
        "blocking": result.get("blocking") or [],
        "needs_review": result.get("needs_review") or [],
        "advisory": result.get("advisory") or [],
        # охват рядом с вердиктом (`absent-base-is-resolved-or-refused`): вердикт без охвата
        # непроверяем — «clear» по пустому дифу и «clear» по проверенному дифу выглядят одинаково
        "scan_scope": result.get("scan_scope"),
        "domain_results": [{
            "domain": r["domain"],
            "status": r["status"],
            "severity": r["severity"],
            # ПОЧЕМУ домен вообще применён — иначе непонятно, откуда взялся блокирующий домен
            "applies_because": r.get("applies_because") or [],
            "findings": [{k: f[k] for k in FINDING_REPORT_FIELDS if k in f}
                         for f in (r.get("findings") or [])],
            "remediation": r.get("remediation"),
        } for r in (result.get("results") or [])],
    }


def main(argv):
    ap = argparse.ArgumentParser(prog="security_pack.py")
    ap.add_argument("child_root", nargs="?", default=".")
    ap.add_argument("--base")
    ap.add_argument("--signals", default="{}")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    # ОТКАЗ НАЗЫВАЕТ ПРИЧИНУ, а не показывает человеку стек: без базы охват определить нечем.
    try:
        res = run_pack(Path(a.child_root), base=a.base, signals=json.loads(a.signals))
    except RuntimeError as e:
        print(f"SECURITY-PACK: скан не выполнен — {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        _sc = res.get("scan_scope") or {}
        print(f"SECURITY-PACK: overall={res['overall']} · применимо доменов {len(res['applicable_domains'])} · "
              f"блокеров {len(res['blocking'])} · needs_review {len(res['needs_review'])} · "
              f"охват {_sc.get('mode')}" + (f" против {_sc.get('base')}" if _sc.get("base") else ""))
        for r in res["results"]:
            mark = {"fail": "✗", "pass": "✓", "needs_review": "?"}.get(r["status"], "·")
            print(f"  {mark} {r['domain']} [{r['severity']}] {r['status']}"
                  + (f" — находок {len(r['findings'])}" if r["findings"] else ""))
    return 1 if res["blocking"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
