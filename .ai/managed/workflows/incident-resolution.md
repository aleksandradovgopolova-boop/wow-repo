# Workflow: incident resolution

```text
Detect
→ classify impact
→ stabilize
→ communicate
→ diagnose
→ recover
→ verify
→ postmortem
→ corrective actions
→ memory update
```

Во время стабилизации приоритет — снижение ущерба. Глубокий root cause analysis выполняется после восстановления сервиса.

Шаг «memory update» обязателен: инцидент не считается закрытым без записи в
`memory/incidents/` (для этого workflow отказ от записи не предусмотрен).
