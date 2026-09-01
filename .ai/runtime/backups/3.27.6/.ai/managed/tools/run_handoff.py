#!/usr/bin/env python3
"""Context Lifecycle -> RunHandoff + resume (v2.99, эпик Context Engineering, этап 3).

Длинная задача должна переживать несколько сессий без потери решений, архитектуры и состояния
(борьба с context rot). Сущности: Feature -> WorkItem -> Run -> Stage -> Step -> Handoff.

После значимого этапа сохраняем RunHandoff: что сделано, что изменилось, какие решения приняты,
какие проверки прошли/упали, что осталось, открытые вопросы/риски, СЛЕДУЮЩИЙ безопасный шаг и
актуальный commit/revision. Resume перечитывает последний Handoff и продолжает с подтверждённого
шага — НЕ начинает заново, НЕ повторяет подтверждённое без причины, НЕ использует старый контекст
после изменения main, НЕ удаляет предыдущий результат.

Использование:
  run_handoff.py build <run-report.json> [--out handoff.yaml]
  run_handoff.py resume-preflight <child_root> <workitem_id> [--base main] [--json]
  run_handoff.py --selftest
Возврат 0 — ок, 1 — ошибка/resume небезопасен.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

import lifecycle_store as _ls   # v3.0.12: fail-closed чтение RunHandoff (битый != «resume безопасен»)


def _git(root, *args):
    import gitio
    return gitio.git(root, *args)   # v3.0.13 (блок C): единый git-хелпер с таймаутом


def build_handoff(report, work_root=None):
    """Собрать RunHandoff из отчёта прогона движка (execution-pipeline). Детерминированно."""
    rep = report or {}
    wid = rep.get("workitem_id", "unknown")
    commit = rep.get("commit") or {}
    sha = commit.get("sha")
    loop = rep.get("loop") or {}
    gates = rep.get("gates") or {}

    # completed: подтверждённые шаги петли (успешные write/shell) — краткое резюме
    completed = []
    if loop.get("applied_writes"):
        completed.append(f"применено правок: {loop['applied_writes']}")
    if commit.get("sha"):
        completed.append(f"коммит {sha[:12]} на {commit.get('branch')} (evidence на точном SHA: "
                         f"{commit.get('evidence_on_exact_sha')})")

    # changed_files: имена из diff коммита (если есть git и sha)
    changed_files = []
    if work_root and sha:
        rc, out, _ = _git(work_root, "diff", "--name-only", f"{sha}~1..{sha}")
        if rc == 0:
            changed_files = [ln for ln in out.splitlines() if ln.strip()]

    # verification: закрытые/незакрытые гейты + проверки
    evaluated = gates.get("evaluated") or []
    unmet = gates.get("unmet") or []
    passed = [g for g in evaluated if g not in unmet]
    checks = rep.get("checks") or {}
    checks_failed = [k for k, v in checks.items() if (v or {}).get("status") == "fail"]

    # known_risks: секреты/новые зависимости + регрессии baseline
    known_risks = []
    ss = rep.get("security_scan") or {}
    if ss.get("secrets"):
        known_risks.append(f"security: секретов {len(ss['secrets'])}")
    if ss.get("new_dependencies"):
        known_risks.append("security: новые зависимости " + ", ".join(ss["new_dependencies"]))
    baseline = rep.get("baseline") or {}
    if baseline.get("regressions"):
        known_risks.append("регрессии: " + ", ".join(baseline["regressions"]))

    # next_action: следующий безопасный шаг
    if rep.get("ready_for_pr"):
        next_action = "открыть/обновить draft PR (--open-pr) либо передать на ревью человеку"
    elif unmet:
        next_action = "закрыть незакрытые гейты: " + ", ".join(unmet)
    elif loop.get("stopped") and loop["stopped"] != "done":
        next_action = f"продолжить реализацию (петля остановилась: {loop['stopped']})"
    else:
        next_action = "проверить отчёт и решить следующий шаг"

    return {
        "schema_version": 1, "kind": "RunHandoff",
        "run_id": f"{wid}@{sha[:12]}" if sha else wid,
        "workitem_id": wid,
        # v3.0.10 (finding аудита P0): RunHandoff несёт ИСХОДНЫЙ BaseBinding (base_ref+base_sha+mode+source)
        # прогона. Это источник истины для resume: точная база, от которой форкнута работа, а не «текущий
        # SHA той же ветки». Без него resume не мог отличить force-push/пересоздание базы от fast-forward.
        "base_binding": rep.get("base_binding") or {},
        "completed": completed,
        "decisions": rep.get("decisions") or [],
        "changed_files": changed_files,
        "verification": {"passed": passed, "failed": unmet + [f"check:{c}" for c in checks_failed]},
        "open_questions": rep.get("not_yet") or [],
        "known_risks": known_risks,
        "next_action": next_action,
        "resume_from_revision": sha,
    }


def resume_preflight(child_root, wid, base="main"):
    """Проверить, безопасно ли продолжать WorkItem, и что требует ревалидации. Детерминированно.
    -> {can_resume, revalidation_needed, base_rewritten, reasons[], handoff?, next_action?,
        resume_from_revision?}.

    v3.0.10 (finding аудита P0): различаем ДВА класса изменения базы:
      * FAST-FORWARD (сохранённый base_sha — предок текущего HEAD базы): база ушла вперёд —
        revalidation_needed=True, снимается осознанным force_resume;
      * REWRITE (base force-push назад / пересоздан на несвязанном SHA — сохранённый base_sha НЕ предок
        текущего HEAD базы): base_rewritten=True. Это НЕ снимается force_resume — старую работу нельзя
        переобозначить как проверенную против ДРУГОЙ базы; нужен явный replan/отмена."""
    child_root = Path(child_root)
    reasons, revalidation, base_rewritten, base_moved = [], False, False, False
    hp = child_root / "features" / wid / "run-handoff.yaml"
    # v3.0.12 (finding аудита блок B): FAIL-CLOSED чтение. Прежде safe_load(...) or {} на битом/пустом
    # handoff давал {} -> sha=None -> ВСЕ проверки устаревания (база ушла/переписана, ревизия пропала)
    # пропускались -> preflight отвечал «can_resume=True, ревалидация не нужна» на пустом состоянии —
    # ровно тот ложный «resume безопасен», который модуль обязан предотвращать. Теперь: битый -> отказ.
    # require kind+workitem_id (структурная целостность); resume_from_revision МОЖЕТ быть null легитимно
    # (прогон без коммита) — это не «повреждён», а «нет точки резюме», и обрабатывается ниже как sha=None.
    _g = _ls.load_guarded(hp, required_keys=("kind", "workitem_id"))
    if _g["state"] == "absent":
        return {"can_resume": False, "revalidation_needed": False, "base_rewritten": False,
                "reasons": [f"нет RunHandoff для {wid} (features/{wid}/run-handoff.yaml) — нечего продолжать"]}
    if _g["state"] == "corrupt":
        return {"can_resume": False, "revalidation_needed": True, "base_rewritten": False,
                "reasons": [f"RunHandoff повреждён ({_g['reason']}) — состояние resume недостоверно; "
                            "нужна явная recovery/ревалидация, не тихое продолжение на пустом состоянии"]}
    handoff = _g["data"]
    sha = handoff.get("resume_from_revision")
    saved_base_sha = ((handoff.get("base_binding") or {}).get("base_sha")) or None
    # v3.8.3 (finding живого resume): base не передан явно (--base опущен) -> резолвим из handoff
    # (base_binding.base_sha) либо 'main'. Иначе `git rev-parse --verify None` роняет preflight (None не str).
    if not base:
        base = saved_base_sha or "main"
    branch = f"ai-ops/{wid}"

    # ветка/worktree на месте?
    rc_b, _, _ = _git(child_root, "rev-parse", "--verify", branch)
    branch_exists = rc_b == 0
    if not branch_exists:
        reasons.append(f"ветка {branch} не найдена — worktree прошлого прогона утерян; resume пересоберёт с базы")
        revalidation = True
    wt = child_root / ".ai" / "worktrees" / wid
    if not wt.is_dir():
        reasons.append(f"worktree {wt.relative_to(child_root)} отсутствует — будет пересоздан")

    # base-ветку вообще можно разрешить? Иначе устаревание НЕ проверить — честно отметим (не молчим).
    rc_base, _, _ = _git(child_root, "rev-parse", "--verify", base)
    base_resolvable = rc_base == 0
    if not base_resolvable:
        reasons.append(f"base-ветку '{base}' не удалось разрешить — устаревание относительно базы НЕ "
                       f"проверено; укажи --base явно. Требуется ревалидация из осторожности")
        revalidation = True

    # изменился ли base с момента handoff? (main ушёл вперёд относительно ревизии прогона)
    if sha:
        rc_h, _, _ = _git(child_root, "rev-parse", "--verify", sha)
        if rc_h != 0:
            reasons.append(f"ревизия прогона {sha[:12]} не найдена в репозитории — evidence устарел")
            revalidation = True
        elif base_resolvable:
            rc_a, ahead, _ = _git(child_root, "rev-list", "--count", f"{sha}..{base}")
            if rc_a == 0 and ahead.isdigit() and int(ahead) > 0:
                reasons.append(f"{base} ушёл вперёд на {ahead} коммит(ов) с момента прогона — "
                               "нужна ревалидация (старый evidence НЕ действителен для нового состояния)")
                revalidation = True

    # v3.0.10 (finding аудита P0): сохранённый base_sha исходного прогона — ИММУТАБЕЛЬНЫЙ контракт. Сверяем
    # его с ТЕКУЩИМ HEAD base-ветки. Если base переписан (сохранённый SHA больше НЕ предок текущего HEAD
    # базы — force-push назад / ветку пересоздали на несвязанном коммите) — это НЕ fast-forward, а СМЕНА
    # базы: старую работу нельзя выдать за проверенную против неё. base_rewritten не снимается force_resume.
    if saved_base_sha and base_resolvable:
        rc_cur, cur_base_sha, _ = _git(child_root, "rev-parse", "--verify", base)
        cur_base_sha = (cur_base_sha or "").strip()
        rc_saved, _, _ = _git(child_root, "rev-parse", "--verify", saved_base_sha)
        if rc_saved != 0:
            reasons.append(f"сохранённый base_sha {saved_base_sha[:12]} исходного прогона отсутствует в "
                           "репозитории — base переписан/недостижим; resume против него невозможен")
            revalidation = True
            base_rewritten = True
        elif rc_cur == 0 and cur_base_sha and cur_base_sha != saved_base_sha:
            rc_anc, _, _ = _git(child_root, "merge-base", "--is-ancestor", saved_base_sha, cur_base_sha)
            if rc_anc != 0:   # сохранённая база НЕ предок текущей -> переписана, а не ушла вперёд
                reasons.append(f"base '{base}' ПЕРЕПИСАН: сохранённый base_sha {saved_base_sha[:12]} не "
                               f"является предком текущего HEAD {cur_base_sha[:12]} (force-push/пересоздание). "
                               "Это смена базы, не fast-forward — resume против новой базы запрещён; "
                               "нужен явный replan (пересобрать и переисполнить) или отмена")
                revalidation = True
                base_rewritten = True
            else:
                # v3.0.14 (finding аудита #1, вариант B): FAST-FORWARD — сохранённый base_sha ПРЕДОК текущего
                # HEAD (база ушла вперёд A->B). Старый worktree/ветка WorkItem построены от A и НЕ
                # интегрированы с B; baseline считался на A. Отдать PR против B нельзя — работа не проверена
                # на интеграции с B. force_resume это НЕ снимает; нужен явный replan (свежий прогон от B).
                # Авто-интеграция+ревалидация (rebase onto B + повтор проверок) — v3.1.
                reasons.append(f"base '{base}' ушёл вперёд (fast-forward) с {saved_base_sha[:12]} до "
                               f"{cur_base_sha[:12]}: работа построена от старой базы и НЕ интегрирована с "
                               "новой. resume против сдвинувшейся базы запрещён — нужен явный replan "
                               "(свежий прогон от новой базы); force его не снимает (иначе PR против "
                               "непроверенной интеграции)")
                revalidation = True
                base_moved = True

    # устаревшие решения (ссылки на прошлую версию)
    for d in handoff.get("decisions") or []:
        if isinstance(d, dict) and d.get("stale"):
            reasons.append(f"решение {d.get('id')} помечено устаревшим — пересмотреть")
            revalidation = True

    if not reasons:
        reasons.append("состояние актуально: ветка на месте, base не двигался — можно продолжить с "
                       "последнего подтверждённого шага")
    return {"can_resume": True, "revalidation_needed": revalidation, "base_rewritten": base_rewritten,
            "base_moved": base_moved,
            "reasons": reasons,
            "handoff": {"next_action": handoff.get("next_action"),
                        "open_questions": handoff.get("open_questions"),
                        "resume_from_revision": sha},
            "next_action": handoff.get("next_action"),
            "resume_from_revision": sha}


def selftest():
    import tempfile
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    # build_handoff из ready-отчёта
    rep_ready = {"workitem_id": "feat-x", "ready_for_pr": True,
                 "commit": {"sha": "a" * 40, "branch": "ai-ops/feat-x", "evidence_on_exact_sha": True},
                 "loop": {"applied_writes": 2, "stopped": "done"},
                 "gates": {"evaluated": ["requirements", "code_review"], "unmet": []},
                 "not_yet": [], "checks": {}}
    h = build_handoff(rep_ready)
    expect("handoff: kind=RunHandoff", h["kind"] == "RunHandoff")
    expect("handoff: resume_from_revision = commit SHA", h["resume_from_revision"] == "a" * 40)
    expect("handoff: ready -> next_action про PR", "PR" in h["next_action"])
    expect("handoff: verification.passed непуст", set(h["verification"]["passed"]) == {"requirements", "code_review"})

    # build_handoff из not-ready (гейты unmet) -> next_action про гейты
    rep_block = {"workitem_id": "feat-y", "ready_for_pr": False,
                 "commit": {"sha": "b" * 40, "branch": "ai-ops/feat-y", "evidence_on_exact_sha": True},
                 "loop": {"applied_writes": 1, "stopped": "done"},
                 "gates": {"evaluated": ["requirements", "security"], "unmet": ["security"]},
                 "security_scan": {"secrets": [{"path": "a"}], "new_dependencies": [], "injection_flags": []},
                 "not_yet": ["draft PR"], "checks": {}}
    hb = build_handoff(rep_block)
    expect("handoff: unmet -> next_action про гейты", "security" in hb["next_action"])
    expect("handoff: known_risks содержит секреты", any("секрет" in r for r in hb["known_risks"]))
    expect("handoff: verification.failed содержит security", "security" in hb["verification"]["failed"])

    # resume_preflight: нет handoff -> can_resume False
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        subprocess.run(["git", "-C", td, "init", "-q"])
        subprocess.run(["git", "-C", td, "config", "user.email", "t@t"])
        subprocess.run(["git", "-C", td, "config", "user.name", "t"])
        (root / "f").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", td, "add", "-A"]); subprocess.run(["git", "-C", td, "commit", "-q", "-m", "i"])
        pf0 = resume_preflight(root, "nope")
        expect("resume: нет handoff -> can_resume=False", pf0["can_resume"] is False)

        # положим handoff, ветку не создаём -> revalidation (worktree утерян)
        rc, head, _ = _git(root, "rev-parse", "HEAD")
        fdir = root / "features" / "feat-z"; fdir.mkdir(parents=True)
        (fdir / "run-handoff.yaml").write_text(yaml.safe_dump(
            {"kind": "RunHandoff", "workitem_id": "feat-z", "resume_from_revision": head,
             "next_action": "продолжить", "open_questions": []}), encoding="utf-8")
        pf = resume_preflight(root, "feat-z", base="master")
        expect("resume: handoff есть -> can_resume=True", pf["can_resume"] is True)
        expect("resume: ветки нет -> revalidation_needed", pf["revalidation_needed"] is True)
        expect("resume: next_action перенесён из handoff", pf["next_action"] == "продолжить")

        # base ушёл вперёд -> revalidation
        (root / "g").write_text("y", encoding="utf-8")
        subprocess.run(["git", "-C", td, "add", "-A"]); subprocess.run(["git", "-C", td, "commit", "-q", "-m", "advance"])
        # текущая ветка (master/main) ушла вперёд от head
        cur = _git(root, "rev-parse", "--abbrev-ref", "HEAD")[1]
        pf2 = resume_preflight(root, "feat-z", base=cur)
        expect("resume: base ушёл вперёд -> revalidation + причина",
               pf2["revalidation_needed"] is True and any("вперёд" in r for r in pf2["reasons"]))
        # v2.105 (самоаудит): неразрешимая base-ветка -> НЕ молчим, требуем ревалидацию из осторожности
        pf3 = resume_preflight(root, "feat-z", base="no-such-branch-xyz")
        expect("resume: неразрешимая base -> revalidation + честная причина (не молча 'актуально')",
               pf3["revalidation_needed"] is True and any("не удалось разрешить" in r for r in pf3["reasons"]))
        expect("resume: fast-forward base -> base_rewritten=False (не переписан, снимается force)",
               pf2.get("base_rewritten") is False)

    # v3.0.10 (finding аудита P0): build_handoff несёт исходный BaseBinding; resume_preflight отличает
    # FAST-FORWARD (снимается force) от REWRITE базы (force-push/пересоздание — force НЕ снимает).
    hbb = build_handoff({"workitem_id": "bb", "ready_for_pr": True,
                         "commit": {"sha": "c" * 40, "branch": "ai-ops/bb", "evidence_on_exact_sha": True},
                         "base_binding": {"kind": "BaseBinding", "base_ref": "main",
                                          "base_sha": "d" * 40, "mode": "auto", "source": "upstream"},
                         "loop": {"applied_writes": 1, "stopped": "done"}, "gates": {}, "checks": {}})
    expect("v3.0.10 handoff: BaseBinding.base_sha сохранён в RunHandoff",
           hbb.get("base_binding", {}).get("base_sha") == "d" * 40)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for a in (("init", "-q"), ("config", "user.email", "t@t"), ("config", "user.name", "t")):
            _git(root, *a)
        (root / "f").write_text("x", encoding="utf-8")
        _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "base-A")
        base_A = _git(root, "rev-parse", "HEAD")[1]
        cur = _git(root, "rev-parse", "--abbrev-ref", "HEAD")[1]
        # рабочий коммит поверх base-A на ветке ai-ops/rw
        _git(root, "checkout", "-q", "-b", "ai-ops/rw")
        (root / "w").write_text("work", encoding="utf-8")
        _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "work")
        work_sha = _git(root, "rev-parse", "HEAD")[1]
        _git(root, "checkout", "-q", cur)
        fdir = root / "features" / "rw"; fdir.mkdir(parents=True)

        def _put_handoff(base_sha):
            (fdir / "run-handoff.yaml").write_text(yaml.safe_dump(
                {"kind": "RunHandoff", "workitem_id": "rw", "resume_from_revision": work_sha,
                 "base_binding": {"kind": "BaseBinding", "base_ref": cur, "base_sha": base_sha,
                                  "mode": "auto", "source": "upstream"},
                 "next_action": "продолжить", "open_questions": []}), encoding="utf-8")
        # (1) сохранённый base_sha == текущий HEAD базы -> НЕ переписан
        _put_handoff(base_A)
        pf_ok = resume_preflight(root, "rw", base=cur)
        expect("v3.0.10 resume: base_sha == текущий HEAD -> base_rewritten=False",
               pf_ok.get("base_rewritten") is False and pf_ok.get("base_moved") is False)
        # (1b) v3.0.14 (finding аудита #1, вариант B): FAST-FORWARD базы (A предок нового HEAD) -> base_moved
        #      (не rewrite); resume против сдвинувшейся базы запрещён без replan.
        (root / "b2").write_text("advance", encoding="utf-8")
        _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "base-B (ff)")
        pf_ff = resume_preflight(root, "rw", base=cur)   # handoff всё ещё хранит base_sha=base_A
        expect("v3.0.14 resume: fast-forward базы -> base_moved=True, base_rewritten=False",
               pf_ff.get("base_moved") is True and pf_ff.get("base_rewritten") is False
               and any("fast-forward" in r for r in pf_ff["reasons"]))
        # (2) base force-push НАЗАД на несвязанный коммит: пересоздаём ветку на orphan -> сохранённый
        #     base_sha больше НЕ предок нового HEAD -> REWRITE (не fast-forward)
        _git(root, "checkout", "-q", "--orphan", "reborn")
        (root / "z").write_text("reborn", encoding="utf-8")
        _git(root, "add", "-A"); _git(root, "commit", "-q", "-m", "unrelated")
        reborn_sha = _git(root, "rev-parse", "HEAD")[1]
        _git(root, "branch", "-f", cur, reborn_sha)
        _git(root, "checkout", "-q", cur)
        pf_rw = resume_preflight(root, "rw", base=cur)
        expect("v3.0.10 resume: base переписан (не предок) -> base_rewritten=True + причина",
               pf_rw.get("base_rewritten") is True
               and any("ПЕРЕПИСАН" in r for r in pf_rw["reasons"]))

        # v3.0.12 (finding аудита блок B): битый/пустой RunHandoff -> НЕ «resume безопасен» (fail-closed).
        (fdir / "run-handoff.yaml").write_text("", encoding="utf-8")   # оборванная запись
        pf_empty = resume_preflight(root, "rw", base=cur)
        expect("v3.0.12: пустой RunHandoff -> can_resume=False (не тихий 'актуально')",
               pf_empty["can_resume"] is False and any("повреждён" in r for r in pf_empty["reasons"]))
        (fdir / "run-handoff.yaml").write_text("kind: RunHandoff\n:::not yaml:::\n  - [", encoding="utf-8")
        pf_bad = resume_preflight(root, "rw", base=cur)
        expect("v3.0.12: битый YAML RunHandoff -> can_resume=False",
               pf_bad["can_resume"] is False)

    print("run_handoff selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    ap = argparse.ArgumentParser(prog="run_handoff.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("report"); b.add_argument("--out"); b.add_argument("--root")
    r = sub.add_parser("resume-preflight"); r.add_argument("child_root"); r.add_argument("workitem_id")
    r.add_argument("--base", default="main"); r.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "build":
        rep = json.loads(Path(a.report).read_text(encoding="utf-8"))
        h = build_handoff(rep, work_root=a.root)
        text = yaml.safe_dump(h, allow_unicode=True, sort_keys=False)
        if a.out:
            Path(a.out).write_text(text, encoding="utf-8")
            print(f"RUN-HANDOFF: записан {a.out}")
        else:
            print(text)
        return 0
    if a.cmd == "resume-preflight":
        pf = resume_preflight(a.child_root, a.workitem_id, base=a.base)
        if a.json:
            print(json.dumps(pf, ensure_ascii=False, indent=2))
        else:
            print(f"RESUME-PREFLIGHT {a.workitem_id}: can_resume={pf['can_resume']} · "
                  f"revalidation_needed={pf.get('revalidation_needed')}")
            for r_ in pf["reasons"]:
                print(f"  · {r_}")
            if pf.get("next_action"):
                print(f"  next: {pf['next_action']}")
        return 0 if pf["can_resume"] else 1
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
