#!/usr/bin/env python3
"""Человекочитаемый вывод результата прогона `ai_ops_run`.

Здесь живёт печать отчёта контроллера задачи для человека: `_print_pipeline`
(вердикт собранного движка), `_print_contour_consistency` (находки гейта
связности контуров) и `print_human` (диспетчер печати по форме отчёта).
Вынесено из god-модуля `ai_ops_run.py` без изменения поведения: сам контроллер
ре-экспортирует эти имена, поэтому внешние вызовы (`ai_ops_run.print_human`,
`ai_ops_run._print_pipeline`, `ai_ops_run._print_contour_consistency`) работают
по-прежнему.
"""
from __future__ import annotations

from ai_ops_kit.engine.pipeline_helpers import work_produced, _stacks_human   # noqa: E402


def _style_finding_text(r):
    """Находка «стиль кода никто не проверяет» из отчёта прогона (checks.lint.finding) -> str|None."""
    return (((r.get("checks") or {}).get("lint") or {}).get("finding") or {}).get("text")


def _print_pipeline_product(r):
    """СВОДКА прогона для человека (аудитория product): что произошло → честные оговорки → шаг.

    Тех-разбор (SHA, tool-loop, worktree, context-токены, spec-level) сюда НЕ идёт — он «по запросу».
    Но ОГОВОРКИ («готово ≠ проверено») сохраняются целиком: подменять признание утверждением значит
    зеленить непроверенное — это и есть кардинальный запрет кита.
    """
    ready = r.get("ready_for_pr")
    commit = r.get("commit") or {}
    n_files = len(commit.get("changed_files") or []) if commit.get("sha") else 0
    gates = r.get("gates") or {}
    files = f" Файлов изменено: {n_files}." if n_files else ""
    if ready:
        print("Готово: изменение внесено, проверки закрыты." + files)
    else:
        unmet = gates.get("unmet") or []
        tail = (f" Не закрыто: {', '.join(unmet)}." if unmet else "")
        print("Пока не готово к PR." + files + tail)

    # Честные оговорки — «готово» не должно читаться как «сверено с ожиданием».
    _ac = r.get("acceptance_criteria") or {}
    if not _ac.get("declared") and ready:
        print("  ⚠ критериев приёмки не было — «готово» значит «внесено и проверки закрыты», "
              "а не «сверено с тем, что ты ждал».")
    elif _ac.get("declared") and not _ac.get("verified"):
        print("  ⚠ критерии приёмки не сверялись с результатом.")
    elif _ac.get("declared") and not _ac.get("met_all"):
        print(f"  ⚠ критерии приёмки: не выполнено {len(_ac.get('unmet') or [])} из {_ac.get('count')}.")
    # #1183: стиль кода без линтера — находка владельцу; текст написан слоем речи (ui), здесь
    # только показывается. Без этой строки находка жила бы лишь в предупреждениях гейта.
    _caveats = [r.get("tests_warn") and "тестов в стеке нет — проверка тестами пропущена.",
                (r.get("isolation") or {}).get("sandboxed") is False and work_produced(r)
                and "прогон шёл без песочницы — изоляция условна (управляемость, не защита).",
                (r.get("work_package") or {}).get("should_decompose")
                and "задача крупновата — стоит разбить на части.",
                _style_finding_text(r)]
    for _c in (c for c in _caveats if c):
        print(f"  ⚠ {_c}")

    # Следующий шаг.
    pr = r.get("draft_pr") or {}
    if pr.get("url"):
        print(f"\nДальше: черновик PR открыт — {pr['url']}.")
    elif ready:
        print("\nДальше: открыть черновик PR — ai-ops run --open-pr. Изменения в отдельной ветке, "
              "main не тронут.")
    else:
        print("\nДальше: закрыть оставшееся (см. оговорки выше) и повторить.")
    # Исход #958: обещание НЕ кончается на PR. Раньше нарратив завершения обрывался на «открыть
    # черновик PR» — и человек не знал, что после слияния кит вернётся и покажет ЭФФЕКТ на продукт.
    # Обещание честное: это будущий шаг ПОСЛЕ merge, а не уже известный результат, и о самом слиянии
    # кит не узнаёт автоматически. Точную команду (`ai-ops readout …`) человеку в лицо не даём —
    # она в технических деталях; здесь только продуктовый смысл.
    if ready or pr.get("url"):
        print("\nНа PR история не кончается: когда сольёшь это в основную ветку, приходи за итогом — "
              "я покажу, что от этой работы изменилось для продукта (эффект, а не просто «влито»). "
              "Это отдельный шаг уже после слияния: сам момент merge я не отслеживаю.")
    print("\nТехнические детали прогона — по запросу («покажи технические детали»).")
    _print_contour_consistency(r)


