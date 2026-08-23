"use client";

import { useEffect, useRef, useState, type ChangeEvent } from "react";

import {
  ApiError,
  deleteResume,
  listResumes,
  uploadResume,
  type ResumeResponse,
  type ResumeStatus,
} from "@/lib/api-client";

interface ResumeSectionProps {
  accessToken: string;
}

// How often to re-fetch the list while any resume is still
// queued/processing (Prompt 2.2's extraction worker). Frequent enough
// to feel responsive for a job that typically finishes in well under a
// few seconds, without hammering the API while it's not needed — the
// polling effect below stops entirely once nothing is pending.
const POLL_INTERVAL_MS = 2000;

const PENDING_STATUSES: ResumeStatus[] = ["queued", "processing"];

const STATUS_LABELS: Record<ResumeStatus, string> = {
  queued: "Queued",
  processing: "Processing…",
  succeeded: "Ready",
  failed: "Failed",
};

const STATUS_CLASSES: Record<ResumeStatus, string> = {
  queued: "text-zinc-500 dark:text-zinc-400",
  processing: "text-blue-600 dark:text-blue-400",
  succeeded: "text-green-700 dark:text-green-400",
  failed: "text-red-700 dark:text-red-400",
};

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(1)} KB`;
  return `${(kb / 1024).toFixed(1)} MB`;
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export default function ResumeSection({ accessToken }: ResumeSectionProps) {
  const [resumes, setResumes] = useState<ResumeResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const errorRef = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    let cancelled = false;
    listResumes(accessToken)
      .then((list) => {
        if (!cancelled) setResumes(list);
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

  // Polls the list while any resume is still queued/processing, and
  // stops as soon as none are (including immediately, if nothing ever
  // was) — re-evaluated whenever `resumes` changes, so a newly
  // uploaded resume (or a delete) correctly starts/stops this again.
  // Silently retries on a transient poll failure rather than
  // surfacing a page-level error for it — the next tick tries again.
  useEffect(() => {
    const hasPending = resumes.some((resume) =>
      PENDING_STATUSES.includes(resume.status),
    );
    if (!hasPending) return;

    let cancelled = false;
    const interval = setInterval(() => {
      listResumes(accessToken)
        .then((list) => {
          if (!cancelled) setResumes(list);
        })
        .catch(() => undefined);
    }, POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [accessToken, resumes]);

  // Uploads as soon as a file is picked — no separate "confirm" step —
  // simplest flow the native file input supports on its own.
  async function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;

    setUploading(true);
    setError(null);
    try {
      const created = await uploadResume(accessToken, file);
      setResumes((prev) => [created, ...prev]);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "something went wrong");
      queueMicrotask(() => errorRef.current?.focus());
    } finally {
      setUploading(false);
      // Reset so selecting the exact same file again still fires onChange.
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
    }
  }

  async function handleDelete(id: string) {
    setError(null);
    try {
      await deleteResume(accessToken, id);
      setResumes((prev) => prev.filter((resume) => resume.id !== id));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "something went wrong");
    }
  }

  if (loading) {
    return (
      <p role="status" className="text-sm text-zinc-600 dark:text-zinc-400">
        Loading resumes…
      </p>
    );
  }

  return (
    <div className="flex w-full flex-col gap-4 text-left">
      {error && (
        <p
          id="resume-section-error"
          ref={errorRef}
          role="alert"
          tabIndex={-1}
          className="rounded bg-red-100 px-3 py-2 text-sm text-red-800"
        >
          {error}
        </p>
      )}

      <div className="flex flex-col gap-1">
        <label
          htmlFor="resume-upload"
          className="text-sm font-medium text-black dark:text-zinc-50"
        >
          Upload resume
        </label>
        <input
          id="resume-upload"
          ref={fileInputRef}
          type="file"
          accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
          disabled={uploading}
          onChange={handleFileChange}
          aria-describedby="resume-upload-hint"
          className="rounded border border-zinc-300 px-3 py-2 text-sm dark:border-zinc-700 dark:bg-zinc-900"
        />
        <p
          id="resume-upload-hint"
          className="text-xs text-zinc-500 dark:text-zinc-500"
        >
          {uploading ? "Uploading…" : "PDF or DOCX, up to 5 MB."}
        </p>
      </div>

      {resumes.length === 0 ? (
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          No resumes uploaded yet.
        </p>
      ) : (
        <ul className="flex flex-col gap-2">
          {resumes.map((resume) => (
            <li
              key={resume.id}
              className="flex items-center justify-between gap-3 rounded border border-zinc-300 px-3 py-2 dark:border-zinc-700"
            >
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-black dark:text-zinc-50">
                  {resume.original_filename}
                </p>
                <p className="text-xs text-zinc-500 dark:text-zinc-500">
                  {formatFileSize(resume.file_size_bytes)} ·{" "}
                  <span className={STATUS_CLASSES[resume.status]}>
                    {STATUS_LABELS[resume.status]}
                  </span>{" "}
                  · {formatDate(resume.created_at)}
                </p>
                {resume.status === "failed" && resume.error_message && (
                  <p className="mt-1 text-xs text-red-700 dark:text-red-400">
                    {resume.error_message}
                  </p>
                )}
              </div>
              <button
                type="button"
                onClick={() => handleDelete(resume.id)}
                className="shrink-0 rounded border border-red-300 px-3 py-1 text-sm text-red-700 dark:border-red-800 dark:text-red-400"
              >
                Delete
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
