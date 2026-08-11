# Error Handling

- Ошибки классифицируются: validation, business, dependency, internal.
- Пользователь не получает stack trace и секреты.
- Retry применяется только к временным и идемпотентным операциям.
- Молчаливый fallback запрещён, если он скрывает потерю данных или нарушение бизнес-правила.
- Критичные ошибки имеют correlation id и observability signal.
