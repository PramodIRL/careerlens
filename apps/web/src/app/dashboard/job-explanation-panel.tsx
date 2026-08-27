"use client";

import { useState } from "react";

import {
  ApiError,
  getJobExplanation,
  type CitedEvidence,
  type ExplanationClaim,
  type JobExplanationResponse,
} from "@/lib/api-client";

/**
 * A structured, validated explanation of a match.
 *
 * OPT-IN, AND FETCHED ON DEMAND. Nothing is requested until the user
 * asks for it — unlike every other panel here, which loads with the
 * page. Generated prose is the weakest thing on this screen and should
 * not arrive uninvited alongside the numbers it describes.
 *
 * IT EXPLAINS THE SCORE, IT DOES NOT PRODUCE IT. `overall_score` comes
 * from skill_match_v1 and is rendered by JobMatchPanel; this panel
 * never shows a number of its own.
 *
 * A REJECTION IS A NORMAL OUTCOME, not an error. The API validates the
 * model's answer against the persisted facts and returns nothing
 * generated when it fails — so this says "we could not produce a
 * grounded explanation", which is the truthful message, and the match
 * above is unaffected.
 *
 * EVIDENCE SITS BEHIND A DISCLOSURE, not under every sentence. The
 * excerpts are the proof and all of them stay available — but proof
 * belongs where a reader reaches for it, rather than interleaved
 * through the first paragraph they read.
 *
 * EVERY QUOTE IS A STORED ROW. `cited_evidence` excerpts are the
 * candidate's own saved text, hydrated server-side; the model chose
 * which rows to cite and wrote none of their words.
 */
interface JobExplanationPanelProps {
  accessToken: string;
  savedJobId: string;
}

/** Wording for the machine-readable reasons app/explanation/validate.py
 * returns. Unknown values fall back to the generic sentence rather than
 * showing a raw enum. */
const REASON_LABEL: Record<string, string> = {
  malformed_json: "the response was not valid JSON",
  schema_invalid: "the response did not match the expected structure",
  unknown_evidence_id: "it cited evidence that does not exist",
  ungrounded_claim: "it made a claim with no evidence behind it",
  invented_skill: "it named a skill this job never mentions",
  invented_number: "it stated a number the stored facts do not contain",
  disallowed_link: "it included a link",
  response_too_large: "the response was too long",
  // Provider-side failures (Prompt 6.2). Already retried where retrying
  // could help, so these mean the attempts were used up.
  provider_timeout: "the explanation service did not answer in time",
  provider_unavailable: "the explanation service was unavailable",
  provider_error: "the explanation service failed unexpectedly",
};

function ClaimList({ claims }: { claims: ExplanationClaim[] }) {
  return (
    <ul className="flex flex-col gap-1">
      {claims.map((claim, index) => (
        <li key={index} className="text-xs text-black dark:text-zinc-50">
          {claim.text}
        </li>
      ))}
    </ul>
  );
}

/**
 * Every cited row, once, behind a disclosure.
 *
 * NOT INLINE UNDER EACH CLAIM, which is where it used to live. Excerpts
 * are the proof, and proof belongs where a reader can reach for it —
 * not interleaved through the first paragraph they read, and not
 * repeated because two claims happened to cite the same row.
 *
 * EVERY QUOTE IS STILL A STORED ROW. Nothing is summarised or
 * paraphrased here; the excerpts are exactly what the API returned.
 */
function SupportingEvidence({ evidence }: { evidence: CitedEvidence[] }) {
  if (evidence.length === 0) return null;

  return (
    <details className="border-t border-zinc-200 pt-2 dark:border-zinc-800">
      <summary className="cursor-pointer text-xs text-zinc-600 dark:text-zinc-400">
        Supporting evidence ({evidence.length})
      </summary>
      <ul className="mt-2 flex flex-col gap-2">
        {evidence.map((row) => (
          <li
            key={row.evidence_id}
            className="border-l-2 border-zinc-300 pl-2 text-xs text-zinc-600 dark:border-zinc-700 dark:text-zinc-400"
          >
            <span className="font-medium">{row.source_type}</span>
            {row.excerpt ? `: “${row.excerpt}”` : null}
          </li>
        ))}
      </ul>
    </details>
  );
}

export default function JobExplanationPanel({
  accessToken,
  savedJobId,
}: JobExplanationPanelProps) {
  const [explanation, setExplanation] = useState<JobExplanationResponse | null>(
    null,
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setExplanation(await getJobExplanation(accessToken, savedJobId));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "something went wrong");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <h4 className="text-xs font-semibold text-black dark:text-zinc-50">
          Explain this match
        </h4>
        <button
          type="button"
          onClick={load}
          disabled={loading}
          className="rounded border border-zinc-300 px-2 py-1 text-xs text-black disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-50"
        >
          {explanation ? "Regenerate" : "Explain"}
        </button>
      </div>

      {loading && (
        <p role="status" className="text-xs text-zinc-600 dark:text-zinc-400">
          Writing an explanation…
        </p>
      )}

      {error && (
        <p role="alert" className="text-xs text-red-700 dark:text-red-400">
          {error}
        </p>
      )}

      {explanation && explanation.status === "rejected" && (
        <p role="status" className="text-xs text-zinc-600 dark:text-zinc-400">
          No explanation was shown because{" "}
          {REASON_LABEL[explanation.reason ?? ""] ??
            "it could not be checked against your stored facts"}
          . Your match score and skill gaps above are unaffected.
        </p>
      )}

      {explanation && explanation.status === "generated" && (
        <div className="flex flex-col gap-3">
          {/* Says plainly what this is, for a reader who lands on the
              prose without context. */}
          <p className="text-xs text-zinc-600 dark:text-zinc-400">
            Written from your stored match facts and checked against them.
            Guidance, not a hiring decision — the score above is unchanged by
            it.
          </p>
          {explanation.summary && (
            <p className="text-xs text-black dark:text-zinc-50">
              {explanation.summary}
            </p>
          )}
          {explanation.strengths.length > 0 && (
            <div>
              <h5 className="mb-1 text-xs font-medium text-black dark:text-zinc-50">
                Strengths
              </h5>
              <ClaimList claims={explanation.strengths} />
            </div>
          )}
          {explanation.gaps.length > 0 && (
            <div>
              <h5 className="mb-1 text-xs font-medium text-black dark:text-zinc-50">
                Gaps
              </h5>
              <ClaimList claims={explanation.gaps} />
            </div>
          )}
          {explanation.next_steps.length > 0 && (
            <div>
              <h5 className="mb-1 text-xs font-medium text-black dark:text-zinc-50">
                Next steps
              </h5>
              <ul className="list-disc pl-4 text-xs text-black dark:text-zinc-50">
                {explanation.next_steps.map((step, index) => (
                  <li key={index}>{step}</li>
                ))}
              </ul>
            </div>
          )}
          <SupportingEvidence evidence={explanation.cited_evidence} />
        </div>
      )}
    </div>
  );
}
