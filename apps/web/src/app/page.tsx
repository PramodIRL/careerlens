"use client";

import {
  ArrowRight,
  BadgeCheck,
  Bot,
  FileText,
  Route,
  Scale,
  ScanSearch,
  WandSparkles,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import {
  STATUS_LABEL,
  STATUS_STYLES,
  type HealthStatus,
} from "@/lib/health-status";

import BrandMark from "./brand-mark";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const FEATURES: { icon: LucideIcon; title: string; body: string }[] = [
  {
    icon: BadgeCheck,
    title: "Evidence-backed skills",
    body: "Every skill links back to the resume line or repository that shows it, so you know what you can actually prove.",
  },
  {
    icon: ScanSearch,
    title: "Explainable job matches",
    body: "Paste a job and see a match score you can read: which requirements you meet, and the evidence behind each one.",
  },
  {
    icon: Scale,
    title: "Gaps and eligibility",
    body: "Find the missing skills, the weak evidence and the hard requirements like degree or location before you apply.",
  },
  {
    icon: Route,
    title: "A learning roadmap",
    body: "Turn your gaps into a day-by-day plan sized to the hours you have, ordered by what your target jobs need most.",
  },
  {
    icon: Bot,
    title: "AI career assistant",
    body: "Ask questions about your profile, your matches and your plan, and get answers grounded in your own data.",
  },
  {
    icon: WandSparkles,
    title: "Resume tailor",
    body: "Rework your resume for a specific job, using only experience your evidence supports.",
  },
];

const STEPS = [
  {
    title: "Bring your evidence",
    body: "Upload a resume and connect a public GitHub account.",
  },
  {
    title: "Review your skills",
    body: "Confirm or reject what was found. Nothing counts until you say so.",
  },
  {
    title: "Pick your jobs",
    body: "Save the roles you want and get matches, gaps and a roadmap for them.",
  },
];

const PREVIEW_SKILLS: { name: string; source: string; met: boolean }[] = [
  { name: "Python", source: "Resume + GitHub", met: true },
  { name: "PostgreSQL", source: "GitHub", met: true },
  { name: "REST APIs", source: "Resume", met: true },
  { name: "Kubernetes", source: "No evidence yet", met: false },
];

export default function Home() {
  const [status, setStatus] = useState<HealthStatus>("checking");
  const [detail, setDetail] = useState("");

  useEffect(() => {
    let cancelled = false;

    async function checkHealth() {
      try {
        const response = await fetch(`${API_URL}/api/v1/health`, {
          cache: "no-store",
        });
        if (!response.ok) {
          throw new Error(`API responded with status ${response.status}`);
        }
        const data = (await response.json()) as unknown;
        if (!cancelled) {
          setStatus("ok");
          setDetail(JSON.stringify(data));
        }
      } catch (error) {
        if (!cancelled) {
          setStatus("error");
          setDetail(error instanceof Error ? error.message : "Unknown error");
        }
      }
    }

    void checkHealth();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="flex min-h-screen flex-col overflow-x-clip">
      <header className="sticky top-0 z-20 border-b border-border bg-background/70 backdrop-blur-xl">
        <div className="mx-auto flex h-16 w-full max-w-6xl items-center justify-between gap-4 px-4 sm:px-6">
          <BrandMark />
          <nav
            aria-label="Account"
            className="flex items-center gap-1 text-sm sm:gap-2"
          >
            <a
              href="#features"
              className="hidden rounded-lg px-3 py-2 text-muted hover:text-foreground md:inline-block"
            >
              Features
            </a>
            <a
              href="#how-it-works"
              className="hidden rounded-lg px-3 py-2 text-muted hover:text-foreground md:inline-block"
            >
              How it works
            </a>
            <Link
              href="/dashboard"
              className="hidden rounded-lg px-3 py-2 text-muted hover:text-foreground sm:inline-block"
            >
              Dashboard
            </Link>
            <Link
              href="/login"
              className="rounded-lg px-3 py-2 text-foreground hover:bg-surface-2"
            >
              Log in
            </Link>
            <Link
              href="/register"
              className="rounded-lg bg-accent px-3.5 py-2 font-medium text-accent-foreground shadow-sm shadow-accent/30 hover:bg-accent/90"
            >
              Register
            </Link>
          </nav>
        </div>
      </header>

      <main className="flex-1">
        <section className="mx-auto grid w-full max-w-6xl items-center gap-12 px-4 pt-16 pb-20 sm:px-6 lg:grid-cols-[1.1fr_1fr] lg:pt-24">
          <div>
            <p className="inline-flex items-center gap-2 rounded-full border border-border bg-surface px-3 py-1 text-xs font-medium text-muted">
              <span className="bg-brand size-1.5 rounded-full" />
              Career guidance you can check
            </p>
            <h1 className="mt-6 text-4xl font-semibold tracking-tight text-balance sm:text-5xl lg:text-6xl">
              See exactly where you stand for the{" "}
              <span className="text-brand">job you want</span>.
            </h1>
            <p className="mt-6 max-w-xl text-base leading-relaxed text-muted sm:text-lg">
              CareerLens reads your resume and GitHub and turns them into skills
              backed by real evidence. Then it shows how you match each job,
              what is missing, and a learning roadmap to close the gap, with an
              AI career assistant and resume tailor on hand.
            </p>
            <div className="mt-8 flex flex-col gap-3 sm:flex-row">
              <Link
                href="/register"
                className="inline-flex items-center justify-center gap-2 rounded-xl bg-accent px-5 py-3 text-sm font-medium text-accent-foreground shadow-lg shadow-accent/30 hover:bg-accent/90"
              >
                Create your free account
                <ArrowRight className="size-4" aria-hidden="true" />
              </Link>
              <Link
                href="/login"
                className="inline-flex items-center justify-center rounded-xl border border-border bg-surface px-5 py-3 text-sm font-medium text-foreground hover:bg-surface-2"
              >
                Log in
              </Link>
            </div>
          </div>

          <div className="relative" aria-hidden="true">
            <div className="bg-brand absolute -inset-6 rounded-[2rem] opacity-20 blur-3xl" />
            <div className="card relative p-5 sm:p-6">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <p className="text-xs font-medium tracking-wide text-muted uppercase">
                    Example match
                  </p>
                  <p className="mt-1 truncate text-lg font-semibold">
                    Backend Engineer
                  </p>
                  <p className="text-sm text-muted">Acme Robotics</p>
                </div>
                <div className="flex size-16 shrink-0 items-center justify-center rounded-full border-4 border-accent/30 text-lg font-semibold text-accent">
                  75%
                </div>
              </div>
              <ul className="mt-5 flex flex-col gap-2">
                {PREVIEW_SKILLS.map((skill) => (
                  <li
                    key={skill.name}
                    className="flex items-center justify-between gap-3 rounded-xl border border-border bg-surface-2/50 px-3 py-2.5"
                  >
                    <span className="flex items-center gap-2 text-sm font-medium">
                      <span
                        className={`size-2 rounded-full ${skill.met ? "bg-success" : "bg-warning"}`}
                      />
                      {skill.name}
                    </span>
                    <span className="text-xs text-muted">{skill.source}</span>
                  </li>
                ))}
              </ul>
              <div className="mt-4 flex items-center gap-2 rounded-xl bg-accent/10 px-3 py-2.5 text-xs text-accent">
                <Route className="size-4 shrink-0" />
                Next: a two-week Kubernetes plan, one hour a day.
              </div>
            </div>
          </div>
        </section>

        <section
          id="features"
          className="mx-auto w-full max-w-6xl scroll-mt-20 px-4 py-16 sm:px-6"
        >
          <div className="max-w-2xl">
            <h2 className="text-3xl font-semibold tracking-tight">
              Everything between your experience and your next role
            </h2>
            <p className="mt-3 text-muted">
              Each answer shows its working, so you can trust it or challenge
              it.
            </p>
          </div>
          <div className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {FEATURES.map(({ icon: Icon, title, body }) => (
              <article
                key={title}
                className="card p-6 transition-colors hover:border-accent/40"
              >
                <span className="flex size-10 items-center justify-center rounded-xl bg-accent/10 text-accent">
                  <Icon className="size-5" aria-hidden="true" />
                </span>
                <h3 className="mt-4 font-semibold">{title}</h3>
                <p className="mt-2 text-sm leading-relaxed text-muted">
                  {body}
                </p>
              </article>
            ))}
          </div>
        </section>

        <section
          id="how-it-works"
          className="mx-auto w-full max-w-6xl scroll-mt-20 px-4 py-16 sm:px-6"
        >
          <h2 className="text-3xl font-semibold tracking-tight">
            How it works
          </h2>
          <ol className="mt-10 grid gap-4 md:grid-cols-3">
            {STEPS.map((step, index) => (
              <li key={step.title} className="card p-6">
                <span className="bg-brand flex size-8 items-center justify-center rounded-full text-sm font-semibold text-accent-foreground">
                  {index + 1}
                </span>
                <h3 className="mt-4 font-semibold">{step.title}</h3>
                <p className="mt-2 text-sm leading-relaxed text-muted">
                  {step.body}
                </p>
              </li>
            ))}
          </ol>
        </section>

        <section className="mx-auto w-full max-w-6xl px-4 py-16 sm:px-6">
          <div className="card relative overflow-hidden p-8 text-center sm:p-12">
            <div className="bg-brand absolute inset-x-0 -top-24 mx-auto h-48 w-2/3 opacity-25 blur-3xl" />
            <FileText
              className="relative mx-auto size-8 text-accent"
              aria-hidden="true"
            />
            <h2 className="relative mt-4 text-2xl font-semibold tracking-tight sm:text-3xl">
              Start with the resume you already have
            </h2>
            <p className="relative mx-auto mt-3 max-w-lg text-muted">
              It takes a minute to set up, and every result comes with its
              reasons.
            </p>
            <div className="relative mt-8 flex flex-col justify-center gap-3 sm:flex-row">
              <Link
                href="/register"
                className="inline-flex items-center justify-center gap-2 rounded-xl bg-accent px-5 py-3 text-sm font-medium text-accent-foreground shadow-lg shadow-accent/30 hover:bg-accent/90"
              >
                Get started
                <ArrowRight className="size-4" aria-hidden="true" />
              </Link>
              <Link
                href="/login"
                className="inline-flex items-center justify-center rounded-xl border border-border bg-surface px-5 py-3 text-sm font-medium text-foreground hover:bg-surface-2"
              >
                I already have an account
              </Link>
            </div>
          </div>
        </section>
      </main>

      <footer className="border-t border-border">
        <div className="mx-auto flex w-full max-w-6xl flex-col items-center justify-between gap-3 px-4 py-6 text-xs text-muted sm:flex-row sm:px-6">
          <p>CareerLens</p>
          <div
            data-testid="api-health-status"
            title={detail || undefined}
            className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 font-medium ${STATUS_STYLES[status]}`}
          >
            <span className="size-1.5 rounded-full bg-current" />
            {STATUS_LABEL[status]}
          </div>
        </div>
      </footer>
    </div>
  );
}
