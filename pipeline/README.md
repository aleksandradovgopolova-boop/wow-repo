# WowRepo pipeline — provider-neutral

WowRepo turns a repository's content into a designed site. The design work is
done by a **model**, but WowRepo is deliberately **model-agnostic**: Claude,
Kimi, OpenAI, Gemini, or a human can run the pipeline. Nothing here assumes a
specific provider.

## The one principle

**The schema is the source of truth. The model is a swappable part.**

A generated page is only ever accepted if it validates against the contract in
[`schema/page-plan.schema.json`](schema/page-plan.schema.json) (derived from the
Zod schema in `src/engine/page-plan.ts`). The engine never knows or cares which
model produced a plan — only whether it is valid. That is what makes "bring your
own model" true rather than aspirational.

## The pipeline

```
repository content
      │
      ▼
[1] content-analyst   → understand the content (no design)      stages/01-content-analyst.md
      │
      ▼
[2] page-director     → a validated page plan (no HTML)         stages/02-page-director.md
      │
      ▼
   engine renders the plan deterministically from approved components
```

Run once per site, rarely:

```
[3] site-art-director → the whole-site visual language          stages/03-site-art-director.md
```

Only **stage 2's output is schema-enforced** (it produces the page plan the
engine renders). Stages 1 and 3 produce structured prose consumed by later
stages and by humans.

## Running a stage with ANY model

Each stage doc under [`stages/`](stages/) is written as a complete,
provider-neutral instruction. To run a stage with any model, assemble a prompt:

1. **System / instruction** = the full text of the stage doc.
2. **Contract** (stage 2 only) = the contents of `schema/page-plan.schema.json`.
   Ask the model to return output that validates against it. Providers with a
   JSON/structured-output or tool-calling mode should be pinned to this schema;
   plain models are simply told to satisfy it.
3. **Inputs** = the source content for the page (and the site's art-direction
   doc). The stage doc lists exactly what each stage consumes.

The model returns YAML (or JSON) for the page plan. Then close the loop:

```
npm run validate:plan -- path/to/plan.yaml
```

- exit 0 → the plan is valid; drop it into the repo's `page-plans/`.
- exit 1 → the printed errors are written to be fed **straight back to the
  model** to repair its output. Repeat until it passes.

This validate-and-repair loop is identical for every provider — it is where
model-independence actually lives.

## Regenerating the contract

The JSON Schema is generated, never hand-edited:

```
npm run schema:export
```

Re-run it whenever `src/engine/page-plan.ts` changes so the neutral contract
stays in lockstep with the code.

## Relationship to the Claude skills

The Claude Code skills in `.claude/skills/` are one **binding** of this pipeline
for one tool. They defer to these stage docs so there is a single source of
truth. A binding for another tool (an OpenAI/Kimi runner, a CLI) would do the
same: point at `stages/` + `schema/`, then run the validate-and-repair loop.
