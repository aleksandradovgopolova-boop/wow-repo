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
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

from ai_ops_kit.shared import lifecycle_store as _ls   # v3.0.12: fail-closed чтение RunHandoff (битый != «resume безопасен»)


def _git(root, *args):
    from ai_ops_kit.shared import gitio
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
        completed.append(f"применено правок через брокера: {loop['applied_writes']}")
    elif commit.get("sha") and commit.get("changed_files"):
        # Работа произведена другим каналом (writer своими инструментами, shell, свой коммит
        # модели). Промолчать здесь значило бы передать исполнителю «сделано ничего».
        completed.append(f"изменено файлов: {len(commit['changed_files'])} "
                         f"({commit.get('produced_by') or 'не через брокера'})")
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


def main(argv):
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
