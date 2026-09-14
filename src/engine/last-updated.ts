import { execFileSync } from 'node:child_process';

/**
 * When the ingested repository was last updated — resolved *deterministically*
 * at build time, never from the reader's clock.
 *
 * WowRepo sites are static and rebuilt when their content changes, so "last
 * updated" is a property of the content, not of the moment a page is viewed.
 * We therefore never call `new Date()` at render time. The value is derived
 * from a fixed input (the same commit always yields the same date):
 *
 *   1. `SOURCE_DATE_EPOCH` — the reproducible-builds standard. If a build
 *      environment pins this, we honour it.
 *   2. otherwise, the committer date of the last commit that touched the
 *      repository's content (`git log -1` scoped to the repo root).
 *
 * If neither source is available (e.g. building from an exported tarball with
 * no git history), we return `undefined` and the footer simply omits the line
 * rather than inventing a nondeterministic timestamp.
 */
export interface LastUpdated {
  /** Machine-readable date for the <time> element: `YYYY-MM-DD` (UTC). */
  iso: string;
  /** Human-readable, deterministic English date, e.g. `21 July 2026`. */
  display: string;
}

const MONTHS = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
] as const;

/** Format a fixed epoch (seconds) as a UTC date. Uses UTC getters so the
 *  result never depends on the build machine's timezone. */
function formatEpoch(epochSeconds: number): LastUpdated {
  const d = new Date(epochSeconds * 1000);
  const year = d.getUTCFullYear();
  const month = d.getUTCMonth(); // 0-based
  const day = d.getUTCDate();
  const iso = `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
  return { iso, display: `${day} ${MONTHS[month]} ${year}` };
}

/** Read SOURCE_DATE_EPOCH if it is set to a valid positive integer. */
function fromSourceDateEpoch(): number | undefined {
  const raw = process.env.SOURCE_DATE_EPOCH;
  if (!raw) return undefined;
  const epoch = Number.parseInt(raw, 10);
  return Number.isFinite(epoch) && epoch > 0 ? epoch : undefined;
}

/** Committer epoch of the last commit that touched `repoRoot`, or undefined
 *  when git or history is unavailable. */
function fromGit(repoRoot: string): number | undefined {
  try {
    const out = execFileSync(
      'git',
      ['-C', repoRoot, 'log', '-1', '--format=%ct', '--', '.'],
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] },
    ).trim();
    if (!out) return undefined;
    const epoch = Number.parseInt(out, 10);
    return Number.isFinite(epoch) && epoch > 0 ? epoch : undefined;
  } catch {
    return undefined;
  }
}

/**
 * Resolve the deterministic "last updated" date for a repository, or
 * `undefined` when no deterministic source is available.
 */
export function resolveLastUpdated(repoRoot: string): LastUpdated | undefined {
  const epoch = fromSourceDateEpoch() ?? fromGit(repoRoot);
  return epoch === undefined ? undefined : formatEpoch(epoch);
}
