/**
 * Validate a page-plan file against the WowRepo contract — provider-neutral.
 *
 * Any model's output can be checked here, independent of who produced it:
 *
 *   npm run validate:plan -- path/to/plan.yaml
 *
 * Exit code 0 = valid, 1 = invalid (errors printed), 2 = usage error. The error
 * lines are meant to be fed straight back to a model to repair its output.
 */
import { readFileSync } from 'node:fs';
import { parse as parseYaml } from 'yaml';
import { validatePagePlan } from '../src/engine/page-plan.ts';

const file = process.argv[2];
if (!file) {
  console.error('usage: npm run validate:plan -- <file.yaml|file.json>');
  process.exit(2);
}

let raw: unknown;
try {
  raw = parseYaml(readFileSync(file, 'utf8'));
} catch (err) {
  console.error(`✗ could not read/parse ${file}: ${(err as Error).message}`);
  process.exit(1);
}

const result = validatePagePlan(raw);
if (result.ok) {
  console.log(`✓ ${file} is a valid WowRepo page plan`);
  process.exit(0);
}

console.error(`✗ ${file} is not a valid page plan:`);
for (const e of result.errors) console.error(`  - ${e}`);
process.exit(1);
