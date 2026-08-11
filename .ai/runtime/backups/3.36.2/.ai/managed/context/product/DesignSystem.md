---
read_tier: 2   # ярус чтения: 2 по теме задачи
---

# Design System

Единый источник истины по дизайну продукта. Всё, что проектируется и ревьюится,
опирается на этот файл. UI/UX Designer переиспользует отсюда; design-system-reviewer и ux-reviewer сверяют с этим (гейты design_system_usage, ux_review).

## Design principles
<!-- 3–5 принципов, отличающих продукт (напр. "плотность > воздух", "explain-on-demand") -->

## Design tokens
<!-- Формат: Design Tokens Community Group (DTCG) Format Module (черновик 2025.10).
     Это Community Group draft, НЕ формальный стандарт W3C — но де-факто общий формат,
     который читают Figma, Style Dictionary, Terrazzo, Storybook, Supernova, zeroheight.
     Файлы: JSON с расширением .tokens или .tokens.json (MIME application/design-tokens+json). -->
<!-- Токен = имя + $value (обязательно); опционально $type, $description, $deprecated, $extensions.
     Группировка через вложенные объекты; $type наследуется вниз по группе.
     Алиасы: {group.token} на весь токен; $ref (JSON Pointer) на отдельное свойство.
     $deprecated (true | строка-объяснение) — помечать устаревшее, не удаляя молча. -->
### Color (роль → значение: bg, surface, text, border, accent, semantic success/warning/danger)
### Typography (composite: fontFamily, fontSize, fontWeight, lineHeight)
### Spacing scale (напр. 4/8/12/16/24/32/48)
### Radius / Elevation / Shadow (shadow — composite type)
### Motion (duration + cubicBezier easing — типы DTCG)
### Breakpoints

## Device matrix
<!-- ИСТОЧНИК ИСТИНЫ по целевым устройствам (Responsive by Default, v2.6).
     Определяется один раз на продукт; ux_review, e2e и дизайн ссылаются сюда.
     Формат: класс | min viewport | ввод | обязателен? -->
<!-- Пример:
     | mobile  | 360px  | touch          | да |
     | tablet  | 768px  | touch + курсор | да |
     | desktop | 1280px | курсор + клавиатура | да |
     | wide    | 1680px | курсор + клавиатура | нет (не ломается) |
     Если продукт осознанно НЕ поддерживает класс (например, внутренний
     desktop-инструмент) — зафиксируйте это здесь явно, с причиной. -->

## Components
<!-- Реестр компонентов: имя, назначение, обязательные состояния, do/don't, ссылка на код/Figma -->

## Patterns
<!-- Составные паттерны: формы, таблицы, пустые состояния, онбординг, ошибки, пагинация -->

## Voice, tone and content design
<!-- Тон, правила микрокопирайта, terminology (синхронизировать с team/Glossary.md) -->

## Tooling
<!-- translation: Style Dictionary / Terrazzo; docs: Storybook / zeroheight / Supernova.
     Источник токенов (Figma variables / репозиторий) → генерация платформенного кода. -->

## Accessibility baseline reference
<!-- Ссылка на rules/quality/AccessibilityBaseline.md; целевой уровень WCAG -->

## Owner and source
<!-- Владелец дизайн-системы; где живут токены/компоненты -->
