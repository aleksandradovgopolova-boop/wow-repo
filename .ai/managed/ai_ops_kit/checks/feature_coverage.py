"""Судья охвата фич дочки: сверка реестра фич с поверхностями, извлечёнными из кода (W3).

Кит выводит поверхности продукта из кода дочки (извлечение — W2) и сверяет их с реестром фич
(`registry/features.yaml` дочки по схеме `registry/feature-registry/feature-registry.schema.yaml`).
Незадокументированная фича — поверхность в коде без сопоставленной фичи в реестре — становится
обнаружимой находкой; а СИЛА находки честна по УВЕРЕННОСТИ вывода поверхности (confidence).

Эта логика — ЧИСТАЯ и ДЕТЕРМИНИРОВАННАЯ: stdlib, без сети и без модели. Живёт в пакете `checks`
(слой primitives), чтобы её звали ВНИЗ по слоям — процессный вход `validation/validate_feature_coverage.py`
(entrypoints) тянет её к себе, не наоборот. Тем же приёмом в ките развязана вся проверяющая логика
валидаторов (AGENTS.md, лента №5).

КОНТРАКТ ВХОДА (соблюдается точно):
  * registry — dict реестра фич: `{"features": [ {id,name,description,surfaces[],status,owner}, ... ]}`.
  * surfaces — СПИСОК записей поверхностей, извлечённых из кода дочки; каждая по схеме surface:
    `{kind, ref, confidence: verified|inferred, extractor}`. Судья НЕ извлекает их сам (это W2) —
    он принимает готовый список, поэтому не зависит от конкретного экстрактора.

КОНТРАКТ ВЫХОДА coverage report:
  `{covered: [...], orphans: [{surface, confidence}], ghosts: [...], coverage_pct_verified}`.
    * orphan  — поверхность в коде без сопоставленной фичи в реестре;
    * ghost   — фича в реестре без следа в коде (planned ИСКЛЮЧАЕТСЯ: у неё кода ещё нет законно);
    * covered — поверхность сопоставлена описанной фиче (по её полю `surfaces[]`).

КОНТРАКТ ВЫХОДА gate verdict:
  `{status: pass|warn|fail, blocking_reason?, evidence: [{file, lines}]}`.

СИЛА ПО УВЕРЕННОСТИ (честность = честность confidence):
  * verified-orphan → блокирующая проблема (fail): вывод доказан детерминированно, молчать нельзя;
  * inferred-orphan → advisory (warn): предположение эвристики только предупреждает;
  * ghost (не planned) → advisory (warn): фича без следа в коде — сигнал, не блок.

BOOTSTRAP / РАТЧЕТ: у пустого реестра (или на первом прогоне) ВСЕ поверхности — сироты, и блокировать
их разом значило бы утопить первую установку. Поэтому verdict принимает `baseline_verified_orphans` —
число заранее принятых («дедов») verified-сирот: fail наступает лишь когда verified-сирот СТАЛО
БОЛЬШЕ baseline (появилась НОВАЯ незадокументированная verified-поверхность). Ратчет ходит только
ВНИЗ: закрыли фичей — число убывает, вырасти незаметно не может. `bootstrap_baseline()` даёт стартовое
значение (все нынешние verified-сироты), которое дальше вправе лишь уменьшаться.
"""
from __future__ import annotations


# ─── Сопоставление поверхностей по ссылке на код ────────────────────────────────────────────────

def ref_key(ref: str) -> str:
    """Ключ сопоставления surface↔surface по ссылке на код.

    ref — "file:line" или "file:line|symbol" (схема surface). Номер строки ДРЕЙФУЕТ при правках, а
    символ — нет; поэтому при наличии символа ключ = "file|symbol" (устойчив к сдвигу строк), а без
    символа — точный "file:line" (иного якоря нет). Детерминированно и объяснимо.
    """
    if not isinstance(ref, str):
        return ""
    head = ref.split("|", 1)
    if len(head) == 2:
        file = head[0].rsplit(":", 1)[0]
        return f"{file}|{head[1]}"
    return ref


def ref_evidence(ref: str) -> dict:
    """Ссылка на код -> улика {file, lines} для gate verdict."""
    head = ref.split("|", 1)[0] if isinstance(ref, str) else ""
    file, _, line = head.rpartition(":")
    if not file:                       # ":"-разделителя не было — весь head это файл
        file = head
        line = ""
    return {"file": file, "lines": int(line) if line.isdigit() else line}


# ─── Отчёт об охвате ─────────────────────────────────────────────────────────────────────────────

