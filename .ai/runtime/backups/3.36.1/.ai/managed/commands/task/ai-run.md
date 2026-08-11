# Команда: ai-run

> **Канонический вход (3.0-срез 1).** `ai-run` — основной путь запуска задачи. `ai-start-task`
> сохраняется как совместимый алиас той же спины и не удаляется (снятие — не раньше 4.0).

## Назначение

Единый вход «задача → контролируемое исполнение → отчёт» одной транзакцией:
классификация/маршрут → RunPlan (base_workflow + треки + агрегированные гейты) →
WorkItem → регистрация в реестре активных работ → исполнение → компактный отчёт.
Собирает то, что раньше было отдельными шагами `ai-start-task`. Инструмент —
`tools/ai_ops_run.py`.

## Порядок выполнения

```
tools/ai_ops_run.py run "<задача>" <child_root> --signals '<json сигналов>' \
    [--runtime claude-code|generic-orchestrator] [--provider mock] [--execute]
```

1. Построить RunPlan по сигналам (ui_changed, measurable_behavior, security_surface_changed,
   events_changed, ai_component, deploy_touched, user_facing_change, affected_areas).
2. Создать WorkItem и записать RunPlan (`features/<id>/run-plan.yaml`).
3. Зарегистрировать активную работу (ветка/зоны/сессия) — для conflict forecast.
4. Исполнение:
   - **claude-code** (и рантаймы со своим tool loop): контроллер готовит план и каркас
     состояния; стадии/патчи/тесты исполняет **рантайм**, следуя плану. status = `planned`.
   - **generic-orchestrator**: контроллер реально прогоняет стадии и гейты; status =
     `done`/`blocked` по evidence.
5. Записать компактный `features/<id>/run-report.json` и показать резюме.

## Границы (честно)

Кит не притворяется, что исполнил за рантайм: для claude-code «сделал» = evidence
(commit SHA, exit codes, структурный reviewer-result), а не факт вызова. `ai-run` —
канонический вход (3.0-срез 1); сплит на пакеты — оставшиеся срезы 3.0 (см.
`docs/3.0-design.md`).

## Результат

WorkItem + RunPlan + запись в реестре активных работ + run-report; для
generic-orchestrator — прогон стадий с вердиктом гейтов.
