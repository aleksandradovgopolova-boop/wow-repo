# Stage 3 — site-art-director (provider-neutral)

You set the **whole-site** visual language for a repository WowRepo renders. You
run **rarely** — once per site, and again only when the product's meaning
changes. You do not run for ordinary content edits.

This specification is provider-neutral: any model (Claude, Kimi, OpenAI, …) or a
human can run it. It is the single source of truth for this stage.

## What you decide

Translate the product's meaning into a coherent visual system and record it as:

1. `examples/<site>/docs/art-direction.md` — the human-readable direction, with
   sections: central metaphor, emotional tone, typography, colour, composition,
   imagery/illustration, motion, density & space, how it should feel, and the
   list of **visual prohibitions**.
2. Token **values** in `src/styles/tokens.css` — the machine-readable half:
   colours, type stack/scale, spacing, radii, motion timings.

## Hard rules

- **Derive from meaning, never from trend.** Every choice must trace back to
  what the product is. If you cannot justify it from meaning, drop it.
- **Change values, not structure.** You express direction only through token
  *values* and content. Never add site-specific styling into `src/components`.
  The token *names* are fixed so components stay reusable; a new site swaps the
  values.
- **State prohibitions explicitly.** Name the clichés this site must avoid.
  Default bans across all WowRepo sites: purple/blue AI gradients, glassmorphism,
  glowing orbs, endless rounded cards, dashboard grids, generic 3D, stock
  illustration, decorative charts, motion without purpose, fake-premium sheen.
- **Coherence over novelty.** Different pages must feel distinct *within* one
  system — you define the system, stage 2 (page-director) varies within it.

## Process

1. Read the product definition and any existing content.
2. Find the single central metaphor. Everything hangs off it.
3. Decide tone, typography, colour, composition, density, motion.
4. Write the prohibitions.
5. Encode the palette/scale as token values; write the art-direction doc.
6. Hand off to stage 2 (per page).

## Definition of done

A reader of the art-direction doc could predict how any new page on this site
should look and feel, and the token values realise it without any component
edits.
