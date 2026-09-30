#!/usr/bin/env python3
"""FoundationProposal — «предложение по фундаменту» ОДНИМ брифингом после обновления кита.

ЗАЧЕМ. Когда кит в уже-подключённой дочке обновился, `update` заканчивался на «N изменений,
создайте PR» и никуда не переходил: владелец узнавал, что файлы поменялись, но не ЧТО это ему даёт
и что теперь делать с фундаментом. Онбординг-поверхности для ОБНОВЛЁННОЙ дочки не было вовсе — этот
модуль её и есть (пункт ROADMAP «Дальше», Claude-native онбординг).

ЧТО ЭТО НЕ. Не новый вычислитель и не второй реестр. Все кирпичи уже есть; здесь только ОРКЕСТРАЦИЯ
их в один брифинг из частей:
  1. ЧТО НОВОГО   — дельта версии кита и стандарта (из last-update-report.json + planning.standard);
  2. РЕВЬЮ ФУНДАМЕНТА — единый вердикт (planning.product_contract.resolve/validate);
  3. ЧТО ПРЕДЛАГАЮ — 1-3 рекомендации «рекомендую X, потому что Y» (planning.next_work +
     пробелы вердикта), а не голый список;
  4. STORYBOOK   — честная зрелость UI-evidence (ui.ui_readiness.assess).

#958 (`kit_recommends_what_the_product_needs`, сестра `advise`): в ЛИЦО человека `propose` ВЕДЁТ с
того, что нужно ПРОДУКТУ (`intelligence.product_advice.recommend`, впрыснут из обработчика интента),
а перечисленные выше части — про НАСТРОЙКУ САМОГО КИТА — подаёт ОТДЕЛЬНО, продуктовым языком и без
жаргона: версии/контуры/пути/Storybook/workflow/блокеры фундамента уходят в технические детали (по
запросу). Инвариант честности `product_advice` не трогаем: нет продуктового сигнала -> так и говорим,
потребность не выдумываем; настройка кита остаётся честной (незакрытое называется незакрытым).

СЛОЙ. Модуль живёт в `cli` (точка входа): ему МОЖНО звать planning и ui вниз. Здоровье/риски меряет
`intelligence` (выше planning) — они ВПРЫСКИВАЮТСЯ параметрами из обработчика интента (как в
`contract`), а не импортируются здесь, иначе оркестратор потянул бы intelligence.

ЧЕСТНЫЕ ГРАНИЦЫ (не сглаживаем): нет отчёта об обновлении -> «сведений об изменениях нет»; не
UI-продукт -> Storybook `absent` называется прямо. Превью Storybook в PR (CI-артефакт статической
сборки, без внешнего хостинга/секретов) кит доставляет отдельным workflow: доставлен -> брифинг
говорит «доступно/включено», не доставлен -> называет условие (UI-продукт со Storybook-билдом).
Авто-подъём Storybook (npm i / storybook init) и внешний хостинг остаются за владельцем — кит их
не делает и не обещает.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml


def _read_last_update_report(child_root) -> dict | None:
    """Отчёт последнего обновления (`.ai/runtime/last-update-report.json`) -> dict | None.

    None — не «ничего не менялось», а «сведений нет»: брифинг обязан их различать и не выдавать
    отсутствие отчёта за пустое обновление.
    """
    p = Path(child_root) / ".ai" / "runtime" / "last-update-report.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _child_standard_version(child_root) -> int | None:
    """Версия стандарта, объявленная дочкой в `.ai-ops.yaml -> standard.version`. -> int | None."""
    p = Path(child_root) / ".ai-ops.yaml"
    if not p.is_file():
        return None
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    try:
        v = (data.get("standard") or {}).get("version")
        return int(v) if v is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def whats_new(child_root) -> dict:
    """ЧТО НОВОГО: дельта версии кита + СМЫСЛ изменений + дельта стандарта.

    Смысл — из `changelog_slice` отчёта (срез заголовков CHANGELOG между старой и новой версией,
    записанный при обновлении): называем ЧТО нового, а не только число файлов. Среза нет (старый
    отчёт / CHANGELOG был недоступен) -> честный откат на версию+число, без выдумок. Дельта
    стандарта — из planning.standard.status: `behind=True` значит, что требования к репозиторию
    стали НОВО обязательными. Нет отчёта -> `update_report_present=False`.
    """
    from ai_ops_kit.planning import standard
    rep = _read_last_update_report(child_root)
    std = standard.status(_child_standard_version(child_root))
    out = {"update_report_present": rep is not None, "standard": std}
    if rep:
        changes = [c for c in (rep.get("managed_changes") or []) if isinstance(c, dict)]
        out.update(from_version=rep.get("from_version"), to_version=rep.get("to_version"),
                   changed_files=len(changes),
                   change_paths=[c.get("path") for c in changes[:5]],
                   changelog_slice=[c for c in (rep.get("changelog_slice") or [])
                                    if isinstance(c, str) and c.strip()],
                   summary_text=(rep.get("report") or "").strip() or None)
    return out


def foundation_review(child_root, *, health=None, risks=None):
    """РЕВЬЮ ФУНДАМЕНТА: единый контракт продукта + вердикт. -> (contract, verdict).

    Ничего не считает сам — зовёт planning.product_contract (единственный источник вердикта). Битый
    реестр артефактов пробрасывается наружу исключением: «не знаю» не выдаём за «valid».
    """
    from ai_ops_kit.planning import product_contract
    contract = product_contract.resolve(child_root, health=health, risks=risks)
    verdict = product_contract.validate(child_root, health=health)
    return contract, verdict


def recommendations(child_root, verdict, *, budget_left=None, me=None) -> list:
    """ЧТО ПРЕДЛАГАЮ: 1-3 рекомендации «X, потому что Y» из пробелов плана и фундамента.

    Источник «что взять» — planning.next_work (тот же, что у `next`): его допуск и ранжирование не
    дублируем. Пробелы фундамента (blocking из вердикта) идут следом. Каждая рекомендация несёт
    ПРИЧИНУ — вопрос без обоснования запрещён политикой (recommend-not-enumerate).
    """
    from ai_ops_kit.planning import contours as _contours
    from ai_ops_kit.planning import delivery_plan as _plan
    from ai_ops_kit.planning import next_work
    recs = []
    try:
        nx = next_work.compute(child_root, budget_left=budget_left, me=me)
    except (_plan.PlanCorrupt, _contours.ModelCorrupt) as e:
        recs.append({"what": "починить описание плана продукта",
                     "why": f"по нему нельзя посоветовать работу: {e}"})
        return recs
    if not nx.get("plan_present") or nx.get("plan_is_template"):
        recs.append({"what": "собрать план работ из фактов репозитория "
                             "(./ai-ops model → ./ai-ops bootstrap --apply)",
                     "why": nx.get("gap") or "плана работ в проекте пока нет"})
    else:
        nb = nx.get("next_best")
        if nb:
            recs.append({"what": f"взять «{nb['title']}»",
                         "why": "; ".join(nb.get("why") or []) or "готова по зависимостям"})
    for b in (verdict.get("blocking") or []):
        if len(recs) >= 3:
            break
        recs.append({"what": "закрыть пробел фундамента", "why": b})
    return recs[:3]


def storybook(child_root) -> dict:
    """STORYBOOK: честная зрелость UI-evidence. -> отчёт ui_readiness.assess (absent не маскируем)."""
    from ai_ops_kit.ui import ui_readiness
    return ui_readiness.assess(child_root)


def foundation_freshness(child_root) -> dict:
    """СВЕЖЕСТЬ ФУНДАМЕНТА: отстал ли фундамент от нижних доков и потока фич + кандидат на пересмотр.

    Тонкая обёртка над planning.foundation_freshness (единственный вычислитель): часть того же
    «Ревью фундамента», не второй путь рассуждения. Битая модель контуров -> честное состояние
    `unknown`, а не выдуманное «свежо»: advisory-часть брифинга не роняет весь брифинг.
    """
    from ai_ops_kit.planning import contours as _contours
    from ai_ops_kit.planning import foundation_freshness as _ff
    try:
        return _ff.assess(child_root)
    except _contours.ModelCorrupt as e:
        return {"schema_version": 1, "kind": "foundation-freshness", "enforcement": "advisory",
                "candidate": None, "state": "unknown",
                "note": f"модель контуров недостоверна — свежесть фундамента не измерена: {e}"}


def build_briefing(child_root, *, health=None, risks=None, budget_left=None, me=None,
                   product_advice=None) -> dict:
    """Собрать весь брифинг ОДНИМ объектом. Ничего не пишет.

    health/risks впрыскиваются сверху (их меряет intelligence, слой выше planning) — как в `contract`.
    `product_advice` — «что нужно ПРОДУКТУ» (#958): его меряет intelligence.product_advice, поэтому
    он тоже ВПРЫСКИВАЕТСЯ из обработчика интента, а не считается здесь (иначе оркестратор потянул бы
    intelligence). Нет впрыска -> None: человеко-обращённый вывод честно скажет «данных не хватает».
    """
    contract, verdict = foundation_review(child_root, health=health, risks=risks)
    return {
        "schema_version": 1, "kind": "foundation-proposal",
        "repository": str(Path(child_root)),
        "whats_new": whats_new(child_root),
        "contract": contract,
        "verdict": verdict,
        "recommendations": recommendations(child_root, verdict,
                                           budget_left=budget_left, me=me),
        "foundation_freshness": foundation_freshness(child_root),
        "storybook": storybook(child_root),
        "product_advice": product_advice,
    }


def _storybook_line(sb: dict) -> str:
    """Честная строка про Storybook: называет зрелость И честную границу превью в PR.

    Превью Storybook в PR = CI-артефакт статической сборки (без внешнего хостинга/секретов). Доставлен
    workflow -> «доступно/включено»; не доставлен -> называем условие, а не обещание. Авто-подъём
    Storybook (npm i / storybook init) и внешний хостинг за меня — по-прежнему нет, это владелец."""
    m = sb.get("storybook_maturity")
    if sb.get("preview_workflow"):
        tail = (" Превью Storybook в PR ВКЛЮЧЕНО: workflow доставлен — на PR с UI-изменениями CI "
                "соберёт Storybook твоим build-скриптом и выложит артефактом (без внешнего хостинга). "
                "Авто-подъём Storybook и внешний хостинг за меня — по-прежнему нет (это владелец).")
    else:
        tail = (" Превью Storybook в PR я доставляю отдельным workflow (CI-артефакт сборки, без "
                "внешних сервисов) — он включится, когда у репозитория есть Storybook-билд. "
                "Авто-подъём Storybook и внешний хостинг за тебя не делаю — это владелец.")
    if m == "absent":
        return ("Storybook не настроен — если это не UI-продукт, так и должно быть (не маскирую). "
                "Могу дать шаблон скрипта; ставить зависимости за тебя не буду." + tail)
    if m in ("configured", "runnable"):
        return (f"Storybook: {m}. {sb.get('recommendation', '')} Шаблон скрипта дам по запросу."
                + tail)
    return ("Storybook: evidence собирается (verified) — адаптер строит реальный UIEvidenceBundle."
            + tail)


def _foundation_freshness_line(ff: dict) -> str | None:
    """Одна человеческая строка про свежесть фундамента — ТОЛЬКО когда есть измеримый кандидат.

    Инвариант честности: нет кандидата (свежо / не с чем сравнить / нет git-даты) -> строки НЕТ,
    молчим. Устаревание не выдумываем, «не знаю» за «устарел» не выдаём."""
    cand = (ff or {}).get("candidate")
    if not cand:
        return None
    return ("возможно, стоит пересмотреть фундамент продукта: " + cand.get("reason", "")
            + " — это совет, а не обязательство")


def _foundation_freshness_technical(ff: dict) -> dict:
    """Подробности свежести фундамента — в технические детали (по запросу)."""
    if not ff:
        return {}
    cand = ff.get("candidate")
    out = {"свежесть фундамента": ff.get("state"), "свежесть фундамента · пояснение": ff.get("note")}
    if cand:
        out["свежесть фундамента · кандидат"] = (
            f"{cand.get('path')} — новее: {cand.get('newer_references_count', 0)} нижних доков, "
            f"{cand.get('newer_features', 0)} фич; отставание ≈ {cand.get('lag_days')} дн.")
    return out


def _whats_new_line(wn: dict) -> str:
    """Одна человеческая строка «что нового»: НАЗЫВАЕТ смысл изменений (срез CHANGELOG), а не только
    число файлов. Нет среза (старый отчёт / CHANGELOG недоступен) -> честный откат на версию+число."""
    if not wn.get("update_report_present"):
        line = ("Сведений о последнем обновлении в проекте нет — показываю фундамент как есть, "
                "без разбора «что нового».")
    else:
        frm, to = wn.get("from_version") or "—", wn.get("to_version") or "—"
        slice_ = wn.get("changelog_slice") or []
        if slice_:
            line = (f"Кит обновлён {frm} → {to}. Что нового: "
                    + "; ".join(slice_[:4]) + ".")
        else:
            line = (f"Кит обновлён {frm} → {to}: "
                    f"{wn.get('changed_files', 0)} изменени(й) в managed-слое.")
    std = wn.get("standard") or {}
    if std.get("behind"):
        line += (f" И требования репозитория обновились: стандарт {std.get('installed')} → "
                 f"{std.get('available')} (появилось ново-обязательное — детали в ./ai-ops contract).")
    return line


def _kit_setup_line(verdict: dict) -> str:
    """Одна человеческая строка про НАСТРОЙКУ САМОГО КИТА — отдельно от продукта, продуктовым языком.

    Честно называет, закончена ли настройка (вердикт фундамента), НО без жаргона (контур/источник
    истины/пути файлов/Storybook/workflow) — детали уходят в технические, по запросу. Ключевые слова
    «настройку самого кита» и «не про продукт» разводят её с продуктовой частью, как в `advise`."""
    if verdict.get("verdict") == "valid":
        return ("отдельно — про настройку самого кита (обновление и базовая настройка): она в "
                "порядке; это не про продукт, детали покажу по запросу")
    return ("отдельно — про настройку самого кита (обновление и базовая настройка): часть ещё не "
            "закончена; это не про продукт, детали покажу по запросу")


def _kit_setup_technical(briefing: dict) -> dict:
    """Все подробности про настройку кита (обновление, фундамент, Storybook) — в технические детали.

    Ничего не теряем: жаргон (версии/пути/контуры/Storybook/workflow) живёт ТОЛЬКО здесь, доступен
    по запросу и на technical/debug, а из человеческого текста убран."""
    wn, verdict, sb = briefing["whats_new"], briefing["verdict"], briefing["storybook"]
    recs = briefing["recommendations"]
    blocking = verdict.get("blocking") or []
    std = wn.get("standard") or {}
    return {
        "— это про НАСТРОЙКУ КИТА, не про продукт —": "обновление, фундамент, storybook",
        "что нового (подробно)": _whats_new_line(wn),
        "версия": f"{wn.get('from_version') or '—'} → {wn.get('to_version') or '—'}",
        "что нового": " | ".join(wn.get("changelog_slice") or []) or "—",
        "изменённых файлов": wn.get("changed_files", "—"),
        "изменения": ", ".join(p for p in (wn.get("change_paths") or []) if p) or "—",
        "отчёт обновления": wn.get("summary_text") or "—",
        "стандарт": f"установлен {std.get('installed')} / доступен {std.get('available')}"
                    + (" (отстал)" if std.get("behind") else ""),
        "вердикт фундамента": verdict.get("verdict"),
        "блокеры фундамента": "; ".join(blocking) or "—",
        "storybook (подробно)": _storybook_line(sb),
        "storybook": sb.get("storybook_maturity"),
        "рекомендации по фундаменту": " | ".join(f"{r['what']} — {r['why']}" for r in recs) or "—",
    }


def to_message(briefing: dict):
    """FoundationProposal -> UserMessage через РЕАЛЬНЫЙ presenter (аудитория product по умолчанию).

    #958 (`kit_recommends_what_the_product_needs`, сестра `advise`): вывод ВЕДЁТ с того, что нужно
    ПРОДУКТУ (`product_advice`, впрыснут в брифинг), а «настройку/фундамент самого кита» подаёт
    ОТДЕЛЬНО, продуктовым языком и без жаргона (детали — в технических, по запросу). Инвариант
    честности `product_advice` не трогаем: нет продуктового сигнала -> так и говорим, потребность
    не выдумываем.

    Четыре вопроса контракта в порядке: что произошло (что нужно продукту) → почему важно (причина
    главной потребности) → нужно ли что-то от меня (главная потребность) → что дальше (остальное по
    продукту + отдельная строка про настройку кита).
    """
    from ai_ops_kit.ui import presenter
    verdict = briefing["verdict"]
    pa = briefing.get("product_advice") or {}
    precs = list(pa.get("recommendations") or [])
    enough = pa.get("enough_product_data")

    technical: dict[str, object] = {"продуктовых рекомендаций": len(precs)}
    technical.update({f"продукт · {p.get('kind')} {i + 1}":
                      f"{p.get('need')} — {p.get('why')} (источник: {p.get('source')})"
                      for i, p in enumerate(precs)})
    if pa.get("note"):
        technical["продукт · примечание"] = pa["note"]
    technical.update(_kit_setup_technical(briefing))
    ff = briefing.get("foundation_freshness") or {}
    technical.update(_foundation_freshness_technical(ff))

    kit_line = _kit_setup_line(verdict)
    # Свежесть фундамента — продуктовый совет (пересмотреть Vision), НЕ настройка кита: строку даём
    # ТОЛЬКО когда есть измеримый кандидат, иначе молчим (честность превыше полноты).
    fresh_line = _foundation_freshness_line(ff)

    # ВЕДЁМ С ПРОДУКТА. Есть продуктовая потребность -> она в заголовке, сводке, «почему» и решении;
    # настройка кита — отдельной строкой в «Дальше».
    if precs:
        lead = precs[0]
        needs = "; ".join(p.get("need", "") for p in precs)
        decision = {"question": "что предлагаю взять по продукту в первую очередь",
                    "recommendation": f"{lead.get('need')} — потому что {lead.get('why')}"}
        next_steps = []
        if fresh_line:
            next_steps.append(fresh_line)
        if len(precs) > 1:
            next_steps.append("остальное по продукту покажу списком")
        next_steps.append(kit_line)
        return presenter.message(
            status="ok", headline="Что нужно продукту",
            summary=f"Вот что сейчас важно для твоего продукта: {needs}.",
            why_it_matters=lead.get("why"),
            decision=decision, next_steps=next_steps, technical=technical)

    # Продуктовых рекомендаций нет — честно различаем «данных не хватает» и «данные есть, срочного нет».
    if not enough:
        return presenter.message(
            status="degraded", headline="Пока не могу советовать по продукту",
            summary="Пока не хватает продуктовых данных, чтобы советовать по продукту.",
            why_it_matters="Придумывать продуктовую потребность я не буду — это была бы выдумка, "
                           "а не совет.",
            next_steps=([fresh_line] if fresh_line else []) + [kit_line], technical=technical)

    return presenter.message(
        status="ok", headline="По продукту сейчас советовать нечего",
        summary="Продуктовые данные есть, но срочного по продукту сейчас нет.",
        why_it_matters=pa.get("note") or "срочной работы по продукту сейчас нет",
        next_steps=([fresh_line] if fresh_line else []) + [kit_line], technical=technical)


def run_intent(task, child_root, signals, a):
    """Обработчик интента `propose` (регистрируется в `ai_ops_cli`). Тонкий: собирает брифинг и
    выводит его через presenter. Здоровье/риски меряет intelligence (выше planning), поэтому их
    считает CLI и ВПРЫСКИВАЕТ вниз — как в `contract`. Ничего не пишет.

    Живёт в этом модуле (а не в `ai_ops_cli_product`), чтобы держать монолит команд под потолком
    module-size: оркестратор и его вход — одна когезивная единица.
    """
    from ai_ops_kit.cli.ai_ops_cli_product import _product_health_report, _product_risks
    from ai_ops_kit.intelligence import product_advice
    from ai_ops_kit.planning import artifact_registry as _AR
    from ai_ops_kit.ui import presenter
    js = a.json
    health = _product_health_report(child_root)
    risks = _product_risks(child_root)
    # #958: propose ВЕДЁТ с «что нужно продукту» — тот же тонкий слой, что у `advise`. Считаем его
    # здесь (в обработчике интента) и ВПРЫСКИВАЕМ вниз, как health/risks: intelligence выше planning,
    # оркестратор его не импортирует. Слой честен сам: нет сигнала -> пусто, ничего не выдумывает.
    advice = product_advice.recommend(str(child_root))
    try:
        briefing = build_briefing(child_root, health=health, risks=risks,
                                  budget_left=getattr(a, "budget", None),
                                  product_advice=advice)
    except _AR.RegistryCorrupt as e:
        print(f"ОШИБКА: реестр артефактов недостоверен: {e}")
        return 1
    if js:
        print(json.dumps(briefing, ensure_ascii=False, indent=2, default=str))
        return 0
    # ЕДИНЫЙ ПУТЬ НАРУЖУ — через presenter: смысл на product, тех.детали на technical/debug.
    print(presenter.render(to_message(briefing),
                           audience=presenter.audience_from_config(child_root)))
    # Код возврата — ГОТОВНОСТЬ фундамента: not_ready -> non-zero, чтобы «предложение» не выглядело
    # успехом, когда обязательные части не закрыты (как `contract`/`next`).
    return 0 if briefing["verdict"]["verdict"] == "valid" else 1
