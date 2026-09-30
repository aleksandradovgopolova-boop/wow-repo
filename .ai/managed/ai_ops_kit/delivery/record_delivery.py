#!/usr/bin/env python3
"""Пост-фактум расписка о поставке для ВРУЧНУЮ влитого PR (v4.5.x).

Закрывает разрыв «человек влил PR сам»: у фичи, чей PR открыл и влил человек (а не кит), нет
DeliveryReceipt — и released-проверка честно жалуется «докажи поставку». Здесь способ ЗАПИСАТЬ
честную расписку для такого PR, но ТОЛЬКО сверив слияние с GitHub. Никакого рубер-штампа:
`sha_verified: true` пишется ИСКЛЮЧИТЕЛЬНО когда GitHub авторитетно сообщает, что PR влит
(`merged_at` выставлен И присутствует `merge_commit_sha`).

PR ищется ПО НОМЕРУ (`GET /repos/{owner}/{name}/pulls/{number}`), а НЕ по ветке: у вручную влитого
PR ветка часто удалена или пришла из форка, и branch-lookup (`pr_open.reconcile_delivery`) её не
найдёт — он доказывает идентичность tip ВЕТКИ, а не «влито в дефолтную ветку». Номер PR —
единственный пользовательский ввод; все ФАКТЫ (merged, sha, base, state) берутся из ответа GitHub,
не из флагов.

Слой delivery: импортирует shared/gates/stdlib и соседний pr_open (переиспользуем разбор owner/repo,
резолв канонического слага, REST-хелпер и проверки). engine НЕ импортируем (ребро delivery->engine
снято в K3): путь приёмника (features/<id>/delivery-outbox) считаем здесь же — тот же, что у
канонического reader и engine._outbox_dir.

Сетевой вызов — живой шаг (нужен токен + доступ к GitHub); механика (разбор ввода, ветвление по
факту merged, конструкция расписки) детерминирована и тестируется offline с замоканным REST-слоем.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ai_ops_kit.gates import concurrency_preflight as _cp   # noqa: E402
from ai_ops_kit.delivery import pr_open as _pr              # noqa: E402
from ai_ops_kit.shared import lifecycle_store as _ls        # noqa: E402


def _outbox_dir(root, feature_id):
    """Приёмник расписок фичи — ТОТ ЖЕ путь, что engine._outbox_dir и канонический reader:
    <root>/features/<feature_id>/delivery-outbox/. delivery не может импортировать engine (K3),
    поэтому путь воспроизводится здесь; расхождение поймал бы released-check, читающий его же."""
    return Path(root) / "features" / str(feature_id) / "delivery-outbox"


def _delivery_id(repository, feature_id, pr_number):
    """Детерминированный id расписки из авторитетных данных (repo + фича + номер PR), не из
    случайности: повторный `record` того же PR перезаписывает ту же расписку, а не плодит дубли."""
    return hashlib.sha256(
        f"{repository}:{feature_id}:pr-{pr_number}".encode("utf-8")).hexdigest()[:16]


def parse_pr_number(raw):
    """Номер PR из пользовательского ввода: целое, строка-число или полный URL PR (.../pull/123).
    -> положительный int | None. Единственный пользовательский ввод, и он уходит в путь GitHub API,
    поэтому валидируем строго (fail-closed): не число / не положительное -> None (не подставляем)."""
    if raw is None:
        return None
    s = str(raw).strip()
    m = re.search(r"/pull/(\d+)", s)
    if m:
        s = m.group(1)
    if not s.isdigit():
        return None
    n = int(s)
    return n if n > 0 else None


def record_delivery_for_pr(root, feature_id, pr_number):
    """Свериться с GitHub ПО НОМЕРУ PR и записать честную DeliveryReceipt в приёмник фичи.

    Возврат (dict):
      * {"status": "unavailable", ...}        — спросить GitHub нечем/не ответил: НИЧЕГО не пишем;
      * {"status": "recorded", ...}           — PR влит по данным GitHub: расписка с sha_verified=True;
      * {"status": "not-delivered"|"mismatch"}— PR есть, но НЕ влит: расписка БЕЗ sha_verified (honest);
      * {"status": "receipt-write-failed",...}— durable_write не подтвердил запись.

    ИНВАРИАНТ ЧЕСТНОСТИ (единственный, который нельзя нарушить): `sha_verified: True` записывается
    ТОЛЬКО в ветке `merged and merge_sha`, где `merged = bool(pr["merged_at"])` и
    `merge_sha = pr["merge_commit_sha"]` пришли из ответа GitHub. Ни отсутствие токена, ни открытый
    PR, ни закрытый-невлитый, ни ошибка API не дают этой ветки."""
    root = Path(root)
    pr_number = parse_pr_number(pr_number)
    if pr_number is None:
        return {"status": "unavailable", "note": "не распознан номер PR"}
    token = _cp._github_token()
    if not token:
        # fail-closed (как engine/pr_open): спросить GitHub нечем -> расписку без подтверждения не пишем.
        return {"status": "unavailable",
                "note": "нет GITHUB_TOKEN/GH_TOKEN — сверить слияние с GitHub нечем"}
    rc, url, _ = _pr._git(root, "remote", "get-url", "origin")
    owner_repo = _cp._parse_owner_repo(url) if rc == 0 else None
    if not owner_repo:
        return {"status": "unavailable", "note": "не удалось определить owner/repo из origin"}
    owner, name = owner_repo
    # Тот же резолв канонического слага, что у pr_open: перенесённое (301) репо ищем по актуальному
    # owner/repo, иначе спрашивали бы PR не в том репозитории. Резолв не удался -> исходный слаг.
    canonical = _pr._canonical_owner_repo(owner, name, token)
    if canonical:
        owner, name = canonical
    repository = f"{owner}/{name}"
    pr, err = _pr._gh_request(
        f"{_pr._api_base()}/repos/{owner}/{name}/pulls/{pr_number}", token)
    if err or not isinstance(pr, dict) or not pr.get("number"):
        return {"status": "unavailable", "repository": repository,
                "note": f"GitHub не ответил про PR #{pr_number} ({err or 'PR не найден'})"}

    # АВТОРИТЕТНЫЕ ФАКТЫ из ответа GitHub (не из пользовательского ввода).
    merged = bool(pr.get("merged_at"))
    merge_sha = pr.get("merge_commit_sha")
    head_sha = (pr.get("head") or {}).get("sha")
    head_ref = (pr.get("head") or {}).get("ref")
    base_ref = (pr.get("base") or {}).get("ref")
    state = pr.get("state")
    html_url = pr.get("html_url")

    did = _delivery_id(repository, feature_id, pr_number)
    rp = _outbox_dir(root, feature_id) / f"{did}.receipt.yaml"
    base = {"schema_version": 1, "kind": "DeliveryReceipt", "delivery_id": did,
            "workitem_id": str(feature_id), "repository": repository, "branch": head_ref,
            "base_ref": base_ref, "pr_url": html_url, "pr_number": pr.get("number"),
            "pr_state": state, "reconciled": True,
            # ПРОВЕНАНС: расписка записана ПОСТ-ФАКТУМ командой `delivery record`, а не кит-прогоном.
            # Честно помечаем, чтобы её нельзя было принять за кит-ведённую доставку.
            "recorded_post_hoc": True, "recorded_via": "delivery record",
            "recorded_note": "PR влит человеком; расписка записана постфактум после сверки с GitHub"}

    if merged and merge_sha:
        # ЕДИНСТВЕННАЯ ветка, где sha_verified=True: GitHub подтвердил merged_at И merge_commit_sha.
        chk = _pr._checks_for_sha(owner, name, merge_sha, token)
        receipt = {**base, "status": "reconciled", "commit_sha": merge_sha,
                   "remote_sha": merge_sha, "merged": True, "sha_verified": True,
                   "checks_status": chk.get("status"), "checks_total": chk.get("total"),
                   "checks_failed": chk.get("failed"),
                   "checks_verified": _pr.checks_verified(chk)}
        w = _ls.durable_write(rp, receipt, require_keys=("kind", "delivery_id", "status"))
        if not w.get("ok"):
            return {"status": "receipt-write-failed", "note": w.get("error"), "path": str(rp)}
        return {"status": "recorded", "delivery_id": did, "repository": repository,
                "commit_sha": merge_sha, "pr_number": pr.get("number"), "pr_url": html_url,
                "sha_verified": True, "checks_verified": _pr.checks_verified(chk), "path": str(rp)}

    # PR есть, но НЕ влит: open -> not-delivered, closed-без-merge -> mismatch. sha_verified НЕ true —
    # расписка делает честное состояние видимым, но released-check по-прежнему справедливо жалуется.
    status = "not-delivered" if state == "open" else "mismatch"
    receipt = {**base, "status": status, "commit_sha": head_sha, "remote_sha": head_sha,
               "merged": False, "sha_verified": False}
    w = _ls.durable_write(rp, receipt, require_keys=("kind", "delivery_id", "status"))
    if not w.get("ok"):
        return {"status": "receipt-write-failed", "note": w.get("error"), "path": str(rp)}
    return {"status": status, "delivery_id": did, "repository": repository, "merged": False,
            "sha_verified": False, "pr_number": pr.get("number"), "pr_url": html_url,
            "pr_state": state, "path": str(rp)}
