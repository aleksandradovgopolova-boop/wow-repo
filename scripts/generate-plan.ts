/**
 * WowRepo runner — generate a page plan with any model, provider-neutrally.
 *
 * It runs the pipeline's schema-enforced path (stage 1 content-analyst → stage 2
 * page-director) against a chosen provider, then runs the validate-and-repair
 * loop against the same contract the engine uses. The model is a swappable part;
 * Moonshot (Kimi) is the default.
 *
 *   export MOONSHOT_API_KEY=sk-...
 *   npm run generate:plan -- \
 *     --content examples/garden/content/place.yaml \
 *     --site examples/garden \
 *     --out /tmp/place.plan.yaml
 *
 * Flags: --provider moonshot|openai|mock (default moonshot), --max-repairs N
 * (default 3), --out <path> (omit to print). Use --provider mock to exercise the
 * loop with no key or network.
 */
import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parse as parseYaml, stringify as toYaml } from 'yaml';
import { validatePagePlan } from '../src/engine/page-plan.ts';
import { selectProvider, type ChatMessage } from './providers.ts';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');

function arg(flag: string, fallback?: string): string | undefined {
  const i = process.argv.indexOf(flag);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const contentPath = arg('--content');
const siteDir = arg('--site');
const outPath = arg('--out');
const providerName = arg('--provider', 'moonshot') as string;
const maxRepairs = Number(arg('--max-repairs', '3'));

if (!contentPath || !siteDir) {
  console.error(
    'usage: npm run generate:plan -- --content <file.yaml> --site <dir> ' +
      '[--out <file.yaml>] [--provider moonshot|openai|mock] [--max-repairs N]',
  );
  process.exit(2);
}

const read = (p: string): string => readFileSync(p, 'utf8');
const readIf = (p: string): string => (existsSync(p) ? read(p) : '');

const stage1 = read(join(root, 'pipeline/stages/01-content-analyst.md'));
const stage2 = read(join(root, 'pipeline/stages/02-page-director.md'));
const schema = read(join(root, 'pipeline/schema/page-plan.schema.json'));

const content = read(contentPath);
const site = readIf(join(siteDir, 'site.yaml'));
const artDirection = readIf(join(siteDir, 'docs/art-direction.md'));

const inputs =
  `## Site\n${site}\n\n## Art direction\n${artDirection}\n\n` +
  `## Page content sources (reference these by their source key)\n${content}`;

/** Pull a JSON object out of a model reply, tolerating ``` fences and prose. */
function extractJson(text: string): unknown {
  const fenced = text.match(/```(?:json)?\s*([\s\S]*?)```/i);
  const body = fenced ? fenced[1] : text;
  const start = body.indexOf('{');
  const end = body.lastIndexOf('}');
  if (start < 0 || end < 0) throw new Error('no JSON object found in reply');
  return JSON.parse(body.slice(start, end + 1));
}

async function main(): Promise<void> {
  const provider = selectProvider(providerName);
  console.error(`▶ provider: ${provider.name}`);

  // Stage 1 — content-analyst.
  console.error('▶ stage 1: content-analyst …');
  const analysis = await provider.chat([
    { role: 'system', content: stage1 },
    { role: 'user', content: `${inputs}\n\nProduce the analysis.` },
  ]);

  // Stage 2 — page-director, with a validate-and-repair loop.
  console.error('▶ stage 2: page-director …');
  const messages: ChatMessage[] = [
    {
      role: 'system',
      content:
        `${stage2}\n\n---\n\nReturn ONLY a single JSON object that validates ` +
        `against this JSON Schema:\n\n${schema}`,
    },
    {
      role: 'user',
      content:
        `## Analysis (from stage 1)\n${analysis}\n\n${inputs}\n\n` +
        `Return the page plan as a single JSON object.`,
    },
  ];

  for (let attempt = 1; attempt <= maxRepairs + 1; attempt++) {
    const reply = await provider.chat(messages, { json: true });
    let parsed: unknown;
    try {
      parsed = extractJson(reply);
    } catch (err) {
      console.error(`  attempt ${attempt}: unparseable — ${(err as Error).message}`);
      messages.push({ role: 'assistant', content: reply });
      messages.push({
        role: 'user',
        content: `That was not valid JSON (${(err as Error).message}). Return ONLY the JSON object.`,
      });
      continue;
    }

    const result = validatePagePlan(parsed);
    if (result.ok) {
      const yaml = toYaml(parsed);
      // Sanity: what we write must itself re-validate.
      if (!validatePagePlan(parseYaml(yaml)).ok) {
        throw new Error('internal: serialised plan failed re-validation');
      }
      if (outPath) {
        mkdirSync(dirname(outPath), { recursive: true });
        writeFileSync(outPath, yaml);
        console.error(`✓ valid plan written to ${outPath} (attempt ${attempt})`);
      } else {
        console.error(`✓ valid plan (attempt ${attempt}):\n`);
        process.stdout.write(yaml);
      }
      return;
    }

    console.error(`  attempt ${attempt}: invalid —`);
    for (const e of result.errors) console.error(`    - ${e}`);
    messages.push({ role: 'assistant', content: reply });
    messages.push({
      role: 'user',
      content:
        `The plan failed validation with these errors:\n` +
        result.errors.map((e) => `- ${e}`).join('\n') +
        `\nFix them and return ONLY the corrected JSON object.`,
    });
  }

  console.error(`✗ could not produce a valid plan after ${maxRepairs} repairs`);
  process.exit(1);
}

main().catch((err) => {
  console.error(`✗ ${(err as Error).message}`);
  process.exit(1);
});
