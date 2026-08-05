#!/usr/bin/env python3
"""Execution budget — жёсткий потолок прогона (v2.38, Execution Engine Фаза 2).

RunPlan объявляет execution_budget (max_model_calls / max_cost). Раньше это была только
декларация; здесь — enforcement: перед каждым вызовом модели бюджет проверяется и, при
превышении, вызов НЕ делается (BudgetExceeded). Нужен и sequential-оркестратору, и будущей
tool-calling петле — чтобы «дал задачу» не превратилось в неограниченный расход.

Честно: max_model_calls детерминирован (считаем вызовы). max_cost — только если провайдер
возвращает стоимость/токены; без учёта токенов cost остаётся 0 и по нему не блокируем
(объявлено, не выдаётся за enforced).

Использование (программно):
  from budget import Budget, BudgetExceeded
  b = Budget(max_model_calls=20)
  b.charge_call()          # перед каждым вызовом модели; бросит BudgetExceeded при превышении

  budget.py --selftest
"""

import sys


class BudgetExceeded(Exception):
    pass


class Budget:
    def __init__(self, max_model_calls=None, max_cost=None, max_duration=None):
        self.max_model_calls = max_model_calls
        self.max_cost = max_cost
        self.max_duration = max_duration       # хранится для отчёта; enforcement — на рантайме
        self.model_calls = 0
        self.cost = 0.0

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(max_model_calls=d.get("max_model_calls"),
                   max_cost=d.get("max_cost"), max_duration=d.get("max_duration"))

    def charge_call(self, cost=0.0):
        """Проверяет ДО инкремента — потолок никогда не превышается, вызов не делается."""
        if self.max_model_calls is not None and self.model_calls + 1 > self.max_model_calls:
            raise BudgetExceeded(f"max_model_calls={self.max_model_calls} превышен "
                                 f"(уже {self.model_calls})")
        if self.max_cost is not None and cost and self.cost + cost > self.max_cost:
            raise BudgetExceeded(f"max_cost={self.max_cost} превышен")
        self.model_calls += 1
        self.cost += cost or 0.0

    def remaining_calls(self):
        return None if self.max_model_calls is None else max(0, self.max_model_calls - self.model_calls)

    def to_dict(self):
        return {"max_model_calls": self.max_model_calls, "model_calls": self.model_calls,
                "max_cost": self.max_cost, "cost": self.cost,
                "remaining_calls": self.remaining_calls()}


def selftest():
    ok = True

    def expect(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"{'PASS' if cond else 'FAIL'} {name}")

    b = Budget(max_model_calls=2)
    b.charge_call(); b.charge_call()
    expect("два вызова в пределах бюджета", b.model_calls == 2)
    try:
        b.charge_call(); raised = False
    except BudgetExceeded:
        raised = True
    expect("третий вызов -> BudgetExceeded", raised)
    expect("потолок не превышен (осталось 0, calls=2)", b.remaining_calls() == 0 and b.model_calls == 2)

    unb = Budget()
    for _ in range(100):
        unb.charge_call()
    expect("без лимита -> не блокирует", unb.model_calls == 100 and unb.remaining_calls() is None)

    cb = Budget(max_cost=1.0)
    cb.charge_call(cost=0.6)
    try:
        cb.charge_call(cost=0.6); raised = False
    except BudgetExceeded:
        raised = True
    expect("max_cost превышение -> BudgetExceeded (если cost учитывается)", raised)

    expect("from_dict читает RunPlan.execution_budget",
           Budget.from_dict({"max_model_calls": 5}).max_model_calls == 5)

    print("budget selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv):
    if "--selftest" in argv:
        return selftest()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
