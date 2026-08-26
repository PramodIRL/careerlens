"use client";

import { useEffect, useState } from "react";

import {
  ApiError,
  getQualifications,
  updateQualifications,
  type Qualifications,
  type QualificationsUpdate,
} from "@/lib/api-client";

interface QualificationsSectionProps {
  accessToken: string;
  refreshKey?: number;
  /** Bumped so every saved job's eligibility panel refetches: ONE
   * profile serves all of them, so one edit changes every answer.
   * Reuses the dashboard's existing coordination — no new system. */
  onChanged?: () => void;
}

// THE CANDIDATE'S ONE REUSABLE QUALIFICATION PROFILE. Entered once
// here and compared against every saved job's entry requirements —
// there is deliberately no qualification form inside a saved job, and
// no per-job copy of any of this.
//
// USER-DECLARED AND AUTHORITATIVE. Only values the candidate has
// asserted count towards eligibility (app/qualifications/store.py), and
// a blank field reads as UNKNOWN — never as a blocker, and never as a
// failure. This is not a form anyone must complete before CareerLens is
// useful: skill matching, the primary feature, does not touch it.

const DEGREES = [
  "diploma",
  "btech",
  "be",
  "bsc",
  "bca",
  "mtech",
  "me",
  "msc",
  "mca",
  "mba",
  "phd",
] as const;

const FIELDS = [
  "computer_science",
  "computer_engineering",
  "information_technology",
  "information_science",
  "ai_ml",
  "data_science",
  "electronics",
  "electrical",
  "mechanical",
  "mechatronics",
  "civil",
  "chemical",
  "aerospace",
  "automobile",
  "instrumentation",
  "industrial_iot",
  "biotechnology",
  "mathematics",
  "other",
] as const;

const DEGREE_LABELS: Record<string, string> = {
  diploma: "Diploma",
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
};

const FIELD_LABELS: Record<string, string> = {
  ai_ml: "AI & ML",
  industrial_iot: "Industrial / IoT",
};

function label(value: string): string {
  return (
    FIELD_LABELS[value] ??
    DEGREE_LABELS[value] ??
    value.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())
  );
}

/** One row of the summary: what to call it, and how to render its
 * value. The API supplies facts; this supplies wording. */
const ROWS: {
  fact: string;
  heading: string;
  render: (q: Qualifications) => string | null;
}[] = [
  {
    fact: "cgpa",
    heading: "CGPA",
    render: (q) =>
      q.cgpa === null
        ? null
        : // The scale is shown even when unstated, because 10 is what
          // the comparison actually uses. Showing "8.2" alone would
          // hide the assumption from the person it affects.
          `${Number(q.cgpa)} / ${q.cgpa_scale === null ? 10 : Number(q.cgpa_scale)}`,
  },
  {
    fact: "class_10_percentage",
    heading: "Class 10",
    render: (q) =>
      q.class_10_percentage === null
        ? null
        : `${Number(q.class_10_percentage)}%`,
  },
  {
    fact: "class_12_percentage",
    heading: "Class 12",
    render: (q) =>
      q.class_12_percentage === null
        ? null
        : `${Number(q.class_12_percentage)}%`,
  },
  {
    fact: "highest_degree",
    heading: "Degree",
    render: (q) => (q.highest_degree === null ? null : label(q.highest_degree)),
  },
  {
    fact: "field_of_study",
    heading: "Field",
    render: (q) => (q.field_of_study === null ? null : label(q.field_of_study)),
  },
  {
    fact: "graduation_year",
    heading: "Graduation",
    render: (q) =>
      q.graduation_year === null ? null : String(q.graduation_year),
  },
  {
    fact: "years_experience",
    heading: "Experience",
    render: (q) =>
      q.years_experience === null
        ? null
        : `${Number(q.years_experience)} years`,
  },
];

interface Draft {
  cgpa: string;
  cgpa_scale: string;
  class_10_percentage: string;
  class_12_percentage: string;
  highest_degree: string;
  field_of_study: string;
  graduation_year: string;
  years_experience: string;
}

