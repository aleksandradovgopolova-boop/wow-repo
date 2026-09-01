"""Learning from human overrides — прошлые override'ы меняют будущие рекомендации.

Фаза 5 (капстоун), работа `learning-from-human-overrides`. Канал override'ов построен в
`human_override.py` (override_signals); этот модуль ПОТРЕБЛЯЕТ сигнал и меняет рекомендации.

Два применения:
  * `adjust_priority` — если человек переопределил приоритет работы, будущий расчёт приоритета
    для похожей работы учитывает направление override (вверх/вниз).
  * `adjust_risk` — если человек отклонил риск, будущая оценка похожих рисков понижается.

Метод: для каждой рекомендации ищем прошлые override'ы по target-префиксу. Если находим —
сдвигаем score в сторону решения человека. Сила сдвига пропорциональна числу override'ов
(больше override'ов = сильнее сигнал), но ограничена (не может полностью перевернуть оценку).

Не меняет рекомендацию ВСЛЕПУЮ: если override'ов нет — score не изменён (честный unknown).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_ops_kit.governance import human_override

# Максимальный сдвиг от одного override (0.0–1.0 шкала)
SINGLE_OVERRIDE_SHIFT = 0.15
# Максимальный суммарный сдвиг (независимо от числа override'ов)
MAX_SHIFT = 0.4


def _count_overrides_for_target(signals: list[dict], target_prefix: str) -> tuple[int, list[str]]:
    """Считает override'ы по префиксу target. Возвращает (count, направления).

    Направление: 'up' если human_decision повышает (priority-up, accept),
    'down' если понижает (priority-down, reject, skip).
    """
    directions: list[str] = []
    for s in signals:
        target = s.get("target", "")
        if not target.startswith(target_prefix):
            continue
        decision = (s.get("human_decision") or "").lower()
        if any(w in decision for w in ("up", "higher", "accept", "approve", "keep")):
            directions.append("up")
        elif any(w in decision for w in ("down", "lower", "reject", "skip", "drop")):
            directions.append("down")
    return len(directions), directions


def adjust_priority(
    root: Path,
    *,
    work_id: str,
    base_score: float,
    registry_rel: str = "decisions/registry.yaml",
) -> dict[str, Any]:
    """Корректирует приоритет работы на основе прошлых override'ов.

    Возвращает:
      adjusted_score: float (0.0–1.0)
      base_score: float (исходный)
      override_count: int
      override_shift: float (фактический сдвиг)
      explanation: str (почему скорректировано или "без override'ов")
    """
    signals = human_override.override_signals(root, registry_rel)
    count, directions = _count_overrides_for_target(signals, f"priority:{work_id}")

    if count == 0:
        # Ищем по более широкому префиксу (priority: без конкретного id)
        count, directions = _count_overrides_for_target(signals, "priority:")

    if count == 0:
        return {
            "adjusted_score": base_score,
            "base_score": base_score,
            "override_count": 0,
            "override_shift": 0.0,
            "explanation": "без override'ов — оценка не изменена",
        }

    # Вычисляем чистый сдвиг
    ups = directions.count("up")
    downs = directions.count("down")
    net = ups - downs  # положительный = человек повышал, отрицательный = понижал

    raw_shift = net * SINGLE_OVERRIDE_SHIFT
    clamped = max(-MAX_SHIFT, min(MAX_SHIFT, raw_shift))
    adjusted = max(0.0, min(1.0, base_score + clamped))

    direction_word = "повышен" if net > 0 else "понижен"
    return {
        "adjusted_score": round(adjusted, 3),
        "base_score": base_score,
        "override_count": count,
        "override_shift": round(clamped, 3),
        "explanation": (
            f"приоритет {direction_word} на основе {count} override'ов "
            f"(up={ups}, down={downs}, сдвиг={clamped:+.3f})"
        ),
    }


def adjust_risk(
    root: Path,
    *,
    risk_category: str,
    base_severity: float,
    registry_rel: str = "decisions/registry.yaml",
) -> dict[str, Any]:
    """Корректирует оценку риска на основе прошлых override'ов.

    Если человек отклонял риск этой категории — severity понижается.
    """
    signals = human_override.override_signals(root, registry_rel)
    count, directions = _count_overrides_for_target(signals, f"risk:{risk_category}")

    if count == 0:
        return {
            "adjusted_severity": base_severity,
            "base_severity": base_severity,
            "override_count": 0,
            "override_shift": 0.0,
            "explanation": "без override'ов — оценка не изменена",
        }

    # Для рисков: 'down' = человек отклонил риск (severity ниже), 'up' = подтвердил
    downs = directions.count("down")
    ups = directions.count("up")
    net = downs - ups  # положительный = человек снижал риск

    raw_shift = net * SINGLE_OVERRIDE_SHIFT
    clamped = max(-MAX_SHIFT, min(MAX_SHIFT, raw_shift))
    adjusted = max(0.0, min(1.0, base_severity - clamped))  # снижение severity

    return {
        "adjusted_severity": round(adjusted, 3),
        "base_severity": base_severity,
        "override_count": count,
        "override_shift": round(clamped, 3),
        "explanation": (
            f"риск скорректирован на основе {count} override'ов "
            f"(reject={downs}, confirm={ups})"
        ),
    }