def _print_pipeline(r, audience="technical"):
    """Человекочитаемый вывод отчёта собранного движка (kind=execution-pipeline).

    finding аудита (P0.1): print_human безусловно читал ключи controller-отчёта
    (status/execution/required_tracks) и падал KeyError на pipeline-отчёте. Формат отчёта
    движка иной (loop/commit/checks/gates/ready_for_pr) — печатаем его явно.

    #702-work: `audience="product"` печатает СВОДКУ (вердикт + файлы + честные оговорки + шаг),
    а не тех-разбор. Дефолт `technical` — полная стена (и для тестов, зовущих напрямую).
    """
    if r.get("status") == "error":
        print(f"ai-ops run (pipeline) → WorkItem {r.get('workitem_id')} [ОШИБКА]")
        print(f"  {r.get('error')}")
        return
    if audience == "product":
        return _print_pipeline_product(r)
    loop = r.get("loop") or {}
    commit = r.get("commit") or {}
    gates = r.get("gates") or {}
    ready = r.get("ready_for_pr")
    print(f"ai-ops run (pipeline) → WorkItem {r.get('workitem_id')} "
          f"[{'READY_FOR_PR' if ready else 'NOT_READY'}]")
    prov = r.get("provider") or "?"
    model = f"/{r['model']}" if r.get("model") else ""
    print(f"  base_workflow: {r.get('base_workflow')} · провайдер: {prov}{model} ({r.get('runtime')})")
    _stacks = (r.get("profile") or {}).get("display") or _stacks_human(r.get("profile"))
    print(f"  стек: {', '.join(_stacks) or 'не определён'}")
    _changed = commit.get("changed_files")
    _files_note = f" · файлов в коммите {len(_changed)}" if _changed is not None and commit.get("sha") else ""
    print(f"  tool-loop: {loop.get('stopped')} · шагов {loop.get('steps')} · "
          f"правок через брокера {loop.get('applied_writes')} · "
          f"отклонено {loop.get('denied')}{_files_note}")
    # F-017 + находка ии-среды: правок через брокера 0, а файлы в коммите есть — работа сделана
    # другим каналом. Прежде строка «правок 0» стояла первой и читалась как «ничего не произошло»,
    # хотя коммит был. Теперь канал НАЗВАН, а не выведен читателем.
    _by = {"broker": "через брокера", "shell": "напрямую в дереве (writer или shell)",
           "model-commit": "модель закоммитила сама"}.get(commit.get("produced_by"))
    if _changed and not (loop.get("applied_writes") or 0):
        print(f"    работа произведена {_by or 'не через брокера'}: {', '.join(_changed[:5])}"
              + (f" и ещё {len(_changed) - 5}" if len(_changed) > 5 else ""))
    # F-012: движок никого не позвал и ничего не написал — назвать режим и что делать дальше.
    # Раньше это читалось только по косвенным признакам (созданный worktree + «not_yet: живой
    # предложитель»), и исполнитель догадывался, что код должен написать он.
    # Тот же предикат, что и у статуса работы: «движок ничего не написал» нельзя объявлять по
    # счётчику брокера, если в коммите лежат файлы.
    if (r.get("provider") == "mock") and not work_produced(r):
        _wt = (r.get("isolation") or {}).get("worktree")
        _br = (r.get("commit") or {}).get("branch") or f"ai-ops/{r.get('workitem_id')}"
        print("  исполнитель: внешний агент — движок с провайдером mock кода НЕ пишет")
        print(f"    рабочий каталог: {_wt or 'основное дерево'} · ветка: {_br}")
        print("    напиши правки там, закоммить, затем переоцени гейты: "
              f"ai-ops run \"<задача>\" . --feature {r.get('workitem_id')} --execute --reevaluate-only")
        print("    или задай живого провайдера: --provider claude-cli (нужен claude в PATH)")
    _iso = r.get("isolation") or {}
    iso = _iso.get("worktree")
    print(f"  изоляция: {iso or 'основное дерево (без worktree)'}")
    # P1 (аудит «непесочный дефолт + сеть ON»): пониженную изоляцию НАЗЫВАЕМ, а не молчим. Дефолт
    # sandbox=False не флипаем — иначе регресс на прогонах без Docker; находка закрывается ЧЕСТНОСТЬЮ.
    # Ноту даём ТОЛЬКО когда прогон реально работал (были правки/коммит) без песочницы: work_produced
    # отсекает dry-run/preview/reevaluate — нет записей, нет и ноты. Читаем поле отчёта, не пересчитываем:
    # уберут isolation.sandboxed -> нота молчит (fail-closed, поза снова невидима).
    if _iso.get("sandboxed") is False and work_produced(r):
        _net = ("сеть доступна модельному shell" if _iso.get("network") == "on"
                else "сеть ограничена")
        print(f"  ⚠ прогон без песочницы (sandbox off), {_net}; изоляция условна — управляемость "
              "брокером, не защита от недоверенного кода. Для чувствительных прогонов включите её: "
              "--sandbox / контейнер run-sandboxed.sh")
    # F-014: от какой базы отрезан worktree — видно сразу, а не выясняется конфликтом при слиянии.
    _bb = r.get("base_binding") or (r.get("delivery") or {}).get("base_binding") or {}
    if _bb.get("base_ref"):
        _src = {"current-branch": "текущая ветка", "upstream": "upstream",
                "remote-default": "remote default", "explicit-local": "задана явно",
                "explicit-remote": "задана явно (origin)"}.get(_bb.get("source"), _bb.get("source"))
        print(f"  база worktree: {_bb['base_ref']} {(_bb.get('base_sha') or '')[:12]} ({_src})")
    if commit.get("sha"):
        print(f"  commit: {commit['sha'][:12]} на {commit.get('branch')} · "
              f"evidence на точном SHA: {commit.get('evidence_on_exact_sha')} · "
              f"дерево чистое: {commit.get('tree_clean_before_checks')}")
    if r.get("exemptions"):
        print(f"  освобождены (не применимо): {', '.join(r['exemptions'])}")
    for _w in (w for w in (r.get("tests_warn"), _style_finding_text(r)) if w):
        print(f"  ⚠ {_w}")
    # B2-14: «доставлено» не должно читаться как «критерии выполнены». Прогон на живом продукте отдал
    # PR со `sha_verified: True`, а критерий приёмки остался невыполненным — и в отчёте об этом не
    # было ни строки. Непроверенное называется непроверенным ЗДЕСЬ, в том же выводе, где стоит
    # «готово», а не только в JSON.
    #
    # ВТОРАЯ ПОЛОВИНА: теперь сверка есть, и у неё ТРИ исхода, а не один. «Сверено» без разбора
    # выполненного было бы тем же смешением, что и «доставлено» = «выполнено»: сверка, нашедшая
    # невыполненный критерий, обязана назвать ЕГО, а не сообщить, что она состоялась.
    _ac = r.get("acceptance_criteria") or {}
    # B2-18 (живой прогон 14.08.2026): когда критериев НЕ БЫЛО вовсе, вывод молчал о них совсем — и
    # `delivered` читалось как «проверено». Урок B2-14 («доставлено ≠ выполнено») был закрыт только
    # для случая, когда критерии есть. Отсутствие критериев — тоже факт о работе, и владелец узнаёт
    # о нём в том же выводе, где стоит «готово».
    if not _ac.get("declared") and r.get("ready_for_pr"):
        print("  ⚠ критериев приёмки не было объявлено — проверять было нечего; «готово» здесь "
              "означает «изменение внесено и гейты закрыты», а не «результат сверен с ожиданием»")
    if _ac.get("declared") and not _ac.get("verified"):
        print(f"  ⚠ критерии приёмки НЕ сверялись с результатом: {_ac.get('reason')}")
    elif _ac.get("declared") and not _ac.get("met_all"):
        _un = [c for c in (_ac.get("criteria") or []) if c.get("status") == "unmet"]
        print(f"  ⚠ критерии приёмки сверены: НЕ ВЫПОЛНЕНО {len(_ac.get('unmet') or [])} "
              f"из {_ac.get('count')} ({', '.join(_ac.get('unmet') or [])})")
        for c in _un[:5]:
            print(f"      · {c['id']}: {c['text'][:110]}")
            if c.get("reason"):
                print(f"        основание ревьюера: {str(c['reason'])[:140]}")
    elif _ac.get("declared"):
        # Сила основания названа и здесь: «выполнены все» с подтверждённой цитатой и то же самое на
        # слове судьи — разные факты. Смешать их значило бы вернуть ложный green с другого конца.
        _weak = _ac.get("judge_only") or []
        print(f"  критерии приёмки сверены с результатом: выполнены все {_ac.get('count')} "
              f"· подтверждено цитатой {_ac.get('quote_verified')} "
              f"({_ac.get('verifier')}, прочитано файлов: {len(_ac.get('reads') or [])})")
        if _weak:
            print(f"      ⚠ только суждение судьи, без машинного подтверждения: {', '.join(_weak)}"
                  f" — эти критерии проверь сам")
    print(f"  гейты: оценено {len(gates.get('evaluated') or [])} · "
          f"не закрыто {gates.get('unmet') or []} · блокирует: {gates.get('blocked')}")
    # РАЗБИВКА ЗАКРЫТИЯ — счёт «N из M» и id гейтов ЗДЕСЬ, в technical (#958): продуктовому человеку
    # её печатает pipeline_stages словами, а машинный счёт и внутренние id — только на этом уровне.
    _cl = gates.get("closure") or {}
    if _cl:
        _op = _cl.get("judged_or_human") or []
        print(f"  разбивка закрытия: машиной {(_cl.get('counts') or {}).get('validator', 0)} "
              f"из {len(gates.get('evaluated') or [])}"
              + (f"; мнением/человеком: {', '.join(_op)}" if _op else "; мнением не закрыт ни один"))
    lc = r.get("lifecycle")
    if lc:
        pf = (lc.get("concurrency_preflight") or {})
        if isinstance(pf, dict) and pf.get("error"):
            _pf_note = f"preflight НЕ ВЫПОЛНЕН ({pf['error']}) — о конфликтах ничего не известно"
        elif isinstance(pf, dict) and pf.get("conflicts") is None and "error" in pf:
            _pf_note = "preflight не выполнен"
        else:
            _pf_note = f"preflight-конфликтов: {len(pf.get('conflicts') or []) if isinstance(pf, dict) else 0}"
        print(f"  lifecycle: WorkItem+RunPlan+active-work+run-report записаны · {_pf_note}")
    cb = r.get("context_bundle")
    if cb:
        print(f"  context: ~{cb['estimated_tokens']}/{cb['context_budget']} ток."
              f"{' ⚠OVERFLOW' if cb.get('overflow') else ''} · агентов {len(cb['agents'])} · "
              f"исключено {cb['excluded_count']} источн.")
    sc = r.get("spec_coverage")
    if sc:
        esc = f" (эскалация с L{sc['escalated_from']})" if sc.get("escalated_from") is not None else ""
        print(f"  spec-level: {sc['level_name']}{esc} · не хватает разделов: "
              f"{len(sc['blocking_missing'])} · needs_human: {len(sc['needs_human'])}")
    wp = r.get("work_package")
    if wp and wp.get("should_decompose"):
        print(f"  ⚠ пакет не атомарен — рекомендуется декомпозиция ({', '.join(wp['decomposition_axes'])})")
    pr = r.get("draft_pr")
    if pr:
        print(f"  draft PR: {pr.get('status')}" + (f" — {pr.get('url')}" if pr.get('url') else ""))
    # Исход #958: пост-релизный readout ПОСЛЕ merge — точная команда живёт в технических деталях
    # (аудитория product видит только продуктовое обещание в _print_pipeline_product). Кит не
    # узнаёт о слиянии сам — это шаг человека после merge, поэтому команда, а не «автомагия».
    if r.get("ready_for_pr") or (pr and pr.get("url")):
        _fid = r.get("workitem_id") or "<id>"
        print(f"  после слияния — эффект на продукт (outcome): ai-ops readout --feature {_fid} "
              "(нужна PRR-ссылка; о самом merge кит не узнаёт автоматически)")
    for n in r.get("not_yet") or []:
        print(f"  · not_yet: {n}")
    _print_contour_consistency(r)


