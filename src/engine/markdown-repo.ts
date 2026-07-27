import { readFileSync, readdirSync, existsSync, statSync } from 'node:fs';
import { join, basename, extname, posix } from 'node:path';
import { parse as parseYaml } from 'yaml';
import { marked } from 'marked';

/**
 * Markdown ingestion — the bridge that lets WowRepo render an ordinary content
 * repository (Markdown + a `wowrepo.yml`) in the WowRepo design, without any
 * hand-authored page plans.
 *
 * This is "document mode": every Markdown file under the content root becomes a
 * page, rendered as calm editorial prose inside the WowRepo visual system, with
 * a reading path built from `wowrepo.yml` navigation + each file's frontmatter
 * order. It complements the authored "composition mode" (page plans); both share
 * the same tokens, layout, and chrome.
 *
 * Point the engine at such a repo with WOWREPO_MODE=markdown and WOWREPO_ROOT
 * set to the directory that holds `wowrepo.yml` (e.g. a repo's `public/`).
 */

export interface MdSiteConfig {
  title: string;
  description: string;
  lang: string;
}

export interface MdNavItem {
  label: string;
  path: string; // section directory, relative to content root
}

export interface MdPage {
  /** URL path, e.g. "/" or "/01-introduction/what-is-garden". */
  path: string;
  title: string;
  /** Human section label this page belongs to (from nav), if any. */
  section?: string;
  order: number;
  /** Rendered HTML of the Markdown body (frontmatter stripped). */
  bodyHtml: string;
  visibility: 'public' | 'internal';
  /** Repo-relative source file, for a "view source" affordance. */
  source: string;
  updated?: string;
}

export interface MdRepo {
  site: MdSiteConfig;
  nav: { label: string; path: string }[]; // resolved to page paths for the top nav
  pages: MdPage[];
}

interface Frontmatter {
  title?: string;
  updated?: string;
  wowrepo?: { order?: number; visibility?: 'public' | 'internal'; entry?: boolean };
  [k: string]: unknown;
}

/** Split a Markdown file into YAML frontmatter (if any) and the body. */
function splitFrontmatter(raw: string): { data: Frontmatter; body: string } {
  if (raw.startsWith('---')) {
    const end = raw.indexOf('\n---', 3);
    if (end !== -1) {
      const fmText = raw.slice(3, end).trim();
      const body = raw.slice(end + 4).replace(/^\s*\n/, '');
      const data = (parseYaml(fmText) ?? {}) as Frontmatter;
      return { data, body };
    }
  }
  return { data: {}, body: raw };
}

/** First H1 in the body, used as a title fallback when frontmatter has none. */
function firstHeading(body: string): string | undefined {
  const m = body.match(/^#\s+(.+)$/m);
  return m ? m[1].trim() : undefined;
}

/** Turn "what-is-garden" into "what-is-garden" (kept as slug). */
function slugOf(file: string): string {
  return basename(file, extname(file));
}

/**
 * Map a repo-relative Markdown path (no extension) to its page URL. The page
 * tree mirrors the content directory tree, so this is a direct translation —
 * except the root README, which is the landing at "/".
 */
function mdPathToUrl(repoRelNoExt: string): string {
  if (repoRelNoExt === 'README' || repoRelNoExt === 'index' || repoRelNoExt === '') {
    return '/';
  }
  return `/${repoRelNoExt}`;
}

/**
 * Rewrite intra-repo links that point at source `.md` files into the real page
 * URLs (with the site base path). Markdown authored for a plain file tree links
 * doc-to-doc as `../section/page.md`; on the built site those must become
 * `/base/section/page`. External links, anchors, and non-Markdown targets are
 * left untouched. `currentDir` is the linking page's directory relative to the
 * content root (""/root for the homepage).
 */
function rewriteMdLinks(html: string, currentDir: string, base: string): string {
  return html.replace(/href="([^"]+)"/g, (whole, url: string) => {
    if (/^(?:[a-z]+:|\/\/|#|\/)/i.test(url)) return whole; // external / anchor / already-absolute
    const hashAt = url.indexOf('#');
    const pathPart = hashAt === -1 ? url : url.slice(0, hashAt);
    const hash = hashAt === -1 ? '' : url.slice(hashAt);
    if (!/\.md$/i.test(pathPart)) return whole;
    const repoRel = posix.normalize(posix.join(currentDir, pathPart)).replace(/\.md$/i, '');
    const target = `${base}${mdPathToUrl(repoRel)}`;
    return `href="${target}${hash}"`;
  });
}

