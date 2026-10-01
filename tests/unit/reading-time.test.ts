import { test } from 'node:test';
import assert from 'node:assert/strict';
import { estimateReadingMinutes } from '../../src/engine/markdown-repo.ts';

/**
 * Unit checks for the page reading-time estimate. Fails on the pre-change code,
 * where `estimateReadingMinutes` does not exist (import is undefined and the
 * call throws), proving the behaviour is new.
 */

const words = (n: number) => Array.from({ length: n }, () => 'word').join(' ');

test('empty body reports 0 so the label can be hidden', () => {
  assert.equal(estimateReadingMinutes(''), 0);
  assert.equal(estimateReadingMinutes('   \n  '), 0);
});

test('rounds word count to whole minutes at 200 wpm', () => {
  assert.equal(estimateReadingMinutes(words(200)), 1);
  assert.equal(estimateReadingMinutes(words(400)), 2);
  assert.equal(estimateReadingMinutes(words(500)), 3); // round(2.5) -> 3
});

test('a page with any words never reports less than one minute', () => {
  assert.equal(estimateReadingMinutes('just a few words here'), 1);
});

test('non-prose syntax is not counted as words', () => {
  const withCode = [
    'One two three four five.',
    '```js',
    'const noise = 1; // many tokens that are not prose to read aloud slowly',
    'function heavy(a, b, c) { return a + b + c; }',
    '```',
    'Six seven eight.',
  ].join('\n');
  // Prose words only: 5 + 3 = 8 -> max(1, round(8/200)) = 1.
  assert.equal(estimateReadingMinutes(withCode), 1);

  // The stripped code block must not inflate the estimate.
  const proseOnly = 'One two three four five. Six seven eight.';
  assert.equal(estimateReadingMinutes(withCode), estimateReadingMinutes(proseOnly));
});

test('links and images count only their visible label', () => {
  const linked = 'See [the guide](https://example.com/very/long/path/url) here.';
  // Words a reader reads: "See the guide here." -> 4 words -> 1 minute.
  assert.equal(estimateReadingMinutes(linked), 1);
});
