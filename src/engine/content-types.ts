/**
 * Content shapes each approved component consumes.
 *
 * A page plan's `sections[].source` names a block of content; the block must
 * match the shape the chosen component expects. These types are the contract
 * between the content-analyst (who writes content/*.yaml) and the components.
 * They are intentionally plain data — no HTML.
 */

export interface HeroContent {
  eyebrow?: string;
  title: string;
  lede?: string;
  meta?: string[];
}

export interface ImmersiveOpeningContent {
  eyebrow?: string;
  lines: string[];
  footnote?: string;
}

export interface ManifestoLinesContent {
  lines: string[];
  coda?: string;
}

export interface ProsePassage {
  heading?: string;
  paragraphs: string[];
}

export interface LongformProseContent {
  eyebrow?: string;
  passages: ProsePassage[];
}

export interface Principle {
  title: string;
  body: string;
}

export interface PrincipleStatementContent {
  eyebrow?: string;
  intro?: string;
  principles: Principle[];
}

export interface PullQuoteContent {
  quote: string;
  attribution?: string;
}

export interface SpatialBreakContent {
  word?: string;
  caption?: string;
}

export interface NarrativeStep {
  title: string;
  body: string;
}

export interface NarrativeStepsContent {
  eyebrow?: string;
  intro?: string;
  steps: NarrativeStep[];
}

export interface BoundaryColumn {
  title: string;
  items: string[];
}

export interface BoundaryComparisonContent {
  eyebrow?: string;
  intro?: string;
  affirm: BoundaryColumn; // what it IS / what it WILL do
  refuse: BoundaryColumn; // what it is NOT / will NEVER do
}

export interface QuietCalloutContent {
  text: string;
  /** 'care' uses the warm/protective accent; 'plain' is neutral. */
  tone?: 'care' | 'plain';
  label?: string;
}

export interface ClosingReflectionContent {
  line: string;
  sub?: string;
}

export interface NextPathContent {
  label?: string;
}

export interface RelatedLinksContent {
  intro?: string;
}

export interface ChapterEntry {
  /** Page id of the chapter's target; title + summary are pulled from it so
   *  content is never duplicated. Entries whose target is hidden for the
   *  current viewer are skipped. */
  id: string;
  /** Optional short word framing the entry (e.g. "Where", "How"). */
  kicker?: string;
}

export interface ChapterIndexContent {
  eyebrow?: string;
  intro?: string;
  chapters: ChapterEntry[];
}
