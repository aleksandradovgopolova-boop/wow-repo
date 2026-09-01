"""RunContext — переписываемое состояние одного pipeline-прогона controller'а.

K6-глубина Волны 5: `run()` в `ai_ops_run.py` — последняя God-функция ядра, держащая
ратчет размеров. Её оставшиеся крупные блоки (resume-policy-restore, model-routing,
execute+fix-loop, delivery) НЕ выносятся простым `def helper(...)`: каждый переписывает
до десятка локалов вызывающего, а функция не может перевязать локалы вызывающего, не
возвращая громоздкий кортеж. RunContext — мутабельная «сумка» этого состояния: хелпер
принимает `ctx` и мутирует его поля на месте, а `run()` читает поля из `ctx` как из
единственного источника истины.

Дизайн-решение (2026-08-26, владелец: «сделай лучшим способом с учётом будущего развития
кита»): ctx — ИСТОЧНИК ИСТИНЫ, а не транспорт с обратной синхронизацией локалов. Новое
состояние прогона впредь добавляется полем сюда, а хелперы просто получают `ctx` — без
роста списков параметров и без риска рассинхронизации «локал ↔ поле».

Только stdlib (dataclasses/typing) — кит не тянет внешние зависимости в ядро.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


@dataclass
class RunContext:
    """Состояние pipeline-ветви `run()`.

    Поля сгруппированы по тому, кто их переписывает; группировка — только для чтения
    человеком, dataclass плоский. Стабильные входы (child_root/features_dir/…) лежат
    рядом с переписываемыми, чтобы сигнатуры вынесенных хелперов оставались короткими
    (`_helper(ctx)` вместо `_helper(ctx, child_root, features_dir, provider_name, …)`).
    """

    # --- стабильные входы прогона (ставятся один раз фабрикой, не переписываются) ---
    child_root: Path
    features_dir: Path
    feature: Optional[str]
    provider_name: str
    model: Any
    runtime: str

    # --- policy-группа (resume-policy-restore может переписать при resume) ---
    task_text: str
    signals: dict
    sandbox: bool
    baseline_diff: bool
    require_fix: bool
    author: bool
    review: bool
    open_pr: bool
    write_scope: Any
    max_steps: int
    base: Any
    replan: bool = False
    saved_task: Optional[str] = None       # F-027: продуктовая задача исходного прогона
    base_binding: Optional[dict] = None    # BaseBinding (ref+sha+mode+source)

    # --- routing/trust-группа (model-routing пишет; fix-loop-эскалация читает) ---
    writer_model: Any = None
    writer_prov: Optional[Callable] = None
    rev_model: Any = None
    rev_prov: Optional[Callable] = None
    model_resolution: Optional[dict] = None
    sec_qualified: bool = False
    klp_by_env: Optional[dict] = None
    trust_cache: dict = field(default_factory=dict)
    trust_now: Optional[str] = None
    trust_env: Optional[dict] = None

    # --- предложители (execute+fix-loop перевязывает при эскалации) ---
    prop: Optional[Callable] = None
    rev_prop: Optional[Callable] = None
    auth_prop: Optional[Callable] = None

    @classmethod
    def from_run_args(cls, *, task_text, signals, child_root, features_dir, feature,
                      provider_name, model, runtime, sandbox, baseline_diff, require_fix,
                      author, review, open_pr, write_scope, max_steps, base, replan=False):
        """Собрать ctx из аргументов `run()`.

        Идемпотентная нормализация: `signals` копируется (не мутируем чужой dict) и
        получает `task_text`, `features_dir` дефолтится в `child_root/features` — тем же
        правилом, что и голова `run()`. Копия сигналов безопасна: после сборки источник
        истины — `ctx.signals`, и дальнейшие мутации идут по нему.
        """
        _sig = dict(signals or {})
        _sig.setdefault("task_text", task_text)
        _root = Path(child_root)
        return cls(
            child_root=_root,
            features_dir=Path(features_dir) if features_dir else _root / "features",
            feature=feature, provider_name=provider_name, model=model, runtime=runtime,
            task_text=task_text, signals=_sig, sandbox=sandbox, baseline_diff=baseline_diff,
            require_fix=require_fix, author=author, review=review, open_pr=open_pr,
            write_scope=write_scope, max_steps=max_steps, base=base, replan=replan,
        )
