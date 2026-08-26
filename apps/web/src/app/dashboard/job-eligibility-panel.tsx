"use client";

import { useEffect, useState } from "react";

import {
  ApiError,
  getJobEligibility,
  type EligibilityEntry,
  type EligibilityRequirementType,
  type EligibilityState,
  type JobEligibilityResponse,
} from "@/lib/api-client";

interface JobEligibilityPanelProps {
  accessToken: string;
  savedJobId: string;
  /** Bumped when the candidate saves their qualification profile. One
   * profile serves every saved job, so one edit changes every answer —
   * reuses the dashboard's existing coordination, no new system. */
  refreshKey?: number;
}

// SUPPORTING INFORMATION, NOT THE HEADLINE. CareerLens is a skill
// matching product; this panel sits below the score and the gaps and
// stays deliberately quiet. It reads the candidate's ONE reusable
// qualification profile — there is no per-job qualification data and no
// form here, only a link to the profile when it is missing.

const TYPE_LABELS: Record<EligibilityRequirementType, string> = {
  cgpa: "CGPA",
  class_10_percentage: "Class 10",
  class_12_percentage: "Class 12",
  highest_degree: "Degree",
  field_of_study: "Branch",
  graduation_year: "Graduation",
  years_experience: "Experience",
};

/** Four markers, never two, and never colour alone — each is paired
 * with visually-hidden text so the state is available to a screen
 * reader and to anyone who cannot distinguish the colours. */
const STATE_MARKS: Record<EligibilityState, string> = {
  satisfied: "✓",
  not_satisfied: "✗",
  unknown: "?",
  undetermined: "?",
};

const STATE_STYLES: Record<EligibilityState, string> = {
  satisfied: "text-emerald-700 dark:text-emerald-400",
  not_satisfied: "text-red-700 dark:text-red-400",
  unknown: "text-zinc-600 dark:text-zinc-400",
  undetermined: "text-zinc-600 dark:text-zinc-400",
};

const STATE_WORDS: Record<EligibilityState, string> = {
  satisfied: "meets this requirement",
  not_satisfied: "does not meet this requirement",
  unknown: "not provided",
  undetermined: "cannot be determined",
};

/** Wording lives here, never in the database. The API returns a stable
 * `reason` token; changing this copy is not an API change. */
const REASON_TEXT: Record<string, string> = {
  below_threshold: "Below the required minimum",
  above_threshold: "Above the permitted maximum",
  outside_range: "Outside the required range",
  value_mismatch: "Does not match what this job asks for",
  not_in_accepted_values: "Not one of the accepted options",
  candidate_value_unknown: "Not provided",
  open_ended_list: "This posting says “or related”, so we cannot decide",
  unsupported_comparator: "We cannot interpret this requirement",
};

const FLAG_TEXT = {
  eligible: {
    label: "Meets all requirements",
    tone: "text-emerald-700 dark:text-emerald-400",
    mark: "✓",
  },
  not_eligible: {
    label: "Not eligible",
    tone: "text-red-700 dark:text-red-400",
    mark: "✗",
  },
  unknown: {
    label: "Unknown",
    tone: "text-zinc-700 dark:text-zinc-300",
    mark: "?",
  },
} as const;

/** Display names for the normalized vocabulary. Stored values are
 * `btech` and `computer_science`; a person reads "B.Tech" and "Computer
 * Science". Kept beside the other wording here for the same reason —
 * the API owns the fact, this file owns how it is said. */
const VALUE_LABELS: Record<string, string> = {
  btech: "B.Tech",
  be: "B.E.",
  bsc: "B.Sc",
  bca: "BCA",
  mtech: "M.Tech",
  me: "M.E.",
  msc: "M.Sc",
  mca: "MCA",
  mba: "MBA",
  phd: "PhD",
  diploma: "Diploma",
  ai_ml: "AI & ML",
  industrial_iot: "Industrial / IoT",
};

function labelFor(value: string): string {
  return (
    VALUE_LABELS[value] ??
    value.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())
  );
}

function formatNumber(value: string | null): string | null {
  if (value === null) return null;
  const parsed = Number(value);
  if (Number.isNaN(parsed)) return value;
  // Trailing zeros come from the Numeric column, not from the person —
  // 88.00 is how they typed 88.
  return String(parsed);
}

/** What the candidate has. Null when they have not said — NEVER a
 * stand-in number, since an absent fact must render as absent. */
function candidateText(entry: EligibilityEntry): string | null {
  if (entry.candidate_text !== null) {
    return labelFor(entry.candidate_text);
  }
  const value = formatNumber(entry.candidate_numeric);
  if (value === null) return null;
  const scale = formatNumber(entry.candidate_scale);
  // An unstated scale still compares as /10, so showing it keeps the
  // assumption visible rather than hidden inside the comparison.
  return entry.requirement_type === "cgpa"
    ? `${value} / ${scale ?? 10}`
    : entry.requirement_type === "class_10_percentage" ||
        entry.requirement_type === "class_12_percentage"
      ? `${value}%`
      : value;
}

/** The bar the candidate's value is held to, rendered as the second
 * half of a comparison: "≥ 7.5", "= 2026", "(needs B.Tech)". */
