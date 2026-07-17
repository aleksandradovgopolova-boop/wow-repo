import { z } from 'astro/zod';

/**
 * WowRepo page-plan schema.
 *
 * A page plan describes the *meaning* and *composition* of a page. It never
 * contains HTML, CSS, or arbitrary markup. The AI (as content analyst and art
 * director) produces a plan; the codebase renders it deterministically from an
 * approved set of components. See docs/page-plan-spec.md.
 *
 * This schema is the single source of truth. It is consumed by:
 *   - src/content/config.ts  (build-time validation of every plan)
 *   - src/engine/render.ts    (resolving a plan into components)
 *   - docs / skills           (as the contract page-director must satisfy)
 */

/** Semantic page types the engine understands. Extend by adding here + a
 *  narrative default, then documenting in docs/page-plan-spec.md. */
export const PAGE_TYPES = [
  'manifesto',
  'concept-explanation',
  'principles',
  'safety-boundaries',
  'process-ritual',
  'place-environment',
  'timeline',
  'architecture-system',
] as const;
export type PageType = (typeof PAGE_TYPES)[number];

/** Approved components. A plan may only compose names in this list; the
 *  renderer maps each to a real component in src/engine/component-registry.ts.
 *  Keeping this list closed is what prevents arbitrary layout generation. */
export const COMPONENT_NAMES = [
  'editorial-hero',
  'immersive-opening',
  'manifesto-lines',
  'longform-prose',
  'principle-statement',
  'pull-quote',
  'spatial-break',
  'narrative-steps',
  'boundary-comparison',
  'quiet-callout',
  'closing-reflection',
  'next-path',
  'related-links',
] as const;
export type ComponentName = (typeof COMPONENT_NAMES)[number];

/** Emotional tones a page may carry. Free-ish but constrained to a vocabulary
 *  so the art direction stays coherent across pages. */
export const TONES = [
  'calm',
  'intimate',
  'quietly-confident',
  'warm',
  'grounded',
  'honest',
  'spacious',
  'tender',
  'clear',
  'protective',
] as const;

const densitySchema = z.enum(['low', 'medium', 'high']);
const emphasisSchema = z.enum(['low', 'medium', 'high']);

/** One composed section. `source` references a named block of resolved content
 *  for this page (see the `content` collection). Options are intentionally
 *  small and declarative — never style. */
export const sectionSchema = z.object({
  component: z.enum(COMPONENT_NAMES),
  /** Key into the page's content sources. Optional for purely structural
   *  components such as spatial-break. */
  source: z.string().optional(),
  emphasis: emphasisSchema.optional(),
  /** A named, approved variant of the component (e.g. spatial-break "ambient").
   *  Components validate their own variant values. */
  variant: z.string().optional(),
  /** Optional human-readable note explaining the art-direction intent for this
   *  section. Never rendered; kept for review + provenance. */
  intent: z.string().optional(),
});
export type PlanSection = z.infer<typeof sectionSchema>;

export const pagePlanSchema = z
  .object({
    version: z.literal(1),
    page: z.object({
      id: z.string().min(1),
      type: z.enum(PAGE_TYPES),
      purpose: z.string().min(1),
      audience: z.string().min(1),
      primary_message: z.string().min(1),
      density: densitySchema,
      tone: z.array(z.enum(TONES)).min(1),
      /** URL path the page is served at, e.g. "/" or "/principles". */
      path: z.string().startsWith('/'),
      /** Short title used in navigation and <title>. */
      title: z.string().min(1),
      /** One-line description for metadata + related-links previews. */
      summary: z.string().min(1),
      /** Ordering hint for site-level navigation. */
      order: z.number().int().nonnegative().default(0),
    }),
    /** The intended reading rhythm, named beat by beat. Guides review; the
     *  renderer does not enforce a mapping to sections 1:1. */
    narrative: z.array(z.string().min(1)).min(1),
    sections: z.array(sectionSchema).min(1),
    motion: z
      .object({
        intensity: z.enum(['none', 'subtle', 'expressive']).default('subtle'),
        purpose: z.array(z.string()).default([]),
      })
      .default({ intensity: 'subtle', purpose: [] }),
    accessibility: z
      .object({
        /** Notes the page author must honour, surfaced to reviewers. */
        notes: z.array(z.string()).default([]),
        /** If true, the page must remain fully usable with motion disabled
         *  (always true in practice; kept explicit for review). */
        motion_optional: z.boolean().default(true),
      })
      .default({ notes: [], motion_optional: true }),
    /** Relationships to other pages, by page id. Powers next-path and
     *  related-links without hard-coding navigation into content. */
    relationships: z
      .object({
        next: z.string().optional(),
        related: z.array(z.string()).default([]),
      })
      .default({ related: [] }),
  })
  .strict();

export type PagePlan = z.infer<typeof pagePlanSchema>;

/**
 * Validate an unknown value as a page plan. Returns a discriminated result so
 * callers (CLI, tests, the standalone validator) can report friendly errors.
 */
export function validatePagePlan(
  input: unknown,
): { ok: true; plan: PagePlan } | { ok: false; errors: string[] } {
  const result = pagePlanSchema.safeParse(input);
  if (result.success) return { ok: true, plan: result.data };
  const errors = result.error.issues.map(
    (issue) => `${issue.path.join('.') || '(root)'}: ${issue.message}`,
  );
  return { ok: false, errors };
}
