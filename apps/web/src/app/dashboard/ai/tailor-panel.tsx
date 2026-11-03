"use client";

import { useEffect, useState, useSyncExternalStore } from "react";

import {
  ApiError,
  listResumes,
  listSavedJobs,
  type ResumeResponse,
  type SavedJobResponse,
} from "@/lib/api-client";
import { tailorResume, type TailorResult } from "@/lib/ai-client";
import {
  hasAiKeyServerSnapshot,
  hasAiKeySnapshot,
  subscribeAiSettings,
} from "@/lib/ai-settings";

const MISSING_KEY = "Add your OpenRouter key in Settings";

function toMarkdown(result: TailorResult): string {
  const lines = [
    `# Resume tailored for ${result.job_title} at ${result.company}`,
    "",
    `_From ${result.resume_filename}. CareerLens match: ${result.match_score}/100._`,
    "",
    "## Summary",
    "",
    result.summary,
    "",
    "## Bullets",
    "",
  ];
  for (const bullet of result.bullets) {
    lines.push(`- ${bullet.rewritten}`);
    lines.push(`  - Original: ${bullet.original}`);
  }
  lines.push("", "## Keywords to weave in", "");
  lines.push(
    result.keywords.length ? result.keywords.join(", ") : "None suggested.",
  );
  lines.push("", "## Gaps (not on your resume, do not claim them)", "");
  if (result.missing_skills.length) {
    for (const skill of result.missing_skills) lines.push(`- ${skill}`);
  } else {
    lines.push("None found.");
  }
  lines.push("", "## ATS note", "", result.ats_note, "");
  return lines.join("\n");
}

function slug(text: string): string {
  return (
    text
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "")
      .slice(0, 48) || "job"
  );
}

function Chip({
  children,
  tone,
}: {
  children: string;
  tone: "accent" | "warning";
}) {
  const styles =
    tone === "accent"
      ? "border-accent/25 bg-accent/10 text-accent"
      : "border-warning/30 bg-warning/10 text-warning";
  return (
    <span
      className={`rounded-full border px-2.5 py-1 text-xs font-medium ${styles}`}
    >
      {children}
    </span>
  );
}

function SectionTitle({ children }: { children: string }) {
  return (
    <h3 className="text-xs font-semibold uppercase tracking-wider text-muted">
      {children}
    </h3>
  );
}