def _print_contour_consistency(r):
    """Находки гейта связности контуров — человеку, в конце прогона.

    ГЕЙТ, ЧЬИ НАХОДКИ НЕ ВИДНЫ, — ЭТО ГЕЙТ, КОТОРОГО НЕТ. Гейт исполнялся, считал находки и писал
    их в evidence; вывод прогона о них молчал. Единственное место, где «описание продукта отстало от
    кода» было видно, — yaml-артефакт, который человек не открывает. Это тот же дефект, что
    «переводчик написан и не подключён», только дороже: здесь молчит главная проверка релиза 3.35.

    Печатается ПОСЛЕ вердикта прогона и отдельным блоком: находка advisory, она не отменяет
    результат, но и не должна тонуть среди строк о шагах и коммитах.
    """
    cc = r.get("contour_consistency") or {}
    rep = cc.get("report")
    if not rep:
        # Гейт не исполнялся (не коммитили) либо проверка не удалась — evidence уже сказал об этом
        # своим `warn`, и выдумывать здесь ещё одно сообщение незачем.
        return
    try:
        from ai_ops_kit.ui import presenter
        msg = presenter.from_contour_consistency(rep)
        if msg.get("status") == "ok":
            return          # согласовано — отдельного блока не нужно, вердикт прогона уже сказал всё
        print()
        print(presenter.render(msg, audience=presenter.audience_from_config(
            r.get("child_root") or ".")))
    except Exception as _e:  # noqa: BLE001 — вывод отчёта не роняет прогон...
        # ...но и молчать нельзя: молчание здесь неотличимо от «расхождений нет».
        print(f"  ⚠ находки связности контуров есть, показать не смог: {type(_e).__name__}: {_e}")


