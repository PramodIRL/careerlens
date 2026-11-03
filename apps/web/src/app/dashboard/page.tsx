"use client";

import { LogOut } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState, type ReactNode } from "react";

import { useAuth } from "@/lib/auth-context";

import BrandMark from "../brand-mark";
import GitHubSection from "./github-section";
import JobsSection from "./jobs-section";
import RoadmapSection from "./roadmap-section";
import ProfileForm from "./profile-form";
import QualificationsSection from "./qualifications-section";
import ResumeSection from "./resume-section";
import SkillProfileSection from "./skill-profile-section";
import SkillsSection from "./skills-section";
import Overview from "./overview";
import {
  VIEWS,
  resolveHash,
  useLocationHash,
  type DashboardView,
  type ViewConfig,
} from "./views";

export default function DashboardPage() {
  const { status, user, accessToken, logout } = useAuth();
  const router = useRouter();
  const [loggingOut, setLoggingOut] = useState(false);

  // --- Cross-section refresh coordination -----------------------------
  //
  // The five sections below are siblings with no shared state, so the
  // two that know when async work finished (resume extraction, GitHub
  // ingestion) had no way to tell the two that display its results.
  // That, not missing polling, is why a manual Refresh was needed: both
  // producers already poll correctly and stop on a terminal state.
  //
  // TWO counters rather than one, deliberately. A single counter would
  // make SkillsSection refetch after its OWN confirm/reject — data it
  // just updated in place from the mutation's response — costing a
  // redundant request per click. Splitting the signals means each
  // section refetches only for changes it could not already know about:
  //
  //   externalVersion  resume/GitHub work reached a terminal state.
  //                    Both skill sections must refetch.
  //   profileVersion   SkillsSection mutated something locally. Only
  //                    the profile summary needs to catch up.
  //
  // A counter, not a boolean: every bump is a distinct value, so two
  // completions in quick succession cannot collapse into one refetch.
  const [externalVersion, setExternalVersion] = useState(0);
  const [profileVersion, setProfileVersion] = useState(0);
  //   qualificationVersion  the candidate edited their ONE reusable
  //                         qualification profile. Only the saved-job
  //                         eligibility panels care. Kept separate from
  //                         externalVersion so a profile edit does not
  //                         make the skill sections refetch data that
  //                         cannot have changed.
  const [qualificationVersion, setQualificationVersion] = useState(0);
  //   jobsVersion           the saved jobs changed — one was added,
  //                         deleted, reordered or edited. Only the
  //                         roadmap cares, and it is the one signal that
  //                         can make an already-rendered plan describe
  //                         something that is no longer true.
  //
  //                         Deliberately NOT added to the sum below.
  //                         JobsSection must not refetch its own list
  //                         after its own mutation — it already applied
  //                         the result — and the roadmap needs to tell
  //                         "your jobs changed" apart from "your skills
  //                         changed", because only the first invalidates
  //                         a plan it has already drawn.
  const [jobsVersion, setJobsVersion] = useState(0);
  //   jobCount              how many jobs JobsSection currently holds.
  //                         NOT A VERSION and not fetched here: it is
  //                         the list's own length, reported upward by
  //                         the section that already has it, so the
  //                         roadmap can bound its "jobs to prepare for"
  //                         input without issuing a second identical
  //                         `listSavedJobs` on every dashboard load.
  //
  //                         `null` until a list actually arrives, which
  //                         is not the same as zero — the roadmap shows
  //                         "—" for the first and "0 saved jobs" for
  //                         the second.
  const [jobCount, setJobCount] = useState<number | null>(null);

  // Stable identities — these are effect dependencies in the children,
  // so an inline arrow would re-run those effects on every render.
  const handleExternalWorkComplete = useCallback(
    () => setExternalVersion((n) => n + 1),
    [],
  );
  const handleSkillsChanged = useCallback(
    () => setProfileVersion((n) => n + 1),
    [],
  );

  const handleQualificationsChanged = useCallback(
    () => setQualificationVersion((n) => n + 1),
    [],
  );

  const handleJobsChanged = useCallback(() => setJobsVersion((n) => n + 1), []);

  const handleJobCountChange = useCallback(
    (count: number) => setJobCount(count),
    [],
  );

  // Protected navigation: the single redirect authority for leaving this
  // page whenever there's no valid session — covers both a mount-time
  // silent refresh (in AuthProvider) finding no session, and logout()
  // below setting status to "unauthenticated". handleLogout deliberately
  // does not also call router.push itself: a second, independent push to
  // the same href raced with this effect's (two overlapping App Router
  // transitions to /login), which could leave client navigation stuck.
  // See docs/decisions.md.
  useEffect(() => {
    if (status === "unauthenticated") {
      router.push("/login");
    }
  }, [status, router]);

  async function handleLogout() {
    setLoggingOut(true);
    await logout();
  }

  // --- Views ------------------------------------------------------------
  //
  // The open view lives in the URL hash (#jobs, #roadmap, …) so links,
  // reload and back/forward all work. In-page anchors such as
  // #qualifications open the view that holds them and then scroll there,
  // because the element sits inside a view that may have been hidden
  // when the link was followed.
  const { view, anchor } = resolveHash(useLocationHash());
  const ready = status === "authenticated";

  useEffect(() => {
    if (!anchor || !ready) return;
    const target = document.getElementById(anchor);
    target?.scrollIntoView({ block: "start" });
    target?.focus({ preventScroll: true });
  }, [anchor, ready]);

  if (status === "loading") {
    return (
      <main className="flex min-h-screen flex-col items-center justify-center gap-4 p-8">
        <span
          aria-hidden="true"
          className="size-8 animate-spin rounded-full border-2 border-border border-t-accent"
        />
        <p role="status" className="text-sm text-muted">
          Checking your session…
        </p>
      </main>
    );
  }

  if (status !== "authenticated" || !user || !accessToken) {
    // The redirect effect above is already firing; nothing meaningful
    // to show for the brief moment before navigation completes.
    return null;
  }

  const current = VIEWS.find((item) => item.id === view) ?? VIEWS[0];

  // EVERY VIEW STAYS MOUNTED; only the open one is shown. The sections
  // fetch on mount and report to each other through the counters above
  // (JobsSection's count feeds the roadmap, a resume finishing refreshes
  // the skills), so unmounting a hidden view would silently cut those
  // signals and refetch on every tab switch.
  const show = (id: DashboardView) => (view === id ? "" : "hidden");

  return (
    <div className="min-h-screen lg:pl-64">
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-64 flex-col border-r border-border bg-surface/70 backdrop-blur-xl lg:flex">
        <div className="flex h-16 items-center px-5">
          <BrandMark />
        </div>
        <nav
          aria-label="Dashboard"
          className="flex flex-1 flex-col gap-6 px-3 py-4"
        >
          {NAV_GROUPS.map((group) => (
            <div key={group.id}>
              {group.label && (
                <p className="mb-2 px-3 text-[11px] font-semibold tracking-wider text-muted uppercase">
                  {group.label}
                </p>
              )}
              <ul className="flex flex-col gap-0.5">
                {VIEWS.filter((item) => item.group === group.id).map(
                  ({ id, label, icon: Icon }) => (
                    <li key={id}>
                      <a
                        href={`#${id}`}
                        aria-current={view === id ? "page" : undefined}
                        className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium ${
                          view === id
                            ? "bg-accent/12 text-foreground shadow-[inset_2px_0_0_var(--accent)]"
                            : "text-muted hover:bg-surface-2 hover:text-foreground"
                        }`}
                      >
                        <Icon
                          className={`size-4 ${view === id ? "text-accent" : ""}`}
                          aria-hidden="true"
                        />
                        {label}
                      </a>
                    </li>
                  ),
                )}
              </ul>
            </div>
          ))}
        </nav>
      </aside>

      <header className="sticky top-0 z-20 border-b border-border bg-background/75 backdrop-blur-xl">
        <div className="flex h-16 items-center justify-between gap-3 px-4 sm:px-6 lg:px-8">
          <div className="min-w-0 lg:hidden">
            <BrandMark />
          </div>
          <p className="hidden text-sm text-muted lg:block">
            Dashboard <span className="mx-1.5 text-border">/</span>
            <span className="text-foreground">{current.label}</span>
          </p>
          <div className="flex min-w-0 items-center gap-2 sm:gap-3">
            <span className="flex min-w-0 items-center gap-2 rounded-full border border-border bg-surface py-1 pr-3 pl-1">
              <span
                aria-hidden="true"
                className="bg-brand flex size-6 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold text-accent-foreground uppercase"
              >
                {user.email.charAt(0)}
              </span>
              <span className="max-w-[9rem] truncate text-xs font-medium text-foreground sm:max-w-[16rem]">
                {user.email}
              </span>
            </span>
            <button
              type="button"
              onClick={handleLogout}
              disabled={loggingOut}
              className="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-sm font-medium text-foreground hover:bg-surface-2 disabled:opacity-50"
            >
              <LogOut className="size-4" aria-hidden="true" />
              <span className="sr-only sm:not-sr-only">
                {loggingOut ? "Logging out…" : "Log out"}
              </span>
            </button>
          </div>
        </div>
        <nav
          aria-label="Dashboard sections"
          className="no-scrollbar flex gap-1 overflow-x-auto px-4 pb-2.5 sm:px-6 lg:hidden"
        >
          {VIEWS.map(({ id, label, icon: Icon }) => (
            <a
              key={id}
              href={`#${id}`}
              aria-current={view === id ? "page" : undefined}
              className={`flex shrink-0 items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium ${
                view === id
                  ? "bg-accent text-accent-foreground shadow-sm"
                  : "border border-border bg-surface text-muted hover:text-foreground"
              }`}
            >
              <Icon className="size-3.5" aria-hidden="true" />
              {label}
            </a>
          ))}
        </nav>
      </header>

      <main className="mx-auto w-full max-w-6xl px-4 py-6 sm:px-6 sm:py-8 lg:px-8">
        <div className="mb-6 sm:mb-8">
          <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">
            {current.label}
          </h1>
          <p className="mt-1 text-sm text-muted">{current.description}</p>
        </div>

        <div className={show("overview")}>
          <Overview
            accessToken={accessToken}
            jobCount={jobCount}
            refreshKey={externalVersion + profileVersion}
          />
        </div>

        <div
          className={`grid items-start gap-6 lg:grid-cols-2 ${show("profile")}`}
        >
          <Panel
            title="Your profile"
            description="Your headline, location and the roles you are aiming for."
          >
            <ProfileForm accessToken={accessToken} userId={user.id} />
          </Panel>

          <div
            id="qualifications"
            tabIndex={-1}
            className="card min-w-0 scroll-mt-32 p-5 outline-none sm:p-6 lg:scroll-mt-24"
          >
            <PanelHeading
              title="Your qualifications"
              description="Grades, degree and experience, checked against every saved job."
            />
            {/* Declaring a fact here changes the eligibility answer for
                EVERY saved job at once, so it bumps the same counter the
                job panels already listen to. */}
            <QualificationsSection
              accessToken={accessToken}
              onChanged={handleQualificationsChanged}
            />
          </div>
        </div>

        <div
          className={`grid items-start gap-6 lg:grid-cols-2 ${show("resume")}`}
        >
          <Panel
            title="Your resumes"
            description="Upload a PDF or DOCX and CareerLens extracts the skills it shows."
          >
            <ResumeSection
              accessToken={accessToken}
              onWorkComplete={handleExternalWorkComplete}
            />
          </Panel>

          <Panel
            title="Your GitHub"
            description="Connect a public account to add evidence from your repositories."
          >
            <GitHubSection
              accessToken={accessToken}
              onWorkComplete={handleExternalWorkComplete}
            />
          </Panel>
        </div>

        <div className={show("jobs")}>
          <Panel
            title="Your saved jobs"
            description="Add a posting, then open it for the match, gaps and eligibility."
          >
            {/* THREE SIGNALS, ROUTED — not one sum handed to four panels.
                Each key carries only the changes its panel's handler can
                actually observe, which is a fact about what those handlers
                read (see JobsSectionProps):

                  match/gaps   skills moved (`profileVersion`) or async work
                               landed evidence (`externalVersion`). A
                               qualification edit cannot move them.
                  eligibility  the qualification profile moved, or async
                               work landed a resume-derived suggestion.
                               Confirming a skill cannot move it.
                  semantic     only new stored evidence changes it, which
                               is `externalVersion` alone.

                The sum this replaced meant one qualification edit refetched
                every panel on every job — measured at twenty requests where
                five were warranted. */}
            <JobsSection
              accessToken={accessToken}
              matchRefreshKey={externalVersion + profileVersion}
              eligibilityRefreshKey={externalVersion + qualificationVersion}
              semanticRefreshKey={externalVersion}
              onJobsChanged={handleJobsChanged}
              onJobCountChange={handleJobCountChange}
            />
          </Panel>
        </div>

        {/* A roadmap is derived from the saved jobs: the user sets their
            priorities in Jobs, and reads the consequence here. Generated
            only on an explicit click — see RoadmapSection. */}
        <div className={show("roadmap")}>
          {/* `jobsVersion` still says the jobs the plan is ABOUT moved,
              which retires a plan already on screen — unchanged from
              7.1(c). What went away is the section's own `refreshKey`:
              it existed only to re-run a `listSavedJobs` this page now
              supplies as `savedJobCount`, so a skill or qualification
              edit no longer refetches a list neither can change. */}
          <RoadmapSection
            accessToken={accessToken}
            savedJobCount={jobCount}
            jobsVersion={jobsVersion}
          />
        </div>

        <div
          className={`grid items-start gap-6 lg:grid-cols-5 ${show("skills")}`}
        >
          <Panel
            className="lg:col-span-2"
            title="Your skill profile"
            description="What your evidence adds up to."
          >
            {/* Listens to BOTH signals: its counts change when async work
                lands new evidence, and when the user confirms or rejects. */}
            <SkillProfileSection
              accessToken={accessToken}
              refreshKey={externalVersion + profileVersion}
            />
          </Panel>

          <Panel
            className="lg:col-span-3"
            title="Your skills"
            description="Confirm what is yours, reject what is not, or add one by hand."
          >
            <SkillsSection
              accessToken={accessToken}
              refreshKey={externalVersion}
              onChanged={handleSkillsChanged}
            />
          </Panel>
        </div>

        <div className={show("assistant")}>
          <div className="card p-5 sm:p-6">
            <div data-slot="ai-assistant" />
          </div>
        </div>

        <div className={show("tailor")}>
          <div className="card p-5 sm:p-6">
            <div data-slot="resume-tailor" />
          </div>
        </div>

        <div className={show("settings")}>
          <div className="card p-5 sm:p-6">
            <div data-slot="ai-settings" />
          </div>
        </div>
      </main>
    </div>
  );
}

const NAV_GROUPS: { id: ViewConfig["group"]; label: string | null }[] = [
  { id: "workspace", label: null },
  { id: "ai", label: "AI tools" },
  { id: "account", label: "Account" },
];

function PanelHeading({
  title,
  description,
}: {
  title: string;
  description: string;
}) {
  return (
    <div className="mb-5">
      <h2 className="text-lg font-semibold tracking-tight">{title}</h2>
      <p className="mt-0.5 text-sm text-muted">{description}</p>
    </div>
  );
}

function Panel({
  title,
  description,
  className = "",
  children,
}: {
  title: string;
  description: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <section className={`card min-w-0 p-5 sm:p-6 ${className}`}>
      <PanelHeading title={title} description={description} />
      {children}
    </section>
  );
}
