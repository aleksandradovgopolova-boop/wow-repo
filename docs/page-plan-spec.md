# WowRepo — Page-Plan Specification

A **page plan** describes the *meaning* and *composition* of one page. It never
contains HTML, CSS, or markup. It is the contract the AI (as content analyst and
art director) produces and the engine renders deterministically.

- Format: YAML, one file per page in `examples/<site>/page-plans/`.
- Schema & validation: `src/engine/page-plan.ts` (Zod). Plans are validated at
  build time; an invalid plan **fails the build** with a readable error.

## Example

```yaml
version: 1
page:
  id: garden-manifesto
  type: manifesto
  path: /
  title: Manifesto
  order: 0
  purpose: help-a-first-time-visitor-feel-the-core-idea-before-explaining-it
  audience: a-curious-first-time-visitor
  primary_message: Garden is a space for living, not for managing life.
  summary: The feeling Garden is built around, in a few plain lines.
  density: low
  tone: [calm, intimate, quietly-confident]

narrative: [immersive-arrival, the-beliefs, a-breath, one-line-that-stays, a-quiet-ending, a-way-onward]

sections:
  - component: immersive-opening
    source: opening
    intent: Feeling before information.
  - component: manifesto-lines
    source: beliefs
  - component: spatial-break
    variant: breath
  - component: next-path
    source: onward

motion:
  intensity: subtle
  purpose: [reveal, continuity]

atmosphere:
  variant: canopy-light
  intensity: immersive

relationships:
  next: what-garden-is
  related: [principles, safety]
```

## Fields

### `page` (required)

| field             | meaning                                                        |
| ----------------- | ------------------------------------------------------------- |
| `id`              | unique page id (used by relationships)                        |
| `type`            | semantic page type (see below)                                |
| `path`            | URL path, starts with `/`                                     |
| `title`           | short title for nav + `<title>`                               |
| `summary`         | one line for metadata + related-links previews                |
| `order`           | navigation ordering hint                                      |
| `purpose`         | what the page is for                                          |
| `audience`        | who it is for                                                 |
| `primary_message` | the one thing the reader should leave with                    |
| `density`         | `low` / `medium` / `high`                                     |
| `tone`            | one or more emotional tones (constrained vocabulary, `TONES`) |

### `narrative` (required)

The intended reading rhythm, named beat by beat. Guides authoring and review; it
is not mechanically mapped 1:1 to sections.

### `sections` (required)

Ordered composition. Each item:

- `component` — an approved name (`COMPONENT_NAMES`). **Only** these may appear.
- `source` — key into the page's `content/<id>.yaml` (optional for purely
  structural components like `spatial-break`).
- `emphasis` — `low` / `medium` / `high` (optional).
- `variant` — a named, approved component variant (e.g. `spatial-break`
  `breath`) (optional).
- `intent` — a human note on art-direction intent; never rendered (optional).

### `motion` (optional, defaulted)

`intensity` (`none`/`subtle`/`expressive`) and `purpose[]`.

### `atmosphere` (optional)

A generative background: `variant` (`ATMOSPHERES`) + `intensity`
(`subtle`/`present`/`immersive`). Decorative only.

### `accessibility` (optional, defaulted)

`notes[]` surfaced to reviewers; `motion_optional` (always effectively true).

### `relationships` (optional, defaulted)

`next` (page id) and `related[]` (page ids). Powers `next-path` and
`related-links` without hard-coding navigation into content. Referenced ids are
validated to exist.

## The plan supports (checklist from the brief)

page purpose ✓ · page type ✓ · audience ✓ · main message ✓ · content density ✓ ·
emotional tone ✓ · narrative sequence ✓ · component selection ✓ · references to
source content ✓ · motion intent ✓ · accessibility constraints ✓ · relationships
to other pages ✓ (plus an optional generative atmosphere).

## Page types (`PAGE_TYPES`)

`manifesto`, `concept-explanation`, `principles`, `safety-boundaries`,
`process-ritual`, `place-environment`, `timeline`, `architecture-system`.

The MVP implements the four needed by Garden's first pages (manifesto,
concept-explanation, principles, safety-boundaries). The rest are declared and
render through the same generic pipeline; they simply have no Garden page yet.

## Extension mechanism

- **New page type:** add to `PAGE_TYPES`, document its typical rhythm here.
- **New component:** see the `component-builder` skill — add to `COMPONENT_NAMES`,
  the registry in `PageRenderer.astro`, and a content-shape in `content-types.ts`.
- **New atmosphere:** add to `ATMOSPHERES` and a case in `atmosphere.ts`.

Because the schema is the single source of truth, every extension is
type-checked and build-validated.
