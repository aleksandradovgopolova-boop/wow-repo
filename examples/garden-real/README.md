# examples/garden-real — the real Garden repository, ingested by WowRepo

This is the **real Garden product content** (its public surface) turned into a
living website by the WowRepo engine — a demonstration of the product thesis on
real data, not placeholder copy.

All prose is drawn verbatim from the Garden repository's public documents
(`public/01-introduction`, `public/04-world`, …). Nothing is invented.

## What it demonstrates

- **A journey, not a file tree.** You do not land on a README or a directory —
  you land in the place: *"A private digital place that can wait."* with a
  single **Begin**, then walk Why → What → World like chapters.
- **The living atmosphere** (the growing tree) on the doorway, settling into
  calm editorial reading.
- **One repository, many views.** The same content is projected for two
  audiences — a public **Visitor** journey at the root, and an **Engineer**
  view under `/engineer/` that additionally sees internal material.
- **Public ↔ internal gating.** On *The world of Garden*, the
  "See implementation" door (Preview Pattern, ADR-012, tests) renders **only**
  for the engineer audience. A visitor never sees it.

## Build it

The engine reads whichever repository `WOWREPO_ROOT` points at (default:
`examples/garden`):

```bash
WOWREPO_ROOT=examples/garden-real npm run build
WOWREPO_ROOT=examples/garden-real npm run preview
```

Routes generated: `/`, `/why`, `/what`, `/world` (visitor) and the same under
`/engineer/…` (engineer projection).

## Structure

```
site.yaml            site identity + audiences (visitor, engineer)
page-plans/*.yaml     one validated plan per page (meaning + composition)
content/*.yaml        named content sources — the real Garden text
```

This is a first opening slice of the full vision (Atlas, Research, Roadmap,
Ask-AI, AI OPS remain to build). It proves the doorway + journey + projection
on real content.
