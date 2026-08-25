"use client";

import { useEffect, useState } from "react";

import {
  ApiError,
  getJobGaps,
  type GapEntry,
  type JobGapResponse,
  type RequirementLevel,
} from "@/lib/api-client";

interface JobGapPanelProps {
  accessToken: string;
  savedJobId: string;
  /** Bumped by the dashboard when candidate skills or job data change.
   * Gaps are recomputed server-side from current rows, so a stale panel
   * is the only way this can be wrong. Reuses the existing refresh
   * coordination — no second architecture. */
  refreshKey?: number;
}

// A sibling to JobMatchPanel, in its own file: JobsSection is already
// ~700 lines, and gaps are a genuinely separate concern from the score.

const LEVEL_LABELS: Record<RequirementLevel, string> = {
  required: "Required",
  preferred: "Preferred",
  mentioned: "Mentioned",
};

/** Each bucket's heading and the plain-language line explaining what the
 * candidate's side says. The API returns facts; this supplies wording —
 * which is why no explanatory text is ever persisted. */
const SECTIONS: {
  key: keyof Pick<
    JobGapResponse,
    | "required_gaps"
    | "needs_confirmation"
    | "preferred_gaps"
    | "informational_gaps"
    | "rejected_requirements"
  >;
  heading: string;
  hint: string;
}[] = [
  {
    key: "required_gaps",
    heading: "Required gaps",
    hint: "This job requires these and we found no evidence you have them.",
  },
  {
    key: "needs_confirmation",
    heading: "Needs confirmation",
    hint: "We found evidence for these — confirm them in Your skills.",
  },
  {
    key: "preferred_gaps",
    heading: "Preferred gaps",
    hint: "Not required, but they would strengthen your application.",
  },
  {
    key: "informational_gaps",
    heading: "Informational",
    hint: "Mentioned in passing by the posting.",
  },
  {
    key: "rejected_requirements",
    heading: "You rejected these",
    hint: "You marked these as not yours. We have kept your decision.",
  },
];

/** What the candidate's side says, in words.
 *
 * Text, never colour alone — and each state reads differently on
 * purpose: "we did not find this" is a statement about OUR search,
 * while "you marked this as not yours" is a statement about the user's
 * decision. Collapsing them would misreport one as the other. */
function candidateLine(entry: GapEntry): string {
  if (entry.candidate_status === "rejected")
    return "You marked this as not yours";
  if (entry.candidate_status === "suggested")
    return "Found, but not yet reviewed by you";
  return "No evidence found";
}

export default function JobGapPanel({
  accessToken,
  savedJobId,
  refreshKey = 0,
}: JobGapPanelProps) {
  const [gaps, setGaps] = useState<JobGapResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // `loading` is only ever cleared, never re-set, so a background
  // refresh never replaces rendered gaps with the skeleton.
  useEffect(() => {
    let cancelled = false;
    getJobGaps(accessToken, savedJobId)
      .then((next) => {
        if (!cancelled) {
          setGaps(next);
          setError(null);
        }
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
    return () => {
      cancelled = true;
    };
  }, [accessToken, savedJobId, refreshKey]);

  if (loading) {
    return (
      <p role="status" className="text-xs text-zinc-600 dark:text-zinc-400">
        Checking skill gaps…
      </p>
    );
  }

  if (error && !gaps) {
    return (
      <p
        role="alert"
        className="rounded bg-red-100 px-3 py-2 text-xs text-red-800"
      >
        {error}
      </p>
    );
  }

  if (!gaps) return null;

  if (gaps.totals.total_requirements === 0) {
    // NOT "you match everything" — the job simply asks for nothing we
    // recognise, which is a fact about the JOB, not the candidate.
    return (
      <p className="text-xs text-zinc-600 dark:text-zinc-400">
        No skill requirements detected in this job description.
      </p>
    );
  }

  const hasAnyGap = SECTIONS.some((section) => gaps[section.key].length > 0);

  return (
    <div className="flex flex-col gap-3">
      {error && (
        <p
          role="alert"
          className="rounded bg-red-100 px-3 py-2 text-xs text-red-800"
        >
          {error}
        </p>
      )}

      {!hasAnyGap ? (
        <p className="text-xs text-zinc-700 dark:text-zinc-300">
          No gaps — you match every requirement.
        </p>
      ) : (
        SECTIONS.filter((section) => gaps[section.key].length > 0).map(
          (section) => (
            <section key={section.key} className="flex flex-col gap-1">
              <h5 className="text-xs font-semibold text-black dark:text-zinc-50">
                {section.heading} ({gaps[section.key].length})
              </h5>
              <p className="text-xs text-zinc-500 dark:text-zinc-500">
                {section.hint}
              </p>
              <ul className="flex flex-col gap-1">
                {gaps[section.key].map((entry) => (
                  <GapRow key={entry.skill_id} entry={entry} />
                ))}
              </ul>
            </section>
          ),
        )
      )}

      <p className="text-xs text-zinc-500 dark:text-zinc-500">
        {gaps.totals.satisfied} of {gaps.totals.total_requirements} requirements
        satisfied · {gaps.formula_version}
      </p>
    </div>
  );
}

function GapRow({ entry }: { entry: GapEntry }) {
  return (
    <li className="text-xs">
      <details>
        <summary className="cursor-pointer text-zinc-800 dark:text-zinc-200">
          <span className="font-medium">{entry.skill_name}</span>{" "}
          <span className="text-zinc-500 dark:text-zinc-500">
            · {LEVEL_LABELS[entry.requirement_level]} · {candidateLine(entry)}
          </span>
        </summary>
        <div className="mt-1 flex flex-col gap-1 pl-4 text-zinc-600 dark:text-zinc-400">
          <p>
            <span className="font-medium">This job:</span> “{entry.job_excerpt}”
          </p>
          {entry.candidate_evidence.length > 0 ? (
            entry.candidate_evidence.map((evidence, index) => (
              <p key={`${entry.skill_id}-${index}`}>
                <span className="font-medium">
                  Your {evidence.source_type}:
                </span>{" "}
                {evidence.excerpt ? `“${evidence.excerpt}”` : "added by you"} ·{" "}
                {Math.round(evidence.confidence * 100)}% match confidence
              </p>
            ))
          ) : (
            // The UI says this, not the API — no explanatory text is
            // ever stored as if it were evidence.
            <p>We found no evidence for this skill on your profile.</p>
          )}
        </div>
      </details>
    </li>
  );
}
