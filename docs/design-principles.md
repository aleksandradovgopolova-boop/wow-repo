# WowRepo — Design Principles

How the codebase keeps generated pages coherent, accessible, and free of generic
AI-design tells — while letting each page feel distinct.

## The design system

All visual decisions resolve to **semantic design tokens** in
`src/styles/tokens.css`. Components and pages never use raw hex, px, one-off
spacing, or ad-hoc timings. The tokens define:

- **Colour** — semantic names (`--color-bg`, `--color-text`, `--color-accent`,
  `--color-care`, …) mapped from a private base palette. A different site swaps
  the *values*; the *names* stay fixed.
- **Typography** — display / body / mono families and a fluid modular scale
  (`--step--1` … `--step-6`), with leading, tracking, and weight tokens.
- **Spacing** — a fluid scale (`--space-3xs` … `--space-3xl`).
- **Layout** — content widths and reading measures (`--measure`, `--width-*`),
  a fluid `--gutter`.
- **Surfaces** — subtle surfaces, hairline borders, restrained radii
  (deliberately small — we avoid pill/card aesthetics), and near-absent shadows.
- **Motion** — durations, easings, and a reduced-motion override that zeroes
  them all.
- **Focus** — a visible focus ring, never removed.
- **Breakpoints** — documented reference values.

Because every component reads tokens, a page cannot introduce a one-off colour
or size, and the whole site drifts together or not at all.

## Component library (13 components)

Built to make the four Garden pages excellent while staying reusable. Each has a
clear semantic role and documented "WHEN TO USE / WHEN NOT TO USE".

`editorial-hero` · `immersive-opening` · `manifesto-lines` · `longform-prose` ·
`principle-statement` · `pull-quote` · `spatial-break` (with an opt-in `breath`
interaction island) · `narrative-steps` · `boundary-comparison` ·
`quiet-callout` · `closing-reflection` · `next-path` · `related-links`.

Every component: works from 320px, uses tokens only, is keyboard-operable where
interactive, supports reduced motion, avoids unnecessary dependencies, and reads
content via typed props (no baked-in copy).

## Making pages distinct without inconsistency

- **Different rhythms.** Each page selects a different set and order of
  components (a manifesto breathes; a concept page explains; a safety page
  reassures).
- **Different atmospheres.** A page may wear a generative background matched to
  its meaning (growing light / a sunbeam / columns of light / a sheltering
  glow) — all rendered by one shared engine, so distinct never means incoherent.
- **One system underneath.** Same tokens, type, palette, chrome, and motion
  language across every page.

## Accessibility (baseline, not optional)

- Semantic HTML and ordered headings; one `h1` per page.
- Visible focus on every interactive element.
- Comfortable measure; text legible over any atmosphere via the reading veil.
- Every animation works — and every page reads — with `prefers-reduced-motion`.
  Atmospheres render a single still frame; reveals show immediately.
- No auto-playing motion aimed at the reader. The only interactive island (the
  breath pacer) is opt-in and stoppable.
- Full usability without WebGL or JavaScript.

## Anti-AI-slop stance

Deliberately avoided: purple/blue AI gradients, glassmorphism, glowing orbs,
endless rounded cards, dashboard grids, generic 3D, stock illustration,
decorative charts, animation without purpose, and fake-premium sheen. Emphasis
is drawn with quiet vertical marks, hairlines, space, and typography — not boxed,
shadowed cards. Structural devices (numbering, eyebrows) encode something true
about the content; they are not decoration. See the `anti-ai-slop-reviewer`
skill.

## The engine / site boundary

Reusable engine code carries no site-specific content or styling. Garden's
identity lives only in `examples/garden/**` and in token *values*. If a Garden
need seems to require editing a component's styling, that is leakage — it belongs
in tokens or content, or the component change must be made generic. The
`design-system-guardian` skill enforces this so WowRepo never becomes a Garden
template.
