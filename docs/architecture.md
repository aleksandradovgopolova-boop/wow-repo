# WowRepo — Architecture

The simplest architecture that can grow into a reusable product: a static Astro
site whose pages are composed deterministically from validated page plans.

## The pipeline

```
examples/<site>/                     ← a "repository" the engine ingests
  site.yaml                          site identity
  page-plans/*.yaml                  one validated plan per page (meaning + composition)
  content/*.yaml                     named content sources referenced by plans
        │
        ▼  build time (Node, no runtime AI)
src/engine/load.ts                   read + validate plans, resolve content
        │
        ▼
src/pages/[...slug].astro            one route generates every page
  → src/components/PageRenderer.astro   map plan.sections → approved components
  → src/layouts/PageLayout.astro        shell, chrome, optional Atmosphere
        │
        ▼
dist/                                plain static HTML — no server, no runtime AI
```

## Directory layout

```
src/
  engine/           reusable engine logic (no site-specific content)
    page-plan.ts        the page-plan schema (Zod) — single source of truth
    content-types.ts    typed content shapes each component consumes
    load.ts             ingest a repo: validate plans, resolve content, check links
  components/        reusable visual components (Garden-agnostic)
    *.astro             13 editorial components (see design-principles.md)
    islands/            React island(s) for genuine interaction (BreathPacer)
    atmosphere/         Atmosphere.astro — generative background wrapper
    site/               SiteHeader / SiteFooter (driven by repo data)
    PageRenderer.astro  the approved-component registry + composition
  layouts/PageLayout.astro   the single page shell
  scripts/          client islands: reveal.ts, atmosphere.ts (WebGL engine)
  styles/           tokens.css (design system), base.css, reveal.css
examples/garden/    Garden as a repository (content + art direction only)
tests/visual/       Playwright browser + visual checks + screenshots
.claude/skills/     the seven project skills (the repeatable workflows)
docs/               this documentation
```

### Why content lives in `examples/garden`, not `src/content`

WowRepo's whole story is that the **engine ingests a repository**. Modelling
Garden as that repository (a directory the engine reads at build time) keeps the
engine and the example cleanly separated, and makes "point the engine at a
different repo" a first-class idea rather than an afterthought. Validation is
done by the engine's own Zod schema in `load.ts`, so the plan format stays
portable rather than tied to Astro's `src/content` conventions.

## Deterministic rendering

`PageRenderer.astro` holds the **approved component registry** — a closed map
from `ComponentName` to a real Astro component. A plan may only name components
in this map; the schema's `COMPONENT_NAMES` is kept in lockstep. There is no
code path from a plan to arbitrary markup. Each section's `source` names a block
in the page's `content/*.yaml`; the component reads it as typed content.

## Atmospheres (generative backgrounds)

A page plan may request an `atmosphere` (`ATMOSPHERES`). `PageLayout` mounts
`Atmosphere.astro`, which renders two fixed canvases and loads
`src/scripts/atmosphere.ts` — one shared WebGL daylight engine (dappled light,
god-rays, a rising sun) plus a small per-variant 2D foreground (a growing plant,
drifting dust, columns of light, or a breathing glow). Atmospheres are:

- **decorative** — every page is fully readable and usable without them;
- **degrade gracefully** — no WebGL / JS / reduced motion → the atmosphere hides
  or renders a single still frame, and the page is unaffected;
- **art-direction-as-data** — the variant is chosen in the plan, not in code.

A reading veil (`Atmosphere.astro`) keeps text legible over the light.

## Design tokens

`src/styles/tokens.css` is the only source of colour, type, spacing, radii,
motion, and breakpoints. Component styles reference semantic tokens; no raw
values live in components or pages. A different site ships a different token
*file* (values) while the token *names* stay fixed, so components stay reusable.

## What is reusable vs. Garden-specific

| Reusable engine (no Garden inside)            | Garden-specific                          |
| --------------------------------------------- | ---------------------------------------- |
| `src/engine`, `src/components`, `src/layouts` | `examples/garden/**`                     |
| `src/scripts/atmosphere.ts` (variant-driven)  | token **values** in `src/styles/tokens.css` |
| token **structure/names** in `tokens.css`     | `examples/garden/docs/art-direction.md`  |

## Automatic updates & cost

- Editing content or a plan changes the static build output — the site rebuilds
  from the repository.
- GitHub Actions runs lint, type-check, tests, and build on every push/PR.
- The site deploys to any free static host (e.g. Cloudflare Pages) — no server,
  no database, **no AI call on a normal page view**.
- AI-assisted planning happens during development (via Claude Code), not at
  runtime. The architecture stays compatible with future BYOK / local-model
  planning, but none is implemented now.
