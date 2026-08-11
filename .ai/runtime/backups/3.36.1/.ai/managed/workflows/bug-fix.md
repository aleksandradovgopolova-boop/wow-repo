# Workflow: исправление дефекта

```text
Repository Explorer
→ подтверждение симптома
→ Regression Analyst
→ Developer
→ Test Engineer
→ Code Reviewer
→ Final Verifier
```

## Правила

- исправлять root cause, а не только симптом;
- по возможности сначала создать воспроизводящий тест;
- не смешивать fix с рефакторингом;
- критичный инцидент после стабилизации передать Incident Analyst.
