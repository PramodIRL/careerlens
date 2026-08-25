"use client";

import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  getSkillProfile,
  type EvidenceSourceType,
  type SkillProfileResponse,
} from "@/lib/api-client";

interface SkillProfileSectionProps {
  accessToken: string;
  /** Bumped by the dashboard on ANY change to the underlying evidence:
   * resume extraction or a GitHub import finishing, and the user adding,
   * confirming or rejecting a skill. Every one of those moves the counts
   * this section displays. */
  refreshKey?: number;
}

// The workers commit a job's terminal status BEFORE the skills derived
// from it (app/worker.py: _mark_succeeded commits, then
// extract_skills_for_resume runs and commits separately; GitHub does the
// same with _derive_github_skills). So a refetch triggered the instant
// we observe "succeeded" can legitimately arrive before the evidence
// lands.
//
// The window is tiny — measured at ~9ms in this database — so one
// delayed follow-up covers it with enormous margin. Deliberately a
// single extra fetch rather than a retry loop: "poll until the payload
// stops changing" cannot tell "still writing" from "genuinely matched
// nothing", and would spin for every resume that produces no skills.
// If both attempts still lose the race, Refresh remains the fallback.
const SETTLE_REFETCH_MS = 2000;

// This section renders the SUMMARY ONLY. It deliberately does not list
// every skill: SkillsSection already does that, and it owns the
// confirm/reject actions. Two lists of the same skill names on one page
// would be a duplication smell rather than a feature, so the two
// sections have distinct jobs — this one answers "what does my evidence
// add up to", that one answers "what do I do about each skill".
const SOURCE_LABELS: Record<EvidenceSourceType, string> = {
  resume: "Resume",
  github: "GitHub",
  manual: "Added by you",
};

const SOURCE_ORDER: EvidenceSourceType[] = ["resume", "github", "manual"];

export default function SkillProfileSection({
  accessToken,
  refreshKey = 0,
}: SkillProfileSectionProps) {
  const [profile, setProfile] = useState<SkillProfileResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setProfile(await getSkillProfile(accessToken));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "something went wrong");
    }
  }, [accessToken]);

  // ONE effect for the initial load and every refresh, keyed on
  // refreshKey. `loading` is only ever cleared, never re-set, so a
  // background refresh never replaces a rendered summary with the
  // skeleton; a failed one keeps the last good numbers rather than
  // blanking the section.
  //
  // On a refreshKey bump this fetches immediately AND once more after
  // SETTLE_REFETCH_MS, to cover the worker's status-before-data commit
  // ordering described above. Both are cancelled on unmount, and the
  // timer is cleared if another bump arrives first.
  useEffect(() => {
    let cancelled = false;

    const fetchProfile = () =>
      getSkillProfile(accessToken)
        .then((next) => {
          if (!cancelled) setProfile(next);
        })
        .catch((err) => {
          if (!cancelled) {
            setError(
              err instanceof ApiError ? err.message : "something went wrong",
            );
          }
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });

    void fetchProfile();

    // Only after an external change — the very first mount has no
    // in-flight worker to wait for, so a second fetch there is waste.
    const timer =
      refreshKey > 0
        ? setTimeout(() => void fetchProfile(), SETTLE_REFETCH_MS)
        : undefined;

    return () => {
      cancelled = true;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [accessToken, refreshKey]);

  if (loading) {
    return (
      <p role="status" className="text-sm text-zinc-600 dark:text-zinc-400">
        Loading your skill profile…
      </p>
    );
  }

  // Only REPLACE the section with an error when there is nothing good to
  // show. Once a summary has rendered, a failed background refetch keeps
  // it and surfaces the error alongside — the same shape SkillsSection
  // uses. Blanking a populated summary because one poll failed would be
  // a worse regression than the stale data auto-refresh set out to fix.
  if (error && !profile) {
    return (
      <p
        role="alert"
        className="rounded bg-red-100 px-3 py-2 text-sm text-red-800"
      >
        {error}
      </p>
    );
  }

  if (!profile) return null;

  const { summary } = profile;

  if (summary.total === 0 && summary.rejected === 0) {
    return (
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        No evidence yet. Upload a resume or import your GitHub repositories to
        build your skill profile.
      </p>
    );
  }

  return (
    <div className="flex w-full flex-col gap-4 text-left">
      {error && (
        <p
          role="alert"
          className="rounded bg-red-100 px-3 py-2 text-sm text-red-800"
        >
          {error}
        </p>
      )}

      <div className="flex flex-wrap gap-4">
        <Stat label="Skills" value={summary.total} />
        <Stat label="Confirmed" value={summary.confirmed} />
        <Stat label="Needs review" value={summary.suggested} />
      </div>

      <section className="flex flex-col gap-1">
        <h3 className="text-sm font-semibold text-black dark:text-zinc-50">
          Where your evidence comes from
        </h3>
        {/* Counts distinct skills per source, not evidence rows — one
            repository can produce several rows for a single skill. */}
        <p className="text-xs text-zinc-500 dark:text-zinc-500">
          Number of skills each source supports.
        </p>
        <ul className="mt-1 flex flex-col gap-1">
          {SOURCE_ORDER.map((source) => (
            <li
              key={source}
              className="flex justify-between text-sm text-zinc-700 dark:text-zinc-300"
            >
              <span>{SOURCE_LABELS[source]}</span>
              <span className="font-medium tabular-nums">
                {summary.by_source[source] ?? 0}
              </span>
            </li>
          ))}
        </ul>
      </section>

      {summary.multi_source > 0 && (
        <p className="rounded bg-zinc-100 px-3 py-2 text-sm text-zinc-700 dark:bg-zinc-900 dark:text-zinc-300">
          <span className="font-medium">{summary.multi_source}</span>{" "}
          {summary.multi_source === 1 ? "skill is" : "skills are"} backed by
          more than one source.
        </p>
      )}

      {summary.rejected > 0 && (
        <p className="text-xs text-zinc-500 dark:text-zinc-500">
          {summary.rejected} rejected{" "}
          {summary.rejected === 1 ? "skill" : "skills"} not shown here.
        </p>
      )}

      <button
        type="button"
        onClick={load}
        className="self-start rounded border border-zinc-300 px-3 py-1 text-sm text-black dark:border-zinc-700 dark:text-zinc-50"
      >
        Refresh
      </button>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex flex-col">
      <span className="text-2xl font-semibold tabular-nums text-black dark:text-zinc-50">
        {value}
      </span>
      <span className="text-xs text-zinc-500 dark:text-zinc-500">{label}</span>
    </div>
  );
}
