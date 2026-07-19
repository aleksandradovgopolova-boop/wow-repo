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

/**
 * An audience the repository is written for. This is the heart of WowRepo's
 * thesis: the repository is authored once, and each audience is a different
 * projection of that single source — never a separate copy.
 */
export interface Audience {
  /** Stable key used in URLs and in a page/section's `audiences` list. */
  key: string;
  /** Human label for switchers and headers. */
  label: string;
  /** Visibility levels this audience is cleared to see. */
  clearances: string[];
  /** Exactly one audience is the default; it is served at the site root. */
  default: boolean;
}

/**
 * A resolved point of view. Derived from an Audience, it is what the engine
 * projects the repository *for*. `basePath` is the URL prefix under which this
 * viewer's projection is served ('' for the default audience).
 */
export interface Viewer {
  audience: string;
  label: string;
  clearances: string[];
  basePath: string;
}

export interface SiteConfig {
  name: string;
  tagline: string;
  description: string;
  /** Locale for <html lang>. */
  lang: string;
  /** Audiences this repository is projected for. Always at least one (the
   *  default `visitor`, public) so a repo that declares none still builds. */
  audiences: Audience[];
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
  /** Present when this repo is a projection for a specific viewer. Link
   *  builders read `viewer.basePath` to keep navigation inside the projection.
   *  Absent on the raw, unprojected repo (which behaves as a single public
   *  site at the root, exactly as before). */
  viewer?: Viewer;
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
    audiences: normaliseAudiences(raw.audiences),
  };
}

/**
 * Turn the raw `audiences` from site.yaml into a valid set. A repository that
 * declares none still gets a single public `visitor` audience, so the projection
 * machinery is always active but invisible until a repo opts into more.
 * Exactly one audience is marked default (the first, if the author marked none).
 */
function normaliseAudiences(raw: unknown): Audience[] {
  const fallback: Audience = {
    key: 'visitor',
    label: 'Visitor',
    clearances: ['public'],
    default: true,
  };
  if (!Array.isArray(raw) || raw.length === 0) return [fallback];

  const audiences: Audience[] = raw.map((a, i) => {
    const item = (a ?? {}) as Partial<Audience>;
    if (!item.key) {
      throw new Error(`[wowrepo] site.yaml audiences[${i}] is missing a key`);
    }
    return {
      key: item.key,
      label: item.label ?? item.key,
      clearances:
        Array.isArray(item.clearances) && item.clearances.length
          ? item.clearances
          : ['public'],
      default: item.default === true,
    };
  });

  // Guarantee exactly one default.
  const defaults = audiences.filter((a) => a.default);
  if (defaults.length === 0) audiences[0].default = true;
  else if (defaults.length > 1) {
    throw new Error(`[wowrepo] site.yaml declares more than one default audience`);
  }
  return audiences;
}

/** The viewers a site projects for — one per declared audience. The default
 *  audience is served at the root; others under a `/<key>` prefix. */
export function viewersFor(site: SiteConfig): Viewer[] {
  return site.audiences.map((a) => ({
    audience: a.key,
    label: a.label,
    clearances: a.clearances,
    basePath: a.default ? '' : `/${a.key}`,
  }));
}

/**
 * Project a loaded repository for a single viewer — the core WowRepo operation.
 * Pages and sections the viewer is not cleared for (or not in the audience of)
 * are removed entirely, and any relationship pointing at a now-hidden page is
 * pruned so navigation never dangles. The result is a normal ResolvedRepo that
 * happens to know which viewer it is for.
 */
export function resolveForViewer(
  repo: ResolvedRepo,
  viewer: Viewer,
): ResolvedRepo {
  const canSee = (
    visibility: string | undefined,
    audiences: string[] | undefined,
  ): boolean =>
    viewer.clearances.includes(visibility ?? 'public') &&
    (audiences === undefined || audiences.includes(viewer.audience));

  const visible = repo.pages
    .filter((p) => canSee(p.plan.page.visibility, p.plan.page.audiences))
    .map((p) => ({
      ...p,
      plan: {
        ...p.plan,
        sections: p.plan.sections.filter((s) =>
          canSee(s.visibility, s.audiences),
        ),
      },
    }));

  const visibleIds = new Set(visible.map((p) => p.plan.page.id));

  const pages: ResolvedPage[] = visible.map((p) => {
    const rel = p.plan.relationships;
    return {
      ...p,
      plan: {
        ...p.plan,
        relationships: {
          ...rel,
          next: rel.next && visibleIds.has(rel.next) ? rel.next : undefined,
          related: rel.related.filter((id) => visibleIds.has(id)),
        },
      },
    };
  });

  return { site: repo.site, pages, viewer };
}

/** Build an href for a page path inside the current projection. On the raw repo
 *  (no viewer) this is the path unchanged; under a projection it is prefixed
 *  with the viewer's basePath so links stay within the audience. */
export function pageHref(repo: ResolvedRepo, path: string): string {
  const base = repo.viewer?.basePath ?? '';
  if (!base) return path;
  return path === '/' ? base : base + path;
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
