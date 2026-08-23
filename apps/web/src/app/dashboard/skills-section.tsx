"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";

import {
  addCandidateSkill,
  ApiError,
  listCandidateSkills,
  updateCandidateSkillStatus,
  type CandidateSkillDecision,
  type CandidateSkillResponse,
  type CandidateSkillStatus,
} from "@/lib/api-client";

interface SkillsSectionProps {
  accessToken: string;
}

// Skills appear once the extraction worker has processed an uploaded
// resume, which happens outside this component's knowledge. Rather than
// polling on a timer forever, or coupling this to ResumeSection's own
// poll (which would mean reaching into Prompt 2.2's component), this
// fetches on mount and offers an explicit Refresh. A known limitation,
// documented in docs/decisions.md.
const GROUPS: {
  status: CandidateSkillStatus;
  heading: string;
  hint: string;
}[] = [
  {
    status: "suggested",
    heading: "Needs review",
    hint: "Found in your resume. Confirm the ones you actually have.",
  },
  { status: "confirmed", heading: "Confirmed", hint: "" },
  { status: "rejected", heading: "Rejected", hint: "" },
];

const SOURCE_LABELS: Record<string, string> = {
  resume: "From your resume",
  github: "From GitHub",
  manual: "Added by you",
};

function confidenceLabel(confidence: number): string {
  return `${Math.round(confidence * 100)}% confidence`;
}

export default function SkillsSection({ accessToken }: SkillsSectionProps) {
  const [skills, setSkills] = useState<CandidateSkillResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [newSkill, setNewSkill] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setSkills(await listCandidateSkills(accessToken));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "something went wrong");
    }
  }, [accessToken]);

  useEffect(() => {
    let cancelled = false;
    listCandidateSkills(accessToken)
      .then((list) => {
        if (!cancelled) setSkills(list);
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
  }, [accessToken]);

  async function decide(id: string, status: CandidateSkillDecision) {
    setBusyId(id);
    setError(null);
    try {
      const updated = await updateCandidateSkillStatus(accessToken, id, status);
      setSkills((prev) =>
        prev.map((skill) => (skill.id === id ? updated : skill)),
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "something went wrong");
    } finally {
      setBusyId(null);
    }
  }

  async function handleAdd(event: FormEvent) {
    event.preventDefault();
    const name = newSkill.trim();
    if (!name) return;

    setAdding(true);
    setError(null);
    try {
      await addCandidateSkill(accessToken, name);
      setNewSkill("");
      await load();
    } catch (err) {
      // A 422 here is the expected "not in the taxonomy" answer, so the
      // API's own message is the useful thing to show.
      setError(err instanceof ApiError ? err.message : "something went wrong");
    } finally {
      setAdding(false);
    }
  }

  if (loading) {
    return (
      <p role="status" className="text-sm text-zinc-600 dark:text-zinc-400">
        Loading skills…
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

      <form onSubmit={handleAdd} className="flex flex-col gap-1">
        <label
          htmlFor="add-skill"
          className="text-sm font-medium text-black dark:text-zinc-50"
        >
          Add a skill
        </label>
        <div className="flex gap-2">
          <input
            id="add-skill"
            value={newSkill}
            onChange={(event) => setNewSkill(event.target.value)}
            disabled={adding}
            aria-describedby="add-skill-hint"
            className="min-w-0 flex-1 rounded border border-zinc-300 px-3 py-2 text-sm dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-50"
          />
          <button
            type="submit"
            disabled={adding || !newSkill.trim()}
            className="shrink-0 rounded bg-black px-3 py-2 text-sm text-white disabled:opacity-50 dark:bg-white dark:text-black"
          >
            {adding ? "Adding…" : "Add"}
          </button>
        </div>
        <p
          id="add-skill-hint"
          className="text-xs text-zinc-500 dark:text-zinc-500"
        >
          Must be a skill we recognise, such as “Python” or “k8s”.
        </p>
      </form>

      <button
        type="button"
        onClick={load}
        className="self-start rounded border border-zinc-300 px-3 py-1 text-sm text-black dark:border-zinc-700 dark:text-zinc-50"
      >
        Refresh
      </button>

      {skills.length === 0 ? (
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          No skills yet. Upload a resume, or add one above.
        </p>
      ) : (
        GROUPS.map(({ status, heading, hint }) => {
          const group = skills.filter((skill) => skill.status === status);
          if (group.length === 0) return null;
          return (
            <section key={status} className="flex flex-col gap-2">
              <h3 className="text-sm font-semibold text-black dark:text-zinc-50">
                {heading} ({group.length})
              </h3>
              {hint && (
                <p className="text-xs text-zinc-500 dark:text-zinc-500">
                  {hint}
                </p>
              )}
              <ul className="flex flex-col gap-2">
                {group.map((skill) => (
                  <li
                    key={skill.id}
                    className="rounded border border-zinc-300 px-3 py-2 dark:border-zinc-700"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="truncate text-sm font-medium text-black dark:text-zinc-50">
                          {skill.skill_name}
                        </p>
                        {skill.skill_category && (
                          <p className="text-xs text-zinc-500 dark:text-zinc-500">
                            {skill.skill_category}
                          </p>
                        )}
                      </div>
                      <div className="flex shrink-0 gap-2">
                        {skill.status !== "confirmed" && (
                          <button
                            type="button"
                            disabled={busyId === skill.id}
                            onClick={() => decide(skill.id, "confirmed")}
                            className="rounded border border-green-300 px-2 py-1 text-xs text-green-700 disabled:opacity-50 dark:border-green-800 dark:text-green-400"
                          >
                            {skill.status === "rejected"
                              ? "Restore"
                              : "Confirm"}
                          </button>
                        )}
                        {skill.status !== "rejected" && (
                          <button
                            type="button"
                            disabled={busyId === skill.id}
                            onClick={() => decide(skill.id, "rejected")}
                            className="rounded border border-red-300 px-2 py-1 text-xs text-red-700 disabled:opacity-50 dark:border-red-800 dark:text-red-400"
                          >
                            Reject
                          </button>
                        )}
                      </div>
                    </div>

                    {/* Evidence is never hidden behind an interaction —
                        the product rule is that an inferred skill must
                        show why it was inferred. */}
                    <ul className="mt-2 flex flex-col gap-1">
                      {skill.evidence.map((item) => (
                        <li
                          key={item.id}
                          className="text-xs text-zinc-600 dark:text-zinc-400"
                        >
                          <span className="font-medium">
                            {SOURCE_LABELS[item.source_type] ??
                              item.source_type}
                          </span>{" "}
                          · {confidenceLabel(item.confidence)}
                          {item.excerpt && (
                            <blockquote className="mt-1 border-l-2 border-zinc-300 pl-2 italic dark:border-zinc-700">
                              {item.excerpt}
                            </blockquote>
                          )}
                        </li>
                      ))}
                    </ul>
                  </li>
                ))}
              </ul>
            </section>
          );
        })
      )}
    </div>
  );
}
