import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parse as parseYaml } from 'yaml';
import { validatePagePlan, type PagePlan } from './page-plan.ts';

/**
 * The engine ingests a *repository* and turns it into resolved pages. A
 * "repository" is any directory laid out like `examples/garden`:
 *
 *   <repo>/
 *     site.yaml           # site-level identity + art-direction summary
 *     page-plans/*.yaml   # one validated page plan per page
 *     content/*.yaml      # named content sources referenced by plans
 *
 * Nothing here is Garden-specific. Garden is simply the first repository we
 * point the engine at (see examples/garden). All reads happen at build time;
 * the shipped site is fully static.
 */

export interface SiteConfig {
  name: string;
  tagline: string;
  description: string;
  /** Locale for <html lang>. */
  lang: string;
}

/** Content sources are intentionally loosely typed: each component knows the
 *  shape of the source it consumes. Validation of *composition* happens on the
 *  plan; validation of *content* is the component's defensive responsibility. */
export type ContentSources = Record<string, unknown>;

export interface ResolvedPage {
  plan: PagePlan;
  content: ContentSources;
}

export interface ResolvedRepo {
  site: SiteConfig;
  pages: ResolvedPage[];
}

/** Absolute path to the default example repository (Garden). */
export const GARDEN_REPO = fileURLToPath(
  new URL('../../examples/garden', import.meta.url),
);

function readYaml<T>(path: string): T {
  return parseYaml(readFileSync(path, 'utf8')) as T;
}

function loadSite(repoRoot: string): SiteConfig {
  const path = join(repoRoot, 'site.yaml');
  if (!existsSync(path)) {
    throw new Error(`[wowrepo] missing site.yaml in repository: ${repoRoot}`);
  }
  const raw = readYaml<Partial<SiteConfig>>(path);
  if (!raw.name || !raw.tagline) {
    throw new Error(`[wowrepo] site.yaml must define at least name + tagline`);
  }
  return {
    name: raw.name,
    tagline: raw.tagline,
    description: raw.description ?? raw.tagline,
    lang: raw.lang ?? 'en',
  };
}

/**
 * Load, validate, and resolve every page in a repository.
 *
 * Each plan is validated against the page-plan schema. A plan that fails
 * validation aborts the build with a readable error — we never render an
 * unvalidated plan. Content sources are attached but not schema-checked here.
 */
export function loadRepo(repoRoot: string = GARDEN_REPO): ResolvedRepo {
  const site = loadSite(repoRoot);
  const plansDir = join(repoRoot, 'page-plans');
  const contentDir = join(repoRoot, 'content');

  if (!existsSync(plansDir)) {
    throw new Error(`[wowrepo] no page-plans/ directory in ${repoRoot}`);
  }

  const planFiles = readdirSync(plansDir)
    .filter((f) => f.endsWith('.yaml') || f.endsWith('.yml'))
    .sort();

  const pages: ResolvedPage[] = planFiles.map((file) => {
    const raw = readYaml<unknown>(join(plansDir, file));
    const result = validatePagePlan(raw);
    if (!result.ok) {
      throw new Error(
        `[wowrepo] invalid page plan "${file}":\n  - ${result.errors.join('\n  - ')}`,
      );
    }
    const plan = result.plan;

    // Resolve the plan's content sources from content/<id>.yaml (if present).
    const contentPath = join(contentDir, `${plan.page.id}.yaml`);
    const content: ContentSources = existsSync(contentPath)
      ? readYaml<ContentSources>(contentPath)
      : {};

    return { plan, content };
  });

  // Stable, intentional ordering for navigation.
  pages.sort((a, b) => a.plan.page.order - b.plan.page.order);

  const ids = new Set(pages.map((p) => p.plan.page.id));
  for (const { plan } of pages) {
    const rel = plan.relationships;
    for (const ref of [rel.next, ...rel.related].filter(Boolean) as string[]) {
      if (!ids.has(ref)) {
        throw new Error(
          `[wowrepo] page "${plan.page.id}" references unknown page "${ref}"`,
        );
      }
    }
  }

  return { site, pages };
}

/** Look up a resolved page by id (used to render next-path / related-links). */
export function findPage(repo: ResolvedRepo, id: string): ResolvedPage | undefined {
  return repo.pages.find((p) => p.plan.page.id === id);
}