const EMPTY: Draft = {
  cgpa: "",
  cgpa_scale: "",
  class_10_percentage: "",
  class_12_percentage: "",
  highest_degree: "",
  field_of_study: "",
  graduation_year: "",
  years_experience: "",
};

function toDraft(loaded: Qualifications): Draft {
  return {
    cgpa: loaded.cgpa ?? "",
    cgpa_scale: loaded.cgpa_scale ?? "",
    class_10_percentage: loaded.class_10_percentage ?? "",
    class_12_percentage: loaded.class_12_percentage ?? "",
    highest_degree: loaded.highest_degree ?? "",
    field_of_study: loaded.field_of_study ?? "",
    graduation_year:
      loaded.graduation_year === null ? "" : String(loaded.graduation_year),
    years_experience: loaded.years_experience ?? "",
  };
}

/** An empty box is an explicit `null`, not an omission: clearing a
 * value the user no longer stands behind has to actually remove it. */
function toUpdate(draft: Draft): QualificationsUpdate {
  const text = (value: string) => (value.trim() === "" ? null : value.trim());
  return {
    cgpa: text(draft.cgpa),
    cgpa_scale: text(draft.cgpa_scale),
    class_10_percentage: text(draft.class_10_percentage),
    class_12_percentage: text(draft.class_12_percentage),
    highest_degree: text(draft.highest_degree),
    field_of_study: text(draft.field_of_study),
    graduation_year:
      draft.graduation_year.trim() === ""
        ? null
        : Number(draft.graduation_year),
    years_experience: text(draft.years_experience),
  };
}