def print_human(r):
    # pipeline-отчёт имеет свою форму — не смешиваем с controller-отчётом (P0.1)
    if r.get("kind") == "execution-pipeline":
        # #702-work: на аудитории product человек видит СУТЬ результата (готово/нет + что изменилось
        # + честные оговорки + следующий шаг), тех-разбор — «по запросу» (communication-policy:
        # technical_details: on_request). Тесты зовут _print_pipeline напрямую -> дефолт technical.
        _aud = "technical"
        _cr = r.get("child_root")
        if _cr:
            try:
                from ai_ops_kit.ui import presenter
                _aud = presenter.audience_from_config(_cr)
            except Exception:  # noqa: BLE001 — печать результата не роняет прогон
                _aud = "technical"
        return _print_pipeline(r, audience=_aud)
    # Минимальный отчёт (например, отказ active-work/preflight ДО классификации) не несёт
    # base_workflow/треков. Раньше вывод для человека падал на нём KeyError('base_workflow') —
    # прогон завершался, а печать результата роняла процесс (замер поля 01.09.2026). Печатаем коротко.
    if "base_workflow" not in r:
        print(f"ai-ops run → WorkItem {r.get('workitem_id', '?')} [{r.get('status', '?')}]")
        if r.get("blocked_by"):
            print(f"  заблокировано: {r['blocked_by']}")
        if r.get("error"):
            print(f"  {r['error']}")
        return
    print(f"ai-ops run → WorkItem {r['workitem_id']} [{r['status']}]")
    print(f"  base_workflow: {r['base_workflow']} · execution: {r['execution']} ({r['runtime']})")
    if r["required_tracks"]:
        print(f"  треки (required): {', '.join(r['required_tracks'])}")
    if r["conditional_tracks"]:
        print(f"  треки (conditional): {', '.join(r['conditional_tracks'])}")
    print(f"  гейты ({len(r['gates'])}): {', '.join(r['gates'])}")
    for s in r["skipped_tracks"]:
        print(f"  · пропущен {s['track']}: {s['reason']}")
    if r["status"] == "planned":
        print("  → план и каркас готовы; стадии исполняет рантайм (claude-code) по плану.")
