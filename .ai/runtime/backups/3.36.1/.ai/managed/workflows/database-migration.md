# Workflow: миграция базы данных

```text
Context Builder
→ Database Engineer
→ Backend/System Analyst
→ Security Review при чувствительных данных
→ Human approval
→ dry-run
→ совместимый rollout
→ backfill
→ validation queries
→ cutover
→ monitoring
```

Удаление старой схемы выполняется отдельным этапом после окончания compatibility window.
