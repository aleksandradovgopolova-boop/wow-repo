/**
 * Purposeful reveal-on-scroll. Elements marked [data-reveal] fade and rise a
 * little as they enter the viewport — used to give the page a sense of calm
 * unfolding, never as decoration.
 *
 * Autonomy first: if the visitor prefers reduced motion, we do nothing and
 * everything is visible immediately. There is no auto-playing motion anywhere.
 */
function initReveal(): void {
  const prefersReduced = window.matchMedia(
    '(prefers-reduced-motion: reduce)',
  ).matches;

  const elements = Array.from(
    document.querySelectorAll<HTMLElement>('[data-reveal]'),
  );

  if (prefersReduced || !('IntersectionObserver' in window)) {
    elements.forEach((el) => el.setAttribute('data-reveal', 'shown'));
    return;
  }

  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          entry.target.setAttribute('data-reveal', 'shown');
          observer.unobserve(entry.target);
        }
      }
    },
    { rootMargin: '0px 0px -8% 0px', threshold: 0.08 },
  );

  elements.forEach((el) => observer.observe(el));
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initReveal, { once: true });
} else {
  initReveal();
}