export default function QualificationsSection({
  accessToken,
  refreshKey = 0,
  onChanged,
}: QualificationsSectionProps) {
  const [loaded, setLoaded] = useState<Qualifications | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [editing, setEditing] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // `loading` is only ever cleared, never re-set, so a refetch never
  // replaces a rendered summary with the skeleton.
  useEffect(() => {
    let cancelled = false;
    getQualifications(accessToken)
      .then((next) => {
        if (!cancelled) {
          setLoaded(next);
          setDraft(toDraft(next));
          setError(null);
        }
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setError(
            caught instanceof ApiError
              ? caught.message
              : "Could not load your qualifications.",
          );
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [accessToken, refreshKey]);

  function set<K extends keyof Draft>(key: K, value: string) {
    setDraft((current) => ({ ...current, [key]: value }));
  }

  function applyResult(next: Qualifications) {
    setLoaded(next);
    setDraft(toDraft(next));
    onChanged?.();
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    try {
      applyResult(await updateQualifications(accessToken, toUpdate(draft)));
      setEditing(false);
    } catch (caught: unknown) {
      setError(
        caught instanceof ApiError
          ? caught.message
          : "Could not save your qualifications.",
      );
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return (
      <p role="status" className="text-sm text-zinc-600 dark:text-zinc-400">
        Loading your qualifications…
      </p>
    );
  }

  if (loaded === null) {
    return error !== null ? (
      <p role="alert" className="text-xs text-red-700 dark:text-red-400">
        {error}
      </p>
    ) : null;
  }

  const anyKnown = ROWS.some((row) => row.render(loaded) !== null);

  if (!editing) {
    return (
      <div className="flex flex-col gap-3">
        {!anyKnown ? (
          <p className="text-xs text-zinc-600 dark:text-zinc-400">
            You have not added any qualifications yet. Jobs that state entry
            requirements will show what they need once you do.
          </p>
        ) : null}

        <ul className="flex flex-col gap-1">
          {ROWS.map((row) => {
            const value = row.render(loaded);
            return (
              <li key={row.fact} className="text-sm">
                <span
                  aria-hidden="true"
                  className={
                    value === null
                      ? "mr-2 text-zinc-500 dark:text-zinc-500"
                      : "mr-2 text-emerald-700 dark:text-emerald-400"
                  }
                >
                  {value === null ? "—" : "✓"}
                </span>
                <span className="sr-only">
                  {value === null ? "not provided: " : "provided: "}
                </span>
                <span className="inline-block w-28 text-zinc-700 dark:text-zinc-300">
                  {row.heading}
                </span>
                {value === null ? (
                  <span className="text-zinc-500 dark:text-zinc-500">
                    Not provided
                  </span>
                ) : (
                  <>
                    <span className="font-medium text-black dark:text-zinc-50">
                      {value}
                    </span>
                  </>
                )}
              </li>
            );
          })}
        </ul>

        {error !== null ? (
          <p role="alert" className="text-xs text-red-700 dark:text-red-400">
            {error}
          </p>
        ) : null}

        <button
          type="button"
          onClick={() => setEditing(true)}
          className="self-start rounded border border-zinc-300 px-3 py-1 text-sm dark:border-zinc-700"
        >
          Review / Edit
        </button>
      </div>
    );
  }

  const numberField = (key: keyof Draft, text: string, hint?: string) => (
    <div>
      <label
        htmlFor={`qualification-${key}`}
        className="block text-xs text-zinc-700 dark:text-zinc-300"
      >
        {text}
      </label>
      <input
        id={`qualification-${key}`}
        type="text"
        inputMode="decimal"
        value={draft[key]}
        onChange={(event) => set(key, event.target.value)}
        className="mt-1 w-full rounded border border-zinc-300 bg-white px-2 py-1 text-sm dark:border-zinc-700 dark:bg-zinc-900"
      />
      {hint ? (
        <p className="mt-1 text-xs text-zinc-500 dark:text-zinc-500">{hint}</p>
      ) : null}
    </div>
  );

  const selectField = (
    key: keyof Draft,
    text: string,
    options: readonly string[],
  ) => (
    <div>
      <label
        htmlFor={`qualification-${key}`}
        className="block text-xs text-zinc-700 dark:text-zinc-300"
      >
        {text}
      </label>
      <select
        id={`qualification-${key}`}
        value={draft[key]}
        onChange={(event) => set(key, event.target.value)}
        className="mt-1 w-full rounded border border-zinc-300 bg-white px-2 py-1 text-sm dark:border-zinc-700 dark:bg-zinc-900"
      >
        <option value="">Not set</option>
        {options.map((option) => (
          <option key={option} value={option}>
            {label(option)}
          </option>
        ))}
      </select>
    </div>
  );

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3">
      <p className="text-xs text-zinc-600 dark:text-zinc-400">
        These are used to check every saved job&apos;s entry requirements, so
        you only enter them once. Blank stays <strong>unknown</strong> — never
        counted against you.
      </p>

      <div className="grid grid-cols-2 gap-3">
        {numberField("cgpa", "CGPA")}
        {numberField(
          "cgpa_scale",
          "out of",
          "Leave blank if yours is out of 10.",
        )}
        {numberField("class_10_percentage", "Class 10 %")}
        {numberField("class_12_percentage", "Class 12 %")}
        {numberField("graduation_year", "Graduation year")}
        {numberField("years_experience", "Years of experience")}
        {selectField("highest_degree", "Highest degree", DEGREES)}
        {selectField("field_of_study", "Field of study", FIELDS)}
      </div>

      {error !== null ? (
        <p role="alert" className="text-xs text-red-700 dark:text-red-400">
          {error}
        </p>
      ) : null}

      <div className="flex gap-2">
        <button
          type="submit"
          disabled={saving}
          className="rounded bg-black px-3 py-1 text-sm text-white disabled:opacity-50 dark:bg-white dark:text-black"
        >
          {saving ? "Saving…" : "Save qualifications"}
        </button>
        <button
          type="button"
          onClick={() => {
            setDraft(toDraft(loaded));
            setEditing(false);
            setError(null);
          }}
          className="rounded border border-zinc-300 px-3 py-1 text-sm dark:border-zinc-700"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
