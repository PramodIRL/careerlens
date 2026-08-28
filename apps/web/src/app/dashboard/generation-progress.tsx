"use client";

import { useEffect, useState } from "react";

/**
 * Elapsed-time feedback for a running LLM generation.
 *
 * WHY THIS EXISTS. `explanation_timeout_seconds` is 180, and a local
 * 7B model generates at roughly ten tokens a second — so "Writing an
 * explanation…" can sit unchanged on screen for minutes. A static
 * string cannot distinguish a model that is working from a daemon that
 * has gone away, and the honest difference between the two is simply
 * whether time is passing and how much.
 *
 * IT COUNTS, IT DOES NOT POLL. The interval touches only local state;
 * no request is made or repeated while it runs. The generation is a
 * single in-flight call throughout, exactly as before.
 *
 * IT SAYS NOTHING ABOUT THE RESULT. The counter reports elapsed time
 * and nothing else — it never predicts success, estimates a finish, or
 * implies the answer is nearly ready.
 */

/** When the wait stops being unremarkable and deserves an explanation.
 *
 * Chosen against the measured rate rather than picked round: a short
 * explanation is tens of seconds of generation, so a note any earlier
 * would fire on every normal run and teach the user to ignore it. */
const SLOW_AFTER_SECONDS = 20;

interface GenerationProgressProps {
  running: boolean;
  /** What is being generated, in the caller's own words — the existing
   * loading copy, kept rather than replaced. */
  label: string;
}

/**
 * THE RESET IS THE UNMOUNT. This counter is mounted only while a
 * generation is running, so it starts at zero by construction and a
 * finished run leaves no number behind for the next one to inherit —
 * a success and a failure end it identically, which is what the
 * caller's `finally` already guarantees.
 *
 * Written this way rather than as a `running`-dependent effect that
 * zeroes its own state: that shape sets state synchronously inside an
 * effect body, which cascades a render and which this project's lint
 * rules reject on exactly those grounds.
 */
export default function GenerationProgress({
  running,
  label,
}: GenerationProgressProps) {
  if (!running) return null;
  return <ElapsedNotice label={label} />;
}

function ElapsedNotice({ label }: { label: string }) {
  const [seconds, setSeconds] = useState(0);

  // The only state change here happens inside the interval CALLBACK,
  // never in the effect body.
  useEffect(() => {
    const timer = setInterval(() => setSeconds((prior) => prior + 1), 1000);
    return () => clearInterval(timer);
  }, []);

  return (
    <div className="flex flex-col gap-1">
      {/* ONE status region, updated in place. A second live region for
          the note below would announce the whole thing again every
          second; keeping the note outside `role="status"` means a
          screen reader hears the count without the explanation being
          repeated at it. */}
      <p role="status" className="text-xs text-zinc-600 dark:text-zinc-400">
        {label} {seconds}s
      </p>
      {seconds >= SLOW_AFTER_SECONDS && (
        // LEADS WITH WHAT IS STILL TRUE. The deterministic results are
        // already on screen and are not waiting on this, which is the
        // fact that makes a long wait tolerable rather than alarming.
        <p className="text-xs text-zinc-500 dark:text-zinc-500">
          This runs on a local model and can take a couple of minutes. Your
          scores and gaps are already complete and are not waiting for it.
        </p>
      )}
    </div>
  );
}