function comparisonText(entry: EligibilityEntry): string {
  if (entry.comparator === "in") {
    // A categorical match needs no operator — the value already is the
    // answer, and "computer science ≥ computer science" is nonsense.
    if (entry.state === "satisfied") return "";
    return `(needs ${entry.accepted_values.map(labelFor).join(", ")})`;
  }
  const value = formatNumber(entry.requirement_numeric);
  if (value === null) return "";
  if (entry.comparator === "between") {
    return `in ${value}–${formatNumber(entry.requirement_max) ?? "?"}`;
  }
  if (entry.comparator === "eq") return `= ${value}`;
  const suffix =
    entry.requirement_type === "class_10_percentage" ||
    entry.requirement_type === "class_12_percentage"
      ? "%"
      : "";
  return `${entry.comparator === "lte" ? "≤" : "≥"} ${value}${suffix}`;
}

export default function JobEligibilityPanel({
  accessToken,
  savedJobId,
  refreshKey = 0,
}: JobEligibilityPanelProps) {
  const [eligibility, setEligibility] = useState<JobEligibilityResponse | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // `loading` is only ever cleared, never re-set, so a refetch after a
  // profile edit never replaces a rendered verdict with the skeleton.
  useEffect(() => {
    let cancelled = false;
    getJobEligibility(accessToken, savedJobId)
      .then((next) => {
        if (!cancelled) {
          setEligibility(next);
          setError(null);
        }
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setError(
            caught instanceof ApiError
              ? caught.message
              : "Could not load eligibility.",
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
        Checking eligibility…
      </p>
    );
  }

  if (error !== null) {
    return (
      <p role="alert" className="text-xs text-red-700 dark:text-red-400">
        {error}
      </p>
    );
  }

  if (eligibility === null) return null;

  // CASE 1 — a statement about the JOB, not the candidate. Rendering it
  // as "eligible" would claim a check we never ran; rendering it as
  // "not eligible" would be simply false.
  if (!eligibility.has_requirements) {
    return (
      <p className="text-xs text-zinc-600 dark:text-zinc-400">
        No eligibility requirements detected
      </p>
    );
  }

  // CASE 2 — the job asks for qualifications and the candidate has
  // asserted none. Distinct from a profile with gaps: this one is fixed
  // by filling in a form, so say that and point at it, rather than
  // showing a column of identical question marks.
  if (!eligibility.has_qualification_profile) {
    return (
      <div className="flex flex-col gap-2">
        <p className="text-xs font-semibold text-black dark:text-zinc-50">
          Qualification profile not set up
        </p>
        <p className="text-xs text-zinc-600 dark:text-zinc-400">
          Add your qualifications to check whether you meet this job&apos;s
          requirements.
        </p>
        {/* A real link, so keyboard and screen-reader navigation work
            without any JavaScript of ours. */}
        <a
          href="#qualifications"
          className="self-start rounded border border-zinc-300 px-2 py-1 text-xs font-medium text-black dark:border-zinc-700 dark:text-zinc-50"
        >
          Set up eligibility
        </a>
      </div>
    );
  }

  // CASES 3 and 4 — a profile exists, so evaluate. Missing fields come
  // back as `unknown`, never as failure.
  const flag = FLAG_TEXT[eligibility.flag];
  const { totals } = eligibility;

  return (
    <div className="flex flex-col gap-2">
      <p className={`text-xs font-semibold ${flag.tone}`}>
        <span aria-hidden="true">{flag.mark} </span>
        {flag.label}
      </p>

      <ul className="flex flex-col gap-1">
        {eligibility.requirements.map((entry) => {
          const mine = candidateText(entry);
          const comparison = comparisonText(entry);
          return (
            <li
              key={entry.requirement_type}
              className="text-xs text-zinc-700 dark:text-zinc-300"
            >
              <span
                aria-hidden="true"
                className={`mr-1 ${STATE_STYLES[entry.state]}`}
              >
                {STATE_MARKS[entry.state]}
              </span>
              <span className="inline-block w-20 font-medium">
                {TYPE_LABELS[entry.requirement_type]}
              </span>{" "}
              {/* The candidate's own value first, then the bar it is
                  held to: "8.2 / 10 ≥ 7.5" reads as a comparison, where
                  the reverse reads as a demand. */}
              {mine !== null ? (
                <span className="text-black dark:text-zinc-50">
                  {mine}
                  {comparison === "" ? "" : ` ${comparison}`}
                </span>
              ) : (
                <span className="text-zinc-500 dark:text-zinc-500">
                  Not provided
                </span>
              )}
              {entry.requirement_level === "preferred" ? (
                <span className="text-zinc-500 dark:text-zinc-500">
                  {" "}
                  · preferred
                </span>
              ) : null}
              <span className="sr-only"> — {STATE_WORDS[entry.state]}</span>
              {/* The reason line is suppressed when the row already
                  says it: "Not provided" twice is noise. */}
              {entry.state !== "satisfied" &&
              mine !== null &&
              entry.reason in REASON_TEXT ? (
                <span className="block pl-5 text-zinc-500 dark:text-zinc-500">
                  {REASON_TEXT[entry.reason]}
                </span>
              ) : null}
            </li>
          );
        })}
      </ul>

      <p className="text-xs text-zinc-500 dark:text-zinc-500">
        {totals.satisfied} met · {totals.not_satisfied} not met ·{" "}
        {totals.unknown + totals.undetermined} unknown ·{" "}
        {eligibility.formula_version}
      </p>
    </div>
  );
}