export default function TailorPanel({ accessToken }: { accessToken: string }) {
  const hasKey = useSyncExternalStore(
    subscribeAiSettings,
    hasAiKeySnapshot,
    hasAiKeyServerSnapshot,
  );
  const [jobs, setJobs] = useState<SavedJobResponse[] | null>(null);
  const [resumes, setResumes] = useState<ResumeResponse[]>([]);
  const [jobId, setJobId] = useState("");
  const [resumeId, setResumeId] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<TailorResult | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let cancelled = false;
    Promise.all([listSavedJobs(accessToken), listResumes(accessToken)])
      .then(([savedJobs, allResumes]) => {
        if (cancelled) return;
        setJobs(savedJobs);
        setResumes(allResumes.filter((r) => r.status === "succeeded"));
        setJobId((current) => current || savedJobs[0]?.id || "");
      })
      .catch((err) => {
        if (!cancelled) {
          setLoadError(
            err instanceof ApiError ? err.message : "something went wrong",
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [accessToken]);

  async function onTailor() {
    if (!jobId || pending) return;
    setPending(true);
    setError(null);
    setCopied(false);
    try {
      setResult(await tailorResume(accessToken, jobId, resumeId || null));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "something went wrong");
    } finally {
      setPending(false);
    }
  }

  async function onCopy() {
    if (!result) return;
    try {
      await navigator.clipboard.writeText(toMarkdown(result));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setError("Could not copy. Use Download .md instead.");
    }
  }

  function onDownload() {
    if (!result) return;
    const blob = new Blob([toMarkdown(result)], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `resume-${slug(result.company)}-${slug(result.job_title)}.md`;
    link.click();
    // Revoked a moment later: revoking synchronously can cancel the
    // download in some browsers.
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  const noJobs = jobs !== null && jobs.length === 0;
  const selectClass =
    "w-full rounded-xl border border-border px-3 py-2 text-sm text-foreground";

  return (
    <section
      aria-labelledby="tailor-heading"
      className="rounded-2xl border border-border bg-surface p-5 shadow-[var(--shadow)] backdrop-blur-xl sm:p-6"
    >
      <h2
        id="tailor-heading"
        className="text-base font-semibold tracking-tight text-foreground"
      >
        Resume tailor
      </h2>
      <p className="mt-1 text-xs text-muted">
        Rewrites your existing bullets in the job&apos;s language. It never adds
        experience you do not have: missing skills are shown as gaps.
      </p>

      {!hasKey && (
        <p className="mt-4 rounded-xl border border-border bg-surface-2 px-3 py-2 text-xs text-muted">
          Tailoring needs your OpenRouter key.{" "}
          <a
            href="#settings"
            className="font-semibold text-accent underline underline-offset-2"
          >
            Add it in Settings
          </a>
        </p>
      )}

      {loadError && (
        <p role="alert" className="mt-4 text-xs text-danger">
          {loadError}
        </p>
      )}

      {noJobs ? (
        <p className="mt-4 text-sm text-muted">
          Save a job first, then come back to tailor your resume to it.
        </p>
      ) : (
        <div className="mt-4 grid gap-3 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
          <div className="flex flex-col gap-1">
            <label
              htmlFor="tailor-job"
              className="text-xs font-medium text-foreground"
            >
              Saved job
            </label>
            <select
              id="tailor-job"
              value={jobId}
              onChange={(event) => setJobId(event.target.value)}
              disabled={jobs === null}
              className={selectClass}
            >
              {jobs === null && <option value="">Loading...</option>}
              {jobs?.map((job) => (
                <option key={job.id} value={job.id}>
                  {job.title} at {job.company}
                </option>
              ))}
            </select>
          </div>
          <div className="flex flex-col gap-1">
            <label
              htmlFor="tailor-resume"
              className="text-xs font-medium text-foreground"
            >
              Resume
            </label>
            <select
              id="tailor-resume"
              value={resumeId}
              onChange={(event) => setResumeId(event.target.value)}
              className={selectClass}
            >
              <option value="">Latest processed resume</option>
              {resumes.map((resume) => (
                <option key={resume.id} value={resume.id}>
                  {resume.original_filename}
                </option>
              ))}
            </select>
          </div>
          <button
            type="button"
            onClick={() => void onTailor()}
            disabled={!jobId || pending}
            className="h-[38px] rounded-xl bg-brand px-4 text-sm font-semibold text-accent-foreground shadow-sm hover:opacity-90 disabled:opacity-50"
          >
            {pending ? "Tailoring..." : "Tailor"}
          </button>
        </div>
      )}

      {pending && (
        <p role="status" className="mt-4 text-xs text-muted">
          Reading your resume and the posting. This can take up to a minute.
        </p>
      )}

      {error && (
        <p
          role="alert"
          className="mt-4 rounded-xl border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger"
        >
          {error}
          {error === MISSING_KEY && (
            <>
              {" "}
              <a href="#settings" className="font-semibold underline">
                Open Settings
              </a>
            </>
          )}
        </p>
      )}

      {result && (
        <div className="mt-6 space-y-6">
          <div className="flex flex-wrap items-start justify-between gap-3 rounded-2xl border border-border bg-surface-2/70 p-4">
            <div className="min-w-0">
              <p className="text-sm font-semibold text-foreground">
                {result.job_title} at {result.company}
              </p>
              <p className="mt-0.5 text-xs text-muted">
                From {result.resume_filename} with{" "}
                <span className="font-mono">{result.model}</span>
              </p>
            </div>
            <div className="flex items-center gap-2">
              <span className="rounded-full bg-accent/10 px-2.5 py-1 text-xs font-semibold text-accent">
                Match {result.match_score}/100
              </span>
              <button
                type="button"
                onClick={() => void onCopy()}
                className="rounded-lg border border-border bg-surface px-2.5 py-1 text-xs font-medium text-foreground hover:border-accent/50"
              >
                {copied ? "Copied" : "Copy as Markdown"}
              </button>
              <button
                type="button"
                onClick={onDownload}
                className="rounded-lg border border-border bg-surface px-2.5 py-1 text-xs font-medium text-foreground hover:border-accent/50"
              >
                Download .md
              </button>
            </div>
          </div>

          <div className="space-y-2">
            <SectionTitle>Summary</SectionTitle>
            <p className="text-sm leading-relaxed text-foreground">
              {result.summary}
            </p>
          </div>

          <div className="space-y-3">
            <SectionTitle>Bullets, before and after</SectionTitle>
            {result.bullets.length === 0 && (
              <p className="text-sm text-muted">
                No bullets were worth rewriting for this job.
              </p>
            )}
            {result.bullets.map((bullet, index) => (
              <div
                key={index}
                className="grid gap-2 rounded-2xl border border-border p-3 sm:grid-cols-2"
              >
                <div className="rounded-xl bg-surface-2 p-3">
                  <p className="text-[11px] font-semibold uppercase tracking-wider text-muted">
                    Before
                  </p>
                  <p className="mt-1 text-sm text-muted">{bullet.original}</p>
                </div>
                <div className="rounded-xl border border-accent/25 bg-accent/5 p-3">
                  <p className="text-[11px] font-semibold uppercase tracking-wider text-accent">
                    After
                  </p>
                  <p className="mt-1 text-sm text-foreground">
                    {bullet.rewritten}
                  </p>
                </div>
                {bullet.why && (
                  <p className="text-xs text-muted sm:col-span-2">
                    {bullet.why}
                  </p>
                )}
              </div>
            ))}
          </div>

          <div className="grid gap-6 sm:grid-cols-2">
            <div className="space-y-2">
              <SectionTitle>Keywords to weave in</SectionTitle>
              <div className="flex flex-wrap gap-1.5">
                {result.keywords.length === 0 ? (
                  <span className="text-sm text-muted">None suggested.</span>
                ) : (
                  result.keywords.map((keyword) => (
                    <Chip key={keyword} tone="accent">
                      {keyword}
                    </Chip>
                  ))
                )}
              </div>
            </div>
            <div className="space-y-2">
              <SectionTitle>Gaps, not on your resume</SectionTitle>
              <div className="flex flex-wrap gap-1.5">
                {result.missing_skills.length === 0 ? (
                  <span className="text-sm text-success">None found.</span>
                ) : (
                  result.missing_skills.map((skill) => (
                    <Chip key={skill} tone="warning">
                      {skill}
                    </Chip>
                  ))
                )}
              </div>
              {result.missing_skills.length > 0 && (
                <p className="text-xs text-muted">
                  Learn these or show them in a project before you claim them.
                </p>
              )}
            </div>
          </div>

          <div className="space-y-2">
            <SectionTitle>ATS fit</SectionTitle>
            <p className="rounded-xl border border-border bg-surface-2/70 p-3 text-sm leading-relaxed text-foreground">
              {result.ats_note}
            </p>
          </div>
        </div>
      )}
    </section>
  );
}
