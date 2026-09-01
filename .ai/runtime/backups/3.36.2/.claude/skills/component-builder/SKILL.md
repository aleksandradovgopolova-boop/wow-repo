---
name: component-builder
description: >-
  Create a new reusable component only when existing approved components cannot
  express the required meaning. Use design tokens, support mobile and
  accessibility, document intended usage, and avoid single-use decorative tricks.
---

# component-builder

You add to WowRepo's approved component library — but only as a last resort.
Components are the stable, accessible, responsible half of the pipeline. A new
one is a long-term commitment, so the bar to add is high.

## Before you build anything

1. **Try to compose instead.** Re-read the existing components in
   `src/components` and their "WHEN TO USE / WHEN NOT TO USE" docs. If any
   existing component (possibly with a new `variant` or `emphasis`) can carry
   the meaning, use it. Do not build.
2. **Confirm the meaning, not the look.** A new component must express a
   *semantic role* the library lacks (e.g. "a two-sided boundary"), not a
   visual flourish you fancy.
3. If you do build, you must also register it: add its name to
   `COMPONENT_NAMES` in `src/engine/page-plan.ts`, add it to the registry in
   `src/components/PageRenderer.astro`, and add a content-shape type in
   `src/engine/content-types.ts`.

## Rules for a new component

- **Tokens only.** Every colour, size, space, radius, duration, and easing comes
  from `src/styles/tokens.css`. No raw hex, px, or one-off timings. Ever.
- **Mobile first and fluid.** It must work from 320px up. Use the fluid spacing
  and type steps; test the narrow layout.
- **Accessible.** Semantic HTML; real headings; keyboard operable if
  interactive; visible focus; `aria-*` only where it adds meaning. If it
  animates, it must also work under `prefers-reduced-motion`.
- **Content in, no content baked.** It receives typed content via props; it does
  not hard-code Garden (or any site's) copy.
- **Engine-agnostic.** No Garden-specific styling. If Garden needs a particular
  look, that belongs in token *values* or content, not in the component. The
  `design-system-guardian` will reject leakage.
- **Document it.** A frontmatter comment with a clear semantic role and explicit
  "WHEN TO USE" / "WHEN NOT TO USE". If you can't write when *not* to use it,
  it's not well-defined enough to add.
- **No single-use decorative tricks.** If it only makes sense on exactly one
  page, it is probably content or art direction, not a component.

## Definition of done

The component compiles, type-checks, is registered, uses only tokens, works at
320px and with reduced motion, and its docs make its role and boundaries
unambiguous.
