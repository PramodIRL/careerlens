"use client";

import { useEffect, useState } from "react";

import {
  ApiError,
  getJobSemantic,
  type JobSemanticResponse,
  type SemanticEvidence,
} from "@/lib/api-client";

/**
 * Candidate evidence that reads as relevant to a job's wording.
 *
 * DELIBERATELY NOT A SCORE PANEL. It shows a coarse band and the
 * evidence behind it, never a percentage next to the match score — the
 * thresholds it rests on are provisional (see
 * apps/api/app/embeddings/semantic_fit.py), so a precise-looking number
 * would claim more than the calibration supports.
 *
 * NEVER SAYS THE CANDIDATE HAS A SKILL. Skill ownership is decided by
 * the deterministic matcher and shown in JobMatchPanel. This panel says
 * only "here is stored evidence worth reading", and the wording in the
 * markup keeps that distinction explicit for a reader who lands on it
 * without context.
 *
 * Its own fetch, mirroring JobGapPanel: the match score renders
 * immediately whether or not this request is slow, unconfigured or
 * failing.
 */
interface JobSemanticPanelProps {
  accessToken: string;
  savedJobId: string;
  refreshKey?: number;
}

const BAND_LABEL: Record<string, string> = {
  strong: "Strong",
  moderate: "Moderate",
  weak: "Weak",
  none: "None found",
};

/** Where the evidence came from, in the candidate's terms. */
function sourceLabel(evidence: SemanticEvidence): string {
  if (evidence.evidence_source_type === "resume") return "Resume";
  if (evidence.evidence_source_type === "github") return "GitHub";
  return "Project";
}

export default function JobSemanticPanel({
  accessToken,
  savedJobId,
  refreshKey = 0,
}: JobSemanticPanelProps) {
  const [semantic, setSemantic] = useState<JobSemanticResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getJobSemantic(accessToken, savedJobId)
      .then((next) => {
        if (!cancelled) {
          setSemantic(next);
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
        Looking for related evidence…
      </p>
    );
  }

  // A failure here is not worth an alert banner: semantic relevance is
  // supporting information, and the deterministic match above is
  // unaffected. Said plainly and quietly instead.
  if (error && !semantic) {
    return (
      <p className="text-xs text-zinc-600 dark:text-zinc-400">
        Related evidence is unavailable right now.
      </p>
    );
  }

  if (!semantic) return null;

  if (semantic.considered === 0) {
    return (
      <p className="text-xs text-zinc-600 dark:text-zinc-400">
        This job has not been indexed for related evidence yet.
      </p>
    );
  }

  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-baseline gap-2">
        <h4 className="text-xs font-semibold text-zinc-900 dark:text-zinc-100">
          Semantic relevance
        </h4>
        <span className="text-xs text-zinc-700 dark:text-zinc-300">
          {BAND_LABEL[semantic.band] ?? semantic.band}
        </span>
      </div>

      {/* The load-bearing sentence. A reader must not leave this panel
          believing it asserts the candidate has a skill. */}
      <p className="text-xs text-zinc-600 dark:text-zinc-400">
        Supporting evidence only — this does not confirm the candidate has a
        skill. Skill matches are shown above.
      </p>

      {semantic.evidence.length === 0 ? (
        <p className="text-xs text-zinc-600 dark:text-zinc-400">
          No closely related evidence found.
        </p>
      ) : (
        <ul className="flex flex-col gap-1">
          {semantic.evidence.map((row) => (
            <li
              key={row.embedding_id}
              className="rounded bg-zinc-100 px-2 py-1 text-xs dark:bg-zinc-800"
            >
              <p className="text-zinc-900 dark:text-zinc-100">
                {row.excerpt ?? "(no excerpt stored)"}
              </p>
              <p className="text-zinc-600 dark:text-zinc-400">
                {sourceLabel(row)} · similarity {row.similarity.toFixed(2)}
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
