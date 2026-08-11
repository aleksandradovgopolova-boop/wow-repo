# WowRepo — Interface Scenario (document mode)

The single, explicit scenario for how the interface is built: what experience it
serves, how a reader moves through it, and why each screen and element is the way
it is. It exists so interface decisions follow a written rule instead of being
made ad-hoc, page by page.

It complements — does not replace — `docs/design-principles.md` (the system) and
the per-site art-direction a repository supplies through token *values*. Those say
*what the system is*; this says *how the interface is composed and why*. The model
is generic: any content repository ingested in document mode. A site supplies only
its own tokens and content; the engine composes, it does not impose a look.

---

## 0. The one job

Every site has one job, set by its own content — it is not a docs portal, a
dashboard, or a default landing page. The interface must make **the meaning of the
content felt before a single feature is explained**. Success: the reader leaves a
little more settled than they arrived, with no moment of pressure, urgency, or
manipulation.

Every decision below is measured against that job.

## 1. The scenario — a reader's journey

The interface is composed as a sequence of five beats. Each beat names what the
reader should *feel* and what the interface *does*.

1. **Threshold (the landing).** Feel: "it is calm here; nothing is rushing me."
   Do: the one living atmosphere (a generative, meaning-matched scene), the site's
   name set large, a quiet tagline, and the sections offered as doors.
2. **Orientation (always).** Feel: "I know where I am and what is next." Do: a
   permanent quiet index on the left — sections as numbered chapters, the current
   one marked, its pages revealed.
3. **Reading a chapter.** Feel: "this was made to be read, not skimmed." Do: a
   chapter-opener spread (chapter number, title, a factual meta line, a hairline),
   then one calm reading column at a comfortable measure.
4. **Onward.** Feel: "there is a path, and I choose to continue." Do: a *Назад /
   Дальше* pair at the foot of every chapter — one path, never a fork or a grid.
5. **Rest.** Feel: "I can stop whenever." Do: a quiet footer that asks for
   nothing. No CTA band, no sign-up, no urgency.

## 2. The spatial model

- **A permanent frame:** a quiet index rail (15–18rem) + one reading column.
  *One column, one path* — never a multi-column dashboard or card grid.
- **Reading measure ~62ch;** chapter headers run wider, prose stays narrow.
  Asymmetry over centring: content aligns to a reading edge.
- **Mobile:** the index moves to the top; everything below is a single vertical
  flow. No horizontal scroll anywhere (wide code/tables scroll inside themselves).

## 3. Screen anatomy — every element earns its place

### A. Threshold (landing)
- **Atmosphere** — the only "living" scene; the front door. Kept here *only* (for
  speed and focus), with a reading veil holding the reading zone legible while the
  generative scene occupies the rest of the frame.
- **Name + tagline** — set in the veiled reading zone.
- **Section index** — sections as a numbered list of doors (number · section ·
  entry title · arrow). Replaces a hand-written link list with a real index.

### B. Chapter opener (section page header)
- **Kicker `SECTION · NN / total`** — honest numbering: sections *are* a real
  sequence, so the number carries true information, not decoration.
- **Oversized serif title** — the voice.
- **Large quiet chapter numeral** — a typographic mark of place in the sequence,
  *not* an illustration. (This deliberately replaced earlier hand-drawn motifs,
  which read as decorative doodles.)
- **Meta line `Материал N из M · Обновлено …`** — orientation and freshness,
  drawn only from real structure and frontmatter; nothing invented.
- **Hairline** — the threshold into reading.

### C. Reading body
- First paragraph reads as a **lead** (larger). `h2` headings carry a small green
  mark. Measure ~62ch. Links are green with a quiet underline. Blockquotes use a
  green left rule. Code and tables **wrap or scroll within themselves** — the page
  body never scrolls sideways.

### D. Onward + footer
- **Назад / Дальше** as doors (the next chapter's title). Footer: site name +
  "Собрано с WowRepo". Nothing else.

## 4. Navigation & information architecture

- Sections are **chapters, numbered 01…N**, because they are a real route through
  the material — the number is honest wayfinding, not ornament.
- Current state is always legible: the active section is marked, its sub-pages
  shown, `NN / total` in the kicker, `Материал N из M` in the meta.

## 5. Interaction & states

- **Hover:** door rows lift slightly; the arrow slides. **Focus:** a visible ring,
  never removed. **Keyboard:** a skip link, ordered headings, logical order.
  **Mobile:** index on top, the chapter numeral smaller.

## 6. Motion

- The **only** ambient motion is the landing atmosphere (generative light);
  `prefers-reduced-motion` renders a single still frame. **Chapters have no motion
  at all** → they paint instantly (the speed lesson). No reveal-on-scroll that
  hides content behind JavaScript. No auto-playing motion at the reader.

## 7. Colour, type, space

Realised entirely through tokens (`src/styles/tokens.css`) — the interface reads
token *roles*, never raw values: a ground, reading inks at a few strengths, one
restrained accent, hairlines. A display face for voice, a humanist face for
reading, a mono face for labels/data. Each site supplies its own values for those
roles; the composition stays the same. No values are hard-coded in the engine.

## 8. Accessibility guarantees

One `h1` per page; ordered headings; visible focus; comfortable measure; fully
usable without WebGL or JavaScript; reduced-motion honoured; no manipulative
engagement mechanics (streaks, badges, urgency).

## 9. How the interface is generated (controlled generation)

The interface is **not hand-drawn per page.** It is derived from the content by
this scenario:

```
content (Markdown + wowrepo.yml)
  → structure (sections, order, frontmatter)
  → scenario (the layout rules above)
  → deterministic render (one route, static HTML)
```

`wowrepo.yml` gives the site title, language, and the ordered sections;
frontmatter gives each page's title, order, and last-updated. The engine maps
that structure onto the beats and screens above and renders static HTML — same
rules for every page. This is why the interface is repeatable and coherent
rather than decided by eye: change the content, and the site rebuilds to the
same scenario.

## 10. Interface questions, answered

- **Why a left index, not a top menu?** Constant orientation across a long route,
  and it keeps a single reading column.
- **Why the large chapter numeral?** A typographic mark of place in the sequence —
  not a picture. It replaced decorative doodles.
- **Why no imagery/atmosphere on chapters?** Readability and speed. The "living"
  element is concentrated at the front door so reading never becomes background
  noise.
- **Why this text width?** ~62ch is the comfortable measure for sustained reading.
- **Why numbering?** Sections are a real sequence; the number carries truth about
  order, it does not decorate.
- **Why these colours?** The engine holds no palette of its own — a site's tokens
  decide. The engine only forbids the generic tells listed below, whatever the
  palette.

## 11. Deliberately excluded

AI gradients, glassmorphism, glowing orbs, endless rounded cards, dashboard
grids, SaaS-landing patterns, stock illustration, decorative charts, animation
without purpose, fake-premium sheen, and any urgency/streak/badge mechanic.
