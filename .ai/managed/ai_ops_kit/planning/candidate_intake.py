#!/usr/bin/env python3
"""candidate_intake.py — приёмка задач-кандидатов пачкой: ЕДИНСТВЕННОЕ место, что пишет plan.yaml.

ПОВОД (эпик auto-slice-candidates). Кит НАРЕЗАЕТ кандидатов (непокрытые направления роадмапа —
`intelligence.roadmap_candidates`; наблюдения дочек — `intelligence.child_findings`), но
предохранитель владельца требует: кит сам НЕ дописывает активную работу в план. Здесь — обратная
половина: явная приёмка. Владелец выбирает кандидатов, и ТОЛЬКО тогда, ТОЛЬКО с `apply=True`,
выбранные становятся work items в `planning/plan.yaml`.

ПОЧЕМУ КАНДИДАТЫ ПРИХОДЯТ СПИСКОМ, А НЕ СОБИРАЮТСЯ ЗДЕСЬ. Union источников (findings + roadmap)
живёт в `intelligence`, а слой `planning` (ниже `intelligence`) импортировать его ВВЕРХ не вправе
(инвариант слоёв, `packages/layering.yaml`). Поэтому `accept_candidates` принимает уже собранный
список кандидатов, а собирает его вызыватель из слоя `entrypoints` (CLI), которому разрешены оба
источника. Так предохранитель и границы слоёв соблюдены одновременно.

ЧЕСТНЫЕ ГРАНИЦЫ:
  * БЕЗ `apply=True` не пишется НИЧЕГО — сухой прогон возвращает, что было бы добавлено.
  * ИДЕМПОТЕНТНО: кандидат, чья целевая работа уже есть в плане, пропускается (`skipped_existing`).
    Повторная приёмка того же кандидата дубля не создаёт.
  * plan.yaml НЕ round-trip'ится через `yaml.safe_dump`: файл насыщен комментариями (конституция
    их ценит), а ruamel в ките нет. Новые записи ДОПИСЫВАЮТСЯ ТЕКСТОМ в конец блока `work:` —
    каждый существующий байт и комментарий сохраняется.
  * Роль/тип кандидата (DRAFT — `owner_role: product-manager`, `type: improvement`) переводятся в
    словарь модели (`delivery_plan.validate` их сверяет): агент→роль, тип→ближайший валидный.
    Итоговый work item обязан проходить `delivery_plan.validate`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from ai_ops_kit.planning import delivery_plan as _plan
from ai_ops_kit.planning import contours as _contours


# Перевод DRAFT-типа кандидата (`improvement`/`investigation` — метки проекций, НЕ словарь плана) в
# валидный `work_types` модели. Если тип кандидата уже валиден — оставляем как есть.
_CAND_TYPE_MAP = {"improvement": "product", "investigation": "engineering"}
_DEFAULT_TYPE = "product"
_DEFAULT_ROLE = "product"


def _slug(candidate_id: str) -> str:
    """id кандидата → чистый slug работы: снимаем префиксы `cand-dir-`/`cand-`, нижний регистр.

    `cand-dir-<goal>` и `cand-<obs>` — технические id проекций. Работе плана нужен slug нижнего
    регистра (правило `delivery_plan._engine_id_ok`), поэтому префикс проекции срезаем."""
    s = str(candidate_id or "").strip().lower()
    for prefix in ("cand-dir-", "cand-"):
        if s.startswith(prefix):
            return s[len(prefix):]
    return s


def _agent_to_role(model: dict) -> dict:
    """{agent: role} из модели: кандидат называет АГЕНТА (`product-manager`), план — РОЛЬ."""
    out: dict = {}
    for role, spec in (model.get("roles") or {}).items():
        for agent in (spec or {}).get("agents") or []:
            out.setdefault(str(agent), role)
    return out


def _role_for(cand_role, model: dict) -> str:
    """Роль кандидата → валидная роль модели. Уже роль — оставляем; агент — резолвим в его роль;
    иначе — безопасный дефолт `product` (или первая роль модели)."""
    roles = set((model.get("roles") or {}).keys())
    r = str(cand_role or "").strip()
    if r in roles:
        return r
    mapped = _agent_to_role(model).get(r)
    if mapped in roles:
        return mapped
    if _DEFAULT_ROLE in roles:
        return _DEFAULT_ROLE
    return sorted(roles)[0] if roles else _DEFAULT_ROLE


def _type_for(cand_type, model: dict) -> str:
    """Тип кандидата → валидный `work_types` модели. Уже валиден — оставляем; DRAFT-метку переводим;
    иначе — безопасный дефолт `product` (или первый тип модели)."""
    types = set((model.get("work_types") or {}).keys())
    t = str(cand_type or "").strip()
    if t in types:
        return t
    mapped = _CAND_TYPE_MAP.get(t)
    if mapped in types:
        return mapped
    if _DEFAULT_TYPE in types:
        return _DEFAULT_TYPE
    return sorted(types)[0] if types else _DEFAULT_TYPE


def _text(v) -> str:
    return str(v or "").strip()


def _plan_item_from_candidate(cand: dict, model: dict, goal: str | None = None) -> dict:
    """Кандидат → work item плана. Роль/тип переводятся в словарь модели; `goal` — УЖЕ разрешённое
    направление (source_goal кандидата или явный --goal владельца), проставляется только если задано.
    `source` метит происхождение для обратной связи."""
    slug = _slug(cand.get("id"))
    item = {
        "id": slug,
        "title": _text(cand.get("title")) or slug,
        "type": _type_for(cand.get("type"), model),
        "status": "todo",
        "owner_role": _role_for(cand.get("owner_role"), model),
        "source": _text(cand.get("source")) or "candidate",
        "rationale": _text(cand.get("rationale")),
    }
    if goal:
        item["goal"] = goal
    return item


def accept_candidates(child_root, candidate_ids, candidates, *, goal: str | None = None,
                      apply: bool = False) -> dict:
    """Принять выбранных кандидатов пачкой. -> {"to_add", "skipped_existing", "skipped_no_goal",
    "skipped_collision", "applied", ["error"]}.

    `candidate_ids` — id кандидатов к приёмке (в порядке владельца). `candidates` — уже собранный
    список кандидатов (union источников делает вызыватель, см. докстринг модуля). `goal` — явное
    направление владельца (`--goal <id>`) для кандидатов без своего `source_goal`. `apply=True` —
    ЕДИНСТВЕННЫЙ режим, что пишет plan.yaml.

    ПРИЁМКА НИКОГДА НЕ ПИШЕТ НЕВАЛИДНЫЙ ПЛАН:
      * НАПРАВЛЕНИЕ. Кандидат без направления в МНОГОЦЕЛЕВОМ плане дал бы work item без `goal` — а это
        ошибка delivery_plan.validate. Такой кандидат НЕ пишется молча: он уходит в `skipped_no_goal`
        с внятной причиной («укажи --goal <id>»). В одноцелевом плане goal можно опустить — валидатор
        выводит единственную цель сам. Явный `--goal`, не существующий в плане, тоже отклоняется.
      * ИДЕМПОТЕНТНОСТЬ. Кандидат, чей целевой slug уже есть в `plan["work"]`, уходит в
        `skipped_existing` — повтор дубля не создаёт.
      * КОЛЛИЗИЯ ИСТОЧНИКОВ. `cand-dir-foo` и `cand-foo` дают ОДИН slug `foo`. Это не «уже в плане»:
        второй уходит в `skipped_collision` (с кем схлопнулся), чтобы владелец видел настоящую причину.

    Пустого/битого плана здесь не бывает молча: дописывать не во что — возвращаем `error`, ничего не
    трогаем. Запись атомарна и застрахована (см. `_apply_to_plan_file`): не разобралось — не пишем.
    """
    root = Path(child_root)
    result = {"to_add": [], "skipped_existing": [], "skipped_no_goal": [],
              "skipped_collision": [], "applied": False}
    try:
        plan = _plan.load(root)
    except _plan.PlanCorrupt as e:
        return {**result, "error": f"план не прочитан ({e}) — приёмка небезопасна"}
    if plan is None:
        return {**result, "error": "плана нет — дописывать кандидатов не во что"}

    model = _contours.load_model()
    existing = {w.get("id") for w in _plan.items(plan) if w.get("id")}
    goal_ids = {g["id"] for g in _plan.goals(plan)}
    multi_goal = len(goal_ids) > 1
    explicit_goal = _text(goal)
    by_id = {c.get("id"): c for c in (candidates or []) if isinstance(c, dict) and c.get("id")}

    seen: dict = {}                        # slug -> id кандидата, уже принятого в этой пачке
    for cid in (candidate_ids or []):
        cand = by_id.get(cid)
        if cand is None:
            continue                       # id, которого нет среди кандидатов — молча мимо
        slug = _slug(cid)
        if slug in existing:
            result["skipped_existing"].append(slug)
            continue
        if slug in seen:
            result["skipped_collision"].append(
                {"id": cid, "slug": slug, "clashes_with": seen[slug]})
            continue
        wgoal = _text(cand.get("source_goal")) or explicit_goal
        if wgoal and wgoal not in goal_ids:
            result["skipped_no_goal"].append(
                {"id": cid, "reason": f"направление «{wgoal}» не найдено в плане — "
                                      f"укажи существующую цель: --goal <id>"})
            continue
        if not wgoal and multi_goal:
            result["skipped_no_goal"].append(
                {"id": cid, "reason": "у кандидата нет направления, а целей в плане несколько — "
                                      "укажи, к какой относится: --goal <id>"})
            continue
        seen[slug] = cid
        result["to_add"].append(_plan_item_from_candidate(cand, model, goal=wgoal or None))

    if apply and result["to_add"]:
        err = _apply_to_plan_file(root, result["to_add"])
        if err:
            result["error"] = err          # applied остаётся False — на диск ничего не легло
        else:
            result["applied"] = True
    return result


# ── Запись в plan.yaml ТЕКСТОМ (комментарии сохраняются) ─────────────────────────────────────────

def _yaml_scalar(s: str) -> str:
    """Строка → безопасный YAML-скаляр. JSON-строка — валидный YAML flow-скаляр в двойных кавычках,
    поэтому `json.dumps(..., ensure_ascii=False)` корректно экранирует кавычки/переводы строк и не
    зависит от содержимого (в отличие от ручного `>-`, где легко ошибиться с отступом)."""
    return json.dumps(str(s), ensure_ascii=False)


def _render_items_yaml(items: list) -> str:
    """work items → YAML-текст для дописывания под `work:` (отступ списка — 2 пробела)."""
    lines = ["  # ── Принято приёмкой кандидатов (candidate_intake) ─────────────────────────────"]
    for it in items:
        lines.append(f"  - id: {it['id']}")
        lines.append(f"    title: {_yaml_scalar(it['title'])}")
        lines.append(f"    type: {it['type']}")
        if it.get("goal"):
            lines.append(f"    goal: {it['goal']}")
        lines.append(f"    status: {it['status']}")
        lines.append(f"    owner_role: {it['owner_role']}")
        lines.append(f"    source: {_yaml_scalar(it['source'])}")
        if it.get("rationale"):
            lines.append(f"    rationale: {_yaml_scalar(it['rationale'])}")
    return "\n".join(lines) + "\n"


def _append_work_items_text(text: str, items_yaml: str) -> str:
    """Дописать `items_yaml` в конец блока `work:`, сохранив каждый существующий байт/комментарий.

    Находим верхнеуровневый ключ `work:` и конец его блока — следующий верхнеуровневый ключ (строка,
    начинающаяся не с пробела и не с `#`), либо конец файла. Вставляем новые записи перед ним.

    ГАРАНТИЯ ФОРМЫ. Дописывание рассчитано на БЛОК-СПИСОК с отступом 2 пробела (`  - id: …`) — как в
    plan.yaml кита. Если блок оформлен иначе (flow `work: []`/значение в той же строке; mapping или
    список с другим отступом), дописывание вслепую дало бы битый YAML. Такой случай не угадываем —
    поднимаем ValueError (вызыватель вернёт error и НЕ запишет).
    """
    lines = text.splitlines(keepends=True)
    work_idx = next((i for i, ln in enumerate(lines) if re.match(r"^work\s*:", ln)), None)
    if work_idx is None:
        raise ValueError("в plan.yaml нет верхнеуровневого блока work: — дописывать некуда")
    # Flow/скаляр: непустое значение в той же строке, что и `work:` — не блок-список.
    after = lines[work_idx].split(":", 1)[1] if ":" in lines[work_idx] else ""
    if after.split("#", 1)[0].strip():
        raise ValueError("блок work: не блок-список (значение в той же строке — flow-стиль) — "
                         "дописывать вслепую нельзя")
    end = len(lines)
    for i in range(work_idx + 1, len(lines)):
        ln = lines[i]
        stripped = ln.lstrip()
        if ln and not ln[0].isspace() and stripped and not stripped.startswith("#"):
            end = i
            break
    # Первая содержательная строка блока (не пустая, не комментарий) обязана быть item'ом `  - `
    # с отступом 2 пробела. Пустой блок (нет ни одного item) — законен: мы начинаем список.
    first_content = next((lines[i] for i in range(work_idx + 1, end)
                          if lines[i].strip() and not lines[i].lstrip().startswith("#")), None)
    if first_content is not None and not re.match(r"^  -\s", first_content):
        raise ValueError("блок work: не 2-space блок-список (item без `- ` или иной отступ) — "
                         "дописывать вслепую нельзя")
    head = "".join(lines[:end])
    if head and not head.endswith("\n"):
        head += "\n"
    tail = "".join(lines[end:])
    return head + items_yaml + tail


def _apply_to_plan_file(root: Path, items: list) -> str | None:
    """Дописать work items в plan.yaml ТЕКСТОМ. -> строка ошибки или None (успех). НЕ round-trip'ит
    YAML — комментарии остаются.

    СТРАХОВКА ПЕРЕД ЗАПИСЬЮ (F4). Итоговый текст СНАЧАЛА собирается и разбирается в памяти
    (`yaml.safe_load`), и только если он парсится И все новые id в нём присутствуют — пишется на диск.
    Не собралось/не распарсилось/id не появились → возвращаем ошибку и НИЧЕГО не пишем: половинчатый
    или битый план хуже ненаписанного.
    """
    p = _plan.plan_path(root)
    text = p.read_text(encoding="utf-8")
    try:
        new_text = _append_work_items_text(text, _render_items_yaml(items))
    except ValueError as e:
        return str(e)
    try:
        parsed = yaml.safe_load(new_text)
    except yaml.YAMLError as e:
        return f"итоговый план не разбирается ({e}) — не записываю"
    if not isinstance(parsed, dict):
        return "итоговый план после дописывания — не mapping — не записываю"
    got = {w.get("id") for w in _plan.items(parsed) if w.get("id")}
    missing = [it["id"] for it in items if it["id"] not in got]
    if missing:
        return f"новые работы не появились в разобранном плане ({missing}) — не записываю"
    p.write_text(new_text, encoding="utf-8")
    return None
