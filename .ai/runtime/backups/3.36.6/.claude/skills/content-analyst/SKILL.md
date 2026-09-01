---
name: content-analyst
description: >-
  Read repository source content and produce a structured analysis of a page's
  purpose, audience, message, information types, and what belongs as text vs.
  visual — without inventing facts. Use before page-director plans a page.
---

# content-analyst

This skill is the **Claude Code binding** of WowRepo pipeline stage 1. The
pipeline is provider-neutral, so the full specification lives once, outside this
tool, and every binding defers to it:

> **Follow [`pipeline/stages/01-content-analyst.md`](../../../pipeline/stages/01-content-analyst.md) exactly.**

That document is the single source of truth for inputs, the absolute
"never invent facts" rule, the analysis fields to produce, and the definition of
done. Do not restate or diverge from it here.

See [`pipeline/README.md`](../../../pipeline/README.md) for how the stages fit
together and how the same pipeline runs on other models (Kimi, OpenAI, …).
