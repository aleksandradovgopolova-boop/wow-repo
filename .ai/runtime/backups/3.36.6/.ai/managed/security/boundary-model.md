# Boundary model — managed / project / custom / generated / runtime

Модель разделения зон в дочернем репозитории (child), куда устанавливается пакет AI-first системы (Фаза 4). Цель: обновления parent→child не затирают локальное, а ручную правку «управляемого» файла система обнаруживает и не перезаписывает молча (принцип 13, 26).

Рабочий пример структуры — `examples/child-install/.ai/`. Проверка целостности — `02_tools/ci/ai_managed_checksums.py`.

## Зоны `.ai/`

| Зона | Кто пишет | Обновляется parent? | Защищена? | Назначение |
|---|---|---|---|---|
| `managed/` | parent (обновление пакета) | **да** | правка вручную запрещена | ядро + выбранные presets, контракты, схемы |
| `project/` | команда продукта | нет | **да** | локальные правила, ограничения, контекст, overlays |
| `custom/` | команда продукта | нет | **да** | локальные агенты, skills, workflows, extensions |
| `generated/` | adapters (генерация) | перегенерируется | — | runtime-файлы (.claude/.codex/…) из общего source of truth |
| `runtime/` | исполнение | нет | — | временное состояние выполнения (вне постоянных specs) |

## Служебные файлы в `managed/`

- `.provenance.json` — откуда и какой версией установлено (source, installed_version, package, дата).
- `.checksums.json` — sha256 каждого managed-файла (для обнаружения ручной правки).
- `.update-lock` — версия/lock текущего обновления.

## Пути (что обновляет parent / что защищено / что генерируется)

- **обновляет parent:** `.ai/managed/**`.
- **защищено (parent не трогает):** `.ai/project/**`, `.ai/custom/**`, `.ai-ops.yaml` (кроме version bump через PR).
- **generated:** `.ai/generated/**` (в git — опционально; перегенерируемо).
- **продукт:** код и `openspec/**`.

## Обнаружение прямого изменения managed-файла

1. При установке/обновлении parent записывает `.checksums.json` (sha256 всех managed-файлов).
2. Перед следующим обновлением `ai_managed_checksums.py verify` пересчитывает суммы.
3. Расхождение (changed/added/removed) = прямая правка managed → обновление **останавливается**, показывается diff, создаётся backup, предлагается перенести правку в `custom/`-overlay (Ф9, Section 28). **Молча не перезаписываем.**

## Уровни разрешений

См. `security/permission-levels.yaml` (6 уровней) и `config/protected-paths.yaml` (пути, требующие approval — сохранён без изменений).
