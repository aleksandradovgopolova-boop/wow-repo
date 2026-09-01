#!/usr/bin/env python3
"""Container delivery scope — доставка ТОЛЬКО ветки текущего прогона (v2.113).

Аудит: контейнерная доставка должна забирать из одноразового клона обратно в основной репо ТОЛЬКО
ветку(и), которые СОЗДАЛ/ИЗМЕНИЛ прогон — не все ai-ops/* (иначе параллельная ai-ops/* ветка в
основном репо перезаписывается устаревшей версией из клона). Логика вынесена в
`containers/deliver-run-branches.sh` и проверяется здесь ДЕТЕРМИНИРОВАННО на настоящем git (без docker).

Сценарий:
  1. child с веткой ai-ops/old (v1). Клонируем -> клон тоже имеет ai-ops/old (v1). Снимок ДО.
  2. Прогон в клоне: создаёт ai-ops/new и продвигает ai-ops/old (v2 в клоне).
  3. ПАРАЛЛЕЛЬНО основной child продвигает свою ai-ops/old вперёд (v-concurrent) — как другой прогон.
  4. Доставка: должна принести ТОЛЬКО ai-ops/new и ai-ops/old из клона... стоп — ai-ops/old изменён
     И в клоне: он попадёт в доставку (это его прогон изменил). Проверяем ГЛАВНОЕ: НЕтронутая клоном
     ветка (ai-ops/untouched, что была в снимке и не менялась) НЕ перезаписывает concurrent-версию.

Использование: validate_container_delivery.py [--selftest]
Возврат 0 — ок, 1 — доставка утащила лишнее / затёрла чужое.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
DELIVER = PKG / "containers" / "deliver-run-branches.sh"


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _sha(root, ref):
    return _git(root, "rev-parse", ref).stdout.strip()


def _commit(root, fname, content, msg):
    (Path(root) / fname).write_text(content, encoding="utf-8")
    _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", msg)


def run_scenario():
    r = []

    def ok(name, cond):
        r.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        child = Path(td) / "child"
        child.mkdir()
        _git(child, "init", "-q"); _git(child, "config", "user.email", "t@t"); _git(child, "config", "user.name", "t")
        _commit(child, "f", "base", "init")
        # пред-существующие ai-ops/* ветки: untouched (не тронем) и old (тронем прогоном)
        _git(child, "branch", "ai-ops/untouched")
        _git(child, "branch", "ai-ops/old")

        # одноразовый клон
        clone = Path(td) / "clone"
        subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", "--local", str(child), str(clone)])
        # клон делает локальные ветки из origin/ai-ops/*, чтобы for-each-ref refs/heads видел их
        for b in ("ai-ops/untouched", "ai-ops/old"):
            _git(clone, "branch", b, f"origin/{b}")

        # снимок ai-ops/* клона ДО прогона
        snap = Path(td) / "snap.before"
        out = _git(clone, "for-each-ref", "--format=%(objectname) %(refname:short)", "refs/heads/ai-ops/*").stdout
        snap.write_text(out, encoding="utf-8")

        # ПРОГОН в клоне: новая ветка ai-ops/new + продвижение ai-ops/old
        _git(clone, "checkout", "-q", "-b", "ai-ops/new")
        _commit(clone, "new.py", "n=1", "run: new branch work")
        _git(clone, "checkout", "-q", "ai-ops/old")
        _commit(clone, "old.py", "o=2", "run: advance old")
        new_sha_clone = _sha(clone, "ai-ops/new")
        old_sha_clone = _sha(clone, "ai-ops/old")

        # ПАРАЛЛЕЛЬНО: основной child продвигает СВОЮ ai-ops/untouched (другой прогон/сессия)
        _git(child, "checkout", "-q", "ai-ops/untouched")
        _commit(child, "concurrent.py", "c=1", "concurrent work on untouched")
        untouched_sha_child = _sha(child, "ai-ops/untouched")
        _git(child, "checkout", "-q", "master") if _git(child, "rev-parse", "--verify", "master").returncode == 0 \
            else _git(child, "checkout", "-q", "main")

        # ДОСТАВКА
        res = subprocess.run(["bash", str(DELIVER), str(clone), str(child), str(snap)],
                             capture_output=True, text=True)
        ok("delivery: скрипт отработал (rc=0)", res.returncode == 0)
        delivered_line = (res.stdout + res.stderr)

        # 1) ai-ops/new доставлена в child с правильным SHA
        ok("delivery: ai-ops/new доставлена (создана прогоном)",
           _git(child, "rev-parse", "--verify", "ai-ops/new").returncode == 0
           and _sha(child, "ai-ops/new") == new_sha_clone)
        # 2) ai-ops/old доставлена с версией прогона (её изменил именно прогон)
        ok("delivery: ai-ops/old доставлена с версией прогона (изменена прогоном)",
           _sha(child, "ai-ops/old") == old_sha_clone)
        # 3) ГЛАВНОЕ: ai-ops/untouched в child НЕ перезаписана устаревшей версией из клона
        #    (её прогон не трогал -> concurrent-версия сохранена)
        ok("delivery: НЕтронутая прогоном ai-ops/untouched НЕ затёрта (параллельная работа цела)",
           _sha(child, "ai-ops/untouched") == untouched_sha_child)
        # 4) в отчёте доставки — только затронутые ветки
        ok("delivery: отчёт называет только ветки прогона (new/old), не untouched",
           "ai-ops/new" in delivered_line and "ai-ops/untouched" not in delivered_line)

        # 5) «доставлять нечего», если прогон ничего не создал/не изменил
        clone2 = Path(td) / "clone2"
        subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", "--local", str(child), str(clone2)])
        snap2 = Path(td) / "snap2.before"
        out2 = _git(clone2, "for-each-ref", "--format=%(objectname) %(refname:short)", "refs/heads/ai-ops/*").stdout
        snap2.write_text(out2, encoding="utf-8")
        res2 = subprocess.run(["bash", str(DELIVER), str(clone2), str(child), str(snap2)],
                              capture_output=True, text=True)
        ok("delivery: нет изменений прогона -> 'nothing' (ничего не тащим)",
           res2.returncode == 0 and "nothing" in (res2.stdout + res2.stderr))

    return r


def main(argv):
    if not DELIVER.is_file():
        print(f"CONTAINER-DELIVERY: нет {DELIVER}")
        return 1
    results = run_scenario()
    ok = True
    for name, passed in results:
        ok = ok and passed
        print(f"{'PASS' if passed else 'FAIL'} {name}")
    print("validate_container_delivery:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def selftest():
    return main([])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
