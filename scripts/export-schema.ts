/**
 * Export the page-plan contract as a standalone JSON Schema.
 *
 * The Zod schema in src/engine/page-plan.ts is the single source of truth. This
 * script derives a machine-readable JSON Schema from it so that ANY model or
 * tool — Claude, Kimi, OpenAI, a human — can be handed the exact contract a
 * valid page plan must satisfy. Re-run whenever the Zod schema changes:
 *
 *   npm run schema:export
 */
import { writeFileSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { zodToJsonSchema } from 'zod-to-json-schema';
import { pagePlanSchema } from '../src/engine/page-plan.ts';

const here = dirname(fileURLToPath(import.meta.url));
const out = join(here, '..', 'pipeline', 'schema', 'page-plan.schema.json');

const schema = zodToJsonSchema(pagePlanSchema, {
  name: 'WowRepoPagePlan',
  $refStrategy: 'none',
});

mkdirSync(dirname(out), { recursive: true });
writeFileSync(out, JSON.stringify(schema, null, 2) + '\n');
console.log(`✓ wrote ${out}`);
