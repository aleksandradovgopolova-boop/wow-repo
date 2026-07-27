# CLAUDE.md — WowRepo

Persistent context for working in this repo. Repeatable workflows live in
`.claude/skills/`, not here.

## What WowRepo is

An open-source engine that turns a repository into a beautiful, living website —
every page designed around the *meaning* of its content, and rebuilt
automatically when the content changes. It is **not** a Markdown-in-a-docs-theme
renderer.

The pipeline is controlled generation — the AI is a content analyst and art
director; the codebase owns everything that must stay correct:

```
repository content → content analysis → page plan → approved component
composition → deterministic rendering → visual QA
```

The AI never emits arbitrary HTML/CSS. Pages are composed only from approved
components (`COMPONENT_NAMES`) and optional approved atmospheres (`ATMOSPHERES`),
described by a validated page plan.

## Garden — the first example

Garden (`examples/garden/`) is the first repository the engine renders, and the
demanding test case. It is a personal space for rituals, meaningful places, lived
experiences, the recovery of attention, and reflection. Treat `examples/garden`
as *content the engine ingests*, never as part of the engine.

Garden must feel calm, expressive, alive, human, and emotionally intelligent. It
must **not** look like a docs portal, SaaS landing, dashboard, corporate KB,
generic AI site, or a wall of identical rounded cards.

## Product principles

- **Meaning first.** Every element earns its place by carrying meaning.
- **Controlled generation.** No arbitrary HTML; compose approved components.
- **Reusable vs. Garden-specific are separated.** The engine
  (`src/engine`, `src/components`, `src/layouts`, `src/scripts`, and the token
  *structure* in `src/styles`) carries no Garden content or styling. Garden lives
  in `examples/garden/**` and in token *values* (`src/styles/tokens.css`).
- **Static & cheap.** No backend, DB, auth, or runtime AI. No AI call on a normal
  page view. Prefer build-time processing and free static hosting.

## Architecture constraints

- Stack: Astro + TypeScript, Astro-native Zod content validation, CSS design
  tokens, tiny vanilla/WebGL islands (React available for real interaction),
  Playwright, GitHub Actions.
- Page plans: YAML in `examples/<site>/page-plans/`, validated at build against
  `src/engine/page-plan.ts` (schema is the single source of truth).
- Rendering: `src/pages/[...slug].astro` → `PageRenderer.astro` maps plan
  sections to components via the registry. Atmospheres mount in `PageLayout`.
- Don't add large dependencies without a concrete need. Prefer static generation.

## Commands

```bash
npm run dev        # local dev server
npm run build      # static production build (validates all page plans)
npm run check      # astro/type check
npm run lint       # eslint
npm run test       # Playwright browser + visual checks (builds + previews)
```

## Safety & accessibility (non-negotiable)

- Garden must preserve user autonomy: never diagnose, pressure, create
  dependency, use shame / fear / urgency, present assumptions as facts, decide
  for the user, or use manipulative engagement mechanics.
- Every page: keyboard-operable, visible focus, ordered headings, comfortable
  measure, legible over any atmosphere, fully usable with motion disabled
  (`prefers-reduced-motion`) and without WebGL/JS. No auto-playing motion at the
  reader.

## Modifying canonical Garden content

The Garden copy in `examples/garden/content/*.yaml` is drawn from confirmed
Garden context. Lines marked `# CANON` are approved — do not reword their
meaning. Lines marked `# DRAFT` are placeholder voice and may be revised. New
copy beyond canon must be marked `# DRAFT`. Never invent facts about Garden or
the user.

## Definition of done

Project runs locally; production build succeeds; the four Garden pages exist,
are meaningfully different, share one visual system, and each has a validated
page plan; tokens implemented; reusable code separated from Garden-specific; all
skills present; Playwright checks run; desktop + mobile screenshots exist; no
horizontal overflow or broken navigation; the site reads as neither generic docs
nor a SaaS landing; `docs/mvp-report.md` is honest.
