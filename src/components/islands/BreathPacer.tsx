import { useEffect, useRef, useState } from 'react';

/**
 * A small, optional breathing pacer — the only genuine interaction island in
 * the MVP. It exists to serve Garden's theme of recovering attention, and it is
 * built to honour user autonomy:
 *   - it never plays on its own; the visitor chooses to begin;
 *   - it can be stopped at any moment with no penalty or persuasion;
 *   - it announces phases to assistive tech via aria-live;
 *   - under prefers-reduced-motion the circle does not scale, but the pacing
 *     text still guides breathing.
 *
 * It is deliberately tiny and dependency-free beyond React.
 */

type Phase = { label: string; ms: number; key: 'in' | 'hold' | 'out' };

const CYCLE: Phase[] = [
  { label: 'Breathe in', ms: 4000, key: 'in' },
  { label: 'Hold', ms: 2000, key: 'hold' },
  { label: 'Let it go', ms: 6000, key: 'out' },
];

export default function BreathPacer(): React.ReactElement {
  const [active, setActive] = useState(false);
  const [index, setIndex] = useState(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!active) return;
    const phase = CYCLE[index];
    timer.current = setTimeout(() => {
      setIndex((i) => (i + 1) % CYCLE.length);
    }, phase.ms);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [active, index]);

  function toggle(): void {
    setActive((a) => {
      const next = !a;
      if (!next && timer.current) clearTimeout(timer.current);
      if (next) setIndex(0);
      return next;
    });
  }

  const phase = CYCLE[index];

  return (
    <div className="breath">
      <div
        className="breath__circle"
        data-phase={active ? phase.key : 'rest'}
        aria-hidden="true"
      />
      <p className="breath__cue" aria-live="polite">
        {active ? phase.label : 'A pause, if you would like one'}
      </p>
      <button type="button" className="breath__btn" onClick={toggle}>
        {active ? 'Stop' : 'Take a breath'}
      </button>
    </div>
  );
}
