import { test, expect, type Page, type ConsoleMessage } from '@playwright/test';

/**
 * Browser + visual checks for the four Garden pages.
 *
 * For every page and every viewport we assert:
 *   - the page loads (200 + <main> present);
 *   - critical content (the page's heading) is visible;
 *   - there is no horizontal overflow;
 *   - no console errors were logged.
 * Navigation is checked once. Screenshots for all pages are written on the
 * desktop and mobile projects (see the screenshot tests at the bottom).
 */

const PAGES = [
  { slug: '/', name: 'manifesto', title: 'Manifesto' },
  { slug: '/what-garden-is', name: 'what-garden-is', title: 'What Garden is' },
  { slug: '/principles', name: 'principles', title: 'Principles' },
  { slug: '/safety', name: 'safety', title: 'Safety & autonomy' },
] as const;

function trackConsoleErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on('console', (msg: ConsoleMessage) => {
    if (msg.type() === 'error') errors.push(msg.text());
  });
  page.on('pageerror', (err) => errors.push(err.message));
  return errors;
}

async function hasHorizontalOverflow(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const doc = document.documentElement;
    // Allow a 1px rounding tolerance.
    return doc.scrollWidth > doc.clientWidth + 1;
  });
}

for (const p of PAGES) {
  test(`${p.name}: loads, shows content, no overflow, no console errors`, async ({
    page,
  }) => {
    const errors = trackConsoleErrors(page);

    const response = await page.goto(p.slug, { waitUntil: 'networkidle' });
    expect(response?.ok(), 'page responds with 2xx').toBeTruthy();

    // Critical content: the site chrome and a visible page heading.
    await expect(page.locator('main')).toBeVisible();
    await expect(
      page.getByRole('heading', { level: 1 }).first(),
    ).toBeVisible();

    expect(await hasHorizontalOverflow(page), 'no horizontal overflow').toBe(
      false,
    );

    expect(errors, `no console errors on ${p.name}`).toEqual([]);
  });
}

test('navigation: header links reach every page and mark the current one', async ({
  page,
}) => {
  await page.goto('/');
  const nav = page.getByRole('navigation', { name: 'Reading path' });
  await expect(nav).toBeVisible();

  // Follow the nav link to each non-home page and confirm arrival.
  for (const p of PAGES.filter((x) => x.slug !== '/')) {
    await page.goto('/');
    await nav.getByRole('link', { name: p.title }).click();
    await expect(page).toHaveURL(new RegExp(`${p.slug}/?$`));
    await expect(
      page.getByRole('heading', { level: 1 }).first(),
    ).toBeVisible();
    // The current page is marked for assistive tech.
    await expect(
      page
        .getByRole('navigation', { name: 'Reading path' })
        .getByRole('link', { name: p.title }),
    ).toHaveAttribute('aria-current', 'page');
  }
});

test('reading path: manifesto offers an onward step', async ({ page }) => {
  await page.goto('/');
  const next = page.getByRole('navigation', { name: 'Continue reading' });
  await expect(next).toBeVisible();
  await next.getByRole('link').click();
  await expect(page).toHaveURL(/\/what-garden-is\/?$/);
});

// --- Screenshots: full-page captures for every Garden page. ---
// Run on the desktop and mobile projects so we get both layouts.
for (const p of PAGES) {
  test(`screenshot: ${p.name}`, async ({ page }, testInfo) => {
    const project = testInfo.project.name;
    if (project !== 'desktop' && project !== 'mobile') test.skip();

    // Reduced motion makes every [data-reveal] element show immediately, so a
    // full-page capture is stable rather than half-faded-in.
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto(p.slug, { waitUntil: 'networkidle' });
    await page.waitForTimeout(200);
    await page.screenshot({
      path: `tests/visual/screenshots/${p.name}-${project}.png`,
      fullPage: true,
    });
  });
}
