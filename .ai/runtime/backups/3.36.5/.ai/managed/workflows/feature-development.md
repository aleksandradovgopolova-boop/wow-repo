# Workflow: разработка новой функции

## Trigger

Новая пользовательская возможность или существенное изменение поведения.

## Поток

```text
Product Manager / Business Analyst
→ Context Builder
→ Requirements Reviewer
→ UI/UX Designer + System Analyst
→ Solution Architect при среднем/высоком риске
→ Human approval
→ Task Planner
→ Frontend / Backend / Database / Integration work packages
→ Implementation Integrator
→ Test Engineer + Regression Analyst
→ Code Review + risk-based reviews
→ Final Verifier
→ Documentation + Memory
```

## Gates

- требования проверяемы;
- контракты согласованы;
- write-scope разделён;
- tests/build/lint пройдены;
- acceptance evidence приложено;
- monitoring и rollback определены для рискованных изменений.
