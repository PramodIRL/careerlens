"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";

import {
  ApiError,
  createSavedJob,
  deleteSavedJob,
  listSavedJobs,
  updateSavedJob,
  type EmploymentType,
  type SavedJobCreateRequest,
  type SavedJobResponse,
} from "@/lib/api-client";

interface JobsSectionProps {
  accessToken: string;
}

// Deliberately NOT wired into the dashboard's refresh counters. A saved
// job produces no skill evidence in Prompt 4.1, so the skill sections
// have nothing to refetch when one changes. Coupling them now would be
// a dependency with no data behind it; that belongs to Prompt 4.2/4.3,
// when a job actually contributes skills.

const EMPLOYMENT_TYPES: { value: EmploymentType; label: string }[] = [
  { value: "full_time", label: "Full-time" },
  { value: "part_time", label: "Part-time" },
  { value: "contract", label: "Contract" },
  { value: "internship", label: "Internship" },
  { value: "temporary", label: "Temporary" },
];

const EMPLOYMENT_LABELS: Record<EmploymentType, string> = {
  full_time: "Full-time",
  part_time: "Part-time",
  contract: "Contract",
  internship: "Internship",
  temporary: "Temporary",
};

const MAX_DESCRIPTION_LENGTH = 60_000;

interface JobFormValues {
  company: string;
  title: string;
  location: string;
  employment_type: string;
  source_url: string;
  description: string;
}

const EMPTY_FORM: JobFormValues = {
  company: "",
  title: "",
  location: "",
  employment_type: "",
  source_url: "",
  description: "",
};

function toFormValues(job: SavedJobResponse): JobFormValues {
  return {
    company: job.company,
    title: job.title,
    location: job.location ?? "",
    employment_type: job.employment_type ?? "",
    source_url: job.source_url ?? "",
    description: job.description,
  };
}

/** Blank optional inputs become null, which is how the API spells
 * "no value" — sending "" would be a second way to say the same thing
 * and the server rejects it for the non-clearable fields. */
