# Database Changes

- Использовать expand-migrate-contract для значимых изменений.
- DDL, backfill и удаление старой схемы разделять по этапам.
- Оценивать объём, блокировки и replication lag.
- Миграции должны быть повторяемыми или безопасно восстанавливаемыми.
- Перед cutover обязательны validation queries и rollback trigger.
