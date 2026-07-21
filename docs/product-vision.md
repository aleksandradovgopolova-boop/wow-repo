# WowRepo — Product Vision

## What it is

WowRepo is an open-source engine that turns a repository into a beautiful,
living website — where **every page is designed around the meaning of its
content**, and the site rebuilds automatically as the repository changes.

It is deliberately *not* a documentation theme that renders Markdown into a
fixed template. Two pages with different purposes should not look the same just
because they share a stylesheet.

## What it does

1. **Reads and understands** repository content.
2. **Determines the purpose and structure** of each page.
3. **Sets an individual direction** for each page from the meaning of its
   content.
4. **Renders** every page through one coherent visual system.
5. **Keeps pages distinct** without making the site inconsistent.
6. **Rebuilds automatically** when the repository content changes.

## The core principle: controlled generation

The model does not write HTML/CSS. It acts as a **content analyst and art
director**; the codebase stays responsible for accessibility, responsiveness,
design tokens, stable component behaviour, layout constraints, performance, and
deterministic rendering.

```
repository content
  → content analysis      (what does this page mean?)
  → page plan             (meaning + composition, validated, no HTML)
  → component composition (only approved components + atmospheres)
  → deterministic render  (the codebase turns the plan into pixels)
  → visual QA             (browser checks, screenshots, anti-slop review)
```

## The hypothesis under test

> AI can understand the meaning of repository content and compose different,
> high-quality pages from a controlled visual language, while keeping the whole
> website coherent.

The MVP does not try to support every repository. It proves the approach on one
demanding example — **Garden** — while keeping the reusable engine cleanly
separated from Garden-specific decisions.

## Garden — the first example

Garden is a personal digital space supporting rituals, meaningful places, lived
experiences, the recovery of attention, reflection, and a careful relationship
with one's own life. It is **not** a second brain, knowledge graph, note-taking
app, PKM tool, or information-organisation product.

Garden must feel calm, expressive, alive, human, and emotionally intelligent —
and must preserve user autonomy absolutely (never diagnose, pressure, create
dependency, use shame/fear/urgency, present assumptions as facts, decide for the
user, or use manipulative engagement mechanics).

Garden is the first *test case*, not the product. The architecture must never
become a Garden-specific website template.

## Long-term uses

Product sites, documentation, knowledge bases, research reports, educational
projects, company handbooks, portfolios, open-source projects, internal portals,
and interactive longreads — each getting pages composed around their own
meaning, from one controlled visual language per site.

## Non-goals (for this MVP)

No SaaS dashboard, auth, billing, visual editor, GitHub App, org accounts,
multi-tenant infra, CMS, arbitrary-repository support, runtime page generation,
autonomous publishing of AI copy, theme marketplace, huge component library, or
CLI distribution. The MVP validates the hypothesis and nothing more.