/** The linking page's directory relative to the content root, "" at the root. */
function dirOfUrl(urlPath: string): string {
  if (urlPath === '/') return '';
  return urlPath.slice(1).split('/').slice(0, -1).join('/');
}

function listMarkdown(dir: string): string[] {
  if (!existsSync(dir) || !statSync(dir).isDirectory()) return [];
  return readdirSync(dir)
    .filter((f) => f.endsWith('.md'))
    .sort();
}

/**
 * Load a Markdown content repository. `contentRoot` is the directory that holds
 * `wowrepo.yml` (and the numbered section directories it navigates).
 */
export function loadMarkdownRepo(contentRoot: string): MdRepo {
  const cfgPath = join(contentRoot, 'wowrepo.yml');
  if (!existsSync(cfgPath)) {
    throw new Error(`[wowrepo] markdown mode: no wowrepo.yml in ${contentRoot}`);
  }
  const cfg = parseYaml(readFileSync(cfgPath, 'utf8')) as {
    site?: Record<string, unknown>;
    navigation?: MdNavItem[];
  };
  const site: MdSiteConfig = {
    title: (cfg.site?.title as string) ?? 'Site',
    description: (cfg.site?.description as string) ?? '',
    lang: (cfg.site?.language as string) ?? 'en',
  };
  const navigation = Array.isArray(cfg.navigation) ? cfg.navigation : [];
  // Base path for the deployment (e.g. "/garden" for a GitHub Pages project
  // site). Body links to other docs are rewritten to include it; must match
  // the base Astro builds with (see astro.config.mjs).
  const base = (process.env.WOWREPO_BASE ?? '').replace(/\/$/, '');

  const pages: MdPage[] = [];

  const render = (file: string, section: string | undefined, urlPath: string): MdPage => {
    const raw = readFileSync(file, 'utf8');
    const { data, body } = splitFrontmatter(raw);
    const title = data.title ?? firstHeading(body) ?? slugOf(file);
    // Drop a leading H1 that repeats the title — the page renders its own header.
    const bodyNoTitle = body.replace(/^#\s+.+\n+/, '');
    const rendered = marked.parse(bodyNoTitle, { async: false }) as string;
    return {
      path: urlPath,
      title,
      section,
      order: data.wowrepo?.order ?? 999,
      bodyHtml: rewriteMdLinks(rendered, dirOfUrl(urlPath), base),
      visibility: data.wowrepo?.visibility ?? 'public',
      source: file.replace(contentRoot, '').replace(/^\//, ''),
      updated: data.updated,
    };
  };

  // Homepage (README.md at the content root) becomes the landing at "/".
  const readme = join(contentRoot, 'README.md');
  if (existsSync(readme)) {
    pages.push({ ...render(readme, undefined, '/'), order: -1 });
  }

  // Each navigation section → its Markdown files, ordered by frontmatter.
  for (const navItem of navigation) {
    const dir = join(contentRoot, navItem.path);
    const files = listMarkdown(dir);
    const sectionPages = files
      .map((f) => render(join(dir, f), navItem.label, `/${navItem.path}/${slugOf(f)}`))
      .sort((a, b) => a.order - b.order);
    pages.push(...sectionPages);
  }

  // Top nav points at the first page of each section (its natural entry).
  const nav = navigation
    .map((navItem) => {
      const first = pages.find((p) => p.section === navItem.label);
      return first ? { label: navItem.label, path: first.path } : null;
    })
    .filter((x): x is { label: string; path: string } => x !== null);

  return { site, nav, pages };
}

/** Reading order across the whole site (README first, then section by section)
 *  — used to offer a "continue reading" step at the foot of each page. */
export function readingNeighbours(
  repo: MdRepo,
  path: string,
): { prev?: MdPage; next?: MdPage } {
  const i = repo.pages.findIndex((p) => p.path === path);
  if (i === -1) return {};
  return { prev: repo.pages[i - 1], next: repo.pages[i + 1] };
}
