"use client";

import { ArrowRight } from "lucide-react";
import { useEffect, useState } from "react";

import { getSkillProfile, type SkillProfileSummary } from "@/lib/api-client";

import { VIEWS, type DashboardView } from "./views";

interface OverviewProps {
  accessToken: string;
  /** Saved-job count reported by JobsSection; `null` until it arrives. */
  jobCount: number | null;
  /** Same key the skill profile listens to, so the numbers here move
   * when resume/GitHub work lands or a skill is reviewed. */
  refreshKey: number;
}

const SHORTCUTS: DashboardView[] = [
  "resume",
  "skills",
  "jobs",
  "roadmap",
  "assistant",
  "tailor",
];

function nextStep(
  summary: SkillProfileSummary | null,
  jobCount: number | null,
): { text: string; view: DashboardView; action: string } | null {
  if (!summary || jobCount === null) return null;
  if (summary.total === 0) {
    return {
      text: "Upload a resume or connect GitHub so CareerLens can find your skills.",
      view: "resume",
      action: "Add a source",
    };
  }
  if (summary.suggested > 0) {
    return {
      text: `${summary.suggested} suggested ${summary.suggested === 1 ? "skill is" : "skills are"} waiting for your review.`,
      view: "skills",
      action: "Review skills",
    };
  }
  if (jobCount === 0) {
    return {
      text: "Save a job posting to see how you match it and what is missing.",
      view: "jobs",
      action: "Save a job",
    };
  }
  return {
    text: "Your skills are reviewed and your jobs are saved. Build a plan for the gaps.",
    view: "roadmap",
    action: "Open roadmap",
  };
}

export default function Overview({
  accessToken,
  jobCount,
  refreshKey,
}: OverviewProps) {
  const [summary, setSummary] = useState<SkillProfileSummary | null>(null);

  useEffect(() => {
    let cancelled = false;
    getSkillProfile(accessToken)
      .then((profile) => {
        if (!cancelled) setSummary(profile.summary);
      })
      // The stat cards keep their placeholder; the Skills view shows the error.
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [accessToken, refreshKey]);

  const stats: { label: string; value: number | null; hint: string }[] = [
    { label: "Saved jobs", value: jobCount, hint: "Roles you are tracking" },
    {
      label: "Confirmed skills",
      value: summary?.confirmed ?? null,
      hint: "Skills you have accepted",
    },
    {
      label: "Awaiting review",
      value: summary?.suggested ?? null,
      hint: "Suggested from your evidence",
    },
    {
      label: "Multi-source skills",
      value: summary?.multi_source ?? null,
      hint: "Backed by more than one source",
    },
  ];

  const step = nextStep(summary, jobCount);

  return (
    <div className="flex flex-col gap-6">
      <section className="card relative overflow-hidden p-6 sm:p-8">
        <div
          aria-hidden="true"
          className="bg-brand absolute -top-24 -right-16 size-64 rounded-full opacity-20 blur-3xl"
        />
        <p className="relative text-sm font-medium text-accent">Welcome back</p>
        <h2 className="relative mt-1 text-2xl font-semibold tracking-tight">
          Your career snapshot
        </h2>
        <p className="relative mt-2 max-w-2xl text-sm text-muted">
          Everything here is counted from your own evidence and saved jobs.
        </p>
        {step && (
          <div className="relative mt-5 flex flex-col gap-3 rounded-xl border border-accent/25 bg-accent/10 p-4 sm:flex-row sm:items-center sm:justify-between">
            <p className="text-sm text-foreground">{step.text}</p>
            <a
              href={`#${step.view}`}
              className="inline-flex shrink-0 items-center justify-center gap-1.5 rounded-lg bg-accent px-3.5 py-2 text-sm font-medium text-accent-foreground shadow-sm hover:bg-accent/90"
            >
              {step.action}
              <ArrowRight className="size-4" aria-hidden="true" />
            </a>
          </div>
        )}
      </section>

      <dl className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
        {stats.map((stat) => (
          <div key={stat.label} className="card p-4 sm:p-5">
            <dt className="text-xs font-medium text-muted">{stat.label}</dt>
            <dd className="mt-2 text-3xl font-semibold tracking-tight tabular-nums">
              {stat.value ?? "…"}
            </dd>
            <dd className="mt-1 text-xs text-muted">{stat.hint}</dd>
          </div>
        ))}
      </dl>

      <section>
        <h2 className="mb-3 text-sm font-semibold text-muted">Jump to</h2>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {VIEWS.filter((view) => SHORTCUTS.includes(view.id)).map(
            ({ id, label, description, icon: Icon }) => (
              <a
                key={id}
                href={`#${id}`}
                className="card group flex items-start gap-4 p-4 hover:border-accent/40 sm:p-5"
              >
                <span className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-accent/10 text-accent">
                  <Icon className="size-5" aria-hidden="true" />
                </span>
                <span className="min-w-0">
                  <span className="flex items-center gap-1 font-medium text-foreground">
                    {label}
                    <ArrowRight
                      className="size-3.5 opacity-0 transition-opacity group-hover:opacity-100"
                      aria-hidden="true"
                    />
                  </span>
                  <span className="mt-0.5 block text-sm text-muted">
                    {description}
                  </span>
                </span>
              </a>
            ),
          )}
        </div>
      </section>
    </div>
  );
}
