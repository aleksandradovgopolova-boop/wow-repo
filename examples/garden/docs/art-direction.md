# Garden — Art Direction

> This document defines the visual language for the Garden website. It is
> Garden-specific. The reusable WowRepo engine and component library must not
> absorb any of these decisions as defaults — they live here, and are expressed
> only through token *values* (`src/styles/tokens.css`) and content, never
> through new component styling.

The art direction is derived from Garden's meaning, not from a trend. Garden
supports rituals, meaningful places, lived experiences, the recovery of
attention, reflection, and a careful relationship with one's own life. The
design has one job: to make that care felt before a single feature is explained.

## 1. Central visual metaphor

**Daylight in a quiet room, and a garden tended by hand.**

Not a lush illustrated garden, and not a botanical brand. The metaphor is the
*condition* of a garden: something living, tended slowly, given light and space,
never finished. Pages should feel like paper on a table near a window in the
late morning — warm, still, unhurried. Growth is implied through small living
marks (a seed, a sprout, a stem), never through literal plant imagery.

## 2. Emotional tone

Calm, expressive, alive, human, emotionally intelligent. The site should feel
like it is paying attention to the reader rather than demanding attention from
them. Confidence here is quiet — Garden never raises its voice, never rushes,
never flatters.

## 3. Typography direction

- **Display / voice:** a warm old-style serif (Iowan Old Style / Palatino /
  Georgia stack). Serifs carry the human, literary, personal register Garden
  needs. Headings and manifesto lines are set large and given room.
- **Reading / body:** a humanist system sans at a generous line height for
  comfort and neutrality under long reading.
- **Labels:** small, letter-spaced uppercase in the faint ink — used sparingly
  to name a section's role, never to shout.
- Line length is held to a comfortable measure. Intimate passages narrow
  further. Text wrap is balanced on headings and pretty on prose.

## 4. Colour direction

Warm paper and botanical ink, in daylight. **No dark UI, no purple/blue AI
gradients.**

- **Paper** (`--color-bg`): warm off-white, like uncoated stock.
- **Ink** (`--color-text`): a warm near-black — never pure `#000`.
- **Leaf** (`--color-accent`): a muted botanical green — the one living accent,
  used for small marks, links, and moments of growth.
- **Clay** (`--color-care`): a warm terracotta reserved for care and safety —
  warmth and protection, never decoration or alarm.
- **Dawn** (`--color-glow`): a soft light wash used only as barely-there
  daylight in the background, never as a glowing orb or gradient feature.

Colour is used at low saturation and low frequency. The page is mostly paper.

## 5. Composition principles

- **Asymmetry over centring.** Ledes offset from titles; content aligned to a
  reading edge, not floated in the middle of the viewport.
- **One column, one path.** A clear vertical reading path. No multi-column
  dashboards, no card grids.
- **Space is content.** Empty space marks breath and shifts in the narrative;
  it is composed deliberately, not left as leftover padding.
- **Left edges, not boxes.** Emphasis is drawn with quiet vertical marks and
  hairlines rather than boxed, shadowed cards.

## 6. Image & illustration language

For the MVP: **no photography, no stock illustration, no 3D, no decorative
graphs.** Visual interest comes from typography, space, hairlines, and a tiny
family of hand-simple CSS marks (seed, sprout, stem, node). If illustration is
added later it must be original, quiet, and botanical-adjacent — line, not
render. Never a hero image of a literal garden.

## 7. Motion language

Motion is subtle and purposeful: content settles into place on first view
(reveal), links ease, the breath pacer breathes. Motion always *means*
something — arrival, continuity, rest. There is **no auto-playing motion** and
no motion used for spectacle. Everything works, and reads well, with motion
fully disabled (`prefers-reduced-motion`).

## 8. Density & empty space

Low density. Generous margins. Few elements per screen. The reader should never
feel a page is trying to fit more in. Density may rise slightly on the
concept and principles pages, but paper always wins.

## 9. How Garden should feel

Like being trusted. Like a space that is already calm when you arrive, that
does not want anything from you, and that leaves you a little more settled than
it found you.

## 10. Visual clichés to avoid (hard prohibitions)

- purple/blue AI gradients; glassmorphism; glowing orbs
- endless identical rounded cards; card-grid layouts
- dashboard grids; admin-panel chrome
- SaaS landing patterns (feature triples, logo walls, giant CTA bands)
- generic 3D objects; stock illustration; decorative charts
- animation without a communicative purpose
- fake-premium sheen; heavy shadows and elevation
- urgency, streaks, badges, or any manipulative engagement mechanic

## 11. Relationship to the engine

Everything above is realised through **token values and content only**. The
components (`src/components`) stay Garden-agnostic: a different repository can
supply a different `tokens.css` and different content and get a coherent, very
different-looking site from the same code. If a Garden need cannot be met
without new component styling, that styling must be generic and reusable, and
the Garden-specific choice must remain in tokens or content. This boundary is
enforced by the `design-system-guardian` skill.
