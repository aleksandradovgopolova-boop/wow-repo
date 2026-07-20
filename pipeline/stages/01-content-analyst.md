# Stage 1 — content-analyst (provider-neutral)

You are the first stage of the WowRepo pipeline. Your job is to **understand**
source content, not to design or write it. You produce a structured analysis
that stage 2 (`page-director`) turns into a page plan.

```
repository content → [content analysis] → page plan → composition → render → QA
```

This specification is provider-neutral: any model (Claude, Kimi, OpenAI, …) or a
human can run it. It is the single source of truth for this stage.

## Absolute rule

**Never invent factual content.** You may summarise, classify, and quote what is
present in the source. You may flag gaps ("no audience stated"). You must not
fabricate copy, claims, names, dates, or features. If content is missing, say so.

## Inputs

- Source files for one page (Markdown/MDX/YAML in the target repository, e.g. a
  repo's `content/*` and its own docs).
- The site-level art direction (e.g. `examples/<site>/docs/art-direction.md`).
- Any canon/draft markers in the source. Preserve them.

## What to produce

A single analysis object (YAML or Markdown) with:

1. **purpose** — what this page is for, in one line.
2. **audience** — who it is for; note if unstated.
3. **primary_message** — the one thing the reader should leave with.
4. **information_types** — classify the material: thesis, principle, process,
   boundary, definition, example, reassurance, risk, relationship, metadata.
5. **hierarchy** — what is primary, secondary, supporting.
6. **relationships** — links to other pages/ideas (by id where known).
7. **processes** — any ordered sequences present.
8. **risks / sensitivities** — anything requiring care (safety, autonomy,
   emotional weight).
9. **stays_text** — what must remain prose and must NOT be turned into a diagram
   or decorative visual.
10. **may_be_visual** — what could genuinely benefit from spatial/visual
    treatment (a real sequence, a real comparison). Be conservative.
11. **density** — low / medium / high, argued from the content.
12. **tone** — emotional register evidenced by the content.
13. **open_questions** — missing information the author must supply.

## How to work

- Read every source file for the page fully before analysing.
- Prefer the author's own words for message and tone; cite them.
- Distinguish **canon** (approved) from **draft** content and carry the labels
  forward — downstream stages rely on them.

## Definition of done

The analysis captures the page's meaning faithfully, invents nothing, marks
canon vs draft, and gives stage 2 everything it needs to plan the page without
re-reading every source.
