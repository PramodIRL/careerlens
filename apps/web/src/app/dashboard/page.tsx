"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { useAuth } from "@/lib/auth-context";

import GitHubSection from "./github-section";
import JobsSection from "./jobs-section";
import RoadmapSection from "./roadmap-section";
import ProfileForm from "./profile-form";
import QualificationsSection from "./qualifications-section";
import ResumeSection from "./resume-section";
import SkillProfileSection from "./skill-profile-section";
import SkillsSection from "./skills-section";

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

  if (status === "loading") {
    return (
      <main className="flex min-h-screen flex-col items-center justify-center gap-4 bg-zinc-50 p-8 dark:bg-black">
        <p role="status" className="text-sm text-zinc-600 dark:text-zinc-400">
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

  return (
    <main className="flex min-h-screen flex-col items-center gap-6 bg-zinc-50 p-8 dark:bg-black">
      <div className="w-full max-w-sm text-center">
        <h1 className="mb-2 text-2xl font-semibold text-black dark:text-zinc-50">
          Dashboard
        </h1>
        <p className="mb-6 text-sm text-zinc-600 dark:text-zinc-400">
          Signed in as <span className="font-medium">{user.email}</span>
        </p>
        <button
          type="button"
          onClick={handleLogout}
          disabled={loggingOut}
          className="rounded bg-black px-4 py-2 text-white disabled:opacity-50 dark:bg-white dark:text-black"
        >
          {loggingOut ? "Logging out…" : "Log out"}
        </button>
      </div>

      <div className="w-full max-w-sm">
        <h2 className="mb-4 text-lg font-semibold text-black dark:text-zinc-50">
          Your profile
        </h2>
        <ProfileForm accessToken={accessToken} userId={user.id} />
      </div>

      <div className="w-full max-w-sm">
        <h2 className="mb-4 text-lg font-semibold text-black dark:text-zinc-50">
          Your resumes
        </h2>
        <ResumeSection
          accessToken={accessToken}
          onWorkComplete={handleExternalWorkComplete}
        />
      </div>

      <div className="w-full max-w-sm">
        <h2 className="mb-4 text-lg font-semibold text-black dark:text-zinc-50">
          Your GitHub
        </h2>
        <GitHubSection
          accessToken={accessToken}
          onWorkComplete={handleExternalWorkComplete}
        />
      </div>

      <div
        id="qualifications"
        tabIndex={-1}
        className="w-full max-w-sm scroll-mt-4"
      >
        <h2 className="mb-4 text-lg font-semibold text-black dark:text-zinc-50">
          Your qualifications
        </h2>
        {/* Declaring a fact here changes the eligibility answer for
            EVERY saved job at once, so it bumps the same counter the
            job panels already listen to. */}
        <QualificationsSection
          accessToken={accessToken}
          onChanged={handleQualificationsChanged}
        />
      </div>

      <div className="w-full max-w-sm">
        <h2 className="mb-4 text-lg font-semibold text-black dark:text-zinc-50">
          Your saved jobs
        </h2>
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
      </div>

      {/* BELOW the jobs, because a roadmap is derived from them: the
          user sets their priorities above, and reads the consequence
          here. Generated only on an explicit click — see
          RoadmapSection. */}
      <div className="w-full max-w-sm">
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

      <div className="w-full max-w-sm">
        <h2 className="mb-4 text-lg font-semibold text-black dark:text-zinc-50">
          Your skill profile
        </h2>
        {/* Listens to BOTH signals: its counts change when async work
            lands new evidence, and when the user confirms or rejects. */}
        <SkillProfileSection
          accessToken={accessToken}
          refreshKey={externalVersion + profileVersion}
        />
      </div>

      <div className="w-full max-w-sm">
        <h2 className="mb-4 text-lg font-semibold text-black dark:text-zinc-50">
          Your skills
        </h2>
        <SkillsSection
          accessToken={accessToken}
          refreshKey={externalVersion}
          onChanged={handleSkillsChanged}
        />
      </div>
    </main>
  );
}
