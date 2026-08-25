"use client";

import { useEffect, useState } from "react";

import {
  ApiError,
  getJobMatch,
  type JobMatchResponse,
  type MatchedSkill,
  type MissingSkill,
  type RequirementLevel,
} from "@/lib/api-client";

interface JobMatchPanelProps {
  accessToken: string;
  savedJobId: string;
  /** Bumped by the dashboard when candidate skills or job data change,
   * so the score refetches without a manual action. The score is
   * recomputed server-side from current rows, so a stale panel is the
   * only way it can be wrong. */
  refreshKey?: number;
}

// Extracted from JobsSection rather than added to it: that file was
// already ~670 lines before matching, and a score panel is a genuinely
// separate concern from the create/edit/delete form.

const LEVEL_LABELS: Record<RequirementLevel, string> = {
  required: "Required",
  preferred: "Preferred",
  mentioned: "Mentioned",
};

const LEVEL_ORDER: RequirementLevel[] = ["required", "preferred", "mentioned"];

/** A plain-language band for the score.
 *
 * Text, not colour. Colour alone would carry the meaning for nobody
 * using a screen reader and for anyone who cannot distinguish the
 * palette — so every visual cue in this panel is paired with words. */
function scoreLabel(score: number): string {
  if (score >= 80) return "Strong match";
  if (score >= 50) return "Partial match";
  if (score > 0) return "Weak match";
  return "No match";
}

export default function JobMatchPanel({
  accessToken,
  savedJobId,
  refreshKey = 0,
}: JobMatchPanelProps) {
  const [match, setMatch] = useState<JobMatchResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // `loading` is only ever cleared, never re-set, so a background
  // refresh never replaces a rendered score with the skeleton.
  useEffect(() => {
    let cancelled = false;
    getJobMatch(accessToken, savedJobId)
      .then((next) => {
        if (!cancelled) {
          setMatch(next);
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
        Calculating match…
      </p>
    );
  }

  if (error && !match) {
    return (
      <p
        role="alert"
        className="rounded bg-red-100 px-3 py-2 text-xs text-red-800"
      >
        {error}
      </p>
    );
  }

  if (!match) return null;

  if (!match.has_requirements) {
    // NOT "0% match" — that would be a claim about the candidate, when
    // the truth is a fact about the job.
    return (
      <p className="text-xs text-zinc-600 dark:text-zinc-400">
        No skill requirements detected in this job description.
      </p>
    );
  }

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

      <div className="flex items-baseline gap-2">
        <p
          className="text-2xl font-semibold tabular-nums text-black dark:text-zinc-50"
          // Read as words, not as a bare number next to a symbol.
          aria-label={`${match.overall_score} percent match`}
        >
          {match.overall_score}%
        </p>
        <p className="text-xs text-zinc-600 dark:text-zinc-400">
          {scoreLabel(match.overall_score)}
        </p>
      </div>

      <dl className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
        {LEVEL_ORDER.filter((level) => match.by_level[level].total > 0).map(
          (level) => (
            <div key={level} className="flex gap-1">
              <dt className="text-zinc-600 dark:text-zinc-400">
                {LEVEL_LABELS[level]}
              </dt>
              <dd className="font-medium tabular-nums text-black dark:text-zinc-50">
                {match.by_level[level].matched} / {match.by_level[level].total}
              </dd>
            </div>
          ),
        )}
      </dl>

      {match.required_missing.length > 0 && (
        // The v1 formula applies no penalty for a missing required
        // skill, so the SCORE can look healthy while a hard requirement
        // is unmet. This is what makes that unmistakable.
        <p
          role="status"
          className="rounded bg-amber-100 px-3 py-2 text-xs text-amber-900 dark:bg-amber-950 dark:text-amber-200"
        >
          <span className="font-medium">
            Missing {match.required_missing.length} required{" "}
            {match.required_missing.length === 1 ? "skill" : "skills"}:
          </span>{" "}
          {match.required_missing.map((row) => row.skill_name).join(", ")}
        </p>
      )}

      {match.matched_skills.length > 0 && (
        <section className="flex flex-col gap-1">
          <h5 className="text-xs font-semibold text-black dark:text-zinc-50">
            Matched skills ({match.matched_skills.length})
          </h5>
          <ul className="flex flex-col gap-1">
            {match.matched_skills.map((row) => (
              <MatchedRow key={row.skill_id} skill={row} />
            ))}
          </ul>
        </section>
      )}

      {match.missing_skills.length > 0 && (
        <section className="flex flex-col gap-1">
          <h5 className="text-xs font-semibold text-black dark:text-zinc-50">
            Missing skills ({match.missing_skills.length})
          </h5>
          <ul className="flex flex-col gap-1">
            {match.missing_skills.map((row) => (
              <MissingRow key={row.skill_id} skill={row} />
            ))}
          </ul>
        </section>
      )}

      <p className="text-xs text-zinc-500 dark:text-zinc-500">
        Scored by {match.formula_version}: {match.earned_weight} of{" "}
        {match.obtainable_weight} weighted points.
      </p>
    </div>
  );
}

function MatchedRow({ skill }: { skill: MatchedSkill }) {
  return (
    <li className="text-xs">
      <details>
        <summary className="cursor-pointer text-zinc-800 dark:text-zinc-200">
          {/* The tick is decorative; the word "Matched" carries the
              meaning for assistive technology. */}
          <span aria-hidden="true">✓ </span>
          <span className="sr-only">Matched: </span>
          <span className="font-medium">{skill.skill_name}</span>{" "}
          <span className="text-zinc-500 dark:text-zinc-500">
            · {LEVEL_LABELS[skill.requirement_level]}
            {skill.candidate_unreviewed && " · needs review"}
          </span>
        </summary>
        <div className="mt-1 flex flex-col gap-1 pl-4 text-zinc-600 dark:text-zinc-400">
          <p>
            <span className="font-medium">This job:</span> “{skill.job_excerpt}”
          </p>
          {skill.candidate_evidence.length > 0 ? (
            skill.candidate_evidence.map((evidence, index) => (
              <p key={`${skill.skill_id}-${index}`}>
                <span className="font-medium">
                  Your {evidence.source_type}:
                </span>{" "}
                {evidence.excerpt ? `“${evidence.excerpt}”` : "added by you"} ·{" "}
                {Math.round(evidence.confidence * 100)}% match confidence
              </p>
            ))
          ) : (
            <p>You confirmed this skill.</p>
          )}
        </div>
      </details>
    </li>
  );
}

function MissingRow({ skill }: { skill: MissingSkill }) {
  return (
    <li className="text-xs">
      <details>
        <summary className="cursor-pointer text-zinc-800 dark:text-zinc-200">
          <span aria-hidden="true">✗ </span>
          <span className="sr-only">Missing: </span>
          <span className="font-medium">{skill.skill_name}</span>{" "}
          <span className="text-zinc-500 dark:text-zinc-500">
            · {LEVEL_LABELS[skill.requirement_level]}
            {/* "You rejected this" and "you don't have this" are
                different messages. */}
            {skill.candidate_rejected && " · you rejected this"}
          </span>
        </summary>
        <div className="mt-1 pl-4 text-zinc-600 dark:text-zinc-400">
          <p>
            <span className="font-medium">This job:</span> “{skill.job_excerpt}”
          </p>
        </div>
      </details>
    </li>
  );
}
