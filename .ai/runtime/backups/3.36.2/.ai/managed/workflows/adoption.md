# Workflow: adoption (решение уже у пользователей)

## Trigger

Функция выпущена; нужно довести до активации, удержания и оценить эффект.

```text
Intake
→ launch readiness (события tracking plan live, dashboard с данными, baseline зафиксирован)
→ analytics review (независимая проверка: events_verified_live)
→ adoption plan (activation/aha, онбординг, retention)
→ user docs (UserGuide + InAppContent + FAQ + user changelog)
→ docs review
→ feedback loop (сбор, триаж, темы -> Opportunity Solution Tree)
→ [окно анализа]
→ post-launch review (эффект vs baseline; continue / iterate / scale / rollback)
→ independent review (product-reviewer)
→ memory update (обязательно: learning_output = required)
```

Правила: baseline фиксируется до запуска; rollback-триггеры определены заранее;
успех — по достижению outcome и активации, подтверждённых данными, а не по факту
выката. Инсайты adoption — вход workflow INSIGHTS и следующего Discovery
(knowledge graph: insight -> feeds).

Шаблоны: `templates/product/{LaunchPlan,AdoptionPlan,FeedbackLoop,PostLaunchReview}.md`,
`templates/documentation/InAppContent.md`.
