---
name: page-director
description: >-
  Turn a content analysis into a validated page plan — narrative rhythm,
  approved component selection, source references, beginning and ending — without
  generating any HTML. Run once per page.
---

# page-director

This skill is the **Claude Code binding** of WowRepo pipeline stage 2. The
pipeline is provider-neutral, so the full specification lives once, outside this
tool, and every binding defers to it:

> **Follow [`pipeline/stages/02-page-director.md`](../../../pipeline/stages/02-page-director.md) exactly.**

That document is the single source of truth for the contract you must satisfy
(`pipeline/schema/page-plan.schema.json`), how to direct a page, projection
(audience-aware output), the hard rules, and the definition of done. Do not
restate or diverge from it here.

Before finishing, validate your plan and repair any errors:

```
npm run validate:plan -- your-plan.yaml
```

See [`pipeline/README.md`](../../../pipeline/README.md) for how the stages fit
together and how the same pipeline runs on other models (Kimi, OpenAI, …).
