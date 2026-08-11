# WowRepo

An open-source engine that turns a repository into a beautiful, living website —
where **every page is designed around the meaning of its content**, and the site
rebuilds automatically as the repository changes. It is not a Markdown-in-a-docs-
theme renderer.

WowRepo uses **controlled generation**: the AI acts as content analyst and art
director and produces a validated **page plan** (meaning + composition, never
HTML); the codebase renders that plan deterministically from an approved set of
components. Accessibility, responsiveness, design tokens, and stable behaviour
stay the codebase's job.

```
repository content → content analysis → page plan → approved component
composition → deterministic rendering → visual QA
```

The first repository rendered is **Garden** (`examples/garden/`), a calm personal
space for rituals, places, lived experience, and the recovery of attention — the
demanding test case that keeps the engine honest.

## Quick start

```bash
npm install
npm run dev        # local dev server
npm run build      # static production build (validates all page plans)
npm run check      # astro/type check
npm run lint       # eslint
npm run test       # Playwright browser + visual checks (builds + previews)
```

The build is fully static — no server, no database, and no AI call on a page
view. Deploy `dist/` to any free static host (e.g. Cloudflare Pages).

## Repository layout

```
src/
  engine/       page-plan schema + validation, repo loader, content types
  components/   reusable components, atmosphere system, site chrome, renderer
  layouts/      the single page shell
  scripts/      client islands (reveal, generative atmosphere / WebGL)
  styles/       design tokens + base styles
examples/garden/  Garden as an ingested repository (content + art direction)
tests/visual/     Playwright checks + screenshots
.claude/skills/   the seven project skills
docs/             product vision, architecture, page-plan spec, design, report
```

## Documentation

- [`docs/product-vision.md`](docs/product-vision.md) — what and why
- [`docs/architecture.md`](docs/architecture.md) — how it fits together
- [`docs/page-plan-spec.md`](docs/page-plan-spec.md) — the page-plan format
- [`docs/design-principles.md`](docs/design-principles.md) — the design system
- [`docs/mvp-report.md`](docs/mvp-report.md) — an honest MVP summary
- [`CLAUDE.md`](CLAUDE.md) — persistent working context

## Status

First working MVP: four distinct Garden pages sharing one visual system, each
with a validated page plan; a design-token system; 13 reusable components; a
generative WebGL atmosphere (the growing tree on the Manifesto); Playwright
checks and CI. See the MVP report for what's validated and what's next.