def build_coverage_report(registry: dict, surfaces: list) -> dict:
    """Сверить реестр фич с извлечёнными поверхностями. -> coverage report по контракту.

    Сопоставление surface↔feature — по полю `surfaces[]` фичи (объявленные ссылки на код). Поверхность
    из кода ПОКРЫТА, если её ключ есть среди объявленных фичами; иначе — СИРОТА. Фича — ПРИЗРАК, если
    она не planned и НИ ОДНА её объявленная поверхность не встретилась в извлечённых (следа в коде нет).
    """
    features = (registry or {}).get("features") or []

    declared: dict[str, str] = {}          # ключ поверхности -> id фичи, его объявившей
    for feat in features:
        for surf in feat.get("surfaces") or []:
            key = ref_key(surf.get("ref"))
            if key:
                declared.setdefault(key, feat.get("id"))

    covered: list[dict] = []
    orphans: list[dict] = []
    matched_feature_ids: set[str] = set()
    verified_total = 0
    verified_covered = 0

    for surf in surfaces or []:
        conf = surf.get("confidence")
        key = ref_key(surf.get("ref"))
        is_verified = conf == "verified"
        if is_verified:
            verified_total += 1
        fid = declared.get(key)
        if fid is not None:
            covered.append({"surface": surf, "feature": fid})
            matched_feature_ids.add(fid)
            if is_verified:
                verified_covered += 1
        else:
            orphans.append({"surface": surf, "confidence": conf})

    ghosts: list[str] = []
    for feat in features:
        if feat.get("status") == "planned":
            continue                       # planned законно без кода — из ghost исключается
        if feat.get("id") not in matched_feature_ids:
            ghosts.append(feat.get("id"))

    coverage_pct_verified = (
        round(100.0 * verified_covered / verified_total, 1) if verified_total else 100.0
    )
    return {
        "covered": covered,
        "orphans": orphans,
        "ghosts": ghosts,
        "coverage_pct_verified": coverage_pct_verified,
    }


# ─── Вердикт гейта (сила по confidence + bootstrap-ратчет) ──────────────────────────────────────

def verified_orphans(report: dict) -> list[dict]:
    return [o for o in report.get("orphans") or [] if o.get("confidence") == "verified"]


def inferred_orphans(report: dict) -> list[dict]:
    return [o for o in report.get("orphans") or [] if o.get("confidence") != "verified"]


def bootstrap_baseline(report: dict) -> int:
    """Стартовое число дедов-сирот для первого прогона: все нынешние verified-сироты.

    Дальше это число вправе лишь УМЕНЬШАТЬСЯ (ратчет вниз): закрыл фичей — опусти baseline."""
    return len(verified_orphans(report))


def coverage_verdict(report: dict, baseline_verified_orphans: int = 0) -> dict:
    """coverage report -> gate verdict `{status, blocking_reason?, evidence:[{file,lines}]}`.

    fail наступает ТОЛЬКО когда verified-сирот стало больше baseline (появилась НОВАЯ
    незадокументированная verified-поверхность). inferred-сироты и ghost'ы — advisory (warn), не блок.
    """
    v = verified_orphans(report)
    inf = inferred_orphans(report)
    ghosts = report.get("ghosts") or []

    baseline = max(0, int(baseline_verified_orphans or 0))
    new_verified = max(0, len(v) - baseline)

    if new_verified > 0:
        evidence = [ref_evidence(o["surface"].get("ref")) for o in v]
        reason = (
            f"{new_verified} незадокументированн(ая/ых) verified-поверхност(ь/ей) в коде без фичи "
            f"в реестре (сверх baseline={baseline}). Заведите фичу или свяжите поверхность."
        )
        return {"status": "fail", "blocking_reason": reason, "evidence": evidence}

    if v or inf or ghosts:
        evidence = [ref_evidence(o["surface"].get("ref")) for o in (v + inf)]
        return {"status": "warn", "evidence": evidence}

    return {"status": "pass", "evidence": []}


def reconcile_baseline(current_verified_orphans: int, stored_baseline=None) -> dict:
    """Свести текущее число verified-сирот с ПЕРСИСТЕНТНЫМ baseline. Ратчет ходит ТОЛЬКО ВНИЗ.

    Чистая логика без ввода-вывода: файл baseline читает и пишет процессный вход (валидатор/CI),
    а решение — здесь, чтобы его можно было проверить вызовом. Три случая:

      * `stored_baseline is None` — ПЕРВЫЙ прогон: baseline СИДИРУЕТСЯ текущим числом, ничего не
        блокируем (иначе первая установка утонула бы в блокировках — тот же bootstrap, что и в
        `coverage_verdict`, но теперь ПЕРСИСТЕНТНЫЙ: seeded-значение переживёт прогон в файле дочки);
      * `current > stored` — verified-сирот СТАЛО БОЛЬШЕ принятого потолка: РОСТ, блокируем; baseline
        НЕ поднимаем молча (ратчет вверх запрещён) — его опустит только устранение сирот;
      * `current <= stored` — без роста или снижение: baseline опускаем до текущего (ратчет вниз),
        не блокируем. Закрыли фичей — число убыло и назад не поднимется незаметно.

    -> {"baseline": int, "seeded": bool, "blocked": bool, "regressed": int, "changed": bool}
       baseline — значение, которое надлежит ЗАПИСАТЬ (down-only); changed — отличается ли оно от
       stored (нужно ли переписать файл); regressed — на сколько превышен потолок при blocked.
    """
    current = max(0, int(current_verified_orphans or 0))
    if stored_baseline is None:
        return {"baseline": current, "seeded": True, "blocked": False,
                "regressed": 0, "changed": True}
    prev = max(0, int(stored_baseline))
    if current > prev:
        return {"baseline": prev, "seeded": False, "blocked": True,
                "regressed": current - prev, "changed": False}
    return {"baseline": current, "seeded": False, "blocked": False,
            "regressed": 0, "changed": current != prev}


def evaluate(registry: dict, surfaces: list, baseline_verified_orphans: int = 0) -> dict:
    """Свести отчёт и вердикт в один результат — удобная точка для процессного входа и тестов."""
    report = build_coverage_report(registry, surfaces)
    verdict = coverage_verdict(report, baseline_verified_orphans)
    return {"report": report, "verdict": verdict}