function toRequest(values: JobFormValues): SavedJobCreateRequest {
  return {
    company: values.company.trim(),
    title: values.title.trim(),
    description: values.description.trim(),
    location: values.location.trim() || null,
    employment_type: (values.employment_type || null) as EmploymentType | null,
    source_url: values.source_url.trim() || null,
  };
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export default function JobsSection({ accessToken }: JobsSectionProps) {
  const [jobs, setJobs] = useState<SavedJobResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [values, setValues] = useState<JobFormValues>(EMPTY_FORM);
  // One flag per in-flight operation, each also used to DISABLE its own
  // control — the duplicate-submit guard this section needs, and the
  // same pattern ProfileForm and SkillsSection use.
  const [saving, setSaving] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValues, setEditValues] = useState<JobFormValues>(EMPTY_FORM);
  const [busyId, setBusyId] = useState<string | null>(null);
  // Two-step delete instead of window.confirm: a native dialog cannot be
  // driven in tests and is not reliably announced to screen readers.
  const [confirmingId, setConfirmingId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setJobs(await listSavedJobs(accessToken));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "something went wrong");
    }
  }, [accessToken]);

  // `loading` is only ever cleared, never re-set, so a later refresh
  // never replaces a rendered list with the skeleton, and a failed
  // refresh keeps the last good data on screen.
  useEffect(() => {
    let cancelled = false;
    listSavedJobs(accessToken)
      .then((list) => {
        if (!cancelled) setJobs(list);
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

  async function handleCreate(event: FormEvent) {
    event.preventDefault();
    if (saving) return;

    setSaving(true);
    setError(null);
    try {
      const created = await createSavedJob(accessToken, toRequest(values));
      setJobs((prev) => [created, ...prev]);
      setValues(EMPTY_FORM);
    } catch (err) {
      // A 422 here is the API's own validation message (a bad URL, an
      // over-long description), which is the useful thing to show.
      setError(err instanceof ApiError ? err.message : "something went wrong");
    } finally {
      setSaving(false);
    }
  }

  async function handleUpdate(event: FormEvent, id: string) {
    event.preventDefault();
    if (busyId) return;

    setBusyId(id);
    setError(null);
    try {
      const updated = await updateSavedJob(
        accessToken,
        id,
        toRequest(editValues),
      );
      setJobs((prev) => prev.map((job) => (job.id === id ? updated : job)));
      setEditingId(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "something went wrong");
    } finally {
      setBusyId(null);
    }
  }

  async function handleDelete(id: string) {
    if (busyId) return;

    setBusyId(id);
    setError(null);
    try {
      await deleteSavedJob(accessToken, id);
      setJobs((prev) => prev.filter((job) => job.id !== id));
      setConfirmingId(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "something went wrong");
    } finally {
      setBusyId(null);
    }
  }

  function startEditing(job: SavedJobResponse) {
    setEditingId(job.id);
    setEditValues(toFormValues(job));
    setConfirmingId(null);
    setError(null);
  }

  if (loading) {
    return (
      <p role="status" className="text-sm text-zinc-600 dark:text-zinc-400">
        Loading saved jobs…
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

      <form onSubmit={handleCreate} className="flex flex-col gap-3">
        <JobFields
          idPrefix="new-job"
          values={values}
          onChange={setValues}
          disabled={saving}
        />
        <button
          type="submit"
          disabled={saving || !values.company.trim() || !values.title.trim()}
          className="self-start rounded bg-black px-3 py-2 text-sm text-white disabled:opacity-50 dark:bg-white dark:text-black"
        >
          {saving ? "Saving…" : "Save job"}
        </button>
      </form>

      <button
        type="button"
        onClick={load}
        className="self-start rounded border border-zinc-300 px-3 py-1 text-sm text-black dark:border-zinc-700 dark:text-zinc-50"
      >
        Refresh
      </button>

      {jobs.length === 0 ? (
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          No saved jobs yet. Paste a job description above to save your first
          one.
        </p>
      ) : (
        <ul className="flex flex-col gap-2">
          {jobs.map((job) => (
            <li
              key={job.id}
              className="rounded border border-zinc-300 px-3 py-2 dark:border-zinc-700"
            >
              {editingId === job.id ? (
                <form
                  onSubmit={(event) => handleUpdate(event, job.id)}
                  className="flex flex-col gap-3"
                  aria-label={`Edit ${job.title}`}
                >
                  <JobFields
                    idPrefix={`edit-${job.id}`}
                    values={editValues}
                    onChange={setEditValues}
                    disabled={busyId === job.id}
                  />
                  <div className="flex gap-2">
                    <button
                      type="submit"
                      disabled={busyId === job.id}
                      className="rounded bg-black px-3 py-1 text-sm text-white disabled:opacity-50 dark:bg-white dark:text-black"
                    >
                      {busyId === job.id ? "Saving…" : "Save changes"}
                    </button>
                    <button
                      type="button"
                      onClick={() => setEditingId(null)}
                      className="rounded border border-zinc-300 px-3 py-1 text-sm text-black dark:border-zinc-700 dark:text-zinc-50"
                    >
                      Cancel
                    </button>
                  </div>
                </form>
              ) : (
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-black dark:text-zinc-50">
                      {job.title}
                    </p>
                    <p className="truncate text-xs text-zinc-600 dark:text-zinc-400">
                      {job.company}
                      {job.location && ` · ${job.location}`}
                      {job.employment_type &&
                        ` · ${EMPLOYMENT_LABELS[job.employment_type]}`}
                    </p>
                    <p className="text-xs text-zinc-500 dark:text-zinc-500">
                      Saved {formatDate(job.created_at)}
                    </p>
                    {job.source_url && (
                      // rel="noreferrer" so the posting never learns
                      // where the click came from.
                      <a
                        href={job.source_url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-xs text-blue-700 underline dark:text-blue-400"
                      >
                        View posting
                      </a>
                    )}
                  </div>
                  <div className="flex shrink-0 gap-2">
                    <button
                      type="button"
                      onClick={() => startEditing(job)}
                      className="rounded border border-zinc-300 px-2 py-1 text-xs text-black dark:border-zinc-700 dark:text-zinc-50"
                    >
                      Edit
                    </button>
                    {confirmingId === job.id ? (
                      <>
                        <button
                          type="button"
                          disabled={busyId === job.id}
                          onClick={() => handleDelete(job.id)}
                          className="rounded border border-red-300 px-2 py-1 text-xs text-red-700 disabled:opacity-50 dark:border-red-800 dark:text-red-400"
                        >
                          {busyId === job.id ? "Deleting…" : "Confirm delete"}
                        </button>
                        <button
                          type="button"
                          onClick={() => setConfirmingId(null)}
                          className="rounded border border-zinc-300 px-2 py-1 text-xs text-black dark:border-zinc-700 dark:text-zinc-50"
                        >
                          Cancel
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        onClick={() => setConfirmingId(job.id)}
                        className="rounded border border-red-300 px-2 py-1 text-xs text-red-700 dark:border-red-800 dark:text-red-400"
                      >
                        Delete
                      </button>
                    )}
                  </div>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** The create and edit forms are the same fields, so they are one
 * component — two copies would drift. `idPrefix` keeps every id unique
 * on a page that can show several of these at once, which is what makes
 * the label/input association actually work. */
function JobFields({
  idPrefix,
  values,
  onChange,
  disabled,
}: {
  idPrefix: string;
  values: JobFormValues;
  onChange: (next: JobFormValues) => void;
  disabled: boolean;
}) {
  const set = (field: keyof JobFormValues) => (value: string) =>
    onChange({ ...values, [field]: value });

  const inputClass =
    "rounded border border-zinc-300 px-3 py-2 text-sm dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-50";

  return (
    <>
      <div className="flex flex-col gap-1">
        <label
          htmlFor={`${idPrefix}-company`}
          className="text-sm font-medium text-black dark:text-zinc-50"
        >
          Company
        </label>
        <input
          id={`${idPrefix}-company`}
          value={values.company}
          onChange={(event) => set("company")(event.target.value)}
          disabled={disabled}
          required
          maxLength={200}
          className={inputClass}
        />
      </div>

      <div className="flex flex-col gap-1">
        <label
          htmlFor={`${idPrefix}-title`}
          className="text-sm font-medium text-black dark:text-zinc-50"
        >
          Job title
        </label>
        <input
          id={`${idPrefix}-title`}
          value={values.title}
          onChange={(event) => set("title")(event.target.value)}
          disabled={disabled}
          required
          maxLength={200}
          className={inputClass}
        />
      </div>

      <div className="flex flex-col gap-1">
        <label
          htmlFor={`${idPrefix}-location`}
          className="text-sm font-medium text-black dark:text-zinc-50"
        >
          Location
        </label>
        <input
          id={`${idPrefix}-location`}
          value={values.location}
          onChange={(event) => set("location")(event.target.value)}
          disabled={disabled}
          maxLength={200}
          className={inputClass}
        />
      </div>

      <div className="flex flex-col gap-1">
        <label
          htmlFor={`${idPrefix}-employment_type`}
          className="text-sm font-medium text-black dark:text-zinc-50"
        >
          Employment type
        </label>
        <select
          id={`${idPrefix}-employment_type`}
          value={values.employment_type}
          onChange={(event) => set("employment_type")(event.target.value)}
          disabled={disabled}
          className={inputClass}
        >
          <option value="">Not set</option>
          {EMPLOYMENT_TYPES.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>

      <div className="flex flex-col gap-1">
        <label
          htmlFor={`${idPrefix}-source_url`}
          className="text-sm font-medium text-black dark:text-zinc-50"
        >
          Link to posting
        </label>
        <input
          id={`${idPrefix}-source_url`}
          type="url"
          value={values.source_url}
          onChange={(event) => set("source_url")(event.target.value)}
          disabled={disabled}
          aria-describedby={`${idPrefix}-source_url-hint`}
          className={inputClass}
        />
        <p
          id={`${idPrefix}-source_url-hint`}
          className="text-xs text-zinc-500 dark:text-zinc-500"
        >
          Optional. We store the link so you can find the posting again — we
          never open it.
        </p>
      </div>

      <div className="flex flex-col gap-1">
        <label
          htmlFor={`${idPrefix}-description`}
          className="text-sm font-medium text-black dark:text-zinc-50"
        >
          Job description
        </label>
        <textarea
          id={`${idPrefix}-description`}
          value={values.description}
          onChange={(event) => set("description")(event.target.value)}
          disabled={disabled}
          required
          rows={6}
          maxLength={MAX_DESCRIPTION_LENGTH}
          aria-describedby={`${idPrefix}-description-hint`}
          className={inputClass}
        />
        <p
          id={`${idPrefix}-description-hint`}
          className="text-xs text-zinc-500 dark:text-zinc-500"
        >
          Paste the posting. Stored exactly as you enter it.
        </p>
      </div>
    </>
  );
}
